"""``EventBus.delivers`` and the ``_notify_user`` double-write guard.

ADR 0010 rule 5: a publisher must be able to ask "will a subscriber, local or
durable remote, receive this type?" before performing the side effect itself.
``_notify_user`` previously answered that question by peeking at the bus's
private ``_handlers`` table, which is only correct for an in-process bus: a
broker-backed bus has no local handlers in the publishing process, so the
publisher would write the notification directly *and* the remote consumer
would write it again. These tests pin the public ``delivers`` answer for both
shipped buses, the direct-write behaviour of ``_notify_user`` in both
directions, and the fallback taken for a bus object that predates the method.
"""

from typing import ClassVar

import pipeline.server  # noqa: F401  -- breaks the pre-existing import cycle
from pipeline import event_wiring, persistence
from pipeline.events import EventBus, InProcessEventBus, JsonlEventBus


def _lines(path):
    return path.read_text().splitlines() if path.exists() else []


def _noop_handler(event):
    """A subscriber that does nothing: the test only counts deliveries."""


def _plan_dir(tmp_path, monkeypatch):
    """Point the notifications log at a per-test directory."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", plan_dir)
    return plan_dir


def _notify(monkeypatch, bus, plan_name="delivers"):
    """Drive ``_notify_user`` with *bus* standing in for the process bus."""
    monkeypatch.setattr("pipeline.event_wiring.get_bus", lambda: bus)
    persistence._notify_user(plan_name, "hello", story_key="BR-01")


class _BrokerBus:
    """A durable-remote bus: no local handlers, but a remote subscriber exists."""

    _handlers: ClassVar[dict] = {}

    def publish(self, event):
        """No local delivery."""

    def delivers(self, event_type):
        """The remote consumer subscribes to notifications."""
        return event_type == "notification"


class _LegacyBus:
    """A bus double that predates ``delivers`` and keeps no handlers."""

    _handlers: ClassVar[dict] = {}

    def publish(self, event):
        """No local delivery."""


# ---------------------------------------------------------------------------
# The public contract on EventBus
# ---------------------------------------------------------------------------

def test_delivers_is_concrete_on_the_base_class():
    """A subclass must not be forced to implement ``delivers`` itself."""
    assert "delivers" not in EventBus.__abstractmethods__


# ---------------------------------------------------------------------------
# InProcessEventBus
# ---------------------------------------------------------------------------

def test_inprocess_bus_without_a_subscriber_delivers_nothing():
    bus = InProcessEventBus()

    assert bus.delivers("notification") is False


def test_inprocess_bus_with_a_notification_subscriber_delivers_that_type_only():
    bus = InProcessEventBus()
    bus.subscribe("notification", _noop_handler)

    assert bus.delivers("notification") is True
    assert bus.delivers("some_other_type") is False


def test_inprocess_bus_with_an_empty_handler_list_delivers_nothing():
    """An empty list means nobody subscribes -- answering True would lose the notice."""
    bus = InProcessEventBus()
    bus._handlers["notification"] = []

    assert bus.delivers("notification") is False


def test_delivers_is_a_pure_read():
    """Calling it must not create or mutate the handler table."""
    bus = InProcessEventBus()
    bus._handlers["notification"] = []

    bus.delivers("notification")
    bus.delivers("notification")

    assert len(bus._handlers) == 1
    assert bus._handlers["notification"] == []

    bus.subscribe("notification", _noop_handler)

    assert bus.delivers("notification") is True
    assert len(bus._handlers) == 1


# ---------------------------------------------------------------------------
# JsonlEventBus
# ---------------------------------------------------------------------------

def test_jsonl_bus_without_a_subscriber_delivers_nothing(tmp_path):
    bus = JsonlEventBus(tmp_path)

    assert bus.delivers("notification") is False


def test_jsonl_bus_with_a_notification_subscriber_delivers_that_type_only(tmp_path):
    bus = JsonlEventBus(tmp_path)
    bus.subscribe("notification", _noop_handler)

    assert bus.delivers("notification") is True
    assert bus.delivers("some_other_type") is False


# ---------------------------------------------------------------------------
# _notify_user: the direct write happens exactly when nothing delivers
# ---------------------------------------------------------------------------

def test_notify_user_writes_directly_once_when_nothing_delivers(
    tmp_path, monkeypatch
):
    plan_dir = _plan_dir(tmp_path, monkeypatch)
    log = plan_dir / "delivers.notifications.log"
    log.write_text("2026-01-01T00:00:00Z seed line\n", encoding="utf-8")

    _notify(monkeypatch, InProcessEventBus())

    lines = _lines(log)
    assert len(lines) == 2, "exactly one appended line, the seed line is kept"
    assert lines[0] == "2026-01-01T00:00:00Z seed line"


def test_notify_user_writes_directly_when_the_handler_list_is_empty(
    tmp_path, monkeypatch
):
    plan_dir = _plan_dir(tmp_path, monkeypatch)
    log = plan_dir / "delivers.notifications.log"
    log.write_text("seed\n", encoding="utf-8")
    bus = InProcessEventBus()
    bus._handlers["notification"] = []

    _notify(monkeypatch, bus)

    assert len(_lines(log)) == 2


def test_notify_user_does_not_write_when_a_local_subscriber_delivers(
    tmp_path, monkeypatch
):
    plan_dir = _plan_dir(tmp_path, monkeypatch)
    log = plan_dir / "delivers.notifications.log"
    log.write_text("seed\n", encoding="utf-8")
    bus = InProcessEventBus()
    bus.subscribe("notification", _noop_handler)

    _notify(monkeypatch, bus)

    assert _lines(log) == ["seed"]


def test_notify_user_does_not_write_when_a_remote_subscriber_delivers(
    tmp_path, monkeypatch
):
    """The broker case: no local handlers, yet the remote consumer writes it."""
    plan_dir = _plan_dir(tmp_path, monkeypatch)
    log = plan_dir / "delivers.notifications.log"
    log.write_text("seed\n", encoding="utf-8")

    _notify(monkeypatch, _BrokerBus())

    assert _lines(log) == ["seed"]


# ---------------------------------------------------------------------------
# _notify_user: the fallback for a bus object without ``delivers``
# ---------------------------------------------------------------------------

def test_notify_user_falls_back_for_a_bus_without_delivers(tmp_path, monkeypatch):
    plan_dir = _plan_dir(tmp_path, monkeypatch)
    log = plan_dir / "delivers.notifications.log"
    log.write_text("seed\n", encoding="utf-8")

    _notify(monkeypatch, _LegacyBus())

    lines = _lines(log)
    assert len(lines) == 2
    assert lines[0] == "seed"


# ---------------------------------------------------------------------------
# The production bus: build_bus() subscribes a notification sink, so the
# publisher must not write the same notice a second time.
# ---------------------------------------------------------------------------

def test_build_bus_delivers_notifications_to_its_subscribed_sink(monkeypatch):
    """``build_bus`` wires the notification sinks, so nothing writes directly."""
    monkeypatch.delenv("PIPELINE_OTEL_ENDPOINT", raising=False)
    monkeypatch.delenv("PIPELINE_EVENT_BUS", raising=False)

    assert event_wiring.build_bus().delivers("notification") is True


def test_notify_user_does_not_double_write_against_the_wired_bus(
    tmp_path, monkeypatch
):
    plan_dir = _plan_dir(tmp_path, monkeypatch)
    log = plan_dir / "delivers.notifications.log"
    log.write_text("seed\n", encoding="utf-8")
    monkeypatch.delenv("PIPELINE_OTEL_ENDPOINT", raising=False)
    monkeypatch.delenv("PIPELINE_EVENT_BUS", raising=False)
    monkeypatch.setattr(persistence, "PLAN_DIR", plan_dir)

    _notify(monkeypatch, event_wiring.build_bus())

    # The wired ``file_log_sink`` owns the free-text log line, so the seed line
    # plus exactly one sink line is all there is: a second append here would be
    # the duplicate the guard exists to prevent.
    lines = _lines(log)
    assert len(lines) == 2, "the sink's line only; no direct-write duplicate"
    assert lines[0] == "seed"


def test_notify_user_fallback_still_honours_registered_handlers(
    tmp_path, monkeypatch
):
    plan_dir = _plan_dir(tmp_path, monkeypatch)
    log = plan_dir / "delivers.notifications.log"
    log.write_text("seed\n", encoding="utf-8")
    legacy = _LegacyBus()
    legacy._handlers = {"notification": [_noop_handler]}

    _notify(monkeypatch, legacy)

    assert _lines(log) == ["seed"]


class _Undelivered:
    """A bus whose ``delivers`` says nobody receives the notification."""

    _handlers: ClassVar[dict] = {"notification": []}

    def publish(self, event):
        """No local delivery."""

    def delivers(self, event_type):
        """Nothing receives notifications."""
        return False


def test_notify_user_across_calls_appends_and_never_rewrites_the_log(
    tmp_path, monkeypatch
):
    """Suppressing a write must not create, truncate or replace the log."""
    plan_dir = _plan_dir(tmp_path, monkeypatch)
    log = plan_dir / "delivers.notifications.log"
    log.write_text("seed\n", encoding="utf-8")

    # (a) nothing delivers -> one appended line.
    _notify(monkeypatch, _Undelivered())
    after_write = _lines(log)
    assert len(after_write) == 2
    assert after_write[0] == "seed"

    # (b) a remote subscriber delivers -> unchanged, nothing truncated.
    _notify(monkeypatch, _BrokerBus())
    assert _lines(log) == after_write

    # (c) nothing delivers again -> a third line, the first two untouched.
    _notify(monkeypatch, _Undelivered())
    lines = _lines(log)
    assert len(lines) == 3
    assert lines[:2] == after_write


def test_notify_user_creates_no_log_when_the_notice_is_delivered(
    tmp_path, monkeypatch
):
    plan_dir = _plan_dir(tmp_path, monkeypatch)
    log = plan_dir / "delivers.notifications.log"

    _notify(monkeypatch, _BrokerBus())

    assert not log.exists()
