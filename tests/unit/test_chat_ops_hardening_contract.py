"""Authoritative regression tests for the Secure-by-Design hardening of the
chat ops tool set.

This story removes two human-confirmation / bypass controls from the chat
``TOOLS`` registry and from ``SYSTEM_PROMPT`` so that an attacker-controlled
LLM (prompt injection) can never reach them:

* ``approve_merge`` -- the human-confirmation override for a parked
  ``risk=high`` story.  Letting the model call it hands the human
  confirmation step to the attacker.
* ``set_story_status`` -- can move a story to ``pr_open``, a status the
  unattended scheduler will merge on its own next tick.  Same bypass risk.

These tests pin the *post-removal* (hardened) contract directly against
``app.chat``'s public surface.  They are the authoritative spec the stale
ops-tool tests in ``tests/unit/test_chat_ops_tools.py`` must be aligned to:
any test that still asserts ``approve_merge`` / ``set_story_status`` are
*registered* or *advertised* contradicts this contract and is itself the bug.

They are self-contained: they build a fake ``httpx`` transport and exercise
the real ``_execute_tool`` deny-by-default path.  They must pass against the
hardened ``app/chat.py``; if they fail it is because the two tools have been
re-added to the registry or re-advertised in the prompt -- a security
regression.
"""
from __future__ import annotations

import inspect

import httpx
import pytest

import app.chat as chat_module
from app.chat import SYSTEM_PROMPT, TOOLS, _execute_tool


# The two tools this hardening removes from the chat surface.
_REMOVED_TOOLS = ["approve_merge", "set_story_status"]


# --------------------------------------------------------------------------- #
# Fakes (redefined locally - do not import across test files)
# --------------------------------------------------------------------------- #
class _FakeTransport:
    """An ``httpx`` transport that records every request and returns a JSON
    body.  Used to assert that NO request is issued for the removed tools."""

    def __init__(self, payload=None) -> None:
        self.payload = payload if payload is not None else {"ok": True}
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            content=b"{}",
            headers={"content-type": "application/json"},
            request=request,
        )


def _client(transport: _FakeTransport) -> httpx.Client:
    return httpx.Client(transport=transport, base_url="http://test")


# =========================================================================== #
# Registry: the two bypass controls must NOT be chat tools
# =========================================================================== #
class TestRemovedBypassToolsNotRegistered:
    @pytest.mark.parametrize("name", _REMOVED_TOOLS)
    def test_not_a_key_in_tools(self, name: str) -> None:
        assert name not in TOOLS, (
            f"{name!r} must be deleted from the TOOLS dict; it is a "
            "human-confirmation/bypass control that an attacker-controlled "
            "LLM must not reach"
        )

    @pytest.mark.parametrize("name", _REMOVED_TOOLS)
    def test_no_tool_entry_with_required_keys(self, name: str) -> None:
        # The whole entry (description/params/execute) must be gone, not just
        # the execute callable.
        assert name not in TOOLS
        entry = TOOLS.get(name)
        assert entry is None, (
            f"{name!r} must not have a TOOLS entry of any shape; got {entry!r}"
        )


# =========================================================================== #
# _execute_tool deny-by-default: removed tools return an error and make no
# HTTP request (the existing ``name not in TOOLS`` branch must fire)
# =========================================================================== #
class TestExecuteToolDeniesRemovedBypassTools:
    def test_approve_merge_denied_without_request(self) -> None:
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

    def test_set_story_status_denied_without_request(self) -> None:
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
    def test_error_names_the_unknown_tool(self, name: str, args: dict) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        result = _execute_tool(name, args, client, "http://test")
        assert "error" in result
        # The deny-by-default branch returns "unknown tool: <name>".
        assert name in result["error"], (
            f"error message {result['error']!r} should name the unknown "
            f"tool {name!r}"
        )

    def test_approve_merge_empty_args_still_denies_without_request(self) -> None:
        # Boundary: deny-by-default must fire before any kwarg validation, so
        # no TypeError and no request even with an empty args dict.
        transport = _FakeTransport()
        client = _client(transport)
        result = _execute_tool("approve_merge", {}, client, "http://test")
        assert "error" in result
        assert transport.requests == []

    def test_set_story_status_missing_status_still_denies_without_request(self) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        result = _execute_tool(
            "set_story_status",
            {"plan_name": "demo", "story_key": "s1"},
            client,
            "http://test",
        )
        assert "error" in result
        assert transport.requests == []


# =========================================================================== #
# SYSTEM_PROMPT: the merge-bypass tool must NOT be advertised to the model
# =========================================================================== #
class TestSystemPromptDoesNotAdvertiseBypassTools:
    def test_approve_merge_not_in_system_prompt(self) -> None:
        # approve_merge is the merge-bypass / human-confirmation override.
        # The model must never be told it exists.
        assert "approve_merge" not in SYSTEM_PROMPT, (
            "approve_merge must NOT be advertised in SYSTEM_PROMPT; telling "
            "an attacker-controlled LLM the merge-bypass tool exists defeats "
            "the human-confirmation step"
        )

    def test_set_story_status_not_in_system_prompt(self) -> None:
        assert "set_story_status" not in SYSTEM_PROMPT, (
            "set_story_status must NOT be advertised in SYSTEM_PROMPT; it can "
            "move a story to pr_open which the scheduler auto-merges"
        )

    def test_control_actions_clause_does_not_list_approve_merge(self) -> None:
        # The control-actions sentence enumerates example actions in
        # parentheses.  approve_merge must no longer appear inside that
        # clause -- re-advertising it there is the exact security regression
        # the review flagged.
        assert "control actions" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must still contain the control-actions clause"
        )
        clause_start = SYSTEM_PROMPT.index("control actions")
        clause_end = SYSTEM_PROMPT.index(")", clause_start)
        clause = SYSTEM_PROMPT[clause_start:clause_end]
        assert "approve_merge" not in clause, (
            "the control-actions clause must NOT advertise approve_merge; "
            f"got clause: {clause!r}"
        )

    def test_control_actions_clause_still_lists_surviving_examples(self) -> None:
        # The clause must remain for the tools that DO survive -- membership,
        # not exact-match, so sibling stories can extend it.
        for phrase in ["dispatch", "interrupt", "patch", "review", "mark done"]:
            assert phrase in SYSTEM_PROMPT, (
                f"SYSTEM_PROMPT must still mention {phrase!r} in the "
                "control-actions sentence"
            )

    def test_control_actions_anchor_sentences_present(self) -> None:
        # The surrounding gate/bypass guidance must remain intact.
        assert "control actions" in SYSTEM_PROMPT
        assert "subject to server-side gates" in SYSTEM_PROMPT
        assert "do NOT attempt to bypass it" in SYSTEM_PROMPT


# =========================================================================== #
# Module source: no leftover tool definitions for the removed bypass controls
# =========================================================================== #
class TestNoLeftoverBypassToolDefinitions:
    def test_no_approve_merge_tools_entry(self) -> None:
        source = inspect.getsource(chat_module)
        # The description string unique to the approve_merge TOOLS entry must
        # be gone -- the whole entry (with its execute lambda) is deleted.
        assert "Approve merge for a story." not in source, (
            "the approve_merge TOOLS entry (description 'Approve merge for a "
            "story.') must be deleted from app/chat.py"
        )

    def test_no_set_story_status_tools_entry(self) -> None:
        source = inspect.getsource(chat_module)
        assert "Set story status." not in source, (
            "the set_story_status TOOLS entry (description 'Set story "
            "status.') must be deleted from app/chat.py"
        )