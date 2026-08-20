"""Tests for app/chat.py — the read-only TOOLS registry (PART 3 of 4).

This story populates the (previously empty) ``TOOLS`` registry with three
read-only tools: ``list_plans``, ``get_plan``, and ``health``. It also inserts
one new sentence into ``SYSTEM_PROMPT`` immediately before the final
"Call tools..." sentence.

These tests must pass against an implementation that does not yet exist, so
they import ``app.chat`` and assert the documented behavior. They are
self-contained: they build a fake ``httpx`` transport and a scripted driver
rather than touching any real network or service layer.
"""
from __future__ import annotations

import json

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
    """Records every ``.get`` call and returns a scripted ``_FakeResponse``.

    ``responses`` maps the exact URL passed to ``.get`` to the payload that
    the returned response's ``.json()`` should yield. Any unrecorded URL
    raises ``AssertionError`` so a wrong URL is caught loudly.
    """

    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.responses = responses or {}
        self.get_calls: list[str] = []

    def get(self, url, *args, **kwargs):
        self.get_calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected GET to {url!r}")
        return _FakeResponse(self.responses[url])


class _FakeTransport:
    """An ``httpx`` transport that records requests and returns JSON bodies.

    Used to drive the real ``TOOLS['list_plans']['execute']`` end-to-end
    through an ``httpx.Client(transport=...)`` so we assert the actual HTTP
    method and path the tool emits.
    """

    def __init__(self, payload=None) -> None:
        self.payload = payload if payload is not None else {"plans": ["a", "b"]}
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
# Registry shape
# --------------------------------------------------------------------------- #
class TestRegistryShape:
    def test_tools_is_a_dict(self) -> None:
        assert isinstance(TOOLS, dict)

    @pytest.mark.parametrize("name", ["list_plans", "get_plan", "health"])
    def test_all_three_tools_present(self, name: str) -> None:
        assert name in TOOLS, f"TOOLS missing {name!r}"

    @pytest.mark.parametrize("name", ["list_plans", "get_plan", "health"])
    def test_each_entry_has_required_keys(self, name: str) -> None:
        assert name in TOOLS
        entry = TOOLS[name]
        assert set(entry.keys()) >= {"description", "params", "execute"}
        assert isinstance(entry["description"], str) and entry["description"]
        assert isinstance(entry["params"], dict)
        assert callable(entry["execute"])

    def test_no_extra_tools_registered(self) -> None:
        # The three read-only tools must be present. Later stories in this
        # epic add plan-authoring tools (decompose/save_plan/ingest_plan) to
        # the same registry, so this asserts a superset rather than an
        # exact-match set.
        assert {"list_plans", "get_plan", "health"} <= set(TOOLS.keys())

    def test_list_plans_params_empty(self) -> None:
        assert TOOLS["list_plans"]["params"] == {}

    def test_health_params_empty(self) -> None:
        assert TOOLS["health"]["params"] == {}

    def test_get_plan_params_declares_plan_name(self) -> None:
        assert TOOLS["get_plan"]["params"] == {"plan_name": "str"}

    @pytest.mark.parametrize(
        "name,fragment",
        [
            ("list_plans", "plan"),
            ("get_plan", "plan"),
            ("health", "health"),
        ],
    )
    def test_descriptions_are_meaningful(self, name: str, fragment: str) -> None:
        assert fragment.lower() in TOOLS[name]["description"].lower()


# --------------------------------------------------------------------------- #
# list_plans
# --------------------------------------------------------------------------- #
class TestListPlans:
    def test_returns_json_from_plans_endpoint(self) -> None:
        client = _FakeHttpClient({"/api/plans": {"plans": ["a", "b"]}})
        out = TOOLS["list_plans"]["execute"](client, "http://base.test")
        assert out == {"plans": ["a", "b"]}

    def test_calls_exact_plans_url(self) -> None:
        client = _FakeHttpClient({"/api/plans": {"plans": []}})
        TOOLS["list_plans"]["execute"](client, "http://base.test")
        assert client.get_calls == ["/api/plans"]

    def test_empty_plans_list(self) -> None:
        client = _FakeHttpClient({"/api/plans": {"plans": []}})
        out = TOOLS["list_plans"]["execute"](client, "http://base.test")
        assert out == {"plans": []}

    def test_single_plan(self) -> None:
        client = _FakeHttpClient({"/api/plans": {"plans": ["only"]}})
        out = TOOLS["list_plans"]["execute"](client, "http://base.test")
        assert out == {"plans": ["only"]}

    def test_ignores_extra_kwargs(self) -> None:
        # The execute signature accepts **kwargs so unknown args don't break.
        client = _FakeHttpClient({"/api/plans": {"plans": ["a"]}})
        out = TOOLS["list_plans"]["execute"](
            client, "http://base.test", unexpected="x"
        )
        assert out == {"plans": ["a"]}


# --------------------------------------------------------------------------- #
# get_plan
# --------------------------------------------------------------------------- #
class TestGetPlan:
    def test_returns_json_for_named_plan(self) -> None:
        client = _FakeHttpClient({"/api/plans/demo": {"name": "demo"}})
        out = TOOLS["get_plan"]["execute"](
            client, "http://base.test", plan_name="demo"
        )
        assert out == {"name": "demo"}

    def test_calls_exact_plan_url(self) -> None:
        client = _FakeHttpClient({"/api/plans/demo": {"name": "demo"}})
        TOOLS["get_plan"]["execute"](
            client, "http://base.test", plan_name="demo"
        )
        assert client.get_calls == ["/api/plans/demo"]

    def test_url_includes_plan_name_with_special_chars(self) -> None:
        # A plan name containing a slash is percent-encoded so it cannot be
        # mistaken for an extra path segment (see test_chat_path_encoding.py).
        client = _FakeHttpClient({"/api/plans/a%2Fb": {"name": "a/b"}})
        TOOLS["get_plan"]["execute"](
            client, "http://base.test", plan_name="a/b"
        )
        assert client.get_calls == ["/api/plans/a%2Fb"]

    def test_missing_plan_name_raises_type_error(self) -> None:
        # plan_name is a required positional/keyword arg; omitting it must fail.
        client = _FakeHttpClient({})
        with pytest.raises(TypeError):
            TOOLS["get_plan"]["execute"](client, "http://base.test")


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
class TestHealth:
    def test_returns_json_from_health_endpoint(self) -> None:
        client = _FakeHttpClient({"/api/health": {"status": "ok"}})
        out = TOOLS["health"]["execute"](client, "http://base.test")
        assert out == {"status": "ok"}

    def test_calls_exact_health_url(self) -> None:
        client = _FakeHttpClient({"/api/health": {"status": "ok"}})
        TOOLS["health"]["execute"](client, "http://base.test")
        assert client.get_calls == ["/api/health"]

    def test_ignores_extra_kwargs(self) -> None:
        client = _FakeHttpClient({"/api/health": {"status": "ok"}})
        out = TOOLS["health"]["execute"](
            client, "http://base.test", stray="y"
        )
        assert out == {"status": "ok"}


# --------------------------------------------------------------------------- #
# End-to-end through the agent loop with a real httpx.Client + fake transport
# --------------------------------------------------------------------------- #
class TestEndToEndViaLoop:
    def test_list_plans_called_through_loop_records_get(self) -> None:
        transport = _FakeTransport(payload={"plans": ["a", "b"]})
        client = httpx.Client(transport=transport)
        driver = _FakeDriver(
            replies=[
                _tool_call("list_plans", {}),
                "all done",
            ]
        )
        svc = ChatService(
            driver=driver, http_client=client, api_base_url="http://x.test"
        )
        out = svc.execute_turn("list the plans")
        assert out["reply"] == "all done"
        assert out["turns"] == 2
        # The real list_plans tool ran against the fake transport.
        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.method == "GET"
        assert req.url.path == "/api/plans"
        # The tool result was fed back into the loop and recorded.
        assert out["tool_calls"] == [
            {"name": "list_plans", "args": {}, "result": {"result": {"plans": ["a", "b"]}}}
        ]

    def test_get_plan_called_through_loop_records_get(self) -> None:
        transport = _FakeTransport(payload={"name": "demo", "steps": []})
        client = httpx.Client(transport=transport)
        driver = _FakeDriver(
            replies=[
                _tool_call("get_plan", {"plan_name": "demo"}),
                "got it",
            ]
        )
        svc = ChatService(
            driver=driver, http_client=client, api_base_url="http://x.test"
        )
        out = svc.execute_turn("show me demo")
        assert out["reply"] == "got it"
        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.method == "GET"
        assert req.url.path == "/api/plans/demo"

    def test_health_called_through_loop_records_get(self) -> None:
        transport = _FakeTransport(payload={"status": "ok"})
        client = httpx.Client(transport=transport)
        driver = _FakeDriver(
            replies=[
                _tool_call("health", {}),
                "healthy",
            ]
        )
        svc = ChatService(
            driver=driver, http_client=client, api_base_url="http://x.test"
        )
        out = svc.execute_turn("check health")
        assert out["reply"] == "healthy"
        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.method == "GET"
        assert req.url.path == "/api/health"


# --------------------------------------------------------------------------- #
# SYSTEM_PROMPT update
#
# NOTE: this class originally pinned the "Available tools:" sentence to the
# exact 3 tools this story registers (list_plans, get_plan, health). That
# exact-match assertion is what let the sentence silently drift: 6 later
# stories (W2-03 through W2-05) each registered more tools without being
# able to touch this frozen sentence, so by the time all 8 W2 stories had
# merged, 14 of the 23 registered tools were never named anywhere in
# SYSTEM_PROMPT (root-caused 2026-08-20). SYSTEM_PROMPT now builds the
# sentence from TOOLS itself (see app/chat.py's _available_tools_sentence),
# so these tests assert the forward-compatible property instead: every
# CURRENTLY registered tool is named, and the enumeration still sits
# immediately before the final sentence, whatever tools TOOLS holds.
# --------------------------------------------------------------------------- #
class TestSystemPromptUpdate:
    FINAL_SENTENCE = (
        "Call tools to gather information, then provide a natural-language reply."
    )

    def test_contains_new_available_tools_sentence(self) -> None:
        assert "Available tools: " in SYSTEM_PROMPT

    def test_every_registered_tool_is_named_in_the_prompt(self) -> None:
        missing = [name for name in TOOLS if name not in SYSTEM_PROMPT]
        assert missing == [], f"tools registered but never named in SYSTEM_PROMPT: {missing}"

    def test_new_sentence_precedes_final_sentence(self) -> None:
        new_idx = SYSTEM_PROMPT.index("Available tools: ")
        final_idx = SYSTEM_PROMPT.index(self.FINAL_SENTENCE)
        assert new_idx < final_idx, (
            "the 'Available tools' sentence must come before the final "
            "'Call tools...' sentence"
        )

    def test_new_sentence_immediately_before_final_sentence(self) -> None:
        # The tools enumeration must be inserted immediately before the
        # final sentence, with only the separating whitespace between them.
        available_idx = SYSTEM_PROMPT.index("Available tools: ")
        final_idx = SYSTEM_PROMPT.index(self.FINAL_SENTENCE)
        between = SYSTEM_PROMPT[available_idx:final_idx]
        assert between.rstrip().endswith(")"), (
            "nothing but the tool enumeration and whitespace may sit "
            "between 'Available tools:' and the final sentence"
        )

    def test_still_ends_with_final_sentence(self) -> None:
        assert SYSTEM_PROMPT.endswith(self.FINAL_SENTENCE)

    def test_available_tools_sentence_appears_exactly_once(self) -> None:
        assert SYSTEM_PROMPT.count("Available tools: ") == 1


# --------------------------------------------------------------------------- #
# Module-level invariants
# --------------------------------------------------------------------------- #
class TestModuleInvariants:
    def test_no_pipeline_service_import(self) -> None:
        import inspect

        source = inspect.getsource(chat_module)
        assert "PipelineService" not in source
        assert "_service" not in source

    def test_tools_registry_populated(self) -> None:
        # PART 3 populates the registry; it must no longer be empty.
        assert TOOLS != {}
        assert {"list_plans", "get_plan", "health"} <= set(TOOLS.keys())