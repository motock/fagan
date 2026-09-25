"""The outbox drain phase must not hold a tick open forever.

It is the last phase of run_once, and the liveness / restart checks only run
once it returns -- so an unbounded drain leaves a wedged daemon looking alive.
"""
import threading
import time

from pipeline import notification_outbox, scheduler_timeouts
from pipeline import scheduler_daemon as mod
from pipeline.events import InProcessEventBus


def _daemon():
    return mod.SchedulerDaemon(
        reconcile_fn=lambda: None,
        scan_fn=lambda: None,
        bus=InProcessEventBus(),
        interval_s=60,
        sleep_fn=lambda _s: None,
    )


def test_wedged_drain_does_not_hold_the_tick_open(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(
        notification_outbox, "drain_outbox", lambda *a, **k: release.wait(30)
    )
    monkeypatch.setattr(scheduler_timeouts, "_DRAIN_JOIN_TIMEOUT_SECONDS", 0.5)

    daemon = _daemon()
    started = time.monotonic()
    daemon.run_once()
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 10.0, (
        f"run_once blocked {elapsed:.1f}s on a wedged drain despite a 0.5s ceiling"
    )


def test_wedged_drain_is_not_counted_as_an_abandoned_worker(monkeypatch):
    # A slow drain is not evidence of a leaked plan lock: feeding it into the
    # abandon streak would let an SMTP outage bounce the scheduler.
    release = threading.Event()
    monkeypatch.setattr(
        notification_outbox, "drain_outbox", lambda *a, **k: release.wait(30)
    )
    monkeypatch.setattr(scheduler_timeouts, "_DRAIN_JOIN_TIMEOUT_SECONDS", 0.5)

    daemon = _daemon()
    daemon.run_once()
    release.set()

    assert daemon._consecutive_abandons == 0


def test_a_completing_drain_still_runs(monkeypatch):
    # Negative control: the ceiling must not turn the drain into a no-op.
    calls = []
    monkeypatch.setattr(
        notification_outbox, "drain_outbox", lambda *a, **k: calls.append(a)
    )

    _daemon().run_once()

    assert len(calls) == 1
