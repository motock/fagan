"""Tests for the consumer-stall watchdog and its scheduler tick hook.

The watchdog is pure state: it maps successive ``consumer_status()`` snapshots
plus an injected ``now`` onto ``("consumer_stalled" | "consumer_recovered",
durable)`` transitions. The hook (``pipeline.tick_checks.run_tick_checks``) is
what turns a transition into a user notice.
"""

from __future__ import annotations

import logging

import pytest

from pipeline import events_nats_consumer, persistence, tick_checks
from pipeline.consumer_watchdog import ConsumerWatchdog

STALL_ENV = "PIPELINE_CONSUMER_STALL_SECONDS"
DURABLE = "pipeline-story"


def _snapshot(pending=1, subscribed=False, durable=DURABLE):
    """One ``consumer_status()`` success payload, in the live shape."""
    return {
        durable: {
            "pending": pending,
            "redelivered": 0,
            "dead_lettered": 0,
            "subscribed": subscribed,
            "oldest_pending_age_s": None,
        }
    }


@pytest.fixture(autouse=True)
def _no_stall_env(monkeypatch):
    monkeypatch.delenv(STALL_ENV, raising=False)


# --------------------------------------------------------------------------
# Pure watchdog: threshold, dedup, recovery
# --------------------------------------------------------------------------


def test_stall_below_threshold_is_silent():
    watchdog = ConsumerWatchdog()

    assert watchdog.observe(_snapshot(), 1000.0) == []
    assert watchdog.observe(_snapshot(), 1299.0) == []


def test_stall_at_threshold_emits_exactly_one_transition():
    watchdog = ConsumerWatchdog()

    watchdog.observe(_snapshot(), 1000.0)
    assert watchdog.observe(_snapshot(), 1300.0) == [("consumer_stalled", DURABLE)]


def test_repeated_ticks_do_not_reannounce_the_same_stall():
    watchdog = ConsumerWatchdog()

    watchdog.observe(_snapshot(), 1000.0)
    assert watchdog.observe(_snapshot(), 1300.0) == [("consumer_stalled", DURABLE)]
    assert watchdog.observe(_snapshot(), 1301.0) == []
    # A watchdog that reset its start (or forgot ``announced``) computes
    # 1600 - 1300 == 300 here and announces a duplicate.
    assert watchdog.observe(_snapshot(), 1600.0) == []


def test_recovery_emits_once_then_resets_state():
    watchdog = ConsumerWatchdog()

    watchdog.observe(_snapshot(), 1000.0)
    watchdog.observe(_snapshot(), 1300.0)

    assert watchdog.observe(_snapshot(pending=0), 1400.0) == [
        ("consumer_recovered", DURABLE)
    ]
    assert watchdog.observe(_snapshot(pending=0), 1401.0) == []
    # The reset really happened: a fresh stall needs a fresh full threshold.
    assert watchdog.observe(_snapshot(), 1500.0) == []
    assert watchdog.observe(_snapshot(), 1800.0) == [("consumer_stalled", DURABLE)]


def test_subscribed_again_after_a_stall_recovers():
    watchdog = ConsumerWatchdog()

    watchdog.observe(_snapshot(), 1000.0)
    watchdog.observe(_snapshot(), 1300.0)
    assert watchdog.observe(_snapshot(subscribed=True), 1400.0) == [
        ("consumer_recovered", DURABLE)
    ]


def test_zero_pending_never_stalls():
    watchdog = ConsumerWatchdog()

    for now in (1000.0, 1300.0, 2000.0, 5000.0):
        assert watchdog.observe(_snapshot(pending=0), now) == []


def test_unreachable_broker_is_ignored_and_keeps_the_stall_clock():
    watchdog = ConsumerWatchdog()

    assert watchdog.observe(_snapshot(), 1000.0) == []
    # Clearing state here would wrongly delay the stall from 1300 to 1400.
    assert watchdog.observe({"reachable": False}, 1100.0) == []
    assert watchdog.observe(_snapshot(), 1300.0) == [("consumer_stalled", DURABLE)]


def test_missing_fields_are_ignored_and_keep_the_stall_clock():
    watchdog = ConsumerWatchdog()

    watchdog.observe(_snapshot(), 1000.0)
    assert watchdog.observe({DURABLE: {"pending": 5}}, 1200.0) == []
    assert watchdog.observe({DURABLE: {"subscribed": False}}, 1250.0) == []
    assert watchdog.observe({}, 1260.0) == []
    assert watchdog.observe(_snapshot(), 1300.0) == [("consumer_stalled", DURABLE)]


# --------------------------------------------------------------------------
# Threshold env handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["abc", "", " ", "0", "-5", "3.5"])
def test_malformed_or_non_positive_threshold_falls_back_to_300(monkeypatch, raw):
    monkeypatch.setenv(STALL_ENV, raw)
    watchdog = ConsumerWatchdog()

    assert watchdog.observe(_snapshot(), 1000.0) == []
    assert watchdog.observe(_snapshot(), 1299.0) == []
    assert watchdog.observe(_snapshot(), 1300.0) == [("consumer_stalled", DURABLE)]


def test_threshold_is_re_read_on_every_call(monkeypatch):
    monkeypatch.setenv(STALL_ENV, "100")
    watchdog = ConsumerWatchdog()

    assert watchdog.observe(_snapshot(), 1000.0) == []
    assert watchdog.observe(_snapshot(), 1050.0) == []
    # Caching 100 would stall at 1100; the malformed value now means 300.
    monkeypatch.setenv(STALL_ENV, "abc")
    assert watchdog.observe(_snapshot(), 1099.0) == []
    assert watchdog.observe(_snapshot(), 1300.0) == [("consumer_stalled", DURABLE)]


def test_custom_threshold_is_honoured(monkeypatch):
    monkeypatch.setenv(STALL_ENV, "10")
    watchdog = ConsumerWatchdog()

    assert watchdog.observe(_snapshot(), 1000.0) == []
    assert watchdog.observe(_snapshot(), 1010.0) == [("consumer_stalled", DURABLE)]


def test_backwards_clock_does_not_move_the_stall_start():
    watchdog = ConsumerWatchdog()

    assert watchdog.observe(_snapshot(), 1000.0) == []
    assert watchdog.observe(_snapshot(), 900.0) == []
    assert watchdog.observe(_snapshot(), 1300.0) == [("consumer_stalled", DURABLE)]


def test_durables_are_tracked_independently():
    watchdog = ConsumerWatchdog()
    status = {
        "pipeline-a": {"pending": 1, "subscribed": False},
        "pipeline-b": {"pending": 0, "subscribed": False},
    }

    assert watchdog.observe(status, 1000.0) == []
    assert watchdog.observe(status, 1300.0) == [("consumer_stalled", "pipeline-a")]


# --------------------------------------------------------------------------
# Tick hook
# --------------------------------------------------------------------------


class _Clock:
    def __init__(self, now=0.0):
        self.now = now

    def time(self):
        return self.now


class _Daemon:
    """Minimal stand-in for ``SchedulerDaemon``: no broker, no git."""

    def __init__(self, status=None, error=None):
        self.status = status if status is not None else _snapshot()
        self.error = error
        self.status_calls = 0
        self.stale_checks = []

    def _notify_if_checkout_stale(self, config):
        self.stale_checks.append(config)

    def consumer_status(self):
        self.status_calls += 1
        if self.error is not None:
            raise self.error
        return self.status


@pytest.fixture
def notices(monkeypatch):
    recorded = []

    def _record(plan_name, message, **kwargs):
        recorded.append((plan_name, message, kwargs))

    monkeypatch.setattr(persistence, "_notify_user", _record)
    return recorded


@pytest.fixture
def plan_dir(monkeypatch, tmp_path):
    from pipeline import server

    (tmp_path / "alpha.manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    return tmp_path


def test_hook_runs_the_staleness_check_first(monkeypatch, notices):
    monkeypatch.delenv("PIPELINE_EVENT_BUS", raising=False)
    daemon = _Daemon()

    tick_checks.run_tick_checks(daemon, {"checkout_behind_origin": 0})

    assert daemon.stale_checks == [{"checkout_behind_origin": 0}]


def test_hook_skips_the_watchdog_entirely_without_the_nats_bus(
    monkeypatch, notices, plan_dir
):
    daemon = _Daemon()

    for value in (None, "", "local", "inprocess"):
        if value is None:
            monkeypatch.delenv("PIPELINE_EVENT_BUS", raising=False)
        else:
            monkeypatch.setenv("PIPELINE_EVENT_BUS", value)
        tick_checks.run_tick_checks(daemon, {})

    assert daemon.status_calls == 0
    assert notices == []


def test_hook_falls_back_to_the_module_probe_without_a_daemon_method(
    monkeypatch, notices, plan_dir
):
    """``SchedulerDaemon`` has no ``consumer_status``; the probe is module-level."""
    monkeypatch.setenv("PIPELINE_EVENT_BUS", "nats")
    clock = _Clock(1000.0)
    monkeypatch.setattr(tick_checks, "time", clock)
    calls = []

    def _probe():
        calls.append(True)
        return _snapshot()

    monkeypatch.setattr(events_nats_consumer, "consumer_status", _probe)
    daemon = _Daemon()
    monkeypatch.delattr(_Daemon, "consumer_status")

    tick_checks.run_tick_checks(daemon, {})
    clock.now = 1300.0
    tick_checks.run_tick_checks(daemon, {})

    assert calls == [True, True]
    assert len(notices) == 1
    assert notices[0][2]["event"] == "consumer_stalled"


def test_hook_notifies_once_per_stall_with_the_required_fields(
    monkeypatch, notices, plan_dir
):
    monkeypatch.setenv("PIPELINE_EVENT_BUS", "nats")
    clock = _Clock(1000.0)
    monkeypatch.setattr(tick_checks, "time", clock)
    daemon = _Daemon()

    tick_checks.run_tick_checks(daemon, {})
    assert notices == []

    clock.now = 1300.0
    tick_checks.run_tick_checks(daemon, {})
    assert len(notices) == 1
    plan_name, message, kwargs = notices[0]
    assert plan_name == "alpha"
    assert DURABLE in message
    assert kwargs["event"] == "consumer_stalled"
    assert kwargs["severity"] == "error"
    assert kwargs["dedup_key"] == f"consumer_stalled:{DURABLE}"

    # The watchdog instance survives between ticks: no duplicate at 1600.
    clock.now = 1600.0
    tick_checks.run_tick_checks(daemon, {})
    assert len(notices) == 1


def test_hook_notifies_recovery(monkeypatch, notices, plan_dir):
    monkeypatch.setenv("PIPELINE_EVENT_BUS", "nats")
    clock = _Clock(1000.0)
    monkeypatch.setattr(tick_checks, "time", clock)
    daemon = _Daemon()

    tick_checks.run_tick_checks(daemon, {})
    clock.now = 1300.0
    tick_checks.run_tick_checks(daemon, {})
    assert len(notices) == 1

    daemon.status = _snapshot(pending=0)
    clock.now = 1400.0
    tick_checks.run_tick_checks(daemon, {})

    assert len(notices) == 2
    _plan_name, _message, kwargs = notices[1]
    assert kwargs["event"] == "consumer_recovered"
    assert kwargs["severity"] == "info"
    assert kwargs["dedup_key"] == f"consumer_recovered:{DURABLE}"


def test_hook_watchdog_exception_never_propagates_and_keeps_the_clock(
    monkeypatch, notices, plan_dir, caplog
):
    monkeypatch.setenv("PIPELINE_EVENT_BUS", "nats")
    clock = _Clock(1000.0)
    monkeypatch.setattr(tick_checks, "time", clock)

    daemon = _Daemon()
    tick_checks.run_tick_checks(daemon, {})  # starts the stall clock at 1000

    daemon.error = RuntimeError("broker probe exploded")
    clock.now = 1300.0
    with caplog.at_level(logging.ERROR):
        tick_checks.run_tick_checks(daemon, {})  # must not raise
    records = [r for r in caplog.records if r.exc_info is not None]
    assert records, "the hook must log the failure with exc_info"
    assert notices == []

    # State (start=1000) survived: the first notice is not delayed to 1601.
    daemon.error = None
    clock.now = 1301.0
    tick_checks.run_tick_checks(daemon, {})
    assert len(notices) == 1
    assert notices[0][2]["event"] == "consumer_stalled"


def test_hook_staleness_failure_does_not_block_the_watchdog(
    monkeypatch, notices, plan_dir, caplog
):
    monkeypatch.setenv("PIPELINE_EVENT_BUS", "nats")
    clock = _Clock(1300.0)
    monkeypatch.setattr(tick_checks, "time", clock)

    class _BrokenStale(_Daemon):
        def _notify_if_checkout_stale(self, config):
            raise RuntimeError("git is gone")

    daemon = _BrokenStale()
    with caplog.at_level(logging.ERROR):
        tick_checks.run_tick_checks(daemon, {})

    assert "scheduler staleness check failed" in caplog.text
    # The fresh watchdog starts at 1300, so no stall yet — but the tick ran.
    assert daemon.status_calls == 1
    assert notices == []


def test_hook_notification_failure_does_not_break_the_tick(
    monkeypatch, plan_dir, caplog
):
    monkeypatch.setenv("PIPELINE_EVENT_BUS", "nats")
    clock = _Clock(1000.0)
    monkeypatch.setattr(tick_checks, "time", clock)

    def _explode(*args, **kwargs):
        raise RuntimeError("notify failed")

    monkeypatch.setattr(persistence, "_notify_user", _explode)
    daemon = _Daemon()

    tick_checks.run_tick_checks(daemon, {})  # starts the stall clock at 1000
    clock.now = 1300.0
    with caplog.at_level(logging.ERROR):
        tick_checks.run_tick_checks(daemon, {})  # must not raise

    assert "consumer watchdog notification failed" in caplog.text


def test_hook_skips_notification_when_no_manifest_plan_exists(
    monkeypatch, notices, tmp_path
):
    monkeypatch.setenv("PIPELINE_EVENT_BUS", "nats")
    from pipeline import server

    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)  # no manifests at all
    clock = _Clock(1300.0)
    monkeypatch.setattr(tick_checks, "time", clock)
    daemon = _Daemon()

    tick_checks.run_tick_checks(daemon, {})

    assert notices == []
