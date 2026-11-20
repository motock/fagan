"""POST /api/chat/stream requires the dashboard shared secret.

The chat stream used to accept any ``k-``-prefixed ``X-Pipeline-Api-Key``
value in place of the dashboard secret, and invoked the model without
checking it. These tests drive the real dashboard app and pin that the
stream route is gated exactly like every other ``/api/`` route, and that a
refused request never constructs a ``ChatService``.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.chat as chat_module
from app import auth
from app import dashboard as d

_HEADER = "X-Pipeline-Api-Key"
_CHAT_STREAM_ROUTE = "/api/chat/stream"


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so these tests never read or
    create this repo's real .dashboard_api_key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def constructed(monkeypatch):
    """Replace ChatService with a recorder; the list holds one init-kwargs
    dict per construction, so an empty list means the model was never built."""
    created: list[dict] = []

    class _RecordingChatService:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def stream_turn(self, message, plan_name=None, history=None, workspace=None):
            yield {"type": "result", "reply": "hi", "tool_calls": [], "turns": 1}

    monkeypatch.setattr(chat_module, "ChatService", _RecordingChatService)
    return created


def _client_without_default_key():
    client = TestClient(d.app)
    client.headers.pop(_HEADER, None)
    return client


def test_k_prefixed_key_on_chat_stream_is_refused_and_no_service_built(key_file, constructed):
    client = _client_without_default_key()

    response = client.post(
        _CHAT_STREAM_ROUTE,
        json={"message": "hello"},
        headers={_HEADER: "k-123"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert constructed == []


def test_empty_key_on_chat_stream_is_refused(key_file, constructed):
    client = _client_without_default_key()

    response = client.post(
        _CHAT_STREAM_ROUTE,
        json={"message": "hello"},
        headers={_HEADER: ""},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert constructed == []


def test_bare_prefix_key_on_chat_stream_is_refused(key_file, constructed):
    client = _client_without_default_key()

    response = client.post(
        _CHAT_STREAM_ROUTE,
        json={"message": "hello"},
        headers={_HEADER: "k-"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert constructed == []


def test_missing_key_on_chat_stream_is_refused(key_file, constructed):
    client = _client_without_default_key()

    response = client.post(_CHAT_STREAM_ROUTE, json={"message": "hello"})

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert constructed == []


def test_dashboard_secret_on_chat_stream_is_accepted_and_forwarded(key_file, constructed):
    secret = auth.get_or_create_api_key()
    client = TestClient(d.app)

    response = client.post(
        _CHAT_STREAM_ROUTE,
        json={"message": "hello"},
        headers={_HEADER: secret},
    )

    assert response.status_code == 200
    assert len(constructed) == 1
    assert constructed[0]["api_key"] == secret


def test_require_api_key_refuses_k_prefixed_value_on_chat_stream_path(key_file):
    with pytest.raises(HTTPException) as excinfo:
        auth.require_api_key("k-x", "/api/chat/stream")

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "unauthorized"
