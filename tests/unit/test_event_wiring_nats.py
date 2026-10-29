"""``PIPELINE_EVENT_BUS=nats`` wiring tests for ``pipeline/event_wiring.py``.

The selector must build a :class:`NatsEventBus` and subscribe exactly the
handlers the JSONL path subscribes -- the same callables, so a NATS-selected
process handles events exactly as an in-process one would. A broker that
cannot be reached must fail the build, never fall back to a local bus: a silent
fallback would give a deployment that looks durable and is not.
"""

from __future__ import annotations

import pytest

# pipeline.server is imported here, before the pipeline submodules this file
# uses, to defuse a pre-existing import cycle; see the same idiom in
# tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server  # noqa: F401  # isort: skip
from pipeline import event_wiring

ENV = "PIPELINE_EVENT_BUS"


@pytest.fixture
def jsonl_subscriptions(monkeypatch):
    """The ``(type, handler)`` pairs the JSONL path subscribes today."""
    monkeypatch.setenv(ENV, "jsonl")
    bus = event_wiring.build_bus()
    return bus, {
        (event_type, handler)
        for event_type, handlers in bus._handlers.items()
        for handler in handlers
    }


def test_nats_selection_builds_the_nats_bus_and_subscribes_the_same_handlers(
    monkeypatch, jsonl_subscriptions
):
    _jsonl_bus, expected = jsonl_subscriptions
    built = []

    class Recorder:
        def __init__(self):
            self.handlers = {}
            built.append(self)

        def subscribe(self, event_type, handler):
            self.handlers.setdefault(event_type, []).append(handler)

        def delivers(self, event_type):
            return bool(self.handlers.get(event_type))

        def publish(self, event):  # pragma: no cover - not exercised here
            raise AssertionError("build_bus must not publish")

    monkeypatch.setattr(event_wiring, "NatsEventBus", Recorder)
    monkeypatch.setenv(ENV, "nats")

    bus = event_wiring.build_bus()

    assert isinstance(bus, Recorder)
    assert built == [bus]
    actual = {
        (event_type, handler)
        for event_type, handlers in bus.handlers.items()
        for handler in handlers
    }
    assert actual == expected, (
        "the NATS path must subscribe the same handlers as the jsonl path, got "
        f"{sorted(t for t, _ in actual)}"
    )
    assert {event_type for event_type, _ in actual} == {
        event_type for event_type, _ in expected
    }


def test_a_failing_nats_construction_is_not_replaced_by_a_local_bus(monkeypatch):
    class Exploding:
        def __init__(self):
            raise ValueError("PIPELINE_NATS_URL must be set to the NATS server URL")

    monkeypatch.setattr(event_wiring, "NatsEventBus", Exploding)
    monkeypatch.setenv(ENV, "nats")

    with pytest.raises(ValueError) as excinfo:
        event_wiring.build_bus()

    assert "PIPELINE_NATS_URL" in str(excinfo.value)
