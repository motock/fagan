"""Unit tests for :mod:`pipeline.events_nats_consumer`.

The broker-facing behaviour (redelivery, dead-lettering, restart resume) is
covered by ``tests/integration/test_events_nats_consumer.py`` against a live
``nats-server``. These cases pin the parts that need no broker: which bus may
start consumers at all, the shape of the durable consumer that is requested,
the order handlers run in, the backoff delay on a failed attempt, and that
:func:`stop_consumers` really stops the fetch loop.

Fake broker objects stand in for ``nats-py`` here: the consumer talks to the
JetStream context through ``pull_subscribe``/``fetch``/``ack``/``nak`` only, so
a recording fake is enough to pin the contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

# pipeline.server is imported here, before the pipeline submodules this file
# uses, to defuse a pre-existing import cycle; see the same idiom in
# tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server  # noqa: F401  # isort: skip
from pipeline import events_nats_consumer
from pipeline.events import InProcessEventBus, JsonlEventBus
from pipeline.events_nats import NatsEventBus

STREAM = "PIPELINE_UNIT"
EVENT_TYPE = "agent_done"


class _FakeMessage:
    """The slice of ``nats.aio.msg.Msg`` the consumer uses."""

    def __init__(self, event: dict | None = None, *, num_delivered: int = 1, data: bytes | None = None):
        self.data = data if data is not None else json.dumps(event or {}).encode()
        self._num_delivered = num_delivered
        self.acked = 0
        self.nak_delays: list[float | None] = []

    @property
    def metadata(self):
        return SimpleNamespace(num_delivered=self._num_delivered)

    async def ack(self):
        self.acked += 1

    async def nak(self, delay=None):
        self.nak_delays.append(delay)


class _FakeSubscription:
    """A pull subscription that hands out the messages it was given."""

    def __init__(self, messages=()):
        self._pending = list(messages)

    def push(self, message):
        self._pending.append(message)

    async def fetch(self, batch: int = 1, timeout: float | None = None):
        if self._pending:
            batch = max(1, batch)
            taken, self._pending = self._pending[:batch], self._pending[batch:]
            return taken
        # An idle pull subscription: nats-py raises its own TimeoutError here.
        await asyncio.sleep(0.01)
        raise TimeoutError


class _FakeJetStream:
    """Records ``pull_subscribe`` calls and publishes."""

    def __init__(self):
        self.calls: list[dict] = []
        self.published: list[tuple[str, bytes]] = []
        self.subscription = _FakeSubscription()

    async def pull_subscribe(self, subject, durable=None, stream=None, config=None):
        self.calls.append(
            {
                "subject": subject,
                "durable": durable,
                "stream": stream,
                "config": config,
                "subscription": self.subscription,
            }
        )
        return self.subscription

    async def publish(self, subject, payload):
        self.published.append((subject, payload))


class _FakeNatsBus(NatsEventBus):
    """A NATS bus with the broker replaced by recording fakes.

    ``NatsEventBus.__init__`` is deliberately not called: the real one
    validates ``PIPELINE_NATS_*`` and dials a server. Only the attributes the
    consumer reads are installed, and ``_ensure_loop`` -- which the consumer
    calls to reach the bus's own loop thread -- is inherited unchanged, so the
    consumer runs on a real loop with a real thread, exactly as in the daemon.
    """

    def __init__(self, durable_types=(), handlers=None, js=None):
        self._settings = SimpleNamespace(
            url="nats://127.0.0.1:1",
            stream=STREAM,
            publish_timeout_s=2.0,
            manage_stream=True,
            creds_file=None,
            tls_ca_file=None,
            durable_types=frozenset(durable_types),
        )
        self._handlers = {key: list(value) for key, value in (handlers or {}).items()}
        self._durable_types = frozenset(durable_types)
        self._js = js if js is not None else _FakeJetStream()
        self._nc = None
        self._stream_ready = True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="fake-nats-loop", daemon=True
        )
        self._thread.start()
        self._pid = os.getpid()

    def _ensure_connection(self, loop):
        return None

    def close(self):
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _no_consumers_leak():
    """Every case starts and ends with no registered consumers."""
    events_nats_consumer.stop_consumers()
    yield
    events_nats_consumer.stop_consumers()


@pytest.fixture
def fake_js():
    return _FakeJetStream()


@pytest.fixture
def bus_factory(fake_js):
    built = []

    def build(**kwargs):
        kwargs.setdefault("js", fake_js)
        bus = _FakeNatsBus(**kwargs)
        built.append(bus)
        return bus

    yield build
    # Stop the fetch loops while their loop still runs, then close the loop:
    # stopping a consumer whose loop is already stopped cannot await its task.
    events_nats_consumer.stop_consumers()
    for bus in built:
        bus.close()


def _wait_for(predicate, timeout: float = 5.0) -> None:
    """Poll *predicate* until it is true, or fail the calling test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("condition never became true within the timeout")


def test_one_durable_consumer_is_created_per_durable_type(bus_factory, fake_js):
    durable = ("agent_done", "notification")
    bus = bus_factory(durable_types=durable)

    events_nats_consumer.start_consumers(bus)

    assert len(fake_js.calls) == len(durable), (
        f"expected {len(durable)} durable pull consumers, got {len(fake_js.calls)}"
    )
    assert sorted(call["durable"] for call in fake_js.calls) == [
        f"pipeline-{event_type}" for event_type in sorted(durable)
    ], "each durable consumer is named pipeline-<type>"
    assert sorted(call["subject"] for call in fake_js.calls) == [
        f"pipeline.{event_type}.>" for event_type in sorted(durable)
    ], "each consumer binds every plan's subject for its type"
    assert {call["stream"] for call in fake_js.calls} == {STREAM}


def test_durable_consumer_requests_the_delivery_limit_and_exponential_backoff(
    bus_factory, fake_js
):
    bus = bus_factory(durable_types=(EVENT_TYPE,))

    events_nats_consumer.start_consumers(bus)

    assert len(fake_js.calls) == 1, (
        f"expected 1 durable pull consumer, got {len(fake_js.calls)}"
    )
    config = fake_js.calls[0]["config"]
    assert config.max_deliver == 5, (
        f"the default delivery limit is 5, got {config.max_deliver!r}"
    )
    assert list(config.backoff) == [1, 2, 4, 8], (
        f"backoff must double from one second, got {config.backoff!r}"
    )


def test_the_delivery_limit_is_read_from_the_environment(bus_factory, fake_js, monkeypatch):
    monkeypatch.setenv("PIPELINE_EVENT_MAX_DELIVER", "2")
    bus = bus_factory(durable_types=(EVENT_TYPE,))

    events_nats_consumer.start_consumers(bus)

    config = fake_js.calls[0]["config"]
    assert config.max_deliver == 2
    assert list(config.backoff) == [1]


def test_an_invalid_delivery_limit_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("PIPELINE_EVENT_MAX_DELIVER", "not-a-number")
    assert events_nats_consumer.max_deliver() == 5

    monkeypatch.setenv("PIPELINE_EVENT_MAX_DELIVER", "0")
    assert events_nats_consumer.max_deliver() == 5


def test_recorded_handlers_run_in_order_then_the_message_is_acked_once(bus_factory):
    calls: list[str] = []

    def first(event):
        calls.append("a")

    def second(event):
        calls.append("b")

    fake_js = _FakeJetStream()
    message = _FakeMessage({"type": EVENT_TYPE, "plan": "plan-a", "event_id": "e-1"})
    fake_js.subscription.push(message)
    bus = bus_factory(
        durable_types=(EVENT_TYPE,),
        handlers={EVENT_TYPE: [first, second]},
        js=fake_js,
    )

    events_nats_consumer.start_consumers(bus)
    _wait_for(lambda: message.acked == 1)

    assert calls == ["a", "b"], f"handlers must run in recorded order, got {calls}"
    assert message.acked == 1
    assert message.nak_delays == []


def test_a_raising_handler_naks_with_a_doubling_delay(bus_factory):
    def raising(event):
        raise RuntimeError("handler fails on purpose")

    bus = bus_factory(
        durable_types=(EVENT_TYPE,),
        handlers={EVENT_TYPE: [raising]},
    )
    first = _FakeMessage(
        {"type": EVENT_TYPE, "plan": "plan-a", "event_id": "e-1"}, num_delivered=1
    )
    second = _FakeMessage(
        {"type": EVENT_TYPE, "plan": "plan-a", "event_id": "e-1"}, num_delivered=2
    )
    bus._js.subscription.push(first)
    bus._js.subscription.push(second)

    events_nats_consumer.start_consumers(bus)
    _wait_for(lambda: len(second.nak_delays) == 1)

    assert first.nak_delays == [1], (
        f"the first failed delivery waits 1s (1 * 2**0), got {first.nak_delays!r}"
    )
    assert second.nak_delays == [2], (
        f"the second failed delivery waits 2s (1 * 2**1), got {second.nak_delays!r}"
    )
    assert first.acked == 0 and second.acked == 0, "a failed delivery is never ack'd"


def test_a_non_nats_bus_starts_no_consumer(bus_factory, fake_js, tmp_path):
    for bus in (InProcessEventBus(), JsonlEventBus(tmp_path)):
        events_nats_consumer.start_consumers(bus)

    assert fake_js.calls == []
    assert events_nats_consumer.running_consumer_names() == ()


def test_stop_consumers_stops_the_fetch_loop_and_no_handler_runs_afterward(
    bus_factory, fake_js
):
    calls: list[dict] = []

    def handler(event):
        calls.append(event)

    bus = bus_factory(durable_types=(EVENT_TYPE,), handlers={EVENT_TYPE: [handler]})
    events_nats_consumer.start_consumers(bus)
    assert events_nats_consumer.running_consumer_names() == (f"pipeline-{EVENT_TYPE}",)

    fake_js.subscription.push(
        _FakeMessage({"type": EVENT_TYPE, "plan": "plan-a", "event_id": "e-1"})
    )
    _wait_for(lambda: len(calls) == 1)

    events_nats_consumer.stop_consumers()

    assert events_nats_consumer.running_consumer_names() == (), (
        "stop_consumers must leave no registered consumer behind"
    )
    fake_js.subscription.push(
        _FakeMessage({"type": EVENT_TYPE, "plan": "plan-a", "event_id": "e-2"})
    )
    time.sleep(0.2)
    assert len(calls) == 1, (
        f"no handler may run after stop_consumers, ran {len(calls)} times"
    )
