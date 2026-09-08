"""Unit tests for the api_key plumbing in app/chat.py.

Complements the read-only end-to-end acceptance fixture
tests/unit/test_chat_internal_loopback_auth.py (which drives the REAL
app.dashboard.app through a real TestClient). These tests cover the
mechanics that fixture does not:

* ChatService.__init__'s keyword-only ``api_key`` parameter (signature).
* The header is applied to an INJECTED client that has a ``.headers``
  attribute - not just the self-constructed branch.
* The ``hasattr(self._http_client, 'headers')`` guard: duck-typed fakes
  without ``.headers`` (the shape every existing test_chat_*.py fake uses)
  must not raise, whether or not api_key is supplied.
* api_key omitted / empty string => no header written (truthiness gate).
* chat_endpoint declares the ``x-pipeline-api-key`` header parameter
  (fastapi.Header, alias, default None) and forwards it into ChatService.
* The auth middleware itself is untouched (still importable/callable).
"""
from __future__ import annotations

import inspect

import fastapi
import pytest

import app.chat as chat_mod
from app.chat import ChatRequest, ChatService


class _StubDriver:
    model = "stub"

    def complete(self, *, prompt, system, model, cwd):  # pragma: no cover
        return "ok"


class _FakeClientWithHeaders:
    def __init__(self) -> None:
        self.headers: dict = {}

    def post(self, *a, **k):  # pragma: no cover - never called here
        raise AssertionError("no HTTP expected in this unit test")


class _DuckClientNoHeaders:
    """The shape used by the ~29 existing test_chat_*.py fakes: no .headers."""

    def post(self, *a, **k):  # pragma: no cover - never called here
        raise AssertionError("no HTTP expected in this unit test")


def _close(service) -> None:
    client = getattr(service, "_http_client", None)
    if client is not None and hasattr(client, "close"):
        client.close()


# ---------------------------------------------------------------------------
# ChatService.__init__ signature
# ---------------------------------------------------------------------------


def test_init_has_keyword_only_api_key_defaulting_to_none():
    sig = inspect.signature(ChatService.__init__)
    assert "api_key" in sig.parameters
    param = sig.parameters["api_key"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None
    assert str(param.annotation) in ("str | None", "typing.Optional[str]")


# ---------------------------------------------------------------------------
# Header application on the http client
# ---------------------------------------------------------------------------


def test_init_sets_header_on_injected_client_preserving_existing_headers():
    fake = _FakeClientWithHeaders()
    fake.headers["User-Agent"] = "unit-test"
    svc = ChatService(driver=_StubDriver(), http_client=fake, api_key="secret-key")
    try:
        assert fake.headers["X-Pipeline-Api-Key"] == "secret-key"
        assert fake.headers["User-Agent"] == "unit-test"
    finally:
        _close(svc)


def test_init_sets_header_on_self_constructed_client():
    svc = ChatService(driver=_StubDriver(), api_key="secret-key")
    try:
        assert svc._http_client.headers.get("X-Pipeline-Api-Key") == "secret-key"
    finally:
        _close(svc)


def test_init_without_api_key_writes_no_header():
    fake = _FakeClientWithHeaders()
    svc = ChatService(driver=_StubDriver(), http_client=fake)
    try:
        assert "X-Pipeline-Api-Key" not in fake.headers
    finally:
        _close(svc)

    svc2 = ChatService(driver=_StubDriver())
    try:
        assert "X-Pipeline-Api-Key" not in svc2._http_client.headers
    finally:
        _close(svc2)


def test_init_with_empty_string_api_key_writes_no_header():
    fake = _FakeClientWithHeaders()
    svc = ChatService(driver=_StubDriver(), http_client=fake, api_key="")
    try:
        assert fake.headers == {}
    finally:
        _close(svc)


# ---------------------------------------------------------------------------
# hasattr guard: duck-typed clients without .headers must never raise
# ---------------------------------------------------------------------------


def test_duck_typed_client_without_headers_does_not_raise_with_api_key():
    fake = _DuckClientNoHeaders()
    svc = ChatService(driver=_StubDriver(), http_client=fake, api_key="secret-key")
    assert not hasattr(fake, "headers")
    _close(svc)


def test_duck_typed_client_without_headers_still_works_without_api_key():
    fake = _DuckClientNoHeaders()
    svc = ChatService(driver=_StubDriver(), http_client=fake)
    assert not hasattr(fake, "headers")
    _close(svc)


# ---------------------------------------------------------------------------
# chat_endpoint: header parameter declaration + forwarding
# ---------------------------------------------------------------------------


def test_chat_module_imports_header_from_fastapi():
    import fastapi

    assert chat_mod.Header is fastapi.Header


def test_chat_endpoint_declares_x_pipeline_api_key_header_param():
    sig = inspect.signature(chat_mod.chat_endpoint)
    assert "x_pipeline_api_key" in sig.parameters
    default = sig.parameters["x_pipeline_api_key"].default
    assert isinstance(default, fastapi.Header)
    assert default.alias == "x-pipeline-api-key"
    assert default.default is None


class _Sentinel(BaseException):
    pass


class _RecordingService:
    constructed: list = []

    def __init__(self, *args, **kwargs):
        type(self).constructed.append(kwargs)
        raise _Sentinel("construction recorded; stop chat_endpoint here")


def test_chat_endpoint_forwards_header_value_to_chat_service(monkeypatch):
    _RecordingService.constructed.clear()
    monkeypatch.setattr(chat_mod, "ChatService", _RecordingService)
    with pytest.raises(_Sentinel):
        chat_mod.chat_endpoint(ChatRequest(message="hi"), x_pipeline_api_key="caller-key")
    assert _RecordingService.constructed[-1].get("api_key") == "caller-key"


def test_chat_endpoint_forwards_none_when_header_absent(monkeypatch):
    _RecordingService.constructed.clear()
    monkeypatch.setattr(chat_mod, "ChatService", _RecordingService)
    with pytest.raises(_Sentinel):
        chat_mod.chat_endpoint(ChatRequest(message="hi"), x_pipeline_api_key=None)
    assert _RecordingService.constructed[-1].get("api_key", None) is None


# ---------------------------------------------------------------------------
# The fix must stay inside app/chat.py: the auth gate is untouched
# ---------------------------------------------------------------------------


def test_auth_middleware_still_in_place():
    import app.auth as auth_mod
    import app.dashboard as dashboard_mod

    assert callable(auth_mod.require_api_key)
    assert callable(getattr(dashboard_mod, "_enforce_api_key", None))