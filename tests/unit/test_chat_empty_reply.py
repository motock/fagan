"""Tests for the empty-reply guard in ``app/chat.py`` (cer-01).

Story: a chat turn whose model answers with no text after its tool calls
used to end with an empty reply and no explanation. ``stream_turn`` now
spends one loop iteration on a nudge when the no-tool-call answer is empty or
whitespace-only. If the answer is still empty, or the nudge cannot run because
the turn cap is reached, the turn ends with the fixed reply
``_EMPTY_REPLY_TEXT`` and one WARNING ``chat reply empty`` is logged carrying
only the provider, model tag and turn count.

Driven through the public ``ChatService.execute_turn`` with a scripted fake
driver, the same pattern as ``tests/unit/test_chat_deferred_action_nudge.py``.
"""
from __future__ import annotations

import json
import logging

import pytest

import app.chat as chat_module
from app.chat import _EMPTY_REPLY_TEXT, ChatService

_TOOL_NAME = "list_plans"
_TOOL_RESULT = {"plans": ["SECRET-RESULT-abc"]}
_USER_MESSAGE = "SECRET-QUESTION-xyz"
_REAL_ANSWER = "Here is the plan you asked about."


class _ScriptedDriver:
    """Returns the next scripted reply on each call; fails if the script runs out."""

    model = "test-model"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls = 0

    def complete(self, prompt: str, *, system, model, **kwargs) -> str:
        self.calls += 1
        if not self._replies:
            raise AssertionError("driver called more times than the script allows")
        return self._replies.pop(0)


def _tool_call() -> str:
    return f"[TOOL_CALL]{json.dumps({'name': _TOOL_NAME, 'args': {}})}[/TOOL_CALL]"


@pytest.fixture(autouse=True)
def _stub_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_module, "_execute_tool", lambda name, args, client, base: _TOOL_RESULT)


def _service(driver, max_turns: int = 10) -> ChatService:
    return ChatService(driver=driver, http_client=object(), api_base_url="http://x.test", max_turns=max_turns)


class TestEmptyReplyNudge:
    def test_empty_answer_after_tool_call_is_nudged_and_real_answer_returned(self) -> None:
        driver = _ScriptedDriver([_tool_call(), "", _REAL_ANSWER])

        result = _service(driver).execute_turn(_USER_MESSAGE)

        assert result["reply"] == _REAL_ANSWER
        assert result["turns"] == 3
        assert driver.calls == 3

    def test_whitespace_only_answer_is_treated_as_empty(self) -> None:
        driver = _ScriptedDriver([_tool_call(), " \n\t ", _REAL_ANSWER])

        result = _service(driver).execute_turn(_USER_MESSAGE)

        assert result["reply"] == _REAL_ANSWER
        assert driver.calls == 3

    def test_two_empty_answers_in_a_row_return_the_fixed_reply(self) -> None:
        driver = _ScriptedDriver([_tool_call(), "", ""])

        result = _service(driver).execute_turn(_USER_MESSAGE)

        assert result["reply"] == _EMPTY_REPLY_TEXT
        assert driver.calls == 3

    def test_non_empty_answer_is_returned_unchanged_without_a_nudge(self) -> None:
        driver = _ScriptedDriver([_REAL_ANSWER])

        result = _service(driver).execute_turn(_USER_MESSAGE)

        assert result["reply"] == _REAL_ANSWER
        assert result["turns"] == 1
        assert driver.calls == 1

    def test_empty_answer_on_last_turn_returns_fixed_reply_without_another_call(self) -> None:
        driver = _ScriptedDriver([_tool_call(), ""])

        result = _service(driver, max_turns=2).execute_turn(_USER_MESSAGE)

        assert result["reply"] == _EMPTY_REPLY_TEXT
        assert result["turns"] == 2
        assert driver.calls == 2

    def test_warning_record_carries_no_prompt_or_response_text(self, caplog: pytest.LogCaptureFixture) -> None:
        driver = _ScriptedDriver([_tool_call(), "", ""])

        with caplog.at_level(logging.WARNING, logger="pipeline"):
            _service(driver).execute_turn(_USER_MESSAGE)

        records = [r for r in caplog.records if r.getMessage() == "chat reply empty"]
        assert len(records) == 1
        leaked = [
            text
            for record in records
            for text in [record.getMessage(), *(str(value) for value in vars(record).values())]
            if "SECRET-" in text or _USER_MESSAGE in text or _TOOL_RESULT["plans"][0] in text
        ]
        assert leaked == []
