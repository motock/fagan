"""Regression tests for the chat turn-cap answer (CHA-1).

Story: ``ChatService.stream_turn`` used to return the model's raw final reply
plus ``"(turn cap reached)"`` when the loop exhausted ``max_turns``. When that
final turn was a tool-call turn, the user was shown raw ``[TOOL_CALL]``
protocol markup as the assistant's answer.

Required behaviour under test:

* A near-cap nudge is appended to the running prompt so the model is told how
  many turns remain and asked to answer in prose instead of calling a tool.
* At the cap, raw ``[TOOL_CALL]`` blocks are stripped from the reply; whatever
  prose remains is kept, and when nothing remains a plain fallback sentence is
  used. Every cap reply still ENDS with ``"(turn cap reached)"``.
* The fix adds no model call: the driver is called exactly ``max_turns`` times.
* The normal tool-call-free path is untouched (prose returned as-is, no cap
  marker).

These tests use a local scripted driver (the same minimal pattern as
``tests/unit/test_chat_stream_turn.py``) rather than importing helpers from
another test module.
"""
from __future__ import annotations

import json

import pytest

import app.chat as chat_module
from app.chat import ChatService

_TOOL_NAME = "list_plans"
_TOOL_RESULT_PAYLOAD = {"plans": ["alpha", "beta"]}
_CAP_MARKER = "(turn cap reached)"


# --------------------------------------------------------------------------- #
# Fakes
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
    """Returns the next reply in ``replies`` on each ``complete`` call."""

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


def _service(driver: _ScriptedDriver, max_turns: int) -> ChatService:
    """Build a ChatService with an injected driver and an explicit cap.

    ``max_turns`` is always passed explicitly so the ambient
    ``PIPELINE_CHAT_MAX_TURNS`` environment variable cannot perturb a test.
    """
    return ChatService(
        driver=driver,
        http_client=_FakeClient(),
        api_base_url="http://x.test",
        max_turns=max_turns,
    )


def _drain(generator) -> list[dict]:
    return list(generator)


def _result(events: list[dict]) -> dict:
    """Return the final ``result`` event's data (the loop's return value)."""
    assert events, "stream_turn yielded no events"
    assert events[-1]["type"] == "result", (
        f"last event must be 'result', got {events[-1]['type']!r}"
    )
    return events[-1]["data"]


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
# Near-cap nudge
# --------------------------------------------------------------------------- #
class TestNearCapNudge:
    def test_final_turn_prompt_warns_about_remaining_turns(self):
        """The last turn's prompt must tell the model to stop calling tools."""
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}) for _ in range(3)])
        service = _service(driver, max_turns=3)

        _drain(service.stream_turn("tell me the current state"))

        assert len(driver.calls) == 3, (
            f"expected exactly 3 driver calls (no extra nudge call), got {len(driver.calls)}"
        )
        final_prompt = driver.calls[-1]["prompt"]
        assert "Do not call another tool" in final_prompt, (
            "the final turn's prompt must instruct the model not to call another tool; "
            f"got: {final_prompt!r}"
        )
        assert "turn(s) left" in final_prompt, (
            f"the final turn's prompt must state the remaining turns; got: {final_prompt!r}"
        )


# --------------------------------------------------------------------------- #
# Cap-path sanitisation
# --------------------------------------------------------------------------- #
class TestCapReplySanitisation:
    def test_prose_plus_trailing_tool_call_keeps_prose_only(self):
        """Prose before a trailing tool call survives; the markup does not."""
        prose = "Here is what I found."
        driver = _ScriptedDriver([prose + "\n" + _tool_call(_TOOL_NAME, {})])
        service = _service(driver, max_turns=1)

        data = _result(_drain(service.stream_turn("tell me the current state")))

        assert prose in data["reply"], (
            f"prose must be preserved at the cap; got: {data['reply']!r}"
        )
        assert "[TOOL_CALL]" not in data["reply"], (
            f"raw [TOOL_CALL] markup must never be returned as the answer; got: {data['reply']!r}"
        )
        assert data["reply"].endswith(_CAP_MARKER), (
            f"cap reply must end with {_CAP_MARKER!r}; got: {data['reply']!r}"
        )

    def test_tool_call_only_reply_falls_back_to_plain_sentence(self):
        """A cap turn with nothing but a tool call yields a plain explanation."""
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {})])
        service = _service(driver, max_turns=1)

        data = _result(_drain(service.stream_turn("tell me the current state")))

        assert "[TOOL_CALL]" not in data["reply"], (
            f"raw [TOOL_CALL] markup must never be returned as the answer; got: {data['reply']!r}"
        )
        assert "ran out of tool turns" in data["reply"], (
            f"an empty-after-strip cap reply must say so plainly; got: {data['reply']!r}"
        )
        assert data["reply"].endswith(_CAP_MARKER), (
            f"cap reply must end with {_CAP_MARKER!r}; got: {data['reply']!r}"
        )


# --------------------------------------------------------------------------- #
# Budget and normal path
# --------------------------------------------------------------------------- #
class TestBudgetAndNormalPath:
    def test_cap_costs_no_extra_model_call(self):
        """The cap path must spend exactly ``max_turns`` driver calls."""
        driver = _ScriptedDriver([_tool_call(_TOOL_NAME, {}) for _ in range(3)])
        service = _service(driver, max_turns=3)

        _drain(service.stream_turn("tell me the current state"))

        assert len(driver.calls) == 3, (
            f"the fix must not add a model call; expected 3, got {len(driver.calls)}"
        )

    def test_normal_reply_is_returned_unchanged(self):
        """A tool-call-free reply is returned as-is, with no cap marker."""
        driver = _ScriptedDriver(["All done."])
        service = _service(driver, max_turns=3)

        data = _result(_drain(service.stream_turn("anything to report?")))

        assert data["reply"] == "All done.", (
            f"the normal path must return the model's prose verbatim; got: {data['reply']!r}"
        )
        assert _CAP_MARKER not in data["reply"], (
            f"the normal path must not carry the cap marker; got: {data['reply']!r}"
        )
