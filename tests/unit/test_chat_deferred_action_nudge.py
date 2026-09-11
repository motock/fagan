"""Tests for the deferred-action nudge in ``app/chat.py``.

Story: stop ``ChatService.execute_turn`` from ending the turn when the model
narrates an intention to act ("Let me check on that.") but emits no tool call.

Contract under test (all of it lives in ``app/chat.py``):

1. A new module-level detector ``_looks_like_deferred_action(response: str)``
   placed immediately after the existing ``_looks_like_unparsed_tool_call``.
   It returns True only for narration-style promises to act (matched
   case-insensitively against a narrow, module-level tuple of anchored
   phrases) and False for ordinary informative replies.
2. The existing ``if not parsed:`` branch in ``execute_turn`` is extended:
   when the reply is NOT an unparsed ``[TOOL_CALL]`` marker but IS a
   deferred-action narration AND the nudge budget is not exhausted, the loop
   must set ``current_prompt`` to a nudge that restates the required shape
   ``[TOOL_CALL]{"name": "<tool>", "args": {...}}[/TOOL_CALL]`` and
   ``continue`` instead of returning the narration as the final reply.
3. Budget: module-level ``_DEFERRED_NUDGE_CAP == 2``; at most that many
   nudges per ``execute_turn`` call, after which the loop falls through and
   returns exactly as before. The existing ``max_turns`` cap is unchanged.
4. Return shape unchanged: ``{"reply": ..., "tool_calls": ..., "turns": ...}``
   with no new keys — in particular the strict-equality case pinned by
   ``tests/unit/test_chat_agent_loop.py::TestExecuteTurnNegative::
   test_unparseable_output_returned_as_is`` must keep passing untouched.

TDD RED state: ``_looks_like_deferred_action`` and ``_DEFERRED_NUDGE_CAP``
do not exist in ``app.chat`` yet, so this module fails at collection with an
ImportError until the implementation lands. That is the expected failure
reason, not a bug in these tests.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import app.chat as chat_module
from app.chat import (
    _DEFERRED_NUDGE_CAP,
    TOOLS,
    ChatService,
    _looks_like_deferred_action,
    _looks_like_unparsed_tool_call,
)


# --------------------------------------------------------------------------- #
# Fakes (same patterns as tests/unit/test_chat_agent_loop.py)
# --------------------------------------------------------------------------- #
class _ScriptedDriver:
    """Returns the next reply in ``replies`` on each ``complete`` call.

    The last reply is reused if the loop runs past the script length. All
    calls are recorded.
    """

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, prompt: str, *, system, model, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system, "model": model})
        if self.replies:
            return self.replies.pop(0)
        # Fall back to the last prompt if the script is exhausted.
        return self.calls[-1]["prompt"]


class _AlwaysNarrateDriver:
    """A driver that returns the same narration reply for every call.

    Used for the budget tests. ``_ScriptedDriver``'s script-exhaustion
    fallback echoes the *previous prompt* back, and the nudge prompt contains
    a literal ``[TOOL_CALL]`` shape — that echo would trip the *unparsed
    marker* branch instead of staying a pure narration, so it is unsuitable
    for "narrates every single time" scenarios.
    """

    def __init__(self, reply: str = "Let me check.") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def complete(self, prompt: str, *, system, model, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system, "model": model})
        return self.reply


def _tool_call(name: str, args: dict) -> str:
    return f"[TOOL_CALL]{json.dumps({'name': name, 'args': args})}[/TOOL_CALL]"


def _service(driver, max_turns: int | None = None) -> ChatService:
    if max_turns is None:
        return ChatService(driver=driver, http_client=object(), api_base_url="http://x.test")
    return ChatService(
        driver=driver, http_client=object(), api_base_url="http://x.test", max_turns=max_turns
    )


def _module_source() -> str:
    return Path(chat_module.__file__).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# _looks_like_deferred_action — positive
# --------------------------------------------------------------------------- #
class TestDeferredActionDetectorPositive:
    def test_let_me_check_narration_matches(self) -> None:
        assert _looks_like_deferred_action("Let me check on that for you.") is True

    def test_ill_look_narration_matches(self) -> None:
        assert _looks_like_deferred_action("I'll look that up.") is True

    def test_matching_is_case_insensitive(self) -> None:
        assert _looks_like_deferred_action("LET ME CHECK") is True
        assert _looks_like_deferred_action("I'LL LOOK THAT UP.") is True

    def test_detector_accepts_the_documented_keyword_name(self) -> None:
        # The signature is ``_looks_like_deferred_action(response: str)``.
        assert chat_module._looks_like_deferred_action(response="Let me check.") is True

    @pytest.mark.parametrize(
        "reply",
        [
            "let me check",
            "let me look",
            "let me find",
            "i'll check",
            "i will check",
            "i'll look",
            "i will look",
            "checking now",
            "one moment",
            "hold on",
            "let me pull",
            "let me fetch",
            "Let me check on that for you.",
            "One moment, please.",
            "HOLD ON",
            "Checking now.",
        ],
    )
    def test_documented_deferred_phrases_match(self, reply: str) -> None:
        assert _looks_like_deferred_action(reply) is True


# --------------------------------------------------------------------------- #
# _looks_like_deferred_action — negative / boundary
# --------------------------------------------------------------------------- #
class TestDeferredActionDetectorNegative:
    def test_exact_string_from_existing_strict_equality_test_is_false(self) -> None:
        # This is the exact reply used by the existing strict dict-equality
        # test (test_unparseable_output_returned_as_is); the detector MUST
        # reject it so that test stays green untouched.
        assert _looks_like_deferred_action("just a plain reply, no tags") is False

    def test_empty_string_is_false(self) -> None:
        assert _looks_like_deferred_action("") is False

    def test_whitespace_only_string_is_false(self) -> None:
        assert _looks_like_deferred_action("   ") is False

    @pytest.mark.parametrize(
        "reply",
        [
            "The pipeline run PIPE-1 finished with 3 stages passing.",
            "Here is a summary of the run: all checks passed.",
            "The answer is 42.",
            "PIPE-1 failed because the schema mismatched; fix the config and rerun.",
            "The check ran and passed.",  # contains "check" but is not a promise
            "I checked earlier; it was fine.",  # past tense, not a promise
            "You can check the dashboard for the status.",  # informative, not a promise
        ],
    )
    def test_ordinary_informative_replies_do_not_match(self, reply: str) -> None:
        assert _looks_like_deferred_action(reply) is False

    def test_detector_returns_real_bools_not_truthy_values(self) -> None:
        assert isinstance(_looks_like_deferred_action("Let me check."), bool)
        assert isinstance(_looks_like_deferred_action("plain answer"), bool)


# --------------------------------------------------------------------------- #
# Module structure: placement, existing helper survival, cap constant
# --------------------------------------------------------------------------- #
class TestModuleStructure:
    def test_nudge_cap_constant_is_two(self) -> None:
        assert isinstance(chat_module._DEFERRED_NUDGE_CAP, int)
        assert _DEFERRED_NUDGE_CAP == 2

    def test_detector_is_module_level_and_immediately_after_unparsed_helper(self) -> None:
        top_level_defs = re.findall(r"^def (\w+)", _module_source(), re.MULTILINE)
        assert "_looks_like_unparsed_tool_call" in top_level_defs
        assert "_looks_like_deferred_action" in top_level_defs
        index = top_level_defs.index("_looks_like_unparsed_tool_call")
        assert top_level_defs[index + 1] == "_looks_like_deferred_action", (
            "_looks_like_deferred_action must be defined immediately after "
            "_looks_like_unparsed_tool_call among module-level functions"
        )

    def test_existing_unparsed_marker_helper_still_exists(self) -> None:
        # The sibling branch must not be removed or renamed by this story.
        assert callable(chat_module._looks_like_unparsed_tool_call)

    def test_plain_narration_is_not_treated_as_an_unparsed_marker(self) -> None:
        # Precondition for the new branch to be reachable: a narration reply
        # must fall through the existing marker check (no [TOOL_CALL] tag).
        assert _looks_like_unparsed_tool_call("Let me check on that.") is False


# --------------------------------------------------------------------------- #
# execute_turn — the nudge keeps the turn alive
# --------------------------------------------------------------------------- #
class TestExecuteTurnNudgesInsteadOfEnding:
    def test_narration_then_tool_call_then_answer(self, monkeypatch) -> None:
        def _execute(http_client, api_base_url, **kwargs):
            return {"pong": True}

        monkeypatch.setitem(TOOLS, "ping", {"execute": _execute})
        driver = _ScriptedDriver(
            replies=[
                "Let me check on that.",
                _tool_call("ping", {"host": "db"}),
                "here is the answer",
            ]
        )
        out = _service(driver).execute_turn("is PIPE-1 ok?")

        # The narration did NOT end the turn: the tool call was still made.
        assert out["reply"] == "here is the answer"
        assert out["tool_calls"] == [
            {"name": "ping", "args": {"host": "db"}, "result": {"result": {"pong": True}}}
        ]
        assert out["turns"] == 3
        assert set(out) == {"reply", "tool_calls", "turns"}
        assert len(driver.calls) == 3

    def test_nudge_prompt_contains_required_tool_call_shape(self, monkeypatch) -> None:
        def _execute(http_client, api_base_url, **kwargs):
            return {}

        monkeypatch.setitem(TOOLS, "ping", {"execute": _execute})
        driver = _ScriptedDriver(
            replies=["Let me check on that.", _tool_call("ping", {}), "done"]
        )
        _service(driver).execute_turn("status?")

        # The prompt handed to the driver on the turn AFTER the narration is
        # the nudge, and it restates the required tool-call shape.
        nudge = driver.calls[1]["prompt"]
        assert "[TOOL_CALL]" in nudge
        assert '[TOOL_CALL]{"name": "<tool>", "args": {...}}[/TOOL_CALL]' in nudge
        assert re.search(r"tool[-_ ]?call", nudge, re.IGNORECASE)
        # It is a new nudge prompt, not the bare message re-sent.
        assert nudge != "status?"
        # The first call still received the plain message.
        assert driver.calls[0]["prompt"] == "status?"

    def test_nudge_fires_mid_conversation_after_tool_results(self, monkeypatch) -> None:
        def _execute(http_client, api_base_url, **kwargs):
            return {"n": 1}

        monkeypatch.setitem(TOOLS, "ping", {"execute": _execute})
        driver = _ScriptedDriver(
            replies=[
                _tool_call("ping", {}),
                "Let me check the logs next.",
                _tool_call("ping", {"n": 2}),
                "all done",
            ]
        )
        out = _service(driver).execute_turn("go")

        assert out["reply"] == "all done"
        assert out["turns"] == 4
        assert len(out["tool_calls"]) == 2
        assert len(driver.calls) == 4
        # Turn 3's prompt is the nudge for the post-tool narration.
        assert (
            '[TOOL_CALL]{"name": "<tool>", "args": {...}}[/TOOL_CALL]'
            in driver.calls[2]["prompt"]
        )
        # Turn 4's prompt still carries the second TOOL_RESULT feedback block.
        assert "[TOOL_RESULT name=ping]" in driver.calls[3]["prompt"]

    def test_narration_alongside_a_real_tool_call_is_dispatched_not_nudged(
        self, monkeypatch
    ) -> None:
        def _execute(http_client, api_base_url, **kwargs):
            return {"ok": True}

        monkeypatch.setitem(TOOLS, "ping", {"execute": _execute})
        driver = _ScriptedDriver(
            replies=[
                "Let me check that for you. " + _tool_call("ping", {}),
                "done",
            ]
        )
        out = _service(driver).execute_turn("hello")

        # A parseable tool call takes the normal dispatch path; the detector
        # must not hijack it.
        assert out["reply"] == "done"
        assert out["turns"] == 2
        assert len(out["tool_calls"]) == 1
        assert out["tool_calls"][0]["name"] == "ping"


# --------------------------------------------------------------------------- #
# execute_turn — the nudge budget
# --------------------------------------------------------------------------- #
class TestDeferredNudgeBudget:
    def test_narration_only_driver_terminates_within_budget(self) -> None:
        driver = _AlwaysNarrateDriver("Let me check.")
        out = _service(driver, max_turns=10).execute_turn("hello")

        cap = chat_module._DEFERRED_NUDGE_CAP
        # Exactly cap nudges were issued, then the loop fell through and
        # returned the narration exactly as the old code would have.
        assert len(driver.calls) == cap + 1
        assert out == {"reply": "Let me check.", "tool_calls": [], "turns": cap + 1}
        assert set(out) == {"reply", "tool_calls", "turns"}

    def test_max_turns_cap_still_applies_when_narrating(self) -> None:
        driver = _AlwaysNarrateDriver("Let me check.")
        out = _service(driver, max_turns=1).execute_turn("hello")

        # The pre-existing max_turns cap is unchanged and still terminates.
        assert out["turns"] == 1
        assert "(turn cap reached)" in out["reply"]
        assert out["reply"].startswith("Let me check.")
        assert len(driver.calls) == 1
        assert set(out) == {"reply", "tool_calls", "turns"}


# --------------------------------------------------------------------------- #
# execute_turn — existing behaviour preserved
# --------------------------------------------------------------------------- #
class TestExistingBehaviourPreserved:
    def test_plain_non_deferred_reply_returned_exactly(self) -> None:
        driver = _ScriptedDriver(replies=["The answer is 42."])
        out = _service(driver).execute_turn("hello")
        assert out == {"reply": "The answer is 42.", "tool_calls": [], "turns": 1}

    def test_exact_string_from_existing_strict_equality_test_unchanged(self) -> None:
        driver = _ScriptedDriver(replies=["just a plain reply, no tags"])
        out = _service(driver).execute_turn("hello")
        assert out == {"reply": "just a plain reply, no tags", "tool_calls": [], "turns": 1}

    def test_malformed_marker_still_handled_by_the_existing_branch(self) -> None:
        # A literal [TOOL_CALL] marker that fails to parse must keep taking
        # the pre-existing re-prompt branch, not the deferred nudge.
        driver = _ScriptedDriver(
            replies=["[TOOL_CALL]{not valid json}[/TOOL_CALL]", "done"]
        )
        out = _service(driver).execute_turn("hello")
        assert out == {"reply": "done", "tool_calls": [], "turns": 2}
        assert driver.calls[1]["prompt"] != driver.calls[0]["prompt"]