"""TDD spec for the stale-activity dispatch watchdog (check_story_status).

Context: the dispatch watchdog killed stories purely on wall-clock elapsed
time (DISPATCH_WATCHDOG_SECONDS), terminating live dispatches that were
making steady progress. The repo already measures per-story activity via
pipeline.wedge_io.collect_story_wedge_signals (newest mtime across the story
journal and <worktree>/agent.log). This story rewires check_story_status's
watchdog branch to kill on STALE ACTIVITY first, keeping the wall-clock
ceiling only as an absolute backstop:

  1. activity_age_seconds is not None and > DISPATCH_STALE_ACTIVITY_SECONDS
     -> terminate-and-checkpoint exactly as today (same call, same step name
        'dispatch_watchdog_timeout'), but with the summary
        "no activity for {age:.0f}s (stale-activity watchdog); "
        "elapsed {elapsed:.0f}s; process terminated."
  2. elif elapsed > DISPATCH_WATCHDOG_SECONDS -> terminate exactly as today
     (backstop: kills a livelocked-but-heartbeating agent, and covers
     activity_age_seconds being None, e.g. within the startup grace).
  3. else -> do NOT terminate (the fix: fresh activity past the old 3600s
     ceiling keeps running).

Wiring contract graded here:
  * pipeline/config.py gains
        DISPATCH_STALE_ACTIVITY_SECONDS = int(os.environ.get(
            'PIPELINE_DISPATCH_STALE_ACTIVITY_SECONDS', '1800'))
    directly below DISPATCH_WATCHDOG_SECONDS, whose default stays '3600'.
  * pipeline/config_provenance.py gains EnvVarSpec(
        'PIPELINE_DISPATCH_STALE_ACTIVITY_SECONDS', '1800')
    adjacent to the existing PIPELINE_DISPATCH_WATCHDOG_SECONDS spec.
  * pipeline/story_status.py exports the new constant into pipeline.server's
    namespace AFTER the types.FunctionType rebinding block, mirroring the
    existing DETACHED_GRADE_WATCHDOG_SECONDS export:
        _server.DISPATCH_STALE_ACTIVITY_SECONDS = DISPATCH_STALE_ACTIVITY_SECONDS

REBINDING TRAP (why every stub below is applied to pipeline.server):
check_story_status is rebound at the bottom of story_status.py via
types.FunctionType(check_story_status.__code__, _server.__dict__, ...), so
EVERY bare name in its body (the thresholds, _store, subprocess,
_terminate_and_checkpoint, _rebrief_step_cap_struggle, ...) resolves against
pipeline.server's namespace at call time. Monkeypatching the story_status
module namespace is a SILENT NO-OP for the call sites inside
check_story_status. All stubs therefore go on ``p`` (pipeline.server),
mirroring how the existing tests patch ``p.PLAN_DIR``.
"""

import json
import os
import re
import subprocess
import sys
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
from pipeline import story_status

REPO_ROOT = Path(__file__).resolve().parents[2]
STORY_STATUS_SRC = REPO_ROOT / "pipeline" / "story_status.py"
CONFIG_SRC = REPO_ROOT / "pipeline" / "config.py"
PROVENANCE_SRC = REPO_ROOT / "pipeline" / "config_provenance.py"

# Stubbed thresholds — the resolution tests must never depend on today's
# configured (env/plist) values, only on these module-namespace stubs.
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
def _find_line(lines, regex):
    """Return the 0-based index of the first line matching ``regex``, else None."""
    compiled = re.compile(regex)
    for idx, line in enumerate(lines):
        if compiled.search(line):
            return idx
    return None


def _make_story(tmp_path, *, dispatched_seconds_ago, log_age_seconds=None):
    """Build an in_progress story with a live pid and a past dispatched_at.

    ``log_age_seconds=None`` leaves the worktree without an agent.log (and no
    journal exists under the tmp PLAN_DIR), so collect_story_wedge_signals
    reports activity_age_seconds=None. Otherwise the agent.log mtime is set
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
    stub the external boundaries (terminate spy, rebrief no-op, fake ps).

    ``monkeypatch.setattr(p, "DISPATCH_STALE_ACTIVITY_SECONDS", ...)`` uses
    default raising=True on purpose: before the implementation exists (or if
    the ``_server.DISPATCH_STALE_ACTIVITY_SECONDS = ...`` export is missing)
    every behavior test errors with a clear AttributeError pointing at the
    missing wiring, instead of silently passing against a fixture-supplied
    name.
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
    # the worktree — true external boundaries. With pid=os.getpid() an
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
# behavior: the decision order inside check_story_status's watchdog branch
# --------------------------------------------------------------------------
def test_fresh_activity_past_old_ceiling_keeps_running(watchdog_env, tmp_path):
    """THE FIX: fresh activity + elapsed past the old 3600s ceiling keeps
    running (no terminate). Proves the stale-activity signal is consulted
    BEFORE the elapsed check."""
    story = _make_story(tmp_path, dispatched_seconds_ago=7200, log_age_seconds=0)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {"status": "running", "pid": os.getpid()}
    assert watchdog_env.terminate_calls == []


def test_fresh_activity_within_ceiling_keeps_running(watchdog_env, tmp_path):
    """Happy path: fresh activity, well inside the ceiling -> running."""
    story = _make_story(tmp_path, dispatched_seconds_ago=60, log_age_seconds=0)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {"status": "running", "pid": os.getpid()}
    assert watchdog_env.terminate_calls == []


def test_stale_activity_terminates_with_stale_watchdog_summary(
        watchdog_env, tmp_path):
    """Rule 1: stale activity -> terminate with the NEW stale-activity
    summary, same step name as today."""
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
    assert call["plan_name"] == watchdog_env.plan_name
    assert call["story_key"] == STORY_KEY
    assert call["pid"] == os.getpid()
    assert call["step"] == "dispatch_watchdog_timeout"
    match = re.search(STALE_SUMMARY_RE, call["summary"] or "")
    assert match, f"summary missing stale-activity format: {call['summary']!r}"
    reported_age = int(match.group(1))
    reported_elapsed = int(match.group(2))
    assert 3950 <= reported_age <= 4400, reported_age
    assert 7150 <= reported_elapsed <= 7400, reported_elapsed


def test_stale_activity_terminates_even_within_wall_clock_ceiling(
        watchdog_env, tmp_path):
    """Rule 1 has no elapsed gate: stale activity terminates even when
    elapsed is well under the wall-clock ceiling."""
    story = _make_story(tmp_path, dispatched_seconds_ago=60,
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


def test_activity_just_over_stale_threshold_terminates_as_stale(
        watchdog_env, tmp_path):
    """Boundary (just over): age slightly > STALE_SECONDS -> stale terminate."""
    story = _make_story(tmp_path, dispatched_seconds_ago=7200,
                        log_age_seconds=1820)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result["watchdog_killed"] is True
    assert len(watchdog_env.terminate_calls) == 1
    call = watchdog_env.terminate_calls[0]
    match = re.search(STALE_SUMMARY_RE, call["summary"] or "")
    assert match, f"expected stale summary, got: {call['summary']!r}"
    assert int(match.group(1)) >= STALE_SECONDS


def test_activity_just_under_stale_threshold_keeps_running(
        watchdog_env, tmp_path):
    """Boundary (just under): age slightly <= STALE_SECONDS is NOT stale, so
    Rule 1 doesn't fire. Rule 2's backstop is documented as covering
    "activity_age_seconds being None" (unknown liveness) - it must NOT
    override a KNOWN, not-yet-stale activity signal just because elapsed is
    also past the ceiling, or the whole feature's stated purpose ("kill on
    stale activity INSTEAD OF absolute wall clock... terminating live
    dispatches that were making steady progress") is defeated for any story
    whose activity happens to sit just under the stale threshold.

    Corrected 2026-09-05: this test previously asserted the opposite
    (backstop fires here) via `activity_age is None` removed from Rule 2's
    guard, which broke the sibling `test_fresh_activity_past_old_ceiling_
    keeps_running` - that test is unambiguously the feature's own headline
    "THE FIX" case (fresh/known-non-stale activity must survive an elapsed
    ceiling that would otherwise kill it), so this boundary case must agree
    with it rather than the reverse."""
    story = _make_story(tmp_path, dispatched_seconds_ago=7200,
                        log_age_seconds=1780)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {"status": "running", "pid": os.getpid()}
    assert watchdog_env.terminate_calls == []


def test_missing_activity_signal_past_ceiling_terminates_as_today(
        watchdog_env, tmp_path):
    """activity_age_seconds is None (no readable journal/agent.log) ->
    fall back to wall-clock semantics only: elapsed > ceiling terminates
    exactly as today (old summary)."""
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


def test_missing_activity_signal_within_ceiling_does_not_kill(
        watchdog_env, tmp_path):
    """None activity age + elapsed UNDER the ceiling must NOT kill (never
    kill on None + elapsed<ceiling)."""
    story = _make_story(tmp_path, dispatched_seconds_ago=60,
                        log_age_seconds=None)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {"status": "running", "pid": os.getpid()}
    assert watchdog_env.terminate_calls == []


def test_elapsed_just_under_ceiling_with_fresh_activity_does_not_terminate(
        watchdog_env, tmp_path):
    """Boundary (elapsed just under the ceiling): fresh activity -> running."""
    story = _make_story(tmp_path, dispatched_seconds_ago=3580,
                        log_age_seconds=0)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {"status": "running", "pid": os.getpid()}
    assert watchdog_env.terminate_calls == []


def test_missing_dispatched_at_still_returns_running_without_terminating(
        watchdog_env, tmp_path):
    """Negative case: a dispatched story without dispatched_at skips the
    watchdog branch entirely and must keep returning running (out-of-scope
    branch must not be disturbed by the rewiring)."""
    story = _make_story(tmp_path, dispatched_seconds_ago=7200,
                        log_age_seconds=None)
    del story["dispatched_at"]
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(watchdog_env.plan_name, STORY_KEY)

    assert result == {"status": "running", "pid": os.getpid()}
    assert watchdog_env.terminate_calls == []


# --------------------------------------------------------------------------
# wiring: config.py constant
# --------------------------------------------------------------------------
def test_config_declares_stale_activity_constant_directly_below_watchdog():
    """config.py gains DISPATCH_STALE_ACTIVITY_SECONDS =
    int(os.environ.get('PIPELINE_DISPATCH_STALE_ACTIVITY_SECONDS', '1800'))
    directly below DISPATCH_WATCHDOG_SECONDS, whose default stays 3600."""
    lines = CONFIG_SRC.read_text(encoding="utf-8").splitlines()

    watchdog_idx = _find_line(lines, r"^DISPATCH_WATCHDOG_SECONDS\s*=")
    stale_idx = _find_line(lines, r"^DISPATCH_STALE_ACTIVITY_SECONDS\s*=")
    assert watchdog_idx is not None, "DISPATCH_WATCHDOG_SECONDS assignment missing"
    assert stale_idx is not None, (
        "DISPATCH_STALE_ACTIVITY_SECONDS assignment missing from config.py"
    )
    assert 0 < stale_idx - watchdog_idx <= 3, (
        f"DISPATCH_STALE_ACTIVITY_SECONDS must be added directly below "
        f"DISPATCH_WATCHDOG_SECONDS (watchdog line {watchdog_idx + 1}, "
        f"stale line {stale_idx + 1})"
    )

    assert re.search(
        r"int\(\s*os\.environ\.get\(\s*['\"]"
        r"PIPELINE_DISPATCH_STALE_ACTIVITY_SECONDS"
        r"['\"]\s*,\s*['\"]1800['\"]\s*\)\s*\)",
        lines[stale_idx],
    ), f"wrong assignment form: {lines[stale_idx]!r}"

    # The operator plist overrides the watchdog via env; the config default
    # must stay hermetic at 3600.
    assert re.search(r"['\"]3600['\"]", lines[watchdog_idx]), (
        f"DISPATCH_WATCHDOG_SECONDS default must stay 3600 in code, "
        f"got: {lines[watchdog_idx]!r}"
    )


def test_config_stale_activity_constant_reads_env_at_import():
    """The new constant must be an int built from the env var at import time:
    default 1800 when unset, honoring an override when set (hermetic
    subprocess, never the test process's live config)."""
    env = dict(os.environ)
    env.pop("PIPELINE_DISPATCH_STALE_ACTIVITY_SECONDS", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    code = (
        "from pipeline.config import DISPATCH_STALE_ACTIVITY_SECONDS as v; "
        "print(v)"
    )

    default_run = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert default_run.returncode == 0, default_run.stderr
    assert default_run.stdout.strip() == "1800"

    override_run = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=dict(env, PIPELINE_DISPATCH_STALE_ACTIVITY_SECONDS="900"),
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert override_run.returncode == 0, override_run.stderr
    assert override_run.stdout.strip() == "900"


# --------------------------------------------------------------------------
# wiring: config_provenance.py registry entry
# --------------------------------------------------------------------------
def test_config_provenance_registers_stale_activity_spec_adjacent_to_watchdog():
    """One EnvVarSpec('PIPELINE_DISPATCH_STALE_ACTIVITY_SECONDS', '1800')
    entry, appended adjacent to (immediately after) the existing
    PIPELINE_DISPATCH_WATCHDOG_SECONDS spec. Membership + adjacency relative
    to the fixed anchor only — never the registry's total contents."""
    text = PROVENANCE_SRC.read_text(encoding="utf-8")
    spec_re = re.compile(
        r"EnvVarSpec\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]*)['\"]"
    )
    found = [
        (match.group(1), match.group(2), text[:match.start()].count("\n"))
        for match in spec_re.finditer(text)
    ]

    watchdog_line = next(
        (line for name, _default, line in found
         if name == "PIPELINE_DISPATCH_WATCHDOG_SECONDS"),
        None,
    )
    assert watchdog_line is not None, (
        "could not find the existing PIPELINE_DISPATCH_WATCHDOG_SECONDS "
        "EnvVarSpec in config_provenance.py"
    )

    stale_entries = [
        (default, line) for name, default, line in found
        if name == "PIPELINE_DISPATCH_STALE_ACTIVITY_SECONDS"
    ]
    assert stale_entries, (
        "PIPELINE_DISPATCH_STALE_ACTIVITY_SECONDS EnvVarSpec missing from "
        "config_provenance.py"
    )
    stale_default, stale_line = stale_entries[0]
    assert stale_default == "1800", stale_default
    assert stale_line > watchdog_line, (
        "the new spec must be appended after the watchdog spec"
    )
    assert stale_line - watchdog_line <= 4, (
        f"the new spec must be adjacent to the PIPELINE_DISPATCH_WATCHDOG_"
        f"SECONDS spec (watchdog at line {watchdog_line + 1}, "
        f"new at line {stale_line + 1})"
    )


# --------------------------------------------------------------------------
# wiring: story_status.py namespace export (the rebinding trap)
# --------------------------------------------------------------------------
def test_story_status_exports_stale_constant_into_server_namespace():
    """The rebound check_story_status resolves bare names against
    pipeline.server's namespace, so the new threshold must be exported there
    AFTER the types.FunctionType rebinding block, right below the existing
    DETACHED_GRADE_WATCHDOG_SECONDS export. Without that export the wiring
    is dead on arrival even though the story_status-module constant exists."""
    lines = STORY_STATUS_SRC.read_text(encoding="utf-8").splitlines()

    rebind_idx = _find_line(lines, r"types\.FunctionType\(")
    assert rebind_idx is not None, "rebinding block missing"

    detached_idx = _find_line(
        lines, r"_server\.DETACHED_GRADE_WATCHDOG_SECONDS\s*="
    )
    assert detached_idx is not None, "DETACHED export anchor missing"

    export_idx = _find_line(
        lines,
        r"_server\.DISPATCH_STALE_ACTIVITY_SECONDS\s*=\s*"
        r"DISPATCH_STALE_ACTIVITY_SECONDS",
    )
    assert export_idx is not None, (
        "story_status.py must export the new constant into pipeline.server's "
        "namespace: _server.DISPATCH_STALE_ACTIVITY_SECONDS = "
        "DISPATCH_STALE_ACTIVITY_SECONDS"
    )
    assert export_idx > rebind_idx, (
        "the export must come after the types.FunctionType rebinding block"
    )
    assert 0 < export_idx - detached_idx <= 3, (
        "the export must sit right below the DETACHED_GRADE_WATCHDOG_SECONDS "
        "export"
    )

    # Runtime half of the same contract: the constant is reachable in (and
    # consistent between) both namespaces after import.
    assert hasattr(p, "DISPATCH_STALE_ACTIVITY_SECONDS"), (
        "pipeline.server must carry DISPATCH_STALE_ACTIVITY_SECONDS for the "
        "rebound check_story_status body to resolve"
    )
    assert hasattr(story_status, "DISPATCH_STALE_ACTIVITY_SECONDS")
    assert (p.DISPATCH_STALE_ACTIVITY_SECONDS
            == story_status.DISPATCH_STALE_ACTIVITY_SECONDS)


def test_story_status_pulls_collect_story_wedge_signals_from_wedge_io():
    """The watchdog branch must consult the existing wedge-signal reader:
    story_status.py references collect_story_wedge_signals and imports it
    from pipeline.wedge_io (wedge_io.py itself must not be touched)."""
    src = STORY_STATUS_SRC.read_text(encoding="utf-8")
    assert "collect_story_wedge_signals" in src, (
        "check_story_status must call collect_story_wedge_signals"
    )
    assert re.search(r"wedge_io", src), (
        "collect_story_wedge_signals must be imported from pipeline.wedge_io"
    )


def test_wedge_signal_reader_contract_unchanged(tmp_path):
    """Seam guard: wedge_io.collect_story_wedge_signals keeps its
    {'pid_alive', 'activity_age_seconds'} contract (this story must NOT
    modify pipeline/wedge_io.py)."""
    from pipeline.wedge_io import collect_story_wedge_signals

    signals = collect_story_wedge_signals(
        "plan-alpha", STORY_KEY,
        {"pid": os.getpid(), "worktree": str(tmp_path)},
    )
    assert set(signals) == {"pid_alive", "activity_age_seconds"}