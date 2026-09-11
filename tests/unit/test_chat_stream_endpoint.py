"""Tests for the SSE streaming chat endpoint (POST /api/chat/stream).

The endpoint streams ``ChatService.stream_turn`` events as Server-Sent
Events frames. These tests stub ``ChatService`` (same pattern as
``tests/unit/test_chat_endpoint.py``) so no live model is needed, and they
exercise the route through the *full* dashboard app.

The implementation does not exist yet; these tests must fail for the right
reason (a missing route / missing ``_sse_pack`` helper) until it is added.
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

import app.chat as chat_module
from app import dashboard as d


# --------------------------------------------------------------------------- #
# SSE parsing helper
# --------------------------------------------------------------------------- #
def _frames(body: str) -> list[tuple[str | None, dict | None]]:
    """Parse an SSE body into (event_type, data_dict) tuples.

    Each frame is ``event: <type>\\ndata: <json>\\n\\n``; the trailing
    separator yields an empty chunk which is skipped.
    """
    parsed: list[tuple[str | None, dict | None]] = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        etype: str | None = None
        data: dict | None = None
        for line in chunk.split("\n"):
            if line.startswith("event: "):
                etype = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        parsed.append((etype, data))
    return parsed


SIMPLE_EVENTS = [
    {"type": "turn", "data": {"n": 1}},
    {"type": "reply", "data": {"text": "hi"}},
    {"type": "result", "data": {"reply": "hi", "tool_calls": [], "turns": 1}},
]

TOOL_EVENTS = [
    {"type": "turn", "data": {"n": 1}},
    {"type": "tool_call", "data": {"name": "get_status", "args": {"plan_name": "P1"}}},
    {"type": "tool_result", "data": {"name": "get_status", "args": {"plan_name": "P1"}, "result": {"ok": True}}},
    {"type": "result", "data": {"reply": "done", "tool_calls": [{"name": "get_status", "args": {"plan_name": "P1"}}], "turns": 1}},
]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def install_fake_chat(monkeypatch):
    """Install a fake ``app.chat.ChatService`` and return (created, calls).

    - ``events``: the stream_turn yield list (default: SIMPLE_EVENTS).
    - ``exc``: raise immediately from stream_turn instead of yielding.
    - ``fail_after``: yield this many events, then raise RuntimeError.
    """

    def _install(events=None, exc=None, fail_after=None):
        created: list[object] = []
        calls: list[dict] = []
        stream_events = SIMPLE_EVENTS if events is None else events

        class _FakeChatService:
            def __init__(self, *args, **kwargs):
                self.init_kwargs = kwargs
                created.append(self)

            def stream_turn(self, message, *, plan_name=None, history=None, workspace=None):
                calls.append(
                    {"message": message, "plan_name": plan_name, "history": history, "workspace": workspace}
                )
                if exc is not None:
                    raise exc
                emitted = 0
                for emitted, ev in enumerate(stream_events):
                    if fail_after is not None and emitted >= fail_after:
                        raise RuntimeError("MID_STREAM_BOOM_MARKER")
                    yield ev

        monkeypatch.setattr(chat_module, "ChatService", _FakeChatService)
        return created, calls

    return _install


# --------------------------------------------------------------------------- #
# Module-level helper: _sse_pack
# --------------------------------------------------------------------------- #
def test_sse_pack_formats_exact_frame():
    """_sse_pack renders one event dict as an exact SSE frame."""
    assert chat_module._sse_pack({"type": "turn", "data": {"n": 1}}) == 'event: turn\ndata: {"n": 1}\n\n'


def test_sse_pack_is_module_level_callable():
    """The helper is a module-level callable on app.chat."""
    assert callable(chat_module._sse_pack)


# --------------------------------------------------------------------------- #
# Route registration (membership only - /chat must survive untouched)
# --------------------------------------------------------------------------- #
def test_chat_stream_route_registered_on_existing_chat_router():
    """chat_router gains a POST /chat/stream route; POST /chat still exists."""
    routes = {(getattr(r, "path", None), frozenset(getattr(r, "methods", None) or []))
              for r in chat_module.chat_router.routes}
    assert ("/chat/stream", frozenset({"POST"})) in routes
    assert ("/chat", frozenset({"POST"})) in routes


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_stream_returns_200_with_event_stream_content_type(client, install_fake_chat):
    """POST /api/chat/stream returns 200 with a text/event-stream body."""
    install_fake_chat()
    resp = client.post("/api/chat/stream", json={"message": "hello"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]


def test_stream_response_headers_disable_caching_and_buffering(client, install_fake_chat):
    """The StreamingResponse sets no-cache and X-Accel-Buffering headers."""
    install_fake_chat()
    resp = client.post("/api/chat/stream", json={"message": "hello"})
    assert resp.headers.get("cache-control") == "no-cache"
    assert resp.headers.get("x-accel-buffering") == "no"


def test_stream_body_contains_event_and_data_lines(client, install_fake_chat):
    """The body has at least one `event: ` line and one `data: ` line."""
    install_fake_chat()
    resp = client.post("/api/chat/stream", json={"message": "hello"})
    body = resp.text
    assert body.count("event: ") >= 1
    assert body.count("data: ") >= 1
    for etype, data in _frames(body):
        assert etype is not None
        assert isinstance(data, dict)


def test_stream_body_contains_result_frame_with_expected_keys(client, install_fake_chat):
    """A `event: result` frame exists whose data has reply/tool_calls/turns."""
    install_fake_chat()
    resp = client.post("/api/chat/stream", json={"message": "hello"})
    result_frames = [(e, d_) for e, d_ in _frames(resp.text) if e == "result"]
    assert result_frames, "expected at least one 'event: result' frame"
    data = result_frames[-1][1]
    assert "reply" in data
    assert "tool_calls" in data
    assert "turns" in data


def test_stream_tool_call_frame_precedes_tool_result_frame(client, install_fake_chat):
    """A tool-using turn emits tool_call before tool_result."""
    install_fake_chat(events=TOOL_EVENTS)
    resp = client.post("/api/chat/stream", json={"message": "run the tool"})
    etypes = [e for e, _ in _frames(resp.text)]
    assert "tool_call" in etypes
    assert "tool_result" in etypes
    assert etypes.index("tool_call") < etypes.index("tool_result")


def test_stream_forwards_plan_name_history_workspace(client, install_fake_chat):
    """plan_name/history/workspace from the body reach stream_turn unchanged."""
    created, calls = install_fake_chat()
    history = [{"role": "user", "content": "earlier"}]
    resp = client.post(
        "/api/chat/stream",
        json={"message": "hello", "plan_name": "P1", "history": history, "workspace": "/tmp/ws"},
    )
    assert resp.status_code == 200
    assert calls == [
        {"message": "hello", "plan_name": "P1", "history": history, "workspace": "/tmp/ws"}
    ]
    assert len(created) == 1


def test_stream_constructs_service_with_api_key_and_base_url(client, install_fake_chat):
    """ChatService gets api_key from the header and an api_base_url."""
    created, _ = install_fake_chat()
    resp = client.post(
        "/api/chat/stream",
        json={"message": "hello"},
        headers={"x-pipeline-api-key": "k-123"},
    )
    assert resp.status_code == 200
    assert created[0].init_kwargs.get("api_key") == "k-123"
    assert "api_base_url" in created[0].init_kwargs


# --------------------------------------------------------------------------- #
# Negative / boundary
# --------------------------------------------------------------------------- #
def test_stream_empty_message_returns_400_without_building_service(client, install_fake_chat):
    """Empty message -> real 400 with exact detail, and no ChatService built."""
    created, _ = install_fake_chat()
    resp = client.post("/api/chat/stream", json={"message": ""})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "message must not be empty"
    assert created == [], "ChatService must not be constructed for an empty message"


def test_stream_whitespace_only_message_returns_400(client, install_fake_chat):
    """Whitespace-only message is rejected exactly like the empty message."""
    created, _ = install_fake_chat()
    resp = client.post("/api/chat/stream", json={"message": "   "})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "message must not be empty"
    assert created == []


def test_stream_missing_message_key_returns_422(client, install_fake_chat):
    """No `message` key at all is a FastAPI validation error (422)."""
    install_fake_chat()
    resp = client.post("/api/chat/stream", json={"plan_name": "P1"})
    assert resp.status_code == 422


def test_stream_mid_stream_failure_yields_error_frame_last_without_leaking(client, install_fake_chat):
    """On failure the stream still returns 200; the LAST frame is the error
    frame with the generic message, and the exception text never leaks."""
    install_fake_chat(fail_after=1)
    resp = client.post("/api/chat/stream", json={"message": "hello"})
    assert resp.status_code == 200
    frames = _frames(resp.text)
    assert frames, "expected at least the error frame"
    assert frames[-1][0] == "error"
    assert frames[-1][1] == {"message": "chat stream failed"}
    assert "MID_STREAM_BOOM_MARKER" not in resp.text
    # events emitted before the failure are still delivered
    assert frames[0][0] == "turn"


def test_stream_immediate_failure_yields_only_error_frame_and_logs(client, install_fake_chat, caplog):
    """An immediate stream_turn failure yields a single error frame, logs the
    full detail server-side on the 'pipeline' logger at ERROR level, and does
    not leak the exception text to the client."""
    install_fake_chat(exc=RuntimeError("IMMEDIATE_BOOM_SECRET"))
    with caplog.at_level(logging.ERROR, logger="pipeline"):
        resp = client.post("/api/chat/stream", json={"message": "hello"})
    assert resp.status_code == 200
    frames = _frames(resp.text)
    assert frames == [("error", {"message": "chat stream failed"})]
    assert "IMMEDIATE_BOOM_SECRET" not in resp.text
    pipeline_errors = [r for r in caplog.records if r.name == "pipeline" and r.levelno >= logging.ERROR]
    assert pipeline_errors, "expected a server-side ERROR log on the 'pipeline' logger"
    assert "IMMEDIATE_BOOM_SECRET" in caplog.text, "full detail must be logged server-side"