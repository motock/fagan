import json

import pytest

from pipeline.settings_registry import REGISTRY
from pipeline.settings_resolve import SAFE_DEFAULT_SOURCE, resolve

KEY = "role_routing"
REGISTRY_ENV = "PIPELINE_MODEL_REGISTRY_PATH"
ROUTING = {"review": {"provider": "ollama", "model": "gpt-oss"}}
OTHER_ROUTING = {"review": {"provider": "claude", "model": "sonnet"}}
MODEL_REGISTRY = {
    "providers": {
        "claude": {"models": {"sonnet": {"tag": "claude-sonnet-5-5"}}},
        "ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b", "class": "on-device"}}},
    },
    "roles": {},
}


@pytest.fixture
def live_registry(tmp_path, monkeypatch):
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps(MODEL_REGISTRY))
    monkeypatch.setenv(REGISTRY_ENV, str(path))
    return path


def test_role_routing_is_a_pref_key_with_an_empty_safe_default():
    key = REGISTRY[KEY]
    assert (key.kind, key.safe_value) == ("pref", {})


def test_role_routing_is_read_from_the_non_deployment_layers_only():
    assert REGISTRY[KEY].allowed_layers == ("org", "team", "team_repo")


def test_accepts_a_role_pinned_to_a_declared_provider_and_model(live_registry):
    assert REGISTRY[KEY].accepts(ROUTING)


def test_refuses_a_model_not_declared_under_its_provider(live_registry):
    value = {"review": {"provider": "ollama", "model": "sonnet"}}
    assert not REGISTRY[KEY].accepts(value)


def test_refuses_an_undeclared_provider(live_registry):
    value = {"review": {"provider": "unknown", "model": "gpt-oss"}}
    assert not REGISTRY[KEY].accepts(value)


def test_refuses_a_role_entry_missing_its_model(live_registry):
    assert not REGISTRY[KEY].accepts({"review": {"provider": "ollama"}})


def test_refuses_a_role_entry_with_an_extra_field(live_registry):
    value = {"review": {"provider": "ollama", "model": "gpt-oss", "tag": "x"}}
    assert not REGISTRY[KEY].accepts(value)


@pytest.mark.parametrize("value", [["review"], "ollama", 3, True])
def test_refuses_a_value_that_is_not_a_role_mapping(live_registry, value):
    assert not REGISTRY[KEY].accepts(value)


def test_refuses_every_value_when_no_model_registry_exists(tmp_path, monkeypatch):
    monkeypatch.setenv(REGISTRY_ENV, str(tmp_path / "missing.json"))
    assert not REGISTRY[KEY].accepts(ROUTING)


def test_team_layer_routing_applies(live_registry):
    result = resolve(KEY, {"team": {KEY: ROUTING}})
    assert (result["value"], result["source"]) == (ROUTING, "team")


def test_team_repo_routing_wins_over_team_routing(live_registry):
    layers = {"team": {KEY: ROUTING}, "team_repo": {KEY: OTHER_ROUTING}}
    result = resolve(KEY, layers)
    assert (result["value"], result["source"]) == (OTHER_ROUTING, "team_repo")


def test_deployment_layer_routing_is_refused(live_registry):
    result = resolve(KEY, {"deployment": {KEY: ROUTING}})
    assert (result["value"], result["source"]) == ({}, SAFE_DEFAULT_SOURCE)
    assert result["invalid_layers"] == ["deployment"]


def test_invalid_team_routing_is_refused_and_falls_back(live_registry):
    bad = {"review": {"provider": "ollama", "model": "sonnet"}}
    result = resolve(KEY, {"team": {KEY: bad}})
    assert (result["value"], result["source"]) == ({}, SAFE_DEFAULT_SOURCE)
    assert result["invalid_layers"] == ["team"]


def test_unset_routing_at_every_layer_resolves_to_the_empty_default(live_registry):
    result = resolve(KEY, {"team": {}, "org": {}, "team_repo": {}})
    assert (result["value"], result["source"]) == ({}, SAFE_DEFAULT_SOURCE)
    assert result["invalid_layers"] == []
