"""The chat turn resolves its provider and model through the caller's team routing.

Before this fix ``ChatService._resolve_driver`` called ``resolve_role`` with no
``layer_routing``, so a team that routed ``chat`` to another provider was
ignored and the Claude CLI ran the turn. Dispatch and review already pass the
team's ``role_routing``; the chat turn must do the same for its own namespace.

Only ``backend.get_backend``, the model registry loader and the settings Store
read are stubbed. ``resolve_role`` runs for real (spied on, never replaced), so
the assertions follow its own priority rules.
"""

from __future__ import annotations

import logging

import pytest

from app import backend, role_registry
from app.chat import ChatService
from pipeline import settings_store
from pipeline.actor import bind_namespace

TEAM_NAMESPACE = "acme/web"
ROUTING = {"chat": {"provider": "ollama", "model": "deepseek-v4.1-flash"}}
OLLAMA_TAG = "deepseek-v4.1-flash:cloud"

_REGISTRY_WITHOUT_CHAT = {
    "providers": {
        "claude": {"models": {"sonnet": {"tag": "claude-sonnet-5-5"}}},
        "ollama": {"models": {"deepseek-v4.1-flash": {"tag": OLLAMA_TAG}}},
    },
    "roles": {},
}


class _Driver:
    model = ""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_CHAT", raising=False)
    monkeypatch.setattr(role_registry, "load_registry", lambda: _REGISTRY_WITHOUT_CHAT)


@pytest.fixture
def backend_calls(monkeypatch):
    """Record each ``get_backend`` call; return a driver for it."""
    calls: list[dict] = []

    def fake_get_backend(role, *, name):
        calls.append({"role": role, "name": name})
        return _Driver()

    monkeypatch.setattr(backend, "get_backend", fake_get_backend)
    return calls


@pytest.fixture
def routing_store(monkeypatch):
    """The settings Store read for ``role_routing``, answered per test.

    ``state["value"]`` is what the Store returns; ``state["exc"]`` makes the
    read raise. Every call is recorded so a test can prove a read happened.
    """
    state: dict = {"value": ROUTING, "source": "team", "exc": None, "reads": []}

    def fake_effective_setting(key, *, namespace, team, repo, **_kwargs):
        state["reads"].append({"key": key, "namespace": namespace, "repo": repo})
        if state["exc"] is not None:
            raise state["exc"]
        return {"value": state["value"], "source": state["source"]}

    monkeypatch.setattr(settings_store, "effective_setting", fake_effective_setting)
    return state


@pytest.fixture
def resolve_spy(monkeypatch):
    """Spy on resolve_role: calls go through to the real function, kwargs are kept."""
    calls: list[dict] = []
    real = role_registry.resolve_role

    def spy(role, **kwargs):
        calls.append({"role": role, "kwargs": kwargs})
        return real(role, **kwargs)

    monkeypatch.setattr(role_registry, "resolve_role", spy)
    return calls


def test_should_run_the_chat_on_the_team_routed_provider(routing_store, backend_calls):
    with bind_namespace(TEAM_NAMESPACE):
        driver, model = ChatService()._resolve_driver()

    assert backend_calls == [{"role": "chat", "name": "ollama"}]
    assert model == OLLAMA_TAG
    assert isinstance(driver, _Driver)


def test_should_not_run_the_chat_on_claude_when_the_team_routes_to_ollama(routing_store, backend_calls):
    with bind_namespace(TEAM_NAMESPACE):
        ChatService()._resolve_driver()

    assert {"role": "chat", "name": "claude"} not in backend_calls


def test_should_resolve_as_today_when_the_team_sets_no_routing(routing_store, backend_calls, resolve_spy):
    routing_store["value"] = {}
    routing_store["source"] = "safe_default"

    _driver, model = ChatService()._resolve_driver()

    assert backend_calls == [{"role": "chat", "name": "claude"}]
    assert model == "sonnet"
    assert resolve_spy and "layer_routing" not in resolve_spy[-1]["kwargs"]


def test_should_follow_resolve_role_priority_when_registry_and_team_disagree(
    monkeypatch, routing_store, backend_calls, resolve_spy
):
    registry = {
        "providers": _REGISTRY_WITHOUT_CHAT["providers"],
        "roles": {"chat": {"provider": "claude", "model": "sonnet"}},
    }
    monkeypatch.setattr(role_registry, "load_registry", lambda: registry)

    with bind_namespace(TEAM_NAMESPACE):
        _driver, model = ChatService()._resolve_driver()

    expected = role_registry.resolve_role(
        "chat", registry=registry, model_fallback=lambda: "unused", layer_routing=ROUTING
    )
    assert backend_calls == [{"role": "chat", "name": expected.provider}]
    assert model == expected.model


def test_should_resolve_as_unset_when_the_settings_read_raises(
    routing_store, backend_calls, resolve_spy, caplog
):
    routing_store["exc"] = RuntimeError("store unavailable")

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        _driver, model = ChatService()._resolve_driver()

    assert backend_calls == [{"role": "chat", "name": "claude"}]
    assert model == "sonnet"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "role_routing" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "malformed",
    [{"chat": "ollama"}, {"chat": {"provider": ""}}, None],
    ids=["string-entry", "empty-provider", "null-mapping"],
)
def test_should_resolve_as_unset_when_routing_is_malformed(malformed, routing_store, backend_calls):
    routing_store["value"] = malformed

    _driver, model = ChatService()._resolve_driver()

    assert backend_calls == [{"role": "chat", "name": "claude"}]
    assert model == "sonnet"


def test_should_use_an_injected_driver_without_reading_settings(routing_store):
    injected = _Driver()
    injected.model = "injected-model"

    driver, model = ChatService(driver=injected)._resolve_driver()

    assert driver is injected
    assert model == "injected-model"
    assert routing_store["reads"] == []


# --- layer_routing_for_request: the request-bound scope the chat turn reads ---

REPO_URL = "https://github.com/acme/app-a.git"


def test_should_return_the_bound_namespace_routing_without_a_repo(routing_store):
    from pipeline.role_routing_scope import layer_routing_for_request

    with bind_namespace(TEAM_NAMESPACE):
        routing = layer_routing_for_request()

    assert routing == ROUTING
    assert routing_store["reads"][-1]["namespace"] == TEAM_NAMESPACE


def test_should_return_the_routing_for_the_given_repository(routing_store):
    from pipeline.role_routing_scope import layer_routing_for_request

    with bind_namespace(TEAM_NAMESPACE):
        routing = layer_routing_for_request(REPO_URL)

    assert routing == ROUTING
    assert routing_store["reads"][-1]["namespace"] == TEAM_NAMESPACE


def test_should_return_empty_for_the_safe_default_source(routing_store):
    from pipeline.role_routing_scope import layer_routing_for_request

    routing_store["source"] = "safe_default"

    assert layer_routing_for_request(REPO_URL) == {}


def test_should_return_empty_when_the_settings_read_raises(routing_store, caplog):
    from pipeline.role_routing_scope import layer_routing_for_request

    routing_store["exc"] = RuntimeError("store unavailable")

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        assert layer_routing_for_request(REPO_URL) == {}


def test_should_resolve_the_turn_through_its_repository_scope(routing_store, backend_calls):
    with bind_namespace(TEAM_NAMESPACE):
        _driver, model = ChatService()._resolve_driver(REPO_URL)

    assert backend_calls == [{"role": "chat", "name": "ollama"}]
    assert model == OLLAMA_TAG
    assert routing_store["reads"][-1]["repo"] == "github.com/acme/app-a"
