"""In-process scheduler daemon that owns the pipeline's cadence.

Historically an external clock (a launchd plist) fired ``advance_all_plans``
on a fixed interval. ``SchedulerDaemon`` takes ownership of that cadence so
the pipeline no longer depends on an external scheduler.

The reconcile sweep is permanently necessary: it is the only thing that
evaluates the dispatch watchdog (``DISPATCH_WATCHDOG_SECONDS``) and the only
recovery path for a lost event. It must never be removed.

This module must not import from ``pipeline.server`` at module import time —
``pipeline.server`` imports from this package, and importing it here would
create a circular import. Any use of ``advance_all_plans`` for a ``__main__``
entrypoint must import it lazily, inside a function.
"""
import logging
import signal as _signal_module
import time

from pipeline.events import EventBus

logger = logging.getLogger(__name__)

# Exposed as a bare callable (rather than the module) so tests can patch it
# in isolation with a fake handler-recorder without losing access to the
# SIGTERM/SIGINT constants, which are read from ``_signal_module`` directly.
signal = _signal_module.signal


class SchedulerDaemon:
    """Drives the event-driven pipeline's scan + reconcile cadence.

    Collaborators are dependency-injected so the daemon is fully testable
    without real time, real processes, or real plans.
    """

    def __init__(
        self,
        reconcile_fn,
        scan_fn,
        bus: EventBus,
        interval_s: float = 60,
        sleep_fn=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self._reconcile_fn = reconcile_fn
        self._scan_fn = scan_fn
        self._bus = bus
        self._interval_s = interval_s
        self._sleep_fn = sleep_fn
        self._clock = clock
        self._last_reconcile = clock()

    def run_once(self) -> dict:
        """Perform one iteration: scan first, then reconcile if due.

        ``scan_fn`` is the cheap, event-driven path and runs every call.
        ``reconcile_fn`` (the watchdog/recovery sweep) only runs once
        ``interval_s`` has elapsed on the injected clock since the last
        reconcile. Exceptions from either are logged and swallowed so a
        single bad plan cannot kill the loop.
        """
        scanned = False
        reconciled = False

        try:
            self._scan_fn()
            scanned = True
        except Exception:
            logger.exception("scan_fn raised during scheduler iteration")

        now = self._clock()
        if now - self._last_reconcile >= self._interval_s:
            try:
                self._reconcile_fn()
                reconciled = True
            except Exception:
                logger.exception("reconcile_fn raised during scheduler iteration")
            finally:
                self._last_reconcile = now

        return {"scanned": scanned, "reconciled": reconciled}

    def run_forever(self, stop_event) -> None:
        """Run until ``stop_event`` is set.

        Registers SIGTERM/SIGINT handlers that set ``stop_event`` so the
        daemon shuts down gracefully. The handlers never call ``sys.exit``.
        """

        def _handler(signum, frame):  # pragma: no cover - exercised via tests
            stop_event.set()

        signal(_signal_module.SIGTERM, _handler)
        signal(_signal_module.SIGINT, _handler)

        while True:
            self.run_once()
            self._sleep_fn(1)
            if stop_event.is_set():
                break
