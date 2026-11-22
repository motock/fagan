import json

import pytest

from app.role_registry import RoleRegistryError, resolve_role
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


ROLE_REGISTRY = {
    "providers": MODEL_REGISTRY["providers"],
    "roles": {"review": {"provider": "claude", "model": "sonnet"}},
}
RESOLVED_REGISTRY_DEFAULT = ("claude", "claude-sonnet-5-5")
RESOLVED_LAYER_ROUTING = ("ollama", "gpt-oss:20b")


def test_layer_routing_beats_the_registry_default():
    resolution = resolve_role("review", registry=ROLE_REGISTRY, layer_routing=ROUTING)
    assert (resolution.provider, resolution.model) == RESOLVED_LAYER_ROUTING


def test_plan_role_config_beats_layer_routing():
    plan = {"review": {"provider": "claude", "model": "sonnet"}}
    resolution = resolve_role(
        "review", registry=ROLE_REGISTRY, plan_role_config=plan, layer_routing=ROUTING
    )
    assert (resolution.provider, resolution.model) == RESOLVED_REGISTRY_DEFAULT


def test_layer_model_is_not_paired_with_a_plan_provider_override():
    plan = {"review": {"provider": "claude"}}
    resolution = resolve_role(
        "review",
        registry={"providers": MODEL_REGISTRY["providers"], "roles": {}},
        plan_role_config=plan,
        layer_routing=ROUTING,
        model_fallback="caller-default",
    )
    assert (resolution.provider, resolution.model) == ("claude", "caller-default")


@pytest.mark.parametrize("layer_routing", [None, {}])
def test_unset_layer_routing_keeps_the_registry_default(layer_routing):
    resolution = resolve_role(
        "review", registry=ROLE_REGISTRY, layer_routing=layer_routing
    )
    assert (resolution.provider, resolution.model) == RESOLVED_REGISTRY_DEFAULT


def test_layer_routing_for_another_role_leaves_this_role_on_its_default():
    resolution = resolve_role(
        "review", registry=ROLE_REGISTRY, layer_routing={"planner": ROUTING["review"]}
    )
    assert (resolution.provider, resolution.model) == RESOLVED_REGISTRY_DEFAULT


def test_layer_routing_must_be_passed_by_keyword():
    with pytest.raises(TypeError):
        resolve_role("review", ROUTING, registry=ROLE_REGISTRY)


def test_layer_routing_naming_an_undeclared_model_is_refused():
    bad = {"review": {"provider": "ollama", "model": "sonnet"}}
    with pytest.raises(RoleRegistryError, match="not declared under"):
        resolve_role("review", registry=ROLE_REGISTRY, layer_routing=bad)


# --- undeclared Ollama cloud models (the value check matches resolution) ----

HOSTED_ENDPOINT = "https://ollama.com"
LOCAL_ENDPOINT = "http://localhost:11434"
ENDPOINT_ENVS = ("PIPELINE_LOCAL_ENDPOINT_OLLAMA", "PIPELINE_LOCAL_ENDPOINT")


def _pin(provider, model):
    return {"review": {"provider": provider, "model": model}}


@pytest.fixture
def hosted_endpoint(live_registry, monkeypatch):
    for name in ENDPOINT_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", HOSTED_ENDPOINT)


@pytest.fixture
def local_endpoint(live_registry, monkeypatch):
    for name in ENDPOINT_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", LOCAL_ENDPOINT)


@pytest.fixture
def no_endpoint(live_registry, monkeypatch):
    for name in ENDPOINT_ENVS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("model", ["glm-5.3", "gemma4:31b", "a" * 64])
def test_accepts_an_undeclared_ollama_model_on_a_hosted_endpoint(hosted_endpoint, model):
    assert REGISTRY[KEY].accepts(_pin("ollama", model))


@pytest.mark.parametrize("endpoint_fixture", ["no_endpoint", "local_endpoint"])
def test_refuses_a_plain_undeclared_ollama_model_without_a_remote_endpoint(
    request, endpoint_fixture
):
    request.getfixturevalue(endpoint_fixture)
    assert not REGISTRY[KEY].accepts(_pin("ollama", "glm-5.3"))


@pytest.mark.parametrize("endpoint_fixture", ["no_endpoint", "local_endpoint", "hosted_endpoint"])
def test_accepts_a_cloud_tagged_undeclared_model_on_any_endpoint(request, endpoint_fixture):
    request.getfixturevalue(endpoint_fixture)
    assert REGISTRY[KEY].accepts(_pin("ollama", "glm-5.3:cloud"))


@pytest.mark.parametrize(
    "pin",
    [
        {"provider": "claude", "model": "glm-5.3"},
        {"provider": "ollama", "model": ""},
        {"provider": "ollama", "model": "GLM-5.3"},
        {"provider": "ollama", "model": "glm 5.3"},
        {"provider": "ollama", "model": "library/glm"},
        {"provider": "ollama", "model": "a" * 65},
        {"provider": "ollama", "model": 3},
    ],
)
def test_refuses_an_undeclared_model_the_name_rule_or_provider_rejects(hosted_endpoint, pin):
    assert not REGISTRY[KEY].accepts({"review": pin})


def test_stored_undeclared_cloud_model_resolves_for_its_team(hosted_endpoint):
    result = resolve(KEY, {"team": {KEY: _pin("ollama", "glm-5.3")}})
    assert (result["source"], result["invalid_layers"]) == ("team", [])

    resolution = resolve_role("review", registry=ROLE_REGISTRY, layer_routing=result["value"])
    assert (resolution.provider, resolution.model) == ("ollama", "glm-5.3")
