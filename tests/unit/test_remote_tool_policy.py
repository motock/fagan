"""The remote MCP default-deny tool policy (RTOOL-01).

The middleware is driven directly with a minimal context object -- no server,
no SDK session -- and each test asserts exactly one property of the policy: who
may call what, and what a refusal leaves behind in the host audit stream.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from pipeline.remote_tool_policy import remote_tool_policy

import pipeline.persistence
from app.identity import LOCAL_OPERATOR, Principal
from app.oidc import IDENTITY_PROVIDER_ENV, OidcProvider
from pipeline import remote_transport as rt
from pipeline.actor import bind_principal
from pipeline.audit import HOST_AUDIT_FILE

#: A caller the bearer gate authenticated: not the local operator.
ALICE = Principal(subject="https://idp#alice", auth_method="oidc")

#: Distinguishes "the handler ran" from any dict the middleware might return.
SENTINEL = object()


class _Ctx:
    """The two attributes the middleware is allowed to read."""

    def __init__(self, params: object, method: str = "tools/call") -> None:
        self.method = method
        self.params = params


class _Recorder:
    """An async ``call_next`` that remembers whether it was reached."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx: Any) -> object:
        self.calls += 1
        return SENTINEL


def _call(params: object, method: str = "tools/call", principal: Principal | None = ALICE):
    """Drive the middleware once, returning ``(result, recorder)``."""

    recorder = _Recorder()

    async def _run() -> object:
        if principal is None:
            return await remote_tool_policy(_Ctx(params, method), recorder)
        with bind_principal(principal):
            return await remote_tool_policy(_Ctx(params, method), recorder)

    return asyncio.run(_run()), recorder


def _refused(params: object, method: str = "tools/call") -> bool:
    """Return whether a remote ``tools/call`` was refused with an error result."""
    result, _ = _call(params, method)
    return isinstance(result, dict) and result.get("isError") is True


def _audit_records(tmp_path: Path) -> list[dict[str, Any]]:
    """Return the host audit records written under *tmp_path*."""
    path = tmp_path / HOST_AUDIT_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the audit stream at a temporary plan directory."""
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    return tmp_path


# --- refusals -------------------------------------------------------------


def test_remote_principal_cannot_call_save_plan() -> None:
    assert _refused({"name": "save_plan"}) is True


def test_refused_call_never_reaches_the_handler() -> None:
    _, recorder = _call({"name": "save_plan"})
    assert recorder.calls == 0


def test_refusal_is_audited_as_a_denied_tool_call(tmp_path: Path) -> None:
    _call({"name": "save_plan"})
    assert [
        (r["action"], r["outcome"], r["detail"]["tool"])
        for r in _audit_records(tmp_path)
        if r["action"] == "tool:call"
    ] == [("tool:call", "denied", "save_plan")]


def test_remote_principal_cannot_call_set_story_status() -> None:
    assert _refused({"name": "set_story_status"}) is True


def test_remote_principal_cannot_call_record_security_audit() -> None:
    assert _refused({"name": "record_security_audit"}) is True


def test_remote_principal_cannot_call_patch_plan() -> None:
    # patch_plan rewrites role_config without asking for config:write.
    assert _refused({"name": "patch_plan"}) is True


# --- allowlist ------------------------------------------------------------


def test_enforced_tool_reaches_the_handler() -> None:
    _, recorder = _call({"name": "approve_plan"})
    assert recorder.calls == 1


def test_read_tool_reaches_the_handler() -> None:
    _, recorder = _call({"name": "list_plans"})
    assert recorder.calls == 1


def test_local_operator_is_unaffected() -> None:
    # Nothing bound at all is the stdio / CLI case: it must not hit the policy.
    _, recorder = _call({"name": "save_plan"}, principal=None)
    assert recorder.calls == 1


def test_bound_local_operator_is_unaffected() -> None:
    _, recorder = _call({"name": "save_plan"}, principal=LOCAL_OPERATOR)
    assert recorder.calls == 1


@pytest.mark.parametrize("method", ["tools/list", "initialize"])
def test_non_tools_call_method_passes_through(method: str) -> None:
    _, recorder = _call({"name": "save_plan"}, method=method)
    assert recorder.calls == 1


# --- malformed calls ------------------------------------------------------


def test_call_without_a_tool_name_is_refused() -> None:
    assert _refused({}) is True


def test_call_with_a_non_string_tool_name_is_refused() -> None:
    assert _refused({"name": 12345}) is True


def test_call_with_an_unhashable_tool_name_is_refused() -> None:
    assert _refused({"name": {"tool": "save_plan"}}) is True


def test_call_with_non_dict_params_is_refused() -> None:
    assert _refused(["save_plan"]) is True


def test_audit_detail_truncates_a_long_tool_name(tmp_path: Path) -> None:
    _call({"name": "a" * 70})
    assert [r["detail"]["tool"] for r in _audit_records(tmp_path)] == ["a" * 64]


def test_audit_failure_still_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _boom(**kwargs: object) -> object:
        raise OSError("read-only filesystem")

    monkeypatch.setattr("pipeline.remote_tool_policy.append_host_audit", _boom)
    result, recorder = _call({"name": "save_plan"})
    assert recorder.calls == 0 and result["isError"] is True


# --- consecutive calls ----------------------------------------------------


def test_consecutive_calls_keep_every_refusal(tmp_path: Path) -> None:
    _call({"name": "save_plan"})
    _call({"name": "save_plan"})
    _, recorder = _call({"name": "approve_plan"})
    denied = [r for r in _audit_records(tmp_path) if r["outcome"] == "denied"]
    assert (
        len(denied),
        [r["detail"]["tool"] for r in denied],
        recorder.calls,
    ) == (2, ["save_plan", "save_plan"], 1)


# --- serve() installs it --------------------------------------------------

REMOTE_ENV = {
    "PIPELINE_MCP_TRANSPORT": "streamable-http",
    IDENTITY_PROVIDER_ENV: "oidc",
}


class _FakeMCP:
    """Mirror of the SDK app host: records factories and the middleware list."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.middleware: list[object] = []

    def streamable_http_app(self, host: str | None = None) -> object:
        self.calls.append(("streamable_http_app", host))
        return object()

    def sse_app(self, host: str | None = None) -> object:
        self.calls.append(("sse_app", host))
        return object()


def test_serve_installs_the_remote_tool_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _FakeMCP()
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=Mock()))
    monkeypatch.setattr(
        rt, "build_identity_provider", lambda *a, **k: Mock(spec=OidcProvider)
    )
    rt.serve(mcp, environ=dict(REMOTE_ENV))
    assert remote_tool_policy in mcp.middleware
