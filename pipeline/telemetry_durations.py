"""Story lifecycle duration histograms fed by the events already on the bus.

Three label-free histograms, each measuring one hop of a story's life in
seconds:

* ``fagan.story.dispatch_to_pr.duration`` -- ``agent_dispatched`` -> ``pr_open``;
* ``fagan.story.pr_to_ci.duration`` -- ``pr_open`` -> ``ci_complete``;
* ``fagan.story.pr_to_done.duration`` -- ``pr_open`` -> ``story_done``.

Invariants:

* **Label-free** -- plan names and story keys are unbounded, so they never
  become metric labels; the series carry only the resource attributes, which
  also keeps them inside the restrictive ``customer`` profile.
* **Lossy** -- a start is remembered in this process only, so a process that
  never saw it records nothing for that story. A sample is emitted only when
  both timestamps parse and the duration lies in ``[0, 30 days]``; anything
  else is dropped silently and recording never raises to the caller.
* **Bounded** -- at most :data:`MAX_TRACKED_STORIES` stories are tracked,
  oldest evicted first, so a long-lived process cannot grow the map forever.
* **No SDK import** -- ``opentelemetry`` appears as a typing hint only, so the
  disabled path pulls in no ``opentelemetry`` module.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at run time
    from opentelemetry.metrics import Meter

DISPATCH_TO_PR = "fagan.story.dispatch_to_pr.duration"
PR_TO_CI = "fagan.story.pr_to_ci.duration"
PR_TO_DONE = "fagan.story.pr_to_done.duration"

DISPATCH_EVENT = "agent_dispatched"  #: opens the dispatch hop
PR_EVENT = "pr_open"  #: opens both PR-anchored hops
CLOSING_EVENTS = {"ci_complete": PR_TO_CI, "story_done": PR_TO_DONE}

MAX_TRACKED_STORIES = 512  #: longest start map; the oldest story goes first
MAX_DURATION_SECONDS = 30 * 24 * 3600  #: a longer gap is bad data

_instruments: dict[str, Any] | None = None  #: None while telemetry is disabled
_starts: dict[tuple[Any, Any], dict[str, datetime]] = {}  #: oldest story first
_lock = threading.Lock()


def register(meter: Meter) -> None:
    """Create the three histograms on *meter* (unit seconds, no labels)."""
    global _instruments
    _instruments = {
        name: meter.create_histogram(
            name, unit="s", description=f"Lifecycle duration {name}."
        )
        for name in (DISPATCH_TO_PR, PR_TO_CI, PR_TO_DONE)
    }


def reset() -> None:
    """Forget every pending start (``telemetry.reset_metrics`` and tests)."""
    with _lock:
        _starts.clear()


def observe(event: dict) -> None:
    """Record a duration when *event* closes one of the three measured hops."""
    try:
        _observe(event)
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        return


def _observe(event: dict) -> None:
    """The unchecked body of :func:`observe`; may raise, the caller swallows it."""
    event_type = event.get("type")
    if event_type not in CLOSING_EVENTS and event_type not in (DISPATCH_EVENT, PR_EVENT):
        return
    plan = event.get("plan")
    if not plan:
        return
    key = (plan, event.get("story_key"))
    with _lock:
        if event_type == DISPATCH_EVENT:
            timestamp = _parse(event.get("ts"))
            if timestamp is not None:
                _entry(key, evict=True)[DISPATCH_EVENT] = timestamp
            return
        starts = _entry(key, evict=False)
        if starts is None:  # untracked story and the map is full: drop it
            return
        if event_type == PR_EVENT:
            # A rejected dispatch delta must not lose the dispatch start, and
            # this timestamp opens BOTH later pairs.
            start = starts.get(DISPATCH_EVENT)
            if start is not None:
                _record(DISPATCH_TO_PR, start, event.get("ts"))
            timestamp = _parse(event.get("ts"))
            if timestamp is not None:
                starts[PR_EVENT] = timestamp
            return
        start = starts.get(PR_EVENT)
        if start is not None:
            _record(CLOSING_EVENTS[event_type], start, event.get("ts"))


def _entry(key: tuple[Any, Any], *, evict: bool) -> dict[str, datetime] | None:
    """The start dict for *key*, creating it when there is room (or *evict*).

    Membership is checked before any eviction, so a repeat event for an already
    tracked story never displaces another one.
    """
    starts = _starts.get(key)
    if starts is not None:
        return starts
    if len(_starts) >= MAX_TRACKED_STORIES:
        if not evict:
            return None
        del _starts[next(iter(_starts))]
    starts = _starts[key] = {}
    return starts


def _parse(value: Any) -> datetime | None:
    """Parse an ISO-8601 *value*, or ``None`` when it is not a timestamp."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _record(name: str, start: datetime, end_value: Any) -> None:
    """Record ``end - start`` in seconds for histogram *name* when it is sane."""
    instruments = _instruments
    if instruments is None:
        return
    end = _parse(end_value)
    if end is None:
        return
    try:
        seconds = (end - start).total_seconds()
    except TypeError:  # naive vs aware mix: unusable, drop it
        return
    if 0 <= seconds <= MAX_DURATION_SECONDS:
        instruments[name].record(seconds)
