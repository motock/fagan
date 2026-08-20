"""Tests: chat tool path segments (plan_name, story_key) must be URL-encoded
before being interpolated into the HTTP request path.

An LLM-controlled value containing '/', '#', '?', or '..' must NOT be able to
redirect a request onto a different route than the one the called tool names,
nor escape the ``/api/plans/`` prefix. Only path segments are encoded; query
string and JSON body values must pass through untouched.

These tests are self-contained: they reuse the ``_FakeTransport`` shape from
``tests/unit/test_chat_ops_tools.py`` (re-declared here, not imported across
test files) and drive the real ``TOOLS['<name>']['execute']`` callables through
an ``httpx.Client(transport=...)`` so the actual request path/method/body can
be inspected.

They must fail for the right reason - the implementation does not yet encode
path segments - until ``app/chat.py`` is updated to wrap every plan_name /
story_key path segment in ``urllib.parse.quote(str(x), safe="")``.
"""
from __future__ import annotations

import json

import httpx
import pytest

import app.chat as chat_module
from app.chat import TOOLS


# --------------------------------------------------------------------------- #
# Fakes (same shape as tests/unit/test_chat_ops_tools.py, re-declared locally)
# --------------------------------------------------------------------------- #
class _FakeTransport:
    """An ``httpx`` transport that records requests and returns a JSON body."""

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


def _client(payload=None) -> tuple[httpx.Client, _FakeTransport]:
    transport = _FakeTransport(payload=payload)
    client = httpx.Client(transport=transport, base_url="http://test")
    return client, transport


def _run(tool_name: str, payload=None, **args) -> httpx.Request:
    """Execute one tool and return its single recorded request."""
    client, transport = _client(payload=payload)
    TOOLS[tool_name]["execute"](client, "http://test", **args)
    assert len(transport.requests) == 1, f"expected 1 request, got {len(transport.requests)}"
    return transport.requests[0]


# --------------------------------------------------------------------------- #
# Source-level: the implementation must import & use urllib.parse.quote
# --------------------------------------------------------------------------- #
class TestSourceImportsQuote:
    def test_urllib_parse_quote_imported(self) -> None:
        src = chat_module.__file__
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        # quote must be imported from urllib.parse (or urllib.parse referenced).
        assert "urllib.parse" in text, "app/chat.py must import urllib.parse.quote"
        assert "quote" in text, "app/chat.py must use urllib.parse.quote for path segments"

    def test_seg_helper_present(self) -> None:
        """A small local helper wrapping quote(safe='') must exist."""
        src = chat_module.__file__
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        # The task suggests a `_seg` helper using quote(..., safe="").
        assert "safe=\"\"" in text or "safe=''" in text, (
            "path-segment encoding must use quote(..., safe='') so '/' is encoded"
        )


# --------------------------------------------------------------------------- #
# Tool -> (http method, path suffix after the encoded plan_name segment,
#          number of path segments: 1=plan_name only, 2=plan_name+story_key)
# --------------------------------------------------------------------------- #
# Single plan_name path segment tools.  suffix is what follows the encoded
# plan_name in the path ("" means the path is exactly /api/plans/{plan_name}).
SINGLE_SEGMENT_TOOLS = {
    "get_plan": ("GET", ""),
    "list_decisions": ("GET", ""),
    "save_plan": ("POST", "/save"),
    "ingest_plan": ("POST", "/ingest"),
    "advance_pipeline": ("POST", "/advance"),
    "pause_plan": ("POST", "/pause"),
    "resume_plan": ("POST", "/resume"),
}

# Two path-segment tools: /api/plans/{plan_name}/stories/{story_key}{suffix}.
TWO_SEGMENT_TOOLS = {
    "dispatch_story": ("POST", "/dispatch"),
    "interrupt_story": ("POST", "/interrupt"),
    "patch_story": ("POST", "/patch"),
    "set_story_status": ("POST", "/status"),
    "review_story": ("POST", "/review"),
    "approve_merge": ("POST", "/approve_merge"),
    "mark_story_done": ("POST", "/done"),
    "checkpoint": ("POST", "/checkpoint"),
    "get_story_journal": ("GET", "/journal"),
    "get_story_log": ("GET", "/log"),
    "get_story_checklist": ("GET", "/checklist"),
}

# answer_decision: plan_name is a path segment, story_key is a JSON body value.
# path = /api/plans/{plan_name}/decisions
ANSWER_DECISION_SUFFIX = "/decisions"


# --------------------------------------------------------------------------- #
# Headline: '#' must not redirect to a different route
# --------------------------------------------------------------------------- #
class TestHashDoesNotRedirect:
    def test_pause_plan_hash_redirects_to_different_route(self) -> None:
        req = _run("pause_plan", plan_name="demo/stories/s1/approve_merge#")
        assert req.url.path != "/api/plans/demo/stories/s1/approve_merge"
        assert req.url.path.endswith("/pause")
        # The whole malicious blob must be a single encoded segment.
        assert req.url.path == "/api/plans/demo%2Fstories%2Fs1%2Fapprove_merge%23/pause"

    @pytest.mark.parametrize(
        "name,suffix",
        [(k, v[1]) for k, v in SINGLE_SEGMENT_TOOLS.items()],
    )
    def test_hash_in_plan_name_single_segment_tools(self, name: str, suffix: str) -> None:
        payload = {"decisions": []} if name == "list_decisions" else {"ok": True}
        req = _run(name, payload=payload, plan_name="a#b/../c")
        # Must not land on the unencoded interpretation.
        assert req.url.path != f"/api/plans/a#b/../c{suffix}".replace("#", "")
        assert req.url.path.startswith("/api/plans/")
        assert req.url.path.endswith(suffix) if suffix else req.url.path != "/api/plans/a"
        # '#' and '/' must both be percent-encoded within the segment.
        assert "%23" in req.url.path
        assert "%2F" in req.url.path


# --------------------------------------------------------------------------- #
# '..' must not escape the /api/plans/ prefix
# --------------------------------------------------------------------------- #
class TestDotDotDoesNotEscape:
    def test_pause_plan_dotdot_does_not_escape(self) -> None:
        req = _run("pause_plan", plan_name="../../../etc")
        assert req.url.path.startswith("/api/plans/")
        assert req.url.path.endswith("/pause")
        # The literal '../' must not appear unencoded in the path.
        assert "../" not in req.url.path
        assert req.url.path == "/api/plans/..%2F..%2F..%2Fetc/pause"

    @pytest.mark.parametrize(
        "name,suffix",
        [(k, v[1]) for k, v in SINGLE_SEGMENT_TOOLS.items()],
    )
    def test_dotdot_in_plan_name_all_single_segment(self, name: str, suffix: str) -> None:
        payload = {"decisions": []} if name == "list_decisions" else {"ok": True}
        req = _run(name, payload=payload, plan_name="../../../etc")
        assert req.url.path.startswith("/api/plans/")
        assert "../" not in req.url.path
        if suffix:
            assert req.url.path.endswith(suffix)
        else:
            # path is exactly /api/plans/<encoded>
            assert req.url.path.count("/") == 2  # only the /api/plans/ separators


# --------------------------------------------------------------------------- #
# Two-segment tools: BOTH plan_name and story_key encoded independently
# --------------------------------------------------------------------------- #
class TestTwoSegmentsEncodedIndependently:
    @pytest.mark.parametrize(
        "name,suffix",
        [(k, v[1]) for k, v in TWO_SEGMENT_TOOLS.items()],
    )
    def test_slash_in_story_key_not_a_separator(self, name: str, suffix: str) -> None:
        # plan_name normal, story_key contains a '/'.
        kwargs = {
            "patch_story": {"fields": {"a": 1}},
            "set_story_status": {"status": "done"},
            "checkpoint": {"step": "s", "summary": "m"},
        }.get(name, {})
        req = _run(name, plan_name="p1", story_key="a/b", **kwargs)
        assert req.url.path == f"/api/plans/p1/stories/a%2Fb{suffix}"
        # A literal '/' inside the story_key segment must be encoded.
        assert "/stories/a/b" not in req.url.path

    @pytest.mark.parametrize(
        "name,suffix",
        [(k, v[1]) for k, v in TWO_SEGMENT_TOOLS.items()],
    )
    def test_slash_in_plan_name_two_segment(self, name: str, suffix: str) -> None:
        kwargs = {
            "patch_story": {"fields": {"a": 1}},
            "set_story_status": {"status": "done"},
            "checkpoint": {"step": "s", "summary": "m"},
        }.get(name, {})
        req = _run(name, plan_name="x/y", story_key="s1", **kwargs)
        assert req.url.path == f"/api/plans/x%2Fy/stories/s1{suffix}"

    @pytest.mark.parametrize(
        "name,suffix",
        [(k, v[1]) for k, v in TWO_SEGMENT_TOOLS.items()],
    )
    def test_both_segments_malicious(self, name: str, suffix: str) -> None:
        kwargs = {
            "patch_story": {"fields": {"a": 1}},
            "set_story_status": {"status": "done"},
            "checkpoint": {"step": "s", "summary": "m"},
        }.get(name, {})
        req = _run(name, plan_name="../p#?", story_key="..\\s", **kwargs)
        assert req.url.path.startswith("/api/plans/")
        assert "../" not in req.url.path
        assert req.url.path.endswith(suffix)
        # Neither segment leaked a raw '/' beyond the structural separators.
        # Structural separators are exactly: /api/plans/ , /stories/ , suffix.
        assert "%2F" in req.url.path


# --------------------------------------------------------------------------- #
# answer_decision: plan_name is a path segment, story_key/question/answer are
# JSON body values and must NOT be encoded.
# --------------------------------------------------------------------------- #
class TestAnswerDecision:
    def test_plan_name_encoded_story_key_in_body_not_encoded(self) -> None:
        req = _run(
            "answer_decision",
            plan_name="p/q#r",
            story_key="s/t",
            question="what?",
            answer="yes/no",
            context="ctx#1",
        )
        assert req.url.path == "/api/plans/p%2Fq%23r/decisions"
        body = json.loads(req.content.decode())
        assert body["story_key"] == "s/t"
        assert body["question"] == "what?"
        assert body["answer"] == "yes/no"
        assert body["context"] == "ctx#1"

    def test_dotdot_plan_name_does_not_escape(self) -> None:
        req = _run(
            "answer_decision",
            plan_name="../../etc",
            story_key="s1",
            question="q",
            answer="a",
        )
        assert req.url.path == "/api/plans/..%2F..%2Fetc/decisions"
        assert "../" not in req.url.path


# --------------------------------------------------------------------------- #
# Body / query values must NOT be encoded (regression guard)
# --------------------------------------------------------------------------- #
class TestBodyValuesNotEncoded:
    def test_set_story_status_body_untouched(self) -> None:
        req = _run("set_story_status", plan_name="p1", story_key="s1", status="a/b#c?d")
        body = json.loads(req.content.decode())
        assert body == {"status": "a/b#c?d"}

    def test_patch_story_body_untouched(self) -> None:
        req = _run("patch_story", plan_name="p1", story_key="s1", fields={"k/v": "a#b"})
        body = json.loads(req.content.decode())
        assert body == {"k/v": "a#b"}

    def test_checkpoint_body_untouched(self) -> None:
        req = _run(
            "checkpoint",
            plan_name="p1",
            story_key="s1",
            step="step/1",
            summary="sum#mary",
            next_hint="hint?x",
        )
        body = json.loads(req.content.decode())
        assert body == {"step": "step/1", "summary": "sum#mary", "next_hint": "hint?x"}

    def test_save_plan_body_untouched(self) -> None:
        req = _run("save_plan", plan_name="p1", plan_json='{"a/b": "c#d"}')
        body = json.loads(req.content.decode())
        assert body == {"plan_json": '{"a/b": "c#d"}'}

    def test_ingest_plan_body_untouched(self) -> None:
        req = _run("ingest_plan", plan_name="p1", only_epics=["e/1", "e#2"], overwrite=True)
        body = json.loads(req.content.decode())
        assert body == {"only_epics": ["e/1", "e#2"], "overwrite": True}


# --------------------------------------------------------------------------- #
# Negative/regression: ordinary names produce identical, unencoded-looking URLs
# --------------------------------------------------------------------------- #
class TestOrdinaryNamesUnchanged:
    @pytest.mark.parametrize(
        "name,suffix",
        [(k, v[1]) for k, v in SINGLE_SEGMENT_TOOLS.items()],
    )
    def test_single_segment_ordinary_name(self, name: str, suffix: str) -> None:
        payload = {"decisions": []} if name == "list_decisions" else {"ok": True}
        req = _run(name, payload=payload, plan_name="my-plan_1")
        assert req.url.path == f"/api/plans/my-plan_1{suffix}"

    @pytest.mark.parametrize(
        "name,suffix",
        [(k, v[1]) for k, v in TWO_SEGMENT_TOOLS.items()],
    )
    def test_two_segment_ordinary_names(self, name: str, suffix: str) -> None:
        kwargs = {
            "patch_story": {"fields": {}},
            "set_story_status": {"status": "done"},
            "checkpoint": {"step": "s", "summary": "m"},
        }.get(name, {})
        req = _run(name, plan_name="my-plan_1", story_key="story-2", **kwargs)
        assert req.url.path == f"/api/plans/my-plan_1/stories/story-2{suffix}"

    def test_answer_decision_ordinary_names(self) -> None:
        req = _run(
            "answer_decision",
            plan_name="my-plan_1",
            story_key="story-2",
            question="q",
            answer="a",
        )
        assert req.url.path == "/api/plans/my-plan_1/decisions"


# --------------------------------------------------------------------------- #
# HTTP method correctness is preserved (encoding must not change the verb)
# --------------------------------------------------------------------------- #
class TestMethodPreserved:
    @pytest.mark.parametrize(
        "name,method,suffix",
        [(k, v[0], v[1]) for k, v in SINGLE_SEGMENT_TOOLS.items()],
    )
    def test_single_segment_method(self, name: str, method: str, suffix: str) -> None:
        payload = {"decisions": []} if name == "list_decisions" else {"ok": True}
        req = _run(name, payload=payload, plan_name="evil#../x")
        assert req.method == method

    @pytest.mark.parametrize(
        "name,method,suffix",
        [(k, v[0], v[1]) for k, v in TWO_SEGMENT_TOOLS.items()],
    )
    def test_two_segment_method(self, name: str, method: str, suffix: str) -> None:
        kwargs = {
            "patch_story": {"fields": {}},
            "set_story_status": {"status": "done"},
            "checkpoint": {"step": "s", "summary": "m"},
        }.get(name, {})
        req = _run(name, plan_name="evil#p", story_key="evil/s", **kwargs)
        assert req.method == method


# --------------------------------------------------------------------------- #
# Exactly one request per call (no extra/redirected requests)
# --------------------------------------------------------------------------- #
class TestExactlyOneRequest:
    @pytest.mark.parametrize("name", list(SINGLE_SEGMENT_TOOLS))
    def test_single_segment_one_request(self, name: str) -> None:
        payload = {"decisions": []} if name == "list_decisions" else {"ok": True}
        client, transport = _client(payload=payload)
        TOOLS[name]["execute"](client, "http://test", plan_name="a#b/../c")
        assert len(transport.requests) == 1

    @pytest.mark.parametrize("name", list(TWO_SEGMENT_TOOLS))
    def test_two_segment_one_request(self, name: str) -> None:
        kwargs = {
            "patch_story": {"fields": {}},
            "set_story_status": {"status": "done"},
            "checkpoint": {"step": "s", "summary": "m"},
        }.get(name, {})
        client, transport = _client()
        TOOLS[name]["execute"](client, "http://test", plan_name="a#b", story_key="c/d", **kwargs)
        assert len(transport.requests) == 1