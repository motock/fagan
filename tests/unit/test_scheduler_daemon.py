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


# ---------------------------------------------------------------------------
# start() — reconcile-on-startup
# ---------------------------------------------------------------------------

def test_start_calls_reconcile_fn_exactly_once():
    f = make_daemon(interval_s=60)
    f.daemon.start()
    assert f.reconcile_fn.calls == 1


def test_start_calls_reconcile_fn_immediately_with_no_elapsed_clock_time():
    # The startup sweep must fire with zero elapsed clock time, i.e. before
    # any interval gating. We use a clock that has not advanced at all.
    f = make_daemon(interval_s=60, start=0.0)
    # Even though interval_s is 60 and the clock is at 0, start() must still
    # reconcile immediately.
    f.daemon.start()
    assert f.reconcile_fn.calls == 1


def test_start_records_last_reconcile_time():
    f = make_daemon(interval_s=60, start=42.0)
    f.daemon.start()
    # The startup reconcile must be recorded as the most recent reconcile so
    # that run_once does not immediately reconcile again.
    assert f.daemon._last_reconcile == 42.0


def test_after_start_run_once_does_not_reconcile_again_immediately():
    f = make_daemon(interval_s=60)
    f.daemon.start()
    assert f.reconcile_fn.calls == 1

    # No clock advance: run_once must scan but NOT reconcile again, because
    # the startup sweep already counted as the most recent reconcile.
    result = f.daemon.run_once()
    assert f.reconcile_fn.calls == 1
    assert result == {"scanned": True, "reconciled": False}


def test_after_start_reconcile_fires_again_only_after_interval():
    f = make_daemon(interval_s=60)
    f.daemon.start()
    assert f.reconcile_fn.calls == 1

    # Within the interval: no extra reconcile.
    f.clock.advance(59)
    f.daemon.run_once()
    assert f.reconcile_fn.calls == 1

    # After a full interval: reconcile fires again.
    f.clock.advance(1)
    f.daemon.run_once()
    assert f.reconcile_fn.calls == 2


def test_run_forever_calls_start_before_first_run_once():
    # Track the order of calls: start() (reconcile) must happen before the
    # first scan_fn call from run_once.
    order = []

    class OrderedReconcile:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            order.append("reconcile")

    class OrderedScan:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            order.append("scan")

    clock = FakeClock(0.0)
    reconcile_fn = OrderedReconcile()
    scan_fn = OrderedScan()
    bus = InProcessEventBus()
    daemon = mod.SchedulerDaemon(
        reconcile_fn=reconcile_fn,
        scan_fn=scan_fn,
        bus=bus,
        interval_s=60,
        sleep_fn=lambda _s: None,
        clock=clock,
    )

    stop_event = threading.Event()
    stop_event.set()  # so run_forever exits after one iteration
    daemon.run_forever(stop_event)

    # The first recorded event must be the startup reconcile (from start()),
    # before the first scan (from run_once).
    assert order[0] == "reconcile", (
        f"start() reconcile must precede first run_once scan; got {order}"
    )
    assert "scan" in order


def test_run_forever_start_reconcile_counts_as_most_recent_reconcile():
    # run_forever calls start() which reconciles once; the first run_once in
    # the loop must NOT reconcile again (no clock advance).
    f = make_daemon(interval_s=60)
    stop_event = threading.Event()
    stop_event.set()
    f.daemon.run_forever(stop_event)

    # start() reconciled once; the single run_once in the loop scanned but did
    # not reconcile again.
    assert f.reconcile_fn.calls == 1
    assert f.scan_fn.calls == 1


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------

def test_health_reports_alive_true_and_none_timestamps_before_anything_runs():
    f = make_daemon()
    h = f.daemon.health()
    assert h["alive"] is True
    assert h["last_reconcile_ts"] is None
    assert h["last_scan_ts"] is None
    assert h["last_error"] is None
    assert h["reconcile_count"] == 0
    assert h["scan_count"] == 0


def test_health_has_expected_keys():
    f = make_daemon()
    h = f.daemon.health()
    assert set(h.keys()) == {
        "alive",
        "last_reconcile_ts",
        "last_scan_ts",
        "last_error",
        "reconcile_count",
        "scan_count",
    }


def test_health_populates_last_scan_ts_and_increments_scan_count_after_scan():
    f = make_daemon(interval_s=60)
    f.daemon.run_once()  # scans, does not reconcile (interval not elapsed)
    h = f.daemon.health()
    assert h["scan_count"] == 1
    assert h["last_scan_ts"] is not None
    # ISO8601 string must be parseable.
    import datetime as _dt

    _dt.datetime.fromisoformat(h["last_scan_ts"])
    # No reconcile happened yet.
    assert h["reconcile_count"] == 0
    assert h["last_reconcile_ts"] is None


def test_health_populates_last_reconcile_ts_and_increments_reconcile_count():
    f = make_daemon(interval_s=60)
    f.daemon.run_once()
    f.clock.advance(60)
    f.daemon.run_once()  # this one reconciles
    h = f.daemon.health()
    assert h["reconcile_count"] == 1
    assert h["last_reconcile_ts"] is not None
    import datetime as _dt

    _dt.datetime.fromisoformat(h["last_reconcile_ts"])
    # scan also ran twice.
    assert h["scan_count"] == 2


def test_health_scan_count_increments_even_when_scan_fn_raises():
    f = make_daemon(interval_s=60)
    f.scan_fn.raise_on_call = True
    f.daemon.run_once()
    h = f.daemon.health()
    # The scan was attempted, so the count must increment even on failure.
    assert h["scan_count"] == 1


def test_health_last_error_populated_after_raising_scan_fn():
    f = make_daemon(interval_s=60)
    f.scan_fn.raise_on_call = True
    f.daemon.run_once()
    h = f.daemon.health()
    assert h["last_error"] is not None
    assert isinstance(h["last_error"], str)
    assert "boom" in h["last_error"]


def test_health_last_error_persists_after_subsequent_successful_iteration():
    f = make_daemon(interval_s=60)
    f.scan_fn.raise_on_call = True
    f.daemon.run_once()
    assert f.daemon.health()["last_error"] is not None

    # Now make scan succeed and run again.
    f.scan_fn.raise_on_call = False
    f.daemon.run_once()
    h = f.daemon.health()
    # The error must remain as a diagnostic breadcrumb (not cleared).
    assert h["last_error"] is not None
    assert "boom" in h["last_error"]


def test_health_last_error_populated_after_raising_reconcile_fn():
    f = make_daemon(interval_s=60)
    f.reconcile_fn.raise_on_call = True
    f.daemon.run_once()
    f.clock.advance(60)
    f.daemon.run_once()  # reconcile fires and raises
    h = f.daemon.health()
    assert h["last_error"] is not None
    assert isinstance(h["last_error"], str)
    assert "boom" in h["last_error"]


def test_health_reconcile_count_increments_even_when_reconcile_fn_raises():
    f = make_daemon(interval_s=60)
    f.reconcile_fn.raise_on_call = True
    f.daemon.run_once()
    f.clock.advance(60)
    f.daemon.run_once()  # reconcile fires and raises
    h = f.daemon.health()
    # The reconcile was attempted, so the count must increment even on failure.
    assert h["reconcile_count"] == 1


def test_health_alive_is_always_true():
    f = make_daemon()
    f.scan_fn.raise_on_call = True
    f.daemon.run_once()
    assert f.daemon.health()["alive"] is True


# ---------------------------------------------------------------------------
# write_health()
# ---------------------------------------------------------------------------

def test_write_health_produces_file_that_round_trips_to_health_dict(tmp_path):
    f = make_daemon(interval_s=60)
    f.daemon.run_once()
    f.clock.advance(60)
    f.daemon.run_once()

    path = tmp_path / "health.json"
    f.daemon.write_health(str(path))

    import json

    assert path.exists()
    with open(path) as fh:
        on_disk = json.load(fh)
    assert on_disk == f.daemon.health()


def test_write_health_leaves_no_tmp_file(tmp_path):
    f = make_daemon(interval_s=60)
    f.daemon.run_once()

    path = tmp_path / "health.json"
    f.daemon.write_health(str(path))

    assert path.exists()
    # No leftover .tmp sibling.
    assert not (tmp_path / "health.json.tmp").exists()
    # And no stray .tmp files anywhere in the directory.
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())


def test_write_health_is_atomic_via_os_replace(tmp_path, monkeypatch):
    # Confirm write_health uses os.replace by patching it and ensuring the
    # final file ends up at the target path (not the .tmp sibling).
    f = make_daemon(interval_s=60)
    f.daemon.run_once()

    path = tmp_path / "health.json"
    real_replace = mod.os.replace if hasattr(mod, "os") else __import__("os").replace

    replaced = {}

    def spy_replace(src, dst):
        replaced["src"] = str(src)
        replaced["dst"] = str(dst)
        real_replace(src, dst)

    import os

    monkeypatch.setattr(os, "replace", spy_replace)
    f.daemon.write_health(str(path))

    assert "src" in replaced, "write_health did not call os.replace"
    assert replaced["dst"] == str(path)
    assert replaced["src"].endswith(".tmp")
    assert path.exists()


# ---------------------------------------------------------------------------
# health_path constructor argument + run_once integration
# ---------------------------------------------------------------------------

def test_scheduler_daemon_accepts_health_path_constructor_argument():
    f = make_daemon()
    # Default is None.
    assert getattr(f.daemon, "_health_path", None) is None

    daemon = mod.SchedulerDaemon(
        reconcile_fn=Counter(),
        scan_fn=Counter(),
        bus=InProcessEventBus(),
        interval_s=60,
        sleep_fn=lambda _s: None,
        clock=FakeClock(0.0),
        health_path="/tmp/whatever.json",
    )
    assert daemon._health_path == "/tmp/whatever.json"


def test_run_once_writes_health_file_when_health_path_configured(tmp_path):
    f = make_daemon()
    path = tmp_path / "health.json"
    f.daemon._health_path = str(path)

    f.daemon.run_once()

    assert path.exists()
    import json

    with open(path) as fh:
        on_disk = json.load(fh)
    assert on_disk == f.daemon.health()


def test_run_once_writes_no_file_when_health_path_is_none(tmp_path):
    f = make_daemon()
    # health_path defaults to None.
    f.daemon.run_once()

    # Nothing should have been written anywhere in tmp_path (we never gave it
    # a path, but assert the daemon didn't invent one).
    assert not any(tmp_path.iterdir())


def test_run_once_writes_health_after_each_iteration(tmp_path):
    f = make_daemon()
    path = tmp_path / "health.json"
    f.daemon._health_path = str(path)

    f.daemon.run_once()
    import json

    with open(path) as fh:
        first = json.load(fh)
    assert first["scan_count"] == 1

    f.daemon.run_once()
    with open(path) as fh:
        second = json.load(fh)
    assert second["scan_count"] == 2


def test_run_once_writes_health_even_when_scan_fn_raises(tmp_path):
    f = make_daemon()
    path = tmp_path / "health.json"
    f.daemon._health_path = str(path)
    f.scan_fn.raise_on_call = True

    f.daemon.run_once()

    assert path.exists()
    import json

    with open(path) as fh:
        on_disk = json.load(fh)
    assert on_disk["last_error"] is not None


def test_write_health_updates_last_scan_ts_in_written_file(tmp_path):
    f = make_daemon(interval_s=60)
    f.daemon.run_once()
    path = tmp_path / "health.json"
    f.daemon.write_health(str(path))

    import json

    with open(path) as fh:
        on_disk = json.load(fh)
    assert on_disk["last_scan_ts"] is not None


# ---------------------------------------------------------------------------
# start() + health() integration
# ---------------------------------------------------------------------------

def test_start_increments_reconcile_count_in_health():
    f = make_daemon(interval_s=60)
    f.daemon.start()
    h = f.daemon.health()
    assert h["reconcile_count"] == 1
    assert h["last_reconcile_ts"] is not None


def test_start_does_not_increment_scan_count_in_health():
    f = make_daemon(interval_s=60)
    f.daemon.start()
    h = f.daemon.health()
    # start() only reconciles; it must not scan.
    assert h["scan_count"] == 0
    assert h["last_scan_ts"] is None
