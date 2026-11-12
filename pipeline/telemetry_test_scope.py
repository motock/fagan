"""Test-scope dispatch counter and time-to-done histogram.

Two instruments that let rework rate and time-to-done be compared by the
``test_scope`` setting a story was dispatched under:

* ``fagan.story.dispatches`` -- counter, one per ``agent_dispatched``, labelled
  with ``fagan.test_scope`` (``full``, ``scoped``) and ``fagan.rework``
  (``none``, ``ci``, ``review``);
* ``fagan.story.dispatch_to_done.duration`` -- histogram of seconds from the
  story's first dispatch to ``story_done``, labelled with the scope in force at
  that first dispatch.

Invariants:

* **Bounded labels** -- both label sets are closed; any other payload value
  folds to ``full`` / ``none``. Plan and story key are never labels.
* **First dispatch wins** -- a later rework dispatch does not move the start.
* **Lossy** -- a start is remembered in this process only, so a process that
  never saw it records nothing for that story. A sample is emitted only when
  both timestamps parse and the duration lies in ``[0, 30 days]``.
* **Bounded** -- at most :data:`MAX_TRACKED_STORIES` stories are tracked,
  oldest evicted first.
* **Never raises** -- recording never raises to the caller.
* **No SDK import** -- ``opentelemetry`` appears as a typing hint only.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at run time
    from opentelemetry.metrics import Meter

DISPATCHES = "fagan.story.dispatches"
DISPATCH_TO_DONE = "fagan.story.dispatch_to_done.duration"

SCOPE_ATTRIBUTE = "fagan.test_scope"
REWORK_ATTRIBUTE = "fagan.rework"
SCOPES = ("full", "scoped")
REWORKS = ("none", "ci", "review")

DISPATCH_EVENT = "agent_dispatched"
DONE_EVENT = "story_done"

MAX_TRACKED_STORIES = 512  #: longest start map; the oldest story goes first
MAX_DURATION_SECONDS = 30 * 24 * 3600  #: a longer gap is bad data

_instruments: dict[str, Any] | None = None  #: None while telemetry is disabled
_starts: dict[tuple[Any, Any], tuple[datetime, str]] = {}  #: oldest story first
_lock = threading.Lock()


def register(meter: Meter) -> None:
    """Create the dispatch counter and the time-to-done histogram on *meter*."""
    global _instruments
    _instruments = {
        DISPATCHES: meter.create_counter(
            DISPATCHES,
            unit="{dispatch}",
            description="Agent dispatches by test scope and rework.",
        ),
        DISPATCH_TO_DONE: meter.create_histogram(
            DISPATCH_TO_DONE,
            unit="s",
            description="Seconds from first dispatch to done.",
        ),
    }


def reset() -> None:
    """Forget the instruments and every pending start (``reset_metrics``)."""
    global _instruments
    with _lock:
        _instruments = None
        _starts.clear()


def observe(event: dict) -> None:
    """Count a dispatch, or record time-to-done when a story finishes."""
    try:
        _observe(event)
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        return


def _observe(event: dict) -> None:
    """The unchecked body of :func:`observe`; may raise, the caller swallows it."""
    event_type = event.get("type")
    if event_type == DISPATCH_EVENT:
        _observe_dispatch(event)
    elif event_type == DONE_EVENT:
        _observe_done(event)


def _observe_dispatch(event: dict) -> None:
    """Add one to the counter and remember the story's first dispatch."""
    payload = event.get("payload") or {}
    scope = _bounded(payload.get("test_scope"), SCOPES)
    rework = _bounded(payload.get("rework"), REWORKS, default="none")
    instruments = _instruments
    if instruments is not None:
        instruments[DISPATCHES].add(
            1, {SCOPE_ATTRIBUTE: scope, REWORK_ATTRIBUTE: rework}
        )
    timestamp = _parse(event.get("ts"))
    key = _key(event)
    if timestamp is None or key is None:
        return
    with _lock:
        if key in _starts:
            return
        if len(_starts) >= MAX_TRACKED_STORIES:
            del _starts[next(iter(_starts))]
        _starts[key] = (timestamp, scope)


def _observe_done(event: dict) -> None:
    """Record first-dispatch-to-done for the story and forget it."""
    key = _key(event)
    if key is None:
        return
    with _lock:
        start = _starts.pop(key, None)
    if start is None:
        return
    timestamp, scope = start
    end = _parse(event.get("ts"))
    if end is None:
        return
    try:
        seconds = (end - timestamp).total_seconds()
    except TypeError:  # naive vs aware mix: unusable, drop it
        return
    instruments = _instruments
    if instruments is not None and 0 <= seconds <= MAX_DURATION_SECONDS:
        instruments[DISPATCH_TO_DONE].record(seconds, {SCOPE_ATTRIBUTE: scope})


def _key(event: dict) -> tuple[Any, Any] | None:
    """The ``(plan, story_key)`` pair identifying a story, or ``None`` without a plan."""
    plan = event.get("plan")
    if not plan:
        return None
    return (plan, event.get("story_key"))


def _bounded(value: Any, allowed: tuple[str, ...], default: str = "full") -> str:
    """*value* when it is one of *allowed*, otherwise *default*."""
    return value if value in allowed else default


def _parse(value: Any) -> datetime | None:
    """Parse an ISO-8601 *value*, or ``None`` when it is not a timestamp."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
