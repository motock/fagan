"""Compose the scheduler's plan-advancement runtime (ADR 0011 S1).

Today ``event_wiring.wake_handler`` advances a plan synchronously on whichever
thread delivered the ``agent_done`` event -- the daemon's scan thread or the
NATS consumer's handler thread -- so one slow advance holds up the scan and
every event behind it, and the reconcile pass waits for the slowest plan.

``PIPELINE_ADVANCE_MODE=queue`` replaces both: events enqueue, a pool of
long-lived workers drains the queue, and the reconcile pass only enqueues the
plans that have gone stale. The default (``pool``, or unset) keeps today's
behaviour byte for byte.

Every import in this module other than ``logging`` is deliberately lazy: the
serial path must not import ``pipeline.server`` (tests replace that module with
a stand-in) and importing ``pipeline.advance_pool`` eagerly would close a
pre-existing import cycle.
"""

import logging

__all__ = ["build_runtime", "queue_mode_enabled"]

logger = logging.getLogger(__name__)

#: Environment variable that selects how plan advances reach the workers.
ADVANCE_MODE_ENV = "PIPELINE_ADVANCE_MODE"
#: Accepted value: the serial in-process reconcile pass (the default).
ADVANCE_MODE_POOL = "pool"
#: Accepted value: event-driven advancement through the queue and its workers.
ADVANCE_MODE_QUEUE = "queue"
#: Every accepted value, in the order the warning lists them.
ALLOWED_ADVANCE_MODES = (ADVANCE_MODE_POOL, ADVANCE_MODE_QUEUE)

#: Environment variable holding the age (seconds) after which a plan is re-queued.
STALE_SECONDS_ENV = "PIPELINE_ADVANCE_STALE_SECONDS"
#: Default staleness bound: today's at-least-once-a-minute advance of every plan.
DEFAULT_STALE_SECONDS = 60.0


def queue_mode_enabled() -> bool:
    """Return whether event-driven advancement through the queue is selected.

    ``PIPELINE_ADVANCE_MODE`` must be exactly ``queue``; unset and ``pool`` are
    today's serial behaviour. The variable is read on every call (never cached
    at import time) so a process can be reconfigured between passes, and an
    unsupported value is reported without echoing it -- it may be a path or a
    secret. Reporting degrades to the default; nothing raises, because a typo
    in an operator's environment must not take the scheduler down.
    """
    import os

    raw = os.environ.get(ADVANCE_MODE_ENV)
    if raw is None or raw == ADVANCE_MODE_POOL:
        return False
    if raw == ADVANCE_MODE_QUEUE:
        return True
    allowed = " or ".join(repr(mode) for mode in ALLOWED_ADVANCE_MODES)
    logger.warning(
        "%s must be one of %s; using %r",
        ADVANCE_MODE_ENV,
        allowed,
        ADVANCE_MODE_POOL,
    )
    return False


def _noop_stop() -> None:
    """Stop nothing: the serial path has no runtime to tear down."""


def _now() -> float:
    """Return the clock ``AdvanceQueue.last_advanced`` stamps with.

    ``time.monotonic()`` on purpose: the queue records a monotonic instant, so
    comparing it against a wall clock would make every plan look stale (or
    never stale) depending on the epoch and on system clock changes.
    """
    import time

    return time.monotonic()


def _stale_seconds() -> float:
    """Return the age after which the sweep re-queues a plan, read per call.

    ``PIPELINE_ADVANCE_STALE_SECONDS`` unset means the default silently.
    Otherwise the value must parse as a finite positive number: ``inf`` would
    make every plan perpetually fresh (reaping, lease expiry and CI polling
    would stop silently) and a non-positive bound would re-queue every plan on
    every pass. Both degrade to the default with a WARNING that names the
    variable and never echoes the value.
    """
    import math
    import os

    raw = os.environ.get(STALE_SECONDS_ENV)
    if raw is None:
        return DEFAULT_STALE_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value) or value <= 0:
        logger.warning(
            "%s must be a finite positive number; using default %gs",
            STALE_SECONDS_ENV,
            DEFAULT_STALE_SECONDS,
        )
        return DEFAULT_STALE_SECONDS
    return value


def build_runtime(bus, serial_fn):
    """Return ``(reconcile_fn, stop_fn)`` for the selected advance mode.

    Parameters
    ----------
    bus : EventBus
        The process event bus, kept alive by the caller for the daemon's
        lifetime. Unused on the serial path.
    serial_fn : Callable[[], dict]
        Today's reconcile pass, used verbatim when queue mode is off.

    Returns
    -------
    tuple[Callable[[], dict], Callable[[], None]]
        The reconcile callable the daemon should run each interval, and a
        stop callable the daemon must call once its loop returns.
    """
    if not queue_mode_enabled():
        # Byte-identical to the pre-queue runtime: the same object, and a stop
        # that does nothing. ``bus`` is not touched, so no subscription appears
        # and no handler changes under an operator who did not opt in.
        return serial_fn, _noop_stop

    from .advance_pool import _advance_workers, _plan_timeout_seconds
    from .advance_queue import AdvanceQueue
    from .advance_workers import AdvanceWorkerPool
    from .event_wiring import set_wake_hook

    queue = AdvanceQueue()
    pool = AdvanceWorkerPool(
        _advance_workers(), queue, plan_timeout_s=_plan_timeout_seconds
    )
    pool.start()
    # From here on, an event enqueues instead of advancing on the delivering
    # thread. ``request`` coalesces a plan that is already pending or in
    # flight, so a burst of events costs one advance.
    set_wake_hook(queue.request)

    def sweep() -> dict:
        """Queue every active plan whose last advance has gone stale.

        This is the reconcile pass in queue mode: it never advances and never
        waits, so a slow plan cannot delay the plans behind it or the tick
        itself. It also keeps the pre-queue cadence -- with the default bound
        every active plan is requested at least once a minute -- so timer-driven
        work (dead-agent reaping, lease expiry, CI polling, deferred reviews)
        still runs even when no event arrives.

        The staleness decision reads ``queue.last_advanced`` on every call and
        never records its own timestamp: a request that the pool rejects leaves
        ``last_advanced`` unchanged, and the next sweep must retry it rather
        than wait out another window.
        """
        from . import plan_activity, server

        bound = _stale_seconds()
        queued: list[str] = []
        for plan_name in list(server._store.list_manifests()):
            manifest = server._store.get_manifest_or_none(plan_name)
            if plan_activity.is_inert(manifest):
                continue
            last_advanced = queue.last_advanced(plan_name)
            if last_advanced is not None and _now() - last_advanced <= bound:
                continue
            queue.request(plan_name, "sweep")
            queued.append(plan_name)
        workers = pool.stats()
        logger.info(
            "advance sweep queued %d plan(s)",
            len(queued),
            extra={"queued": len(queued), "workers": workers},
        )
        return {"ok": True, "queued": queued, "workers": workers}

    def stop() -> None:
        """Unregister the wake hook, then drain and stop the workers.

        The hook goes first: a hook left installed after the pool stops would
        answer every later event with ``queued`` and silently drop it.
        """
        set_wake_hook(None)
        pool.stop()

    return sweep, stop

