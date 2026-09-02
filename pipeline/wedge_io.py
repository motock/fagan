"""I/O gatherers for the wedge detector (pipeline/wedge.py).

``pipeline/wedge.py`` is PURE by committed contract
(tests/unit/test_wedge_verdict.py scans its imports and module bindings and
forbids os/subprocess/time/pathlib), so the measurement side of wedge
detection lives here: read-only, never raises, bounded cost.

``collect_story_wedge_signals`` measures one dispatched story's wedge
signals:

- ``pid_alive``: three-valued liveness. True (process alive), False (dead OR
  zombie -- pipeline/story_status.py establishes that os.kill(pid, 0)
  succeeds for defunct processes and only a ``ps -o stat=`` value starting
  with Z distinguishes them, so both collapse into False), or None (story
  has no int pid / liveness unknown). A string pid like "12345" is not
  trusted; liveness is never consulted for it.
- ``activity_age_seconds``: time.time() minus the NEWEST (max) mtime across
  the story's journal and its worktree's agent.log -- the most recent
  activity wins, so a fresh agent.log outranks an old journal -- or None
  when no source is readable (fail open, same convention as
  pipeline/usage.py's staleness helper). Negative ages (future mtime /
  clock skew) are reported as-is, never clamped.

Bounded cost per story: one ``ps -p`` subprocess (only for int pids) and at
most two stat calls. Stories with no pid and no readable activity files
short-circuit cheaply to ``{"pid_alive": None, "activity_age_seconds":
None}``.

NOTE: added by the dashboard-decoration story (8b11b51c) because its
committed contract imports ``collect_story_wedge_signals`` from
``pipeline.wedge`` (which re-exports it) while that module's purity test
forbids I/O imports there. The wedge-scan prerequisite story owns this
surface and supersedes it on merge.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

# PLAN_DIR is resolved at CALL time through the _ServerRef pattern (see
# pipeline/concurrency.py): a module-load copy would freeze the real
# ~/.claude/plans path into every test run, so
# monkeypatch.setattr(pipeline.server, "PLAN_DIR", ...) would never land.
from .concurrency import _ServerRef

PLAN_DIR = _ServerRef("PLAN_DIR")


def _pid_is_alive(pid: int) -> bool | None:
    """Three-valued liveness for a dispatched agent pid.

    Pattern copied from pipeline/story_status.py (liveness: os.kill(pid, 0),
    which succeeds for zombie/defunct processes too, then ``ps -p <pid> -o
    stat=`` where a stat starting with Z means zombie) and
    pipeline/concurrency.py (PermissionError means the process exists but
    belongs to another user, so treat it as alive rather than dead).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it (owned by another user).
        # Trust that it's alive, exactly like concurrency.py's slot accounting.
        return True
    # os.kill succeeds for zombie (defunct) processes too -- check ps stat.
    ps = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        check=False,
        capture_output=True,
        text=True,
    )
    stat = ps.stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _journal_path_for(plan_name: str, story_key: str, story: dict) -> Path | None:
    """Resolve the story's journal path, or None when it cannot be determined.

    The exact pattern is pipeline/store.py's journal helper:
    ``PLAN_DIR / f"{plan_name}.{story_key}.journal.json"``. Callers normally
    pass the manifest key; bare story dicts without one fall back to
    ``story["key"]``, then to the plan's single unambiguous journal, and only
    for dispatched (int-pid) stories -- a journal mtime says nothing about an
    agent that was never dispatched, and with several journals in the plan
    none can be attributed to a key-less story.
    """
    if isinstance(story_key, str) and story_key:
        return PLAN_DIR / f"{plan_name}.{story_key}.journal.json"
    key = story.get("key")
    if isinstance(key, str) and key:
        return PLAN_DIR / f"{plan_name}.{key}.journal.json"
    pid = story.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    try:
        matches = sorted(PLAN_DIR.glob(f"{plan_name}.*.journal.json"))
    except OSError:
        return None
    if len(matches) == 1:
        return matches[0]
    return None


def collect_story_wedge_signals(plan_name: str, story_key: str, story: dict) -> dict:
    """Measure one story's wedge signals. Read-only; never raises.

    Returns ``{"pid_alive": bool | None, "activity_age_seconds": float |
    None}`` (see module docstring for the three-valued semantics).
    """
    pid = story.get("pid")
    is_int_pid = isinstance(pid, int) and not isinstance(pid, bool)
    pid_alive = _pid_is_alive(pid) if is_int_pid else None

    now = time.time()
    newest_mtime: float | None = None

    journal = _journal_path_for(plan_name, story_key, story)
    if journal is not None:
        try:
            newest_mtime = journal.stat().st_mtime
        except OSError:
            pass  # missing / broken symlink -> treat as absent (fail open)

    worktree = story.get("worktree")
    if isinstance(worktree, str) and worktree:
        agent_log = Path(worktree) / "agent.log"
        try:
            mtime = agent_log.stat().st_mtime
        except OSError:
            pass  # missing / broken symlink -> treat as absent (fail open)
        else:
            newest_mtime = mtime if newest_mtime is None else max(newest_mtime, mtime)

    if newest_mtime is None:
        activity_age_seconds = None
    else:
        activity_age_seconds = now - newest_mtime

    return {
        "pid_alive": pid_alive,
        "activity_age_seconds": activity_age_seconds,
    }