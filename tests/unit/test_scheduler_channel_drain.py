"""The scheduler tick drains the legacy e-mail outbox AND the instance outboxes."""
import logging
import threading
import time

from pipeline import notification_outbox, scheduler_timeouts
from pipeline import scheduler_daemon as mod
from pipeline.events import InProcessEventBus

_DRAIN_THREAD_NAME = "scheduler-drain-worker"


def _daemon():
    return mod.SchedulerDaemon(
        reconcile_fn=lambda: None,
        scan_fn=lambda: None,
        bus=InProcessEventBus(),
        interval_s=60,
        sleep_fn=lambda _s: None,
    )


def _wait_for_drain_workers_to_exit(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(
            t.name == _DRAIN_THREAD_NAME and t.is_alive()
            for t in threading.enumerate()
        ):
            return True
        time.sleep(0.01)
    return False


def test_tick_calls_both_drains(monkeypatch):
    calls = []
    monkeypatch.setattr(
        notification_outbox, "drain_outbox", lambda *a, **k: calls.append("email")
    )
    monkeypatch.setattr(
        notification_outbox, "drain_channel_outboxes", lambda *a, **k: calls.append("instances")
    )

    _daemon().run_once()

    assert calls == ["email", "instances"]


def test_raising_instance_drain_does_not_skip_email_drain(monkeypatch, caplog):
    calls = []

    def boom(*_a, **_k):
        raise RuntimeError("instance drain broke")

    monkeypatch.setattr(
        notification_outbox, "drain_outbox", lambda *a, **k: calls.append("email")
    )
    monkeypatch.setattr(notification_outbox, "drain_channel_outboxes", boom)

    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        _daemon().run_once()

    assert calls == ["email"]
    assert any("instance outbox drain failed" in r.getMessage() for r in caplog.records)


def test_raising_email_drain_does_not_skip_instance_drain(monkeypatch, caplog):
    calls = []

    def boom(*_a, **_k):
        raise RuntimeError("email drain broke")

    monkeypatch.setattr(notification_outbox, "drain_outbox", boom)
    monkeypatch.setattr(
        notification_outbox, "drain_channel_outboxes", lambda *a, **k: calls.append("instances")
    )

    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        _daemon().run_once()

    assert calls == ["instances"]
    assert any("outbox drain failed" in r.getMessage() for r in caplog.records)


def test_wedged_instance_drain_does_not_start_a_second_concurrent_drain(monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    lock = threading.Lock()
    entries = 0

    def wedged(*_a, **_k):
        nonlocal entries
        with lock:
            entries += 1
        entered.set()
        release.wait(30)

    monkeypatch.setattr(notification_outbox, "drain_outbox", lambda *a, **k: None)
    monkeypatch.setattr(notification_outbox, "drain_channel_outboxes", wedged)
    monkeypatch.setattr(scheduler_timeouts, "_DRAIN_JOIN_TIMEOUT_SECONDS", 0.5)

    daemon = _daemon()
    daemon.run_once()
    assert entered.wait(5), "the instance drain never started"
    daemon.run_once()
    release.set()
    _wait_for_drain_workers_to_exit()

    assert entries == 1
