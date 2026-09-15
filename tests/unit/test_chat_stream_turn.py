"""Behaviour tests for the ``ChatService.stream_turn`` event generator.

Story: refactor ``ChatService.execute_turn`` (``app/chat.py``) into a
rename-and-delegate shape -- a NEW generator method ``stream_turn`` that owns
the loop logic and yields event dicts, and a SHORT ``execute_turn`` that
drains the generator and returns the final ``result`` event's ``data``.
``execute_turn``'s signature and return value must be IDENTICAL to today's;
no HTTP/SSE change in this story.

Event contract under test (exactly five types, nothing more):

* ``{"type": "turn", "data": {"n": <int>}}`` -- top of each loop iteration.
* ``{"type": "tool_call", "data": {"name": <str>, "args": <dict>}}`` -- BEFORE
  each tool executes.
* ``{"type": "tool_result", "data": {"name": <str>, "args": <dict>, "result":
  <dict>}}`` -- AFTER each tool returns.
* ``{"type": "reply", "data": {"text": <str>}}`` -- once, for the final
  conversational reply.
* ``{"type": "result", "data": {"reply": <str>, "tool_calls": <list>,
  "turns": <int>}}`` -- LAST, exactly once, on EVERY exit path; ``data`` is
  byte-for-byte the dict ``execute_turn`` returns today.

Exit paths covered: the normal reply path, the
``_looks_like_unparsed_tool_call`` nudge path, the ``_looks_like_deferred_action``
nudge path and its cap (SSE-01), and the ``_max_turns`` cap path (whose reply
ends with ``(turn cap reached)``).

TDD RED state: ``ChatService.stream_turn`` does not exist yet, so the
behaviour tests fail with ``AttributeError: 'ChatService' object has no
attribute 'stream_turn'`` and the source-shape tests fail their assertions
against today's monolithic ``execute_turn``. That is the expected failure
reason, not a bug in these tests. No existing test file is modified: every
existing caller keeps calling ``execute_turn`` and must pass unchanged.
"""
from __future__ import annotations

import inspect
import json
import tempfile
from pathlib import Path

import pytest

import app.chat as chat_module
from app.chat import ChatService

# Every event dict must have exactly these two keys, and "type" must be one
# of exactly these five values -- nothing more, nothing less.
_ALLOWED_EVENT_TYPES = {"turn", "tool_call", "tool_result", "reply", "result"}

_PLAIN_REPLY = "just a plain reply, no tags"
_TOOL_NAME = "list_plans"
_TOOL_RESULT_PAYLOAD = {"plans": ["alpha", "beta"]}
_EXECUTED_TOOL_RESULT = {"result": _TOOL_RESULT_PAYLOAD}  # _execute_tool wraps
_TOOL_CALL_ENTRY = {"name": _TOOL_NAME, "args": {}, "result": _EXECUTED_TOOL_RESULT}


# --------------------------------------------------------------------------- #
# Fakes (same patterns as tests/unit/test_chat_deferred_action_nudge.py)
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

    def get(self, url, *args, **kwargs) -> _FakeResponse:
        self.get_calls.append(url)
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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _tool_call(name: str, args: dict) -> str:
    return "[TOOL_CALL]" + json.dumps({"name": name, "args": args}) + "[/TOOL_CALL]"


def _service(driver: _ScriptedDriver, max_turns: int = 10) -> ChatService:
    """Build a ChatService with an injected driver and a deterministic cap.

    ``max_turns`` is ALWAYS passed explicitly so the ambient
    ``PIPELINE_CHAT_MAX_TURNS`` environment variable cannot perturb a test.
    """
    return ChatService(
        driver=driver,
        http_client=_FakeClient(),
        api_base_url="http://x.test",
        max_turns=max_turns,
    )


def _drain(generator) -> list[dict]:
    """Consume a ``stream_turn`` generator, validating the event envelope.

    Every yielded value must be a dict with exactly the keys ``type`` and
    ``data``, and ``type`` must be one of the five contracted event types.
    """
    events: list[dict] = []
    for event in generator:
        assert isinstance(event, dict), f"event must be a dict, got {type(event)!r}: {event!r}"
        assert set(event) == {"type", "data"}, (
            f"event must have exactly 'type' and 'data' keys, got {sorted(event)!r}: {event!r}"
        )
        assert event["type"] in _ALLOWED_EVENT_TYPES, (
            f"event type {event['type']!r} is outside the contracted five "
            f"{sorted(_ALLOWED_EVENT_TYPES)!r}"
        )
        events.append(event)
    return events


def _module_source() -> str:
    return Path(chat_module.__file__).read_text(encoding="utf-8")


def _types(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


@pytest.fixture(autouse=True)
def _stub_tool_url_resolver(monkeypatch):
    """Pin the tool-URL resolver so the fake client's ``get`` is deterministic.

    URL resolution itself is covered by test_chat_path_encoding.py; these
    tests only need ``_execute_tool`` to return a predictable dict.
    """
    if hasattr(chat_module, "_resolve_tool_url"):
        monkeypatch.setattr(
            chat_module,
            "_resolve_tool_url",
            lambda http_client, api_base_url, path, *args, **kwargs: f"{api_base_url}{path}",
        )


# --------------------------------------------------------------------------- #
# Positive: the event contract on the happy paths
# --------------------------------------------------------------------------- #
class TestStreamTurnEventContract:
    def test_plain_reply_yields_turn_then_reply_then_result(self) -> None:
        driver = _ScriptedDriver([_PLAIN_REPLY])
        events = _drain(_service(driver).stream_turn("hi"))

        assert [e["type"] for e in events] == ["turn", "reply", "result"]
        assert events[0]["data"] == {"n": 1}
        assert events[1]["data"] == {"text": _PLAIN_REPLY}
        assert events[2]["data"] == {"reply": _PLAIN_REPLY, "tool_calls": [], "turns": 1}

    def test_tool_call_event_precedes_matching_tool_result_event(self) -> None:
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}), "here is the summary."])
        events = _drain(_service(driver).stream_turn("hi"))

        types = [e["type"] for e in events]
        assert types == ["turn", "tool_call", "tool_result", "turn", "reply", "result"]
        call_index = types.index("tool_call")
        result_index = types.index("tool_result")
        assert call_index < result_index, "tool_call must be yielded BEFORE tool_result"
        assert events[call_index]["data"]["name"] == events[result_index]["data"]["name"]

    def test_tool_event_payloads_are_exact(self) -> None:
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}), "here is the summary."])
        events = _drain(_service(driver).stream_turn("hi"))

        assert events[1]["data"] == {"name": _TOOL_NAME, "args": {}}
        assert events[2]["data"] == {
            "name": _TOOL_NAME,
            "args": {},
            "result": _EXECUTED_TOOL_RESULT,
        }

    def test_reply_event_payload_is_exact(self) -> None:
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}), "here is the summary."])
        events = _drain(_service(driver).stream_turn("hi"))

        replies = [e for e in events if e["type"] == "reply"]
        assert len(replies) == 1
        assert replies[0]["data"] == {"text": "here is the summary."}

    def test_turn_events_number_each_loop_iteration(self) -> None:
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}), "here is the summary."])
        events = _drain(_service(driver).stream_turn("hi"))

        assert [e["data"]["n"] for e in events if e["type"] == "turn"] == [1, 2]

    def test_result_event_is_last_and_carries_the_full_turn_record(self) -> None:
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}), "here is the summary."])
        events = _drain(_service(driver).stream_turn("hi"))

        assert events[-1]["type"] == "result"
        assert events[-1]["data"] == {
            "reply": "here is the summary.",
            "tool_calls": [_TOOL_CALL_ENTRY],
            "turns": 2,
        }

    def test_all_five_event_types_appear_and_no_others(self) -> None:
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}), "here is the summary."])
        events = _drain(_service(driver).stream_turn("hi"))

        assert {e["type"] for e in events} == _ALLOWED_EVENT_TYPES

    def test_stream_turn_accepts_every_documented_keyword(self) -> None:
        driver = _ScriptedDriver(["ok"])
        events = _drain(
            _service(driver).stream_turn("hi", plan_name="demo-plan", history=None, workspace=None)
        )

        assert [e["type"] for e in events] == ["turn", "reply", "result"]

    def test_execute_turn_returns_exactly_the_result_event_data(self) -> None:
        script = [_tool_call(_TOOL_NAME, {}), "here is the summary."]

        streamed = None
        for event in _drain(_service(_ScriptedDriver(list(script))).stream_turn("hi")):
            if event["type"] == "result":
                streamed = event["data"]
        assert streamed is not None

        direct = _service(_ScriptedDriver(list(script))).execute_turn("hi")
        assert direct == streamed
        assert direct == {
            "reply": "here is the summary.",
            "tool_calls": [_TOOL_CALL_ENTRY],
            "turns": 2,
        }


# --------------------------------------------------------------------------- #
# Preserved loop behaviour, observed through the event stream
# --------------------------------------------------------------------------- #
class TestPreservedLoopBehaviour:
    def test_max_turns_cap_path_yields_result_with_cap_suffix(self) -> None:
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {})])
        events = _drain(_service(driver, max_turns=1).stream_turn("hi"))

        assert [e["type"] for e in events] == ["turn", "tool_call", "tool_result", "result"]
        data = events[-1]["data"]
        assert data["reply"].endswith("(turn cap reached)")
        assert data["reply"] == _tool_call(_TOOL_NAME, {}) + "\n\n(turn cap reached)"
        assert data["turns"] == 1
        assert data["tool_calls"] == [_TOOL_CALL_ENTRY]

    def test_unparsed_tool_call_nudge_path_still_yields_result(self) -> None:
        stalled = "[TOOL_CALL]{not json}[/TOOL_CALL]"
        driver = _ScriptedDriver([stalled, "all done."])
        events = _drain(_service(driver).stream_turn("hi"))

        # The nudge branch is preserved: the second driver call re-prompts.
        assert "could not be parsed" in driver.calls[1]["prompt"]
        assert [e["type"] for e in events] == ["turn", "turn", "reply", "result"]
        assert [e["data"]["n"] for e in events if e["type"] == "turn"] == [1, 2]
        assert events[-1]["data"] == {"reply": "all done.", "tool_calls": [], "turns": 2}

    def test_deferred_action_nudge_cap_path_still_yields_result(self) -> None:
        narration = "Let me check on that for you."
        driver = _ScriptedDriver([narration, narration, narration])
        events = _drain(_service(driver).stream_turn("hi"))

        # Both SSE-01 nudges were issued (branch and cap preserved)...
        assert "Stop narrating" in driver.calls[1]["prompt"]
        assert "Stop narrating" in driver.calls[2]["prompt"]
        # ...then the third narration falls through as the final reply.
        assert [e["type"] for e in events] == ["turn", "turn", "turn", "reply", "result"]
        replies = [e for e in events if e["type"] == "reply"]
        assert len(replies) == 1
        assert replies[0]["data"] == {"text": narration}
        assert events[-1]["data"] == {"reply": narration, "tool_calls": [], "turns": 3}

    def test_workspace_suffix_is_threaded_into_every_system_prompt(self) -> None:
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}), "done"])
        _drain(_service(driver).stream_turn("hi", workspace="/tmp/demo-ws"))

        assert len(driver.calls) == 2
        for call in driver.calls:
            assert "The active workspace is /tmp/demo-ws" in call["system"]
            assert "repo_root" in call["system"]

    def test_no_workspace_suffix_without_workspace(self) -> None:
        driver = _ScriptedDriver(["ok"])
        _drain(_service(driver).stream_turn("hi"))

        assert "active workspace" not in driver.calls[0]["system"]

    def test_driver_receives_cwd_tempfile_gettempdir(self) -> None:
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}), "done"])
        _drain(_service(driver).stream_turn("hi"))

        assert len(driver.calls) == 2
        for call in driver.calls:
            assert call["cwd"] == tempfile.gettempdir()

    def test_tool_result_feedback_block_feeds_the_next_prompt(self) -> None:
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}), "done"])
        _drain(_service(driver).stream_turn("hi"))

        second_prompt = driver.calls[1]["prompt"]
        assert "[TOOL_RESULT name=list_plans]" in second_prompt
        assert "[/TOOL_RESULT]" in second_prompt
        assert json.dumps(_EXECUTED_TOOL_RESULT) in second_prompt

    def test_stream_turn_resolves_the_driver_exactly_once(self) -> None:
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}), "done"])
        service = _service(driver)
        real_resolve = service._resolve_driver
        seen: list[int] = []

        def spy():
            seen.append(1)
            return real_resolve()

        service._resolve_driver = spy
        events = _drain(service.stream_turn("hi"))

        assert len(seen) == 1
        assert [e["type"] for e in events] == ["turn", "tool_call", "tool_result", "turn", "reply", "result"]

    def test_history_is_rendered_into_the_prompt(self) -> None:
        driver = _ScriptedDriver(["ok"])
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        _drain(_service(driver).stream_turn("go", history=history))

        prompt = driver.calls[0]["prompt"]
        assert "assistant: hello" in prompt
        assert "user: go" in prompt

    def test_no_history_keeps_the_prompt_passthrough(self) -> None:
        driver = _ScriptedDriver(["ok"])
        _drain(_service(driver).stream_turn("hello there"))

        assert driver.calls[0]["prompt"] == "hello there"


# --------------------------------------------------------------------------- #
# Negative / boundary
# --------------------------------------------------------------------------- #
_PATHS = {
    "plain_reply": {"replies": [_PLAIN_REPLY], "max_turns": 10},
    "tool_then_reply": {
        "replies": [_tool_call(_TOOL_NAME, {}), "here is the summary."],
        "max_turns": 10,
    },
    "unparsed_nudge_then_reply": {
        "replies": ["[TOOL_CALL]{not json}[/TOOL_CALL]", "all done."],
        "max_turns": 10,
    },
    "deferred_nudge_cap": {
        "replies": ["Let me check on that for you."] * 3,
        "max_turns": 10,
    },
    "max_turns_cap": {"replies": [_tool_call(_TOOL_NAME, {})], "max_turns": 1},
}


class TestExitPathsAndGeneratorMechanics:
    @pytest.mark.parametrize("case", sorted(_PATHS))
    def test_exactly_one_result_event_on_every_exit_path(self, case: str) -> None:
        spec = _PATHS[case]
        events = _drain(
            _service(_ScriptedDriver(list(spec["replies"])), spec["max_turns"]).stream_turn("hi")
        )

        results = [e for e in events if e["type"] == "result"]
        assert len(results) == 1, (
            f"path {case!r} yielded {len(results)} result events; exactly one is required"
        )
        assert events[-1] is results[0], "the result event must be yielded LAST"

        # execute_turn on an identical scripted driver must return exactly the
        # result event's data -- never None, or every existing caller breaks.
        direct = _service(_ScriptedDriver(list(spec["replies"])), spec["max_turns"]).execute_turn("hi")
        assert direct is not None, f"execute_turn returned None on path {case!r}"
        assert direct == results[0]["data"]

    def test_empty_tool_list_terminates_with_a_single_result(self) -> None:
        driver = _ScriptedDriver([_PLAIN_REPLY])
        events = _drain(_service(driver).stream_turn("hi"))

        assert [e["type"] for e in events] == ["turn", "reply", "result"]
        assert events[-1]["data"]["tool_calls"] == []
        assert len([e for e in events if e["type"] == "result"]) == 1

    def test_stream_turn_is_lazy_until_the_first_item_is_consumed(self) -> None:
        driver = _ScriptedDriver([_PLAIN_REPLY])
        service = _service(driver)
        real_resolve = service._resolve_driver
        resolved: list[int] = []

        def spy():
            resolved.append(1)
            return real_resolve()

        service._resolve_driver = spy

        generator = service.stream_turn("hi")
        assert iter(generator) is generator, "stream_turn must return a true iterator"
        # Calling stream_turn performs NO work at all: no driver call, no
        # driver resolution, until the first item is consumed.
        assert driver.calls == []
        assert resolved == []

        first = next(generator)
        assert set(first) == {"type", "data"}
        assert first["type"] == "turn"
        assert first["data"] == {"n": 1}
        assert len(driver.calls) == 1
        assert len(resolved) == 1
        generator.close()


class TestSourceShape:
    """The rename-and-delegate shape, graded on the source itself."""

    def test_stream_turn_is_a_generator_function(self) -> None:
        assert inspect.isgeneratorfunction(ChatService.stream_turn)

    def test_execute_turn_is_not_a_generator_function(self) -> None:
        # If execute_turn accidentally became a generator, every existing
        # caller would receive a generator object instead of a dict.
        assert not inspect.isgeneratorfunction(ChatService.execute_turn)

    def test_stream_turn_signature_matches_the_brief(self) -> None:
        signature = inspect.signature(ChatService.stream_turn)
        names = [p.name for p in signature.parameters.values()]
        assert names == ["self", "message", "plan_name", "history", "workspace"]
        assert signature.parameters["message"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for name in ("plan_name", "history", "workspace"):
            parameter = signature.parameters[name]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is None

    def test_execute_turn_signature_is_unchanged(self) -> None:
        signature = inspect.signature(ChatService.execute_turn)
        names = [p.name for p in signature.parameters.values()]
        assert names == ["self", "message", "plan_name", "history", "workspace"]
        for name in ("plan_name", "history", "workspace"):
            parameter = signature.parameters[name]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is None
        assert signature.return_annotation in ("dict", dict)

    def test_execute_turn_body_is_a_short_draining_loop(self) -> None:
        source = inspect.getsource(ChatService.execute_turn)
        assert "self.stream_turn(" in source, "execute_turn must delegate to stream_turn"
        assert "return" in source
        # The loop logic must have MOVED, not been duplicated.
        for absent in (
            "yield",
            "driver.complete",
            "_parse_tool_calls",
            "_looks_like_",
            "_execute_tool",
            "_build_chat_prompt",
            "tempfile.gettempdir",
            "SYSTEM_PROMPT",
            "_DEFERRED_NUDGE_CAP",
            "turn cap reached",
        ):
            assert absent not in source, (
                f"execute_turn must be a short draining loop; found {absent!r} in its body"
            )
        assert len(source.strip().splitlines()) <= 20, (
            "execute_turn body must be short (drain the generator, return the result data)"
        )

    def test_stream_turn_body_contains_the_moved_loop_logic(self) -> None:
        source = inspect.getsource(ChatService.stream_turn)
        for token in (
            "_resolve_driver",
            "_build_chat_prompt",
            "SYSTEM_PROMPT",
            "tempfile.gettempdir",
            "_parse_tool_calls",
            "_looks_like_unparsed_tool_call",
            "_looks_like_deferred_action",
            "_DEFERRED_NUDGE_CAP",
            "_execute_tool",
            "turn cap reached",
        ):
            assert token in source, f"stream_turn must own the loop logic; missing {token!r}"
        assert "yield" in source

    def test_module_has_at_least_five_yield_sites(self) -> None:
        # Mirrors the acceptance check: grep -c 'yield' app/chat.py >= 5.
        assert _module_source().count("yield") >= 5

    def test_events_are_plain_dicts_not_formatted_strings(self) -> None:
        # Behavioural guard for the "no SSE formatting in this story" rule:
        # stream_turn yields plain event dicts, never pre-formatted strings.
        driver = _ScriptedDriver([_PLAIN_REPLY])
        for event in _service(driver).stream_turn("hi"):
            assert isinstance(event, dict)


# --------------------------------------------------------------------------- #
# Prompt transcript: the user's message must survive EVERY model call in a turn
# --------------------------------------------------------------------------- #
_QUESTION = "Is that correct?"
_PRIOR_USER_TEXT = "how many plans are there?"
_PRIOR_ASSISTANT_TEXT = "there are three plans in the active workspace"

# The exact nudge texts stream_turn must keep byte-identical while switching
# from REPLACING current_prompt to APPENDING to it. Built by concatenation (not
# ``str.format``) because the texts themselves contain ``{...}`` JSON braces.
_DEFERRED_NUDGE_TEXT = (
    "Stop narrating - emit the tool call now, in exactly this shape: "
    '[TOOL_CALL]{"name": "<tool>", "args": {...}}[/TOOL_CALL]'
)


def _unparsed_nudge_text(response: str) -> str:
    return (
        "Your previous response contained a [TOOL_CALL] marker "
        "but it could not be parsed as a tool call. Here is "
        "exactly what you produced:\n"
        f"{response}\n"
        "Re-emit the tool call using exactly this tag/JSON "
        "shape, with valid JSON (no trailing commas, a string "
        '"name" and a dict "args"):\n'
        '[TOOL_CALL]{"name": "<tool>", "args": {...}}[/TOOL_CALL]'
    )


class TestPromptTranscriptSurvivesTheTurn:
    """The user's message must reach EVERY model call in a turn.

    ``stream_turn`` builds ``current_prompt`` once and then, on each tool-result
    or nudge iteration, must APPEND the assistant turn and the new feedback to
    that running transcript. Replacing it wholesale (the defect) drops the
    user's message and every prior turn, so the model's second and later calls
    receive no question at all. Assertions here are membership-only: the
    transcript is cumulative, so later iterations legitimately contain more text
    than earlier ones.
    """

    def test_user_message_survives_a_tool_round_trip(self) -> None:
        history = [
            {"role": "user", "content": _PRIOR_USER_TEXT},
            {"role": "assistant", "content": _PRIOR_ASSISTANT_TEXT},
        ]
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}), "yes, that is correct."])
        _drain(_service(driver).stream_turn(_QUESTION, history=history))

        assert len(driver.calls) == 2
        for call in driver.calls[1:]:
            assert _QUESTION in call["prompt"], (
                "the user's message must survive into every prompt from the "
                "second model call onward"
            )
            assert _PRIOR_ASSISTANT_TEXT in call["prompt"], (
                "the prior assistant turn must survive into every prompt from "
                "the second model call onward"
            )

    def test_tool_result_still_reaches_the_next_prompt(self) -> None:
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}), "done"])
        _drain(_service(driver).stream_turn(_QUESTION))

        second_prompt = driver.calls[1]["prompt"]
        assert f"[TOOL_RESULT name={_TOOL_NAME}]" in second_prompt
        assert "[/TOOL_RESULT]" in second_prompt
        assert json.dumps(_EXECUTED_TOOL_RESULT) in second_prompt

    def test_message_survives_the_unparsed_tool_call_nudge(self) -> None:
        stalled = "[TOOL_CALL]{not json}[/TOOL_CALL]"
        driver = _ScriptedDriver([stalled, "all done."])
        _drain(_service(driver).stream_turn(_QUESTION))

        nudge_prompt = driver.calls[1]["prompt"]
        assert _QUESTION in nudge_prompt
        assert _unparsed_nudge_text(stalled) in nudge_prompt

    def test_message_survives_the_deferred_action_nudge(self) -> None:
        narration = "Let me check that for you."
        driver = _ScriptedDriver([narration, "all done."])
        _drain(_service(driver).stream_turn(_QUESTION))

        nudge_prompt = driver.calls[1]["prompt"]
        assert _QUESTION in nudge_prompt
        assert _DEFERRED_NUDGE_TEXT in nudge_prompt

    @pytest.mark.parametrize("history", [None, []])
    def test_no_history_first_prompt_is_exactly_the_message(
        self, history: list[dict] | None
    ) -> None:
        driver = _ScriptedDriver(["ok"])
        _drain(_service(driver).stream_turn(_QUESTION, history=history))

        assert driver.calls[0]["prompt"] == _QUESTION

    def test_question_survives_every_call_of_a_multi_round_turn(self) -> None:
        driver = _ScriptedDriver(
            [_tool_call(_TOOL_NAME, {}), _tool_call(_TOOL_NAME, {}), "final answer."]
        )
        _drain(_service(driver).stream_turn(_QUESTION))

        assert len(driver.calls) == 3
        assert _QUESTION in driver.calls[-1]["prompt"], (
            "the question must survive into the FINAL model call, not just the second"
        )

    def test_unknown_tool_error_still_fed_back_with_the_question(self) -> None:
        unknown = "no_such_tool"
        driver = _ScriptedDriver([_tool_call(unknown, {}), "done"])
        _drain(_service(driver).stream_turn(_QUESTION))

        second_prompt = driver.calls[1]["prompt"]
        assert f"unknown tool: {unknown}" in second_prompt
        assert _QUESTION in second_prompt

    def test_all_three_prompt_sites_append_the_assistant_turn(self) -> None:
        import re

        source = _module_source()
        assert 'current_prompt = "\\n".join(result_blocks)' not in source, (
            "the tool-result site must append to the running transcript, not "
            "replace it with the bare tool-result blocks"
        )
        # Tolerant of line wrapping: the append expression must appear at all
        # three sites (tool results, unparsed-tool-call nudge, deferred-action
        # nudge), each keeping the exact '\\nassistant: ' separator.
        appends = re.findall(
            r'current_prompt\s*\+\s*"\\nassistant: "\s*\+\s*response', source
        )
        assert len(appends) >= 3, (
            "all three prompt sites must append '\\nassistant: ' + response to "
            f"the running transcript; found {len(appends)}"
        )

    def test_each_tool_result_is_appended_exactly_once_per_iteration(self) -> None:
        """Two tool iterations must append each result exactly once.

        The appended blocks must be built from the CURRENT iteration's calls,
        not from the turn-cumulative ``tool_calls_made`` list. Re-appending the
        whole cumulative list on every iteration re-states every earlier
        ``[TOOL_RESULT ...]`` block, grows the prompt quadratically in the
        number of tool iterations, and shows the model tool results it has
        already seen (plausibly reading them as repeated executions).
        """
        driver = _ScriptedDriver(
            [_tool_call(_TOOL_NAME, {}), _tool_call(_TOOL_NAME, {}), "final answer."]
        )
        _drain(_service(driver).stream_turn(_QUESTION))

        assert len(driver.calls) == 3
        marker = f"[TOOL_RESULT name={_TOOL_NAME}]"

        # The prompt handed to the THIRD model call is the running transcript
        # after two tool iterations: exactly two appends, one per iteration, so
        # each result is stated exactly once -- 2 blocks, never 3.
        third_prompt = driver.calls[2]["prompt"]
        assert third_prompt.count(marker) == 2, (
            "the third model call must see each tool result exactly once; the "
            "turn-cumulative tool_calls_made list must not be re-appended in "
            f"full on every iteration (found {third_prompt.count(marker)} blocks)"
        )

        # The segment appended for the SECOND iteration must carry only that
        # iteration's result -- iteration 1's block must not be re-stated.
        appended_for_second = third_prompt[len(driver.calls[1]["prompt"]):]
        assert appended_for_second.count(marker) == 1, (
            "each iteration must append only its own tool-result blocks; found "
            f"{appended_for_second.count(marker)} in the second iteration's append"
        )
