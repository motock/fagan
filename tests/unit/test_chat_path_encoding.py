"""Tests that every chat tool URL-encodes caller-supplied path segments
(plan_name, story_key) before interpolating them into the HTTP path.

A malicious or malformed value containing '/', '#', '?' or '..' must not be
able to redirect a tool's request onto a different route than the one the
tool names, nor escape the ``/api/plans/`` prefix.

These tests are self-contained: they drive the real
``TOOLS['<name>']['execute']`` lambdas through an ``httpx.Client`` wired to a
recording fake transport (same shape as ``tests/unit/test_chat_ops_tools.py``
's ``_FakeTransport`` - deliberately duplicated, not imported across files).

They must be RED until ``app/chat.py`` imports ``urllib.parse.quote``, adds a
``_seg`` helper, and applies it to every interpolated path segment.
"""
from __future__ import annotations

import json

import httpx
import pytest

import app.chat as chat_module
from app.chat import TOOLS


# --------------------------------------------------------------------------- #
# Fakes (same shape as test_chat_ops_tools._FakeTransport - not imported)
# --------------------------------------------------------------------------- #
class _FakeTransport:
    """An httpx transport that records every request and returns JSON."""

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
    """An httpx.Client whose base_url is absolute, so _resolve_tool_url
    returns the bare path and the recorded request's ``url.path`` is exactly
    the path the tool built."""
    return httpx.Client(transport=transport, base_url="http://test")


def _one_request(transport: _FakeTransport) -> httpx.Request:
    assert len(transport.requests) == 1, (
        f"expected exactly 1 request, got {len(transport.requests)}"
    )
    return transport.requests[0]


# --------------------------------------------------------------------------- #
# Import / helper presence
# --------------------------------------------------------------------------- #
class TestEncodingHelperPresent:
    def test_quote_imported(self) -> None:
        # The module must import urllib.parse.quote (or from urllib.parse
        # import quote) so the helper can use it without a local import.
        import importlib

        mod = importlib.reload(importlib.import_module("app.chat"))
        # Either a `quote` name or `urllib.parse` module attribute works.
        assert hasattr(mod, "quote") or (
            hasattr(mod, "urllib") and hasattr(mod.urllib.parse, "quote")
        ), "app.chat must import urllib.parse.quote"

    def test_seg_helper_exists(self) -> None:
        assert hasattr(chat_module, "_seg"), "app.chat must define a _seg helper"
        seg = chat_module._seg
        # _seg must percent-encode '/' so it cannot act as a separator.
        assert seg("a/b") == "a%2Fb"
        assert seg("a#b") == "a%23b"
        assert seg("a?b") == "a%3Fb"
        assert seg("..") == ".."
        # safe="" means even '.' is left alone but '/' is encoded; the key
        # property is that '/' is encoded.
        assert "/" not in seg("a/b")

    def test_seg_is_identity_for_plain_names(self) -> None:
        # A name with no special characters must round-trip unchanged.
        assert chat_module._seg("my-plan_1") == "my-plan_1"
        assert chat_module._seg("demo") == "demo"


# --------------------------------------------------------------------------- #
# Single path-segment tools (plan_name only)
# --------------------------------------------------------------------------- #
SINGLE_SEGMENT_TOOLS = [
    # (tool_name, suffix, method)
    ("get_plan", "", "GET"),
    ("list_decisions", "", "GET"),
    ("save_plan", "/save", "POST"),
    ("ingest_plan", "/ingest", "POST"),
    ("advance_pipeline", "/advance", "POST"),
    ("pause_plan", "/pause", "POST"),
    ("resume_plan", "/resume", "POST"),
]


@pytest.mark.parametrize("tool_name,suffix,method", SINGLE_SEGMENT_TOOLS)
class TestSingleSegmentEncoding:
    def _call(self, tool_name, plan_name):
        transport = _FakeTransport()
        client = _client(transport)
        # Provide any extra required kwargs the tool needs.
        extra = {}
        if tool_name == "save_plan":
            extra["plan_json"] = "{}"
        if tool_name == "ingest_plan":
            extra = {}
        TOOLS[tool_name]["execute"](client, "http://test", plan_name=plan_name, **extra)
        return _one_request(transport)

    def test_hash_does_not_redirect(self, tool_name, suffix, method) -> None:
        req = self._call(tool_name, "demo/stories/s1/approve_merge#")
        # The path must NOT be the redirected route.
        assert req.url.path != "/api/plans/demo/stories/s1/approve_merge"
        # It must start with the plans prefix and end with the tool's suffix.
        assert req.url.path.startswith("/api/plans/")
        if suffix:
            assert req.url.path.endswith(suffix)
        # The '#' must have been percent-encoded (not dropped as a fragment).
        assert "%23" in req.url.path

    def test_dotdot_does_not_escape_prefix(self, tool_name, suffix, method) -> None:
        req = self._call(tool_name, "../../../etc")
        assert req.url.path.startswith("/api/plans/")
        if suffix:
            assert req.url.path.endswith(suffix)
        # The literal '../' sequence must not appear unencoded in the path.
        assert "../" not in req.url.path

    def test_slash_in_plan_name_encoded(self, tool_name, suffix, method) -> None:
        req = self._call(tool_name, "a/b")
        # A '/' inside plan_name must be encoded, so the path still has
        # exactly the number of segments the tool defines (prefix + suffix).
        assert req.url.path.startswith("/api/plans/")
        if suffix:
            assert req.url.path.endswith(suffix)
        # No raw '/' beyond the structural ones.
        assert "a/b" not in req.url.path

    def test_plain_name_unchanged(self, tool_name, suffix, method) -> None:
        req = self._call(tool_name, "my-plan_1")
        expected = f"/api/plans/my-plan_1{suffix}"
        assert req.url.path == expected


# --------------------------------------------------------------------------- #
# Two path-segment tools (plan_name + story_key)
# --------------------------------------------------------------------------- #
TWO_SEGMENT_TOOLS = [
    # (tool_name, suffix, method, extra_kwargs)
    ("dispatch_story", "/dispatch", "POST", {}),
    ("interrupt_story", "/interrupt", "POST", {}),
    ("patch_story", "/patch", "POST", {"fields": {"x": 1}}),
    ("set_story_status", "/status", "POST", {"status": "blocked"}),
    ("review_story", "/review", "POST", {}),
    ("approve_merge", "/approve_merge", "POST", {}),
    ("mark_story_done", "/done", "POST", {}),
    ("checkpoint", "/checkpoint", "POST", {"step": "s", "summary": "m"}),
    ("get_story_journal", "/journal", "GET", {}),
    ("get_story_log", "/log", "GET", {}),
    ("get_story_checklist", "/checklist", "GET", {}),
]


@pytest.mark.parametrize(
    "tool_name,suffix,method,extra", TWO_SEGMENT_TOOLS
)
class TestTwoSegmentEncoding:
    def _call(self, tool_name, plan_name, story_key, extra):
        transport = _FakeTransport()
        client = _client(transport)
        TOOLS[tool_name]["execute"](
            client, "http://test",
            plan_name=plan_name, story_key=story_key, **extra,
        )
        return _one_request(transport)

    def test_both_segments_encoded_independently(self, tool_name, suffix, method, extra) -> None:
        # A '/' inside story_key must not be treated as a path separator.
        req = self._call(tool_name, "demo", "s/1", extra)
        assert req.url.path.startswith("/api/plans/demo/stories/")
        assert req.url.path.endswith(f"/{suffix.lstrip('/')}")
        # The raw 's/1' must not appear; it must be encoded.
        assert "s/1" not in req.url.path

    def test_slash_in_plan_name_encoded(self, tool_name, suffix, method, extra) -> None:
        req = self._call(tool_name, "a/b", "s1", extra)
        assert req.url.path.startswith("/api/plans/")
        assert "a/b" not in req.url.path
        # story_key still intact as a segment.
        assert "/s1" in req.url.path or "/s1" in req.url.path.replace("%2F", "/")

    def test_hash_in_story_key_does_not_redirect(self, tool_name, suffix, method, extra) -> None:
        req = self._call(tool_name, "demo", "evil#pause", extra)
        # Must not land on a /pause route via the fragment trick.
        assert req.url.path.endswith(f"/{suffix.lstrip('/')}")
        assert "%23" in req.url.path

    def test_dotdot_in_both_does_not_escape(self, tool_name, suffix, method, extra) -> None:
        req = self._call(tool_name, "../../etc", "../../etc", extra)
        assert req.url.path.startswith("/api/plans/")
        assert "../" not in req.url.path
        assert req.url.path.endswith(f"/{suffix.lstrip('/')}")

    def test_plain_names_unchanged(self, tool_name, suffix, method, extra) -> None:
        req = self._call(tool_name, "my-plan_1", "s4", extra)
        expected = f"/api/plans/my-plan_1/stories/s4{suffix}"
        assert req.url.path == expected


# --------------------------------------------------------------------------- #
# answer_decision: plan_name in path, story_key in BODY (must NOT be encoded)
# --------------------------------------------------------------------------- #
class TestAnswerDecisionEncoding:
    def test_plan_name_encoded_story_key_in_body_not_path(self) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        TOOLS["answer_decision"]["execute"](
            client, "http://test",
            plan_name="a/b", story_key="s/1",
            question="q?", answer="a#b", context="c/d",
        )
        req = _one_request(transport)
        # plan_name is a path segment -> encoded.
        assert req.url.path.startswith("/api/plans/")
        assert "a/b" not in req.url.path
        assert req.url.path.endswith("/decisions")
        # story_key/question/answer/context are JSON body values -> NOT
        # path-encoded. They must arrive intact in the request body.
        body = json.loads(req.content.decode())
        assert body["story_key"] == "s/1"
        assert body["question"] == "q?"
        assert body["answer"] == "a#b"
        assert body["context"] == "c/d"

    def test_plain_plan_name_unchanged(self) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        TOOLS["answer_decision"]["execute"](
            client, "http://test",
            plan_name="my-plan_1", story_key="s4",
            question="q", answer="a",
        )
        req = _one_request(transport)
        assert req.url.path == "/api/plans/my-plan_1/decisions"


# --------------------------------------------------------------------------- #
# Body values must never be path-encoded (regression guard)
# --------------------------------------------------------------------------- #
class TestBodyValuesNotEncoded:
    def test_set_story_status_status_in_body(self) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        TOOLS["set_story_status"]["execute"](
            client, "http://test",
            plan_name="demo", story_key="s1", status="blocked/needs#review?",
        )
        req = _one_request(transport)
        body = json.loads(req.content.decode())
        assert body["status"] == "blocked/needs#review?"

    def test_patch_story_fields_in_body(self) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        fields = {"path": "a/b/c", "note": "has#hash"}
        TOOLS["patch_story"]["execute"](
            client, "http://test",
            plan_name="demo", story_key="s1", fields=fields,
        )
        req = _one_request(transport)
        body = json.loads(req.content.decode())
        assert body == fields

    def test_checkpoint_step_summary_in_body(self) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        TOOLS["checkpoint"]["execute"](
            client, "http://test",
            plan_name="demo", story_key="s1",
            step="step/with/slashes", summary="sum#mary?",
        )
        req = _one_request(transport)
        body = json.loads(req.content.decode())
        assert body["step"] == "step/with/slashes"
        assert body["summary"] == "sum#mary?"


# --------------------------------------------------------------------------- #
# No-segment tools must be unaffected (regression)
# --------------------------------------------------------------------------- #
class TestNoSegmentToolsUnchanged:
    @pytest.mark.parametrize(
        "tool_name,expected_path,extra",
        [
            ("list_plans", "/api/plans", {}),
            ("health", "/api/health", {}),
            ("advance_all_plans", "/api/plans/advance_all", {}),
        ],
    )
    def test_path_unchanged(self, tool_name, expected_path, extra) -> None:
        transport = _FakeTransport()
        client = _client(transport)
        TOOLS[tool_name]["execute"](client, "http://test", **extra)
        req = _one_request(transport)
        assert req.url.path == expected_path


# --------------------------------------------------------------------------- #
# Exhaustiveness: every tool that interpolates a plan_name/story_key into a
# path must encode it. This guards against a future tool being added without
# encoding by checking the source for unencoded f-string interpolations.
# --------------------------------------------------------------------------- #
class TestExhaustiveSourceCoverage:
    def test_no_unencoded_plan_name_in_path(self) -> None:
        import inspect

        src = inspect.getsource(chat_module)
        # Every f-string path that interpolates {plan_name} or {story_key}
        # must wrap it in _seg(...). We look for the raw interpolation
        # without a _seg wrapper.
        import re

        # Find f-string path interpolations like {plan_name} not wrapped in
        # _seg(...). Allow _seg(plan_name) / _seg(story_key).
        bad = []
        for m in re.finditer(r"\{(plan_name|story_key)\}", src):
            start = m.start()
            # Look backwards ~10 chars for "_seg(" preceding this brace.
            before = src[max(0, start - 10):start]
            if "_seg(" not in before:
                bad.append(m.group(0))
        assert not bad, (
            f"Unencoded path segment interpolations found (must wrap in "
            f"_seg(...)): {bad}"
        )