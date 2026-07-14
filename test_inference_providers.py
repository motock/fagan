"""Tests for the local inference server wire-protocol abstraction.

httpx is a shared singleton module, so mocking httpx.post/get here exercises
exactly the same interception point backend.py's own tests use for
OllamaDriver - no separate mock wiring is needed for the two modules to
share fakes.
"""
import pytest

import inference_providers as ip


# ---------- get_local_provider() selection ----------
def test_get_local_provider_defaults_to_ollama(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_PROVIDER", raising=False)
    provider = ip.get_local_provider()
    assert isinstance(provider, ip.OllamaProvider)
    assert provider.name == "ollama"


def test_get_local_provider_selects_lmstudio(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "lmstudio")
    provider = ip.get_local_provider()
    assert isinstance(provider, ip.LMStudioProvider)


def test_get_local_provider_selects_mlx(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "mlx")
    provider = ip.get_local_provider()
    assert isinstance(provider, ip.MLXProvider)


def test_get_local_provider_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="bogus"):
        ip.get_local_provider()


def test_get_local_provider_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "OLLAMA")
    assert isinstance(ip.get_local_provider(), ip.OllamaProvider)


# ---------- T16: get_local_provider(name=) explicit override ----------
def test_get_local_provider_explicit_name_overrides_env(monkeypatch):
    # An explicit name bypasses PIPELINE_LOCAL_PROVIDER entirely - lets a
    # caller pin a specific provider (e.g. per-role backend selection)
    # regardless of the process-wide env default.
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "ollama")
    provider = ip.get_local_provider("lmstudio")
    assert isinstance(provider, ip.LMStudioProvider)


def test_get_local_provider_explicit_name_works_with_no_env_set(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_PROVIDER", raising=False)
    assert isinstance(ip.get_local_provider("mlx"), ip.MLXProvider)


def test_get_local_provider_explicit_unknown_name_raises(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "ollama")
    with pytest.raises(ValueError, match="bogus"):
        ip.get_local_provider("bogus")


def test_get_local_provider_none_still_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "mlx")
    assert isinstance(ip.get_local_provider(None), ip.MLXProvider)


# ---------- OllamaProvider.chat() ----------
def test_ollama_provider_chat_posts_native_api_chat_with_options(monkeypatch):
    calls = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "hi"}, "eval_count": 5}

    def _fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return _Resp()

    monkeypatch.setattr(ip.httpx, "post", _fake_post)
    provider = ip.OllamaProvider()
    result = provider.chat(
        [{"role": "user", "content": "hi"}], model="devstral:24b",
        num_ctx=16384, temperature=0.3, endpoint="http://localhost:11434",
        timeout=600,
    )
    assert result == {"message": {"content": "hi"}, "eval_count": 5}
    url, body, timeout = calls[0]
    assert url == "http://localhost:11434/api/chat"
    assert body["options"] == {"num_ctx": 16384, "temperature": 0.3}
    assert body["stream"] is False
    assert timeout == 600


def test_ollama_provider_chat_includes_tools_when_given(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": ""}}

    def _fake_post(url, json, timeout):
        captured.update(json)
        return _Resp()

    monkeypatch.setattr(ip.httpx, "post", _fake_post)
    tools = [{"type": "function", "function": {"name": "bash"}}]
    ip.OllamaProvider().chat(
        [], model="m", num_ctx=1, temperature=0, tools=tools,
        endpoint="http://x", timeout=10,
    )
    assert captured["tools"] == tools


def test_ollama_provider_chat_raises_rate_limited_on_429(monkeypatch):
    class _Resp:
        status_code = 429

        def raise_for_status(self):
            raise AssertionError("must not reach raise_for_status on 429")

    monkeypatch.setattr(ip.httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(ip.RateLimitedError, match="429"):
        ip.OllamaProvider().chat(
            [], model="m", num_ctx=1, temperature=0,
            endpoint="http://x", timeout=10,
        )


def test_ollama_provider_chat_propagates_http_error_for_non_429(monkeypatch):
    import httpx as real_httpx

    class _Resp:
        status_code = 500

        def raise_for_status(self):
            raise real_httpx.HTTPStatusError("500", request=None, response=None)

    monkeypatch.setattr(ip.httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(real_httpx.HTTPStatusError):
        ip.OllamaProvider().chat(
            [], model="m", num_ctx=1, temperature=0,
            endpoint="http://x", timeout=10,
        )


# ---------- OllamaProvider.loaded_models() ----------
def test_ollama_provider_loaded_models_parses_api_ps(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "devstral:24b"}, {"model": "minimax-m3:cloud"}]}

    monkeypatch.setattr(ip.httpx, "get", lambda url, timeout: _Resp())
    loaded = ip.OllamaProvider().loaded_models("http://localhost:11434")
    assert loaded == {"devstral:24b", "minimax-m3:cloud"}


def test_ollama_provider_loaded_models_returns_empty_set_on_error(monkeypatch):
    def _boom(url, timeout):
        raise ip.httpx.ConnectError("refused")

    monkeypatch.setattr(ip.httpx, "get", _boom)
    assert ip.OllamaProvider().loaded_models("http://x") == set()


# ---------- OllamaProvider.reachable() ----------
def test_ollama_provider_reachable_ok(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(ip.httpx, "get", lambda url, timeout: _Resp())
    ok, reason = ip.OllamaProvider().reachable("http://localhost:11434")
    assert ok is True
    assert reason == ""


def test_ollama_provider_reachable_not_ok_on_error(monkeypatch):
    def _boom(url, timeout):
        raise ip.httpx.ConnectError("refused")

    monkeypatch.setattr(ip.httpx, "get", _boom)
    ok, reason = ip.OllamaProvider().reachable("http://x")
    assert ok is False
    assert "unreachable" in reason


def test_lmstudio_provider_default_endpoint():
    assert ip.LMStudioProvider().default_endpoint == "http://localhost:1234"


# ---------- LMStudioProvider.chat() ----------
# Request/response shapes below are captured live against LM Studio's `lms`
# server (2026-07-10) serving google/gemma-4-e4b, not guessed from the OpenAI
# spec alone - see MODEL_PROVIDER_ABSTRACTION_PLAN.md for the session.
def test_lmstudio_provider_chat_posts_v1_chat_completions_with_max_tokens_for_num_ctx(
    monkeypatch,
):
    calls = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{"message": {
                    "role": "assistant", "content": "Hello, how are you?",
                    "reasoning_content": "", "tool_calls": [],
                }}],
                "usage": {"prompt_tokens": 24, "completion_tokens": 7, "total_tokens": 31},
            }

    def _fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return _Resp()

    monkeypatch.setattr(ip.httpx, "post", _fake_post)
    result = ip.LMStudioProvider().chat(
        [{"role": "user", "content": "hi"}], model="google/gemma-4-e4b",
        num_ctx=16384, temperature=0.3, endpoint="http://localhost:1234", timeout=600,
    )
    url, body, timeout = calls[0]
    assert url == "http://localhost:1234/v1/chat/completions"
    assert body["temperature"] == 0.3
    # LM Studio has no per-request context-window control like Ollama's
    # options.num_ctx (context length is fixed by the loaded model) - num_ctx
    # is reused as the max_tokens (output-length) budget instead.
    assert body["max_tokens"] == 16384
    assert body["stream"] is False
    assert timeout == 600
    # Normalized into Ollama's envelope shape so OllamaDriver's existing
    # complete()/_review_loop code works unchanged regardless of provider.
    # Extra fields on the message (reasoning_content) pass through untouched -
    # backend.py's harness doesn't read them, but doesn't need them stripped.
    assert result == {
        "message": {
            "role": "assistant", "content": "Hello, how are you?",
            "reasoning_content": "", "tool_calls": [],
        },
        "prompt_eval_count": 24,
        "eval_count": 7,
    }


def test_lmstudio_provider_chat_includes_tools_when_given(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": ""}}],
                     "usage": {}}

    def _fake_post(url, json, timeout):
        captured.update(json)
        return _Resp()

    monkeypatch.setattr(ip.httpx, "post", _fake_post)
    tools = [{"type": "function", "function": {"name": "get_weather"}}]
    ip.LMStudioProvider().chat(
        [], model="m", num_ctx=1, temperature=0, tools=tools,
        endpoint="http://x", timeout=10,
    )
    assert captured["tools"] == tools


def test_lmstudio_provider_chat_normalizes_tool_call_response(monkeypatch):
    # Live-captured shape: tool_calls[].function.arguments is a JSON-encoded
    # STRING (same as MLX/OpenAI) and - unlike MLX's server, which always
    # returns null - tool_calls[].id is a real non-null string here. Neither
    # detail matters to backend.py's existing tool-calling loop (it only
    # reads .function.name/.arguments), so this just confirms the shape
    # passes through untouched.
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{"message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [{
                        "type": "function", "id": "878309038",
                        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                    }],
                }}],
                "usage": {"prompt_tokens": 72, "completion_tokens": 191, "total_tokens": 263},
            }

    monkeypatch.setattr(ip.httpx, "post", lambda *a, **k: _Resp())
    result = ip.LMStudioProvider().chat(
        [], model="m", num_ctx=1, temperature=0, endpoint="http://x", timeout=10,
    )
    tc = result["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "get_weather"
    assert tc["function"]["arguments"] == '{"city":"Paris"}'


def test_lmstudio_provider_chat_raises_rate_limited_on_429(monkeypatch):
    class _Resp:
        status_code = 429

        def raise_for_status(self):
            raise AssertionError("must not reach raise_for_status on 429")

    monkeypatch.setattr(ip.httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(ip.RateLimitedError, match="429"):
        ip.LMStudioProvider().chat(
            [], model="m", num_ctx=1, temperature=0, endpoint="http://x", timeout=10,
        )


def test_lmstudio_provider_chat_propagates_http_error_for_non_429(monkeypatch):
    import httpx as real_httpx

    class _Resp:
        status_code = 500

        def raise_for_status(self):
            raise real_httpx.HTTPStatusError("500", request=None, response=None)

    monkeypatch.setattr(ip.httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(real_httpx.HTTPStatusError):
        ip.LMStudioProvider().chat(
            [], model="m", num_ctx=1, temperature=0, endpoint="http://x", timeout=10,
        )


# ---------- LMStudioProvider.loaded_models() ----------
def test_lmstudio_provider_loaded_models_filters_by_state(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [
                {"id": "google/gemma-4-e4b", "state": "loaded"},
                {"id": "text-embedding-nomic-embed-text-v1.5", "state": "not-loaded"},
            ]}

    monkeypatch.setattr(ip.httpx, "get", lambda url, timeout: _Resp())
    loaded = ip.LMStudioProvider().loaded_models("http://localhost:1234")
    assert loaded == {"google/gemma-4-e4b"}


def test_lmstudio_provider_loaded_models_returns_empty_set_on_error(monkeypatch):
    def _boom(url, timeout):
        raise ip.httpx.ConnectError("refused")

    monkeypatch.setattr(ip.httpx, "get", _boom)
    assert ip.LMStudioProvider().loaded_models("http://x") == set()


# ---------- LMStudioProvider.reachable() ----------
def test_lmstudio_provider_reachable_probes_v1_models(monkeypatch):
    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

    def _fake_get(url, timeout):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(ip.httpx, "get", _fake_get)
    ok, reason = ip.LMStudioProvider().reachable("http://localhost:1234")
    assert ok is True
    assert reason == ""
    assert calls == ["http://localhost:1234/v1/models"]


def test_lmstudio_provider_reachable_not_ok_on_error(monkeypatch):
    def _boom(url, timeout):
        raise ip.httpx.ConnectError("refused")

    monkeypatch.setattr(ip.httpx, "get", _boom)
    ok, reason = ip.LMStudioProvider().reachable("http://x")
    assert ok is False
    assert "unreachable" in reason


def test_mlx_provider_default_endpoint():
    assert ip.MLXProvider().default_endpoint == "http://localhost:8080"


def test_mlx_provider_loaded_models_returns_empty_set_no_swap_concept():
    # mlx_lm.server serves one model per process - there is no VRAM-swap
    # concept to warn about, so this must return an empty set without
    # making any network call at all.
    assert ip.MLXProvider().loaded_models("http://localhost:8080") == set()


# ---------- MLXProvider.chat() ----------
# Request/response shapes below are captured live against mlx_lm 0.28.3 +
# mlx-community/Qwen2.5-1.5B-Instruct-4bit (2026-07-09), not guessed from the
# OpenAI spec alone - see MODEL_PROVIDER_ABSTRACTION_PLAN.md for the session.
def test_mlx_provider_chat_posts_v1_chat_completions_with_max_tokens_for_num_ctx(
    monkeypatch,
):
    calls = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{"message": {
                    "role": "assistant", "content": "Hello there!", "tool_calls": [],
                }}],
                "usage": {"prompt_tokens": 37, "completion_tokens": 4, "total_tokens": 41},
            }

    def _fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return _Resp()

    monkeypatch.setattr(ip.httpx, "post", _fake_post)
    result = ip.MLXProvider().chat(
        [{"role": "user", "content": "hi"}], model="mlx-community/Qwen2.5-1.5B-Instruct-4bit",
        num_ctx=16384, temperature=0.3, endpoint="http://localhost:8080", timeout=600,
    )
    url, body, timeout = calls[0]
    assert url == "http://localhost:8080/v1/chat/completions"
    assert body["temperature"] == 0.3
    # MLX has no per-request context-window control like Ollama's
    # options.num_ctx (context is fixed at model-load time) - num_ctx is
    # reused as the max_tokens (output-length) budget instead of the
    # server's tiny 512-token default.
    assert body["max_tokens"] == 16384
    assert body["stream"] is False
    assert timeout == 600
    # Normalized into Ollama's envelope shape so OllamaDriver's existing
    # complete()/_review_loop code works unchanged regardless of provider.
    assert result == {
        "message": {"role": "assistant", "content": "Hello there!", "tool_calls": []},
        "prompt_eval_count": 37,
        "eval_count": 4,
    }


def test_mlx_provider_chat_includes_tools_when_given(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": ""}}],
                     "usage": {}}

    def _fake_post(url, json, timeout):
        captured.update(json)
        return _Resp()

    monkeypatch.setattr(ip.httpx, "post", _fake_post)
    tools = [{"type": "function", "function": {"name": "get_weather"}}]
    ip.MLXProvider().chat(
        [], model="m", num_ctx=1, temperature=0, tools=tools,
        endpoint="http://x", timeout=10,
    )
    assert captured["tools"] == tools


def test_mlx_provider_chat_omits_model_field(monkeypatch):
    """mlx_lm.server serves exactly one model per process (loaded_models()
    already reflects this - no VRAM-swap concept). Its ModelProvider.load()
    only reuses the preloaded weights when the request's "model" field maps
    (via an internal alias table) to the exact string mlx_lm.server was
    launched with; any other value - including the model's own metadata id,
    the same string /v1/models reports - makes it attempt a fresh model
    resolution instead, which can hang indefinitely (observed live: two
    separate "server never responds" incidents were actually this mismatch,
    not a broken download). Since there is only ever one model to select,
    the safest fix is to never send "model" at all - the server always
    falls back to its preloaded default in that case."""
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": ""}}],
                     "usage": {}}

    def _fake_post(url, json, timeout):
        captured.update(json)
        return _Resp()

    monkeypatch.setattr(ip.httpx, "post", _fake_post)
    ip.MLXProvider().chat(
        [{"role": "user", "content": "hi"}],
        model="mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit",
        num_ctx=16384, temperature=0.3, endpoint="http://localhost:8080", timeout=600,
    )
    assert "model" not in captured


def test_mlx_provider_chat_normalizes_tool_call_response(monkeypatch):
    # Live-captured shape: tool_calls[].function.arguments is a JSON-encoded
    # STRING (not a dict) and tool_calls[].id is always null on this server
    # version - backend.py's existing tool-calling loop already handles a
    # string arguments field (json.loads it), so no extra parsing is needed
    # here; this just confirms the shape passes through untouched.
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{"message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [{
                        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                        "type": "function", "id": None,
                    }],
                }}],
                "usage": {"prompt_tokens": 177, "completion_tokens": 20, "total_tokens": 197},
            }

    monkeypatch.setattr(ip.httpx, "post", lambda *a, **k: _Resp())
    result = ip.MLXProvider().chat(
        [], model="m", num_ctx=1, temperature=0, endpoint="http://x", timeout=10,
    )
    tc = result["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "get_weather"
    assert tc["function"]["arguments"] == '{"city": "Paris"}'


def test_mlx_provider_chat_raises_rate_limited_on_429(monkeypatch):
    class _Resp:
        status_code = 429

        def raise_for_status(self):
            raise AssertionError("must not reach raise_for_status on 429")

    monkeypatch.setattr(ip.httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(ip.RateLimitedError, match="429"):
        ip.MLXProvider().chat(
            [], model="m", num_ctx=1, temperature=0, endpoint="http://x", timeout=10,
        )


def test_mlx_provider_chat_propagates_http_error_for_non_429(monkeypatch):
    import httpx as real_httpx

    class _Resp:
        status_code = 500

        def raise_for_status(self):
            raise real_httpx.HTTPStatusError("500", request=None, response=None)

    monkeypatch.setattr(ip.httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(real_httpx.HTTPStatusError):
        ip.MLXProvider().chat(
            [], model="m", num_ctx=1, temperature=0, endpoint="http://x", timeout=10,
        )


# ---------- MLXProvider.reachable() ----------
def test_mlx_provider_reachable_probes_v1_models(monkeypatch):
    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

    def _fake_get(url, timeout):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(ip.httpx, "get", _fake_get)
    ok, reason = ip.MLXProvider().reachable("http://localhost:8080")
    assert ok is True
    assert reason == ""
    assert calls == ["http://localhost:8080/v1/models"]


def test_mlx_provider_reachable_not_ok_on_error(monkeypatch):
    def _boom(url, timeout):
        raise ip.httpx.ConnectError("refused")

    monkeypatch.setattr(ip.httpx, "get", _boom)
    ok, reason = ip.MLXProvider().reachable("http://x")
    assert ok is False
    assert "unreachable" in reason
