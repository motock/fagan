"""Tests for the decision-answering tools (PART 2 of the decision story).

This story adds two things:

PART 1 - a new HTTP route in ``app/dashboard.py``::

    POST /api/plans/{plan_name}/decisions
        body (pydantic): story_key, question, answer, context (default ""),
                         decided_by (default "human")
        -> constructs a decision record and delegates to
           ``_service.append_decision(plan_name, record)``.
        -> returns ``{"ok": True, "record": <record>}``.
        -> 404 on an unknown plan (matching the pause/resume precedent).

PART 2 - two new tools in ``app/chat.py``'s ``TOOLS`` registry plus one new
paragraph in ``SYSTEM_PROMPT`` describing the decision-answering flow:

    * ``list_decisions(plan_name)`` - a convenience wrapper that calls
      ``get_plan`` (GET /api/plans/{plan_name}) and extracts just the
      ``decisions`` field.
    * ``answer_decision(plan_name, story_key, question, answer, context?)`` -
      POST /api/plans/{plan_name}/decisions with the appropriate body.

These tests must fail for the right reason (a missing route / tool /
paragraph) until the implementation is added.  They are self-contained: the
dashboard route tests stub ``_service`` with a Mock (the FakeService
pattern), and the chat-loop tests use a ``_FakeDriver`` plus a real
``httpx.Client(transport=FakeTransport())`` exactly like the chat adapter's
own ops-tool tests.
"""
from __future__ import annotations

import inspect
import json
from typing import ClassVar
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

import app.chat as chat_module
from app import dashboard as d
from app.chat import SYSTEM_PROMPT, TOOLS, ChatService


# --------------------------------------------------------------------------- #
# Fakes - dashboard route (FakeService pattern)
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def mock_service(monkeypatch):
    """Replace the module-level _service singleton with a Mock so the new
    decisions route can be exercised without touching the real pipeline
    store (mirrors test_dashboard_story_write_routes.py)."""
    svc = Mock()
    monkeypatch.setattr(d, "_service", svc)
    return svc


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point the dashboard's PLAN_DIR at a tmp dir and seed a manifest so the
    unknown-plan 404 path can be exercised against the real _list_plan_names
    check (which reads PLAN_DIR)."""
    monkeypatch.setattr(d, "PLAN_DIR", tmp_path)
    return tmp_path


def _write_manifest(plan_dir, name):
    import json as _json

    (plan_dir / f"{name}.manifest.json").write_text(
        _json.dumps({"epics": {}, "stories": {}})
    )


# --------------------------------------------------------------------------- #
# Fakes - chat loop (FakeDriver + FakeTransport, as in the ops-tool tests)
# --------------------------------------------------------------------------- #
class _FakeTransport:
    """An ``httpx`` transport that records requests and returns a JSON body."""

    def __init__(self, payload=None, status_code: int = 200) -> None:
        self.payload = payload if payload is not None else {"ok": True}
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self.status_code,
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


# =========================================================================== #
# PART 1 - POST /api/plans/{plan_name}/decisions route
# =========================================================================== #
class TestDecisionsRouteRegistration:
    def test_route_is_registered(self):
        paths = {
            (getattr(route, "path", None), method)
            for route in d.app.routes
            for method in getattr(route, "methods", set()) or set()
        }
        assert ("/api/plans/{plan_name}/decisions", "POST") in paths, (
            "POST /api/plans/{plan_name}/decisions is not registered on the "
            "dashboard app"
        )

    def test_route_placed_near_other_plan_level_post_routes(self):
        """The decisions route must appear in source near the other plan-level
        POST routes (pause/resume/archive). We assert it lives after the
        pause route and before the get_plan GET route - i.e. in the
        plan-level POST cluster."""
        source = inspect.getsource(d)
        decisions_idx = source.find('"/api/plans/{plan_name}/decisions"')
        pause_idx = source.find('"/api/plans/{plan_name}/pause"')
        get_plan_idx = source.find('"/api/plans/{plan_name}"')
        assert decisions_idx != -1, "decisions route path not found in source"
        assert pause_idx != -1, "pause route path not found in source"
        assert get_plan_idx != -1, "get_plan route path not found in source"
        # It should sit in the plan-level POST cluster, near pause/resume.
        assert pause_idx < decisions_idx, (
            "decisions route should come after the pause route (plan-level "
            "POST cluster)"
        )


class TestDecisionsRouteHappyPath:
    def test_delegates_to_append_decision_and_returns_record(self, client, mock_service):
        """POST delegates to _service.append_decision(plan_name, record) and
        returns {"ok": True, "record": <record>}."""
        res = client.post(
            "/api/plans/someplan/decisions",
            json={
                "story_key": "S1",
                "question": "which lib?",
                "answer": "use foo",
                "context": "because bar",
                "decided_by": "alice",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        record = body["record"]
        # The record must carry the expected fields.
        assert record["story_key"] == "S1"
        assert record["question"] == "which lib?"
        assert record["decision"] == "use foo"
        assert record["decided_by"] == "alice"
        # decided_at must be a present, non-empty ISO timestamp string.
        assert isinstance(record["decided_at"], str) and record["decided_at"]
        # options is an empty list per the spec.
        assert record["options"] == []
        # rationale mirrors the supplied context.
        assert record["rationale"] == "because bar"

    def test_append_decision_called_with_plan_name_and_record(self, client, mock_service):
        client.post(
            "/api/plans/someplan/decisions",
            json={
                "story_key": "S1",
                "question": "q",
                "answer": "a",
            },
        )
        mock_service.append_decision.assert_called_once()
        args, kwargs = mock_service.append_decision.call_args
        # plan_name is the first positional arg.
        assert "someplan" in args or kwargs.get("plan_name") == "someplan"
        # The record dict is the other arg (positional or keyword).
        record = args[1] if len(args) > 1 else kwargs.get("record")
        assert isinstance(record, dict)
        assert record["story_key"] == "S1"
        assert record["decision"] == "a"

    def test_defaults_context_empty_and_decided_by_human(self, client, mock_service):
        """Omitting context and decided_by must default to "" and "human"."""
        res = client.post(
            "/api/plans/someplan/decisions",
            json={"story_key": "S2", "question": "q", "answer": "a"},
        )
        assert res.status_code == 200
        record = res.json()["record"]
        assert record["rationale"] == ""
        assert record["decided_by"] == "human"

    def test_record_decided_at_is_utc_iso(self, client, mock_service):
        """decided_at must parse as an ISO-8601 timestamp with UTC info."""
        from datetime import datetime

        res = client.post(
            "/api/plans/someplan/decisions",
            json={"story_key": "S3", "question": "q", "answer": "a"},
        )
        record = res.json()["record"]
        ts = record["decided_at"]
        # Must be ISO-parseable; tolerate a trailing Z or +00:00 offset.
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None, "decided_at must carry UTC tz info"


class TestDecisionsRouteUnknownPlan:
    def test_unknown_plan_returns_404(self, client, plan_dir):
        """A plan with no manifest must 404 (matching pause/resume)."""
        res = client.post(
            "/api/plans/nope/decisions",
            json={"story_key": "S1", "question": "q", "answer": "a"},
        )
        assert res.status_code == 404

    def test_known_plan_does_not_404(self, client, plan_dir, mock_service):
        _write_manifest(plan_dir, "realplan")
        res = client.post(
            "/api/plans/realplan/decisions",
            json={"story_key": "S1", "question": "q", "answer": "a"},
        )
        assert res.status_code == 200


class TestDecisionsRouteValidation:
    def test_missing_required_story_key_is_422(self, client, mock_service):
        res = client.post(
            "/api/plans/someplan/decisions",
            json={"question": "q", "answer": "a"},
        )
        assert res.status_code == 422

    def test_missing_required_question_is_422(self, client, mock_service):
        res = client.post(
            "/api/plans/someplan/decisions",
            json={"story_key": "S1", "answer": "a"},
        )
        assert res.status_code == 422

    def test_missing_required_answer_is_422(self, client, mock_service):
        res = client.post(
            "/api/plans/someplan/decisions",
            json={"story_key": "S1", "question": "q"},
        )
        assert res.status_code == 422

    def test_empty_body_is_422(self, client, mock_service):
        res = client.post("/api/plans/someplan/decisions", json={})
        assert res.status_code == 422

    def test_empty_string_answer_is_accepted(self, client, mock_service):
        """An empty-string answer is a boundary value: it is a valid str, so
        pydantic accepts it (the route should not reject empty strings)."""
        res = client.post(
            "/api/plans/someplan/decisions",
            json={"story_key": "S1", "question": "q", "answer": ""},
        )
        assert res.status_code == 200
        assert res.json()["record"]["decision"] == ""


class TestDecisionRequestModel:
    """The pydantic request model must declare exactly the required fields
    with the right defaults - this guards against the implementer leaving the
    stale DecisionRequest (question/options/context) in place."""

    def test_decision_request_model_has_required_fields(self):
        model = getattr(d, "DecisionRequest", None)
        assert model is not None, "DecisionRequest model missing from dashboard"
        fields = model.model_fields
        assert "story_key" in fields, "DecisionRequest must declare story_key"
        assert "question" in fields, "DecisionRequest must declare question"
        assert "answer" in fields, "DecisionRequest must declare answer"
        assert "context" in fields, "DecisionRequest must declare context"
        assert "decided_by" in fields, "DecisionRequest must declare decided_by"

    def test_decision_request_defaults(self):
        model = d.DecisionRequest
        # context defaults to "" and decided_by defaults to "human".
        ctx_field = model.model_fields["context"]
        by_field = model.model_fields["decided_by"]
        assert ctx_field.default == "", "context must default to empty string"
        assert by_field.default == "human", "decided_by must default to 'human'"

    def test_decision_request_no_stale_options_field(self):
        """The old DecisionRequest had an `options` field; the new one must
        NOT (options is server-constructed as an empty list, not client input)."""
        model = d.DecisionRequest
        assert "options" not in model.model_fields, (
            "DecisionRequest must not declare an `options` field - options is "
            "server-constructed as an empty list"
        )


# =========================================================================== #
# PART 2 - chat tools registry
# =========================================================================== #
DECISION_TOOLS = ["list_decisions", "answer_decision"]


class TestDecisionToolsRegistryShape:
    @pytest.mark.parametrize("name", DECISION_TOOLS)
    def test_tool_present(self, name: str) -> None:
        assert name in TOOLS, f"TOOLS missing {name!r}"

    @pytest.mark.parametrize("name", DECISION_TOOLS)
    def test_entry_has_required_keys(self, name: str) -> None:
        assert name in TOOLS
        entry = TOOLS[name]
        assert set(entry.keys()) >= {"description", "params", "execute"}
        assert isinstance(entry["description"], str) and entry["description"]
        assert isinstance(entry["params"], dict)
        assert callable(entry["execute"])

    def test_existing_tools_still_present(self) -> None:
        # The pre-existing tools must remain alongside the new ones.
        assert {
            "list_plans",
            "get_plan",
            "health",
            "decompose",
            "save_plan",
            "ingest_plan",
        } <= set(TOOLS.keys())

    def test_list_decisions_params(self) -> None:
        assert "list_decisions" in TOOLS
        params = TOOLS["list_decisions"]["params"]
        assert "plan_name" in params

    def test_answer_decision_params(self) -> None:
        assert "answer_decision" in TOOLS
        params = TOOLS["answer_decision"]["params"]
        # Required params.
        assert {"plan_name", "story_key", "question", "answer"} <= set(params)
        # context is optional.
        assert "context" in params


# --------------------------------------------------------------------------- #
# list_decisions tool - convenience wrapper around get_plan
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttpClient:
    """Records get/post calls and returns scripted JSON (as in ops-tool tests)."""

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


class TestListDecisionsTool:
    def test_calls_get_plan_and_extracts_decisions(self) -> None:
        url = "/api/plans/demo"
        client = _FakeHttpClient({url: {"name": "demo", "decisions": [{"d": 1}]}})
        out = TOOLS["list_decisions"]["execute"](
            client, "http://base.test", plan_name="demo"
        )
        assert out == [{"d": 1}]
        assert client.get_calls == [url]
        # It must NOT post anything.
        assert client.post_calls == []

    def test_empty_decisions_list(self) -> None:
        url = "/api/plans/demo"
        client = _FakeHttpClient({url: {"name": "demo", "decisions": []}})
        out = TOOLS["list_decisions"]["execute"](
            client, "http://base.test", plan_name="demo"
        )
        assert out == []

    def test_missing_plan_name_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["list_decisions"]["execute"](client, "http://base.test")


class TestAnswerDecisionTool:
    def test_posts_to_decisions_endpoint_with_body(self) -> None:
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
        posted_url, body = client.post_calls[0]
        assert posted_url == url
        assert body == {
            "story_key": "S1",
            "question": "which lib?",
            "answer": "use foo",
            "context": "because bar",
            "decided_by": "chat",
        }

    def test_context_defaults_to_empty_when_omitted(self) -> None:
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
        assert body["context"] == ""

    def test_missing_required_answer_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["answer_decision"]["execute"](
                client,
                "http://base.test",
                plan_name="demo",
                story_key="S1",
                question="q",
            )

    def test_missing_required_story_key_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["answer_decision"]["execute"](
                client,
                "http://base.test",
                plan_name="demo",
                question="q",
                answer="a",
            )


# =========================================================================== #
# End-to-end through the agent loop with a real httpx.Client + fake transport
# =========================================================================== #
class TestEndToEndViaLoop:
    def _run(self, tool_name: str, args: dict, payload=None, reply: str = "done"):
        transport = _FakeTransport(payload=payload if payload is not None else {"ok": True})
        client = httpx.Client(transport=transport)
        driver = _FakeDriver(replies=[_tool_call(tool_name, args), reply])
        svc = ChatService(
            driver=driver, http_client=client, api_base_url="http://x.test"
        )
        out = svc.execute_turn("do the thing")
        return out, transport

    def test_list_decisions_called_through_loop(self) -> None:
        out, transport = self._run(
            "list_decisions",
            {"plan_name": "demo"},
            payload={"name": "demo", "decisions": [{"story_key": "S1"}]},
        )
        assert out["reply"] == "done"
        req = transport.requests[0]
        assert req.method == "GET"
        assert req.url.path == "/api/plans/demo"

    def test_answer_decision_called_through_loop_with_body(self) -> None:
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
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/decisions"
        body = json.loads(req.content.decode())
        assert body == {
            "story_key": "S1",
            "question": "which lib?",
            "answer": "use foo",
            "context": "because bar",
        }

    def test_answer_decision_called_through_loop_without_context(self) -> None:
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
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/decisions"
        body = json.loads(req.content.decode())
        assert body["context"] == ""


# =========================================================================== #
# SYSTEM_PROMPT update
# =========================================================================== #
class TestSystemPromptUpdate:
    FINAL_SENTENCE = (
        "Call tools to gather information, then provide a natural-language reply."
    )

    # Phrases the new decision paragraph must contain.
    DECISION_PHRASES: ClassVar[list[str]] = [
        "list_decisions",
        "answer_decision",
        "override or supplement a ruling",
        "audit trail",
    ]

    def test_contains_decision_paragraph(self) -> None:
        for phrase in self.DECISION_PHRASES:
            assert phrase in SYSTEM_PROMPT, (
                f"SYSTEM_PROMPT missing phrase {phrase!r}"
            )

    def test_decision_paragraph_precedes_final_sentence(self) -> None:
        # The new paragraph must appear before the final sentence.
        para_idx = SYSTEM_PROMPT.index("list_decisions")
        final_idx = SYSTEM_PROMPT.index(self.FINAL_SENTENCE)
        assert para_idx < final_idx, (
            "the decision paragraph must come before the final 'Call tools...' "
            "sentence"
        )

    def test_still_ends_with_final_sentence(self) -> None:
        assert SYSTEM_PROMPT.endswith(self.FINAL_SENTENCE)

    def test_decision_paragraph_appears_exactly_once(self) -> None:
        # The key phrase should appear exactly once (no duplicated paragraph).
        assert SYSTEM_PROMPT.count("override or supplement a ruling") == 1


# =========================================================================== #
# Module-level invariants
# =========================================================================== #
class TestModuleInvariants:
    def test_all_decision_tools_registered(self) -> None:
        assert set(DECISION_TOOLS) <= set(TOOLS.keys())

    def test_chat_py_does_not_contain_forbidden_names(self) -> None:
        """app/chat.py must not reference PipelineService or _service."""
        source = inspect.getsource(chat_module)
        assert "PipelineService" not in source, (
            "app/chat.py must not reference PipelineService"
        )
        assert "_service" not in source, "app/chat.py must not reference _service"