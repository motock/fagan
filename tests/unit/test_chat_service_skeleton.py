"""Tests for app/chat.py — the ChatService skeleton (PART 1 of 4).

This story locks in the class shape: constructor, lazy chat-role
resolution, and a single-shot (non-looping, no tool calls) execute_turn.
The follow-on stories add the agent loop, tool registry, and HTTP
endpoint. These tests must pass against an implementation that does not
yet exist, so they import app.chat and assert the documented behavior.
"""
from __future__ import annotations

import httpx
import pytest

import app.chat as chat_module
from app.chat import SYSTEM_PROMPT, ChatService


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeDriver:
    """A minimal stand-in for a backend driver.

    An injected driver in tests carries no real model tag (its `model`
    attribute is whatever the test sets, or absent); tests only check
    that driver.complete was called with the right arguments.
    """

    def __init__(self, reply: str = "ok", model: str | None = None) -> None:
        self.reply = reply
        self.calls: list[dict] = []
        if model is not None:
            self.model = model

    def complete(self, prompt: str, *, system, model, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system, "model": model})
        return self.reply


# --------------------------------------------------------------------------- #
# Constructor
# --------------------------------------------------------------------------- #
class TestConstructor:
    def test_accepts_keyword_driver(self) -> None:
        driver = _FakeDriver()
        svc = ChatService(driver=driver)
        assert svc._driver is driver

    def test_driver_defaults_to_none_and_is_lazy(self, monkeypatch) -> None:
        # Constructing with no driver must NOT touch role_registry/backend.
        def _boom(*args, **kwargs):
            raise AssertionError("resolve_role must not be called in __init__")

        def _boom_backend(*args, **kwargs):
            raise AssertionError("get_backend must not be called in __init__")

        monkeypatch.setattr(
            "app.role_registry.resolve_role", _boom, raising=True
        )
        monkeypatch.setattr("app.backend.get_backend", _boom_backend, raising=True)
        # Must not raise.
        svc = ChatService()
        assert svc._driver is None

    def test_api_base_url_explicit(self) -> None:
        svc = ChatService(api_base_url="http://example.test:9")
        assert svc._api_base_url == "http://example.test:9"

    def test_api_base_url_env_default(self, monkeypatch) -> None:
        monkeypatch.setenv("PIPELINE_CHAT_API_BASE", "http://env.test:7")
        svc = ChatService()
        assert svc._api_base_url == "http://env.test:7"

    def test_api_base_url_builtin_default(self) -> None:
        # No env var set (conftest clears PIPELINE_*).
        svc = ChatService()
        assert svc._api_base_url == "http://127.0.0.1:8000"

    def test_max_turns_explicit(self) -> None:
        svc = ChatService(max_turns=42)
        assert svc._max_turns == 42

    def test_max_turns_env_default(self, monkeypatch) -> None:
        monkeypatch.setenv("PIPELINE_CHAT_MAX_TURNS", "5")
        svc = ChatService()
        assert svc._max_turns == 5

    def test_max_turns_builtin_default(self) -> None:
        svc = ChatService()
        assert svc._max_turns == 10

    def test_http_client_explicit(self) -> None:
        client = httpx.Client()
        svc = ChatService(http_client=client)
        assert svc._http_client is client

    def test_http_client_default_uses_api_base_url(self) -> None:
        svc = ChatService(api_base_url="http://hctest:1234")
        client = svc._http_client
        assert isinstance(client, httpx.Client)
        assert str(client.base_url).rstrip("/") == "http://hctest:1234"


# --------------------------------------------------------------------------- #
# SYSTEM_PROMPT
# --------------------------------------------------------------------------- #
class TestSystemPrompt:
    def test_is_str(self) -> None:
        assert isinstance(SYSTEM_PROMPT, str)

    @pytest.mark.parametrize(
        "needle",
        ["[TOOL_CALL]", "[/TOOL_CALL]", "[TOOL_RESULT"],
    )
    def test_contains_tool_protocol_tags(self, needle: str) -> None:
        assert needle in SYSTEM_PROMPT

    def test_ends_with_exact_sentence(self) -> None:
        assert SYSTEM_PROMPT.endswith(
            "Call tools to gather information, then provide a natural-language reply."
        )

    def test_describes_three_roles(self) -> None:
        # plan authoring, ops control, decisions — all three must appear.
        lowered = SYSTEM_PROMPT.lower()
        assert "plan" in lowered
        assert "ops" in lowered
        assert "decision" in lowered


# --------------------------------------------------------------------------- #
# execute_turn — happy path with injected driver
# --------------------------------------------------------------------------- #
class TestExecuteTurnInjected:
    def test_calls_complete_once_and_returns_shape(self) -> None:
        driver = _FakeDriver(reply="hello back")
        svc = ChatService(driver=driver)
        result = svc.execute_turn("hello")
        assert len(driver.calls) == 1
        call = driver.calls[0]
        assert call["prompt"] == "hello"
        assert call["system"] == SYSTEM_PROMPT
        assert result == {
            "reply": "hello back",
            "tool_calls": [],
            "turns": 1,
        }

    def test_passes_model_tag_from_injected_driver(self) -> None:
        # An injected driver may carry a `model` attribute; it should be
        # forwarded as the model tag (empty string when absent).
        driver = _FakeDriver(reply="r")
        svc = ChatService(driver=driver)
        svc.execute_turn("hi")
        assert driver.calls[0]["model"] == ""

    def test_passes_model_tag_when_driver_has_model_attr(self) -> None:
        driver = _FakeDriver(reply="r", model="some-tag")
        svc = ChatService(driver=driver)
        svc.execute_turn("hi")
        assert driver.calls[0]["model"] == "some-tag"

    def test_no_loop_single_complete_call(self) -> None:
        driver = _FakeDriver(reply="anything")
        svc = ChatService(driver=driver)
        svc.execute_turn("x")
        assert len(driver.calls) == 1  # no looping in this story

    def test_optional_kwargs_accepted(self) -> None:
        # plan_name and history are accepted (unused in this story) and
        # must not raise.
        driver = _FakeDriver(reply="r")
        svc = ChatService(driver=driver)
        result = svc.execute_turn("m", plan_name="p", history=[])
        assert result["reply"] == "r"


# --------------------------------------------------------------------------- #
# execute_turn — lazy role resolution
# --------------------------------------------------------------------------- #
class TestLazyResolution:
    def test_resolves_via_role_registry_and_backend(self, monkeypatch) -> None:
        driver = _FakeDriver(reply="resolved-reply")

        seen: dict = {}

        class _FakeResolution:
            provider = "claude"
            model = "sonnet"

        def fake_resolve_role(role, *, registry=None, **kwargs):
            seen["role"] = role
            seen["registry_arg_passed"] = registry is not None
            return _FakeResolution()

        def fake_get_backend(role, *, name=None, **kwargs):
            seen["backend_role"] = role
            seen["provider"] = name
            return driver

        monkeypatch.setattr(
            "app.role_registry.resolve_role", fake_resolve_role
        )
        monkeypatch.setattr("app.backend.get_backend", fake_get_backend)

        svc = ChatService()
        result = svc.execute_turn("hello")
        assert seen["role"] == "chat"
        assert seen["provider"] == "claude"
        assert seen["registry_arg_passed"] is True
        assert seen["backend_role"] == "chat"
        assert result == {
            "reply": "resolved-reply",
            "tool_calls": [],
            "turns": 1,
        }
        # The resolved model tag is forwarded to complete.
        assert driver.calls[0]["model"] == "sonnet"

    def test_resolution_cached_across_turns(self, monkeypatch) -> None:
        driver = _FakeDriver(reply="r")

        call_count = {"resolve": 0, "backend": 0}

        class _FakeResolution:
            provider = "claude"
            model = "sonnet"

        def fake_resolve_role(role, *, registry=None, **kwargs):
            call_count["resolve"] += 1
            return _FakeResolution()

        def fake_get_backend(role, *, name=None, **kwargs):
            call_count["backend"] += 1
            return driver

        monkeypatch.setattr(
            "app.role_registry.resolve_role", fake_resolve_role
        )
        monkeypatch.setattr("app.backend.get_backend", fake_get_backend)

        svc = ChatService()
        svc.execute_turn("first")
        svc.execute_turn("second")
        assert call_count["resolve"] == 1
        assert call_count["backend"] == 1
        # Both turns used the same cached driver.
        assert [c["prompt"] for c in driver.calls] == ["first", "second"]

    def test_resolves_non_claude_provider_by_name(self, monkeypatch) -> None:
        driver = _FakeDriver(reply="r")
        seen: dict = {}

        class _FakeResolution:
            provider = "ollama"
            model = "gpt-oss-20b-high"

        def fake_resolve_role(role, *, registry=None, **kwargs):
            return _FakeResolution()

        def fake_get_backend(role, *, name=None, **kwargs):
            seen["role"] = role
            seen["name"] = name
            return driver

        monkeypatch.setattr("app.role_registry.resolve_role", fake_resolve_role)
        monkeypatch.setattr("app.backend.get_backend", fake_get_backend)

        svc = ChatService()
        svc.execute_turn("hello")
        assert seen["role"] == "chat"
        assert seen["name"] == "ollama"


# --------------------------------------------------------------------------- #
# Security property: no PipelineService / _service references
# --------------------------------------------------------------------------- #
class TestSecurityProperty:
    def test_module_does_not_import_pipeline_service(self) -> None:
        source = chat_module.__file__
        assert source is not None
        text = open(source).read()  # noqa: SIM115
        assert "PipelineService" not in text
        assert "_service" not in text

    def test_module_docstring_states_no_direct_service_access(self) -> None:
        doc = chat_module.__doc__ or ""
        assert doc.strip() != ""
        lowered = doc.lower()
        # The docstring must state the no-direct-service-access constraint.
        source_text = open(chat_module.__file__).read().lower()  # noqa: SIM115
        assert "pipelineservice" not in source_text
        assert "no direct" in lowered or "must not" in lowered or "never" in lowered