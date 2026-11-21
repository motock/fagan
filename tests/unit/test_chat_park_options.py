"""Tests for opo-04: the chat may propose a parked story's park option; the person runs it.

The overlord prepares options for a parked story (decision kind
``park_options``). The chat can talk about them and propose
``choose_park_option``. Only the person's Run button, calling the dashboard
route as the person, chooses one. So the tool is proposal-only: it is in
``PROPOSAL_ONLY_TOOLS`` and never in the chat's executable ``TOOLS``.

Every model call is scripted and the HTTP client is a fake that fails on any
request. No test calls a model or the network.
"""

from __future__ import annotations

import json

import pytest

from app.chat import SYSTEM_PROMPT, TOOLS, ChatService
from app.chat_actions import ACTION_TOOLS, PROPOSAL_ONLY_TOOLS

REPLY = "Understood."
CHOOSE_ARGS = {"plan_name": "demo", "story_key": "s1", "option_id": "o1"}
PROMPT_SENTENCE = (
    "A parked story may carry prepared options (decision kind park_options, one recommended); "
    "explain them and propose choose_park_option when the user wants one."
)


class _ScriptedDriver:
    model = "fake-model"

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)

    def complete(self, *, prompt, system, model, cwd=None, **kwargs) -> str:
        return self._replies.pop(0)


class _ForbiddenHttp:
    """Fake HTTP client that fails the test on any request."""

    def __init__(self) -> None:
        self.headers: dict = {}

    def post(self, url, **kwargs):
        raise AssertionError(f"no HTTP request expected, got POST {url}")

    def get(self, url, **kwargs):
        raise AssertionError(f"no HTTP request expected, got GET {url}")


def _call(name: str, args: dict) -> str:
    return "[TOOL_CALL]" + json.dumps({"name": name, "args": args}) + "[/TOOL_CALL]"


def _service(driver, http) -> ChatService:
    return ChatService(driver=driver, http_client=http, api_base_url="http://127.0.0.1:8000")


# --------------------------------------------------------------------------- #
# Registries
# --------------------------------------------------------------------------- #
def test_should_register_choose_park_option_as_an_action_tool():
    assert "choose_park_option" in ACTION_TOOLS


def test_should_register_choose_park_option_as_proposal_only():
    assert "choose_park_option" in PROPOSAL_ONLY_TOOLS


def test_should_declare_plan_story_and_option_as_required_string_params():
    params = PROPOSAL_ONLY_TOOLS["choose_park_option"]["params"]

    assert params == {"plan_name": "str", "story_key": "str", "option_id": "str"}


def test_should_not_register_choose_park_option_in_the_executable_tools():
    assert "choose_park_option" not in TOOLS


# --------------------------------------------------------------------------- #
# System prompt
# --------------------------------------------------------------------------- #
def test_should_tell_the_model_to_explain_and_propose_park_options():
    assert PROMPT_SENTENCE in SYSTEM_PROMPT


def test_should_place_the_park_option_sentence_after_the_list_decisions_sentence():
    decisions = "You can surface decisions the overlord has ruled on by calling list_decisions."

    assert SYSTEM_PROMPT.index(decisions) < SYSTEM_PROMPT.index(PROMPT_SENTENCE)


# --------------------------------------------------------------------------- #
# Chat turns
# --------------------------------------------------------------------------- #
def test_should_propose_choose_park_option_without_any_http_call():
    driver = _ScriptedDriver(_call("choose_park_option", CHOOSE_ARGS), REPLY)

    out = _service(driver, _ForbiddenHttp()).execute_turn("choose option 1 for s1")

    assert out["proposed_actions"] == [
        {"id": "pa-1", "name": "choose_park_option", "args": CHOOSE_ARGS}
    ]


@pytest.mark.parametrize("missing", ["plan_name", "story_key", "option_id"])
def test_should_refuse_to_propose_choose_park_option_with_a_missing_param(missing):
    args = {k: v for k, v in CHOOSE_ARGS.items() if k != missing}
    driver = _ScriptedDriver(_call("choose_park_option", args), REPLY)

    out = _service(driver, _ForbiddenHttp()).execute_turn("choose option 1 for s1")

    assert out.get("proposed_actions", []) == []


def test_should_refuse_choose_park_option_with_an_unknown_param():
    args = {**CHOOSE_ARGS, "force": True}
    driver = _ScriptedDriver(_call("choose_park_option", args), REPLY)

    out = _service(driver, _ForbiddenHttp()).execute_turn("choose option 1 for s1")

    assert out.get("proposed_actions", []) == []
