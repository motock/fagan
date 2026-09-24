"""TDD spec: a dispatch-watchdog kill must be recorded as a rework event.

Context: when the dispatch watchdog kills a story, the story is checkpointed
and re-dispatched - a genuine rework cycle.  Today the kill path emits no
notification record at all, so ``compute_story_metrics`` never counts it:
``rework_cycles`` and ``cost`` are under-reported for every killed story and
``cost_per_merged_story`` in the plan report is wrong-low.  There is also no
record an operator can read.

Two edits are graded here:

  1. pipeline/story_metrics.py: ``_REWORK_EVENTS`` gains
     ``"dispatch_watchdog_timeout"``.  A kill + re-dispatch IS a rework cycle,
     so it counts toward ``rework_cycles``/``cost`` - but it is NOT a
     first-pass disqualifier (a kill is usually environment/activity-caused,
     not a wrong first attempt), exactly as the standing invariant comment
     above ``_FIRST_PASS_DISQUALIFYING_EVENTS`` says about rework events.
  2. pipeline/story_status.py: ONE ``_notify_user`` call inside the watchdog
     kill branch, immediately AFTER ``_terminate_and_checkpoint(...)`` and
     BEFORE ``_rebrief_step_cap_struggle(...)``, with
     ``severity="warning"``, ``event="dispatch_watchdog_timeout"`` and the
     file's existing conditional ``correlation_id`` spread.

REBINDING TRAP (why every stub below is applied to pipeline.server):
``check_story_status`` is rebound at the bottom of story_status.py via
``types.FunctionType(check_story_status.__code__, _server.__dict__, ...)``, so
EVERY bare name in its body resolves against ``pipeline.server``'s namespace
at call time.  Monkeypatching the story_status module namespace is a SILENT
NO-OP for the call sites inside ``check_story_status``.  All stubs therefore
go on ``p`` (pipeline.server).
"""

import json
import os
import re
import time
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Import server BEFORE story_status: story_status rebinds check_story_status
# against pipeline.server's namespace, and importing story_status first trips
# the module-level import cycle (story_status <-> server).
from pipeline import server as p
from pipeline import story_metrics, story_status

REPO_ROOT = Path(__file__).resolve().parents[2]
STORY_STATUS_SRC = REPO_ROOT / "pipeline" / "story_status.py"

WATCHDOG_SECONDS = 3600
STALE_SECONDS = 1800

STORY_KEY = "story-1"
EVENT = "dispatch_watchdog_timeout"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _make_story(tmp_path, *, dispatched_seconds_ago, log_age_seconds=None,
                correlation_id=None):
    """Build an in_progress story with a live pid and a past dispatched_at.

    ``log_age_seconds=None`` leaves the worktree without an agent.log (and no
    journal exists under the tmp PLAN_DIR), so collect_story_wedge_signals
    reports activity_age_seconds=None.  Otherwise the agent.log mtime is set
    exactly ``log_age_seconds`` seconds in the past.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    story = {
        "status": "in_progress",
        "pid": os.getpid(),  # guaranteed-live pid; terminate is always stubbed
        "dispatched_at": (
            datetime.now(timezone.utc) - timedelta(seconds=dispatched_seconds_ago)
        ).isoformat(),
        "worktree": str(worktree),
    }
    if correlation_id is not None:
        story["correlation_id"] = correlation_id
    if log_age_seconds is not None:
        agent_log = worktree / "agent.log"
        agent_log.write_text("step 1 ok\n", encoding="utf-8")
        mtime = time.time() - log_age_seconds
        os.utime(agent_log, (mtime, mtime))
    return story


def _write_manifest(plan_dir, plan_name, story):
    """Write a real manifest for the store to read from the patched PLAN_DIR."""
    manifest = {
        "name": plan_name,
        "stories": {STORY_KEY: story},
        "role_config": {},
    }
    manifest_path = p._store.manifest_path(plan_name)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------
@pytest.fixture()
def watchdog_env(tmp_path, monkeypatch):
    """Redirect PLAN_DIR at pipeline.server, stub the thresholds there (the
    rebound body resolves them there at call time), and stub the external
    boundaries (terminate spy, rebrief no-op, notify recorder, fake ps).
    """
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    plan_name = f"plan-alpha-{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(p, "DISPATCH_WATCHDOG_SECONDS", WATCHDOG_SECONDS)
    monkeypatch.setattr(p, "DISPATCH_STALE_ACTIVITY_SECONDS", STALE_SECONDS)

    terminate_calls = []

    def _spy_terminate(manifest, manifest_path, plan_name, story_key, story,
                       pid=None, step=None, summary=None):
        terminate_calls.append({
            "plan_name": plan_name,
            "story_key": story_key,
            "pid": pid,
            "step": step,
            "summary": summary,
        })

    # Real _terminate_and_checkpoint SIGTERMs the story pid and git-commits
    # the worktree - true external boundaries.  With pid=os.getpid() an
    # un-stubbed terminate would SIGTERM the test process itself.
    monkeypatch.setattr(p, "_terminate_and_checkpoint", _spy_terminate)
    monkeypatch.setattr(p, "_rebrief_step_cap_struggle", lambda *a, **k: None)

    notify_calls = []

    def _recorder(plan_name, message, **kwargs):
        notify_calls.append({
            "plan_name": plan_name,
            "message": message,
            "kwargs": kwargs,
        })

    monkeypatch.setattr(p, "_notify_user", _recorder)

    class _FakePSResult:
        stdout = " S"  # alive, not a zombie
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _FakePSResult())

    return types.SimpleNamespace(
        plan_dir=plan_dir,
        plan_name=plan_name,
        terminate_calls=terminate_calls,
        notify_calls=notify_calls,
    )


def _watchdog_records(env):
    """The notify calls that carry the watchdog rework event."""
    return [c for c in env.notify_calls if c["kwargs"].get("event") == EVENT]


# --------------------------------------------------------------------------
# behavior: the kill path emits exactly one rework record
# --------------------------------------------------------------------------
def test_stale_activity_kill_records_a_rework_event(watchdog_env, tmp_path):
    """Stale-activity kill -> exactly one notify record, warning severity,
    the story key, and no correlation_id key (the story has none)."""
    story = _make_story(tmp_path, dispatched_seconds_ago=7200,
                        log_age_seconds=4000)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result["watchdog_killed"] is True
    assert len(watchdog_env.terminate_calls) == 1
    records = _watchdog_records(watchdog_env)
    assert len(records) == 1, watchdog_env.notify_calls
    record = records[0]
    assert record["plan_name"] == watchdog_env.plan_name
    assert record["kwargs"]["story_key"] == STORY_KEY
    assert record["kwargs"]["severity"] == "warning"
    # The conditional spread must OMIT the key, never pass None.
    assert "correlation_id" not in record["kwargs"]


def test_backstop_kill_records_the_same_event(watchdog_env, tmp_path):
    """Unknown activity signal (no agent.log) + elapsed over the wall-clock
    ceiling -> the same single record."""
    story = _make_story(tmp_path, dispatched_seconds_ago=7200,
                        log_age_seconds=None)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result["watchdog_killed"] is True
    assert len(watchdog_env.terminate_calls) == 1
    records = _watchdog_records(watchdog_env)
    assert len(records) == 1, watchdog_env.notify_calls
    assert records[0]["kwargs"]["story_key"] == STORY_KEY
    assert records[0]["kwargs"]["severity"] == "warning"


def test_fresh_activity_records_nothing(watchdog_env, tmp_path):
    """Negative case: a story with fresh activity inside the ceiling does not
    terminate and produces no watchdog record."""
    story = _make_story(tmp_path, dispatched_seconds_ago=60, log_age_seconds=0)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {"status": "running", "pid": os.getpid()}
    assert watchdog_env.terminate_calls == []
    assert _watchdog_records(watchdog_env) == []


def test_kill_record_carries_the_story_correlation_id(watchdog_env, tmp_path):
    """Boundary: when the story HAS a correlation_id the spread must include
    it verbatim (the other half of the conditional-spread idiom)."""
    story = _make_story(tmp_path, dispatched_seconds_ago=7200,
                        log_age_seconds=4000, correlation_id="corr-abc")
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    records = _watchdog_records(watchdog_env)
    assert len(records) == 1, watchdog_env.notify_calls
    assert records[0]["kwargs"]["correlation_id"] == "corr-abc"


# --------------------------------------------------------------------------
# metrics: the kill is paid for in cost, never a dirty first pass
# --------------------------------------------------------------------------
def test_rework_event_counts_toward_cost_but_never_disqualifies_first_pass():
    """The reconciliation this story exists for: the kill is paid for in the
    cost metric without being called a dirty first pass."""
    records = [
        {"event": EVENT, "story_key": STORY_KEY, "severity": "warning"},
        {"event": "story_merged", "story_key": STORY_KEY},
    ]

    metrics = story_metrics.compute_story_metrics(records)

    group = metrics[STORY_KEY]
    assert group["rework_cycles"] == 1
    assert group["cost"] == 2
    assert group["disqualifying_events"] == 0
    assert group["first_pass_clean"] is True


def test_event_is_not_first_pass_disqualifying():
    """Membership, not exact contents: later stories may extend the set."""
    assert EVENT in story_metrics._REWORK_EVENTS
    # review_changes_requested is the one deliberate exception (a reviewer bounce
    # is a dirty first pass); every other rework event must stay non-disqualifying.
    assert (story_metrics._REWORK_EVENTS - {"review_changes_requested"}).isdisjoint(
        story_metrics._FIRST_PASS_DISQUALIFYING_EVENTS
    )
    # The four pre-existing members must survive.
    assert {
        "tests_failed",
        "merge_ci_rework",
        "merge_gate_retry",
        "merge_retry",
    } <= story_metrics._REWORK_EVENTS


# --------------------------------------------------------------------------
# structure: ONE call, in the right place, with the noqa the rebinding needs
# --------------------------------------------------------------------------
def test_notify_call_sits_between_checkpoint_and_rebrief():
    """Exactly one ``event="dispatch_watchdog_timeout"`` emit, placed after
    ``_terminate_and_checkpoint`` and before ``_rebrief_step_cap_struggle``,
    carrying the ``# noqa: F821`` the rebound body requires."""
    lines = STORY_STATUS_SRC.read_text(encoding="utf-8").splitlines()

    assert sum('event="dispatch_watchdog_timeout"' in line for line in lines) == 1

    def _index(needle, after=-1):
        for i, line in enumerate(lines):
            if i > after and needle in line:
                return i
        raise AssertionError(f"no {needle!r} line after index {after}")

    terminate_idx = _index("_terminate_and_checkpoint(")
    notify_idx = _index("_notify_user(", after=terminate_idx)
    rebrief_idx = _index("_rebrief_step_cap_struggle(", after=notify_idx)
    assert terminate_idx < notify_idx < rebrief_idx
    assert "noqa: F821" in lines[notify_idx]
    assert re.search(r"event\s*=\s*[\"']" + EVENT + r"[\"']", "\n".join(lines))
