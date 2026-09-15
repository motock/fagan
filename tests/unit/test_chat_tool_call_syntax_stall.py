"""Behaviour tests for the attribute-style ``[TOOL_CALL name=...]`` stall.

Story: ``_looks_like_unparsed_tool_call`` (``app/chat.py``) is the stall guard
that stops the chat loop from silently returning a malformed tool-call attempt
as the final answer.  Its first statement gates the whole check on the LITERAL
substring ``"[TOOL_CALL]"``::

    if "[TOOL_CALL]" not in response:
        return False

A model can emit the same intent in attribute style -- ``[TOOL_CALL name=health]``
followed by ``[/TOOL_CALL]`` -- which contains no literal ``[TOOL_CALL]`` opener.
For that text ``_parse_tool_calls`` returns ``[]`` (its regex requires the exact
``]`` right after ``TOOL_CALL``) and the guard returns ``False``, so no nudge
fires, the loop falls straight through to ``yield`` the reply, and the user is
shown a promise to act followed by dead tool-call text.  Nothing executes.

This file pins the three required changes:

* A -- broaden the detector to a ``[TOOL_CALL``-family opener (canonical or
  attribute-style) that yields zero parseable calls.
* B -- append four re-run phrases to ``_DEFERRED_NUDGE_PHRASES`` (append-only).
* C -- append one concrete canonical-call example to ``_SYSTEM_PROMPT_PREFIX``.

House style follows ``tests/unit/test_chat_stream_turn.py``: a scripted fake
driver records every prompt and drives the REAL ``stream_turn`` /
``execute_turn`` loop with only the model boundary faked -- no live LLM, no
live HTTP.

TDD RED state: on the pristine tree the attribute-style cases fail because the
guard still gates on the literal marker (``assert ... is True`` fails) and the
prompt/phrase assertions fail because the new text is absent.  The
``[TOOL_RESULT ...]``, prose, empty-string and canonical-call cases are
regression guards that already pass and MUST keep passing.  No existing test
file is modified.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

import app.chat as chat_module
from app.chat import ChatService

# --------------------------------------------------------------------------- #
# Fixtures under test
# --------------------------------------------------------------------------- #
# The reported live stall: attribute-style opener, no canonical "[TOOL_CALL]".
_ATTRIBUTE_STYLE_STALL = "[TOOL_CALL name=health]\n[/TOOL_CALL]"
# Same shape carrying a JSON-ish body but still no canonical opener.
_ATTRIBUTE_STYLE_WITH_BODY = '[TOOL_CALL name="health"]{"args": {}}[/TOOL_CALL]'
# A result echo is not a call.
_RESULT_ECHO = '[TOOL_RESULT name=decompose]{"ok": 1}[/TOOL_RESULT]'
# Malformed canonical call -- the shape the existing fixture already covers.
_MALFORMED_CANONICAL = "[TOOL_CALL]{not json}[/TOOL_CALL]"
_PLAIN_REPLY = "Sure, here is some info about plans."

_TOOL_NAME = "list_plans"
_TOOL_RESULT_PAYLOAD = {"plans": ["alpha", "beta"]}

# The twelve pre-existing entries, byte-identical and in their current order.
_ORIGINAL_PHRASES = (
    "let me check", "let me look", "let me find", "i'll check", "i will check",
    "i'll look", "i will look", "checking now", "one moment", "hold on",
    "let me pull", "let me fetch",
)
# The four entries this story appends, in order, at the END of the tuple.
_NEW_PHRASES = ("i'll re-run", "i will re-run", "i'll rerun", "re-running")


# --------------------------------------------------------------------------- #
# Fakes (same patterns as tests/unit/test_chat_stream_turn.py)
# --------------------------------------------------------------------------- #
class _FakeResponse:
    """Minimal stand-in for an ``httpx.Response`` exposing ``.json()``."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Minimal stand-in for the injected ``http_client``."""

    def __init__(self, payload: dict | None = None) -> None:
        self.base_url = "http://x.test"
        self._payload = _TOOL_RESULT_PAYLOAD if payload is None else payload
        self.get_calls: list[str] = []
        self.post_calls: list[str] = []

    def get(self, url, *args, **kwargs) -> _FakeResponse:
        self.get_calls.append(url)
        return _FakeResponse(self._payload)

    def post(self, url, *args, **kwargs) -> _FakeResponse:
        self.post_calls.append(url)
        return _FakeResponse(self._payload)


class _ScriptedDriver:
    """Returns the next reply in ``replies`` on each ``complete`` call.

    All calls are recorded, including the ``cwd`` keyword the production loop
    must keep passing.
    """

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, prompt: str, *, system=None, model=None, cwd=None, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system, "model": model, "cwd": cwd})
        if self.replies:
            return self.replies.pop(0)
        return ""


class _RepeatingDriver:
    """Returns the SAME reply on every ``complete`` call, forever.

    Used for the boundary case: a model that never stops emitting the
    attribute-style stall must still terminate at ``max_turns``.
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def complete(self, prompt: str, *, system=None, model=None, cwd=None, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system, "model": model, "cwd": cwd})
        return self.reply


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _tool_call(name: str, args: dict) -> str:
    return "[TOOL_CALL]" + json.dumps({"name": name, "args": args}) + "[/TOOL_CALL]"


def _service(driver, max_turns: int = 10, client: _FakeClient | None = None) -> ChatService:
    """Build a ChatService with an injected driver and a deterministic cap.

    ``max_turns`` is ALWAYS passed explicitly so the ambient
    ``PIPELINE_CHAT_MAX_TURNS`` environment variable cannot perturb a test.
    """
    return ChatService(
        driver=driver,
        http_client=client if client is not None else _FakeClient(),
        api_base_url="http://x.test",
        max_turns=max_turns,
    )


def _module_source() -> str:
    return Path(chat_module.__file__).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _stub_tool_url_resolver(monkeypatch):
    """Pin the tool-URL resolver so the fake client's ``get`` is deterministic."""
    if hasattr(chat_module, "_resolve_tool_url"):
        monkeypatch.setattr(
            chat_module,
            "_resolve_tool_url",
            lambda http_client, api_base_url, path, *args, **kwargs: f"{api_base_url}{path}",
        )


# --------------------------------------------------------------------------- #
# 1-5: the detector itself
# --------------------------------------------------------------------------- #
class TestUnparsedToolCallDetector:
    def test_attribute_style_opener_is_flagged_as_a_stall(self) -> None:
        assert chat_module._looks_like_unparsed_tool_call(_ATTRIBUTE_STYLE_STALL) is True

    def test_attribute_style_with_body_is_flagged_as_a_stall(self) -> None:
        assert chat_module._looks_like_unparsed_tool_call(_ATTRIBUTE_STYLE_WITH_BODY) is True

    def test_malformed_canonical_call_is_still_flagged_as_a_stall(self) -> None:
        # Unchanged behaviour: the canonical opener with unparseable body.
        assert chat_module._looks_like_unparsed_tool_call(_MALFORMED_CANONICAL) is True

    def test_result_echo_is_still_not_a_stall(self) -> None:
        # Regression guard: a [TOOL_RESULT ...] echo is not a call.
        assert chat_module._looks_like_unparsed_tool_call(_RESULT_ECHO) is False

    def test_plain_reply_and_empty_are_still_not_stalls(self) -> None:
        assert chat_module._looks_like_unparsed_tool_call(_PLAIN_REPLY) is False
        assert chat_module._looks_like_unparsed_tool_call("") is False

    def test_parseable_call_is_still_not_a_stall(self) -> None:
        # The happy path must not be hijacked into a nudge.
        canonical = _tool_call(_TOOL_NAME, {})
        assert chat_module._looks_like_unparsed_tool_call(canonical) is False

    def test_detector_keeps_its_name_and_signature(self) -> None:
        # No rename, one positional parameter named ``response``.
        assert callable(chat_module._looks_like_unparsed_tool_call)
        params = list(inspect.signature(chat_module._looks_like_unparsed_tool_call).parameters.values())
        assert [p.name for p in params] == ["response"]
        assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD

    def test_detector_returns_a_real_bool(self) -> None:
        # A truthy non-bool (e.g. a match object) is not acceptable.
        for text in (_ATTRIBUTE_STYLE_STALL, _PLAIN_REPLY, ""):
            result = chat_module._looks_like_unparsed_tool_call(text)
            assert isinstance(result, bool), f"expected bool, got {type(result)!r} for {text!r}"

    def test_detector_docstring_describes_the_broadened_rule(self) -> None:
        # The docstring's opening rule must no longer claim the check is gated
        # on the literal marker only; it must describe the broadened opener.
        doc = (chat_module._looks_like_unparsed_tool_call.__doc__ or "").lower()
        broadened = any(
            word in doc
            for word in ("attribute", "family", "opener", "style", "variant", "name=", "prefix")
        )
        old_claim = "literal ``[tool_call]`` marker appears" in doc
        assert broadened or not old_claim, (
            "docstring must describe the broadened TOOL_CALL-family opener rule"
        )

    def test_parse_tool_calls_is_unchanged(self) -> None:
        # The fix belongs in the detector, not in a loosened parser: the
        # attribute-style forms must still yield zero parseable calls.
        assert chat_module._parse_tool_calls(_ATTRIBUTE_STYLE_STALL) == []
        assert chat_module._parse_tool_calls(_ATTRIBUTE_STYLE_WITH_BODY) == []
        assert chat_module._parse_tool_calls(_MALFORMED_CANONICAL) == []
        assert chat_module._parse_tool_calls(_tool_call(_TOOL_NAME, {})) == [
            {"name": _TOOL_NAME, "args": {}}
        ]

    def test_no_new_top_level_function_was_inserted_after_the_detector(self) -> None:
        # An existing test pins that _looks_like_deferred_action is the next
        # top-level def after _looks_like_unparsed_tool_call.
        defs = re.findall(r"^def (\w+)", _module_source(), re.MULTILINE)
        assert "_looks_like_unparsed_tool_call" in defs
        idx = defs.index("_looks_like_unparsed_tool_call")
        assert defs[idx + 1] == "_looks_like_deferred_action"


# --------------------------------------------------------------------------- #
# 6-8: the loop, observed through execute_turn
# --------------------------------------------------------------------------- #
class TestLoopRecoversFromTheAttributeStyleStall:
    def test_loop_nudges_instead_of_returning_the_attribute_style_stall(self) -> None:
        driver = _ScriptedDriver([_ATTRIBUTE_STYLE_STALL, _PLAIN_REPLY])
        result = _service(driver).execute_turn("hi")

        # The stall costs a turn and a re-prompt rather than ending the turn.
        assert len(driver.calls) > 1
        assert "could not be parsed as a tool call" in driver.calls[1]["prompt"]
        # The user sees the real reply, never the dead tool-call text.
        assert result["reply"] == _PLAIN_REPLY
        assert _ATTRIBUTE_STYLE_STALL not in result["reply"]
        assert result["tool_calls"] == []

    def test_tool_actually_executes_after_the_attribute_style_recovery(self) -> None:
        client = _FakeClient()
        driver = _ScriptedDriver(
            [_ATTRIBUTE_STYLE_STALL, _tool_call(_TOOL_NAME, {}), "here is the summary."]
        )
        result = _service(driver, client=client).execute_turn("hi")

        # The behavioural core: the work still happens.
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["name"] == _TOOL_NAME
        assert result["tool_calls"][0]["args"] == {}
        assert len(client.get_calls) == 1
        assert result["reply"] == "here is the summary."

    def test_attribute_style_only_driver_terminates_within_budget(self) -> None:
        driver = _RepeatingDriver(_ATTRIBUTE_STYLE_STALL)
        result = _service(driver, max_turns=3).execute_turn("hi")

        # No infinite nudge loop: it returns, at the cap, with the cap notice.
        assert result is not None
        assert result["turns"] == 3
        assert len(driver.calls) == 3
        assert result["reply"].endswith("(turn cap reached)")

    def test_first_prompt_is_still_the_bare_message(self) -> None:
        # Out of scope, must not regress: a falsy history keeps the FIRST
        # call's prompt exactly the message (later prompts are cumulative).
        driver = _ScriptedDriver([_ATTRIBUTE_STYLE_STALL, _PLAIN_REPLY])
        _service(driver).execute_turn("hi")

        assert driver.calls[0]["prompt"] == "hi"

    def test_nudge_texts_are_unchanged(self) -> None:
        # Neither nudge's wording may change in this story.
        driver = _ScriptedDriver([_ATTRIBUTE_STYLE_STALL, _PLAIN_REPLY])
        _service(driver).execute_turn("hi")
        assert "could not be parsed as a tool call" in driver.calls[1]["prompt"]

        deferred = _ScriptedDriver(["I'll re-run the checks now.", _PLAIN_REPLY])
        _service(deferred).execute_turn("hi")
        assert len(deferred.calls) > 1
        assert "Stop narrating - emit the tool call now" in deferred.calls[1]["prompt"]


# --------------------------------------------------------------------------- #
# 9-10: the deferred-action detector
# --------------------------------------------------------------------------- #
class TestDeferredActionDetector:
    def test_deferred_detector_matches_the_re_run_narration(self) -> None:
        assert chat_module._looks_like_deferred_action("I'll re-run the checks now.") is True
        # Case-insensitive variant.
        assert chat_module._looks_like_deferred_action("RE-RUNNING the checks") is True

    def test_deferred_detector_still_rejects_ordinary_replies(self) -> None:
        assert chat_module._looks_like_deferred_action("The answer is 42.") is False
        assert chat_module._looks_like_deferred_action("I checked earlier; it was fine.") is False
        assert chat_module._looks_like_deferred_action("") is False

    def test_deferred_phrase_list_is_append_only(self) -> None:
        phrases = chat_module._DEFERRED_NUDGE_PHRASES
        # The twelve existing entries stay byte-identical and in order.
        assert tuple(phrases[: len(_ORIGINAL_PHRASES)]) == _ORIGINAL_PHRASES
        # The four new entries are present, after the originals, in order.
        last_original = phrases.index(_ORIGINAL_PHRASES[-1])
        positions = []
        for phrase in _NEW_PHRASES:
            assert phrase in phrases, f"missing appended phrase {phrase!r}"
            positions.append(phrases.index(phrase))
        assert all(pos > last_original for pos in positions)
        assert positions == sorted(positions)

    def test_deferred_nudge_cap_is_unchanged(self) -> None:
        assert chat_module._DEFERRED_NUDGE_CAP == 2


# --------------------------------------------------------------------------- #
# 11-12 (+ assembly): the system prompt
# --------------------------------------------------------------------------- #
class TestSystemPromptCarriesTheCanonicalExample:
    def test_system_prompt_carries_a_concrete_canonical_call_example(self) -> None:
        assert '[TOOL_CALL]{"name":' in chat_module.SYSTEM_PROMPT

    def test_system_prompt_names_the_invalid_attribute_form(self) -> None:
        assert "[TOOL_CALL name=" in chat_module.SYSTEM_PROMPT

    def test_system_prompt_prefix_keeps_its_existing_sentences(self) -> None:
        prefix = chat_module._SYSTEM_PROMPT_PREFIX
        # Pure append: the pre-existing sentences survive verbatim.
        assert "You are a helpful assistant." in prefix
        assert "When you need to call a tool, emit a JSON object with keys name and args" in prefix
        assert "This session deliberately has no native Claude Code tools" in prefix
        # The new example lives INSIDE the prefix, after the isolation sentence.
        assert "[TOOL_CALL name=" in prefix
        assert '[TOOL_CALL]{"name":' in prefix
        assert prefix.index("This session deliberately has no native Claude Code tools") < prefix.index(
            "[TOOL_CALL name="
        )

    def test_system_prompt_assembly_is_unchanged_and_constraints_hold(self) -> None:
        prompt = chat_module.SYSTEM_PROMPT
        assert prompt == (
            chat_module._SYSTEM_PROMPT_PREFIX
            + chat_module._available_tools_sentence()
            + chat_module._FINAL_SENTENCE
        )
        assert prompt.endswith(chat_module._FINAL_SENTENCE)
        # The per-turn workspace sentence is the only place repo_root belongs.
        assert "repo_root" not in prompt
        assert "approve_merge" not in prompt
        assert "set_story_status" not in prompt
