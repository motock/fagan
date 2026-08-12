"""
Event handling utilities for the pipeline.

This module defines the event contract and an in‑process event bus used by the
unit tests.
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Module level constants
# ---------------------------------------------------------------------------
EVENT_TYPES = frozenset(
    {
        "story_ready",
        "agent_dispatched",
        "agent_done",
        "tests_passed",
        "changes_requested",
        "pr_open",
        "ci_complete",
        "story_done",
        "parked",
        "rework",
        "reconcile",
    }
)

# ---------------------------------------------------------------------------
# Event construction helper
# ---------------------------------------------------------------------------
def make_event(type: str, plan: str, story_key=None, payload=None) -> dict:
    """Create a new event dictionary.

    Parameters
    ----------
    type:
        The event type. Must be one of :data:`EVENT_TYPES`.
    plan:
        Identifier for the pipeline plan that produced this event. Must not be
        empty or ``None``.
    story_key:
        Optional key identifying a story; defaults to ``None``.
    payload:
        Optional dictionary containing arbitrary data. Defaults to an empty
        dictionary if ``None`` is supplied.

    Returns
    -------
    dict
        A mapping with keys ``type``, ``plan``, ``story_key``, ``payload`` and
        ``ts`` (timestamp in ISO‑8601 UTC format).

    Raises
    ------
    ValueError
        If *type* is not a known event type or if *plan* is empty/None.
    """
    if type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {type}")
    if not plan:
        raise ValueError("Plan must be non-empty")

    if payload is None:
        payload = {}

    ts = datetime.now(timezone.utc).isoformat()
    return {
        "type": type,
        "plan": plan,
        "story_key": story_key,
        "payload": payload,
        "ts": ts,
    }

# ---------------------------------------------------------------------------
# Event bus abstractions
# ---------------------------------------------------------------------------
class EventBus(ABC):
    """Abstract base class for event buses."""

    @abstractmethod
    def publish(self, event: dict) -> None:
        """Publish an event to all subscribed handlers."""

    @abstractmethod
    def subscribe(self, event_type: str, handler) -> None:
        """Subscribe *handler* to events of type *event_type*."""

# ---------------------------------------------------------------------------
# In‑process implementation
# ---------------------------------------------------------------------------
class InProcessEventBus(EventBus):
    """A simple in‑memory event bus.

    Handlers are stored per event type and invoked synchronously when an event
    is published. Errors raised by a handler are logged at ERROR level but do
    not stop subsequent handlers from running.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list] = {}

    def subscribe(self, event_type: str, handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def publish(self, event: dict) -> None:
        handlers = self._handlers.get(event["type"], [])
        logger = logging.getLogger(__name__)
        for h in handlers:
            try:
                h(event)
            except Exception as e:
                # Log the exception but continue with other handlers.
                logger.error(f"Handler {h.__qualname__} raised {e}", exc_info=True)  # noqa: G201

