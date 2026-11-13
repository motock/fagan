"""The JSONL event bus writes under the host-local root (ADR 0012 decision 9).

``PIPELINE_EVENT_BUS=jsonl`` is an EventBus implementation for single-host use,
so its log directory is ``HostState.root()``, resolved through the
``pipeline.server.PLAN_DIR`` binding.

Contract under test:

* The jsonl bus appends ``<host root>/<plan>.events.jsonl`` under the host root.
* The plugin and NATS buses never touch the host root.
"""

import pytest

import pipeline.server  # noqa: F401 - pre-existing import cycle (see test_wake.py)
from pipeline import event_wiring
from pipeline.event_wiring import build_bus
from pipeline.events import InProcessEventBus, make_event


@pytest.fixture()
def host_root(tmp_path, monkeypatch):
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setattr("pipeline.server.PLAN_DIR", root)
    monkeypatch.delenv(event_wiring.EVENT_BUS_ENV, raising=False)
    return root


def test_jsonl_bus_appends_its_log_under_the_host_root(host_root, monkeypatch):
    monkeypatch.setenv(event_wiring.EVENT_BUS_ENV, "jsonl")

    build_bus().publish(make_event("agent_done", "plan-a", "S-1"))

    assert (host_root / "plan-a.events.jsonl").is_file()


def build_inprocess_bus():
    """Plugin factory named by ``PIPELINE_EVENT_BUS_PLUGIN`` in the test below."""
    return InProcessEventBus()


def test_plugin_bus_never_touches_the_host_root(host_root, monkeypatch):
    monkeypatch.setenv("PIPELINE_EVENT_BUS_PLUGIN", f"{__name__}:build_inprocess_bus")

    build_bus()

    assert list(host_root.iterdir()) == []


def test_nats_bus_never_touches_the_host_root(host_root, monkeypatch):
    monkeypatch.setenv(event_wiring.EVENT_BUS_ENV, "nats")
    monkeypatch.setattr(event_wiring, "NatsEventBus", InProcessEventBus)

    build_bus()

    assert list(host_root.iterdir()) == []
