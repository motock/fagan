"""Startup-reconcile watchdog tests for ``SchedulerDaemon.start()``.

``start()`` used to call ``_reconcile_fn`` bare, so a wedged startup sweep
blocked forever: no join deadline, no abandonment ledger entry, no health
write, and ``run_forever`` never reached its loop. ``start()`` must route the
sweep through the same bounded watchdog ``run_once`` uses, while a raising
sweep still propagates to the caller.
"""
import logging
import threading
import time

import pytest

from pipeline import scheduler_daemon as mod
from pipeline.events import InProcessEventBus

RECONCILE_ENV = "PIPELINE_RECONCILE_JOIN_TIMEOUT_SECONDS"
START_BOUND_S = 6.0
WEDGED_MAX_BLOCK_S = 30.0


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now


class Counter:
    def __init__(self, raise_on_call: bool = False) -> None:
        self.calls = 0
        self.raise_on_call = raise_on_call

    def __call__(self):
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("boom")


class WedgeOnce:
    """Blocks on its first call until released (capped so it can't leak)."""

    def __init__(self, release: threading.Event) -> None:
        self.calls = 0
        self._release = release

    def __call__(self):
        self.calls += 1
        if self.calls == 1:
            self._release.wait(WEDGED_MAX_BLOCK_S)


def make_daemon(interval_s=60, reconcile_fn=None, scan_fn=None):
    reconcile = reconcile_fn if reconcile_fn is not None else Counter()
    scan = scan_fn if scan_fn is not None else Counter()
    daemon = mod.SchedulerDaemon(
        reconcile_fn=reconcile,
        scan_fn=scan,
        bus=InProcessEventBus(),
        interval_s=interval_s,
        sleep_fn=lambda _s: None,
        clock=FakeClock(0.0),
        health_path=None,
    )
    return daemon, reconcile, scan


def start_bounded(daemon, bound=START_BOUND_S):
    """Run ``daemon.start()`` on a helper thread with a hard wall-clock cap."""
    holder = {}

    def _target():
        try:
            daemon.start()
        except BaseException as exc:  # noqa: BLE001 - re-raised on caller thread
            holder["error"] = exc

    helper = threading.Thread(target=_target, daemon=True, name="start-helper")
    helper.start()
    helper.join(bound)
    if helper.is_alive():
        raise AssertionError(
            f"start() did not return within {bound:g}s wall clock: a wedged "
            f"reconcile_fn froze startup. The startup reconcile is missing its "
            f"watchdog (bounded join via {RECONCILE_ENV})."
        )
    if "error" in holder:
        raise holder["error"]


def test_wedged_startup_sweep_is_abandoned_and_start_returns(monkeypatch, caplog):
    monkeypatch.setenv(RECONCILE_ENV, "0.3")
    release = threading.Event()
    daemon, _reconcile, _scan = make_daemon(reconcile_fn=WedgeOnce(release))
    try:
        t0 = time.time()
        with caplog.at_level(logging.ERROR):
            start_bounded(daemon)
        t1 = time.time()
        health = daemon.health()
        assert health["reconcile_timed_out"] == 1
        assert isinstance(health["last_error"], str)
        assert any(ch.isdigit() for ch in health["last_error"])
        assert any(
            r.levelno == logging.ERROR and "reconcile" in r.getMessage().lower()
            for r in caplog.records
        )
        assert health["reconcile_count"] == 1
        assert health["last_reconcile_ts"] is not None
        assert t0 <= health["last_reconcile_timeout_ts"] <= t1
    finally:
        release.set()


def test_wedged_startup_sweep_arms_the_abandon_escape_hatch(monkeypatch):
    monkeypatch.setenv(RECONCILE_ENV, "0.3")
    release = threading.Event()
    daemon, _reconcile, _scan = make_daemon(reconcile_fn=WedgeOnce(release))
    try:
        start_bounded(daemon)
        assert len(daemon._abandoned_workers) == 1
        worker, _ts = daemon._abandoned_workers[0]
        assert worker.is_alive()
        assert daemon._consecutive_abandons == 1
    finally:
        release.set()


def test_prompt_startup_sweep_records_no_timeout(monkeypatch):
    monkeypatch.setenv(RECONCILE_ENV, "0.3")
    daemon, reconcile, _scan = make_daemon()
    start_bounded(daemon)
    health = daemon.health()
    assert reconcile.calls == 1
    assert health["reconcile_timed_out"] == 0
    assert "last_reconcile_timeout_ts" not in health


def test_raising_startup_sweep_still_propagates_and_is_not_a_timeout(monkeypatch):
    monkeypatch.setenv(RECONCILE_ENV, "0.3")
    daemon, _reconcile, _scan = make_daemon(reconcile_fn=Counter(raise_on_call=True))
    with pytest.raises(RuntimeError, match="boom"):
        start_bounded(daemon)
    assert daemon.health()["reconcile_timed_out"] == 0
    assert daemon._consecutive_abandons == 0


def test_run_forever_reaches_its_loop_after_wedged_startup_sweep(monkeypatch):
    monkeypatch.setenv(RECONCILE_ENV, "0.3")
    release = threading.Event()
    daemon, _reconcile, scan = make_daemon(reconcile_fn=WedgeOnce(release))
    stop_event = threading.Event()
    stop_event.set()
    try:
        started = time.monotonic()
        daemon.run_forever(stop_event)
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, (
            f"run_forever took {elapsed:.1f}s: start() is missing the "
            f"startup reconcile watchdog bound"
        )
        assert scan.calls == 1
    finally:
        release.set()
