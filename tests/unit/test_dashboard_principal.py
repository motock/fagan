"""Tests for TEN-WIRE-01: the dashboard middleware attaches the authenticated
principal to ``request.state`` (the first consumer of the ADR 0009 identity
seam).

The middleware in ``app/dashboard.py`` resolves the presented
``x-pipeline-api-key`` header to a ``Principal`` via
``app.identity.SharedSecretProvider`` *after* the existing gate passes. These
tests pin three things:

* the happy path attaches ``LOCAL_OPERATOR`` (driven through a real
  ``TestClient`` request so the app-level middleware wiring is exercised, not
  just the function);
* the denial paths (401/403) are byte-for-byte unchanged and never set a
  principal at all — ``hasattr(request.state, "principal")`` stays ``False``,
  so a careless ``request.state.principal = None`` before the gate fails here;
* the chat-stream ``k-`` pass-through is explicitly *not* an identity: the
  request proceeds with ``request.state.principal is None``.

Attaching the principal is not enforcement: nothing reads
``request.state.principal`` yet (ADR 0009 authorizes the seam now; the
service-layer enforcement is the follow-up plan).
"""
import asyncio

import pytest
from fastapi import Request, Response
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth
from app import dashboard as d
from app.identity import LOCAL_OPERATOR

_HEADER = "X-Pipeline-Api-Key"
_PROBE_PATH = "/api/_test/principal"
_NON_CHAT_PATH = "/api/status"
_CHAT_STREAM_PATH = auth.CHAT_STREAM_PATH


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so these tests never read or
    create this repo's real .dashboard_api_key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def principal_probe():
    """Register a test-only /api/ route that reports what the middleware
    attached, then remove it so no other test module sees the extra route."""
    async def _probe(request: Request):
        principal = getattr(request.state, "principal", None)
        return {
            "is_local_operator": principal == LOCAL_OPERATOR,
            "principal_is_none": principal is None,
        }

    route = APIRoute(_PROBE_PATH, _probe, methods=["GET"])
    d.app.router.routes.insert(0, route)
    try:
        yield
    finally:
        d.app.router.routes.remove(route)


def _request(path, *, host="testserver", api_key=None, server=("testserver", 80)):
    """Build a Starlette Request the middleware can run against directly.

    Used for the state-after-denial assertions: a TestClient response cannot
    show whether ``request.state.principal`` was set, and the denial paths
    must not set it at all.
    """
    headers = [(b"host", host.encode("latin-1"))]
    if api_key is not None:
        headers.append((b"x-pipeline-api-key", api_key.encode("latin-1")))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "server": server,
        "client": ("127.0.0.1", 54321),
    }
    return Request(scope)


async def _call_next(request):
    return Response("reached-the-route")


def _run_middleware(request):
    return asyncio.run(d._enforce_api_key(request, _call_next))


# --------------------------------------------------------------------------- #
# Happy path: the authenticated principal is attached
# --------------------------------------------------------------------------- #

def test_correct_dashboard_secret_attaches_local_operator(key_file, principal_probe):
    client = TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})

    response = client.get(_PROBE_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "is_local_operator": True,
        "principal_is_none": False,
    }


def test_chat_stream_with_dashboard_secret_attaches_local_operator(key_file):
    """The chat route authenticated by the dashboard secret (no k- prefix) is
    a real identity, unlike the pass-through branch."""
    request = _request(_CHAT_STREAM_PATH, api_key=auth.get_or_create_api_key())

    response = _run_middleware(request)

    assert response.status_code == 200
    assert request.state.principal == LOCAL_OPERATOR


# --------------------------------------------------------------------------- #
# Denial paths: unchanged 401/403 and no principal at all
# --------------------------------------------------------------------------- #

def test_missing_key_returns_401_and_sets_no_principal(key_file):
    request = _request(_NON_CHAT_PATH)

    response = _run_middleware(request)

    assert response.status_code == 401
    assert response.body == b'{"detail":"unauthorized"}'
    assert hasattr(request.state, "principal") is False


def test_wrong_key_returns_401_and_sets_no_principal(key_file):
    auth.get_or_create_api_key()
    request = _request(_NON_CHAT_PATH, api_key="not-the-real-key")

    response = _run_middleware(request)

    assert response.status_code == 401
    assert hasattr(request.state, "principal") is False


def test_pipeline_prefixed_key_on_non_chat_route_returns_401_and_sets_no_principal(
    key_file,
):
    """A caller-chosen k- value is not an identity outside the chat route."""
    auth.get_or_create_api_key()
    request = _request(_NON_CHAT_PATH, api_key="k-abc")

    response = _run_middleware(request)

    assert response.status_code == 401
    assert hasattr(request.state, "principal") is False


def test_disallowed_host_returns_403_and_sets_no_principal(key_file):
    request = _request(
        _NON_CHAT_PATH,
        host="evil.example.com",
        api_key=auth.get_or_create_api_key(),
    )

    response = _run_middleware(request)

    assert response.status_code == 403
    assert response.body == b'{"detail":"Forbidden"}'
    assert hasattr(request.state, "principal") is False


# --------------------------------------------------------------------------- #
# Chat-stream pass-through: not authenticated by this gate, so no identity
# --------------------------------------------------------------------------- #

def test_chat_stream_passthrough_key_is_not_an_identity(key_file):
    request = _request(_CHAT_STREAM_PATH, api_key="k-abc")

    response = _run_middleware(request)

    assert response.status_code == 200
    assert response.body == b"reached-the-route"
    assert request.state.principal is None


# --------------------------------------------------------------------------- #
# The provider must read the key per request, not freeze it at import time
# --------------------------------------------------------------------------- #

def test_provider_resolves_the_key_on_every_request(key_file):
    """Guards ``SharedSecretProvider(get_or_create_api_key)`` (callable form).

    The dashboard creates the key lazily on first use and can rotate it, so a
    provider built with ``get_or_create_api_key()`` would freeze the first
    value and reject every later key.
    """
    key_file.write_text("abc")
    first = _request(_NON_CHAT_PATH, api_key="abc")
    assert _run_middleware(first).status_code == 200
    assert first.state.principal == LOCAL_OPERATOR

    key_file.write_text("def")
    rotated = _request(_NON_CHAT_PATH, api_key="def")
    assert _run_middleware(rotated).status_code == 200
    assert rotated.state.principal == LOCAL_OPERATOR

    stale = _request(_NON_CHAT_PATH, api_key="abc")
    assert _run_middleware(stale).status_code == 401
    assert hasattr(stale.state, "principal") is False
