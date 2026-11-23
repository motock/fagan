"""A failed chat model call is answered with a structured refusal.

When ``driver.complete`` raises during a chat turn, ``POST /api/chat`` answers
HTTP 502 with a ``detail`` carrying a reason code, a fixed message and the
request's correlation id, and ``POST /api/chat/stream`` ends with an ``error``
frame carrying the same three fields. The raw exception text never reaches the
caller; it is logged server-side at WARNING without the user's message text.
An unconfigured chat role gets the same shape with ``chat_not_configured``.
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

import app.chat as chat_module
from app import dashboard as d
from app.role_registry import RoleRegistryError

RAW_ERROR = "secret /path detail"
PRIVATE_MESSAGE = "PRIVATE_USER_MESSAGE_TEXT"
UPSTREAM_ID = "abcdef012345"
REFUSAL_KEYS = {"reason", "message", "correlation_id"}


class _FailingDriver:
    model = "test-model"

    def complete(self, **kwargs):
        raise RuntimeError(RAW_ERROR)


class _ReplyingDriver:
    model = "test-model"

    def complete(self, **kwargs):
        return "hi"


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def route_to_driver(monkeypatch):
    """Send the chat turn to the given driver, skipping role resolution."""

    def _route(driver):
        monkeypatch.setattr(
            chat_module.ChatService,
            "_resolve_driver",
            lambda self, repo_url=None: (driver, "test-model"),
        )

    return _route


def _frames(body: str) -> list[tuple[str, dict]]:
    frames = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        etype, data = None, None
        for line in chunk.split("\n"):
            if line.startswith("event: "):
                etype = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        frames.append((etype, data))
    return frames


# --------------------------------------------------------------------------- #
# POST /api/chat
# --------------------------------------------------------------------------- #
def test_chat_backend_failure_answers_502_with_reason_and_no_raw_text(client, route_to_driver):
    """A driver failure is a 502 refusal, and the raw exception text is absent."""
    route_to_driver(_FailingDriver())
    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 502
    assert RAW_ERROR not in resp.text
    detail = resp.json()["detail"]
    assert set(detail) == REFUSAL_KEYS
    assert detail["reason"] == "chat_backend_failed"
    assert detail["message"] == "The chat model could not be reached."


def test_chat_backend_failure_body_carries_the_request_correlation_id(client, route_to_driver):
    """The correlation id in the refusal is the one the request carried."""
    route_to_driver(_FailingDriver())
    resp = client.post("/api/chat", json={"message": "hello"}, headers={"x-correlation-id": UPSTREAM_ID})
    assert resp.json()["detail"]["correlation_id"] == UPSTREAM_ID


def test_chat_role_not_configured_answers_502_with_chat_not_configured(client, monkeypatch):
    """A RoleRegistryError during role resolution is a 502 chat_not_configured refusal."""
    from app import role_registry

    def _unconfigured(*args, **kwargs):
        raise RoleRegistryError("chat: no provider declared")

    monkeypatch.setattr(role_registry, "resolve_role", _unconfigured)
    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert set(detail) == REFUSAL_KEYS
    assert detail["reason"] == "chat_not_configured"
    assert detail["message"] == "No chat model is configured for this team."
    assert "chat: no provider declared" not in resp.text


def test_chat_empty_message_is_still_400(client, route_to_driver):
    """A message that is blank is still refused as a 400 before any model call."""
    route_to_driver(_FailingDriver())
    resp = client.post("/api/chat", json={"message": "   "})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "message must not be empty"


def test_chat_successful_turn_is_still_200_with_today_body(client, route_to_driver):
    """A turn whose model call succeeds keeps the existing response body."""
    route_to_driver(_ReplyingDriver())
    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "hi"
    assert body["tool_calls"] == []
    assert body["turns"] == 1
    assert body["proposed_actions"] == []


# --------------------------------------------------------------------------- #
# POST /api/chat/stream
# --------------------------------------------------------------------------- #
def test_stream_backend_failure_ends_with_terminal_error_frame_and_closes(client, route_to_driver):
    """The stream's last frame carries the three fields, and the response closes cleanly."""
    route_to_driver(_FailingDriver())
    resp = client.post("/api/chat/stream", json={"message": "hello"})
    assert resp.status_code == 200
    assert RAW_ERROR not in resp.text
    frames = _frames(resp.text)
    assert len(frames) == 1
    etype, data = frames[-1]
    assert etype == "error"
    assert set(data) == REFUSAL_KEYS
    assert data["reason"] == "chat_backend_failed"
    assert data["message"] == "The chat model could not be reached."


def test_stream_backend_failure_carries_the_request_correlation_id(client, route_to_driver):
    """The stream's terminal frame names the correlation id the request carried."""
    route_to_driver(_FailingDriver())
    resp = client.post(
        "/api/chat/stream", json={"message": "hello"}, headers={"x-correlation-id": UPSTREAM_ID}
    )
    _, data = _frames(resp.text)[-1]
    assert data["correlation_id"] == UPSTREAM_ID


# --------------------------------------------------------------------------- #
# Server-side logging
# --------------------------------------------------------------------------- #
def test_backend_failure_logs_one_warning_with_fields_and_no_message_text(client, route_to_driver, caplog):
    """One WARNING record carries reason, error_type and correlation id, never the user's text."""
    route_to_driver(_FailingDriver())
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        client.post(
            "/api/chat",
            json={"message": PRIVATE_MESSAGE},
            headers={"x-correlation-id": UPSTREAM_ID},
        )
    warnings = [r for r in caplog.records if r.name == "pipeline" and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    record = warnings[0]
    assert record.reason == "chat_backend_failed"
    assert record.error_type == "RuntimeError"
    assert record.correlation_id == UPSTREAM_ID
    assert PRIVATE_MESSAGE not in caplog.text
