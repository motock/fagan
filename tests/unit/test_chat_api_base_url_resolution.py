"""Unit tests for ChatService's internal api_base_url resolution (app/chat.py).

The bug this pins down: ChatService falls back to a hardcoded
``http://127.0.0.1:8000`` when no explicit ``api_base_url`` is supplied, so
when the dashboard is actually bound to a different host/port (e.g.
``DASHBOARD_PORT=8001``), chat's internal tool calls (``/api/health`` etc.)
hit the wrong port and come back ``{"detail": "unauthorized"}`` from whatever
else happens to own 8000.

The fix under test:

* ``chat_mod._resolve_chat_api_base_url(request)`` -- a pure helper that
  returns the explicit ``PIPELINE_CHAT_API_BASE`` env override when set,
  otherwise derives the base URL from the incoming Starlette ``Request``
  (``request.base_url``, trailing slash stripped), otherwise ``None``.
* ``chat_endpoint`` grows a ``request: Request = None`` parameter (a plain
  Python default -- FastAPI auto-injects the real Request on real HTTP calls,
  while direct plain-Python calls keep working) and threads
  ``_resolve_chat_api_base_url(request)`` into ``ChatService(api_base_url=...)``.

Unlike tests/unit/test_chat_internal_loopback_auth.py (whose TestClient
fixture is an in-process ASGI transport with no real TCP port and therefore
cannot exercise a host:port mismatch), every request object here is a REAL
``starlette.requests.Request`` built from a real ASGI scope, so the
derivation logic is genuinely exercised.

``ChatService.__init__`` itself is NOT changed by this story: its
``api_base_url or os.environ.get(...)`` fallback stays as the final layer
(verified by a source-level guard below).
"""
from __future__ import annotations

import inspect
from typing import ClassVar

import fastapi
import pytest
from starlette.requests import Request as StarletteRequest

import app.chat as chat_mod
from app.chat import ChatRequest

ENV_VAR = "PIPELINE_CHAT_API_BASE"


# ---------------------------------------------------------------------------
# Real Starlette Request built from a real ASGI scope (NOT TestClient)
# ---------------------------------------------------------------------------


def _make_request(host: str = "127.0.0.1", port: int = 8001, scheme: str = "http") -> StarletteRequest:
    scope = {
        "type": "http",
        "scheme": scheme,
        "server": (host, port),
        "path": "/api/chat",
        "headers": [],
        "query_string": b"",
    }
    return StarletteRequest(scope)


# ---------------------------------------------------------------------------
# Recorder that short-circuits chat_endpoint before any real HTTP happens
# (same pattern as tests/unit/test_chat_api_key_plumbing.py)
# ---------------------------------------------------------------------------


class _Sentinel(BaseException):
    pass


class _RecordingService:
    constructed: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        type(self).constructed.append(kwargs)
        raise _Sentinel("construction recorded; stop chat_endpoint here")


def _record_construction(monkeypatch) -> ClassVar[list]:
    _RecordingService.constructed.clear()
    monkeypatch.setattr(chat_mod, "ChatService", _RecordingService)
    return _RecordingService.constructed


# ---------------------------------------------------------------------------
# 1. Core regression: request-derived base URL on a NON-default port
# ---------------------------------------------------------------------------


def test_resolve_derives_base_url_from_real_request_non_default_port(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    request = _make_request(port=8001)
    # sanity: the scope really produces a trailing-slash base_url, so the
    # exact equality below proves the rstrip("/") behaviour too
    assert str(request.base_url) == "http://127.0.0.1:8001/"
    assert chat_mod._resolve_chat_api_base_url(request) == "http://127.0.0.1:8001"


def test_resolve_result_has_no_trailing_slash(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    resolved = chat_mod._resolve_chat_api_base_url(_make_request(port=8001))
    assert not resolved.endswith("/")


def test_resolve_derives_base_url_for_other_host_and_port(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    request = _make_request(host="10.1.2.3", port=9443)
    assert chat_mod._resolve_chat_api_base_url(request) == "http://10.1.2.3:9443"


def test_resolve_derives_base_url_for_https_scheme_and_port(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    request = _make_request(scheme="https", port=8443)
    assert chat_mod._resolve_chat_api_base_url(request) == "https://127.0.0.1:8443"


def test_resolve_boundary_default_http_port_omitted_from_url(monkeypatch):
    """Starlette omits the port when it is the scheme's default (80)."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    request = _make_request(port=80)
    assert chat_mod._resolve_chat_api_base_url(request) == "http://127.0.0.1"


def test_resolve_boundary_default_https_port_omitted_from_url(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    request = _make_request(scheme="https", port=443)
    assert chat_mod._resolve_chat_api_base_url(request) == "https://127.0.0.1"


# ---------------------------------------------------------------------------
# 2. Explicit env override always wins, even with a real request in hand
# ---------------------------------------------------------------------------


def test_env_override_wins_over_request_derived_url(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "http://proxy.internal:9009")
    request = _make_request(port=8001)
    assert chat_mod._resolve_chat_api_base_url(request) == "http://proxy.internal:9009"


def test_env_override_wins_regardless_of_request_host_or_scheme(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "http://override.example:7777")
    for kwargs in ({"port": 8001}, {"host": "10.0.0.9", "port": 1234}, {"scheme": "https", "port": 8443}):
        assert chat_mod._resolve_chat_api_base_url(_make_request(**kwargs)) == "http://override.example:7777"


def test_empty_string_env_var_is_not_an_override_falls_through_to_request(monkeypatch):
    """Boundary: '' is falsy, so it must NOT shadow the request-derived URL."""
    monkeypatch.setenv(ENV_VAR, "")
    request = _make_request(port=8001)
    assert chat_mod._resolve_chat_api_base_url(request) == "http://127.0.0.1:8001"


# ---------------------------------------------------------------------------
# 3. No request + no env => None (falls through to ChatService's own default)
# ---------------------------------------------------------------------------


def test_resolve_returns_none_when_no_request_and_env_unset(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert chat_mod._resolve_chat_api_base_url(None) is None


def test_resolve_returns_none_for_missing_request_even_with_env_unset_explicitly(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert chat_mod._resolve_chat_api_base_url(request=None) is None


def test_env_override_wins_even_when_request_is_none(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "http://env-only.example:8000")
    assert chat_mod._resolve_chat_api_base_url(None) == "http://env-only.example:8000"


# ---------------------------------------------------------------------------
# 4. Wiring: chat_endpoint threads the resolved URL into ChatService
# ---------------------------------------------------------------------------


def test_chat_endpoint_threads_request_derived_base_url_into_chat_service(monkeypatch):
    """THE test that would have caught the original bug: end-to-end wiring."""
    constructed = _record_construction(monkeypatch)
    monkeypatch.delenv(ENV_VAR, raising=False)
    with pytest.raises(_Sentinel):
        chat_mod.chat_endpoint(
            ChatRequest(message="hi"),
            request=_make_request(port=8001),
            x_pipeline_api_key="k",
        )
    assert constructed[-1].get("api_base_url") == "http://127.0.0.1:8001"


def test_chat_endpoint_env_override_wins_at_wiring_level(monkeypatch):
    constructed = _record_construction(monkeypatch)
    monkeypatch.setenv(ENV_VAR, "http://operator-override.example:1234")
    with pytest.raises(_Sentinel):
        chat_mod.chat_endpoint(
            ChatRequest(message="hi"),
            request=_make_request(port=8001),
            x_pipeline_api_key="k",
        )
    assert constructed[-1].get("api_base_url") == "http://operator-override.example:1234"


def test_chat_endpoint_direct_call_without_request_passes_none_base_url(monkeypatch):
    """Direct plain-Python calls (existing tests do this) must keep working:
    no request + env unset => api_base_url None => ChatService's own
    ``api_base_url or os.environ.get(...)`` fallback stays in charge."""
    constructed = _record_construction(monkeypatch)
    monkeypatch.delenv(ENV_VAR, raising=False)
    with pytest.raises(_Sentinel):
        chat_mod.chat_endpoint(ChatRequest(message="hi"), x_pipeline_api_key="k")
    assert constructed[-1].get("api_base_url", None) is None


def test_chat_endpoint_still_forwards_api_key_alongside_base_url(monkeypatch):
    constructed = _record_construction(monkeypatch)
    monkeypatch.delenv(ENV_VAR, raising=False)
    with pytest.raises(_Sentinel):
        chat_mod.chat_endpoint(
            ChatRequest(message="hi"),
            request=_make_request(port=8001),
            x_pipeline_api_key="caller-key",
        )
    kwargs = constructed[-1]
    assert kwargs.get("api_key") == "caller-key"
    assert kwargs.get("api_base_url") == "http://127.0.0.1:8001"


# ---------------------------------------------------------------------------
# 5. Signature / import / placement requirements from the brief
# ---------------------------------------------------------------------------


def test_chat_module_imports_request_from_fastapi():
    assert chat_mod.Request is fastapi.Request


def test_chat_endpoint_signature_has_request_param_defaulting_to_none():
    sig = inspect.signature(chat_mod.chat_endpoint)
    assert "request" in sig.parameters
    param = sig.parameters["request"]
    # plain Python default, NOT a FastAPI injection marker: direct calls with
    # no request argument must keep working
    assert param.default is None
    assert not isinstance(param.default, fastapi.params.Header)
    assert not isinstance(param.default, fastapi.params.Depends)
    assert param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert param.annotation in ("Request", StarletteRequest)


def test_x_pipeline_api_key_param_untouched_by_this_change():
    sig = inspect.signature(chat_mod.chat_endpoint)
    param = sig.parameters["x_pipeline_api_key"]
    assert isinstance(param.default, fastapi.params.Header)
    assert param.default.alias == "x-pipeline-api-key"
    assert param.default.default is None


def test_helper_is_defined_directly_above_resolve_tool_url():
    resolve_line = inspect.getsourcelines(chat_mod._resolve_tool_url)[1]
    helper_line = inspect.getsourcelines(chat_mod._resolve_chat_api_base_url)[1]
    assert helper_line < resolve_line
    # "directly above": only the helper's own body (docstring etc.) between them
    assert resolve_line - helper_line < 60


def test_helper_docstring_documents_env_override_and_request_derivation():
    doc = inspect.getdoc(chat_mod._resolve_chat_api_base_url) or ""
    assert "PIPELINE_CHAT_API_BASE" in doc
    assert "request.base_url" in doc


def test_helper_is_pure_module_level_function_taking_single_optional_request():
    sig = inspect.signature(chat_mod._resolve_chat_api_base_url)
    assert list(sig.parameters) == ["request"]
    param = sig.parameters["request"]
    assert param.default is None
    assert param.annotation in ("Request | None", "Request |None", "Optional[Request]", StarletteRequest)


# ---------------------------------------------------------------------------
# 6. Guard rails: the layers this story must NOT change
# ---------------------------------------------------------------------------


def test_chat_service_init_fallback_layer_untouched():
    src = inspect.getsource(chat_mod.ChatService.__init__)
    assert "api_base_url or os.environ.get(" in src
    assert "PIPELINE_CHAT_API_BASE" in src


def test_chat_service_init_still_accepts_optional_api_base_url_keyword():
    sig = inspect.signature(chat_mod.ChatService.__init__)
    assert "api_base_url" in sig.parameters
    assert sig.parameters["api_base_url"].default is None


def test_resolve_tool_url_signature_untouched():
    sig = inspect.signature(chat_mod._resolve_tool_url)
    assert list(sig.parameters)[:2] == ["http_client", "api_base_url"]


# ---------------------------------------------------------------------------
# 5. SSRF regression: a client-supplied Host header must NOT be able to steer
#    the internal call target.  In Starlette 1.6.0, URL(scope=...) prefers the
#    client-supplied ``Host`` header over the ASGI ``server`` entry, so
#    ``request.base_url`` is attacker-controlled when uvicorn is run directly
#    with no TrustedHostMiddleware.  The internal call target must come from
#    ``scope["server"]`` (+ ``scope["scheme"]``), never from client headers.
# ---------------------------------------------------------------------------


def _make_request_with_host_header(
    host_header: bytes,
    host: str = "127.0.0.1",
    port: int = 8000,
    scheme: str = "http",
) -> StarletteRequest:
    """Same shape as _make_request, but the client sends a Host header."""
    scope = {
        "type": "http",
        "scheme": scheme,
        "server": (host, port),
        "path": "/api/chat",
        "headers": [(b"host", host_header)],
        "query_string": b"",
    }
    return StarletteRequest(scope)


def test_client_supplied_host_header_cannot_redirect_internal_calls(monkeypatch):
    """Attacker POSTs /api/chat with ``Host: evil.example:9999``.

    uvicorn is bound to 127.0.0.1:8000, so ``scope["server"]`` is
    ("127.0.0.1", 8000).  The resolved internal base URL must be
    ``http://127.0.0.1:8000`` -- the pre-fix code returned
    ``http://evil.example:9999`` here (request.base_url honours the Host
    header), which made every internal tool call -- carrying the shared
    ``X-Pipeline-Api-Key`` -- hit the attacker-chosen host.
    """
    monkeypatch.delenv(ENV_VAR, raising=False)
    attacker_request = _make_request_with_host_header(b"evil.example:9999")
    resolved = chat_mod._resolve_chat_api_base_url(attacker_request)
    assert resolved == "http://127.0.0.1:8000"
    assert "evil.example" not in resolved


def test_benign_scope_without_host_header_resolves_to_same_loopback_target(monkeypatch):
    """Immediately after the attacker's request, a legitimate request with an
    empty header list still has scope["server"] == ("127.0.0.1", 8000) and must
    resolve to the SAME target -- resolution is per-request, so nothing from
    the attacker's Host header may survive into it."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    benign_request = _make_request(host="127.0.0.1", port=8000)
    assert chat_mod._resolve_chat_api_base_url(benign_request) == "http://127.0.0.1:8000"
