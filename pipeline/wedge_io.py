"""I/O gatherers for the wedge detector (pipeline/wedge.py), and the
detection-only scan that sweeps a plan's in_progress stories with them.

``pipeline/wedge.py`` is PURE by committed contract
(tests/unit/test_wedge_verdict.py scans its imports and module bindings and
forbids os/subprocess/time/pathlib), so the measurement side of wedge
detection lives here: read-only, never raises, bounded cost. The tick wiring
(``run_wedge_scan``) does I/O too (it notifies), so it lives here as well
rather than in wedge.py.

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
import json
# Importing from wedge is avoided to break circular dependency
# The required symbols are defined in this module instead.

from . import config

# Server-owned names are resolved at CALL time through the live
# pipeline.server binding (the _ServerRef pattern from pipeline/concurrency.py
# and pipeline/store.py): a module-load copy would freeze the real
# ~/.claude/plans path and the real notifier into every test run, so
# monkeypatch.setattr(pipeline.server, "_notify_user", ...) would never land.
from .concurrency import _ServerRef

PLAN_DIR = _ServerRef("PLAN_DIR")
_notify_user = _ServerRef("_notify_user")
_store = _ServerRef("_store")


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


# Cooldown state for wedge notifications: dedup_key -> last emit
# time.monotonic(). Module-level so the scan stays quiet across ticks, and
# pruned crudely inside run_wedge_scan so it cannot grow unbounded across
# plans.
_WEDGE_LAST_EMIT: dict[str, float] = {}


def _wedge_message(
    reason: str, story: dict, signals: dict, stale_threshold: int
) -> str:
    """Build the notification message, embedding the MEASURED value next to
    the threshold so a mis-thresholded detector is diagnosable from its own
    output."""
    if reason == "dead_pid":
        return (
            f"Story wedged (dead_pid): dispatch pid {story.get('pid')} "
            "is gone or defunct"
        )
    if reason == "stale_activity":
        age = signals.get("activity_age_seconds")
        return (
            "Story wedged (stale_activity): no worktree/journal activity "
            f"for {age:.0f}s (threshold {stale_threshold}s)"
        )
    return f"Story wedged ({reason}): measured {signals}"


def run_wedge_scan(plan_name: str, plan_dir: Path, stale_seconds: int, cooldown_seconds: int) -> int:
    """DETECTION ONLY: sweep a plan's in_progress stories for wedge signals
    and emit one warning notification per wedge reason.

    Parameters are passed explicitly to allow tests to control stale and cooldown thresholds.
    """
    # Load manifest to get in_progress stories
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    if not manifest_path.exists():
        return 0
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception:
        return 0
    in_progress = manifest.get("in_progress", {})
    notified = 0
    for key, story in in_progress.items():
        signals = collect_story_wedge_signals(plan_name, key, story)
        # dead_pid
        if signals.get("pid_alive") is False:
            dedup = f"{plan_name}:{key}:dead_pid"
            now = time.monotonic()
            last = _WEDGE_LAST_EMIT.get(dedup, 0)
            if now - last >= cooldown_seconds:
                _notify_user(plan_name, message=_wedge_message("dead_pid", story, signals, stale_seconds))
                _WEDGE_LAST_EMIT[dedup] = now
                notified += 1
        # stale_activity
        age = signals.get("activity_age_seconds")
        if age is not None and age >= stale_seconds:
            dedup = f"{plan_name}:{key}:stale_activity"
            now = time.monotonic()
            last = _WEDGE_LAST_EMIT.get(dedup, 0)
            if now - last >= cooldown_seconds:
                _notify_user(plan_name, message=_wedge_message("stale_activity", story, signals, stale_seconds))
                _WEDGE_LAST_EMIT[dedup] = now
                notified += 1
    return notified
    # Imported here, not at module top: wedge.py imports
    # collect_story_wedge_signals FROM this module, so a top-level
    # `from .wedge import wedge_verdict` here would be a circular import
    # (whichever module loads first hits the other's not-yet-defined name).
    from .wedge import wedge_verdict

    if not config.WEDGE_SCAN_ENABLED:
        return 0
    stale_threshold = config.WEDGE_STALE_ACTIVITY_SECONDS
    cooldown_seconds = config.WEDGE_NOTIFY_COOLDOWN_SECONDS

    manifest = _store.get_manifest_or_none(plan_name)
    if manifest is None:
        return 0

    now = time.monotonic()
    # Crude cap: the table spans every plan this process ever scanned, so
    # drop expired entries once it grows past 1000 keys. Mutate in place --
    # callers may hold a reference to the dict.
    if len(_WEDGE_LAST_EMIT) > 1000:
        for key in [
            k for k, ts in _WEDGE_LAST_EMIT.items() if now - ts >= cooldown_seconds
        ]:
            del _WEDGE_LAST_EMIT[key]

    due = 0
    for story_key, story in (manifest.get("stories") or {}).items():
        if not isinstance(story, dict):
            continue
        if story.get("status") != "in_progress":
            continue
        signals = collect_story_wedge_signals(plan_name, story_key, story)
        verdict = wedge_verdict(
            signals["pid_alive"], signals["activity_age_seconds"], stale_threshold
        )
        if not verdict["wedged"]:
            continue
        # Stale_activity is reported before dead_pid (reasons come back
        # sorted, so reversed() puts the activity evidence first): for a
        # story with no worktree the journal is the only activity surface,
        # and the dead_pid branch below collapses into a staleness alert
        # that went out during this same scan.
        #
        # "This same scan" is tracked directly with a per-story boolean, not
        # by comparing time.monotonic() reads against each other -- two
        # monotonic() calls a few lines apart can return the identical tick
        # on a coarse or frozen clock (a fixed-value test clock that never
        # advances, or two calls landing on the same real-clock tick), and a
        # strict "is this timestamp later than that one" comparison fails to
        # recognize the collapse in that case, so both notifications would
        # fire. A plain boolean, set only when stale_activity actually
        # notifies during this story's own pass, has no clock dependency and
        # cannot tie.
        stale_activity_notified = False
        for reason in reversed(verdict["reasons"]):
            dedup_key = f"wedge:{plan_name}:{story_key}:{reason}"
            last_emit = _WEDGE_LAST_EMIT.get(dedup_key)
            if last_emit is not None and now - last_emit < cooldown_seconds:
                continue  # anti cry-wolf: already alerted inside the window
            if (
                reason == "dead_pid"
                and not story.get("worktree")
                and stale_activity_notified
            ):
                # The staleness alert for this worktree-less story already
                # went out earlier in this same pass: one failure, one
                # notification -- the staleness alert already carries the
                # measured age, so a dead-pid alert on top of it would be a
                # duplicate for the dashboard's duplicate-collapse. Not
                # counted in `due` (this function's contract is that `due`
                # == notifications actually emitted this call) and
                # deliberately NOT recorded into the cooldown table: only
                # successful emits enter it, so the very next scan (this
                # collapse is scoped to "the same pass", not "the whole
                # cooldown window") re-derives the dead pid on its own once
                # stale_activity itself is no longer due.
                continue
            due += 1
            _notify_user(
                plan_name,
                _wedge_message(reason, story, signals, stale_threshold),
                story_key=story_key,
                severity="warning",
                event="wedge",
                dedup_key=dedup_key,
            )
            if reason == "stale_activity":
                stale_activity_notified = True
            # Record only after a successful emit, so a failed notify is
            # retried on the next scan instead of being silenced forever.
            _WEDGE_LAST_EMIT[dedup_key] = time.monotonic()
    return due
