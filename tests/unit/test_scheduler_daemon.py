"""Tests for pipeline.scheduler_daemon.SchedulerDaemon.

The daemon replaces an external clock (launchd plist) that used to fire
``advance_all_plans`` on an interval. These tests inject a fake clock and a
fake sleep function so nothing here ever sleeps in real time.
"""
import logging
import threading
from dataclasses import dataclass

import pytest

from pipeline import scheduler_daemon as mod
from pipeline.events import InProcessEventBus


class FakeClock:
    """A controllable monotonic clock for tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, delta: float) -> None:
        self._now += delta


class Counter:
    """A callable that records call count and can be made to raise."""

    def __init__(self, raise_on_call: bool = False) -> None:
        self.calls = 0
        self.raise_on_call = raise_on_call

    def __call__(self):
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("boom")


@dataclass
class DaemonFixture:
    daemon: mod.SchedulerDaemon
    clock: FakeClock
    reconcile_fn: Counter
    scan_fn: Counter
    bus: InProcessEventBus


def make_daemon(interval_s=60, start=0.0) -> DaemonFixture:
    clock = FakeClock(start)
    reconcile_fn = Counter()
    scan_fn = Counter()
    bus = InProcessEventBus()
    daemon = mod.SchedulerDaemon(
        reconcile_fn=reconcile_fn,
        scan_fn=scan_fn,
        bus=bus,
        interval_s=interval_s,
        sleep_fn=lambda _s: None,
        clock=clock,
    )
    return DaemonFixture(daemon, clock, reconcile_fn, scan_fn, bus)


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------

def test_run_once_calls_scan_fn_every_iteration():
    f = make_daemon()
    f.daemon.run_once()
    f.daemon.run_once()
    f.daemon.run_once()
    assert f.scan_fn.calls == 3


def test_run_once_does_not_reconcile_before_interval_elapsed():
    f = make_daemon(interval_s=60)
    result = f.daemon.run_once()
    assert f.reconcile_fn.calls == 0
    assert result == {"scanned": True, "reconciled": False}

    f.clock.advance(59)
    result = f.daemon.run_once()
    assert f.reconcile_fn.calls == 0
    assert result == {"scanned": True, "reconciled": False}


def test_run_once_reconciles_once_interval_elapsed():
    f = make_daemon(interval_s=60)
    f.daemon.run_once()
    f.clock.advance(60)
    result = f.daemon.run_once()
    assert f.reconcile_fn.calls == 1
    assert result == {"scanned": True, "reconciled": True}


def test_reconcile_fires_only_once_per_interval():
    f = make_daemon(interval_s=60)
    f.daemon.run_once()
    f.clock.advance(60)
    f.daemon.run_once()
    assert f.reconcile_fn.calls == 1

    # Subsequent calls within the same interval must not reconcile again.
    f.daemon.run_once()
    f.clock.advance(1)
    f.daemon.run_once()
    assert f.reconcile_fn.calls == 1

    # But once another full interval elapses, it fires again.
    f.clock.advance(59)
    f.daemon.run_once()
    assert f.reconcile_fn.calls == 2


def test_run_once_scan_fn_raises_is_logged_not_propagated(caplog):
    f = make_daemon(interval_s=60)
    f.scan_fn.raise_on_call = True
    with caplog.at_level(logging.ERROR):
        result = f.daemon.run_once()
    assert result == {"scanned": False, "reconciled": False}
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_run_once_reconcile_fn_raises_is_logged_not_propagated(caplog):
    f = make_daemon(interval_s=60)
    f.reconcile_fn.raise_on_call = True
    f.clock.advance(60)
    with caplog.at_level(logging.ERROR):
        result = f.daemon.run_once()
    assert result == {"scanned": True, "reconciled": False}
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_run_once_returns_dict_with_expected_keys():
    f = make_daemon()
    result = f.daemon.run_once()
    assert set(result.keys()) == {"scanned", "reconciled"}
    assert isinstance(result["scanned"], bool)
    assert isinstance(result["reconciled"], bool)


# ---------------------------------------------------------------------------
# run_forever
# ---------------------------------------------------------------------------

def test_run_forever_exits_when_stop_event_is_set():
    f = make_daemon()
    stop_event = threading.Event()

    call_count = 0

    def fake_sleep(_s):
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            stop_event.set()

    f.daemon._sleep_fn = fake_sleep
    f.daemon.run_forever(stop_event)

    assert stop_event.is_set()
    assert f.scan_fn.calls >= 1


def test_run_forever_calls_run_once_at_least_once_before_exiting():
    f = make_daemon()
    stop_event = threading.Event()
    stop_event.set()  # already set before entering the loop

    f.daemon.run_forever(stop_event)

    assert f.scan_fn.calls >= 1


def test_run_forever_registers_sigterm_and_sigint_handlers(monkeypatch):
    installed = {}

    def fake_setter(signum, handler):
        installed[signum] = handler

    monkeypatch.setattr(mod, "signal", fake_setter)

    f = make_daemon()
    stop_event = threading.Event()
    stop_event.set()
    f.daemon.run_forever(stop_event)

    import signal

    assert signal.SIGTERM in installed, "SIGTERM handler was not registered"
    assert signal.SIGINT in installed, "SIGINT handler was not registered"

    # stop_event is already set (run_forever only returns once it is). Clear
    # it and invoke each installed handler directly to confirm it re-sets the
    # same stop_event the daemon is watching, without ever calling sys.exit.
    for signum in (signal.SIGTERM, signal.SIGINT):
        handler = installed[signum]
        assert callable(handler)
        stop_event.clear()
        try:
            handler(signum, None)
        except SystemExit:
            pytest.fail(f"handler for {signum} called sys.exit")
        assert stop_event.is_set(), (
            f"handler for {signum} did not set the stop event"
        )
