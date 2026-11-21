"""Layer ``notification_routes`` are applied at spool time and drained in every namespace."""

from __future__ import annotations

import logging

import pytest

import pipeline.server  # noqa: F401  - breaks a pre-existing import cycle
from pipeline import notification_channels as nc
from pipeline import notification_outbox as ob
from pipeline import persistence, server
from pipeline.actor import bind_namespace
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import validate_namespace

SECRET = "hunter2-layer-secret"
SENT: list[tuple[str, dict]] = []


class _LayerChannel:
    type_name = "fake_nau01"

    def __init__(self, name, token):
        self.name = name
        self.label = token
        self.outbox_suffix = f".outbox.{name}.jsonl"

    @classmethod
    def from_settings(cls, name, settings, env):
        return cls(name, settings.get("label"))

    def send(self, record):
        SENT.append((self.name, record))
        return nc.DeliveryResult(nc.DeliveryStatus.DELIVERED)


class _MultiNamespaceRecords(InMemoryRecordStore):
    """The in-memory store, serving settings in every namespace like the file store."""

    def _draft_namespace(self, namespace):
        validate_namespace(namespace)


class _Store:
    def __init__(self):
        self._records = _MultiNamespaceRecords()

    def records(self):
        return self._records


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    if "fake_nau01" not in nc.registered_types():
        nc.register_channel_type(_LayerChannel)
    SENT.clear()
    store = _Store()
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(ob, "_store", store, raising=False)
    monkeypatch.setattr(persistence, "_store", store, raising=False)
    monkeypatch.setenv("PIPELINE_NOTIFY_CONFIG_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setattr(nc, "_BREAKERS", {})
    return store


def _routes(instance, event="story_parked", plan="*", label="t"):
    return {
        "instances": {instance: {"type": "fake_nau01", "label": label}},
        "routes": [
            {
                "name": f"r-{instance}",
                "match": {"plan": plan},
                "deliver": [{"events": [event], "to": [instance]}],
            }
        ],
    }


def _put_layer(store, namespace, team, value):
    store.records().put_settings(
        namespace, "team", team, "", {"notification_routes": value}, [], "test", None
    )


def _plan(store, namespace, name="plan-a", team="blue"):
    store.records().put_manifest(namespace, name, {"team": team, "stories": {}})


def _spool(plan="plan-a", event="story_parked"):
    ob._spool_instances(plan, event, {"type": "notification", "plan": plan, "payload": {"event": event}})


def _queues(store, namespace):
    return sorted(store.records().list_outboxes(namespace))


def _write_deployment(monkeypatch, tmp_path, instance, plan="*"):
    import json

    path = tmp_path / "deploy.json"
    cfg = _routes(instance, plan=plan)
    cfg["version"] = 1
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("PIPELINE_NOTIFY_CONFIG_PATH", str(path))


def test_team_layer_route_spools_to_layer_instance(env):
    _plan(env, "default")
    _put_layer(env, "default", "blue", _routes("blue-mail"))
    _spool()
    assert _queues(env, "default") == [("plan-a", "blue-mail")]


def test_unset_layer_spools_nothing_without_a_deployment_file(env):
    _plan(env, "default")
    _spool()
    assert _queues(env, "default") == []


def test_unset_layer_matches_deployment_only_behaviour(env, monkeypatch, tmp_path):
    _write_deployment(monkeypatch, tmp_path, "host-mail")
    _plan(env, "default")
    _spool()
    assert _queues(env, "default") == [("plan-a", "host-mail")]


def test_layer_of_another_team_is_not_applied(env):
    _plan(env, "default", team="red")
    _put_layer(env, "default", "blue", _routes("blue-mail"))
    _spool()
    assert _queues(env, "default") == []


def test_deployment_route_wins_over_layer_route(env, monkeypatch, tmp_path):
    _write_deployment(monkeypatch, tmp_path, "host-mail")
    _plan(env, "default")
    _put_layer(env, "default", "blue", _routes("blue-mail"))
    _spool()
    assert _queues(env, "default") == [("plan-a", "host-mail")]


def test_unreadable_manifest_applies_no_layer_routes(env):
    _put_layer(env, "default", "blue", _routes("blue-mail"))
    _spool(plan="missing-plan")
    assert _queues(env, "default") == []


def test_record_spooled_under_team_namespace_is_drained_from_unbound_context(env):
    _plan(env, "team-a")
    _put_layer(env, "team-a", "blue", _routes("blue-mail"))
    with bind_namespace("team-a"):
        _spool()
    assert _queues(env, "team-a") == [("plan-a", "blue-mail")]

    delivered = ob.drain_channel_outboxes()

    assert delivered == {"blue-mail": 1}
    assert [name for name, _ in SENT] == ["blue-mail"]
    assert _queues(env, "team-a") == []


def test_duplicate_instance_with_different_settings_is_not_drained(env, caplog):
    _plan(env, "team-a")
    _put_layer(env, "team-a", "blue", _routes("shared", label=SECRET))
    _put_layer(env, "team-a", "red", _routes("shared", label="other"))
    with bind_namespace("team-a"):
        env.records().append_outbox("team-a", "plan-a", "shared", {"plan": "plan-a"})

    with caplog.at_level(logging.WARNING):
        delivered = ob.drain_channel_outboxes()

    assert "shared" not in delivered
    assert SENT == []
    assert _queues(env, "team-a") == [("plan-a", "shared")]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("team-a" in m and "shared" in m for m in warnings)
    assert not any(SECRET in r.getMessage() or "other" in r.getMessage() for r in caplog.records)


def test_duplicate_instance_with_identical_settings_is_drained(env):
    _plan(env, "team-a")
    _put_layer(env, "team-a", "blue", _routes("shared"))
    _put_layer(env, "team-a", "red", _routes("shared"))
    env.records().append_outbox("team-a", "plan-a", "shared", {"plan": "plan-a"})

    assert ob.drain_channel_outboxes() == {"shared": 1}


def test_invalid_layer_value_does_not_stop_another_namespace(env, caplog):
    _plan(env, "team-a")
    _plan(env, "team-b")
    _put_layer(env, "team-a", "blue", {"instances": {"x": {"type": "nope"}}})
    _put_layer(env, "team-b", "blue", _routes("b-mail"))
    env.records().append_outbox("team-b", "plan-a", "b-mail", {"plan": "plan-a"})

    with caplog.at_level(logging.ERROR):
        delivered = ob.drain_channel_outboxes()

    assert delivered == {"b-mail": 1}
    assert any("team-a" in r.getMessage() for r in caplog.records)


def test_one_namespace_store_failure_does_not_stop_another(env, monkeypatch):
    _plan(env, "team-a")
    _plan(env, "team-b")
    _put_layer(env, "team-b", "blue", _routes("b-mail"))
    env.records().append_outbox("team-b", "plan-a", "b-mail", {"plan": "plan-a"})
    real = env.records().list_settings

    def flaky(namespace):
        if namespace == "team-a":
            raise RuntimeError("boom")
        return real(namespace)

    monkeypatch.setattr(env.records(), "list_settings", flaky)

    assert ob.drain_channel_outboxes() == {"b-mail": 1}


def test_counts_are_summed_across_namespaces(env):
    for ns in ("team-a", "team-b"):
        _plan(env, ns)
        _put_layer(env, ns, "blue", _routes("same-name"))
        env.records().append_outbox(ns, "plan-a", "same-name", {"plan": "plan-a"})

    assert ob.drain_channel_outboxes() == {"same-name": 2}
