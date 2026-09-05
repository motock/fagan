"""Read-only acceptance oracle (do not modify): the activity-based watchdog
must be wired into the real check_story_status entrypoint, not a helper.

Fail-closed contract:
- fresh activity + elapsed past the ceiling -> check_story_status must NOT
  terminate (the story stays in_progress / running with its pid);
- stale activity -> the terminate path (dispatch_watchdog_timeout) fires.

Seam notes (why the fakes are what they are):
- pipeline.story_status imports pipeline.server at module level and
  pipeline.server imports check_story_status back from story_status, so
  server is imported first to break the cycle - same pattern as
  tests/unit/test_dead_pid_detached_grading.py.
- check_story_status is REBOUND at the bottom of story_status via
  types.FunctionType(code, _server.__dict__, ...) so its bare names
  (DISPATCH_WATCHDOG_SECONDS, _terminate_and_checkpoint,
  _rebrief_step_cap_struggle, _store, subprocess) resolve against
  pipeline.server's namespace at call time - NOT the story_status module's.
  Every stub here therefore monkeypatches pipeline.server (imported as p),
  which is also the seam existing story_status tests patch (p.PLAN_DIR).
- _terminate_and_checkpoint is stubbed with a recording spy (its real body
  SIGTERMs the story pid and git-commits the worktree - both true external
  boundaries) and _rebrief_step_cap_struggle is stubbed as a no-op; the
  decision logic under test (collect_story_wedge_signals -> stale check ->
  terminate-or-not) still runs unstubbed inside check_story_status.
"""

import json
import os
import time

from pipeline import server as p
from pipeline import story_status as ss


def _setup(tmp_path, monkeypatch, plan, story_key, journal_age_seconds):
    """Manifest + journal fixture; returns the list of recorded terminate
    steps. The story pid is os.getpid() (alive) and ps is faked to 'S'."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)

    story = {
        "summary": "oracle",
        "status": "in_progress",
        "pid": os.getpid(),
        "dispatched_at": "2026-01-01T00:00:00+00:00",
        "worktree": str(tmp_path / "wt"),
    }
    (plan_dir / (plan + ".manifest.json")).write_text(
        json.dumps({"epics": {}, "stories": {story_key: story}})
    )
    journal = plan_dir / (plan + "." + story_key + ".journal.json")
    journal.write_text("[]")
    if journal_age_seconds is not None:
        old = time.time() - journal_age_seconds
        os.utime(journal, (old, old))

    class _FakePs:
        returncode = 0
        stdout = "S"
        stderr = ""

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _FakePs())

    terminations = []

    def fake_terminate(manifest, manifest_path, plan_name, skey, st, *,
                       pid, step, summary):
        terminations.append(step)
        return "fakesha"

    monkeypatch.setattr(p, "_terminate_and_checkpoint", fake_terminate)
    monkeypatch.setattr(
        p, "_rebrief_step_cap_struggle", lambda *a, **k: None, raising=False)

    # Stub the thresholds in the namespace the rebound check_story_status
    # actually reads: pipeline.server's. raising=False keeps the file
    # collectable while the new constant is still RED.
    monkeypatch.setattr(p, "DISPATCH_WATCHDOG_SECONDS", 3600, raising=False)
    monkeypatch.setattr(
        p, "DISPATCH_STALE_ACTIVITY_SECONDS", 1800, raising=False)
    return terminations


def test_watchdog_prefers_fresh_activity_over_wall_clock(tmp_path, monkeypatch):
    """THE regression guard: fresh activity + elapsed far past the ceiling
    keeps running - the wall clock alone must no longer kill."""
    plan = "AW-ORACLE-FRESH"
    term = _setup(tmp_path, monkeypatch, plan, "oracle-story", 0)
    result = ss.check_story_status(plan, "oracle-story")
    assert term == [], f"fresh activity was terminated: {result!r}"
    assert result.get("status") != "interrupted"


def test_watchdog_terminates_on_stale_activity(tmp_path, monkeypatch):
    plan = "AW-ORACLE-STALE"
    term = _setup(tmp_path, monkeypatch, plan, "oracle-story", 2000)
    result = ss.check_story_status(plan, "oracle-story")
    assert term == ["dispatch_watchdog_timeout"], f"no terminate: {result!r}"
    assert result.get("status") == "interrupted"
