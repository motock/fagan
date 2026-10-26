"""Announce usage-gate pause/resume transitions to the operator.

The spend gate in :mod:`pipeline.usage` already computes a ``paused`` flag on
every probe, but a transition used to be silent: on 2026-10-24 the Claude
session cap (100%) paused dispatch and interrupted running agents and nobody
was told. :func:`announce` closes that gap by emitting exactly one
notification per transition.

The last announced value is persisted in ``usage_announced.json`` next to
``USAGE_STATE_PATH`` so a poller restart does not re-announce a state the
operator already knows about. A missing or corrupt announced-file reads as
"not paused", so the first pause is always announced and a garbled file can
never suppress it.

``USAGE_STATE_PATH`` is read as a module-level free variable (the Option B
pattern used across the pipeline): tests patch
``pipeline_usage_transitions.USAGE_STATE_PATH`` directly.
"""

import json
import logging
from datetime import UTC, datetime

from .config import SESSION_PAUSE_THRESHOLD, WEEK_PAUSE_THRESHOLD
from .parsers import _atomic_write_json
from .paths import USAGE_STATE_PATH

logger = logging.getLogger(__name__)

ANNOUNCED_FILENAME = "usage_announced.json"


def _announced_path():
    """Path of the announced-state sidecar, next to the usage state file."""
    return USAGE_STATE_PATH.parent / ANNOUNCED_FILENAME


def _read_announced() -> bool:
    """Return the last announced paused value; unreadable means "not paused".

    A missing file, invalid JSON, a non-object payload, or a non-boolean
    ``paused`` field all read as ``False`` so the first pause is announced
    rather than silently suppressed by a corrupt sidecar.
    """
    try:
        data = json.loads(_announced_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return data.get("paused") is True


def _pct(value) -> int:
    """Coerce a state percentage to int, treating missing/garbage as 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _reset_clause(state: dict, key: str) -> str:
    """Render " (resets <text>)" for a window, or "" when no reset text."""
    text = state.get(key)
    if not isinstance(text, str) or not text.strip():
        return ""
    return f" (resets {text.strip()})"


def _tripped_windows(state: dict) -> str:
    """Name the window(s) at or over their pause threshold."""
    session_pct = _pct(state.get("session_pct"))
    week_pct = _pct(state.get("week_pct"))
    tripped = []
    if session_pct >= SESSION_PAUSE_THRESHOLD:
        tripped.append("session")
    if week_pct >= WEEK_PAUSE_THRESHOLD:
        tripped.append("week")
    if not tripped:
        # Paused without a window over threshold: hysteresis, or the
        # fail-closed blind branch. Name the gate rather than a window.
        return "usage gate"
    if len(tripped) == 2:
        return "session and week windows"
    return f"{tripped[0]} window"


def _paused_message(state: dict, reason: str) -> str:
    message = (
        f"Usage gate PAUSED ({_tripped_windows(state)} over threshold): "
        f"session {_pct(state.get('session_pct'))}%"
        f"{_reset_clause(state, 'session_reset')}, "
        f"week {_pct(state.get('week_pct'))}%"
        f"{_reset_clause(state, 'week_reset')}. "
        "Dispatch is paused until usage falls below the resume thresholds."
    )
    if reason:
        message = f"{message} Reason: {reason}."
    return message


def _resumed_message(state: dict, reason: str) -> str:
    message = (
        f"Usage gate RESUMED: session {_pct(state.get('session_pct'))}%, "
        f"week {_pct(state.get('week_pct'))}%. Dispatch is running again."
    )
    if reason:
        message = f"{message} Reason: {reason}."
    return message


def announce(paused: bool, state: dict, *, reason: str = "") -> str | None:
    """Announce a usage-gate transition, returning the event name or None.

    Emits ``usage_paused`` when the gate transitions to paused and
    ``usage_resumed`` when it transitions back, and nothing when the value is
    unchanged from the last announcement. Never raises: a notification or
    persistence failure is logged at WARNING by exception type only, so the
    usage poller's tick is never taken down by this side channel.
    """
    try:
        paused = bool(paused)
        if _read_announced() == paused:
            return None

        event = "usage_paused" if paused else "usage_resumed"
        message = (
            _paused_message(state, reason)
            if paused
            else _resumed_message(state, reason)
        )

        # Lazy import: server imports this module's siblings, and tests patch
        # pipeline.server._notify_user (the pattern used in pipeline/merge.py).
        from .server import _notify_user

        # Notify before persisting so a failed notification is retried on the
        # next poll instead of being recorded as already announced.
        _notify_user("usage_gate", message, event=event, severity="warning")
        _atomic_write_json(
            _announced_path(),
            {"paused": paused, "announced_at": datetime.now(UTC).isoformat()},
        )
        return event
    except Exception as exc:  # noqa: BLE001 - must never break the poller
        logger.warning(
            "usage transition announcement failed (%s)", type(exc).__name__
        )
        return None
