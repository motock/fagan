"""resolve_role accepts an undeclared cloud model routed through role_routing.

A hosted deployment's team routes a role to the Ollama provider with a model
its Ollama account offers (``kimi-k3``, ``gemma4:31b``) that the operator's
registry file does not list. Such a layer-routed name resolves to itself
when the endpoint is remote (a plain name) or the tag carries ``:cloud``.
Every other undeclared name keeps raising RoleRegistryError.

Every test passes a synthetic ``registry`` and ``environ`` straight into
resolve_role; the endpoint source is stubbed, so neither the live registry
nor the live environment is read.
"""
from __future__ import annotations

import pytest

from app import role_registry
from app.role_registry import RoleRegistryError, resolve_role

_CLOUD_ENDPOINT = "https://ollama.com"
_LOCAL_ENDPOINT = "http://host.docker.internal:11434"

# Only llama3 is declared; kimi-k3 and gemma4:31b are deliberately absent.
_PROVIDERS = {
    "claude": {"models": {"sonnet": {"tag": "claude-sonnet-4-5"}}},
    "ollama": {"models": {"llama3": {"tag": "llama3:8b", "class": "capable"}}},
}


def _registry(roles: dict | None = None) -> dict:
    reg: dict = {"providers": _PROVIDERS}
    if roles is not None:
        reg["roles"] = roles
    return reg


@pytest.fixture(autouse=True)
def endpoint(monkeypatch) -> dict:
    """Stub the configured Ollama endpoint; default is none configured."""
    state = {"value": ""}
    monkeypatch.setattr(
        role_registry, "endpoint_from_env", lambda: state["value"], raising=False
    )
    return state


def _route(role: str, provider: str, model: str) -> dict:
    return {role: {"provider": provider, "model": model}}


# ---------------------------------------------------------------------------
# 1. POSITIVE: an undeclared cloud model routed through role_routing resolves
# ---------------------------------------------------------------------------

def test_undeclared_plain_name_on_remote_endpoint_resolves_to_itself(endpoint):
    endpoint["value"] = _CLOUD_ENDPOINT
    res = resolve_role(
        "dispatch",
        registry=_registry(),
        environ={},
        layer_routing=_route("dispatch", "ollama", "kimi-k3"),
    )
    assert (res.provider, res.model) == ("ollama", "kimi-k3")


def test_undeclared_tag_on_remote_endpoint_resolves_to_itself(endpoint):
    endpoint["value"] = _CLOUD_ENDPOINT
    res = resolve_role(
        "dispatch",
        registry=_registry(),
        environ={},
        layer_routing=_route("dispatch", "ollama", "gemma4:31b"),
    )
    assert res.model == "gemma4:31b"


def test_undeclared_cloud_tag_on_local_endpoint_resolves_to_itself(endpoint):
    endpoint["value"] = _LOCAL_ENDPOINT
    res = resolve_role(
        "dispatch",
        registry=_registry(),
        environ={},
        layer_routing=_route("dispatch", "ollama", "kimi-k3:cloud"),
    )
    assert res.model == "kimi-k3:cloud"


def test_layer_routed_declared_name_resolves_to_registry_tag_and_class(endpoint):
    endpoint["value"] = _CLOUD_ENDPOINT
    res = resolve_role(
        "dispatch",
        registry=_registry(),
        environ={},
        layer_routing=_route("dispatch", "ollama", "llama3"),
    )
    assert (res.model, res.strength_class) == ("llama3:8b", "capable")


def test_role_review_accepts_undeclared_cloud_model(endpoint):
    endpoint["value"] = _CLOUD_ENDPOINT
    res = resolve_role(
        "review",
        registry=_registry(),
        environ={},
        layer_routing=_route("review", "ollama", "kimi-k3"),
    )
    assert (res.provider, res.model) == ("ollama", "kimi-k3")


# ---------------------------------------------------------------------------
# 2. NEGATIVE: the no-local-models rule and the other gates still raise
# ---------------------------------------------------------------------------

def test_undeclared_plain_name_on_local_endpoint_raises(endpoint):
    endpoint["value"] = _LOCAL_ENDPOINT
    with pytest.raises(RoleRegistryError):
        resolve_role(
            "dispatch",
            registry=_registry(),
            environ={},
            layer_routing=_route("dispatch", "ollama", "qwen3:14b"),
        )


def test_undeclared_plain_name_with_no_endpoint_raises(endpoint):
    endpoint["value"] = ""
    with pytest.raises(RoleRegistryError):
        resolve_role(
            "dispatch",
            registry=_registry(),
            environ={},
            layer_routing=_route("dispatch", "ollama", "kimi-k3"),
        )


@pytest.mark.parametrize(
    "bad_name",
    ["Bad Name", "../x", "a/b", "kimi:", "k" * 70],
    ids=["space", "traversal", "slash", "empty-tag", "too-long"],
)
def test_invalid_name_raises_without_echoing_it(endpoint, bad_name):
    endpoint["value"] = _CLOUD_ENDPOINT
    with pytest.raises(RoleRegistryError) as exc:
        resolve_role(
            "dispatch",
            registry=_registry(),
            environ={},
            layer_routing=_route("dispatch", "ollama", bad_name),
        )
    assert bad_name not in str(exc.value)


def test_undeclared_error_names_role_and_provider(endpoint):
    endpoint["value"] = _LOCAL_ENDPOINT
    with pytest.raises(RoleRegistryError) as exc:
        resolve_role(
            "dispatch",
            registry=_registry(),
            environ={},
            layer_routing=_route("dispatch", "ollama", "qwen3:14b"),
        )
    assert "'dispatch'" in str(exc.value) and "'ollama'" in str(exc.value)


def test_undeclared_model_named_by_plan_role_config_still_raises(endpoint):
    endpoint["value"] = _CLOUD_ENDPOINT
    with pytest.raises(RoleRegistryError):
        resolve_role(
            "dispatch",
            registry=_registry(),
            environ={},
            plan_role_config={"dispatch": {"provider": "ollama", "model": "kimi-k3"}},
        )


def test_undeclared_model_in_registry_roles_entry_still_raises(endpoint):
    endpoint["value"] = _CLOUD_ENDPOINT
    reg = _registry(roles={"dispatch": {"provider": "ollama", "model": "kimi-k3"}})
    with pytest.raises(RoleRegistryError):
        resolve_role("dispatch", registry=reg, environ={})


def test_undeclared_model_on_claude_provider_still_raises(endpoint):
    endpoint["value"] = _CLOUD_ENDPOINT
    with pytest.raises(RoleRegistryError):
        resolve_role(
            "dispatch",
            registry=_registry(),
            environ={},
            layer_routing=_route("dispatch", "claude", "kimi-k3"),
        )
