"""Acceptance fixture: ChatService's internal loopback HTTP calls must carry
the caller's dashboard API key, so tool calls (decompose, health, etc.) do
not 401 against the real `_enforce_api_key` middleware in app/dashboard.py.

Exercises the REAL app (app.dashboard.app, including its real auth
middleware) via a real fastapi.testclient.TestClient rather than calling a
TOOLS['...']['execute'] callable directly, so the fixture fails if the fix
is wired at the wrong layer (e.g. chat_endpoint reads the header but never
threads it into ChatService's HTTP client, or ChatService stores the key
but never applies it to an already-injected http_client).

This module is added to tests/unit/conftest.py's `_authenticated_test_client`
fixture's `self_managed` set (alongside test_dashboard_index_serves_key.py)
because it deliberately constructs TestClients with no key, a wrong key, and
the real key to exercise all three paths itself - the autouse fixture's
"always attach the real key" behavior would make the negative cases
unrunnable (a headerless request would come back 200 instead of 401,
observed live while authoring this fixture).
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.auth import get_or_create_api_key
from app.chat import ChatService


class _ScriptedDriver:
    """Emits one TOOL_CALL turn (a bare JSON object, matching the real wire
    format SYSTEM_PROMPT instructs), then a plain final reply."""

    model = "scripted"

    def __init__(self, tool_call: dict) -> None:
        self._tool_call = tool_call
        self._used = False

    def complete(self, *, prompt, system, model, cwd):
        if not self._used:
            self._used = True
            return "[TOOL_CALL] " + json.dumps(self._tool_call) + " [/TOOL_CALL]"
        return "done"


def _real_app_client() -> TestClient:
    from app.dashboard import app

    return TestClient(app)


def test_health_tool_call_succeeds_with_the_real_api_key():
    driver = _ScriptedDriver({"name": "health", "args": {}})
    svc = ChatService(driver=driver, http_client=_real_app_client(), api_key=get_or_create_api_key())
    result = svc.execute_turn("are you up?")
    tool_result = result["tool_calls"][0]["result"]
    assert "detail" not in tool_result.get("result", {})
    assert tool_result["result"]["ok"] is True


def test_health_tool_call_is_rejected_with_a_wrong_api_key():
    driver = _ScriptedDriver({"name": "health", "args": {}})
    svc = ChatService(driver=driver, http_client=_real_app_client(), api_key="not-the-real-key")
    result = svc.execute_turn("are you up?")
    tool_result = result["tool_calls"][0]["result"]
    assert tool_result["result"] == {"detail": "unauthorized"}


def test_health_tool_call_is_rejected_with_no_api_key():
    driver = _ScriptedDriver({"name": "health", "args": {}})
    svc = ChatService(driver=driver, http_client=_real_app_client())
    result = svc.execute_turn("are you up?")
    tool_result = result["tool_calls"][0]["result"]
    assert tool_result["result"] == {"detail": "unauthorized"}
