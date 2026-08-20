"""Tests for the operational (ops) tool set added to the chat tool registry.

This story adds a set of thin HTTP-wrapper tools to ``app/chat.py``'s
``TOOLS`` registry plus one new paragraph in ``SYSTEM_PROMPT`` describing the
ops Q&A / control-action role.  Each tool calls the same dashboard HTTP
endpoint the UI buttons call, via the injected ``http_client``.

These tests must fail for the right reason (a missing tool / attribute /
paragraph) until the implementation is added.  They are self-contained: they
build a fake ``httpx`` transport and a scripted driver rather than touching
any real network or service layer.
"""
from __future__ import annotations

import inspect
import json
from typing import ClassVar

import httpx
import pytest

import app.chat as chat_module
from app.chat import SYSTEM_PROMPT, TOOLS, ChatService


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeResponse:
    """A minimal stand-in for an ``httpx.Response`` exposing ``.json()``."""

    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttpClient:
    """Records every ``.get``/``.post`` call and returns a scripted response.

    ``responses`` maps the exact URL passed to the method to the payload that
    the returned response's ``.json()`` should yield. Any unrecorded URL
    raises ``AssertionError`` so a wrong URL is caught loudly.
    """

    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.responses = responses or {}
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict | None]] = []

    def get(self, url, *args, **kwargs):
        self.get_calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected GET to {url!r}")
        return _FakeResponse(self.responses[url])

    def post(self, url, *args, **kwargs):
        body = kwargs.get("json")
        self.post_calls.append((url, body))
        if url not in self.responses:
            raise AssertionError(f"unexpected POST to {url!r}")
        return _FakeResponse(self.responses[url])


class _FakeTransport:
    """An ``httpx`` transport that records requests and returns JSON bodies.

    Used to drive the real ``TOOLS['<name>']['execute']`` end-to-end through
    an ``httpx.Client(transport=...)`` so we assert the actual HTTP method,
    path, and request body the tool emits.
    """

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


class _FakeDriver:
    """A driver that returns a scripted sequence of replies."""

    def __init__(self, replies: list[str], model: str = "test-model") -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.model = model

    def complete(self, prompt: str, *, system, model, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system, "model": model})
        if self.replies:
            return self.replies.pop(0)
        return self.calls[-1]["prompt"]


def _tool_call(name: str, args: dict) -> str:
    return f"[TOOL_CALL]{json.dumps({'name': name, 'args': args})}[/TOOL_CALL]"


# The complete set of ops tools this story must register.
OPS_TOOLS = [
    # story-level write actions
    "dispatch_story",
    "interrupt_story",
    "patch_story",
    "review_story",
    "mark_story_done",
    "checkpoint",
    # plan-level write actions
    "advance_pipeline",
    "advance_all_plans",
    "pause_plan",
    "resume_plan",
    # story-level read actions
    "get_story_journal",
    "get_story_log",
    "get_story_checklist",
]


# =========================================================================== #
# Registry shape
# =========================================================================== #
class TestOpsToolsRegistryShape:
    @pytest.mark.parametrize("name", OPS_TOOLS)
    def test_tool_present(self, name: str) -> None:
        assert name in TOOLS, f"TOOLS missing {name!r}"

    @pytest.mark.parametrize("name", OPS_TOOLS)
    def test_entry_has_required_keys(self, name: str) -> None:
        assert name in TOOLS
        entry = TOOLS[name]
        assert set(entry.keys()) >= {"description", "params", "execute"}
        assert isinstance(entry["description"], str) and entry["description"]
        assert isinstance(entry["params"], dict)
        assert callable(entry["execute"])

    def test_existing_tools_still_present(self) -> None:
        # The pre-existing read-only and plan-authoring tools must remain.
        assert {
            "list_plans",
            "get_plan",
            "health",
            "decompose",
            "save_plan",
            "ingest_plan",
        } <= set(TOOLS.keys())

    @pytest.mark.parametrize(
        "name,required_params",
        [
            ("dispatch_story", {"plan_name", "story_key"}),
            ("interrupt_story", {"plan_name", "story_key"}),
            ("patch_story", {"plan_name", "story_key", "fields"}),
            ("review_story", {"plan_name", "story_key"}),
            ("mark_story_done", {"plan_name", "story_key"}),
            ("checkpoint", {"plan_name", "story_key", "step", "summary"}),
            ("advance_pipeline", {"plan_name"}),
            ("advance_all_plans", set()),
            ("pause_plan", {"plan_name"}),
            ("resume_plan", {"plan_name"}),
            ("get_story_journal", {"plan_name", "story_key"}),
            ("get_story_log", {"plan_name", "story_key"}),
            ("get_story_checklist", {"plan_name", "story_key"}),
        ],
    )
    def test_params_declare_expected_fields(self, name: str, required_params: set) -> None:
        assert name in TOOLS, f"TOOLS missing {name!r}"
        params = TOOLS[name]["params"]
        assert required_params <= set(params.keys()), (
            f"{name} params {set(params.keys())} missing required {required_params - set(params.keys())}"
        )

    def test_checkpoint_declares_optional_next_hint(self) -> None:
        assert "checkpoint" in TOOLS
        params = TOOLS["checkpoint"]["params"]
        assert "next_hint" in params


# =========================================================================== #
# Story-level write tools (POST)
# =========================================================================== #
class TestDispatchStoryTool:
    def test_posts_to_dispatch_endpoint(self) -> None:
        url = "/api/plans/demo/stories/s4/dispatch"
        client = _FakeHttpClient({url: {"ok": True}})
        out = TOOLS["dispatch_story"]["execute"](
            client, "http://base.test", plan_name="demo", story_key="s4"
        )
        assert out == {"ok": True}
        assert client.post_calls == [(url, None)] or client.post_calls[0][0] == url

    def test_missing_story_key_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["dispatch_story"]["execute"](
                client, "http://base.test", plan_name="demo"
            )

    def test_missing_plan_name_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["dispatch_story"]["execute"](
                client, "http://base.test", story_key="s4"
            )


class TestInterruptStoryTool:
    def test_posts_to_interrupt_endpoint(self) -> None:
        url = "/api/plans/demo/stories/s4/interrupt"
        client = _FakeHttpClient({url: {"ok": True}})
        out = TOOLS["interrupt_story"]["execute"](
            client, "http://base.test", plan_name="demo", story_key="s4"
        )
        assert out == {"ok": True}
        assert client.post_calls[0][0] == url

    def test_missing_story_key_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["interrupt_story"]["execute"](
                client, "http://base.test", plan_name="demo"
            )


class TestPatchStoryTool:
    def test_posts_to_patch_endpoint_with_fields_body(self) -> None:
        url = "/api/plans/demo/stories/s4/patch"
        client = _FakeHttpClient({url: {"ok": True}})
        fields = {"status": "blocked", "notes": "waiting on dep"}
        out = TOOLS["patch_story"]["execute"](
            client, "http://base.test", plan_name="demo", story_key="s4", fields=fields
        )
        assert out == {"ok": True}
        posted_url, body = client.post_calls[0]
        assert posted_url == url
        assert body == fields

    def test_empty_fields_dict_forwarded(self) -> None:
        # Boundary: empty dict is still forwarded.
        url = "/api/plans/demo/stories/s4/patch"
        client = _FakeHttpClient({url: {"ok": True}})
        TOOLS["patch_story"]["execute"](
            client, "http://base.test", plan_name="demo", story_key="s4", fields={}
        )
        _posted_url, body = client.post_calls[0]
        assert body == {}

    def test_missing_fields_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["patch_story"]["execute"](
                client, "http://base.test", plan_name="demo", story_key="s4"
            )


class TestSetStoryStatusTool:
    def test_posts_to_status_endpoint_with_status_body(self) -> None:
        url = "/api/plans/demo/stories/s4/status"
        client = _FakeHttpClient({url: {"ok": True}})
        out = TOOLS["set_story_status"]["execute"](
            client, "http://base.test", plan_name="demo", story_key="s4", status="blocked"
        )
        assert out == {"ok": True}
        posted_url, body = client.post_calls[0]
        assert posted_url == url
        assert body == {"status": "blocked"}

    def test_missing_status_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["set_story_status"]["execute"](
                client, "http://base.test", plan_name="demo", story_key="s4"
            )


class TestReviewStoryTool:
    def test_posts_to_review_endpoint(self) -> None:
        url = "/api/plans/demo/stories/s4/review"
        client = _FakeHttpClient({url: {"ok": True}})
        out = TOOLS["review_story"]["execute"](
            client, "http://base.test", plan_name="demo", story_key="s4"
        )
        assert out == {"ok": True}
        assert client.post_calls[0][0] == url


class TestApproveMergeTool:
    def test_posts_to_approve_merge_endpoint(self) -> None:
        url = "/api/plans/demo/stories/s4/approve_merge"
        client = _FakeHttpClient({url: {"ok": True}})
        out = TOOLS["approve_merge"]["execute"](
            client, "http://base.test", plan_name="demo", story_key="s4"
        )
        assert out == {"ok": True}
        assert client.post_calls[0][0] == url


class TestMarkStoryDoneTool:
    def test_posts_to_done_endpoint(self) -> None:
        url = "/api/plans/demo/stories/s4/done"
        client = _FakeHttpClient({url: {"ok": True}})
        out = TOOLS["mark_story_done"]["execute"](
            client, "http://base.test", plan_name="demo", story_key="s4"
        )
        assert out == {"ok": True}
        assert client.post_calls[0][0] == url


class TestCheckpointTool:
    def test_posts_to_checkpoint_endpoint_with_body(self) -> None:
        url = "/api/plans/demo/stories/s4/checkpoint"
        client = _FakeHttpClient({url: {"ok": True}})
        out = TOOLS["checkpoint"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="s4",
            step="implement",
            summary="wrote the thing",
            next_hint="run tests",
        )
        assert out == {"ok": True}
        posted_url, body = client.post_calls[0]
        assert posted_url == url
        assert body == {
            "step": "implement",
            "summary": "wrote the thing",
            "next_hint": "run tests",
        }

    def test_next_hint_optional(self) -> None:
        url = "/api/plans/demo/stories/s4/checkpoint"
        client = _FakeHttpClient({url: {"ok": True}})
        TOOLS["checkpoint"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="s4",
            step="implement",
            summary="wrote the thing",
        )
        posted_url, body = client.post_calls[0]
        assert posted_url == url
        assert body["step"] == "implement"
        assert body["summary"] == "wrote the thing"
        # next_hint may be omitted or None; either is acceptable.
        assert body.get("next_hint") in (None, "next_hint" not in body and None)

    def test_missing_step_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["checkpoint"]["execute"](
                client,
                "http://base.test",
                plan_name="demo",
                story_key="s4",
                summary="x",
            )

    def test_missing_summary_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["checkpoint"]["execute"](
                client,
                "http://base.test",
                plan_name="demo",
                story_key="s4",
                step="x",
            )


# =========================================================================== #
# Plan-level write tools (POST)
# =========================================================================== #
class TestAdvancePipelineTool:
    def test_posts_to_advance_endpoint(self) -> None:
        url = "/api/plans/demo/advance"
        client = _FakeHttpClient({url: {"ok": True}})
        out = TOOLS["advance_pipeline"]["execute"](
            client, "http://base.test", plan_name="demo"
        )
        assert out == {"ok": True}
        assert client.post_calls[0][0] == url

    def test_missing_plan_name_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["advance_pipeline"]["execute"](client, "http://base.test")


class TestAdvanceAllPlansTool:
    def test_posts_to_advance_all_endpoint(self) -> None:
        url = "/api/plans/advance_all"
        client = _FakeHttpClient({url: {"ok": True}})
        out = TOOLS["advance_all_plans"]["execute"](
            client, "http://base.test"
        )
        assert out == {"ok": True}
        assert client.post_calls[0][0] == url


class TestPausePlanTool:
    def test_posts_to_pause_endpoint(self) -> None:
        url = "/api/plans/demo/pause"
        client = _FakeHttpClient({url: {"ok": True}})
        out = TOOLS["pause_plan"]["execute"](
            client, "http://base.test", plan_name="demo"
        )
        assert out == {"ok": True}
        assert client.post_calls[0][0] == url


class TestResumePlanTool:
    def test_posts_to_resume_endpoint(self) -> None:
        url = "/api/plans/demo/resume"
        client = _FakeHttpClient({url: {"ok": True}})
        out = TOOLS["resume_plan"]["execute"](
            client, "http://base.test", plan_name="demo"
        )
        assert out == {"ok": True}
        assert client.post_calls[0][0] == url


# =========================================================================== #
# Story-level read tools (GET)
# =========================================================================== #
class TestGetStoryJournalTool:
    def test_gets_journal_endpoint(self) -> None:
        url = "/api/plans/demo/stories/s4/journal"
        client = _FakeHttpClient({url: {"entries": []}})
        out = TOOLS["get_story_journal"]["execute"](
            client, "http://base.test", plan_name="demo", story_key="s4"
        )
        assert out == {"entries": []}
        assert client.get_calls == [url]

    def test_missing_story_key_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["get_story_journal"]["execute"](
                client, "http://base.test", plan_name="demo"
            )


class TestGetStoryLogTool:
    def test_gets_log_endpoint(self) -> None:
        url = "/api/plans/demo/stories/s4/log"
        client = _FakeHttpClient({url: {"lines": []}})
        out = TOOLS["get_story_log"]["execute"](
            client, "http://base.test", plan_name="demo", story_key="s4"
        )
        assert out == {"lines": []}
        assert client.get_calls == [url]


class TestGetStoryChecklistTool:
    def test_gets_checklist_endpoint(self) -> None:
        url = "/api/plans/demo/stories/s4/checklist"
        client = _FakeHttpClient({url: {"items": []}})
        out = TOOLS["get_story_checklist"]["execute"](
            client, "http://base.test", plan_name="demo", story_key="s4"
        )
        assert out == {"items": []}
        assert client.get_calls == [url]


# =========================================================================== #
# End-to-end through the agent loop with a real httpx.Client + fake transport
# =========================================================================== #
class TestEndToEndViaLoop:
    def _run(self, tool_name: str, args: dict, reply: str = "done"):
        transport = _FakeTransport(payload={"ok": True})
        client = httpx.Client(transport=transport)
        driver = _FakeDriver(
            replies=[_tool_call(tool_name, args), reply]
        )
        svc = ChatService(
            driver=driver, http_client=client, api_base_url="http://x.test"
        )
        out = svc.execute_turn("do the thing")
        return out, transport

    # --- story-level write actions ---
    def test_dispatch_story_called_through_loop(self) -> None:
        out, transport = self._run(
            "dispatch_story", {"plan_name": "demo", "story_key": "s4"}
        )
        assert out["reply"] == "done"
        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/stories/s4/dispatch"

    def test_interrupt_story_called_through_loop(self) -> None:
        out, transport = self._run(
            "interrupt_story", {"plan_name": "demo", "story_key": "s4"}
        )
        assert out["reply"] == "done"
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/stories/s4/interrupt"

    def test_patch_story_called_through_loop_with_fields_body(self) -> None:
        out, transport = self._run(
            "patch_story",
            {"plan_name": "demo", "story_key": "s4", "fields": {"status": "blocked"}},
        )
        assert out["reply"] == "done"
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/stories/s4/patch"
        body = json.loads(req.content.decode())
        assert body == {"status": "blocked"}

    def test_review_story_called_through_loop(self) -> None:
        out, transport = self._run(
            "review_story", {"plan_name": "demo", "story_key": "s4"}
        )
        assert out["reply"] == "done"
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/stories/s4/review"

    def test_approve_merge_called_through_loop(self) -> None:
        out, transport = self._run(
            "approve_merge", {"plan_name": "demo", "story_key": "s4"}
        )
        assert out["reply"] == "done"
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/stories/s4/approve_merge"

    # --- plan-level write actions ---
    def test_advance_pipeline_called_through_loop(self) -> None:
        out, transport = self._run(
            "advance_pipeline", {"plan_name": "demo"}
        )
        assert out["reply"] == "done"
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/advance"

    def test_pause_plan_called_through_loop(self) -> None:
        out, transport = self._run(
            "pause_plan", {"plan_name": "demo"}
        )
        assert out["reply"] == "done"
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/pause"

    # --- story-level read actions ---
    def test_get_story_journal_called_through_loop(self) -> None:
        out, transport = self._run(
            "get_story_journal", {"plan_name": "demo", "story_key": "s4"}
        )
        assert out["reply"] == "done"
        req = transport.requests[0]
        assert req.method == "GET"
        assert req.url.path == "/api/plans/demo/stories/s4/journal"

    def test_get_story_log_called_through_loop(self) -> None:
        out, transport = self._run(
            "get_story_log", {"plan_name": "demo", "story_key": "s4"}
        )
        assert out["reply"] == "done"
        req = transport.requests[0]
        assert req.method == "GET"
        assert req.url.path == "/api/plans/demo/stories/s4/log"

    def test_get_story_checklist_called_through_loop(self) -> None:
        out, transport = self._run(
            "get_story_checklist", {"plan_name": "demo", "story_key": "s4"}
        )
        assert out["reply"] == "done"
        req = transport.requests[0]
        assert req.method == "GET"
        assert req.url.path == "/api/plans/demo/stories/s4/checklist"

    def test_checkpoint_called_through_loop_with_body(self) -> None:
        out, transport = self._run(
            "checkpoint",
            {
                "plan_name": "demo",
                "story_key": "s4",
                "step": "implement",
                "summary": "wrote it",
                "next_hint": "test it",
            },
        )
        assert out["reply"] == "done"
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/stories/s4/checkpoint"
        body = json.loads(req.content.decode())
        assert body == {
            "step": "implement",
            "summary": "wrote it",
            "next_hint": "test it",
        }

    def test_advance_all_plans_called_through_loop(self) -> None:
        out, transport = self._run("advance_all_plans", {})
        assert out["reply"] == "done"
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/advance_all"


# =========================================================================== #
# Negative / boundary cases through the loop
# =========================================================================== #
class TestNegativeCases:
    def test_missing_required_arg_becomes_tool_result_error(self) -> None:
        """dispatch_story without story_key must not crash the loop; it feeds
        back a tool_result error and the loop continues."""
        transport = _FakeTransport(payload={"ok": True})
        client = httpx.Client(transport=transport)
        driver = _FakeDriver(
            replies=[
                # Missing story_key on purpose.
                _tool_call("dispatch_story", {"plan_name": "demo"}),
                "sorry, that failed",
            ]
        )
        svc = ChatService(
            driver=driver, http_client=client, api_base_url="http://x.test"
        )
        out = svc.execute_turn("dispatch it")
        # Loop must not crash; it produces a reply and records the failed call.
        assert out["reply"] == "sorry, that failed"
        assert len(out["tool_calls"]) == 1
        result = out["tool_calls"][0]["result"]
        assert "error" in result
        # No HTTP request should have been issued for the failed call.
        assert len(transport.requests) == 0

    def test_unknown_tool_name_becomes_tool_result_error(self) -> None:
        """An unknown tool name must be fed back as an unknown-tool error."""
        transport = _FakeTransport(payload={"ok": True})
        client = httpx.Client(transport=transport)
        driver = _FakeDriver(
            replies=[
                _tool_call("does_not_exist", {"x": 1}),
                "I cannot do that",
            ]
        )
        svc = ChatService(
            driver=driver, http_client=client, api_base_url="http://x.test"
        )
        out = svc.execute_turn("do something weird")
        assert out["reply"] == "I cannot do that"
        assert len(out["tool_calls"]) == 1
        result = out["tool_calls"][0]["result"]
        assert "error" in result
        assert "unknown tool" in result["error"]
        assert len(transport.requests) == 0


# =========================================================================== #
# SYSTEM_PROMPT update
# =========================================================================== #
class TestSystemPromptUpdate:
    FINAL_SENTENCE = (
        "Call tools to gather information, then provide a natural-language reply."
    )

    # Phrases the new ops paragraph must contain.
    OPS_PHRASES: ClassVar[list[str]] = [
        "journals, logs, and checklists",
        "control actions",
        "dispatch",
        "interrupt",
        "approve_merge",
        "subject to server-side gates",
        "do NOT attempt to bypass it",
    ]

    def test_contains_ops_paragraph(self) -> None:
        for phrase in self.OPS_PHRASES:
            assert phrase in SYSTEM_PROMPT, f"SYSTEM_PROMPT missing phrase {phrase!r}"

    def test_ops_paragraph_precedes_final_sentence(self) -> None:
        para_idx = SYSTEM_PROMPT.index("journals, logs, and checklists")
        final_idx = SYSTEM_PROMPT.index(self.FINAL_SENTENCE)
        assert para_idx < final_idx, (
            "the ops paragraph must come before the final 'Call tools...' sentence"
        )

    def test_still_ends_with_final_sentence(self) -> None:
        assert SYSTEM_PROMPT.endswith(self.FINAL_SENTENCE)

    def test_ops_paragraph_appears_exactly_once(self) -> None:
        assert SYSTEM_PROMPT.count("journals, logs, and checklists") == 1


# =========================================================================== #
# Regression: approve_merge must remain advertised in the ops paragraph
# =========================================================================== #
class TestOpsParagraphAdvertisesApproveMerge:
    """Regression test for the removal of ``approve_merge`` from the ops
    paragraph of ``_SYSTEM_PROMPT_PREFIX``.

    The base commit (3acf30d) advertised ``approve_merge`` as one of the
    control actions in the "You can execute control actions (...)" sentence.
    A subsequent edit removed it from that sentence, which breaks the
    contract encoded by ``TestSystemPromptUpdate.OPS_PHRASES``.  This test
    pins the original contract directly against the control-actions clause
    so the regression is reproduced in isolation.
    """

    def test_control_actions_clause_lists_approve_merge(self) -> None:
        # The control-actions sentence enumerates the callable ops actions
        # inside parentheses, e.g.
        #   "control actions (dispatch, interrupt, patch, review,
        #    approve_merge, advance, pause, resume, mark done)"
        assert "control actions" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must contain the control-actions clause"
        )
        clause_start = SYSTEM_PROMPT.index("control actions")
        # The parenthesised action list ends at the first ')' after the
        # clause; everything inside must still mention approve_merge.
        clause_end = SYSTEM_PROMPT.index(")", clause_start)
        clause = SYSTEM_PROMPT[clause_start:clause_end]
        assert "approve_merge" in clause, (
            "the control-actions clause must still advertise approve_merge; "
            f"got clause: {clause!r}"
        )

    def test_approve_merge_phrase_present_in_prompt(self) -> None:
        # Direct, redundant pin of the phrase the reviewer flagged as removed.
        assert "approve_merge" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must still contain the phrase 'approve_merge' "
            "in its ops paragraph"
        )


# =========================================================================== #
# Module-level invariants
# =========================================================================== #
class TestModuleInvariants:
    def test_chat_does_not_import_pipeline_service(self) -> None:
        """app/chat.py must not reference PipelineService or _service."""
        source = inspect.getsource(chat_module)
        assert "PipelineService" not in source, (
            "app/chat.py must not reference PipelineService"
        )
        assert "_service" not in source, "app/chat.py must not reference _service"

    def test_all_ops_tools_registered(self) -> None:
        assert set(OPS_TOOLS) <= set(TOOLS.keys())

    def test_ops_tools_use_http_client_not_service(self) -> None:
        """Every ops tool execute callable must accept http_client + api_base_url
        (i.e. be a thin HTTP wrapper), not call any service directly."""
        source = inspect.getsource(chat_module)
        # The ops tools must not reference a service object.
        for name in OPS_TOOLS:
            assert name in TOOLS
        # No direct service references anywhere in the module.
        assert "_service" not in source
        assert "PipelineService" not in source