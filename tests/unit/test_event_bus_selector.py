"""Event-bus selection via ``PIPELINE_EVENT_BUS`` (story EV-02).

Contract under test:

* ``PIPELINE_EVENT_BUS`` unset, or exactly ``inprocess`` -> today's in-process
  bus, unchanged.
* ``PIPELINE_EVENT_BUS=jsonl`` -> a :class:`pipeline.events.JsonlEventBus`
  rooted at ``pipeline.paths.PLAN_DIR`` (its log is ``<plan>.events.jsonl``).
* Any other value -- including the empty string -- raises ``ValueError`` naming
  the variable and its accepted values. The selector fails closed: an operator
  who asked for a persisting bus must never be handed the in-process one
  silently, and the message must not echo the offending value (it could be a
  path or a secret).
* The selection is read per call, never cached: flipping the variable between
  two ``build_bus()`` calls must change the returned bus.
* ``/api/health`` reports the effective bus name as ``runtime["event_bus"]``.
  The ``runtime`` block is cumulative -- later work adds keys to it -- so only
  membership of ``event_bus`` is asserted, never the whole dict.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first. The ``pipeline`` name is
# used below (``pipeline.paths.PLAN_DIR``) so no ``noqa: F401`` is needed.
import pipeline.server
from app.dashboard import app
from pipeline import event_wiring
from pipeline.event_wiring import build_bus
from pipeline.events import InProcessEventBus, JsonlEventBus

EVENT_BUS_ENV = "PIPELINE_EVENT_BUS"


@pytest.fixture(autouse=True)
def _isolated_event_bus_env(monkeypatch):
    """Start every test from "the variable is not set"."""
    monkeypatch.delenv(EVENT_BUS_ENV, raising=False)


@pytest.fixture()
def health_client(tmp_path, monkeypatch):
    """``/api/health`` client with the plan dir pinned to ``tmp_path``.

    Mirrors the calling idiom of
    ``tests/unit/test_topology_identity.py::test_health_includes_topology_and_keeps_existing_keys``:
    the endpoint is exercised through the real app rather than called as a
    function, so the serialized payload is what gets asserted.
    """
    monkeypatch.setattr(pipeline.paths, "PLAN_DIR", tmp_path / "plans")
    monkeypatch.setattr(event_wiring, "PLAN_DIR", tmp_path / "plans")
    return TestClient(app)


# ---------------------------------------------------------------------------
# bus selection
# ---------------------------------------------------------------------------
def test_default_env_unset_returns_todays_bus():
    """Unset keeps today's behavior: a fresh in-process bus.

    ``build_bus()`` has never been a singleton -- the process-level cache lives
    in ``pipeline.event_wiring.get_bus`` -- so the assertion is on the type,
    not on object identity between two calls.
    """
    assert type(build_bus()) is InProcessEventBus


def test_inprocess_matches_default():
    """``inprocess`` is indistinguishable from unset."""
    default_type = type(build_bus())
    bus = build_bus()
    assert type(bus) is default_type
    assert type(bus) is InProcessEventBus


def test_jsonl_builds_jsonl_event_bus(monkeypatch):
    monkeypatch.setenv(EVENT_BUS_ENV, "jsonl")
    assert isinstance(build_bus(), JsonlEventBus)


def test_jsonl_bus_writes_under_the_plan_dir(monkeypatch, tmp_path):
    """The documented root of the ``jsonl`` bus is ``pipeline.paths.PLAN_DIR``.

    ``JsonlEventBus.publish`` writes ``<root>/<plan>.events.jsonl``; asserting
    on that artifact rather than on the private ``_root`` attribute pins the
    "which path does this bus use" answer by behavior.
    """
    root = tmp_path / "event-log"
    monkeypatch.setattr(event_wiring, "PLAN_DIR", root)
    monkeypatch.setenv(EVENT_BUS_ENV, "jsonl")

    build_bus().publish({"plan": "demo", "type": "notification"})

    assert (root / "demo.events.jsonl").exists()


def test_unknown_value_raises_value_error(monkeypatch):
    monkeypatch.setenv(EVENT_BUS_ENV, "nope")
    with pytest.raises(ValueError) as excinfo:
        build_bus()
    message = str(excinfo.value)
    assert EVENT_BUS_ENV in message
    assert "inprocess" in message
    assert "jsonl" in message
    # Secure-by-Design: the rejected value is not echoed back.
    assert "nope" not in message


def test_empty_string_value_raises_value_error(monkeypatch):
    """``""`` is not "unset": only an absent variable selects the default."""
    monkeypatch.setenv(EVENT_BUS_ENV, "")
    with pytest.raises(ValueError) as excinfo:
        build_bus()
    assert EVENT_BUS_ENV in str(excinfo.value)


def test_selection_is_not_cached_between_calls(monkeypatch):
    """Call 2 must follow the environment, not call 1's answer."""
    monkeypatch.setenv(EVENT_BUS_ENV, "jsonl")
    assert isinstance(build_bus(), JsonlEventBus)

    monkeypatch.setenv(EVENT_BUS_ENV, "inprocess")
    assert type(build_bus()) is InProcessEventBus

    monkeypatch.delenv(EVENT_BUS_ENV, raising=False)
    assert type(build_bus()) is InProcessEventBus


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


def test_health_keeps_topology_and_runtime_keys(health_client):
    res = health_client.get("/api/health")

    assert res.status_code == 200, res.text
    body = res.json()
    assert "ok" in body
    assert "topology" in body
    assert "runtime" in body
    assert "event_bus" in body["runtime"]
