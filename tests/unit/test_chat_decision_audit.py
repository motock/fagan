"""Tests for the ``decided_by`` audit-trail stamp on the chat
``answer_decision`` tool.

SCOPE: ``app/chat.py`` only, the ``answer_decision`` entry in ``TOOLS``.

The chat ``answer_decision`` tool POSTs to
``/api/plans/{plan_name}/decisions``. The server defaults ``decided_by`` to
``"human"`` when the caller omits it, so a chat-authored decision was being
recorded as if a human had typed it directly into the dashboard - forging
the audit trail this feature explicitly claims to preserve. This story adds
``'decided_by': 'chat'`` to the JSON body the tool posts, alongside the
existing ``story_key`` / ``question`` / ``answer`` / ``context`` fields.

These tests must fail for the right reason (the body lacks ``decided_by``)
until the implementation is added. They are self-contained: the chat-loop
tests use a real ``httpx.Client(transport=FakeTransport())`` exactly like
the chat adapter's own ops-tool tests, and the unit tests use a
``_FakeHttpClient`` that records the posted body.
"""
from __future__ import annotations

import inspect
import json

import httpx
import pytest

import app.chat as chat_module
from app.chat import TOOLS


# --------------------------------------------------------------------------- #
# Fakes - unit level (records the exact posted body)
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttpClient:
    """Records get/post calls and returns scripted JSON."""

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


# --------------------------------------------------------------------------- #
# Fakes - end-to-end through the agent loop (real httpx.Client + transport)
# --------------------------------------------------------------------------- #
class _FakeTransport:
    """httpx MockTransport-style transport that records every request."""

    def __init__(self, payload=None) -> None:
        self.requests: list[httpx.Request] = []
        self._payload = payload if payload is not None else {"ok": True}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            content=json.dumps(self._payload).encode(),
            request=request,
        )


class _FakeDriver:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    def complete(self, prompt, *, system, model, **kwargs):
        return self._replies.pop(0)


def _tool_call(name: str, args: dict) -> str:
    return f"[TOOL_CALL]{json.dumps({'name': name, 'args': args})}[/TOOL_CALL]"


# =========================================================================== #
# Unit-level: the answer_decision tool body
# =========================================================================== #
class TestAnswerDecisionDecidedBy:
    """The tool must stamp ``decided_by='chat'`` on every POST body."""

    def test_body_includes_decided_by_chat(self) -> None:
        url = "/api/plans/demo/decisions"
        client = _FakeHttpClient({url: {"ok": True, "record": {"story_key": "S1"}}})
        out = TOOLS["answer_decision"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="S1",
            question="which lib?",
            answer="use foo",
            context="because bar",
        )
        assert out == {"ok": True, "record": {"story_key": "S1"}}
        # Exactly one POST.
        assert len(client.post_calls) == 1
        posted_url, body = client.post_calls[0]
        assert posted_url == url
        assert body["decided_by"] == "chat"

    def test_full_body_dict_unchanged_plus_decided_by(self) -> None:
        """Assert the FULL body dict so a future edit can't silently drop an
        existing field. The only change from prior behavior is the addition
        of ``decided_by``; story_key/question/answer/context must be
        unchanged."""
        url = "/api/plans/demo/decisions"
        client = _FakeHttpClient({url: {"ok": True, "record": {}}})
        TOOLS["answer_decision"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="S1",
            question="which lib?",
            answer="use foo",
            context="because bar",
        )
        assert len(client.post_calls) == 1
        _, body = client.post_calls[0]
        assert body == {
            "story_key": "S1",
            "question": "which lib?",
            "answer": "use foo",
            "context": "because bar",
            "decided_by": "chat",
        }

    def test_context_omitted_still_defaults_empty_alongside_decided_by(
        self,
    ) -> None:
        """Omitting ``context`` must still default it to ``""`` exactly as
        before, AND ``decided_by`` must still be ``"chat"`` - the two are
        independent."""
        url = "/api/plans/demo/decisions"
        client = _FakeHttpClient({url: {"ok": True, "record": {}}})
        TOOLS["answer_decision"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="S1",
            question="q",
            answer="a",
        )
        assert len(client.post_calls) == 1
        _, body = client.post_calls[0]
        # context still defaults to empty string (unchanged behavior).
        assert body["context"] == ""
        # decided_by is still stamped even without context (independent).
        assert body["decided_by"] == "chat"
        # Full body for completeness.
        assert body == {
            "story_key": "S1",
            "question": "q",
            "answer": "a",
            "context": "",
            "decided_by": "chat",
        }

    def test_context_explicitly_empty_string_stamps_decided_by(self) -> None:
        """Boundary: an explicitly-empty context string must still carry
        decided_by='chat'."""
        url = "/api/plans/demo/decisions"
        client = _FakeHttpClient({url: {"ok": True, "record": {}}})
        TOOLS["answer_decision"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="S1",
            question="q",
            answer="a",
            context="",
        )
        _, body = client.post_calls[0]
        assert body["context"] == ""
        assert body["decided_by"] == "chat"

    def test_context_none_stamps_decided_by(self) -> None:
        """Boundary: an explicit ``context=None`` must default to ``""`` and
        still carry decided_by='chat'."""
        url = "/api/plans/demo/decisions"
        client = _FakeHttpClient({url: {"ok": True, "record": {}}})
        TOOLS["answer_decision"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="S1",
            question="q",
            answer="a",
            context=None,
        )
        _, body = client.post_calls[0]
        assert body["context"] == ""
        assert body["decided_by"] == "chat"

    def test_decided_by_value_is_exactly_chat_not_other(self) -> None:
        """The value must be the exact string ``'chat'`` - not ``'human'``,
        not ``'Chat'``, not ``'CHAT'``."""
        url = "/api/plans/demo/decisions"
        client = _FakeHttpClient({url: {"ok": True, "record": {}}})
        TOOLS["answer_decision"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="S1",
            question="q",
            answer="a",
        )
        _, body = client.post_calls[0]
        assert body["decided_by"] == "chat"
        assert body["decided_by"] != "human"
        assert body["decided_by"] != "Chat"
        assert body["decided_by"] != "CHAT"

    def test_exactly_one_post_made(self) -> None:
        """Calling the tool must result in exactly one POST - no retries, no
        duplicate calls."""
        url = "/api/plans/demo/decisions"
        client = _FakeHttpClient({url: {"ok": True, "record": {}}})
        TOOLS["answer_decision"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="S1",
            question="q",
            answer="a",
            context="c",
        )
        assert len(client.post_calls) == 1
        assert len(client.get_calls) == 0

    def test_no_get_calls_made(self) -> None:
        """The tool must only POST, never GET."""
        url = "/api/plans/demo/decisions"
        client = _FakeHttpClient({url: {"ok": True, "record": {}}})
        TOOLS["answer_decision"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="S1",
            question="q",
            answer="a",
        )
        assert client.get_calls == []

    def test_url_path_is_decisions_endpoint(self) -> None:
        """The POST must target the decisions endpoint for the plan."""
        url = "/api/plans/demo/decisions"
        client = _FakeHttpClient({url: {"ok": True, "record": {}}})
        TOOLS["answer_decision"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="S1",
            question="q",
            answer="a",
        )
        posted_url, _ = client.post_calls[0]
        assert posted_url == url

    def test_plan_name_url_encoded(self) -> None:
        """A plan name with special chars must be URL-encoded in the path."""
        url = "/api/plans/my%20plan/decisions"
        client = _FakeHttpClient({url: {"ok": True, "record": {}}})
        TOOLS["answer_decision"]["execute"](
            client,
            "http://base.test",
            plan_name="my plan",
            story_key="S1",
            question="q",
            answer="a",
        )
        posted_url, body = client.post_calls[0]
        assert posted_url == url
        assert body["decided_by"] == "chat"

    def test_body_has_exactly_five_keys(self) -> None:
        """The body must contain exactly the five known keys - no extra,
        no missing."""
        url = "/api/plans/demo/decisions"
        client = _FakeHttpClient({url: {"ok": True, "record": {}}})
        TOOLS["answer_decision"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="S1",
            question="q",
            answer="a",
            context="c",
        )
        _, body = client.post_calls[0]
        assert set(body.keys()) == {
            "story_key",
            "question",
            "answer",
            "context",
            "decided_by",
        }


# =========================================================================== #
# Negative / boundary: missing required fields still raise
# =========================================================================== #
class TestAnswerDecisionRequiredFields:
    def test_missing_answer_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["answer_decision"]["execute"](
                client,
                "http://base.test",
                plan_name="demo",
                story_key="S1",
                question="q",
            )

    def test_missing_story_key_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["answer_decision"]["execute"](
                client,
                "http://base.test",
                plan_name="demo",
                question="q",
                answer="a",
            )

    def test_missing_question_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["answer_decision"]["execute"](
                client,
                "http://base.test",
                plan_name="demo",
                story_key="S1",
                answer="a",
            )

    def test_missing_plan_name_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["answer_decision"]["execute"](
                client,
                "http://base.test",
                story_key="S1",
                question="q",
                answer="a",
            )

    def test_no_post_made_when_required_field_missing(self) -> None:
        """When a required field is missing the tool must raise before any
        POST is attempted."""
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["answer_decision"]["execute"](
                client,
                "http://base.test",
                plan_name="demo",
                story_key="S1",
                question="q",
            )
        assert client.post_calls == []


# =========================================================================== #
# End-to-end through the agent loop with a real httpx.Client + fake transport
# =========================================================================== #
class TestEndToEndDecidedBy:
    def _run(self, tool_name: str, args: dict, payload=None, reply: str = "done"):
        transport = _FakeTransport(payload=payload if payload is not None else {"ok": True})
        client = httpx.Client(transport=transport)
        driver = _FakeDriver(replies=[_tool_call(tool_name, args), reply])
        from app.chat import ChatService

        svc = ChatService(
            driver=driver, http_client=client, api_base_url="http://x.test"
        )
        out = svc.execute_turn("do the thing")
        return out, transport

    def test_decided_by_chat_through_loop_with_context(self) -> None:
        out, transport = self._run(
            "answer_decision",
            {
                "plan_name": "demo",
                "story_key": "S1",
                "question": "which lib?",
                "answer": "use foo",
                "context": "because bar",
            },
            payload={"ok": True, "record": {"story_key": "S1"}},
        )
        assert out["reply"] == "done"
        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/decisions"
        body = json.loads(req.content.decode())
        assert body["decided_by"] == "chat"
        assert body == {
            "story_key": "S1",
            "question": "which lib?",
            "answer": "use foo",
            "context": "because bar",
            "decided_by": "chat",
        }

    def test_decided_by_chat_through_loop_without_context(self) -> None:
        out, transport = self._run(
            "answer_decision",
            {
                "plan_name": "demo",
                "story_key": "S1",
                "question": "q",
                "answer": "a",
            },
            payload={"ok": True, "record": {}},
        )
        assert out["reply"] == "done"
        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/decisions"
        body = json.loads(req.content.decode())
        # context still defaults to empty string.
        assert body["context"] == ""
        # decided_by still stamped without context (independent).
        assert body["decided_by"] == "chat"
        assert body == {
            "story_key": "S1",
            "question": "q",
            "answer": "a",
            "context": "",
            "decided_by": "chat",
        }

    def test_exactly_one_request_through_loop(self) -> None:
        _, transport = self._run(
            "answer_decision",
            {
                "plan_name": "demo",
                "story_key": "S1",
                "question": "q",
                "answer": "a",
            },
            payload={"ok": True, "record": {}},
        )
        assert len(transport.requests) == 1


# =========================================================================== #
# Source-level invariants: the implementation must reference decided_by='chat'
# in app/chat.py, and must not regress the other TOOLS entries.
# =========================================================================== #
class TestSourceInvariants:
    def test_answer_decision_source_contains_decided_by_chat(self) -> None:
        """The ``answer_decision`` entry's source must reference
        ``decided_by`` with value ``'chat'``."""
        source = inspect.getsource(chat_module)
        # The literal must appear in the module source.
        assert "'decided_by': 'chat'" in source or '"decided_by": "chat"' in source, (
            "app/chat.py answer_decision must post decided_by='chat'"
        )

    def test_answer_decision_entry_present(self) -> None:
        assert "answer_decision" in TOOLS

    def test_answer_decision_params_unchanged(self) -> None:
        """The params dict must still declare the same fields (decided_by is
        not a caller-supplied param - it is stamped by the tool)."""
        params = TOOLS["answer_decision"]["params"]
        # Required params unchanged.
        assert {"plan_name", "story_key", "question", "answer"} <= set(params)
        # context still optional.
        assert "context" in params
        # decided_by is NOT a caller param - it is hardcoded by the tool.
        assert "decided_by" not in params

    def test_other_tools_entries_unchanged(self) -> None:
        """This story must not change any other TOOLS entry. The sibling
        tools must still be present with their execute callables."""
        for name in (
            "list_plans",
            "get_plan",
            "list_decisions",
            "health",
            "decompose",
            "save_plan",
            "ingest_plan",
            "dispatch_story",
            "interrupt_story",
            "patch_story",
            "review_story",
        ):
            assert name in TOOLS, f"TOOLS must still contain {name!r}"
            assert callable(TOOLS[name]["execute"])

    def test_patch_story_still_rejects_risk(self) -> None:
        """Sibling story 'Reject risk field in the chat patch_story tool'
        must remain intact: patch_story must still reject a 'risk' field."""
        assert "patch_story" in TOOLS
        client = _FakeHttpClient({"/api/plans/demo/stories/S1/patch": {"ok": True}})
        out = TOOLS["patch_story"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            story_key="S1",
            fields={"risk": "high"},
        )
        assert out == {"error": "risk field cannot be patched via chat"}
        assert client.post_calls == []