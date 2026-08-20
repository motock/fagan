"""Tests for the FastAPI chat endpoint (PART 4 of 4).

This story mounts a POST /api/chat route onto the existing dashboard app.
The endpoint delegates to ``ChatService.execute_turn``. These tests stub
``ChatService`` so they never need a live model, and they prove the
endpoint is reachable through the *full* dashboard app (i.e. the router is
mounted BEFORE the static-file catch-all, which would otherwise shadow
every /api/chat request with a 404).

The implementation does not exist yet; these tests must fail for the
right reason (a missing import / attribute) until it is added.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeChatService:
    """Stand-in for app.chat.ChatService.

    ``execute_turn`` returns a fixed dict so no live model is required.
    It also records the arguments it was called with so the tests can
    assert the endpoint forwarded the request body faithfully.
    """

    def __init__(self, *args, **kwargs):
        self.init_args = args
        self.init_kwargs = kwargs
        self.calls: list[dict] = []

    def execute_turn(self, message, *, plan_name=None, history=None) -> dict:
        self.calls.append(
            {"message": message, "plan_name": plan_name, "history": history}
        )
        return {"reply": "hi", "tool_calls": [], "turns": 1}


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def fake_chat(monkeypatch):
    """Replace app.chat.ChatService with the fake for the duration of a test."""
    import app.chat as chat_module

    fake = _FakeChatService
    monkeypatch.setattr(chat_module, "ChatService", fake)
    return chat_module.ChatService


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_chat_happy_path_returns_200(client, fake_chat):
    """POST /api/chat with a valid message returns 200 and the service reply."""
    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "hi"
    assert body["tool_calls"] == []
    assert body["turns"] == 1


def test_chat_forwards_plan_name_and_history(client, monkeypatch):
    """plan_name and history from the request body reach execute_turn."""

    captured: list[dict] = []

    class _CapturingService:
        def __init__(self, *args, **kwargs):
            pass

        def execute_turn(self, message, *, plan_name=None, history=None) -> dict:
            captured.append(
                {"message": message, "plan_name": plan_name, "history": history}
            )
            return {"reply": "hi", "tool_calls": [], "turns": 1}

    import app.chat as chat_module

    monkeypatch.setattr(chat_module, "ChatService", _CapturingService)

    resp = client.post(
        "/api/chat",
        json={
            "message": "hello",
            "plan_name": "demo",
            "history": [{"role": "user", "content": "prev"}],
        },
    )
    assert resp.status_code == 200
    assert captured == [
        {
            "message": "hello",
            "plan_name": "demo",
            "history": [{"role": "user", "content": "prev"}],
        }
    ]


def test_chat_optional_fields_default(client, monkeypatch):
    """Omitting plan_name/history still works; they default to None."""

    captured: list[dict] = []

    class _CapturingService:
        def __init__(self, *args, **kwargs):
            pass

        def execute_turn(self, message, *, plan_name=None, history=None) -> dict:
            captured.append(
                {"message": message, "plan_name": plan_name, "history": history}
            )
            return {"reply": "hi", "tool_calls": [], "turns": 1}

    import app.chat as chat_module

    monkeypatch.setattr(chat_module, "ChatService", _CapturingService)

    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200
    assert captured == [{"message": "hello", "plan_name": None, "history": None}]


# --------------------------------------------------------------------------- #
# Negative / boundary cases
# --------------------------------------------------------------------------- #
def test_chat_empty_message_returns_400(client, fake_chat):
    """An empty message string must be rejected with 400 by an explicit check.

    Pydantic's bare ``str`` validation silently accepts ``""``, so the
    endpoint must add an explicit ``if not req.message.strip()`` guard.
    """
    resp = client.post("/api/chat", json={"message": ""})
    assert resp.status_code == 400
    # The detail message must mention the empty-message condition.
    assert "empty" in resp.json()["detail"].lower()


def test_chat_whitespace_only_message_returns_400(client, fake_chat):
    """A whitespace-only message must also be rejected (the check uses .strip())."""
    resp = client.post("/api/chat", json={"message": "   \n\t  "})
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


def test_chat_missing_message_field_returns_422(client, fake_chat):
    """Omitting the required ``message`` field is a pydantic validation error (422)."""
    resp = client.post("/api/chat", json={})
    assert resp.status_code == 422


def test_chat_wrong_type_message_returns_422(client, fake_chat):
    """A non-string ``message`` is a pydantic validation error (422)."""
    resp = client.post("/api/chat", json={"message": 123})
    assert resp.status_code == 422


def test_chat_null_message_returns_422(client, fake_chat):
    """An explicit null ``message`` is a pydantic validation error (422)."""
    resp = client.post("/api/chat", json={"message": None})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Reachability through the full app (ordering of router vs static mount)
# --------------------------------------------------------------------------- #
def test_chat_endpoint_reachable_through_full_app(client, fake_chat):
    """The /api/chat route must be reachable through the full dashboard app.

    If this test fails with a 404, the router was very likely mounted
    AFTER the static catch-all (``app.mount("/", StaticFiles(...))``),
    which shadows every /api/chat request. The router must be included
    BEFORE that mount line.
    """
    resp = client.post("/api/chat", json={"message": "hello"})
    # A 404 here means the static catch-all shadowed the route.
    assert resp.status_code != 404, (
        "/api/chat returned 404 — the chat router was likely mounted "
        "AFTER the static catch-all and is shadowed by it."
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == "hi"


# --------------------------------------------------------------------------- #
# Structural / mechanically-checkable requirements
# --------------------------------------------------------------------------- #
def test_chat_router_is_apirouter():
    """app.chat must expose a ``chat_router`` that is a FastAPI APIRouter."""
    from fastapi import APIRouter

    from app.chat import chat_router

    assert isinstance(chat_router, APIRouter)


def test_chat_request_model_fields():
    """ChatRequest must define plan_name (optional), message (required), history (optional)."""
    from app.chat import ChatRequest

    fields = ChatRequest.model_fields
    assert "message" in fields
    assert "plan_name" in fields
    assert "history" in fields
    # message has no default (required).
    assert fields["message"].is_required()
    # plan_name and history are optional (default None).
    assert fields["plan_name"].default is None
    assert fields["history"].default is None


def test_chat_response_model_fields():
    """ChatResponse must define reply, tool_calls, turns."""
    from app.chat import ChatResponse

    fields = ChatResponse.model_fields
    assert set(fields) >= {"reply", "tool_calls", "turns"}


def test_dashboard_imports_chat_module():
    """dashboard.py must import app.chat (so the router is registered)."""
    import app.dashboard as dashboard_module

    # The import must have happened; check the module attribute is present.
    assert hasattr(dashboard_module, "chat")
    import app.chat as chat_module

    assert dashboard_module.chat is chat_module


def test_router_mounted_before_static_catch_all():
    """The include_router call must appear BEFORE the app.mount static line.

    This ordering is load-bearing: mounting the router after the static
    catch-all shadows every /api/chat request.
    """
    import inspect

    import app.dashboard as dashboard_module

    source = inspect.getsource(dashboard_module)
    include_idx = source.find("include_router(chat.chat_router")
    mount_idx = source.find('app.mount("/", StaticFiles')
    assert include_idx != -1, (
        "app.include_router(chat.chat_router, prefix=\"/api\") not found in "
        "dashboard.py source"
    )
    assert mount_idx != -1, 'app.mount("/", StaticFiles(...)) not found in dashboard.py source'
    assert include_idx < mount_idx, (
        "The chat router must be included BEFORE the static catch-all mount; "
        "otherwise the static mount shadows /api/chat."
    )


def test_chat_router_prefix_is_api():
    """The router must be mounted under the /api prefix."""
    import inspect

    import app.dashboard as dashboard_module

    source = inspect.getsource(dashboard_module)
    assert 'include_router(chat.chat_router, prefix="/api")' in source


def test_chat_py_does_not_contain_forbidden_names():
    """app/chat.py must not reference PipelineService or _service.

    A later story grades this as a security property (no direct pipeline
    access from the chat adapter).
    """
    import inspect

    import app.chat as chat_module

    source = inspect.getsource(chat_module)
    assert "PipelineService" not in source, (
        "app/chat.py must not reference PipelineService"
    )
    assert "_service" not in source, "app/chat.py must not reference _service"