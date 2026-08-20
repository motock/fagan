"""Tests for app/chat.py — the multi-turn agent loop (PART 2 of 4).

This story replaces the minimal single-shot ``execute_turn`` body with the
full agent loop: [TOOL_CALL] parsing, tool dispatch via the (currently empty)
``TOOLS`` registry, [TOOL_RESULT] feedback, and the ``max_turns`` cap.

These tests must pass against an implementation that does not yet exist, so
they import ``app.chat`` and assert the documented behavior. They are
self-contained: they register temporary test-only entries in ``TOOLS`` and
clean them up afterwards so the registry is left empty for later stories.
"""
from __future__ import annotations

import json

import app.chat as chat_module
from app.chat import SYSTEM_PROMPT, TOOLS, ChatService, _execute_tool, _parse_tool_calls


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _ScriptedDriver:
    """A driver that returns a scripted sequence of replies.

    Each call to ``complete`` returns the next reply in ``replies``. The last
    reply is reused if the loop runs past the script length (useful for the
    max-turns-cap test). All calls are recorded.
    """

    def __init__(self, replies: list[str], model: str = "test-model") -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.model = model

    def complete(self, prompt: str, *, system, model, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system, "model": model})
        if self.replies:
            return self.replies.pop(0)
        # Fall back to the last reply if the script is exhausted.
        return self.calls[-1]["prompt"]


def _tool_call(name: str, args: dict) -> str:
    return f"[TOOL_CALL]{json.dumps({'name': name, 'args': args})}[/TOOL_CALL]"


# --------------------------------------------------------------------------- #
# _parse_tool_calls
# --------------------------------------------------------------------------- #
class TestParseToolCalls:
    def test_no_tool_calls_returns_empty(self) -> None:
        assert _parse_tool_calls("just plain text, no tags here") == []

    def test_single_valid_call(self) -> None:
        text = _tool_call("ping", {"host": "x"})
        result = _parse_tool_calls(text)
        assert result == [{"name": "ping", "args": {"host": "x"}}]

    def test_two_valid_calls_separated_by_plain_text(self) -> None:
        text = (
            "before text\n"
            + _tool_call("first", {"a": 1})
            + "\nmiddle text\n"
            + _tool_call("second", {"b": 2})
            + "\nafter text"
        )
        result = _parse_tool_calls(text)
        assert result == [
            {"name": "first", "args": {"a": 1}},
            {"name": "second", "args": {"b": 2}},
        ]

    def test_malformed_json_is_skipped_not_raised(self) -> None:
        text = "[TOOL_CALL]{not valid json}[/TOOL_CALL]"
        assert _parse_tool_calls(text) == []

    def test_missing_name_key_is_skipped(self) -> None:
        # Valid JSON but no 'name' key -> skipped.
        text = "[TOOL_CALL]" + json.dumps({"args": {}}) + "[/TOOL_CALL]"
        assert _parse_tool_calls(text) == []

    def test_missing_args_key_is_skipped(self) -> None:
        text = "[TOOL_CALL]" + json.dumps({"name": "x"}) + "[/TOOL_CALL]"
        assert _parse_tool_calls(text) == []

    def test_mixed_valid_and_invalid_keeps_valid(self) -> None:
        text = (
            _tool_call("good", {"n": 1})
            + "[TOOL_CALL]{bad json}[/TOOL_CALL]"
            + _tool_call("also_good", {"n": 2})
        )
        result = _parse_tool_calls(text)
        assert result == [
            {"name": "good", "args": {"n": 1}},
            {"name": "also_good", "args": {"n": 2}},
        ]

    def test_empty_string(self) -> None:
        assert _parse_tool_calls("") == []


# --------------------------------------------------------------------------- #
# _execute_tool
# --------------------------------------------------------------------------- #
class TestExecuteTool:
    def test_unknown_tool_returns_error(self) -> None:
        # 'ping' is not registered in the (empty) TOOLS registry.
        result = _execute_tool("ping", {}, None, "http://x.test")
        assert result == {"error": "unknown tool: ping"}

    def test_known_tool_success_wraps_result(self, monkeypatch) -> None:
        def _execute(http_client, api_base_url, **kwargs):
            assert http_client == "CLIENT"
            assert api_base_url == "http://base.test"
            assert kwargs == {"x": 1}
            return {"pong": True}

        monkeypatch.setitem(TOOLS, "ping", {"execute": _execute})
        try:
            result = _execute_tool("ping", {"x": 1}, "CLIENT", "http://base.test")
        finally:
            TOOLS.pop("ping", None)
        assert result == {"result": {"pong": True}}

    def test_tool_exception_is_caught(self, monkeypatch) -> None:
        def _boom(http_client, api_base_url, **kwargs):
            raise RuntimeError("boom!")

        monkeypatch.setitem(TOOLS, "explode", {"execute": _boom})
        try:
            result = _execute_tool("explode", {}, None, "http://x.test")
        finally:
            TOOLS.pop("explode", None)
        assert result == {"error": "boom!"}


# --------------------------------------------------------------------------- #
# execute_turn — happy path
# --------------------------------------------------------------------------- #
class TestExecuteTurnHappyPath:
    def test_tool_call_then_final_reply(self, monkeypatch) -> None:
        def _execute(http_client, api_base_url, **kwargs):
            return {"pong": True}

        monkeypatch.setitem(TOOLS, "ping", {"execute": _execute})
        driver = _ScriptedDriver(
            replies=[
                _tool_call("ping", {}),
                "done",
            ]
        )
        svc = ChatService(driver=driver, http_client=object(), api_base_url="http://x.test")
        out = svc.execute_turn("hello")
        assert out == {
            "reply": "done",
            "tool_calls": [
                {"name": "ping", "args": {}, "result": {"result": {"pong": True}}}
            ],
            "turns": 2,
        }

    def test_system_prompt_passed_to_driver(self, monkeypatch) -> None:
        def _execute(http_client, api_base_url, **kwargs):
            return {}

        monkeypatch.setitem(TOOLS, "ping", {"execute": _execute})
        driver = _ScriptedDriver(replies=[_tool_call("ping", {}), "done"])
        svc = ChatService(driver=driver, http_client=object(), api_base_url="http://x.test")
        svc.execute_turn("hello")
        for call in driver.calls:
            assert call["system"] == SYSTEM_PROMPT

    def test_tool_result_fed_back_into_next_turn(self, monkeypatch) -> None:
        def _execute(http_client, api_base_url, **kwargs):
            return {"value": 42}

        monkeypatch.setitem(TOOLS, "ping", {"execute": _execute})
        driver = _ScriptedDriver(replies=[_tool_call("ping", {}), "done"])
        svc = ChatService(driver=driver, http_client=object(), api_base_url="http://x.test")
        svc.execute_turn("hello")
        # Second call's prompt must contain the TOOL_RESULT feedback block.
        assert len(driver.calls) == 2
        second_prompt = driver.calls[1]["prompt"]
        assert "[TOOL_RESULT name=ping]" in second_prompt
        assert "[/TOOL_RESULT]" in second_prompt
        assert json.dumps({"result": {"value": 42}}) in second_prompt


# --------------------------------------------------------------------------- #
# execute_turn — negative / boundary cases
# --------------------------------------------------------------------------- #
class TestExecuteTurnNegative:
    def test_unparseable_output_returned_as_is(self) -> None:
        driver = _ScriptedDriver(replies=["just a plain reply, no tags"])
        svc = ChatService(driver=driver, http_client=object(), api_base_url="http://x.test")
        out = svc.execute_turn("hello")
        assert out == {"reply": "just a plain reply, no tags", "tool_calls": [], "turns": 1}

    def test_max_turns_cap(self, monkeypatch) -> None:
        def _noop(http_client, api_base_url, **kwargs):
            return {}

        monkeypatch.setitem(TOOLS, "loop", {"execute": _noop})
        # Always returns a tool call -> never a tool-call-free response.
        driver = _ScriptedDriver(replies=[_tool_call("loop", {})] * 10)
        svc = ChatService(
            driver=driver, http_client=object(), api_base_url="http://x.test", max_turns=3
        )
        out = svc.execute_turn("hello")
        assert out["turns"] == 3
        assert "(turn cap reached)" in out["reply"]
        # The reply is the last response plus the cap marker.
        assert out["reply"].startswith(_tool_call("loop", {}))
        # Exactly max_turns driver calls were made.
        assert len(driver.calls) == 3

    def test_unknown_tool_handled_gracefully_and_continues(self, monkeypatch) -> None:
        # 'ghost' is NOT registered in TOOLS.
        driver = _ScriptedDriver(
            replies=[
                _tool_call("ghost", {}),
                "recovered",
            ]
        )
        svc = ChatService(driver=driver, http_client=object(), api_base_url="http://x.test")
        out = svc.execute_turn("hello")
        assert out["reply"] == "recovered"
        assert out["turns"] == 2
        assert out["tool_calls"] == [
            {"name": "ghost", "args": {}, "result": {"error": "unknown tool: ghost"}}
        ]
        # The error was fed back as a TOOL_RESULT block to the next turn.
        second_prompt = driver.calls[1]["prompt"]
        assert "[TOOL_RESULT name=ghost]" in second_prompt
        assert "unknown tool: ghost" in second_prompt

    def test_tool_exception_caught_and_loop_continues(self, monkeypatch) -> None:
        def _boom(http_client, api_base_url, **kwargs):
            raise ValueError("kaboom")

        monkeypatch.setitem(TOOLS, "explode", {"execute": _boom})
        driver = _ScriptedDriver(
            replies=[
                _tool_call("explode", {}),
                "after-error",
            ]
        )
        svc = ChatService(driver=driver, http_client=object(), api_base_url="http://x.test")
        out = svc.execute_turn("hello")
        assert out["reply"] == "after-error"
        assert out["turns"] == 2
        assert out["tool_calls"] == [
            {"name": "explode", "args": {}, "result": {"error": "kaboom"}}
        ]

    def test_multiple_tool_calls_in_one_response(self, monkeypatch) -> None:
        calls = {"count": 0}

        def _execute(http_client, api_base_url, **kwargs):
            calls["count"] += 1
            return {"i": calls["count"]}

        monkeypatch.setitem(TOOLS, "ping", {"execute": _execute})
        driver = _ScriptedDriver(
            replies=[
                _tool_call("ping", {}) + _tool_call("ping", {}),
                "done",
            ]
        )
        svc = ChatService(driver=driver, http_client=object(), api_base_url="http://x.test")
        out = svc.execute_turn("hello")
        assert out["reply"] == "done"
        assert out["turns"] == 2
        assert len(out["tool_calls"]) == 2
        assert out["tool_calls"][0]["result"] == {"result": {"i": 1}}
        assert out["tool_calls"][1]["result"] == {"result": {"i": 2}}
        # Both results fed back in the next prompt.
        second_prompt = driver.calls[1]["prompt"]
        assert second_prompt.count("[TOOL_RESULT name=ping]") == 2


# --------------------------------------------------------------------------- #
# Module-level invariants
# --------------------------------------------------------------------------- #
class TestModuleInvariants:
    def test_tools_registry_is_empty_by_default(self) -> None:
        # This story must NOT populate TOOLS; a later story adds real tools.
        assert TOOLS == {}

    def test_tools_is_a_dict(self) -> None:
        assert isinstance(TOOLS, dict)

    def test_no_pipeline_service_import(self) -> None:
        import inspect

        source = inspect.getsource(chat_module)
        assert "PipelineService" not in source
        assert "_service" not in source