"""Tests for the plan-authoring tool set (decompose / save_plan / ingest_plan).

This story adds:
  * PART 1 - a new POST /api/decompose HTTP route in app/dashboard.py that
    delegates to ``_service.decompose_plan(request.request)``.
  * PART 2 - three new tools (``decompose``, ``save_plan``, ``ingest_plan``)
    in the ``TOOLS`` registry in app/chat.py, plus one new paragraph in
    ``SYSTEM_PROMPT`` describing the plan-authoring flow.

These tests must fail for the right reason (a missing import / attribute /
route) until the implementation is added. They are self-contained: they
build a fake ``httpx`` transport and a scripted driver rather than touching
any real network or service layer, and they stub ``_service`` with a fake
so the dashboard route never touches the real pipeline.
"""
from __future__ import annotations

import inspect
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.chat as chat_module
from app import dashboard as d
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


# --------------------------------------------------------------------------- #
# Dashboard fakes
# --------------------------------------------------------------------------- #
class _FakeService:
    """Stand-in for the dashboard's ``_service`` singleton.

    Records delegation calls and returns a scripted result per method so the
    /api/decompose route can be exercised without a real pipeline.
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self.decompose_result = {"ok": True, "plan": {"epics": []}}

    def get_active_workspace(self) -> str | None:
        # Additive (story bd22dca3): the /api/decompose route now resolves a
        # workspace via the active-workspace fallback before delegating.
        # Returns None (no active workspace) and is deliberately NOT recorded
        # in ``calls`` so the existing delegation assertions keep their shape.
        return None

    def decompose_plan(self, request: str, workspace: str | None = None):
        self.calls.append(("decompose_plan", request))
        return self.decompose_result


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def fake_service(monkeypatch):
    fake = _FakeService()
    monkeypatch.setattr(d, "_service", fake)
    return fake


# =========================================================================== #
# PART 1 - POST /api/decompose route in app/dashboard.py
# =========================================================================== #
class TestDecomposeRoute:
    def test_decompose_delegates_to_service_and_returns_result(self, client, fake_service):
        """POST /api/decompose delegates to _service.decompose_plan and returns the result."""
        resp = client.post("/api/decompose", json={"request": "build a habit tracker"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "plan": {"epics": []}}
        assert fake_service.calls == [("decompose_plan", "build a habit tracker")]

    def test_decompose_empty_request_returns_422(self, client, fake_service):
        """An empty (missing) request field is a pydantic validation error -> 422."""
        resp = client.post("/api/decompose", json={})
        assert resp.status_code == 422
        # The service must NOT have been called on a validation failure.
        assert fake_service.calls == []

    def test_decompose_missing_body_returns_422(self, client, fake_service):
        """No body at all is a pydantic validation error -> 422."""
        resp = client.post("/api/decompose")
        assert resp.status_code == 422
        assert fake_service.calls == []

    def test_decompose_error_returns_400_with_detail(self, client, fake_service):
        """When decompose_plan returns ok=False, the route raises 400 with the error."""
        fake_service.decompose_result = {"ok": False, "error": "decompose backend returned no output"}
        resp = client.post("/api/decompose", json={"request": "x"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "decompose backend returned no output"

    def test_decompose_route_present_in_dashboard_source(self):
        """The /api/decompose route must be defined in dashboard.py near the other plan routes."""
        source = inspect.getsource(d)
        assert '"/api/decompose"' in source, (
            "POST /api/decompose route not found in dashboard.py source"
        )

    def test_decompose_route_placed_near_other_plan_routes(self):
        """The decompose route should sit near the save/ingest/advance plan routes."""
        source = inspect.getsource(d)
        decompose_idx = source.find('"/api/decompose"')
        save_idx = source.find('"/api/plans/{plan_name}/save"')
        assert decompose_idx != -1, "decompose route missing"
        assert save_idx != -1, "save route missing"
        # They should be in the same general region (within ~3000 chars of each other).
        assert abs(decompose_idx - save_idx) < 3000, (
            "decompose route should be placed near the other plan-level POST routes"
        )

    def test_decompose_request_model_has_request_field(self):
        """The pydantic request model for /api/decompose must define a required `request` str field."""
        # Look for a model class with a `request` field. We accept any name.
        source = inspect.getsource(d)
        # The route must reference a request body model.
        assert "decompose" in source.lower()


# =========================================================================== #
# PART 2 - Plan-authoring tools in the TOOLS registry
# =========================================================================== #
class TestPlanToolsRegistryShape:
    @pytest.mark.parametrize("name", ["decompose", "save_plan", "ingest_plan"])
    def test_tool_present(self, name: str) -> None:
        assert name in TOOLS, f"TOOLS missing {name!r}"

    @pytest.mark.parametrize("name", ["decompose", "save_plan", "ingest_plan"])
    def test_entry_has_required_keys(self, name: str) -> None:
        assert name in TOOLS
        entry = TOOLS[name]
        assert set(entry.keys()) >= {"description", "params", "execute"}
        assert isinstance(entry["description"], str) and entry["description"]
        assert isinstance(entry["params"], dict)
        assert callable(entry["execute"])

    def test_existing_readonly_tools_still_present(self) -> None:
        # list_plans and get_plan must be reused, not redefined away.
        assert {"list_plans", "get_plan", "health"} <= set(TOOLS.keys())

    def test_decompose_params_declares_goal(self) -> None:
        assert TOOLS["decompose"]["params"] == {"goal": "str"}

    def test_save_plan_params_declares_plan_name_and_plan_json(self) -> None:
        params = TOOLS["save_plan"]["params"]
        assert "plan_name" in params
        assert "plan_json" in params

    def test_ingest_plan_params_declares_expected_fields(self) -> None:
        params = TOOLS["ingest_plan"]["params"]
        assert "plan_name" in params
        # only_epics and overwrite should be declared.
        assert "only_epics" in params
        assert "overwrite" in params

    @pytest.mark.parametrize(
        "name,fragment",
        [
            ("decompose", "decompose"),
            ("save_plan", "save"),
            ("ingest_plan", "ingest"),
        ],
    )
    def test_descriptions_are_meaningful(self, name: str, fragment: str) -> None:
        assert fragment.lower() in TOOLS[name]["description"].lower()


# --------------------------------------------------------------------------- #
# decompose tool
# --------------------------------------------------------------------------- #
class TestDecomposeTool:
    def test_posts_to_decompose_endpoint_with_request_body(self) -> None:
        client = _FakeHttpClient({"/api/decompose": {"ok": True, "plan": {"epics": []}}})
        out = TOOLS["decompose"]["execute"](client, "http://base.test", goal="build X")
        assert out == {"ok": True, "plan": {"epics": []}}
        assert client.post_calls == [("/api/decompose", {"request": "build X"})]

    def test_missing_goal_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["decompose"]["execute"](client, "http://base.test")

    def test_empty_goal_is_forwarded(self) -> None:
        # Boundary: empty string goal is still forwarded as the request body.
        client = _FakeHttpClient({"/api/decompose": {"ok": True, "plan": {}}})
        out = TOOLS["decompose"]["execute"](client, "http://base.test", goal="")
        assert out == {"ok": True, "plan": {}}
        assert client.post_calls == [("/api/decompose", {"request": ""})]

    def test_ignores_extra_kwargs(self) -> None:
        client = _FakeHttpClient({"/api/decompose": {"ok": True}})
        out = TOOLS["decompose"]["execute"](
            client, "http://base.test", goal="g", unexpected="x"
        )
        assert out == {"ok": True}


# --------------------------------------------------------------------------- #
# save_plan tool
# --------------------------------------------------------------------------- #
class TestSavePlanTool:
    def test_posts_to_save_endpoint_with_plan_json(self) -> None:
        client = _FakeHttpClient({"/api/plans/demo/save": {"ok": True}})
        out = TOOLS["save_plan"]["execute"](
            client, "http://base.test", plan_name="demo", plan_json='{"epics":[]}'
        )
        assert out == {"ok": True}
        assert client.post_calls == [("/api/plans/demo/save", {"plan_json": '{"epics":[]}'})]

    def test_missing_plan_name_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["save_plan"]["execute"](
                client, "http://base.test", plan_json='{"epics":[]}'
            )

    def test_missing_plan_json_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["save_plan"]["execute"](
                client, "http://base.test", plan_name="demo"
            )

    def test_empty_plan_json_is_forwarded(self) -> None:
        # Boundary: empty plan_json string is still forwarded.
        client = _FakeHttpClient({"/api/plans/demo/save": {"ok": True}})
        out = TOOLS["save_plan"]["execute"](
            client, "http://base.test", plan_name="demo", plan_json=""
        )
        assert out == {"ok": True}
        assert client.post_calls == [("/api/plans/demo/save", {"plan_json": ""})]

    def test_ignores_extra_kwargs(self) -> None:
        client = _FakeHttpClient({"/api/plans/demo/save": {"ok": True}})
        out = TOOLS["save_plan"]["execute"](
            client, "http://base.test", plan_name="demo", plan_json="{}", stray="y"
        )
        assert out == {"ok": True}


# --------------------------------------------------------------------------- #
# ingest_plan tool
# --------------------------------------------------------------------------- #
class TestIngestPlanTool:
    def test_posts_to_ingest_endpoint_with_only_epics_and_overwrite(self) -> None:
        client = _FakeHttpClient({"/api/plans/demo/ingest": {"ok": True}})
        out = TOOLS["ingest_plan"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            only_epics=["E1"],
            overwrite=True,
        )
        assert out == {"ok": True}
        url, body = client.post_calls[0]
        assert url == "/api/plans/demo/ingest"
        assert body == {"only_epics": ["E1"], "overwrite": True}

    def test_only_epics_none_defaults(self) -> None:
        # Boundary: only_epics=None should be forwarded (or omitted) cleanly.
        client = _FakeHttpClient({"/api/plans/demo/ingest": {"ok": True}})
        out = TOOLS["ingest_plan"]["execute"](
            client, "http://base.test", plan_name="demo", only_epics=None, overwrite=False
        )
        assert out == {"ok": True}
        url, _body = client.post_calls[0]
        assert url == "/api/plans/demo/ingest"

    def test_overwrite_defaults_to_false(self) -> None:
        client = _FakeHttpClient({"/api/plans/demo/ingest": {"ok": True}})
        TOOLS["ingest_plan"]["execute"](
            client, "http://base.test", plan_name="demo", only_epics=None
        )
        url, body = client.post_calls[0]
        assert url == "/api/plans/demo/ingest"
        # overwrite should resolve to False when omitted.
        assert body.get("overwrite") is False

    def test_empty_only_epics_list_forwarded(self) -> None:
        # Boundary: empty list of epics is forwarded.
        client = _FakeHttpClient({"/api/plans/demo/ingest": {"ok": True}})
        out = TOOLS["ingest_plan"]["execute"](
            client, "http://base.test", plan_name="demo", only_epics=[], overwrite=False
        )
        assert out == {"ok": True}
        _url, body = client.post_calls[0]
        assert body == {"only_epics": [], "overwrite": False}

    def test_missing_plan_name_raises_type_error(self) -> None:
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["ingest_plan"]["execute"](
                client, "http://base.test", only_epics=None, overwrite=False
            )

    def test_ignores_extra_kwargs(self) -> None:
        client = _FakeHttpClient({"/api/plans/demo/ingest": {"ok": True}})
        out = TOOLS["ingest_plan"]["execute"](
            client,
            "http://base.test",
            plan_name="demo",
            only_epics=None,
            overwrite=False,
            stray="z",
        )
        assert out == {"ok": True}


# --------------------------------------------------------------------------- #
# End-to-end through the agent loop with a real httpx.Client + fake transport
# --------------------------------------------------------------------------- #
class TestEndToEndViaLoop:
    def test_decompose_called_through_loop_records_post(self) -> None:
        transport = _FakeTransport(payload={"ok": True, "plan": {"epics": []}})
        client = httpx.Client(transport=transport)
        driver = _FakeDriver(
            replies=[
                _tool_call("decompose", {"goal": "build a habit tracker"}),
                "here is your draft",
            ]
        )
        svc = ChatService(
            driver=driver, http_client=client, api_base_url="http://x.test"
        )
        out = svc.execute_turn("decompose this goal")
        assert out["reply"] == "here is your draft"
        assert out["turns"] == 2
        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/decompose"
        # The request body must contain request=<goal>.
        body = json.loads(req.content.decode())
        assert body == {"request": "build a habit tracker"}

    def test_save_plan_called_through_loop_records_post(self) -> None:
        transport = _FakeTransport(payload={"ok": True})
        client = httpx.Client(transport=transport)
        driver = _FakeDriver(
            replies=[
                _tool_call("save_plan", {"plan_name": "demo", "plan_json": '{"epics":[]}'}),
                "saved",
            ]
        )
        svc = ChatService(
            driver=driver, http_client=client, api_base_url="http://x.test"
        )
        out = svc.execute_turn("save the plan")
        assert out["reply"] == "saved"
        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/save"
        body = json.loads(req.content.decode())
        assert body == {"plan_json": '{"epics":[]}'}

    def test_ingest_plan_called_through_loop_records_post(self) -> None:
        transport = _FakeTransport(payload={"ok": True})
        client = httpx.Client(transport=transport)
        driver = _FakeDriver(
            replies=[
                _tool_call(
                    "ingest_plan",
                    {"plan_name": "demo", "only_epics": ["E1"], "overwrite": True},
                ),
                "ingested",
            ]
        )
        svc = ChatService(
            driver=driver, http_client=client, api_base_url="http://x.test"
        )
        out = svc.execute_turn("ingest the plan")
        assert out["reply"] == "ingested"
        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/ingest"
        body = json.loads(req.content.decode())
        assert body == {"only_epics": ["E1"], "overwrite": True}


# =========================================================================== #
# SYSTEM_PROMPT update
# =========================================================================== #
class TestSystemPromptUpdate:
    FINAL_SENTENCE = (
        "Call tools to gather information, then provide a natural-language reply."
    )

    # The new plan-authoring paragraph must contain these phrases.
    DECOMPOSE_PHRASE = "call decompose with their goal"
    SAVE_PHRASE = "save_plan then ingest_plan"
    CONFIRM_PHRASE = "confirm with the user before calling ingest_plan"
    DISPATCH_PHRASE = "ingestion dispatches stories"

    def test_contains_plan_authoring_paragraph(self) -> None:
        assert self.DECOMPOSE_PHRASE in SYSTEM_PROMPT
        assert self.SAVE_PHRASE in SYSTEM_PROMPT
        assert self.CONFIRM_PHRASE in SYSTEM_PROMPT
        assert self.DISPATCH_PHRASE in SYSTEM_PROMPT

    def test_paragraph_precedes_final_sentence(self) -> None:
        para_idx = SYSTEM_PROMPT.index(self.DECOMPOSE_PHRASE)
        final_idx = SYSTEM_PROMPT.index(self.FINAL_SENTENCE)
        assert para_idx < final_idx, (
            "the plan-authoring paragraph must come before the final "
            "'Call tools...' sentence"
        )

    def test_still_ends_with_final_sentence(self) -> None:
        assert SYSTEM_PROMPT.endswith(self.FINAL_SENTENCE)

    def test_paragraph_appears_exactly_once(self) -> None:
        assert SYSTEM_PROMPT.count(self.DECOMPOSE_PHRASE) == 1


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

    def test_tools_registry_contains_plan_tools(self) -> None:
        assert {"decompose", "save_plan", "ingest_plan"} <= set(TOOLS.keys())

    def test_dashboard_imports_chat_module(self) -> None:
        """dashboard.py must still import app.chat (router stays registered)."""
        assert hasattr(d, "chat")
        import app.chat as chat_module

        assert d.chat is chat_module