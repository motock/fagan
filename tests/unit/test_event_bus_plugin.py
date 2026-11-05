"""Event-bus selection through the plugin loader (``PIPELINE_EVENT_BUS_PLUGIN``).

Contract under test:

* A plugin is selected only by the operator-controlled variable
  ``PIPELINE_EVENT_BUS_PLUGIN=module:attr``. Unset means "today's bus".
* The selected bus is the factory's object, with the pipeline's handlers
  subscribed exactly as the built-in buses get them -- a plugin transport must
  add delivery, never trade it away.
* Combining the plugin variable with a disagreeing ``PIPELINE_EVENT_BUS`` is a
  configuration error (``ValueError``), and the plugin branch is reached before
  the factory is called.
* A bad plugin (invalid spec, or a factory that does not build an ``EventBus``)
  fails closed with ``ValueError`` and never falls back to the built-in bus, and
  the rejected spec is never echoed.
* ``reported_event_bus_name()`` reports ``"plugin"`` and never raises, and the
  selection is re-read on every call.
"""

from __future__ import annotations

import pytest

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server  # noqa: F401
from pipeline import event_wiring
from pipeline.event_wiring import (
    build_bus,
    reported_event_bus_name,
    selected_event_bus_name,
)
from pipeline.events import EventBus, InProcessEventBus, make_event

EVENT_BUS_ENV = "PIPELINE_EVENT_BUS"
PLUGIN_ENV = "PIPELINE_EVENT_BUS_PLUGIN"


class RecordingBus(InProcessEventBus):
    """An in-process bus that also records what was subscribed and published."""

    def __init__(self) -> None:
        super().__init__()
        self.subscriptions: list[tuple[str, object]] = []
        self.published: list[dict] = []

    def subscribe(self, event_type: str, handler) -> None:
        self.subscriptions.append((event_type, handler))
        super().subscribe(event_type, handler)

    def publish(self, event: dict) -> None:
        self.published.append(event)
        super().publish(event)


#: The buses built by :func:`build_recording_bus`, so a test can inspect the
#: object the loader's factory returned.
_BUILT: list[RecordingBus] = []
#: How many times the plugin factory ran, so a test can prove it never ran.
_FACTORY_CALLS: list[int] = []


def build_recording_bus() -> RecordingBus:
    """Plugin factory: a recording in-process bus."""
    _FACTORY_CALLS.append(1)
    bus = RecordingBus()
    _BUILT.append(bus)
    return bus


def build_not_a_bus():
    """Plugin factory that violates the seam: it does not build an ``EventBus``."""
    return {}


FACTORY_SPEC = f"{__name__}:build_recording_bus"
NOT_A_BUS_SPEC = f"{__name__}:build_not_a_bus"


@pytest.fixture(autouse=True)
def _isolated_event_bus_env(monkeypatch):
    """Start every test from "neither selector variable is set"."""
    monkeypatch.delenv(EVENT_BUS_ENV, raising=False)
    monkeypatch.delenv(PLUGIN_ENV, raising=False)
    _BUILT.clear()
    _FACTORY_CALLS.clear()


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------
def test_no_plugin_returns_todays_bus():
    """Regression: with neither variable set nothing about the bus changes."""
    assert type(build_bus()) is InProcessEventBus
    assert selected_event_bus_name() == "inprocess"


def test_plugin_variable_selects_the_plugin_bus(monkeypatch):
    monkeypatch.setenv(PLUGIN_ENV, FACTORY_SPEC)

    assert selected_event_bus_name() == "plugin"

    bus = build_bus()

    assert isinstance(bus, EventBus)
    assert _FACTORY_CALLS == [1]
    assert bus is _BUILT[0]


def test_plugin_bus_receives_the_pipeline_handlers(monkeypatch):
    """The plugin transport adds delivery; it must not lose the handlers."""
    monkeypatch.setenv(PLUGIN_ENV, FACTORY_SPEC)
    builtin = event_wiring._subscribe_handlers(RecordingBus())

    bus = build_bus()

    assert [event_type for event_type, _ in bus.subscriptions] == [
        event_type for event_type, _ in builtin.subscriptions
    ]
    assert bus._handlers.get("notification")

    seen: list[dict] = []
    monkeypatch.setattr(event_wiring, "wake_handler", seen.append)
    handler_bus = build_bus()
    handler_bus.publish(make_event("agent_done", "plan-a", "S-1"))

    assert [event["type"] for event in seen] == ["agent_done"]


# ---------------------------------------------------------------------------
# conflicting selectors
# ---------------------------------------------------------------------------
def test_plugin_conflicts_with_another_bus_selector(monkeypatch):
    monkeypatch.setenv(PLUGIN_ENV, FACTORY_SPEC)
    monkeypatch.setenv(EVENT_BUS_ENV, "nats")

    with pytest.raises(ValueError) as excinfo:
        selected_event_bus_name()

    assert "conflicts" in str(excinfo.value).lower()

    with pytest.raises(ValueError) as excinfo:
        build_bus()

    assert "conflicts" in str(excinfo.value).lower()
    assert _FACTORY_CALLS == []


def test_plugin_agrees_with_the_default_selector_and_recovers(monkeypatch):
    """``inprocess`` is the default, so it is not a conflict."""
    monkeypatch.setenv(PLUGIN_ENV, FACTORY_SPEC)
    monkeypatch.setenv(EVENT_BUS_ENV, "inprocess")
    assert selected_event_bus_name() == "plugin"

    # A raised conflict must not stick: dropping the selector decides again.
    monkeypatch.setenv(EVENT_BUS_ENV, "jsonl")
    with pytest.raises(ValueError):
        selected_event_bus_name()

    monkeypatch.delenv(EVENT_BUS_ENV)
    assert selected_event_bus_name() == "plugin"


# ---------------------------------------------------------------------------
# fail closed
# ---------------------------------------------------------------------------
def test_factory_that_is_not_a_bus_raises(monkeypatch):
    monkeypatch.setenv(PLUGIN_ENV, NOT_A_BUS_SPEC)

    with pytest.raises(ValueError) as excinfo:
        build_bus()

    message = str(excinfo.value)
    assert "PIPELINE_EVENT_BUS_PLUGIN" in message
    assert "EventBus" in message


def test_invalid_spec_raises_without_echoing_it(monkeypatch):
    bogus = "no-colon-here"
    monkeypatch.setenv(PLUGIN_ENV, bogus)

    with pytest.raises(ValueError) as excinfo:
        build_bus()

    assert bogus not in str(excinfo.value)


# ---------------------------------------------------------------------------
# reporting and freshness
# ---------------------------------------------------------------------------
def test_reported_name_is_plugin_and_never_raises(monkeypatch):
    monkeypatch.setenv(PLUGIN_ENV, FACTORY_SPEC)

    assert reported_event_bus_name() == "plugin"


def test_selection_is_reread_on_every_call(monkeypatch):
    monkeypatch.setenv(PLUGIN_ENV, FACTORY_SPEC)
    assert selected_event_bus_name() == "plugin"

    monkeypatch.delenv(PLUGIN_ENV, raising=False)
    assert selected_event_bus_name() == "inprocess"

    monkeypatch.setenv(PLUGIN_ENV, FACTORY_SPEC)
    assert selected_event_bus_name() == "plugin"
