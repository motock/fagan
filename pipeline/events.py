"""
Event handling utilities for the pipeline.

This module defines the event contract and an in‑process event bus used by the
unit tests.
"""

import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

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
        "notification",
    }
)

# ---------------------------------------------------------------------------
# Event construction helper
# ---------------------------------------------------------------------------

def make_event(type: str, plan: str, story_key=None, payload=None,
               correlation_id=None, *, event_id=None, schema_version=None,
               idempotency_key=None) -> dict:
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
    correlation_id:
        Optional correlation identifier stamped as a TOP-LEVEL
        ``"correlation_id"`` key on the returned event dict. When ``None``
        (the default) the key is absent entirely, so the legacy event shape
        is unchanged.
    event_id:
        Optional stable identifier for this event, stamped as a TOP-LEVEL
        ``"event_id"`` key. Must be a non-empty string of at most 64
        characters. When ``None`` (the default) the key is absent.
    schema_version:
        Optional envelope schema version, stamped as a TOP-LEVEL
        ``"schema_version"`` key. Must be an ``int`` >= 1; ``bool`` is not
        accepted because it is not a meaningful version. When ``None`` (the
        default) the key is absent.
    idempotency_key:
        Optional producer-supplied idempotency key, stamped as a TOP-LEVEL
        ``"idempotency_key"`` key. Must be a non-empty string of at most 128
        characters. When ``None`` (the default) the key is absent.

    Returns
    -------
    dict
        A mapping with keys ``type``, ``plan``, ``story_key``, ``payload`` and
        ``ts`` (timestamp in ISO‑8601 UTC format), plus each of
        ``correlation_id``, ``event_id``, ``schema_version`` and
        ``idempotency_key`` only when it is supplied. Each call builds a fresh
        dict, so optional fields never leak between calls.

    Raises
    ------
    ValueError
        If *type* is not a known event type or if *plan* is empty/None, or if
        any optional envelope field is supplied with an invalid shape. The
        message names the offending field and never its value.
    """
    if type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {type}")
    if not plan:
        raise ValueError("Plan must be non-empty")

    if event_id is not None and not (
        isinstance(event_id, str) and 0 < len(event_id) <= 64
    ):
        raise ValueError("event_id must be a non-empty str of at most 64 characters")
    if schema_version is not None and not (
        isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version >= 1
    ):
        raise ValueError("schema_version must be an int >= 1")
    if idempotency_key is not None and not (
        isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 128
    ):
        raise ValueError(
            "idempotency_key must be a non-empty str of at most 128 characters"
        )

    if payload is None:
        payload = {}

    ts = datetime.now(UTC).isoformat()
    event = {
        "type": type,
        "plan": plan,
        "story_key": story_key,
        "payload": payload,
        "ts": ts,
    }
    if correlation_id is not None:
        event["correlation_id"] = correlation_id
    if event_id is not None:
        event["event_id"] = event_id
    if schema_version is not None:
        event["schema_version"] = schema_version
    if idempotency_key is not None:
        event["idempotency_key"] = idempotency_key
    return event

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

# ---------------------------------------------------------------------------
# JsonlEventBus implementation
# ---------------------------------------------------------------------------
class JsonlEventBus(EventBus):
    """Persist events to a per‑plan JSONL append‑log.

    The log file for a plan is ``<root>/<plan>.events.jsonl``.  Events are
    written as one JSON object per line, newline terminated.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._handlers: dict[str, list] = {}

    # -----------------------------------------------------------------------
    # Validation helpers
    # -----------------------------------------------------------------------
    def _validate_plan(self, plan: str) -> str:
        """Validate a *plan* identifier.

        Raises
        ------
        ValueError
            If *plan* is ``None``, empty/whitespace, contains path separators,
            or includes any component that is ``.`` or ``..``.
        """
        if not isinstance(plan, str):
            raise ValueError("Plan must be a string")  # noqa: TRY004

        stripped = plan.strip()
        if not stripped:
            raise ValueError("Plan must be non‑empty")

        # Reject explicit '.' or '..' which resolve to the current directory
        # or parent directory and would allow writing outside the intended root.
        if stripped in (".", ".."):
            raise ValueError(
                f"Invalid plan '{plan}': contains forbidden component {stripped!r}"
            )

        # Reject any path separator characters.
        if os.sep in stripped or '/' in stripped or '\\' in stripped:
            raise ValueError(f"Invalid plan '{plan}': contains path separators")

        parts = Path(stripped).parts
        for part in parts:
            if part in (".", ".."):
                raise ValueError(
                    f"Invalid plan '{plan}': contains forbidden component {part!r}"
                )
        return stripped

    # -----------------------------------------------------------------------
    def subscribe(self, event_type: str, handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    # -----------------------------------------------------------------------
    def publish(self, event: dict) -> None:
        plan = event.get("plan")
        if not plan:
            raise ValueError("Event must contain a non‑empty 'plan' key")
        validated_plan = self._validate_plan(plan)
        path = self._root / f"{validated_plan}.events.jsonl"
        # Ensure parent directory exists.
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    # -----------------------------------------------------------------------
    def drain(self, plan: str) -> list[dict]:
        """Read and remove all events for *plan*.

        Parameters
        ----------
        plan : str
            The plan identifier whose events should be drained.

        Returns
        -------
        list[dict]
            All events that were present in the log file before it was truncated.
        """
        validated_plan = self._validate_plan(plan)
        path = self._root / f"{validated_plan}.events.jsonl"
        if not path.exists():
            return []

        parsed_events: list[dict] = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                try:
                    ev = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.warning(
                        "Malformed JSON line in %s: %r", path, stripped
                    )
                    continue
                parsed_events.append(ev)
        # Dispatch events.
        for ev in parsed_events:
            handlers = self._handlers.get(ev.get("type"), [])
            for h in handlers:
                try:
                    h(ev)
                except Exception as e:
                    logger.error(f"Handler {h.__qualname__} raised {e}", exc_info=True)  # noqa: G201
        # Truncate the file after dispatch.
        with open(path, "w", encoding="utf-8"):
            pass
        return parsed_events
