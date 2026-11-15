"""Remote settings administration is authorised per layer through the authorizer seam.

The remote transport reaches ``set_setting``, ``get_settings`` and
``list_settings`` only for a caller the authorizer allows on the layer it names.
With no plugin configured the policy is ``LocalPolicy``, which refuses every
remote caller, so a remote settings call is refused by the transport exactly as
before. The deployment layer is host-only and is refused remotely whatever the
authorizer says.

The middleware is driven directly with a minimal context object, and the store
is real and file-backed over a throwaway plans directory. Only the authorizer
is stubbed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import pipeline.persistence
from app.identity import LOCAL_OPERATOR, Principal
from pipeline import authorization, settings_tools
from pipeline import server as p
from pipeline.actor import bind_principal
from pipeline.audit import HOST_AUDIT_FILE
from pipeline.remote_tool_policy import remote_tool_policy
from pipeline.store_records import DEFAULT_NAMESPACE

REPO = "github.com/acme/widgets"
ALICE = Principal(subject="https://idp#alice", auth_method="oidc")
AGENT = Principal(
    subject="https://idp#builder",
    auth_method="oidc",
    principal_type="agent",
    team_scope="acme/layer-1",
)

#: The transport's refusal for a tool the remote policy does not allow.
REFUSED = {
    "content": [
        {
            "type": "text",
            "text": "This tool is not available over the remote transport.",
        }
    ],
    "isError": True,
}

#: Distinguishes "the handler ran" from any dict the middleware might return.
SENTINEL = object()

SETTINGS_TOOLS = ("set_setting", "get_settings", "list_settings")


class _Ctx:
    def __init__(self, params: object, method: str = "tools/call") -> None:
        self.method = method
        self.params = params


class _Recorder:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx: Any) -> object:
        self.calls += 1
        return SENTINEL


class _Layer:
    """Allows one action on one layer of ``REPO``, and records each question."""

    def __init__(self, action: str, team: str) -> None:
        self.action = action
        self.team = team
        self.asked: list[tuple[object, object]] = []

    def allow(self, principal, action, resource=None):
        self.asked.append((action, resource))
        return (
            action == self.action
            and resource is not None
            and resource.team == self.team
            and resource.repo == REPO
        )


class _AllowAll:
    def __init__(self) -> None:
        self.asked: list[tuple[object, object]] = []

    def allow(self, principal, action, resource=None):
        self.asked.append((action, resource))
        return True


class _Raise:
    def allow(self, principal, action, resource=None):
        raise RuntimeError("policy backend unreachable")


def _set_args(
    scope: str = "team_repo",
    scope_id: str = "layer-1",
    repo: str = REPO,
    value: int = 600,
) -> dict:
    return {
        "scope": scope,
        "scope_id": scope_id,
        "repo": repo,
        "key": "merge_ci_timeout_s",
        "value": value,
        "expected_version": None,
    }


def _get_args(
    scope: str = "team_repo", scope_id: str = "layer-1", repo: str = REPO
) -> dict:
    return {"scope": scope, "scope_id": scope_id, "repo": repo}


def _params(name: str) -> dict:
    arguments = {
        "set_setting": _set_args(),
        "get_settings": _get_args(),
        "list_settings": {},
    }[name]
    return {"name": name, "arguments": arguments}


def _through_middleware(params: dict, principal: Principal) -> tuple[object, _Recorder]:
    recorder = _Recorder()

    async def _run() -> object:
        with bind_principal(principal):
            return await remote_tool_policy(_Ctx(params), recorder)

    return asyncio.run(_run()), recorder


def _stored(scope: str, scope_id: str, repo: str = REPO) -> dict | None:
    return p._store.records().get_settings(DEFAULT_NAMESPACE, scope, scope_id, repo)


def _audit_records(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / HOST_AUDIT_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _plans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def install_authorizer(monkeypatch: pytest.MonkeyPatch):
    """Replace the seam's authorizer with a stub and keep enforcement on."""
    monkeypatch.delenv(authorization.ENFORCE_ENV, raising=False)

    def install(stub: object) -> object:
        monkeypatch.setattr(authorization, "default_authorizer", lambda: stub)
        return stub

    return install


# --- default policy: no plugin configured ----------------------------------


@pytest.mark.parametrize("name", SETTINGS_TOOLS)
def test_default_policy_refuses_a_remote_settings_call_before_its_handler(
    name: str, tmp_path: Path
) -> None:
    result, recorder = _through_middleware(_params(name), ALICE)

    assert result == REFUSED
    assert recorder.calls == 0
    denials = [r for r in _audit_records(tmp_path) if r["outcome"] == "denied"]
    assert denials and denials[-1]["action"] == "tool:call"


def test_default_policy_refuses_a_remote_write_at_the_service_and_writes_nothing() -> (
    None
):
    with bind_principal(ALICE):
        result = settings_tools.set_setting(**_set_args())

    assert result["ok"] is False
    assert result["reason"] == "not_authorized"
    assert _stored("team_repo", "layer-1") is None


def test_default_policy_refuses_a_remote_read_at_the_service() -> None:
    with bind_principal(ALICE):
        result = settings_tools.get_settings(**_get_args())

    assert result == {
        "ok": False,
        "reason": "not_authorized",
        "error": result["error"],
    }


def test_default_policy_refuses_a_remote_listing_at_the_service() -> None:
    with bind_principal(ALICE):
        result = settings_tools.list_settings()

    assert result["ok"] is False
    assert result["reason"] == "not_authorized"


def test_local_operator_write_without_a_plugin_is_unchanged(tmp_path: Path) -> None:
    result = settings_tools.set_setting(
        scope="deployment",
        scope_id="",
        repo="",
        key="merge_ci_timeout_s",
        value=600,
        expected_version=None,
    )

    assert result["ok"] is True
    assert result["new_version"] == 1
    allowed = [
        r
        for r in _audit_records(tmp_path)
        if r.get("action") == "settings:set" and r.get("outcome") == "allowed"
    ]
    assert len(allowed) == 1


def test_local_operator_read_without_a_plugin_is_unchanged() -> None:
    settings_tools.set_setting(
        scope="deployment",
        scope_id="",
        repo="",
        key="merge_ci_timeout_s",
        value=600,
        expected_version=None,
    )

    result = settings_tools.get_settings("deployment", "", "")

    assert result["ok"] is True
    assert result["record"]["values"] == {"merge_ci_timeout_s": 600}


# --- a stub authorizer allowing one layer write ----------------------------


def test_stub_allowing_one_layer_write_lets_the_remote_write_through(
    install_authorizer,
) -> None:
    stub = install_authorizer(_Layer("config:write", "layer-1"))

    with bind_principal(ALICE):
        result = settings_tools.set_setting(**_set_args())

    assert result["ok"] is True
    assert _stored("team_repo", "layer-1")["values"] == {"merge_ci_timeout_s": 600}
    assert stub.asked[0][0] == "config:write"
    assert stub.asked[0][1].team == "layer-1"
    assert stub.asked[0][1].repo == REPO


def test_stub_allowing_one_layer_write_refuses_another_layer(
    install_authorizer,
) -> None:
    install_authorizer(_Layer("config:write", "layer-1"))

    with bind_principal(ALICE):
        result = settings_tools.set_setting(**_set_args(scope_id="layer-2"))

    assert result["ok"] is False
    assert result["reason"] == "not_authorized"
    assert _stored("team_repo", "layer-2") is None


def test_stub_allowing_one_layer_write_passes_the_middleware_for_that_layer(
    install_authorizer,
) -> None:
    install_authorizer(_Layer("config:write", "layer-1"))

    result, recorder = _through_middleware(_params("set_setting"), ALICE)

    assert result is SENTINEL
    assert recorder.calls == 1


def test_stub_allowing_one_layer_write_refuses_the_middleware_for_another_layer(
    install_authorizer,
) -> None:
    install_authorizer(_Layer("config:write", "layer-1"))
    params = {"name": "set_setting", "arguments": _set_args(scope_id="layer-2")}

    result, recorder = _through_middleware(params, ALICE)

    assert result == REFUSED
    assert recorder.calls == 0


def test_stub_allowing_one_layer_read_lets_the_remote_read_through(
    install_authorizer,
) -> None:
    install_authorizer(_Layer("config:read", "layer-1"))

    with bind_principal(ALICE):
        result = settings_tools.get_settings(**_get_args())

    assert result == {"ok": True, "record": None}


def test_stub_allowing_one_layer_read_refuses_another_layer(install_authorizer) -> None:
    install_authorizer(_Layer("config:read", "layer-1"))

    with bind_principal(ALICE):
        result = settings_tools.get_settings(**_get_args(scope_id="layer-2"))

    assert result["ok"] is False
    assert result["reason"] == "not_authorized"


def test_stub_allowing_only_one_layer_refuses_a_remote_listing(
    install_authorizer,
) -> None:
    install_authorizer(_Layer("config:read", "layer-1"))

    result, recorder = _through_middleware(_params("list_settings"), ALICE)

    assert result == REFUSED
    assert recorder.calls == 0


# --- deployment layer is host-only remotely --------------------------------


def test_deployment_write_is_refused_by_the_middleware_even_when_the_stub_allows(
    install_authorizer,
) -> None:
    install_authorizer(_AllowAll())
    params = {
        "name": "set_setting",
        "arguments": _set_args(scope="deployment", scope_id="", repo=""),
    }

    result, recorder = _through_middleware(params, ALICE)

    assert result == REFUSED
    assert recorder.calls == 0


def test_deployment_read_is_refused_by_the_middleware_even_when_the_stub_allows(
    install_authorizer,
) -> None:
    install_authorizer(_AllowAll())
    params = {
        "name": "get_settings",
        "arguments": _get_args(scope="deployment", scope_id="", repo=""),
    }

    result, recorder = _through_middleware(params, ALICE)

    assert result == REFUSED
    assert recorder.calls == 0


def test_remote_listing_omits_deployment_records_and_keeps_team_repo_records(
    install_authorizer,
) -> None:
    install_authorizer(_AllowAll())
    settings_tools.set_setting(
        scope="deployment",
        scope_id="",
        repo="",
        key="merge_ci_timeout_s",
        value=600,
        expected_version=None,
    )
    settings_tools.set_setting(**_set_args(value=900))

    with bind_principal(ALICE):
        result = settings_tools.list_settings()

    assert result["ok"] is True
    scopes = [record["scope"] for record in result["records"]]
    assert "deployment" not in scopes
    assert "team_repo" in scopes


# --- agent principals and failing authorizers stay refused ----------------


def test_agent_principal_is_refused_a_remote_write_even_when_the_stub_allows(
    install_authorizer,
) -> None:
    install_authorizer(_AllowAll())

    with bind_principal(AGENT):
        result = settings_tools.set_setting(**_set_args())

    assert result["ok"] is False
    assert result["reason"] == "agent_principal"
    assert _stored("team_repo", "layer-1") is None


def test_failing_authorizer_refuses_a_remote_write(install_authorizer) -> None:
    install_authorizer(_Raise())

    with bind_principal(ALICE):
        result = settings_tools.set_setting(**_set_args())

    assert result["ok"] is False
    assert result["reason"] == "not_authorized"
    assert _stored("team_repo", "layer-1") is None


def test_refused_remote_write_is_audited_as_a_denied_settings_change(
    tmp_path: Path,
) -> None:
    with bind_principal(ALICE):
        settings_tools.set_setting(**_set_args())

    changes = [r for r in _audit_records(tmp_path) if r.get("action") == "settings:set"]
    assert changes and changes[-1]["outcome"] == "denied"
    assert LOCAL_OPERATOR.subject not in json.dumps(changes[-1])
