"""Plan settings resolve in the namespace bound for the call (pns-01).

A manifest is stored under its namespace and does not carry one, so the
settings context must take the namespace from the bound namespace. Each test
runs against the real Store under ``tmp_path`` with a fixture team layer, never
the live configuration.
"""

from __future__ import annotations

import json

import pytest

from pipeline import persistence, server, settings_store
from pipeline.actor import bind_namespace
from pipeline.ci import DEFAULT_NAMESPACE, context_from_manifest, settings_for_plan
from pipeline.role_routing_scope import layer_routing_for_manifest

TEAM_NAMESPACE = "acme/web"
OTHER_NAMESPACE = "acme/api"
TEAM = "alpha"
REPO = "https://github.com/acme/app-a.git"
ROUTING = {"dispatch": {"provider": "ollama", "model": "gpt-oss"}}
MODEL_REGISTRY = {
    "providers": {
        "claude": {"models": {"sonnet": {"tag": "claude-sonnet-5-5"}}},
        "ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b", "class": "on-device"}}},
    },
    "roles": {},
}


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(persistence, "PLAN_DIR", plans)
    yield plans


@pytest.fixture(autouse=True)
def model_registry(tmp_path, monkeypatch):
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps(MODEL_REGISTRY))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    return path


class _FixtureRecords:
    """A record view over fixture dicts: settings and manifests by namespace.

    Only the reads the settings path makes are served. Any other namespace than
    the one a fixture row was written under finds nothing, as a real store would.
    """

    def __init__(self) -> None:
        self.settings: dict[tuple[str, str, str, str], dict] = {}
        self.manifests: dict[tuple[str, str], dict] = {}

    def get_settings(self, namespace, scope, scope_id, repo):
        return self.settings.get((namespace, scope, scope_id, repo))

    def get_manifest(self, namespace, plan_name):
        return self.manifests.get((namespace, plan_name))


class _FixtureStore:
    def __init__(self, records: _FixtureRecords) -> None:
        self._records = records

    def records(self) -> _FixtureRecords:
        return self._records


@pytest.fixture(autouse=True)
def records(monkeypatch):
    """The settings source and the manifest store, both fixture-backed."""
    fixture = _FixtureRecords()
    monkeypatch.setattr(settings_store, "_store", _FixtureStore(fixture))
    monkeypatch.setattr(server, "_store", _FixtureStore(fixture), raising=False)
    return fixture


def _put_team(records, namespace, key, value):
    records.settings[(namespace, "team", TEAM, "")] = {
        "values": {key: value},
        "allow_loosen": [],
    }


def _manifest(**extra):
    return {"team": TEAM, "repo_url": REPO, **extra}


# --- the bound namespace supplies the context namespace ---


def test_should_take_the_bound_namespace_when_manifest_names_none():
    with bind_namespace(TEAM_NAMESPACE):
        context = context_from_manifest(_manifest())

    assert context.namespace == TEAM_NAMESPACE


def test_should_use_the_default_namespace_when_nothing_is_bound_and_manifest_names_none():
    context = context_from_manifest(_manifest())

    assert context.namespace == DEFAULT_NAMESPACE


def test_should_prefer_the_manifest_namespace_over_the_bound_one():
    with bind_namespace(TEAM_NAMESPACE):
        context = context_from_manifest(_manifest(namespace=OTHER_NAMESPACE))

    assert context.namespace == OTHER_NAMESPACE


def test_should_fall_back_to_the_bound_namespace_when_manifest_namespace_is_empty():
    with bind_namespace(TEAM_NAMESPACE):
        context = context_from_manifest(_manifest(namespace=""))

    assert context.namespace == TEAM_NAMESPACE


def test_should_fall_back_to_the_bound_namespace_when_manifest_namespace_is_none():
    with bind_namespace(TEAM_NAMESPACE):
        context = context_from_manifest(_manifest(namespace=None))

    assert context.namespace == TEAM_NAMESPACE


# --- routing read by a plan in a bound team namespace ---


def test_should_read_the_team_routing_of_the_bound_namespace(records):
    _put_team(records, TEAM_NAMESPACE, "role_routing", ROUTING)

    with bind_namespace(TEAM_NAMESPACE):
        routing = layer_routing_for_manifest(_manifest())

    assert routing == ROUTING


def test_should_not_read_another_teams_routing(records):
    _put_team(records, TEAM_NAMESPACE, "role_routing", ROUTING)

    with bind_namespace(OTHER_NAMESPACE):
        routing = layer_routing_for_manifest(_manifest())

    assert routing == {}


# --- merge gate of a plan stored under a team namespace ---


def test_should_resolve_a_plan_merge_gate_from_its_teams_layer(records):
    _put_team(records, TEAM_NAMESPACE, "merge_ci_gate", False)
    records.manifests[(TEAM_NAMESPACE, "calc-subtract")] = _manifest()

    with bind_namespace(TEAM_NAMESPACE):
        context = settings_for_plan("calc-subtract")

    assert context.value("merge_ci_gate") is False


def test_should_return_the_default_namespace_for_an_unknown_plan_name():
    with bind_namespace(TEAM_NAMESPACE):
        context = settings_for_plan("no-such-plan")

    assert context.namespace == DEFAULT_NAMESPACE
    assert context.team == "local"


# --- the default namespace is unchanged ---


def test_should_resolve_a_default_team_setting_as_before(records):
    records.settings[(DEFAULT_NAMESPACE, "team", "local", "")] = {
        "values": {"merge_ci_gate": False},
        "allow_loosen": [],
    }

    context = context_from_manifest({"team": "local"})

    assert context.value("merge_ci_gate") is False
