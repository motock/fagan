"""In-process scheduler daemon that owns the pipeline's cadence.

Historically an external clock (a launchd plist) fired ``advance_all_plans``
on a fixed interval. ``SchedulerDaemon`` takes ownership of that cadence so
the pipeline no longer depends on an external scheduler.

This module must not import from ``pipeline.server`` at module import time —
``pipeline.server`` imports from this package, and importing it here would
create a circular import. Any use of ``advance_all_plans`` for a ``__main__``
entrypoint must import it lazily, inside a function.
"""
import datetime as _dt
import fcntl
import json
import logging
import os
import signal as _signal_module
import sys
import threading
import time

from pipeline.event_wiring import build_bus
from pipeline.events import EventBus
from pipeline.paths import PLAN_DIR
from pipeline.watchers import scan_all_plans

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
        health_path=None,
    ) -> None:
        self._reconcile_fn = reconcile_fn
        self._scan_fn = scan_fn
        self._bus = bus
        self._interval_s = interval_s
        self._sleep_fn = sleep_fn
        self._clock = clock
        # Interval gating: last time we reconciled.
        self._last_reconcile = clock()
        # Health surface state
        self._health_path = health_path
        self._last_reconcile_ts = None
        self._last_scan_ts = None
        self._last_error = None
        self._reconcile_count = 0
        self._scan_count = 0

    def start(self) -> None:
        """Perform an immediate reconcile sweep on startup.

        This method is idempotent for the caller: it calls ``_reconcile_fn``
        exactly once, updates health metrics and interval gating state.
        It does not swallow exceptions; any error propagates to the caller.
        """
        # Perform reconcile immediately.
        self._reconcile_fn()
        now_ts = _dt.datetime.now().isoformat()
        self._last_reconcile_ts = now_ts
        self._reconcile_count += 1
        # Update interval gating clock so run_once does not reconcile again
        # until the configured interval has elapsed.
        self._last_reconcile = self._clock()

    def health(self) -> dict:
        """Return a snapshot of the daemon's health state."""
        return {
            "alive": True,
            "last_reconcile_ts": self._last_reconcile_ts,
            "last_scan_ts": self._last_scan_ts,
            "last_error": self._last_error,
            "reconcile_count": self._reconcile_count,
            "scan_count": self._scan_count,
        }

    def write_health(self, path: str) -> None:
        """Atomically write the health JSON to *path*.

        The file is written to ``<path>.tmp`` first and then moved into place
        with :func:`os.replace` for atomicity.
        """
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(self.health(), fh)
        os.replace(tmp_path, path)

    def run_once(self) -> dict:
        """Perform one iteration: scan first, then reconcile if due.

        ``scan_fn`` is the cheap, event-driven path and runs every call.
        ``reconcile_fn`` (the watchdog/recovery sweep) only runs once
        ``interval_s`` has elapsed on the injected clock since the last
        reconcile. Exceptions from either are logged and swallowed so a single
        bad plan cannot kill the loop.
        """
        scanned = False
        reconciled = False

        # Scan phase – always attempted, count regardless of success.
        try:
            self._scan_fn()
            scanned = True
        except Exception as exc:  # pragma: no cover - exercised via tests
            logger.exception("scan_fn raised during scheduler iteration")
            self._last_error = str(exc)
        finally:
            # Update health metrics for scan.
            self._scan_count += 1
            self._last_scan_ts = _dt.datetime.now().isoformat()

        now = self._clock()
        if now - self._last_reconcile >= self._interval_s:
            try:
                self._reconcile_fn()
                reconciled = True
            except Exception as exc:  # pragma: no cover - exercised via tests
                logger.exception("reconcile_fn raised during scheduler iteration")
                self._last_error = str(exc)
            finally:
                # Update health metrics for reconcile regardless of success.
                self._reconcile_count += 1
                self._last_reconcile_ts = _dt.datetime.now().isoformat()
                self._last_reconcile = now

        if self._health_path is not None:
            try:
                self.write_health(self._health_path)
            except Exception as exc:  # pragma: no cover - unlikely but safe
                logger.exception("write_health failed")

        return {"scanned": scanned, "reconciled": reconciled}

    def run_forever(self, stop_event) -> None:
        """Run until ``stop_event`` is set.

        Registers SIGTERM/SIGINT handlers that set ``stop_event`` so the daemon
        shuts down gracefully. The handlers never call :func:`sys.exit`.
        """

        def _handler(signum, frame):  # pragma: no cover - exercised via tests
            stop_event.set()

        signal(_signal_module.SIGTERM, _handler)
        signal(_signal_module.SIGINT, _handler)

        # Perform an initial reconcile sweep before entering the loop.
        self.start()

        while True:
            self.run_once()
            self._sleep_fn(1)
            if stop_event.is_set():
                break


def run_daemon() -> int:
    """Production composition root: build real collaborators and run.

    Acquires an exclusive, non-blocking single-instance lock at
    ``PLAN_DIR / ".scheduler_daemon.lock"`` before touching anything else, so
    two daemons can never run two clocks. If another instance already holds
    the lock, this returns 1 immediately without calling ``scan_fn``,
    ``reconcile_fn``, or ``run_forever``.
    """
    raw_interval = os.environ.get("PIPELINE_SCHEDULER_INTERVAL_S", "60")
    try:
        interval_s = int(raw_interval)
    except ValueError:
        print(
            f"scheduler_daemon: PIPELINE_SCHEDULER_INTERVAL_S must be an "
            f"integer, got {raw_interval!r}",
            file=sys.stderr,
        )
        return 1
    if interval_s <= 0:
        print(
            f"scheduler_daemon: PIPELINE_SCHEDULER_INTERVAL_S must be a "
            f"positive integer, got {interval_s}",
            file=sys.stderr,
        )
        return 1

    lock_path = PLAN_DIR / ".scheduler_daemon.lock"
    # Held for the process lifetime: do not close this fd or let it go out of
    # scope before run_forever() runs, or the flock releases early.
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(
            "scheduler_daemon: another instance already holds the lock at "
            f"{lock_path}, exiting",
            file=sys.stderr,
        )
        os.close(lock_fd)
        return 1

    health_path = os.environ.get("PIPELINE_SCHEDULER_HEALTH_PATH") or None

    bus = build_bus()

    def scan_fn():
        scan_all_plans(bus)

    # Lazy import: pipeline.server imports from this module at import time,
    # so importing it here at module level would be circular.
    from .server import advance_all_plans

    daemon = SchedulerDaemon(
        reconcile_fn=advance_all_plans,
        scan_fn=scan_fn,
        bus=bus,
        interval_s=interval_s,
        health_path=health_path,
    )
    daemon.run_forever(threading.Event())
    return 0


if __name__ == "__main__":
    sys.exit(run_daemon())
