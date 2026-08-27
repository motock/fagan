"""Tests for app/chat.py — ``bare=True`` passthrough on driver.complete().

ChatService.execute_turn() must call ``driver.complete(...)`` with
``bare=True`` so that ClaudeCliDriver appends ``--bare`` to the underlying
``claude -p`` invocation, suppressing this repo's own CLAUDE.md
auto-discovery. Without it, CLAUDE.md's coding-agent "Agent Workflow"
confirmation rules silently layer on top of chat.py's own SYSTEM_PROMPT
and tool-confirmation policy, making the chat assistant over-conservative
about actions SYSTEM_PROMPT already permits without confirmation.

This file targets ONLY that one-line passthrough addition at the single
``driver.complete(...)`` call site in ``app/chat.py::ChatService.execute_turn``.
It does not re-test agent-loop mechanics already covered by
``test_chat_agent_loop.py`` / ``test_chat_service_skeleton.py``.
"""
from __future__ import annotations

import inspect
import json

import app.chat as chat_module
from app.chat import SYSTEM_PROMPT, ChatService


# --------------------------------------------------------------------------- #
# Fake driver — records the full kwargs dict, not just prompt/system/model.
# --------------------------------------------------------------------------- #
class _KwargsRecordingDriver:
    """Records every call's prompt/system/model plus the raw kwargs dict.

    Mirrors the existing fake-driver pattern (a ``complete`` signature of
    ``(self, prompt, *, system, model, **kwargs)``) but additionally
    captures ``kwargs`` verbatim so tests can assert on exactly what
    additional keyword arguments (e.g. ``bare``) were passed through.
    """

    def __init__(self, replies=("done",), model: str | None = None) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        if model is not None:
            self.model = model

    def complete(self, prompt: str, *, system, model, **kwargs) -> str:
        self.calls.append(
            {"prompt": prompt, "system": system, "model": model, "kwargs": dict(kwargs)}
        )
        if self.replies:
            return self.replies.pop(0)
        return "done"


def _tool_call(name: str, args: dict) -> str:
    return f"[TOOL_CALL]{json.dumps({'name': name, 'args': args})}[/TOOL_CALL]"


# --------------------------------------------------------------------------- #
# Happy path — bare=True is passed through on a single-turn call.
# --------------------------------------------------------------------------- #
class TestBarePassthroughHappyPath:
    def test_bare_true_included_in_recorded_kwargs(self) -> None:
        driver = _KwargsRecordingDriver(replies=["hello back"])
        svc = ChatService(driver=driver)
        svc.execute_turn("hello")
        assert len(driver.calls) == 1
        assert driver.calls[0]["kwargs"].get("bare") is True

    def test_bare_is_boolean_true_not_truthy_stand_in(self) -> None:
        # Guards against a sloppy implementation passing bare=1 or bare="True".
        driver = _KwargsRecordingDriver(replies=["hello back"])
        svc = ChatService(driver=driver)
        svc.execute_turn("hello")
        bare_value = driver.calls[0]["kwargs"]["bare"]
        assert bare_value is True
        assert isinstance(bare_value, bool)

    def test_system_prompt_unchanged_regression(self) -> None:
        # Regression: adding bare=True must not alter the system prompt passed.
        driver = _KwargsRecordingDriver(replies=["hello back"])
        svc = ChatService(driver=driver)
        svc.execute_turn("hello")
        assert driver.calls[0]["system"] == SYSTEM_PROMPT

    def test_model_tag_unchanged_regression_injected_driver(self) -> None:
        # Regression: the resolved/injected model tag must still be forwarded
        # unchanged alongside the new bare=True kwarg.
        driver = _KwargsRecordingDriver(replies=["hello back"], model="some-tag")
        svc = ChatService(driver=driver)
        svc.execute_turn("hello")
        assert driver.calls[0]["model"] == "some-tag"

    def test_model_tag_unchanged_regression_resolved_driver(self, monkeypatch) -> None:
        driver = _KwargsRecordingDriver(replies=["resolved-reply"])

        class _FakeResolution:
            provider = "claude"
            model = "sonnet"

        monkeypatch.setattr(
            "app.role_registry.resolve_role",
            lambda role, *, registry=None, **kwargs: _FakeResolution(),
        )
        monkeypatch.setattr(
            "app.backend.get_backend", lambda role, *, name=None, **kwargs: driver
        )

        svc = ChatService()
        svc.execute_turn("hello")
        assert driver.calls[0]["model"] == "sonnet"
        assert driver.calls[0]["kwargs"].get("bare") is True

    def test_prompt_unchanged_regression(self) -> None:
        driver = _KwargsRecordingDriver(replies=["hello back"])
        svc = ChatService(driver=driver)
        svc.execute_turn("hello")
        assert driver.calls[0]["prompt"] == "hello"

    def test_no_other_kwargs_introduced(self) -> None:
        # Only bare=True should be added; no other new keyword arguments.
        driver = _KwargsRecordingDriver(replies=["hello back"])
        svc = ChatService(driver=driver)
        svc.execute_turn("hello")
        assert driver.calls[0]["kwargs"] == {"bare": True}


# --------------------------------------------------------------------------- #
# Multi-turn (agent loop) — bare=True must hold on every driver.complete call,
# since the changed line sits inside the turn loop.
# --------------------------------------------------------------------------- #
class TestBarePassthroughAcrossLoopTurns:
    def test_bare_true_on_every_turn_with_tool_calls(self, monkeypatch) -> None:
        def _execute(http_client, api_base_url, **kwargs):
            return {"pong": True}

        monkeypatch.setitem(chat_module.TOOLS, "ping", {"execute": _execute})
        try:
            driver = _KwargsRecordingDriver(replies=[_tool_call("ping", {}), "done"])
            svc = ChatService(driver=driver, http_client=object(), api_base_url="http://x.test")
            svc.execute_turn("hello")
            assert len(driver.calls) == 2
            for call in driver.calls:
                assert call["kwargs"].get("bare") is True
        finally:
            chat_module.TOOLS.pop("ping", None)


# --------------------------------------------------------------------------- #
# Boundary — empty message still gets bare=True (no special-casing).
# --------------------------------------------------------------------------- #
class TestBarePassthroughBoundary:
    def test_bare_true_with_empty_message(self) -> None:
        driver = _KwargsRecordingDriver(replies=["reply"])
        svc = ChatService(driver=driver)
        svc.execute_turn("")
        assert driver.calls[0]["kwargs"].get("bare") is True


# --------------------------------------------------------------------------- #
# Source-level checks — the exact anchored edit, the explanatory comment, and
# the "only this call site" constraint.
# --------------------------------------------------------------------------- #
class TestSourceEditShape:
    def test_exact_call_site_line_present(self) -> None:
        source = inspect.getsource(chat_module)
        assert (
            "response = driver.complete(prompt=current_prompt, "
            "system=SYSTEM_PROMPT, model=model_tag, bare=True)"
        ) in source

    def test_old_line_without_bare_no_longer_present(self) -> None:
        source = inspect.getsource(chat_module)
        assert (
            "response = driver.complete(prompt=current_prompt, "
            "system=SYSTEM_PROMPT, model=model_tag)"
        ) not in source

    def test_exactly_one_driver_complete_call_site(self) -> None:
        # Guards "do not touch any other call site" — there must be exactly
        # one driver.complete(...) invocation in the whole module.
        source = inspect.getsource(chat_module)
        assert source.count("driver.complete(") == 1

    def test_explanatory_comment_immediately_precedes_call_site(self) -> None:
        source = inspect.getsource(chat_module)
        lines = source.splitlines()
        call_line_indices = [
            i for i, line in enumerate(lines) if "driver.complete(" in line
        ]
        assert len(call_line_indices) == 1
        call_index = call_line_indices[0]
        preceding_line = lines[call_index - 1].strip()
        assert preceding_line.startswith("#"), (
            "Expected a one-line comment directly above the driver.complete(...) "
            f"call explaining the bare=True rationale, got: {preceding_line!r}"
        )

    def test_explanatory_comment_mentions_bare_and_claude_md(self) -> None:
        source = inspect.getsource(chat_module)
        lines = source.splitlines()
        call_line_indices = [
            i for i, line in enumerate(lines) if "driver.complete(" in line
        ]
        preceding_line = lines[call_line_indices[0] - 1].strip().lower()
        assert "--bare" in preceding_line or "bare" in preceding_line
        assert "claude.md" in preceding_line

    def test_no_other_files_modified_backend_claude_untouched(self) -> None:
        # The `bare` kwarg's Claude-side handling already exists (prior
        # story); this story must not touch backend_claude.py's behavior.
        # Sanity: importing it must not raise and its complete() signature
        # must still accept a `bare` keyword (already-shipped plumbing).
        from app import backend_claude

        sig = inspect.signature(backend_claude.ClaudeCliDriver.complete)
        assert "bare" in sig.parameters

    def test_no_other_files_modified_backend_ollama_untouched(self) -> None:
        from app import backend_ollama

        sig = inspect.signature(backend_ollama.OllamaDriver.complete)
        assert "bare" in sig.parameters
