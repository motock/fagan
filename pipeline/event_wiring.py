import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from . import telemetry
from .event_guards import check_precondition
from .events import EventBus, InProcessEventBus, JsonlEventBus
from .events_nats import NatsEventBus
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
#: Accepted value: the NATS JetStream bus (durable, cross-process delivery).
EVENT_BUS_NATS = "nats"
#: Every accepted value, in the order the error message lists them.
ALLOWED_EVENT_BUSES = (EVENT_BUS_INPROCESS, EVENT_BUS_JSONL, EVENT_BUS_NATS)
#: Reported by :func:`reported_event_bus_name` when the variable is unset to an
#: unsupported value. Deliberately not a bus name: it says "this process's
#: selection could not be honoured" without echoing the rejected value.
EVENT_BUS_INVALID = "invalid"


#: Guards :data:`_wake_hook`. The hook is read under the lock and called
#: *outside* it: it takes the advance queue's own lock, so holding this one
#: across the call would invite a deadlock between the two.
_hook_lock = threading.Lock()
#: Set by :func:`set_wake_hook` when advancement is queue-driven (ADR 0011 S1).
#: ``None`` means "advance synchronously, as this process always has".
_wake_hook: Callable[[str], object] | None = None


def set_wake_hook(hook: Callable[[str], object] | None) -> None:
    """Install *hook* as the sink for advance requests, or ``None`` to clear it.

    With a hook installed, :func:`wake_handler` hands the plan name to it and
    returns immediately instead of advancing the plan on the delivering
    thread. With no hook the handler advances synchronously, exactly as before.

    The hook's return value is ignored: whether it accepted the request is the
    hook's own business (the queue coalesces a plan that is already pending).
    """
    global _wake_hook
    with _hook_lock:
        _wake_hook = hook


def _current_wake_hook() -> Callable[[str], object] | None:
    """Return the installed wake hook, if any."""
    with _hook_lock:
        return _wake_hook


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

    # Queue-driven advancement (ADR 0011 S1): hand the plan to the workers and
    # return, so a slow advance can never hold up the thread delivering events
    # or the ones behind it. Read under the lock, call outside it.
    hook = _current_wake_hook()
    if hook is not None:
        hook(plan_name)
        return {"ok": True, "queued": plan_name}

    # Resolved at call time (not at module load) so tests patching
    # ``pipeline.server.advance_pipeline`` land on this call site.
    from .server import advance_pipeline

    result = advance_pipeline(plan_name)
    return {"ok": True, "woke": plan_name, "result": result}


def reconcile_handler(event: dict) -> None:
    """Hand a ``reconcile`` event's plan to the wake hook.

    This is a hint, never an advance: with no hook installed (a process that
    did not opt into queue-driven advancement) the event is ignored, so a
    reconcile event can never make the delivering thread run an advance.
    Whether the plan has anything to do is decided by the advance itself, from
    a fresh read of the manifest.

    The hook is read under the lock and called outside it for the same reason
    :func:`wake_handler` does: it takes the advance queue's own lock.
    """
    plan_name = event.get("plan")
    if not plan_name:
        return
    hook = _current_wake_hook()
    if hook is None:
        return
    try:
        hook(plan_name)
    except Exception as exc:  # noqa: BLE001 - a broken queue must not break the bus
        logger.warning(
            "reconcile wake failed: %s", type(exc).__name__, extra={"plan": plan_name}
        )


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


def reported_event_bus_name() -> str:
    """Return the selected bus name for *observation*, never raising.

    Read-only reporting (``/api/health``'s ``runtime.event_bus``) must not turn
    a health check into a 500 because of one mistyped environment variable, so
    an unsupported value is reported as :data:`EVENT_BUS_INVALID` instead of
    raising. The rejected value is never echoed -- it may be a path or a
    secret. :func:`build_bus` remains fail-closed: the pipeline still refuses to
    run with a selection it cannot honour; only the report degrades.
    """
    try:
        return selected_event_bus_name()
    except ValueError:
        return EVENT_BUS_INVALID


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


class DispatchingJsonlEventBus(JsonlEventBus):
    """A :class:`JsonlEventBus` that persists *and* dispatches to subscribers.

    ``JsonlEventBus.publish`` only appends a JSON line; its ``subscribe``
    records handlers that it never calls. Selecting that class directly would
    therefore silently stop every handler from running: ``agent_done`` would
    never reach :func:`wake_handler` (no plan ever advances), the notification
    sinks would never persist, and ``pipeline.persistence._notify_user`` would
    skip its own direct-write fallback because it sees subscribers registered
    -- losing notifications outright rather than merely not duplicating them.

    This subclass keeps the append-log behavior and then dispatches, mirroring
    :class:`pipeline.events.InProcessEventBus`: a handler that raises is logged
    and the remaining handlers still run. Persisting happens first, so a
    failing handler can never lose the log line.
    """

    def publish(self, event: dict) -> None:
        super().publish(event)
        # The event type, the handler's class of failure and nothing else: a
        # handler failure must be diagnosable without putting event contents or
        # the exception's text in the log.
        for handler in self._handlers.get(event.get("type"), []):
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Event handler failed for event type %s (%s)",
                    event.get("type"),
                    type(exc).__name__,
                    extra={"error_type": type(exc).__name__},
                )


def build_bus() -> EventBus:
    """Build the pipeline's event bus, as selected by ``PIPELINE_EVENT_BUS``.

    * unset or ``"inprocess"`` -- an :class:`InProcessEventBus` with the
      pipeline's handlers subscribed (``agent_done`` -> :func:`wake_handler`,
      ``notification`` -> the file-log and outbox sinks, plus the opt-in
      lifecycle tracing handlers when telemetry is enabled).
    * ``"jsonl"`` -- a :class:`DispatchingJsonlEventBus` rooted at
      :data:`pipeline.paths.PLAN_DIR`, with the same handlers subscribed. It
      appends one JSON object per event to ``<PLAN_DIR>/<plan>.events.jsonl``
      *and* dispatches to those handlers, so persistence is added rather than
      traded for delivery. The log is plan-scoped, which is why the plan dir is
      its natural root; ``JsonlEventBus``'s own tests pin only the
      ``<root>/<plan>.events.jsonl`` shape and root it at a per-test temporary
      directory.
    * ``"nats"`` -- a :class:`~pipeline.events_nats.NatsEventBus` publishing to
      the JetStream server named by ``PIPELINE_NATS_URL``, with the same
      handlers subscribed. Its construction validates that configuration and
      the stream, and raises rather than degrading to a local bus, so a
      deployment that asked for durable delivery gets it or fails loudly.
    * anything else, including the empty string -- ``ValueError`` from
      :func:`selected_event_bus_name`.

    The selection is re-read on every call and never cached, so changing the
    variable between two calls changes the bus that comes back. Only
    :func:`get_bus` caches a built bus.
    """
    if selected_event_bus_name() == EVENT_BUS_JSONL:
        return _subscribe_handlers(DispatchingJsonlEventBus(PLAN_DIR))
    if selected_event_bus_name() == EVENT_BUS_NATS:
        # Constructed before subscribing: a bus whose configuration or broker is
        # unusable raises here, so the caller never gets a half-wired bus and no
        # local fallback quietly takes over a deployment that asked for NATS.
        return _subscribe_handlers(NatsEventBus())
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
