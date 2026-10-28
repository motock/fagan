"""Event-bus selection via ``PIPELINE_EVENT_BUS`` (story EV-02).

Contract under test:

* ``PIPELINE_EVENT_BUS`` unset, or exactly ``inprocess`` -> today's in-process
  bus, unchanged.
* ``PIPELINE_EVENT_BUS=jsonl`` -> an event bus rooted at
  ``pipeline.paths.PLAN_DIR`` that appends each event to
  ``<PLAN_DIR>/<plan>.events.jsonl`` **and still dispatches to its
  subscribers**: persisting an event must not disable ``wake_handler`` or the
  notification sinks.
* Any other value -- including the empty string -- raises ``ValueError`` naming
  the variable and its accepted values. The selector fails closed: an operator
  who asked for a persisting bus must never be handed the in-process one
  silently, and the message must not echo the offending value (it could be a
  path or a secret).
* The selection is read per call, never cached: flipping the variable between
  two ``build_bus()`` calls must change the returned bus.
* ``/api/health`` reports the effective bus name as ``runtime["event_bus"]``
  and never 500s. The ``runtime`` block is cumulative -- later work adds keys
  to it -- so only membership of ``event_bus`` is asserted, never the whole
  dict.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server  # noqa: F401
from app.dashboard import app
from pipeline import event_wiring
from pipeline.event_wiring import build_bus
from pipeline.events import InProcessEventBus, JsonlEventBus, make_event

EVENT_BUS_ENV = "PIPELINE_EVENT_BUS"


@pytest.fixture(autouse=True)
def _isolated_event_bus_env(monkeypatch):
    """Start every test from "the variable is not set"."""
    monkeypatch.delenv(EVENT_BUS_ENV, raising=False)


@pytest.fixture()
def health_client(tmp_path, monkeypatch):
    """``/api/health`` client with the endpoint's plan dir pinned to ``tmp_path``.

    ``health()`` reads ``app.dashboard.PLAN_DIR``, so that is the binding
    patched here; ``pipeline.paths.PLAN_DIR`` is patched too because
    ``resolved_topology()`` reads it through the ``pipeline.paths`` module.
    """
    monkeypatch.setattr("app.dashboard.PLAN_DIR", tmp_path / "plans")
    monkeypatch.setattr("pipeline.paths.PLAN_DIR", tmp_path / "plans")
    return TestClient(app)


# ---------------------------------------------------------------------------
# bus selection
# ---------------------------------------------------------------------------
def test_default_env_unset_returns_todays_bus():
    """Unset keeps today's behavior: a fresh in-process bus.

    ``build_bus()`` has never been a singleton -- the process-level cache lives
    in ``pipeline.event_wiring.get_bus`` -- so the assertion is on the type.
    """
    assert type(build_bus()) is InProcessEventBus


def test_inprocess_matches_default(monkeypatch):
    """``inprocess`` selects the same bus type as unset."""
    monkeypatch.setenv(EVENT_BUS_ENV, "inprocess")

    assert type(build_bus()) is InProcessEventBus


def test_jsonl_builds_jsonl_event_bus(monkeypatch):
    monkeypatch.setenv(EVENT_BUS_ENV, "jsonl")

    assert isinstance(build_bus(), JsonlEventBus)


def test_jsonl_bus_writes_under_the_plan_dir(monkeypatch, tmp_path):
    """The jsonl bus is rooted at the plan dir its log names belong to."""
    monkeypatch.setattr(event_wiring, "PLAN_DIR", tmp_path)
    monkeypatch.setenv(EVENT_BUS_ENV, "jsonl")

    build_bus().publish(make_event("agent_done", "plan-a", "S-1"))

    assert (tmp_path / "plan-a.events.jsonl").exists()


def test_unknown_value_raises_value_error(monkeypatch):
    monkeypatch.setenv(EVENT_BUS_ENV, "nope")

    with pytest.raises(ValueError) as excinfo:
        build_bus()

    message = str(excinfo.value)
    assert EVENT_BUS_ENV in message
    assert "inprocess" in message
    assert "jsonl" in message


def test_empty_string_value_raises_value_error(monkeypatch):
    monkeypatch.setenv(EVENT_BUS_ENV, "")

    with pytest.raises(ValueError) as excinfo:
        build_bus()

    assert EVENT_BUS_ENV in str(excinfo.value)


def test_selector_does_not_echo_the_rejected_value(monkeypatch):
    """The rejected value may be a path or a secret, so it is never echoed."""
    secret_like = "/tmp/secret-token-value"
    monkeypatch.setenv(EVENT_BUS_ENV, secret_like)

    with pytest.raises(ValueError) as excinfo:
        build_bus()

    assert secret_like not in str(excinfo.value)


def test_selection_is_not_cached_between_calls(monkeypatch):
    """Call 2 must follow the environment, not call 1's answer."""
    monkeypatch.setenv(EVENT_BUS_ENV, "jsonl")
    assert isinstance(build_bus(), JsonlEventBus)

    monkeypatch.setenv(EVENT_BUS_ENV, "inprocess")
    assert type(build_bus()) is InProcessEventBus

    monkeypatch.delenv(EVENT_BUS_ENV, raising=False)
    assert type(build_bus()) is InProcessEventBus


# ---------------------------------------------------------------------------
# jsonl selection must persist *and* dispatch
# ---------------------------------------------------------------------------
def test_jsonl_dispatches_agent_done_to_wake_handler(monkeypatch, tmp_path):
    """``agent_done`` must still advance the plan under the jsonl bus."""
    monkeypatch.setattr(event_wiring, "PLAN_DIR", tmp_path)
    seen: list[dict] = []
    monkeypatch.setattr(event_wiring, "wake_handler", seen.append)
    monkeypatch.setenv(EVENT_BUS_ENV, "jsonl")

    build_bus().publish(make_event("agent_done", "plan-a", "S-1"))

    assert [event["type"] for event in seen] == ["agent_done"]


def test_jsonl_dispatches_notification_to_both_sinks(monkeypatch, tmp_path):
    """Both notification sinks run, so persistence must not fall back."""
    monkeypatch.setattr(event_wiring, "PLAN_DIR", tmp_path)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        event_wiring, "file_log_sink", lambda event: seen.append(("file", event["type"]))
    )
    monkeypatch.setattr(
        event_wiring, "outbox_sink", lambda event: seen.append(("outbox", event["type"]))
    )
    monkeypatch.setenv(EVENT_BUS_ENV, "jsonl")

    bus = build_bus()
    bus.publish(make_event("notification", "plan-a", "S-1"))

    assert seen == [("file", "notification"), ("outbox", "notification")]
    # pipeline.persistence._notify_user only skips its direct-write fallback
    # when it sees a subscriber registered for "notification".
    assert bus._handlers.get("notification")


# ---------------------------------------------------------------------------
# /api/health runtime block
# ---------------------------------------------------------------------------
def test_health_runtime_reports_jsonl(health_client, monkeypatch):
    monkeypatch.setenv(EVENT_BUS_ENV, "jsonl")

    res = health_client.get("/api/health")

    assert res.status_code == 200, res.text
    assert res.json()["runtime"]["event_bus"] == "jsonl"


def test_health_runtime_reports_inprocess_when_unset(health_client):
    res = health_client.get("/api/health")

    assert res.status_code == 200, res.text
    assert res.json()["runtime"]["event_bus"] == "inprocess"


def test_health_reports_invalid_selector_without_500(health_client, monkeypatch):
    """A health check must never 500, and must not echo the rejected value."""
    bogus = "/tmp/secret-token-value"
    monkeypatch.setenv(EVENT_BUS_ENV, bogus)

    res = health_client.get("/api/health")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["runtime"]["event_bus"] == "invalid"
    assert bogus not in res.text


def test_health_keeps_topology_and_runtime_keys(health_client):
    res = health_client.get("/api/health")

    assert res.status_code == 200, res.text
    body = res.json()
    assert "ok" in body
    assert "topology" in body
    assert "runtime" in body
    assert "event_bus" in body["runtime"]
