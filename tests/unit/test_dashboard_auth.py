"""Tests for the dashboard's shared-secret API key gate (app/auth.py).

The gate is registered as an application-level FastAPI dependency, so every
route is covered uniformly. These tests drive the real `/api/health` route
rather than calling `require_api_key` directly, so a regression that removes
the app-level wiring (and not just the function) still fails here.
"""
import os
import stat

import pytest
from fastapi.testclient import TestClient

import app.chat as chat_module
from app import auth
from app import dashboard as d

_HEADER = "X-Pipeline-Api-Key"


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so these tests never read or
    create this repo's real .dashboard_api_key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


def test_request_without_api_key_header_is_rejected():
    client = TestClient(d.app)
    # tests/unit/conftest.py attaches the header to every TestClient by
    # default; drop it to exercise the missing-header path.
    client.headers.pop(_HEADER, None)

    response = client.get("/api/health")

    assert response.status_code == 401


def test_request_with_wrong_api_key_is_rejected():
    client = TestClient(d.app)
    client.headers[_HEADER] = "not-the-real-key"

    response = client.get("/api/health")

    assert response.status_code == 401


def test_request_with_correct_api_key_succeeds():
    client = TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})

    response = client.get("/api/health")

    assert response.status_code == 200


def test_get_or_create_api_key_is_idempotent(key_file):
    first = auth.get_or_create_api_key()

    assert auth.get_or_create_api_key() == first


def test_generated_key_file_is_owner_readable_only(key_file):
    auth.get_or_create_api_key()

    mode = os.stat(key_file).st_mode

    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


# --------------------------------------------------------------------------- #
# SSE-03 review fix: dual-path auth (dashboard secret vs chat pass-through)
#
# Regression tests for the review finding that ANY caller-chosen
# "k-"-prefixed X-Pipeline-Api-Key value bypassed the dashboard shared
# secret on EVERY /api/* route (app/auth.py's unconditional early return).
# The gate must keep authenticating the caller: the k- pass-through is
# scoped to the chat stream route only; every other /api/* route still
# requires the dashboard shared secret from the 0600 key file.
# --------------------------------------------------------------------------- #

_NON_CHAT_ROUTE = "/api/config"  # real registered non-chat /api/* route
_CHAT_STREAM_ROUTE = "/api/chat/stream"


@pytest.fixture
def fake_chat_service(monkeypatch):
    """Stub app.chat.ChatService so POST /api/chat/stream answers 200 with no
    live model (same pattern as tests/unit/test_chat_stream_endpoint.py's
    install_fake_chat). Returns the list of init-kwargs dicts, one per
    ChatService construction, so tests can assert what the header forwarded.
    """

    created: list[dict] = []

    class _FakeChatService:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def stream_turn(self, message, plan_name=None, history=None, workspace=None):
            yield {"type": "result", "reply": "hi", "tool_calls": [], "turns": 1}

    monkeypatch.setattr(chat_module, "ChatService", _FakeChatService)
    return created


def test_pipeline_key_header_does_not_authenticate_non_chat_routes():
    """A caller-chosen "k-" header value must NOT bypass the dashboard
    shared secret on non-chat /api/* routes.

    Buggy behavior (unconditional k- early return in require_api_key): this
    request answered 200 because the two-character prefix matched. Correct
    behavior: the pass-through exemption is scoped to the chat stream
    route, so a k- header alone here is unauthenticated -> 401.
    """
    client = TestClient(d.app)
    # conftest attaches the real dashboard secret to every TestClient; drop
    # it so the ONLY credential presented is the k- header value.
    client.headers.pop(_HEADER, None)

    response = client.get(_NON_CHAT_ROUTE, headers={_HEADER: "k-x"})

    assert response.status_code == 401


def test_pipeline_key_passthrough_value_also_rejected_on_non_chat_routes():
    """Even a well-formed pipeline-style value ("k-123", the exact value the
    chat pass-through accepts) must not authenticate a non-chat route.

    Guards the per-request evaluation of the exemption: acceptance on the
    chat stream route must not carry over to a later request, and a k-
    header alone must never satisfy the dashboard gate elsewhere.
    """
    client = TestClient(d.app)
    client.headers.pop(_HEADER, None)

    response = client.get(_NON_CHAT_ROUTE, headers={_HEADER: "k-123"})

    assert response.status_code == 401


def test_wrong_dashboard_secret_returns_401():
    """Negative control: a wrong NON-k- dashboard secret still 401s on the
    non-chat route — the dashboard shared secret remains the access control
    for everything outside the chat pass-through."""
    client = TestClient(d.app)
    client.headers[_HEADER] = "not-the-real-key"

    response = client.get(_NON_CHAT_ROUTE)

    assert response.status_code == 401


def test_valid_dashboard_secret_authenticates():
    """Positive control: the correct dashboard secret still authenticates the
    non-chat route, proving the 401 assertions are not vacuous (the route is
    reachable and the gate accepts the real secret)."""
    client = TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})

    response = client.get(_NON_CHAT_ROUTE)

    assert response.status_code == 200


def test_chat_stream_route_accepts_pipeline_passthrough_key(fake_chat_service):
    """Direct auth-level coverage of the pass-through branch: the chat
    stream route accepts a k- pipeline key with NO dashboard secret, and the
    value is forwarded into ChatService as the upstream credential.
    (Previously this branch was only exercised incidentally by
    test_chat_stream_endpoint.py's k-123 test.)"""
    client = TestClient(d.app)

    response = client.post(
        _CHAT_STREAM_ROUTE,
        json={"message": "hello"},
        headers={_HEADER: "k-123"},
    )

    assert response.status_code == 200
    assert fake_chat_service[0]["api_key"] == "k-123"


def test_chat_stream_gate_documents_k_prefix_acceptance(fake_chat_service):
    """Documented behavior, pinned: on the chat stream route the gate passes
    ANY k-prefixed value through without comparing it to the dashboard
    secret — including garbage like "k-not-a-key". The dashboard gate does
    not validate the upstream credential; the pipeline itself rejects
    invalid keys when ChatService's internal calls reach it. Pinning this
    makes garbage-k- acceptance on the chat route intentional and documented
    (per review): a future change that starts validating k- values against
    the dashboard secret here must update this contract deliberately."""
    client = TestClient(d.app)

    response = client.post(
        _CHAT_STREAM_ROUTE,
        json={"message": "hello"},
        headers={_HEADER: "k-not-a-key"},
    )

    assert response.status_code == 200
    assert fake_chat_service[0]["api_key"] == "k-not-a-key"
