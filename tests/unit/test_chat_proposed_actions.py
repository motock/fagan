"""Tests for cpa-02: the chat proposes every state-changing action; the person runs it.

The model reads agent-written logs, journals and diffs that may carry injected
instructions, so a model-driven chat turn must never execute a state-changing
action. Calling one of the ACTION_TOOLS yields a proposal (``proposed_actions``,
a ``proposed_action`` stream event) that the console shows behind a Run button.
Only the person's click runs it, through the existing HTTP route.

Every model call is scripted and every HTTP client here is a fake that records
(or refuses) requests. No test calls a model or the network.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.chat as chat_module
import app.dashboard as dashboard_module
import app.dashboard_routes_story as routes_story
from app.auth import ORIGIN_CHAT, ORIGIN_HEADER, get_or_create_api_key
from app.chat import SYSTEM_PROMPT, TOOLS, ChatResponse, ChatService, _execute_tool
from app.chat_actions import ACTION_TOOLS, propose

REPLY = "Understood."
NOT_RUN_NOTE = "This action has NOT run. It is shown to the user, who must press Run to carry it out."

#: Valid args for every action tool, so each one binds to its declared params.
VALID_ARGS: dict[str, dict] = {
    "ingest_plan": {"plan_name": "demo"},
    "approve_merge": {"plan_name": "demo", "story_key": "s1"},
    "approve_plan": {"plan_name": "demo"},
    "dispatch_story": {"plan_name": "demo", "story_key": "s1"},
    "interrupt_story": {"plan_name": "demo", "story_key": "s1"},
    "patch_story": {"plan_name": "demo", "story_key": "s1", "fields": {"notes": "x"}},
    "review_story": {"plan_name": "demo", "story_key": "s1"},
    "mark_story_done": {"plan_name": "demo", "story_key": "s1"},
    "checkpoint": {"plan_name": "demo", "story_key": "s1", "step": "a", "summary": "b"},
    "advance_pipeline": {"plan_name": "demo"},
    "advance_all_plans": {},
    "pause_plan": {"plan_name": "demo"},
    "resume_plan": {"plan_name": "demo"},
    "answer_decision": {"plan_name": "demo", "story_key": "s1", "question": "q", "answer": "a"},
}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _ScriptedDriver:
    """Fake backend: returns scripted replies and records every prompt."""

    model = "fake-model"

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, *, prompt, system, model, cwd=None, **kwargs) -> str:
        self.prompts.append(prompt)
        return self._replies.pop(0)


class _ForbiddenHttp:
    """Fake HTTP client that fails the test on any request."""

    def __init__(self) -> None:
        self.headers: dict = {}

    def post(self, url, **kwargs):
        raise AssertionError(f"no HTTP request expected, got POST {url}")

    def get(self, url, **kwargs):
        raise AssertionError(f"no HTTP request expected, got GET {url}")


class _RecordingHttp:
    """Fake HTTP client: records every request and answers ``{"ok": True}``."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []

    def post(self, url, json=None, **kwargs):
        self.requests.append(("POST", url, json))
        return _Response({"ok": True})

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, None))
        return _Response({"ok": True})


class _Response:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


def _call(name: str, args: dict) -> str:
    return "[TOOL_CALL]" + json.dumps({"name": name, "args": args}) + "[/TOOL_CALL]"


def _service(driver, http) -> ChatService:
    return ChatService(driver=driver, http_client=http, api_base_url="http://127.0.0.1:8000")


# --------------------------------------------------------------------------- #
# 1. The action set and the propose() shape
# --------------------------------------------------------------------------- #
def test_action_tools_is_the_frozen_set_of_state_changing_tools():
    assert ACTION_TOOLS == frozenset(VALID_ARGS)


@pytest.mark.parametrize("name", ["list_plans", "decompose", "save_plan", "propose_patch", "set_workspace", "health"])
def test_reads_and_staging_tools_are_not_actions(name):
    assert name not in ACTION_TOOLS


def test_propose_returns_the_proposal_and_a_not_run_tool_result():
    proposal, tool_result = propose("dispatch_story", {"plan_name": "demo", "story_key": "s1"}, 1)

    assert proposal == {"id": "pa-1", "name": "dispatch_story", "args": {"plan_name": "demo", "story_key": "s1"}}
    assert tool_result == {
        "result": {"proposed": True, "id": "pa-1", "status": "awaiting_user", "note": NOT_RUN_NOTE}
    }


# --------------------------------------------------------------------------- #
# 2. Every action is proposed with no HTTP call
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(ACTION_TOOLS))
def test_should_propose_each_action_tool_without_any_http_call(name):
    driver = _ScriptedDriver(_call(name, VALID_ARGS[name]), REPLY)
    out = _service(driver, _ForbiddenHttp()).execute_turn("do it")

    assert [p["name"] for p in out["proposed_actions"]] == [name]
    assert out["proposed_actions"][0]["args"] == VALID_ARGS[name]


def test_should_return_proposed_actions_in_the_result_and_the_response_model():
    driver = _ScriptedDriver(_call("dispatch_story", VALID_ARGS["dispatch_story"]), REPLY)
    out = _service(driver, _ForbiddenHttp()).execute_turn("dispatch s1")

    assert out["proposed_actions"] == [
        {"id": "pa-1", "name": "dispatch_story", "args": VALID_ARGS["dispatch_story"]}
    ]
    response = ChatResponse(**out)
    assert response.proposed_actions == out["proposed_actions"]


def test_should_emit_a_proposed_action_event_in_the_stream():
    driver = _ScriptedDriver(_call("dispatch_story", VALID_ARGS["dispatch_story"]), REPLY)
    events = list(_service(driver, _ForbiddenHttp()).stream_turn("dispatch s1"))

    kinds = [event["type"] for event in events]
    assert "proposed_action" in kinds
    assert "tool_call" not in kinds
    assert "tool_result" not in kinds
    proposed = next(event["data"] for event in events if event["type"] == "proposed_action")
    assert proposed == {"id": "pa-1", "name": "dispatch_story", "args": VALID_ARGS["dispatch_story"]}


def test_should_record_the_proposal_in_tool_calls_as_proposed():
    driver = _ScriptedDriver(_call("dispatch_story", VALID_ARGS["dispatch_story"]), REPLY)
    out = _service(driver, _ForbiddenHttp()).execute_turn("dispatch s1")

    assert out["tool_calls"] == [
        {
            "name": "dispatch_story",
            "args": VALID_ARGS["dispatch_story"],
            "result": {"result": {"proposed": True, "id": "pa-1", "status": "awaiting_user", "note": NOT_RUN_NOTE}},
            "proposed": True,
        }
    ]


def test_should_tell_the_model_the_action_has_not_run():
    driver = _ScriptedDriver(_call("dispatch_story", VALID_ARGS["dispatch_story"]), REPLY)
    _service(driver, _ForbiddenHttp()).execute_turn("dispatch s1")

    second_prompt = driver.prompts[1]
    assert '"status": "awaiting_user"' in second_prompt
    assert NOT_RUN_NOTE in second_prompt


def test_should_number_proposals_within_a_turn():
    driver = _ScriptedDriver(
        _call("dispatch_story", VALID_ARGS["dispatch_story"])
        + _call("interrupt_story", VALID_ARGS["interrupt_story"]),
        REPLY,
    )
    out = _service(driver, _ForbiddenHttp()).execute_turn("dispatch and interrupt")

    assert [p["id"] for p in out["proposed_actions"]] == ["pa-1", "pa-2"]


def test_should_number_proposals_from_one_again_in_a_new_turn():
    first = _ScriptedDriver(_call("dispatch_story", VALID_ARGS["dispatch_story"]), REPLY)
    second = _ScriptedDriver(_call("interrupt_story", VALID_ARGS["interrupt_story"]), REPLY)
    assert _service(first, _ForbiddenHttp()).execute_turn("a")["proposed_actions"][0]["id"] == "pa-1"
    assert _service(second, _ForbiddenHttp()).execute_turn("b")["proposed_actions"][0]["id"] == "pa-1"


# --------------------------------------------------------------------------- #
# 3. Argument errors answer the model and propose nothing
# --------------------------------------------------------------------------- #
def test_should_answer_a_missing_arg_error_without_proposing():
    driver = _ScriptedDriver(_call("dispatch_story", {"plan_name": "demo"}), REPLY)
    out = _service(driver, _ForbiddenHttp()).execute_turn("dispatch it")

    assert out["proposed_actions"] == []
    result = out["tool_calls"][0]["result"]
    assert "error" in result
    assert "Expected parameters for dispatch_story: plan_name, story_key" in result["error"]
    assert "proposed" not in out["tool_calls"][0]


def test_should_answer_an_unknown_arg_error_without_proposing():
    driver = _ScriptedDriver(
        _call("dispatch_story", {"plan_name": "demo", "story_key": "s1", "force": True}), REPLY
    )
    out = _service(driver, _ForbiddenHttp()).execute_turn("dispatch it")

    assert out["proposed_actions"] == []
    assert "error" in out["tool_calls"][0]["result"]


# --------------------------------------------------------------------------- #
# 4. Read tools and save_plan still run directly
# --------------------------------------------------------------------------- #
def test_should_still_execute_read_tools_and_save_plan_directly():
    http = _RecordingHttp()
    driver = _ScriptedDriver(
        _call("list_plans", {}),
        _call("save_plan", {"plan_name": "demo", "plan_json": '{"epics":[]}'}),
        REPLY,
    )
    out = _service(driver, http).execute_turn("list and save")

    assert out["proposed_actions"] == []
    assert ("GET", "/api/plans", None) in http.requests
    assert ("POST", "/api/plans/demo/save", {"plan_json": '{"epics":[]}'}) in http.requests


# --------------------------------------------------------------------------- #
# 5. Defence in depth: _execute_tool never runs an action
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(ACTION_TOOLS))
def test_should_refuse_an_action_tool_in__execute_tool(name):
    result = _execute_tool(name, VALID_ARGS[name], _ForbiddenHttp(), "http://test")

    assert result == {"error": f"{name} must be confirmed by the user"}


def test_should_not_call_the_action_execute_callable_from_execute_tool(monkeypatch):
    calls: list[str] = []
    monkeypatch.setitem(
        TOOLS["dispatch_story"], "execute", lambda *a, **k: calls.append("ran")
    )
    _execute_tool("dispatch_story", VALID_ARGS["dispatch_story"], _ForbiddenHttp(), "http://test")

    assert calls == []


def test_approve_plan_execute_refuses_to_run():
    result = TOOLS["approve_plan"]["execute"](_ForbiddenHttp(), "http://test", plan_name="demo")

    assert result == {"error": "approve_plan must be confirmed by the user"}


# --------------------------------------------------------------------------- #
# 6. approve_merge: registered for proposal, refused from the chat origin
# --------------------------------------------------------------------------- #
APPROVE_MERGE_PATH = "/api/plans/demo/stories/s1/approve_merge"


class _ApproveSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def approve_merge(self, plan_name, story_key):
        self.calls.append((plan_name, story_key))
        return {"ok": True}


@pytest.fixture()
def approve_spy(monkeypatch):
    spy = _ApproveSpy()
    monkeypatch.setattr(routes_story, "_service", spy)
    return spy


def _auth_headers(**extra: str) -> dict:
    headers = {"X-Pipeline-Api-Key": get_or_create_api_key()}
    headers.update(extra)
    return headers


def test_should_refuse_approve_merge_from_the_chat_origin(approve_spy):
    response = TestClient(dashboard_module.app).post(
        APPROVE_MERGE_PATH, headers=_auth_headers(**{ORIGIN_HEADER: ORIGIN_CHAT})
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "origin not permitted"}
    assert approve_spy.calls == []


def test_should_allow_approve_merge_without_the_chat_origin(approve_spy):
    response = TestClient(dashboard_module.app).post(APPROVE_MERGE_PATH, headers=_auth_headers())

    assert response.status_code == 200
    assert approve_spy.calls == [("demo", "s1")]


def test_approve_merge_tool_posts_to_the_approve_merge_route_when_executed_directly():
    http = _RecordingHttp()
    TOOLS["approve_merge"]["execute"](http, "http://test", plan_name="demo", story_key="s1")

    assert http.requests == [("POST", "/api/plans/demo/stories/s1/approve_merge", None)]


# --------------------------------------------------------------------------- #
# 7. System prompt
# --------------------------------------------------------------------------- #
def test_should_list_approve_merge_and_approve_plan_in_the_system_prompt():
    sentence = chat_module._available_tools_sentence()

    assert "approve_merge (" in sentence
    assert "approve_plan (" in sentence
    assert "approve_merge" in SYSTEM_PROMPT
    assert "approve_plan" in SYSTEM_PROMPT


def test_prompt_says_calling_an_action_does_not_run_it():
    assert "Calling one of these does NOT run it" in SYSTEM_PROMPT
    assert "never say an action has run unless a later message from the user confirms it" in SYSTEM_PROMPT
