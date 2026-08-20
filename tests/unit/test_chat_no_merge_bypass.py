"""Tests for the removal of the ``approve_merge`` and ``set_story_status``
chat tools.

This story deletes those two entries from ``app/chat.py``'s ``TOOLS`` dict
entirely and drops ``approve_merge`` from the example control-actions sentence
in ``SYSTEM_PROMPT``.  After the change, calling
``chat._execute_tool('approve_merge', ...)`` and
``chat._execute_tool('set_story_status', ...)`` must fall through to the
existing deny-by-default ``name not in TOOLS`` branch (returning an error dict)
and must NOT perform any HTTP request.

These tests are self-contained: they redefine the ``_FakeTransport`` pattern
locally rather than importing it across test files.  They must fail for the
right reason (the two tools are still registered / still named in the prompt)
until the implementation lands.
"""
from __future__ import annotations

import inspect
import json

import httpx
import pytest

import app.chat as chat_module
from app.chat import SYSTEM_PROMPT, TOOLS, _execute_tool


# --------------------------------------------------------------------------- #
# Fakes (redefined locally - do not import across test files)
# --------------------------------------------------------------------------- #
class _FakeTransport:
    """An ``httpx`` transport that records every request and returns a JSON
    body.  Used to drive ``_execute_tool`` end-to-end through a real
    ``httpx.Client(transport=...)`` so we can assert that NO request was
    issued for the removed tools."""

    def __init__(self, payload=None) -> None:
        self.payload = payload if payload is not None else {"ok": True}
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            content=json.dumps(self.payload).encode(),
            headers={"content-type": "application/json"},
            request=request,
        )


def _client(transport: _FakeTransport) -> httpx.Client:
    return httpx.Client(transport=transport, base_url="http://test")


# The two tools this story removes.
_REMOVED_TOOLS = ["approve_merge", "set_story_status"]

# A representative sample of the tools that existed before this story and must
# remain present and callable afterwards.  This is a regression guard: the
# story must not remove anything beyond the two named tools.
_SURVIVING_TOOLS = [
    "list_plans",
    "get_plan",
    "list_decisions",
    "answer_decision",
    "health",
    "decompose",
    "save_plan",
    "ingest_plan",
    "dispatch_story",
    "interrupt_story",
    "patch_story",
    "review_story",
    "mark_story_done",
    "checkpoint",
    "advance_pipeline",
    "advance_all_plans",
    "pause_plan",
    "resume_plan",
    "get_story_journal",
    "get_story_log",
    "get_story_checklist",
]


# =========================================================================== #
# Registry removal
# =========================================================================== #
class TestRemovedToolsNotInRegistry:
    @pytest.mark.parametrize("name", _REMOVED_TOOLS)
    def test_tool_not_a_key_in_tools(self, name: str) -> None:
        assert name not in TOOLS, (
            f"{name!r} must be deleted from the TOOLS dict; it is still present"
        )

    @pytest.mark.parametrize("name", _REMOVED_TOOLS)
    def test_no_dead_reference_in_module_source(self, name: str) -> None:
        """The removed tool name must not survive as a TOOLS key.  We assert
        against the registry (the mechanically-checkable contract) rather than
        grepping the whole source, since the name may legitimately appear in
        comments/docstrings of this very test story."""
        assert name not in TOOLS


# =========================================================================== #
# _execute_tool deny-by-default for the removed tools
# =========================================================================== #
class TestExecuteToolDeniesRemovedTools:
    def test_approve_merge_returns_error_and_makes_no_request(self) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        result = _execute_tool(
            "approve_merge",
            {"plan_name": "demo", "story_key": "s1"},
            client,
            "http://test",
        )
        assert isinstance(result, dict)
        assert "error" in result, (
            f"expected an error dict for the removed tool, got {result!r}"
        )
        assert transport.requests == [], (
            "approve_merge must not perform any HTTP request after removal"
        )

    def test_set_story_status_returns_error_and_makes_no_request(self) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        result = _execute_tool(
            "set_story_status",
            {"plan_name": "demo", "story_key": "s1", "status": "pr_open"},
            client,
            "http://test",
        )
        assert isinstance(result, dict)
        assert "error" in result, (
            f"expected an error dict for the removed tool, got {result!r}"
        )
        assert transport.requests == [], (
            "set_story_status must not perform any HTTP request after removal"
        )

    @pytest.mark.parametrize(
        "name,args",
        [
            ("approve_merge", {"plan_name": "demo", "story_key": "s1"}),
            (
                "set_story_status",
                {"plan_name": "demo", "story_key": "s1", "status": "pr_open"},
            ),
        ],
    )
    def test_error_message_names_the_unknown_tool(self, name: str, args: dict) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        result = _execute_tool(name, args, client, "http://test")
        assert "error" in result
        # The existing deny-by-default branch returns "unknown tool: <name>".
        assert name in result["error"], (
            f"error message {result['error']!r} should name the unknown tool {name!r}"
        )

    def test_approve_merge_missing_args_still_denies_without_request(self) -> None:
        # Boundary: even with an empty args dict the deny-by-default branch
        # must fire before any HTTP call, so no TypeError about missing kwargs
        # and no request.
        transport = _FakeTransport()
        client = _client(transport)
        result = _execute_tool("approve_merge", {}, client, "http://test")
        assert "error" in result
        assert transport.requests == []

    def test_set_story_status_missing_status_still_denies_without_request(self) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        result = _execute_tool(
            "set_story_status", {"plan_name": "demo", "story_key": "s1"}, client, "http://test"
        )
        assert "error" in result
        assert transport.requests == []


# =========================================================================== #
# Regression: surviving tools still present and callable
# =========================================================================== #
class TestSurvivingToolsIntact:
    @pytest.mark.parametrize("name", _SURVIVING_TOOLS)
    def test_tool_still_present(self, name: str) -> None:
        assert name in TOOLS, (
            f"{name!r} must remain in TOOLS - this story only removes "
            "approve_merge and set_story_status"
        )

    @pytest.mark.parametrize("name", _SURVIVING_TOOLS)
    def test_tool_entry_still_well_formed(self, name: str) -> None:
        assert name in TOOLS
        entry = TOOLS[name]
        assert set(entry.keys()) >= {"description", "params", "execute"}
        assert isinstance(entry["description"], str) and entry["description"]
        assert isinstance(entry["params"], dict)
        assert callable(entry["execute"])

    def test_no_other_tools_removed(self) -> None:
        """The only keys that may have disappeared are the two removed tools."""
        # Every surviving tool from our sample must still be registered.
        for name in _SURVIVING_TOOLS:
            assert name in TOOLS, f"{name!r} unexpectedly removed"
        # And the two removed ones must be gone.
        for name in _REMOVED_TOOLS:
            assert name not in TOOLS


# =========================================================================== #
# SYSTEM_PROMPT: approve_merge dropped from the control-actions sentence
# =========================================================================== #
class TestSystemPromptDropsApproveMerge:
    def test_approve_merge_not_in_control_actions_sentence(self) -> None:
        # The control-actions sentence enumerates example actions in
        # parentheses.  approve_merge must no longer appear there.
        assert "approve_merge" not in SYSTEM_PROMPT, (
            "approve_merge must be dropped from the SYSTEM_PROMPT "
            "control-actions sentence"
        )

    def test_control_actions_sentence_still_lists_other_examples(self) -> None:
        # The sentence must still name the other example actions (membership,
        # not exact-match, so sibling stories can extend it later).
        for phrase in ["dispatch", "interrupt", "patch", "review", "mark done"]:
            assert phrase in SYSTEM_PROMPT, (
                f"SYSTEM_PROMPT must still mention {phrase!r} in the "
                "control-actions sentence"
            )

    def test_control_actions_anchor_sentence_present(self) -> None:
        # The surrounding sentence structure must remain intact.
        assert "control actions" in SYSTEM_PROMPT
        assert "subject to server-side gates" in SYSTEM_PROMPT
        assert "do NOT attempt to bypass it" in SYSTEM_PROMPT


# =========================================================================== #
# Module source: no leftover execute lambda for the removed tools
# =========================================================================== #
class TestNoLeftoverToolDefinitions:
    def test_no_approve_merge_execute_lambda(self) -> None:
        source = inspect.getsource(chat_module)
        # The TOOLS dict entry (with its execute lambda) must be gone.  We
        # check that the description string unique to that entry is gone.
        assert "Approve merge for a story." not in source, (
            "the approve_merge TOOLS entry (description 'Approve merge for a "
            "story.') must be deleted from app/chat.py"
        )

    def test_no_set_story_status_execute_lambda(self) -> None:
        source = inspect.getsource(chat_module)
        assert "Set story status." not in source, (
            "the set_story_status TOOLS entry (description 'Set story "
            "status.') must be deleted from app/chat.py"
        )