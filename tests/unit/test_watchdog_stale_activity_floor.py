"""TDD spec for WDFLOOR-1: floor `activity_age` at `elapsed` in
check_story_status's stale-activity watchdog rule.

Context: a story redispatched into a reused worktree (any rework: status
changes_requested or interrupted takes dispatch_story's `resuming` path)
inherits the previous attempt's agent.log mtime. collect_story_wedge_signals
measures the age of the worktree's last activity, not the age of the current
dispatch, so a freshly launched agent can be killed on the very first poll of
the tick it was dispatched in - observed live on LOCKSTARVE-B3: dispatched
23:19:29.303, killed 23:19:29.409 (106ms later), with the watchdog summary
itself stating the contradiction: "no activity for 4851s ... elapsed 0s".

The fix clamps `activity_age = min(activity_age, elapsed)` before the rule's
`if activity_age is not None and activity_age > DISPATCH_STALE_ACTIVITY_SECONDS`
check - activity cannot be staler than the dispatch that produced it. This
file exercises that clamp in isolation; it does not touch pipeline/wedge_io.py
or the wall-clock/backstop rule, and does not repeat the full behavior-order
coverage already in test_story_status_activity_watchdog.py.

Fixture pattern (helpers, watchdog_env) mirrored from that file rather than
imported, per that file's own style of building local copies.
"""

import json
import os
import re
import time
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

# Import server BEFORE story_status: story_status rebinds check_story_status
# against pipeline.server's namespace, and importing story_status first trips
# the module-level import cycle (story_status <-> server).
from pipeline import server as p
from pipeline import story_status

WATCHDOG_SECONDS = 3600
STALE_SECONDS = 1800

STORY_KEY = "story-1"

STALE_SUMMARY_RE = (
    r"no activity for (\d+)s \(stale-activity watchdog\); "
    r"elapsed (\d+)s; process terminated\."
)
BACKSTOP_SUMMARY_RE = r"no completion after (\d+)s; process terminated\."


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _make_story(tmp_path, *, dispatched_seconds_ago, log_age_seconds=None):
    """Build an in_progress story with a live pid and a past dispatched_at.

    ``log_age_seconds=None`` leaves the worktree without an agent.log, so
    collect_story_wedge_signals reports activity_age_seconds=None. Otherwise
    the agent.log mtime is set exactly ``log_age_seconds`` seconds in the
    past - simulating a reused worktree carrying a prior attempt's stale log
    when this is larger than ``dispatched_seconds_ago``.
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
    """Redirect PLAN_DIR at pipeline.server, stub the thresholds on
    pipeline.server (the rebound body resolves them there at call time), and
    stub the external boundaries (terminate spy, rebrief no-op, fake ps)."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    plan_name = f"plan-floor-{uuid.uuid4().hex[:8]}"

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
    # the worktree - true external boundaries. With pid=os.getpid() an
    # un-stubbed terminate would SIGTERM the test process itself.
    monkeypatch.setattr(p, "_terminate_and_checkpoint", _spy_terminate)
    monkeypatch.setattr(p, "_rebrief_step_cap_struggle", lambda *a, **k: None)

    class _FakePSResult:
        stdout = " S"  # alive, not a zombie
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _FakePSResult())

    return types.SimpleNamespace(
        plan_dir=plan_dir,
        plan_name=plan_name,
        terminate_calls=terminate_calls,
    )


# --------------------------------------------------------------------------
# positive
# --------------------------------------------------------------------------
def test_fresh_dispatch_with_ancient_log_keeps_running(watchdog_env, tmp_path):
    """Regression guard for the live LOCKSTARVE-B3 incident: a dispatch that
    just launched (elapsed ~0s) must never be killed because it reused a
    worktree whose agent.log carries a mtime from a long-dead prior attempt.
    This case MUST fail before the production fix (it kills the story)."""
    story = _make_story(tmp_path, dispatched_seconds_ago=0, log_age_seconds=5000)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {"status": "running", "pid": os.getpid()}
    assert watchdog_env.terminate_calls == []


def test_genuinely_stale_dispatch_still_terminates(watchdog_env, tmp_path):
    """The clamp must not disable the watchdog: a dispatch that has itself
    been running long enough, with activity also stale relative to that
    elapsed time, still terminates with the stale-activity summary."""
    story = _make_story(tmp_path, dispatched_seconds_ago=7200,
                        log_age_seconds=4000)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {
        "status": "interrupted",
        "pid": os.getpid(),
        "watchdog_killed": True,
    }
    assert len(watchdog_env.terminate_calls) == 1
    call = watchdog_env.terminate_calls[0]
    assert call["step"] == "dispatch_watchdog_timeout"
    assert re.search(STALE_SUMMARY_RE, call["summary"] or ""), call["summary"]


# --------------------------------------------------------------------------
# negative / boundary
# --------------------------------------------------------------------------
def test_boundary_just_under_stale_threshold_keeps_running(watchdog_env, tmp_path):
    """dispatched_seconds_ago slightly BELOW DISPATCH_STALE_ACTIVITY_SECONDS,
    with an ancient log: elapsed clamps activity_age below the threshold, so
    the story keeps running."""
    story = _make_story(tmp_path, dispatched_seconds_ago=STALE_SECONDS - 20,
                        log_age_seconds=5000)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {"status": "running", "pid": os.getpid()}
    assert watchdog_env.terminate_calls == []


def test_boundary_just_over_stale_threshold_terminates_with_bounded_age(
        watchdog_env, tmp_path):
    """dispatched_seconds_ago slightly ABOVE DISPATCH_STALE_ACTIVITY_SECONDS,
    with an equally ancient log: terminates, and the reported "no activity
    for Ns" figure must never exceed elapsed - the exact property the clamp
    guarantees."""
    elapsed_seconds = STALE_SECONDS + 20
    story = _make_story(tmp_path, dispatched_seconds_ago=elapsed_seconds,
                        log_age_seconds=5000)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result["watchdog_killed"] is True
    assert len(watchdog_env.terminate_calls) == 1
    call = watchdog_env.terminate_calls[0]
    match = re.search(STALE_SUMMARY_RE, call["summary"] or "")
    assert match, f"expected stale summary, got: {call['summary']!r}"
    reported_age = int(match.group(1))
    reported_elapsed = int(match.group(2))
    assert reported_age <= reported_elapsed, (
        f"reported age {reported_age}s must never exceed elapsed "
        f"{reported_elapsed}s - that is the property the clamp guarantees"
    )


def test_missing_activity_signal_past_ceiling_still_uses_wall_clock_backstop(
        watchdog_env, tmp_path):
    """activity_age is None (no agent.log at all) past the wall-clock
    ceiling: the backstop rule must still terminate exactly as today,
    proving the clamp did not leak into the `activity_age is None` path."""
    story = _make_story(tmp_path, dispatched_seconds_ago=7200,
                        log_age_seconds=None)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {
        "status": "interrupted",
        "pid": os.getpid(),
        "watchdog_killed": True,
    }
    assert len(watchdog_env.terminate_calls) == 1
    call = watchdog_env.terminate_calls[0]
    assert call["step"] == "dispatch_watchdog_timeout"
    assert "stale-activity" not in (call["summary"] or "")
    assert re.search(BACKSTOP_SUMMARY_RE, call["summary"] or ""), call["summary"]


def test_fresh_log_long_running_dispatch_keeps_running(watchdog_env, tmp_path):
    """Fresh log, long-running dispatch: unaffected by the clamp since
    activity_age (0) is already far below elapsed (7200)."""
    story = _make_story(tmp_path, dispatched_seconds_ago=7200, log_age_seconds=0)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {"status": "running", "pid": os.getpid()}
    assert watchdog_env.terminate_calls == []
