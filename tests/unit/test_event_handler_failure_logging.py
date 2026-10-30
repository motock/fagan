"""SG-06: a failed event handler must name its exception *class* in the log.

Live incident: ``wake_handler`` raised ``RuntimeError`` and all the container log
carried was ``Event handler failed for event type agent_done``. Finding the cause
took a hand reproduction, so every site that swallows a handler failure now logs
``type(exc).__name__`` as the structured field ``error_type`` and appends it to
the message.

The rule that must survive: the exception *text*, the event payload and a
traceback are never logged, because any of them can carry event contents. These
tests therefore scan every attribute of every record for the secret the failing
handler raises with.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from types import SimpleNamespace

import pytest

from pipeline import event_wiring, events_nats, events_nats_consumer

#: Text only the failing handler's exception carries; it must never be logged.
SECRET = "secret-payload-text"

#: The default delivery limit, i.e. the attempt that dead-letters a message.
_DEAD_LETTER_ATTEMPT = events_nats_consumer.DEFAULT_MAX_DELIVER


def _raise_secret(event):
    """A handler that fails with a message carrying event-like contents."""
    raise RuntimeError(SECRET)


def _noop(event):
    """A handler that returns normally."""
    return


def _assert_no_secret(record: logging.LogRecord) -> None:
    """Assert no part of *record* -- message, args, extras, traceback -- leaks."""
    message = record.getMessage()
    assert SECRET not in message, (
        f"the error text leaked into the log message {message!r}"
    )
    if record.exc_info is not None:
        rendered = "".join(traceback.format_exception(*record.exc_info))
        assert SECRET not in rendered, (
            f"the error text leaked into the traceback of {message!r}"
        )
    for key, value in record.__dict__.items():
        assert SECRET not in repr(value), (
            f"record attribute {key!r} = {value!r} leaked the error text in {message!r}"
        )


def _error_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.ERROR]


def _assert_named_failure(caplog) -> logging.LogRecord:
    """Return the single ERROR record and assert it names ``RuntimeError``."""
    errors = _error_records(caplog)
    assert errors, (
        "a failing handler produced no ERROR record: "
        f"records were {[(r.levelname, r.getMessage()) for r in caplog.records]!r}"
    )
    named = [
        record
        for record in errors
        if "RuntimeError" in record.getMessage()
        and getattr(record, "error_type", None) == "RuntimeError"
    ]
    assert named, (
        "no ERROR record both named the exception class in its message and carried "
        f"error_type='RuntimeError'; got {[(r.getMessage(), getattr(r, 'error_type', None)) for r in errors]!r}"
    )
    for record in caplog.records:
        _assert_no_secret(record)
    return named[0]


# ---------------------------------------------------------------------------
# Site 1: ``pipeline.events_nats.NatsEventBus._dispatch``
# ---------------------------------------------------------------------------


def _nats_dispatch(event_type: str, handlers: list) -> None:
    """Run the NATS bus's dispatch with only the attributes it reads."""
    bus = SimpleNamespace(_handlers={event_type: handlers})
    events_nats.NatsEventBus._dispatch(bus, event_type, {"type": event_type})


def test_nats_dispatch_names_the_exception_class(caplog):
    caplog.set_level(logging.DEBUG)

    _nats_dispatch("agent_done", [_raise_secret])

    record = _assert_named_failure(caplog)
    assert record.getMessage().endswith("(RuntimeError)"), (
        f"the class name must be appended to the message, got {record.getMessage()!r}"
    )


def test_nats_dispatch_never_logs_the_exception_text(caplog):
    caplog.set_level(logging.DEBUG)

    _nats_dispatch("agent_done", [_raise_secret])

    _assert_named_failure(caplog)  # asserts the message and every attribute
    for record in caplog.records:
        _assert_no_secret(record)
    assert all(record.exc_info is None for record in caplog.records), (
        "no traceback may be attached: it would print the exception text"
    )


def test_nats_dispatch_logs_nothing_when_the_handler_returns(caplog):
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    _nats_dispatch("agent_done", [_noop])

    assert _error_records(caplog) == [], (
        f"a handler that returns must log no ERROR: {[r.getMessage() for r in caplog.records]!r}"
    )


def test_nats_dispatch_still_runs_the_handlers_after_a_failure(caplog):
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []

    def second(event):
        seen.append("second")

    _nats_dispatch("agent_done", [_raise_secret, second])

    assert seen == ["second"], "one failing handler must not silence the rest"
    _assert_named_failure(caplog)


# ---------------------------------------------------------------------------
# Site 2: ``pipeline.event_wiring.DispatchingJsonlEventBus.publish``
# ---------------------------------------------------------------------------


def _jsonl_bus(tmp_path, handlers: list) -> event_wiring.DispatchingJsonlEventBus:
    bus = event_wiring.DispatchingJsonlEventBus(tmp_path)
    for handler in handlers:
        bus.subscribe("agent_done", handler)
    return bus


def _publish_agent_done(bus) -> None:
    bus.publish({"type": "agent_done", "plan": "plan-a", "story_key": "SG-06"})


def test_jsonl_bus_publish_names_the_exception_class(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    _publish_agent_done(_jsonl_bus(tmp_path, [_raise_secret]))

    record = _assert_named_failure(caplog)
    assert record.getMessage().endswith("(RuntimeError)"), (
        f"the class name must be appended to the message, got {record.getMessage()!r}"
    )


def test_jsonl_bus_publish_never_logs_the_exception_text(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    _publish_agent_done(_jsonl_bus(tmp_path, [_raise_secret]))

    _assert_named_failure(caplog)
    for record in caplog.records:
        _assert_no_secret(record)
    assert all(record.exc_info is None for record in caplog.records), (
        "no traceback may be attached: it would print the exception text"
    )


def test_jsonl_bus_publish_logs_nothing_when_the_handler_returns(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    _publish_agent_done(_jsonl_bus(tmp_path, [_noop]))

    assert _error_records(caplog) == [], (
        f"a handler that returns must log no ERROR: {[r.getMessage() for r in caplog.records]!r}"
    )


def test_jsonl_bus_publish_still_runs_the_handlers_after_a_failure(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []

    def second(event):
        seen.append("second")

    _publish_agent_done(_jsonl_bus(tmp_path, [_raise_secret, second]))

    assert seen == ["second"], "one failing handler must not silence the rest"
    _assert_named_failure(caplog)


# ---------------------------------------------------------------------------
# Site 3: the durable consumer's ``_run_handlers`` + dead-letter log line
# ---------------------------------------------------------------------------


class _FakeJetStream:
    """Records ``publish`` calls, as the durable-consumer tests' fake does."""

    def __init__(self):
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject, payload):
        self.published.append((subject, payload))


class _FakeMessage:
    """One deliverable message, enough for ``_deliver``."""

    def __init__(self, event: dict, *, num_delivered: int = _DEAD_LETTER_ATTEMPT):
        self.data = json.dumps(event).encode()
        self._num_delivered = num_delivered
        self.acked = 0
        self.naks: list[float] = []

    @property
    def metadata(self):
        return SimpleNamespace(num_delivered=self._num_delivered)

    async def ack(self):
        self.acked += 1

    async def nak(self, delay=None):
        self.naks.append(delay)


def _consumer(handlers: list, event_type: str = "agent_done") -> SimpleNamespace:
    return SimpleNamespace(
        event_type=event_type,
        durable=f"pipeline-{event_type}",
        bus=SimpleNamespace(_handlers={event_type: handlers}, _js=_FakeJetStream()),
    )


def _deliver(consumer, message) -> None:
    asyncio.run(events_nats_consumer._deliver(consumer, message))


def _agent_done_message(**kwargs) -> _FakeMessage:
    # No usable plan: the dead letter cannot be republished or recorded, so the
    # delivery stays in memory and still reaches the log line under test.
    return _FakeMessage({"type": "agent_done", "event_id": "e-1"}, **kwargs)


def test_consumer_dead_letter_names_the_exception_class(caplog):
    caplog.set_level(logging.DEBUG)

    _deliver(_consumer([_raise_secret]), _agent_done_message())

    record = _assert_named_failure(caplog)
    assert record.getMessage().endswith("(RuntimeError)"), (
        f"the class name must be appended to the message, got {record.getMessage()!r}"
    )
    assert "_raise_secret" in record.getMessage(), (
        f"the failing handler must still be named, got {record.getMessage()!r}"
    )


def test_consumer_dead_letter_never_logs_the_exception_text(caplog):
    caplog.set_level(logging.DEBUG)

    _deliver(_consumer([_raise_secret]), _agent_done_message())

    _assert_named_failure(caplog)
    for record in caplog.records:
        _assert_no_secret(record)
    assert all(record.exc_info is None for record in caplog.records), (
        "no traceback may be attached: it would print the exception text"
    )


def test_consumer_logs_no_error_when_the_handler_returns(caplog):
    consumer = _consumer([_noop])
    _deliver(consumer, _agent_done_message())  # a failing delivery first
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    message = _agent_done_message()
    _deliver(consumer, message)

    assert message.acked == 1, "a delivered event must be acked"
    assert _error_records(caplog) == [], (
        f"a handler that returns must log no ERROR: {[r.getMessage() for r in caplog.records]!r}"
    )
    assert [r.error_type for r in caplog.records if hasattr(r, "error_type")] == [], (
        "the class name of an earlier failure must not stick to a later delivery"
    )


def test_consumer_runs_handlers_in_order_and_reports_the_failing_one(caplog):
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []

    def first(event):
        seen.append("first")

    # At the delivery limit the failure is dead-lettered and reported; the
    # consumer stops at the first failing handler because the broker redelivers
    # the whole event, so the later handler is not run.
    message = _agent_done_message()
    _deliver(_consumer([first, _raise_secret]), message)

    assert seen == ["first"], "handlers run in the recorded order"
    record = _assert_named_failure(caplog)
    assert "_raise_secret" in record.getMessage(), (
        f"the failing handler must be named, got {record.getMessage()!r}"
    )
    assert message.acked == 1, "a dead-lettered delivery is acked only afterwards"


def test_consumer_redelivery_without_dead_letter_logs_no_error(caplog):
    # A redelivery below the delivery limit is nacked for a retry and has no
    # log line of its own; the class name travels only through the return value.
    caplog.set_level(logging.DEBUG)

    message = _agent_done_message(num_delivered=1)
    _deliver(_consumer([_raise_secret]), message)

    assert message.naks, "a failed delivery below the limit must be nacked"
    assert _error_records(caplog) == [], (
        f"a retry is logged at no more than WARNING: {[r.getMessage() for r in caplog.records]!r}"
    )


@pytest.mark.parametrize("handler", [_raise_secret, _noop])
def test_consumer_delivery_is_always_the_same_for_one_message(handler, caplog):
    """Guards against stashing the class name in shared state between calls."""
    caplog.set_level(logging.DEBUG)

    _deliver(_consumer([handler]), _agent_done_message())

    errors = _error_records(caplog)
    if handler is _raise_secret:
        _assert_named_failure(caplog)
    else:
        assert errors == []
