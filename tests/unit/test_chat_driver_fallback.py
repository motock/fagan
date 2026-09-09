"""ChatService._resolve_driver must degrade to a working default when the
role registry has no 'chat' entry (or registry loading yields no model).

The PP-01 provider-neutral model_registry.json reset (PR #618) emptied
`roles: {}`. Pre-reset, roles.chat -> ollama/glm carried the chat loop; after
the reset, resolve_role('chat') is called with NO model_fallback, so an
unconfigured machine raises RoleRegistryError ("no model configured") and
/api/chat 500s (observed live 2026-09-08). The chat loop must instead fall
back the same way _run_decompose does: provider claude + the pipeline's
DEFAULT_MODEL tier. A non-claude operator still overrides the provider via
PIPELINE_BACKEND_CHAT (the same env chain resolve_role already applies).

Registry contents are stubbed, never asserted against today's file (see the
project rule: resolve_role tests stub load_registry).
"""

from __future__ import annotations

import pytest

from app import backend, role_registry
from app.chat import ChatService

_EMPTY_REGISTRY = {"roles": {}, "providers": {}}


def test_resolve_driver_falls_back_when_registry_has_no_chat_role(monkeypatch):
    monkeypatch.setattr(role_registry, "load_registry", lambda: _EMPTY_REGISTRY)
    # conftest clears PIPELINE_* env at import, so DEFAULT_MODEL resolves to
    # its "sonnet" constant here regardless of the operator's env.
    monkeypatch.delenv("PIPELINE_BACKEND_CHAT", raising=False)
    fake_driver = object()
    seen: dict = {}

    def fake_get_backend(role, *, name):
        seen["role"], seen["name"] = role, name
        return fake_driver

    monkeypatch.setattr(backend, "get_backend", fake_get_backend)
    svc = ChatService()
    driver, model = svc._resolve_driver()
    assert driver is fake_driver
    assert seen == {"role": "chat", "name": "claude"}
    assert model == "sonnet"


def test_resolve_driver_honors_env_provider_override(monkeypatch):
    monkeypatch.setattr(role_registry, "load_registry", lambda: _EMPTY_REGISTRY)
    monkeypatch.setenv("PIPELINE_BACKEND_CHAT", "ollama")
    fake_driver = object()
    seen: dict = {}

    def fake_get_backend(role, *, name):
        seen["role"], seen["name"] = role, name
        return fake_driver

    monkeypatch.setattr(backend, "get_backend", fake_get_backend)
    svc = ChatService()
    driver, _model = svc._resolve_driver()
    assert driver is fake_driver
    assert seen == {"role": "chat", "name": "ollama"}


def test_resolve_driver_failure_before_fix_would_raise():
    # Documents the pre-fix behavior this regression guards against: with the
    # stubbed empty registry and no model_fallback, resolve_role raised
    # RoleRegistryError instead of degrading. The fallback test above is the
    # real assertion; this guard exists so a future refactor that drops the
    # fallback kwarg fails loudly here too.
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(role_registry, "load_registry", lambda: _EMPTY_REGISTRY)
        monkeypatch.delenv("PIPELINE_BACKEND_CHAT", raising=False)
        svc = ChatService()
        svc._driver = None
        svc._resolved_driver = None
        # Must NOT raise; must resolve to a (driver, model) pair.
        driver, model = svc._resolve_driver()
        assert driver is not None
        assert isinstance(model, str) and model
    finally:
        monkeypatch.undo()
