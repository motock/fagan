import json
import logging
import os
from pathlib import Path

from . import telemetry
from .event_guards import check_precondition
from .events import EventBus, InProcessEventBus, JsonlEventBus
from .notification_outbox import outbox_sink
from .notification_sinks import file_log_sink
from .paths import PLAN_DIR

logger = logging.getLogger(__name__)

#: Environment variable that selects the process event bus.
EVENT_BUS_ENV = "PIPELINE_EVENT_BUS"
#: Accepted value: the in-process bus (the default).
EVENT_BUS_INPROCESS = "inprocess"
#: Accepted value: the append-log bus.
EVENT_BUS_JSONL = "jsonl"
#: Every accepted value, in the order the error message lists them.
ALLOWED_EVENT_BUSES = (EVENT_BUS_INPROCESS, EVENT_BUS_JSONL)


def wake_handler(event: dict) -> dict:
    """Handle an agent_done event to potentially advance the pipeline.

    Parameters
    ----------
    event : dict
        Must contain ``plan``, ``story_key`` and ``type`` keys.  Missing keys
        raise KeyError as a programming error.

    Returns
    -------
    dict
        * If the manifest is missing or unparseable: ``{"ok": False, "skipped": "no_manifest"}``
        * If :func:`check_precondition` returns ``{"ok": False, ...}``: that dict unchanged.
        * Otherwise: ``{"ok": True, "woke": <plan>, "result": <advance_pipeline return>}``
    """

    plan_name = event["plan"]
    story_key = event["story_key"]
    event_type = event["type"]

    manifest_path: Path = PLAN_DIR / f"{plan_name}.manifest.json"
    if not manifest_path.exists():
        logger.warning("Manifest missing for plan %s", plan_name)
        return {"ok": False, "skipped": "no_manifest"}
    try:
        manifest_text = manifest_path.read_text()
        manifest = json.loads(manifest_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read or parse manifest %s: %s", manifest_path, exc)
        return {"ok": False, "skipped": "no_manifest"}

    precond = check_precondition(manifest, story_key, event_type)
    if not precond.get("ok"):
        # Return the dict unchanged – a stale or redelivered event.
        return precond

    # Resolved at call time (not at module load) so tests patching
    # ``pipeline.server.advance_pipeline`` land on this call site.
    from .server import advance_pipeline

    result = advance_pipeline(plan_name)
    return {"ok": True, "woke": plan_name, "result": result}


def selected_event_bus_name() -> str:
    """Return the event-bus name selected by ``PIPELINE_EVENT_BUS``.

    Returns
    -------
    str
        ``"inprocess"`` when the variable is absent or exactly ``"inprocess"``,
        ``"jsonl"`` when it is exactly ``"jsonl"``.

    Raises
    ------
    ValueError
        For any other value, including the empty string. The selector fails
        closed: a typo must not silently downgrade a persisting bus to the
        in-process one. The message names the variable and the accepted values
        only -- the rejected value is never echoed, since it may be a path or a
        secret.

    The environment is read on every call; nothing is cached here (the only
    cache in this module is :func:`get_bus`'s process-level singleton).
    """
    raw = os.environ.get(EVENT_BUS_ENV)
    if raw is None:
        return EVENT_BUS_INPROCESS
    if raw in ALLOWED_EVENT_BUSES:
        return raw
    allowed = " or ".join(repr(name) for name in ALLOWED_EVENT_BUSES)
    raise ValueError(f"{EVENT_BUS_ENV} must be one of {allowed}")


def _subscribe_handlers(bus: EventBus) -> EventBus:
    """Subscribe the pipeline's handlers to *bus* and return it."""
    bus.subscribe("agent_done", wake_handler)
    bus.subscribe("notification", file_log_sink)
    bus.subscribe("notification", outbox_sink)
    # Lifecycle tracing is opt-in: with PIPELINE_OTEL_ENDPOINT unset this adds
    # no handler at all, so an untraced process pays nothing per tick.
    if telemetry.enabled():
        for event_type in telemetry.LIFECYCLE_EVENT_TYPES:
            bus.subscribe(event_type, telemetry.record_lifecycle_event)
    return bus


def build_bus() -> EventBus:
    """Build the pipeline's event bus, as selected by ``PIPELINE_EVENT_BUS``.

    * unset or ``"inprocess"`` -- an :class:`InProcessEventBus` with the
      pipeline's handlers subscribed (``agent_done`` -> :func:`wake_handler`,
      ``notification`` -> the file-log and outbox sinks, plus the opt-in
      lifecycle tracing handlers when telemetry is enabled).
    * ``"jsonl"`` -- a :class:`JsonlEventBus` rooted at
      :data:`pipeline.paths.PLAN_DIR`, with the same handlers subscribed. That
      bus appends one JSON object per event to ``<PLAN_DIR>/<plan>.events.jsonl``
      (the log is plan-scoped, so the plan dir is its natural root; the bus's
      own tests pin only that ``<root>/<plan>.events.jsonl`` shape and root it
      at a per-test temporary directory).
    * anything else, including the empty string -- ``ValueError`` from
      :func:`selected_event_bus_name`.

    The selection is re-read on every call and never cached, so changing the
    variable between two calls changes the bus that comes back.
    :func:`get_bus` is the one place that caches a built bus.
    """
    if selected_event_bus_name() == EVENT_BUS_JSONL:
        return _subscribe_handlers(JsonlEventBus(PLAN_DIR))
    return _subscribe_handlers(InProcessEventBus())


# Process-level singleton for the event bus.
_BUS = None


def get_bus() -> EventBus:
    """The one bus this process publishes on. Subsequent calls return the same instance.

    The bus is lazily created on first use and cached in the module-level ``_BUS``
    variable. It never re-subscribes handlers; a second call returns the
    existing bus.
    """
    global _BUS
    if _BUS is None:
        _BUS = build_bus()
    return _BUS
