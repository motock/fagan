"""Detection-only stall scan (UC-04): a ready story that nobody dispatches.

``run_wedge_scan`` (pipeline/wedge_io.py) covers ``in_progress`` stories whose
pid died. Nothing covered the other failure mode: a story that is
dispatch-eligible but simply never picked up -- after the usage cap, stories
sat in ``interrupted`` and only a manual poll revealed it.

``run_stall_scan`` sweeps a plan for stories in ``interrupted``,
``changes_requested`` or ``todo`` whose dependencies are all done and whose
last transition is older than ``PIPELINE_STALL_SECONDS`` (default 1800), and
emits one ``story_stalled`` notification per story per threshold window.

DETECTION ONLY, exactly like ``run_wedge_scan``: this module never sets a
story status, never interrupts, never re-dispatches and never writes a file.
Recovery stays where it already lives (the tick's own dispatch loop).

The age comes from ``interrupted_at`` when the story carries one, otherwise
from the NEWEST mtime of the story's journal and its worktree's ``agent.log``
-- the same "most recent activity wins" convention as
``pipeline.wedge_io.collect_story_wedge_signals``. A story with no readable
signal at all is skipped (fail open): a missing timestamp is not evidence of
a stall.

The notification names WHY the story is not moving when that is knowable: the
dispatch backend's own gate reason (usage cooldown / usage gate / memory), or
``dispatch slots full`` when the gate is open, or ``unknown`` when the backend
lookup itself fails. The scan never raises.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

# Server-owned names resolve at CALL time through the live pipeline.server
# binding (the _ServerRef pattern from pipeline/concurrency.py): a module-load
# copy would freeze the real ~/.claude/plans path and the real notifier into
# every test run.
from .concurrency import _ServerRef

PLAN_DIR = _ServerRef("PLAN_DIR")
_notify_user = _ServerRef("_notify_user")
_store = _ServerRef("_store")
backend = _ServerRef("backend")
_completed_dep_ids = _ServerRef("_completed_dep_ids")

# Statuses the tick's dispatch loop treats as dispatch-eligible.
_STALL_STATUSES = ("interrupted", "changes_requested", "todo")

_DEFAULT_STALL_SECONDS = 1800

# Cooldown state: dedup_key -> last emit time.monotonic(). Module-level so the
# scan stays quiet across ticks, and pruned crudely inside run_stall_scan so it
# cannot grow unbounded across plans (mirrors _WEDGE_LAST_EMIT).
_STALL_LAST_EMIT: dict[str, float] = {}


def _stall_seconds() -> int:
    """The stall threshold, read from the environment at CALL time.

    A missing, non-integer or non-positive value falls back to the default
    rather than raising or latching a zero-length (useless) threshold.
    """
    raw = os.environ.get("PIPELINE_STALL_SECONDS")
    if raw is None:
        return _DEFAULT_STALL_SECONDS
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_STALL_SECONDS
    return seconds if seconds > 0 else _DEFAULT_STALL_SECONDS


def _scan_enabled() -> bool:
    """Kill switch, read at call time: ``PIPELINE_STALL_SCAN_ENABLED=0``."""
    return os.environ.get("PIPELINE_STALL_SCAN_ENABLED", "1") != "0"


def _parse_timestamp(value: object) -> float | None:
    """Epoch seconds for an ISO-8601 ``interrupted_at``, or None if unusable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _newest_activity_mtime(plan_name: str, story_key: str, story: dict) -> float | None:
    """Newest mtime across the story's journal and its worktree's agent.log."""
    newest: float | None = None
    journal = PLAN_DIR / f"{plan_name}.{story_key}.journal.json"
    try:
        newest = journal.stat().st_mtime
    except OSError:
        pass  # missing / broken symlink -> treat as absent (fail open)

    worktree = story.get("worktree")
    if isinstance(worktree, str) and worktree:
        try:
            mtime = (Path(worktree) / "agent.log").stat().st_mtime
        except OSError:
            pass  # missing / broken symlink -> treat as absent (fail open)
        else:
            newest = mtime if newest is None else max(newest, mtime)
    return newest


def _stall_age_seconds(
    plan_name: str, story_key: str, story: dict, now: float
) -> float | None:
    """Seconds since the story's last transition, or None when unknowable."""
    interrupted_at = _parse_timestamp(story.get("interrupted_at"))
    if interrupted_at is not None:
        return now - interrupted_at
    newest = _newest_activity_mtime(plan_name, story_key, story)
    if newest is None:
        return None
    return now - newest


def _gate_reason() -> str:
    """Why dispatch is not moving this story, as far as the gate can tell."""
    try:
        status = backend.get_backend("dispatch", name="claude").resource_status()
    except Exception:  # noqa: BLE001 (a broken backend must not hide the stall)
        return "unknown"
    if not isinstance(status, dict) or status.get("ok", True):
        return "dispatch slots full"
    reason = (status.get("reason") or "").lower()
    if "cooldown" in reason:
        return "usage cooldown"
    if "usage" in reason:
        return "usage gate"
    if "memory" in reason:
        return "memory"
    return "unknown"


def _stall_message(status: str, age: float, threshold: int, reason: str) -> str:
    """Embed the MEASURED age next to the threshold so a mis-thresholded
    detector is diagnosable from its own output."""
    return (
        f"Story stalled ({status}): dispatch-eligible for {age:.0f}s "
        f"(threshold {threshold}s); reason: {reason}"
    )


def run_stall_scan(plan_name: str) -> int:
    """DETECTION ONLY: notify once per stalled, dispatch-eligible story.

    Returns the number of notifications actually emitted this call. Never
    raises: the tick calls this on its critical path.
    """
    try:
        return _run_stall_scan(plan_name)
    except Exception:  # fail open by design; the tick must survive
        logging.getLogger("pipeline").warning(
            "stall scan raised; skipping this pass", exc_info=True
        )
        return 0


def _run_stall_scan(plan_name: str) -> int:
    if not _scan_enabled():
        return 0
    threshold = _stall_seconds()

    manifest = _store.get_manifest_or_none(plan_name)
    if manifest is None:
        return 0
    stories = manifest.get("stories") or {}
    done = _completed_dep_ids(stories)

    now = time.time()
    monotonic_now = time.monotonic()
    # Crude cap: the table spans every plan this process ever scanned, so drop
    # expired entries once it grows past 1000 keys. Mutate in place -- callers
    # may hold a reference to the dict.
    if len(_STALL_LAST_EMIT) > 1000:
        for key in [
            k for k, ts in _STALL_LAST_EMIT.items() if monotonic_now - ts >= threshold
        ]:
            del _STALL_LAST_EMIT[key]

    emitted = 0
    for story_key, story in stories.items():
        if not isinstance(story, dict):
            continue
        status = story.get("status")
        if status not in _STALL_STATUSES:
            continue
        if not all(dep in done for dep in story.get("dependencies") or []):
            continue
        age = _stall_age_seconds(plan_name, story_key, story, now)
        if age is None or age <= threshold:
            continue
        dedup_key = f"{plan_name}:{story_key}:stalled"
        last_emit = _STALL_LAST_EMIT.get(dedup_key)
        if last_emit is not None and monotonic_now - last_emit < threshold:
            continue
        _notify_user(
            plan_name,
            _stall_message(status, age, threshold, _gate_reason()),
            story_key=story_key,
            severity="warning",
            event="story_stalled",
            dedup_key=dedup_key,
            **(
                {"correlation_id": story["correlation_id"]}
                if story.get("correlation_id")
                else {}
            ),
        )
        # Record only after a successful emit, so a failed notify is retried on
        # the next scan instead of being silenced forever.
        _STALL_LAST_EMIT[dedup_key] = time.monotonic()
        emitted += 1
    return emitted
