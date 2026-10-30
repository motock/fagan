"""SA-10: OLLAMA_API_KEY must reach Ollama as a bearer token.

https://ollama.com (no local daemon in the container) authenticates with
`Authorization: Bearer <OLLAMA_API_KEY>`, while a local
http://host.docker.internal:11434 endpoint needs no key. The header is
therefore only present on the wire while the variable is set, and is never
logged. The tests below pin both directions: header present when set, and
*no* `headers` keyword at all (not `headers=None`, not `headers={}`) when
unset - fixed-signature httpx fakes elsewhere in the suite reject an
unexpected keyword.
"""
import json

import httpx

from app import inference_providers
from tests.unit._local_agent_test_helpers import la


class _FakeResponse:
    """Enough of httpx.Response for the code paths under test."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://localhost"),
                response=self,
            )

    def json(self):
        return self._payload


class _FakeStreamCM:
    """Context-manager stand-in for httpx.stream()'s return value."""

    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *exc_info):
        return False


class _FakeStreamResponse:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://localhost"),
                response=self,
            )

    def iter_lines(self):
        return iter(self._lines)


# ---------- helper: env-var -> kwargs ----------


def test_auth_kwargs_empty_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert inference_providers.ollama_auth_kwargs() == {}


def test_auth_kwargs_empty_for_blank_and_whitespace(monkeypatch):
    for blank in ("", "   ", "\t\n"):
        monkeypatch.setenv("OLLAMA_API_KEY", blank)
        assert inference_providers.ollama_auth_kwargs() == {}, repr(blank)


def test_auth_kwargs_strips_key_and_drops_header_after_unset(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", " k ")
    assert inference_providers.ollama_auth_kwargs() == {
        "headers": {"Authorization": "Bearer k"}
    }
    # Computed at call time, never cached: removing the key must remove the
    # header from the very next request (no stale bearer token on the wire).
    monkeypatch.delenv("OLLAMA_API_KEY")
    assert inference_providers.ollama_auth_kwargs() == {}


# ---------- OllamaProvider: chat / loaded_models / reachable ----------


def _chat(provider_kwargs=None):
    provider = inference_providers.OllamaProvider()
    return provider.chat(
        [{"role": "user", "content": "hi"}],
        model="m",
        num_ctx=1024,
        temperature=0.0,
        **(provider_kwargs or {}),
    )


def test_ollama_chat_sends_bearer_header_when_key_set(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    captured = []

    def fake_post(url, **kwargs):
        captured.append((url, kwargs))
        return _FakeResponse({"message": {"role": "assistant", "content": "ok"}})

    monkeypatch.setattr(inference_providers.httpx, "post", fake_post)

    _chat()

    url, kwargs = captured[-1]
    assert url.endswith("/api/chat")
    assert kwargs["headers"] == {"Authorization": "Bearer k"}


def test_ollama_chat_omits_headers_kwarg_when_key_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    captured = []

    def fake_post(url, **kwargs):
        captured.append(kwargs)
        return _FakeResponse({"message": {"role": "assistant", "content": "ok"}})

    monkeypatch.setattr(inference_providers.httpx, "post", fake_post)

    _chat()

    # Not headers=None and not headers={}: the keyword must be absent so
    # fixed-signature fakes in the rest of the suite keep working.
    assert "headers" not in captured[-1]


def test_ollama_chat_drops_header_on_second_call_after_unset(monkeypatch):
    captured = []

    def fake_post(url, **kwargs):
        captured.append(kwargs)
        return _FakeResponse({"message": {"role": "assistant", "content": "ok"}})

    monkeypatch.setattr(inference_providers.httpx, "post", fake_post)

    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    _chat()
    monkeypatch.delenv("OLLAMA_API_KEY")
    _chat()

    assert captured[0]["headers"] == {"Authorization": "Bearer k"}
    assert "headers" not in captured[1]


def test_ollama_loaded_models_sends_bearer_header_when_key_set(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    captured = []

    def fake_get(url, **kwargs):
        captured.append((url, kwargs))
        return _FakeResponse({"models": []})

    monkeypatch.setattr(inference_providers.httpx, "get", fake_get)

    assert inference_providers.OllamaProvider().loaded_models("http://o") == set()

    url, kwargs = captured[-1]
    assert url.endswith("/api/ps")
    assert kwargs["headers"] == {"Authorization": "Bearer k"}


def test_ollama_loaded_models_omits_headers_kwarg_when_key_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    captured = []

    def fake_get(url, **kwargs):
        captured.append(kwargs)
        return _FakeResponse({"models": []})

    monkeypatch.setattr(inference_providers.httpx, "get", fake_get)

    inference_providers.OllamaProvider().loaded_models("http://o")

    assert "headers" not in captured[-1]


def test_ollama_reachable_sends_bearer_header_when_key_set(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    captured = []

    def fake_get(url, **kwargs):
        captured.append((url, kwargs))
        return _FakeResponse({})

    monkeypatch.setattr(inference_providers.httpx, "get", fake_get)

    ok, reason = inference_providers.OllamaProvider().reachable("http://o")

    assert (ok, reason) == (True, "")
    url, kwargs = captured[-1]
    assert url.endswith("/api/tags")
    assert kwargs["headers"] == {"Authorization": "Bearer k"}


def test_ollama_reachable_omits_headers_kwarg_when_key_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    captured = []

    def fake_get(url, **kwargs):
        captured.append(kwargs)
        return _FakeResponse({})

    monkeypatch.setattr(inference_providers.httpx, "get", fake_get)

    inference_providers.OllamaProvider().reachable("http://o")

    assert "headers" not in captured[-1]


# ---------- streaming path in scripts/local_agent_chat.py ----------


def test_local_agent_stream_sends_bearer_header_when_key_set(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    captured = []
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "ok"}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def fake_stream(method, url, **kwargs):
        captured.append((method, url, kwargs))
        return _FakeStreamCM(_FakeStreamResponse(lines))

    monkeypatch.setattr(la.httpx, "stream", fake_stream)

    msg = la._stream_one_turn(
        {"model": "m", "messages": [], "tools": [], "stream": True}
    )

    assert msg["content"] == "ok"
    method, url, kwargs = captured[-1]
    assert method == "POST"
    assert url.endswith("/api/chat")
    assert kwargs["headers"] == {"Authorization": "Bearer k"}


def test_local_agent_stream_omits_headers_kwarg_when_key_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    captured = []
    lines = [json.dumps({"message": {"role": "assistant", "content": "ok"}, "done": True})]

    def fake_stream(method, url, **kwargs):
        captured.append(kwargs)
        return _FakeStreamCM(_FakeStreamResponse(lines))

    monkeypatch.setattr(la.httpx, "stream", fake_stream)

    la._stream_one_turn({"model": "m", "messages": [], "tools": [], "stream": True})

    assert "headers" not in captured[-1]
