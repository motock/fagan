"""Tests for LiteLLMProvider (app/inference_providers.py).

LiteLLM's Python SDK returns the same OpenAI-shaped response that
LMStudioProvider/MLXProvider already translate, so LiteLLMProvider must do
the same translation into Ollama's native envelope
``{"message": ..., "prompt_eval_count": ..., "eval_count": ...}``.

litellm is an OPTIONAL dependency: it is imported lazily inside the method
bodies and never at module top level, so app.inference_providers must keep
importing cleanly on a machine with no litellm installed.

CUMULATIVE ARTIFACT RULE: _PROVIDERS is a shared, cumulative registry that
later stories extend. Every assertion below checks ONLY membership/behavior
of the 'litellm' entry THIS story adds — never the registry's total
contents, its exact key set, its length, or the presence/absence of
'ollama'/'lmstudio'/'mlx'.
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

from app import inference_providers
from app.inference_providers import get_local_provider

# ---------------------------------------------------------------------------
# Fakes: a litellm module double injected into sys.modules so the lazy
# `import litellm` inside chat()/reachable() binds the fake.
# ---------------------------------------------------------------------------


class _FakeRateLimitError(Exception):
    """Stands in for litellm.exceptions.RateLimitError."""


class _FakeMessage:
    """Pydantic-ish message: exposes .model_dump(), is NOT subscriptable."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump(self) -> dict:
        return dict(self._payload)


class _FakeUsage:
    def __init__(self, prompt_tokens, completion_tokens) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeChoice:
    def __init__(self, message) -> None:
        self.message = message


class _FakeResponse:
    """OpenAI-shaped response object (attribute access only, never a dict)."""

    def __init__(self) -> None:
        self.choices = [_FakeChoice(_FakeMessage({"role": "assistant", "content": "hi"}))]
        self.usage = _FakeUsage(prompt_tokens=7, completion_tokens=3)


def _make_fake_litellm():
    fake = types.ModuleType("litellm")
    fake.calls: list[dict] = []
    fake.error = None  # when set, completion() raises it
    fake.response = _FakeResponse()

    def _completion(**kwargs):
        fake.calls.append(kwargs)
        if fake.error is not None:
            raise fake.error
        return fake.response

    fake.completion = _completion
    exceptions = types.ModuleType("litellm.exceptions")
    exceptions.RateLimitError = _FakeRateLimitError
    fake.exceptions = exceptions
    return fake


_MESSAGES = [{"role": "user", "content": "hi"}]


def _chat(provider, **overrides):
    kwargs = {"model": "mymodel", "num_ctx": 512, "temperature": 0.2}
    kwargs.update(overrides)
    return provider.chat(_MESSAGES, **kwargs)


# ---------------------------------------------------------------------------
# 1. Registry (cumulative-artifact rule: only the 'litellm' entry)
# ---------------------------------------------------------------------------


def test_litellm_is_registered_and_get_local_provider_returns_it():
    assert "litellm" in inference_providers._PROVIDERS
    provider = get_local_provider("litellm")
    assert isinstance(provider, inference_providers.LiteLLMProvider)
    assert provider.name == "litellm"
    # The SDK talks straight to the upstream vendor: no local server to point
    # at, so the default endpoint is the empty string.
    assert provider.default_endpoint == ""


# ---------------------------------------------------------------------------
# 2. Translation: OpenAI shape -> Ollama envelope
# ---------------------------------------------------------------------------


def test_chat_translates_openai_response_into_ollama_envelope(monkeypatch):
    fake = _make_fake_litellm()
    monkeypatch.setitem(sys.modules, "litellm", fake)
    provider = inference_providers.LiteLLMProvider()

    result = _chat(provider)

    # Exactly the Ollama envelope keys — nothing more, nothing less.
    assert set(result) == {"message", "prompt_eval_count", "eval_count"}
    assert result == {
        "message": {"role": "assistant", "content": "hi"},
        "prompt_eval_count": 7,
        "eval_count": 3,
    }
    # The request went through with the caller's values intact.
    assert fake.calls[0]["model"] == "mymodel"
    assert fake.calls[0]["messages"] == _MESSAGES
    assert fake.calls[0]["max_tokens"] == 512
    assert fake.calls[0]["temperature"] == 0.2

    # Missing usage counts collapse to 0 (the `or 0` branch), never None.
    fake.response.usage.prompt_tokens = None
    fake.response.usage.completion_tokens = None
    result2 = _chat(provider)
    assert result2["prompt_eval_count"] == 0
    assert result2["eval_count"] == 0
    assert result2["message"] == {"role": "assistant", "content": "hi"}


# ---------------------------------------------------------------------------
# 3. Timeout: resolved per call, never None, never unbounded
# ---------------------------------------------------------------------------


def test_chat_resolves_wire_timeout_per_call(monkeypatch):
    fake = _make_fake_litellm()
    monkeypatch.setitem(sys.modules, "litellm", fake)
    monkeypatch.delenv("PIPELINE_ROLE_CALL_TIMEOUT_SECONDS", raising=False)
    provider = inference_providers.LiteLLMProvider()

    # Call 1: env unset -> the role-call default of 600.0 seconds.
    _chat(provider)
    assert fake.calls[0]["timeout"] == 600.0
    assert fake.calls[0]["timeout"] is not None

    # Call 2, SAME provider instance: the env changed, so the timeout must be
    # re-resolved per call (not cached at __init__/import time).
    monkeypatch.setenv("PIPELINE_ROLE_CALL_TIMEOUT_SECONDS", "12.5")
    _chat(provider)
    assert fake.calls[1]["timeout"] == 12.5
    assert fake.calls[1]["timeout"] is not None


# ---------------------------------------------------------------------------
# 4. Conditional kwargs: absent vs explicitly-None
# ---------------------------------------------------------------------------


def test_chat_omits_tools_and_api_base_when_not_provided(monkeypatch):
    fake = _make_fake_litellm()
    monkeypatch.setitem(sys.modules, "litellm", fake)
    provider = inference_providers.LiteLLMProvider()

    # tools=None / endpoint=None must be ABSENT from the wire call — explicit
    # None is not the same as absent. (think=True also proves the Protocol
    # parity parameter is accepted and deliberately unused.)
    _chat(provider, tools=None, endpoint=None, think=True)
    assert "tools" not in fake.calls[0]
    assert "api_base" not in fake.calls[0]

    # When provided, both must be forwarded...
    _chat(
        provider,
        tools=[{"type": "function", "function": {"name": "x"}}],
        endpoint="http://127.0.0.1:11434",
    )
    assert fake.calls[1]["tools"] == [{"type": "function", "function": {"name": "x"}}]
    assert fake.calls[1]["api_base"] == "http://127.0.0.1:11434"
    # ...and the first record is still clean.
    assert "tools" not in fake.calls[0]
    assert "api_base" not in fake.calls[0]


# ---------------------------------------------------------------------------
# 5. Rate limiting: litellm's 429 -> inference_providers.RateLimitedError
# ---------------------------------------------------------------------------


def test_chat_translates_rate_limit_error_and_recovers(monkeypatch):
    fake = _make_fake_litellm()
    monkeypatch.setitem(sys.modules, "litellm", fake)
    provider = inference_providers.LiteLLMProvider()

    fake.error = _FakeRateLimitError("429 too many requests")
    with pytest.raises(inference_providers.RateLimitedError) as excinfo:
        _chat(provider)
    # The orchestrator routes on the message: it must name the model.
    assert "mymodel" in str(excinfo.value)

    # Same provider, fake now succeeds: the failed call left no poisoned state.
    fake.error = None
    result = _chat(provider)
    assert set(result) == {"message", "prompt_eval_count", "eval_count"}
    assert result["message"] == {"role": "assistant", "content": "hi"}


# ---------------------------------------------------------------------------
# 6. reachable(): fail closed, never raise
# ---------------------------------------------------------------------------


def test_reachable_fails_closed_when_litellm_missing(monkeypatch):
    fake = _make_fake_litellm()
    monkeypatch.setitem(sys.modules, "litellm", fake)  # teardown restores this
    provider = inference_providers.LiteLLMProvider()

    # sys.modules["litellm"] = None makes `import litellm` raise ImportError.
    monkeypatch.setitem(sys.modules, "litellm", None)
    ok, reason = provider.reachable("http://x")
    assert ok is False
    assert "litellm" in reason  # names the package (and how to install it)

    # Once litellm is importable again, reachable flips to healthy.
    monkeypatch.setitem(sys.modules, "litellm", fake)
    assert provider.reachable("http://x") == (True, "")


# ---------------------------------------------------------------------------
# 7. loaded_models(): LiteLLM has no loaded-model concept
# ---------------------------------------------------------------------------


def test_loaded_models_is_always_empty(monkeypatch):
    # Force litellm "absent": loaded_models must not depend on it (or raise).
    monkeypatch.setitem(sys.modules, "litellm", None)
    provider = inference_providers.LiteLLMProvider()
    assert provider.loaded_models("http://x") == set()


# ---------------------------------------------------------------------------
# 8. Import hygiene: no TOP-LEVEL litellm import (the lazy one is required)
# ---------------------------------------------------------------------------


def test_module_has_no_top_level_litellm_import():
    source = Path(inference_providers.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Walk ONLY module-level nodes: the lazy `import litellm` inside chat()
    # legitimately appears in the source and must NOT trip this assertion.
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] != "litellm", (
                    f"app/inference_providers.py must not import litellm at "
                    f"module top level (got {alias.name!r}); import it lazily "
                    f"inside the method body"
                )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert not (root == "litellm" and node.level == 0), (
                f"app/inference_providers.py must not import litellm at module "
                f"top level (got 'from {node.module or ''} import ...'); import "
                f"it lazily inside the method body"
            )