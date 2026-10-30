"""Every pipeline tool is decided by the remote policy (SA-02).

The middleware is driven directly with a minimal context object and a recording
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
from mcp.types import CallToolResult, TextContent

from pipeline import server
from pipeline import concurrency
import pipeline.persistence
from app.identity import Principal
from pipeline.actor import bind_principal
from pipeline.audit import HOST_AUDIT_FILE
from pipeline.remote_tool_policy import (
    REMOTE_ENFORCED,
    REMOTE_HOST_REFUSED,
    REMOTE_READ,
    remote_tool_allowed,
    remote_tool_policy,
)

ALICE = Principal(subject="https://idp#alice", auth_method="oidc")
BOB = Principal(subject="https://idp#bob", auth_method="oidc")

#: Distinguishes "the handler ran" from any dict the middleware might return.
SENTINEL = object()

NEWLY_EXPOSED = [
    "advance_all_plans",
    "advance_pipeline",
    "checkpoint",
    "checkpoint_story",
    "get_effective_config",
    "mark_story_in_progress",
    "patch_plan",
    "record_security_audit",
    "request_decision",
    "review_story",
    "save_plan",
    "set_story_status",
]

#: Minimal arguments that would reach the handler if the call were allowed.
ARGUMENTS_BY_TOOL = {
    "save_plan": {"plan_name": "alpha", "plan_json": "{}"},
    "decompose_plan": {"request": "add a health endpoint"},
}


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


def _call(
    params: object, principal: Principal, result: object = SENTINEL
) -> tuple[Any, _Recorder]:
    """Drive the middleware once, returning ``(result, recorder)``."""
    recorder = _Recorder(result)

    async def _run() -> object:
        with bind_principal(principal):
            return await remote_tool_policy(_Ctx(params), recorder)

    return asyncio.run(_run()), recorder


def _audit_records(tmp_path: Path) -> list[dict[str, Any]]:
    """Return the host audit records written under *tmp_path*."""
    path = tmp_path / HOST_AUDIT_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_plan(plan: str, owner: str) -> None:
    """Write a plan manifest owned by *owner* into the temporary plan directory."""
    manifest = {"plan": plan, "owner": owner}
    path = pipeline.persistence.PLAN_DIR / f"{plan}.manifest.json"
    path.write_text(json.dumps(manifest))


def _advance_result(plans: dict[str, Any]) -> CallToolResult:
    """The result shape the SDK builds for a dict-returning tool."""
    return CallToolResult(
        content=[TextContent(type="text", text="{}")],
        structured_content={"ok": True, "plans": plans},
    )


@pytest.fixture(autouse=True)
def _plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every PLAN_DIR binding, and so the manifests and audit, at a temp dir."""
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(concurrency, "PLAN_DIR", tmp_path)
    return tmp_path


# --- allowlist ------------------------------------------------------------


@pytest.mark.parametrize("name", NEWLY_EXPOSED)
def test_remote_tool_allowed_for_each_newly_exposed_tool(name: str) -> None:
    assert remote_tool_allowed(name) is True


def test_remote_tool_allowed_refuses_an_unregistered_name() -> None:
    assert remote_tool_allowed("no_such_tool") is False


def test_remote_tool_allowed_refuses_a_missing_name() -> None:
    assert remote_tool_allowed(None) is False


def test_remote_tool_allowed_refuses_decompose_plan() -> None:
    assert remote_tool_allowed("decompose_plan") is False


def test_every_registered_tool_has_a_remote_policy_decision() -> None:
    # A tool added to the server without a remote decision would be silently
    # refused; this fails instead, so the decision is made when the tool lands.
    registered = set(server.mcp._tool_manager._tools)
    assert registered <= REMOTE_ENFORCED | REMOTE_READ | REMOTE_HOST_REFUSED


# --- save_plan / decompose_plan workspace ---------------------------------


@pytest.mark.parametrize("name", sorted(ARGUMENTS_BY_TOOL))
def test_remote_caller_cannot_supply_a_server_workspace(name: str) -> None:
    arguments = {**ARGUMENTS_BY_TOOL[name], "workspace": "/srv/repo"}
    result, recorder = _call({"name": name, "arguments": arguments}, ALICE)
    assert (result["isError"], recorder.calls) == (True, 0)


@pytest.mark.parametrize("name", sorted(ARGUMENTS_BY_TOOL))
def test_empty_workspace_string_is_refused_too(name: str) -> None:
    arguments = {**ARGUMENTS_BY_TOOL[name], "workspace": ""}
    result, recorder = _call({"name": name, "arguments": arguments}, ALICE)
    assert (result["isError"], recorder.calls) == (True, 0)


def test_refused_workspace_is_audited_as_a_denied_tool_call(tmp_path: Path) -> None:
    arguments = {**ARGUMENTS_BY_TOOL["save_plan"], "workspace": "/srv/repo"}
    _call({"name": "save_plan", "arguments": arguments}, ALICE)
    assert [
        (r["action"], r["outcome"], r["detail"]["tool"]) for r in _audit_records(tmp_path)
    ] == [("tool:call", "denied", "save_plan")]


def test_save_plan_without_workspace_reaches_the_handler() -> None:
    _, recorder = _call({"name": "save_plan", "arguments": ARGUMENTS_BY_TOOL["save_plan"]}, ALICE)
    assert recorder.calls == 1


# --- decompose_plan is host-scoped: no remote principal may run it ---------


def test_decompose_plan_without_workspace_is_refused_for_a_remote_principal() -> None:
    arguments = ARGUMENTS_BY_TOOL["decompose_plan"]
    result, recorder = _call({"name": "decompose_plan", "arguments": arguments}, ALICE)
    assert (result["isError"], recorder.calls) == (True, 0)


def test_decompose_plan_refusal_is_audited_as_a_denied_tool_call(tmp_path: Path) -> None:
    arguments = ARGUMENTS_BY_TOOL["decompose_plan"]
    _call({"name": "decompose_plan", "arguments": arguments}, ALICE)
    assert [
        (r["action"], r["outcome"], r["detail"]["tool"])
        for r in _audit_records(tmp_path)
        if r["action"] == "tool:call"
    ] == [("tool:call", "denied", "decompose_plan")]


def test_save_plan_with_null_workspace_reaches_the_handler() -> None:
    arguments = {**ARGUMENTS_BY_TOOL["save_plan"], "workspace": None}
    _, recorder = _call({"name": "save_plan", "arguments": arguments}, ALICE)
    assert recorder.calls == 1


# --- advance_all_plans ----------------------------------------------------


def test_advance_all_plans_keeps_only_the_plans_the_caller_can_see() -> None:
    _write_plan("mine", owner=ALICE.subject)
    _write_plan("theirs", owner=BOB.subject)
    handler_result = _advance_result({"mine": {"advanced": 1}, "theirs": {"advanced": 2}})
    result, _ = _call({"name": "advance_all_plans", "arguments": {}}, ALICE, handler_result)
    assert sorted(result.structured_content["plans"]) == ["mine"]


def test_advance_all_plans_hides_every_plan_from_a_stranger() -> None:
    _write_plan("theirs", owner=BOB.subject)
    handler_result = _advance_result({"theirs": {"advanced": 2}})
    result, _ = _call({"name": "advance_all_plans", "arguments": {}}, ALICE, handler_result)
    assert result.structured_content["plans"] == {}


# --- get_effective_config -------------------------------------------------


def test_get_effective_config_without_plan_name_is_hidden() -> None:
    result, recorder = _call({"name": "get_effective_config", "arguments": {}}, ALICE)
    assert (result["isError"], recorder.calls) == (True, 0)


def test_get_effective_config_with_a_visible_plan_reaches_the_handler() -> None:
    _write_plan("alpha", owner=ALICE.subject)
    arguments = {"plan_name": "alpha"}
    _, recorder = _call({"name": "get_effective_config", "arguments": arguments}, ALICE)
    assert recorder.calls == 1

