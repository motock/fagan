"""Tests for the ``patch_story`` risk-guard added to ``app/chat.py``'s TOOLS.

The chat ``patch_story`` tool must refuse to forward a caller-supplied
``risk`` field.  ``risk`` feeds ``PIPELINE_RISK_THRESHOLD`` in the scheduler,
so a prompt-injected risk downgrade would let the unattended scheduler
auto-merge a story parked for human review.  The guard must fail closed:
reject the *whole* patch (no HTTP request at all) rather than silently
stripping ``risk`` and forwarding the rest.

These tests are self-contained: they reuse the ``_FakeTransport`` pattern
from ``tests/unit/test_chat_ops_tools.py`` (redefined locally) and drive the
real ``TOOLS['patch_story']['execute']`` end-to-end through an
``httpx.Client(transport=...)`` so we assert the actual HTTP method, path,
request body, and request count the tool emits.

They must fail for the right reason (the guard is not yet implemented) until
the implementation lands.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.chat import TOOLS


# --------------------------------------------------------------------------- #
# Fakes (redefined locally per the brief; same shape as test_chat_ops_tools.py)
# --------------------------------------------------------------------------- #
class _FakeTransport:
    """An ``httpx`` transport that records requests and returns JSON bodies.

    Used to drive the real ``TOOLS['patch_story']['execute']`` end-to-end
    through an ``httpx.Client(transport=...)`` so we assert the actual HTTP
    method, path, and request body the tool emits.
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


def _execute(fields: dict, *, plan_name: str = "demo", story_key: str = "s4"):
    """Run ``patch_story``'s execute against a recording fake transport.

    Returns ``(result, transport)`` so callers can assert on both the return
    value and the recorded HTTP traffic.
    """
    transport = _FakeTransport(payload={"ok": True})
    client = httpx.Client(transport=transport, base_url="http://test")
    result = TOOLS["patch_story"]["execute"](
        client,
        "http://test",
        plan_name=plan_name,
        story_key=story_key,
        fields=fields,
    )
    return result, transport


# =========================================================================== #
# Risk guard: reject any patch containing a 'risk' key, fail closed.
# =========================================================================== #
class TestPatchStoryRiskGuard:
    def test_risk_only_returns_error_dict_and_makes_zero_requests(self) -> None:
        result, transport = _execute({"risk": "low"})
        # Must return an error-shaped dict (matching the module's existing
        # error shape, e.g. {'error': '...'}).
        assert isinstance(result, dict)
        assert "error" in result
        assert isinstance(result["error"], str) and result["error"]
        # No HTTP request may be issued at all.
        assert transport.requests == []

    def test_risk_mixed_with_other_fields_rejects_whole_call_zero_requests(self) -> None:
        # The call must fail closed: do NOT silently strip 'risk' and forward
        # the rest. The whole call is rejected with zero requests.
        result, transport = _execute({"risk": "low", "agent_instructions": "x"})
        assert isinstance(result, dict)
        assert "error" in result
        assert isinstance(result["error"], str) and result["error"]
        assert transport.requests == []

    @pytest.mark.parametrize(
        "fields",
        [
            {"risk": "low"},
            {"risk": "high"},
            {"risk": "medium"},
            {"risk": ""},
            {"risk": None},
            {"risk": 0},
            {"risk": "low", "status": "blocked"},
            {"status": "blocked", "risk": "low"},
            {"risk": "low", "agent_instructions": "x", "notes": "n"},
        ],
    )
    def test_any_risk_key_present_rejects_with_zero_requests(self, fields: dict) -> None:
        result, transport = _execute(fields)
        assert isinstance(result, dict)
        assert "error" in result
        assert transport.requests == []

    def test_error_message_mentions_risk(self) -> None:
        # The error string should explain *why* the call was rejected so an
        # operator reading the chat transcript understands the guard.
        result, _ = _execute({"risk": "low"})
        assert "risk" in result["error"].lower()

    def test_risk_guard_does_not_mutate_caller_fields(self) -> None:
        # The guard must not strip 'risk' in place and forward the rest; it
        # must reject outright. Confirm the caller's dict is untouched too.
        fields = {"risk": "low", "agent_instructions": "x"}
        _execute(fields)
        assert fields == {"risk": "low", "agent_instructions": "x"}


# =========================================================================== #
# Happy path: non-risk fields forwarded unchanged (no regression).
# =========================================================================== #
class TestPatchStoryNonRiskFieldsUnchanged:
    def test_agent_instructions_forwarded_unchanged_single_post(self) -> None:
        result, transport = _execute({"agent_instructions": "do X"})
        assert result == {"ok": True}
        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/stories/s4/patch"
        body = json.loads(req.content.decode())
        assert body == {"agent_instructions": "do X"}

    def test_empty_fields_dict_still_posts_empty_body(self) -> None:
        # Boundary: the guard triggers only on the literal 'risk' key, not on
        # "any fields present". An empty dict must still POST an empty body,
        # unchanged from current behavior.
        result, transport = _execute({})
        assert result == {"ok": True}
        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/api/plans/demo/stories/s4/patch"
        body = json.loads(req.content.decode())
        assert body == {}

    def test_multiple_non_risk_fields_forwarded_unchanged(self) -> None:
        fields = {"status": "blocked", "notes": "waiting on dep", "count": 3}
        result, transport = _execute(fields)
        assert result == {"ok": True}
        assert len(transport.requests) == 1
        req = transport.requests[0]
        body = json.loads(req.content.decode())
        assert body == fields

    def test_field_named_risk_prefix_not_treated_as_risk(self) -> None:
        # Boundary: only the exact key 'risk' triggers the guard. A key that
        # merely contains the substring 'risk' (e.g. 'risk_note') must still be
        # forwarded unchanged.
        fields = {"risk_note": "see thread", "riskiness": "high"}
        result, transport = _execute(fields)
        assert result == {"ok": True}
        assert len(transport.requests) == 1
        req = transport.requests[0]
        body = json.loads(req.content.decode())
        assert body == fields


# =========================================================================== #
# Registry shape: patch_story entry still exists and is callable.
# =========================================================================== #
class TestPatchStoryRegistryShape:
    def test_patch_story_present_and_callable(self) -> None:
        assert "patch_story" in TOOLS
        entry = TOOLS["patch_story"]
        assert set(entry.keys()) >= {"description", "params", "execute"}
        assert callable(entry["execute"])

    def test_patch_story_params_unchanged(self) -> None:
        # The guard must not change the declared params surface.
        params = TOOLS["patch_story"]["params"]
        assert set(params.keys()) >= {"plan_name", "story_key", "fields"}