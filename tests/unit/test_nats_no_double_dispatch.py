"""A durable event type is never dispatched inline as well as by its consumer.

Issue 9652184e: ``NatsEventBus.publish`` ran the publishing process's handlers
for every acknowledged event, *including* the types a durable consumer also
serves. ``notification`` is in the default durable set, so the durable consumer
replayed the same event from the stream and both sinks ran again: every Slack
and e-mail notice went out twice.

The contract these tests pin: a type in ``PIPELINE_NATS_DURABLE_TYPES`` is
handled only by its durable consumer, never also inline, while a non-durable
type keeps today's inline dispatch. The broker is doubled at the JetStream
boundary only (a fake ``nats`` module, never a socket); the consumer path is
driven through the real ``events_nats_consumer._run_handlers``.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

# pipeline.server is imported here, before the pipeline submodules this file
# uses, to defuse a pre-existing import cycle; see the same idiom in
# tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server  # noqa: F401  # isort: skip
from pipeline import events_nats_consumer
from pipeline.events_nats import DEFAULT_DURABLE_TYPES, EventPublishError, NatsEventBus

LOCAL_URL = "nats://127.0.0.1:4222"
PLAN = "no-double-plan"

_NATS_ENV_VARS = (
    "PIPELINE_NATS_URL",
    "PIPELINE_NATS_CREDS_FILE",
    "PIPELINE_NATS_TLS_CA_FILE",
    "PIPELINE_NATS_STREAM",
    "PIPELINE_EVENT_PUBLISH_TIMEOUT_S",
    "PIPELINE_NATS_MANAGE_STREAM",
    "PIPELINE_NATS_DURABLE_TYPES",
)


class _FakeNotFound(Exception):
    """Stand-in for ``nats.js.errors.NotFoundError``."""


class _FakeJetStream:
    """Records publishes and answers like JetStream: an ack, or an error."""

    def __init__(self) -> None:
        self.publish_calls: list[dict] = []
        self.duplicate = False
        self.publish_error: BaseException | None = None

    async def stream_info(self, name):
        return {"config": {"name": name}}

    async def add_stream(self, **kwargs):
        return {"config": kwargs}

    async def publish(self, subject, payload=b"", headers=None, **kwargs):
        self.publish_calls.append(
            {"subject": subject, "payload": payload, "headers": headers}
        )
        if self.publish_error is not None:
            raise self.publish_error
        return types.SimpleNamespace(
            stream="PIPELINE",
            seq=len(self.publish_calls),
            duplicate=self.duplicate,
        )


class _FakeConnection:
    def __init__(self, js: _FakeJetStream) -> None:
        self._js = js

    def jetstream(self, **kwargs):
        return self._js

    async def close(self):
        return None


@pytest.fixture
def fake_broker(monkeypatch):
    """Install a fake ``nats`` package; the bus never opens a socket."""
    js = _FakeJetStream()

    async def connect(*args, **kwargs):
        return _FakeConnection(js)

    module = types.ModuleType("nats")
    module.connect = connect
    js_module = types.ModuleType("nats.js")
    errors_module = types.ModuleType("nats.js.errors")
    errors_module.NotFoundError = _FakeNotFound
    js_module.errors = errors_module
    module.js = js_module

    monkeypatch.setitem(sys.modules, "nats", module)
    monkeypatch.setitem(sys.modules, "nats.js", js_module)
    monkeypatch.setitem(sys.modules, "nats.js.errors", errors_module)
    return js


@pytest.fixture
def clean_env(monkeypatch):
    """Start every case from "nothing NATS-related is set"."""
    for name in _NATS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def make_bus(clean_env, fake_broker):
    """Build a connected bus on the fake broker; close it after the case.

    ``durable_types`` is the raw value of ``PIPELINE_NATS_DURABLE_TYPES``:
    ``None`` leaves it unset ("the daemon's default set"), a string sets it.
    """
    clean_env.setenv("PIPELINE_NATS_URL", LOCAL_URL)
    built: list[NatsEventBus] = []

    def _make(durable_types: str | None = None) -> NatsEventBus:
        if durable_types is not None:
            clean_env.setenv("PIPELINE_NATS_DURABLE_TYPES", durable_types)
        bus = NatsEventBus()
        built.append(bus)
        return bus

    yield _make
    for bus in built:
        bus.close()


def _event(event_type: str) -> dict:
    return {
        "type": event_type,
        "plan": PLAN,
        "story_key": "BR-04",
        "payload": {"message": "a notice"},
    }


def _recorder(calls: list) -> object:
    def handler(event: dict) -> None:
        calls.append(event)

    return handler


def _drive_consumer(bus: NatsEventBus, event: dict) -> None:
    """Run *event* through the real durable-consumer handler path."""
    consumer = events_nats_consumer._Consumer(
        event_type=event["type"],
        durable=f"pipeline-{event['type']}",
        subscription=object(),
        stop=asyncio.Event(),
        bus=bus,
    )
    events_nats_consumer._run_handlers(consumer, event)


# ---------------------------------------------------------------------------
# Positive: durable types are handled by their consumer, not inline
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("event_type", sorted(DEFAULT_DURABLE_TYPES))
def test_a_durable_type_is_not_dispatched_inline_and_its_consumer_runs_it_once(
    make_bus, event_type
):
    bus = make_bus()
    calls: list = []
    bus.subscribe(event_type, _recorder(calls))
    event = _event(event_type)

    bus.publish(event)

    assert calls == [], (
        "a durable type must not be dispatched in the publishing process: the "
        "durable consumer would run the same handler a second time"
    )

    _drive_consumer(bus, event)

    assert calls == [event], (
        "the durable consumer must still run the handler exactly once"
    )


@pytest.mark.parametrize("event_type", ["agent_dispatched", "story_completed"])
def test_a_non_durable_type_still_dispatches_inline_exactly_once(
    make_bus, event_type
):
    bus = make_bus()
    calls: list = []
    bus.subscribe(event_type, _recorder(calls))
    event = _event(event_type)

    bus.publish(event)

    assert calls == [event]


@pytest.mark.parametrize("durable_env", ["agent_done", "agent_done,agent_dispatched"])
def test_a_type_left_out_of_the_durable_set_dispatches_inline_once(
    make_bus, durable_env
):
    bus = make_bus(durable_types=durable_env)
    calls: list = []
    bus.subscribe("notification", _recorder(calls))
    event = _event("notification")

    bus.publish(event)

    assert calls == [event], (
        "a publisher told that nothing consumes this type durably must still "
        "do the work inline"
    )


@pytest.mark.parametrize(
    "event_type", ["notification", "agent_done", "agent_dispatched"]
)
def test_set_but_blank_durable_types_dispatch_every_type_inline(make_bus, event_type):
    bus = make_bus(durable_types="")
    calls: list = []
    bus.subscribe(event_type, _recorder(calls))
    event = _event(event_type)

    bus.publish(event)

    assert calls == [event]


# ---------------------------------------------------------------------------
# Negative: nothing is dispatched when the broker already delivered it or
# refused it
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("event_type", ["notification", "agent_dispatched"])
def test_a_duplicate_ack_dispatches_nothing(make_bus, fake_broker, event_type):
    bus = make_bus()
    fake_broker.duplicate = True
    calls: list = []
    bus.subscribe(event_type, _recorder(calls))

    bus.publish(_event(event_type))

    assert len(fake_broker.publish_calls) == 1
    assert calls == [], "an event the stream already has must not be re-run"


@pytest.mark.parametrize("event_type", ["notification", "agent_dispatched"])
def test_a_publish_failure_raises_and_dispatches_nothing(
    make_bus, fake_broker, event_type
):
    bus = make_bus()
    fake_broker.publish_error = RuntimeError("broker down")
    calls: list = []
    bus.subscribe(event_type, _recorder(calls))

    with pytest.raises(EventPublishError):
        bus.publish(_event(event_type))

    assert calls == [], "a refused publish must not run any handler"


# ---------------------------------------------------------------------------
# Existing behaviour that must survive: one failing handler is isolated
# ---------------------------------------------------------------------------
def test_a_failing_inline_handler_does_not_stop_the_next(make_bus):
    bus = make_bus()

    def failing(event: dict) -> None:
        raise RuntimeError("sink exploded")

    calls: list = []
    bus.subscribe("agent_dispatched", failing)
    bus.subscribe("agent_dispatched", _recorder(calls))
    event = _event("agent_dispatched")

    bus.publish(event)

    assert calls == [event]
