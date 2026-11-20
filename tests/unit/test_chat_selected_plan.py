"""Tests for cpa-01: the chat tells the planner which plan the person selected.

The dashboard sends ``plan_name`` with each chat turn. A non-empty name is
written into the system prompt so "this plan" means the selected plan, and
the chat endpoints refuse a name the pipeline would not accept as a plan key,
before any model call and without echoing the value.

Every backend is stubbed: the chat driver is a scripted fake and
``ChatService`` is replaced on the endpoints. No test calls a model.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

import app.chat as chat_module
from app import dashboard as d
from app.chat import SYSTEM_PROMPT, ChatService

REPLY = "All done - no tool calls needed."
SELECTED_PLAN_SENTENCE = (
    "The user has selected the plan {name}. When they say \"this plan\" or do not "
    "name a plan, they mean {name}: use it as plan_name, and call get_plan for its "
    "current state before describing it."
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _ScriptedDriver:
    """Fake backend driver: returns scripted responses and records systems."""

    model = "fake-model"

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.systems: list[str] = []

    def complete(self, *, prompt, system, model, cwd=None, **kwargs) -> str:
        self.systems.append(system)
        return self._responses.pop(0)


class _Response:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


class _RecordingHttp:
    """Fake httpx client: answers every call with ``{"ok": True}``."""

    def post(self, url, json=None, **kwargs):
        return _Response({"ok": True})

    def get(self, url, **kwargs):
        return _Response({"ok": True})


class _NeverConstructed:
    def __init__(self, *args, **kwargs):
        raise AssertionError("ChatService must not be built for a refused request")


class _Spy:
    """Stands in for ChatService on the endpoints and records the plan_name it saw."""

    seen: ClassVar[list[str | None]] = []

    def __init__(self, *args, **kwargs):
        pass

    def execute_turn(self, message, *, plan_name=None, history=None, workspace=None, repo_url=None):
        _Spy.seen.append(plan_name)
        return {"reply": "hi", "tool_calls": [], "turns": 1}

    def stream_turn(self, message, *, plan_name=None, history=None, workspace=None, repo_url=None):
        _Spy.seen.append(plan_name)
        yield {"type": "result", "data": {"reply": "hi", "tool_calls": [], "turns": 1}}


def _turn(driver, *, plan_name=None):
    svc = ChatService(driver=driver, http_client=_RecordingHttp(), api_base_url="http://127.0.0.1:8000")
    return svc.execute_turn("what is the status of this plan?", plan_name=plan_name)


# --------------------------------------------------------------------------- #
# System prompt
# --------------------------------------------------------------------------- #
def test_should_name_the_selected_plan_in_the_system_prompt():
    driver = _ScriptedDriver(REPLY)

    _turn(driver, plan_name="security-review-2026-11")

    system = driver.systems[0]
    assert SELECTED_PLAN_SENTENCE.format(name="security-review-2026-11") in system
    assert system.startswith(SYSTEM_PROMPT)


def test_should_keep_the_system_prompt_unchanged_without_a_plan():
    driver = _ScriptedDriver(REPLY, REPLY)

    _turn(driver)
    _turn(driver, plan_name=None)

    assert driver.systems == [SYSTEM_PROMPT, SYSTEM_PROMPT]


def test_should_treat_an_empty_plan_name_as_none():
    driver = _ScriptedDriver(REPLY)

    _turn(driver, plan_name="")

    assert driver.systems == [SYSTEM_PROMPT]


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_should_not_refuse_an_empty_plan_name_on_either_endpoint(monkeypatch, path):
    _Spy.seen = []
    monkeypatch.setattr(chat_module, "ChatService", _Spy)

    res = TestClient(d.app).post(path, json={"message": "hi", "plan_name": ""})

    assert res.status_code == 200
    assert _Spy.seen == [""]


# --------------------------------------------------------------------------- #
# Endpoint validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("plan_name", ["../x", "a/b"])
def test_should_refuse_an_unsafe_plan_name_before_any_model_call(monkeypatch, path, plan_name):
    monkeypatch.setattr(chat_module, "ChatService", _NeverConstructed)

    res = TestClient(d.app).post(path, json={"message": "hi", "plan_name": plan_name})

    assert res.status_code == 400
    assert res.json()["detail"] == "plan_name is not a valid plan name"
    assert plan_name not in res.text


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_should_accept_a_normal_plan_name_on_both_endpoints(monkeypatch, path):
    _Spy.seen = []
    monkeypatch.setattr(chat_module, "ChatService", _Spy)

    res = TestClient(d.app).post(path, json={"message": "hi", "plan_name": "security-review-2026-11"})

    assert res.status_code == 200
    assert _Spy.seen == ["security-review-2026-11"]
