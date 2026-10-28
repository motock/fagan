"""Remote read tools are scoped to the plans a principal owns or delegates (RMH-02).

The middleware is driven directly with a minimal context object and a fake
``call_next``, exactly as ``tests/unit/test_remote_tool_policy.py`` does: no
server, no SDK session, and the tool itself is never re-implemented here.
"""

from __future__ import annotations

# ruff: noqa: I001
# ``pipeline.server`` is imported before the other pipeline modules on purpose:
# a pre-existing import cycle in the server module means a test that reaches
# ``pipeline.*`` has to load it first (see tests/unit/test_triage_cap_silent_skip.py).
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

import pipeline.server as p
import pipeline.persistence as ppers
import app.policy as policy_module
from app.identity import LOCAL_OPERATOR, Principal
from pipeline import authorization
from pipeline import concurrency as pcon
from pipeline.actor import bind_principal
from pipeline.remote_tool_policy import remote_tool_policy

ALPHA_OWNER = Principal(subject="https://idp#alice", auth_method="oidc")
BETA_OWNER = Principal(subject="https://idp#bob", auth_method="oidc")
ALPHA_DELEGATE = Principal(subject="https://idp#dee", auth_method="oidc")
STRANGER = Principal(subject="https://idp#eve", auth_method="oidc")

#: Distinguishes "the handler ran" from any dict the middleware might return.
SENTINEL = object()

STEMS = ["alpha", "alpha.manifest", "beta", "beta.audit.log"]


class _Ctx:
    """The two attributes the middleware is allowed to read."""

    def __init__(self, params: object, method: str = "tools/call") -> None:
        self.method = method
        self.params = params


class _Recorder:
    """An async ``call_next`` that counts invocations and returns a fixed result."""

    def __init__(self, result: object = SENTINEL) -> None:
        self.calls = 0
        self._result = result

    async def __call__(self, ctx: Any) -> object:
        self.calls += 1
        return self._result


class _RaisingResource:
    """A resource whose every field raises, to prove the policy fails closed."""

    @property
    def owner(self) -> str:
        raise RuntimeError("unreadable resource")

    @property
    def delegates(self) -> tuple[str, ...]:
        raise RuntimeError("unreadable resource")


def _can_view() -> Any:
    """Return ``app.policy.can_view``, failing as an assertion while it is missing."""
    can_view = getattr(policy_module, "can_view", None)
    assert callable(can_view), "app.policy.can_view is missing"
    return can_view


@pytest.fixture(autouse=True)
def _plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point every PLAN_DIR binding at a throwaway directory."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


def _write_plan(plan: str, *, owner: str = "", delegates: tuple[str, ...] = ()) -> None:
    """Write the manifest a plan's visibility is read from."""
    manifest: dict[str, Any] = {"plan": plan}
    if owner:
        manifest["owner"] = owner
    if delegates:
        manifest["delegates"] = list(delegates)
    path = ppers.PLAN_DIR / f"{plan}.manifest.json"
    path.write_text(json.dumps(manifest))


def _call(
    params: object, principal: Principal, result: object = SENTINEL
) -> tuple[Any, _Recorder]:
    """Drive the middleware once, returning ``(result, recorder)``."""
    recorder = _Recorder(result)

    async def _run() -> object:
        with bind_principal(principal):
            return await remote_tool_policy(_Ctx(params), recorder)

    return asyncio.run(_run()), recorder


def _list_plans_result(stems: list[str]) -> CallToolResult:
    """The result shape this SDK version builds for a tool returning ``list[str]``.

    ``_convert_to_content`` unrolls the list into one text block per stem and the
    structured content carries ``{"result": stems}`` (mcp 2.3.0).
    """
    return CallToolResult(
        content=[TextContent(type="text", text=stem) for stem in stems],
        structured_content={"result": list(stems)},
    )


def _stems(result: Any) -> list[str]:
    """Return the plan stems a filtered ``list_plans`` result carries."""
    return list(result.structured_content["result"])


def _list_plans_as(principal: Principal) -> list[str]:
    result, recorder = _call(
        {"name": "list_plans", "arguments": {}}, principal, _list_plans_result(STEMS)
    )
    assert recorder.calls == 1
    return _stems(result)


def test_owner_sees_only_their_own_plan_in_list_plans() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    _write_plan("beta", owner=BETA_OWNER.subject)
    assert _list_plans_as(ALPHA_OWNER) == ["alpha", "alpha.manifest"]


def test_delegate_sees_the_plan_they_are_delegated() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject, delegates=(ALPHA_DELEGATE.subject,))
    _write_plan("beta", owner=BETA_OWNER.subject)
    assert _list_plans_as(ALPHA_DELEGATE) == ["alpha", "alpha.manifest"]


def test_stranger_sees_no_stems_and_one_identical_hidden_response() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    assert _list_plans_as(STRANGER) == []
    known, known_recorder = _call(
        {"name": "check_story_status", "arguments": {"plan_name": "alpha"}}, STRANGER
    )
    unknown, unknown_recorder = _call(
        {"name": "check_story_status", "arguments": {"plan_name": "ghost"}}, STRANGER
    )
    assert (known["isError"], unknown["isError"]) == (True, True)
    # Byte-equal: a distinct answer for "exists but hidden" would be an oracle.
    assert known == unknown
    assert (known_recorder.calls, unknown_recorder.calls) == (0, 0)
    assert "alpha" not in json.dumps(known)


def test_local_operator_can_view_a_plan_it_does_not_own() -> None:
    # The middleware never sees a local caller (serve() refuses that identity),
    # so the operator rule is asserted directly on the policy.
    can_view = _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    resource = authorization.resource_for(
        authorization._load_manifest("alpha"), "alpha", None
    )
    assert can_view(LOCAL_OPERATOR, resource) is True


def _audit_records(directory: Any) -> list[dict[str, Any]]:
    """Return the host audit records written under *directory*."""
    path = directory / ".audit.host.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_un_owned_plan_is_hidden_before_the_tool_runs() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    result, recorder = _call(
        {"name": "check_story_status", "arguments": {"plan_name": "alpha"}}, BETA_OWNER
    )
    assert result["isError"] is True
    assert recorder.calls == 0


def test_hidden_call_is_audited_as_hidden(_plan_dir: Any) -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    _call({"name": "check_story_status", "arguments": {"plan_name": "alpha"}}, STRANGER)
    calls = [r for r in _audit_records(_plan_dir) if r["action"] == "tool:call"]
    assert [(r["outcome"], r["detail"]["tool"]) for r in calls] == [
        ("hidden", "check_story_status")
    ]
    assert [r["detail"]["subject"] for r in calls] == [STRANGER.subject]


@pytest.mark.parametrize(
    "tool", ["check_story_status", "get_story_diff", "list_decisions", "list_ready_stories"]
)
def test_every_plan_scoped_read_tool_is_gated(tool: str) -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    result, recorder = _call({"name": tool, "arguments": {"plan_name": "alpha"}}, STRANGER)
    assert result["isError"] is True
    assert recorder.calls == 0


def test_visible_plan_reaches_the_tool_unchanged() -> None:
    _can_view()
    _write_plan("beta", owner=BETA_OWNER.subject)
    result, recorder = _call(
        {"name": "check_story_status", "arguments": {"plan_name": "beta"}}, BETA_OWNER
    )
    assert result is SENTINEL
    assert recorder.calls == 1


def test_missing_or_non_string_plan_name_is_hidden() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    for arguments in ({}, {"plan_name": 12345}, {"plan_name": None}):
        result, recorder = _call(
            {"name": "get_story_diff", "arguments": arguments}, ALPHA_OWNER
        )
        assert result["isError"] is True
        assert recorder.calls == 0


def test_policy_exception_makes_the_plan_invisible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _can_view()

    def _boom(plan_name: str) -> dict:
        raise FileNotFoundError(plan_name)

    # Patched on the module attribute, which is what the middleware calls.
    monkeypatch.setattr(authorization, "_load_manifest", _boom)
    result, recorder = _call(
        {"name": "get_story_diff", "arguments": {"plan_name": "alpha"}}, ALPHA_OWNER
    )
    assert result["isError"] is True
    assert recorder.calls == 0


def test_can_view_fails_closed_and_does_not_cache_a_denial() -> None:
    can_view = _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    assert can_view(STRANGER, _RaisingResource()) is False
    resource = authorization.resource_for(
        authorization._load_manifest("alpha"), "alpha", None
    )
    assert can_view(ALPHA_OWNER, resource) is True


def test_check_usage_stays_host_level_and_unfiltered() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    result, recorder = _call(
        {"name": "check_usage", "arguments": {"plan_name": "alpha"}}, STRANGER
    )
    assert result is SENTINEL
    assert recorder.calls == 1


def test_list_plans_filtering_does_not_mutate_the_shared_result() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    _write_plan("beta", owner=BETA_OWNER.subject)
    shared = _list_plans_result(STEMS)
    first, _ = _call({"name": "list_plans", "arguments": {}}, BETA_OWNER, shared)
    second, _ = _call({"name": "list_plans", "arguments": {}}, ALPHA_OWNER, shared)
    assert _stems(first) == ["beta", "beta.audit.log"]
    assert _stems(second) == ["alpha", "alpha.manifest"]
    assert _stems(shared) == STEMS


def test_verify_audit_drops_invisible_plans_and_the_host_entry() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    _write_plan("beta", owner=BETA_OWNER.subject)
    payload = {"ok": True, "plans": {"alpha": None, "beta": 2}, "host": 3}
    shared = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=dict(payload),
    )
    result, recorder = _call({"name": "verify_audit", "arguments": {}}, BETA_OWNER, shared)
    assert recorder.calls == 1
    body = result.structured_content
    assert "host" not in body
    assert "alpha" not in body["plans"]
    assert "beta" in body["plans"]


# ---------------------------------------------------------------------------
# The result shape production actually hands the middleware
#
# ``tests/unit/test_remote_tool_policy.py`` and the tests above build results by
# hand, which cannot catch a middleware written against a field the SDK does not
# have. These build the result through the SDK's own conversion path -- a tool
# registered on an ``MCPServer`` and called through ``call_tool`` -- and assert
# the filtered value in the field the model declares *and* in its wire form.
# ---------------------------------------------------------------------------


def _sdk_list_plans_result(stems: list[str]) -> CallToolResult:
    """Return the ``CallToolResult`` the SDK builds for a ``list[str]`` tool."""
    server = MCPServer("scoping-list-plans")

    @server.tool(name="list_plans")
    def list_plans() -> list[str]:
        return stems

    return asyncio.run(server.call_tool("list_plans", {}))


def _sdk_verify_audit_result(payload: dict[str, Any]) -> CallToolResult:
    """Return the ``CallToolResult`` the SDK builds for ``verify_audit``.

    ``verify_audit`` is annotated ``-> dict[str, Any]``, so the SDK puts the
    mapping itself in the structured content -- there is no ``result`` wrapper.
    """
    server = MCPServer("scoping-verify-audit")

    @server.tool(name="verify_audit")
    def verify_audit() -> dict[str, Any]:
        return payload

    return asyncio.run(server.call_tool("verify_audit", {}))


def test_list_plans_filters_the_result_the_sdk_builds() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    _write_plan("beta", owner=BETA_OWNER.subject)
    shared = _sdk_list_plans_result(STEMS)
    # The production shape: the stems live in the structured content, and the
    # text blocks are unrolled one per stem.
    assert shared.structured_content == {"result": STEMS}

    result, recorder = _call({"name": "list_plans", "arguments": {}}, BETA_OWNER, shared)
    assert recorder.calls == 1
    assert result.structured_content == {"result": ["beta", "beta.audit.log"]}
    # The wire form is what a remote client reads; the filtered stems must be
    # there, which they only are if the update landed on the model's real field.
    assert result.model_dump(by_alias=True)["structuredContent"] == {
        "result": ["beta", "beta.audit.log"]
    }
    assert [block.text for block in result.content] == ["beta", "beta.audit.log"]
    # The result handed in is not mutated.
    assert shared.structured_content == {"result": STEMS}


def test_verify_audit_filters_the_result_the_sdk_builds() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    _write_plan("beta", owner=BETA_OWNER.subject)
    payload = {"ok": True, "plans": {"alpha": None, "beta": 2}, "host": 3}
    shared = _sdk_verify_audit_result(payload)
    assert shared.structured_content == payload

    result, recorder = _call({"name": "verify_audit", "arguments": {}}, BETA_OWNER, shared)
    assert recorder.calls == 1
    body = result.structured_content
    assert body["ok"] is True
    assert "host" not in body
    assert "alpha" not in body["plans"]
    assert "beta" in body["plans"]
    # The text block is the answer a model reads, so it must not carry the
    # filtered-out plans or the host chain either.
    text = result.model_dump(by_alias=True)["content"][0]["text"]
    assert text == json.dumps({"ok": True, "plans": {"beta": 2}})
    assert "alpha" not in text


def test_visible_result_is_returned_untouched() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    _write_plan("beta", owner=BETA_OWNER.subject)
    shared = _sdk_list_plans_result(["alpha", "alpha.manifest"])
    result, recorder = _call({"name": "list_plans", "arguments": {}}, ALPHA_OWNER, shared)
    assert recorder.calls == 1
    assert result is shared


def test_unrewriteable_result_is_withheld_as_the_hidden_refusal() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)
    # A result this middleware cannot rewrite must not pass through unfiltered.
    result, recorder = _call({"name": "list_plans", "arguments": {}}, STRANGER, object())
    assert recorder.calls == 1
    assert result["isError"] is True
    # Withheld means the same refusal a hidden plan gets, not a new message.
    unknown, _ = _call(
        {"name": "check_story_status", "arguments": {"plan_name": "ghost"}}, STRANGER
    )
    assert result == unknown


def test_unsafe_plan_name_is_hidden_before_the_manifest_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _can_view()
    calls: list[str] = []

    def _record(plan_name: str) -> dict:
        calls.append(plan_name)
        return {}

    # A traversal-shaped name must be refused without resolving it to a path.
    monkeypatch.setattr(authorization, "_load_manifest", _record)
    result, recorder = _call(
        {"name": "get_story_diff", "arguments": {"plan_name": "../../etc/passwd"}},
        ALPHA_OWNER,
    )
    assert result["isError"] is True
    assert recorder.calls == 0
    assert calls == []
