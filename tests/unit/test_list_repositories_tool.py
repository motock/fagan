"""The ``list_repositories`` MCP tool: its arguments, its bounds and its remote policy."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.identity import Principal
from app.plugin_loader import ALLOWLIST_ENV
from pipeline import server
from pipeline.actor import bind_namespace, bind_principal
from pipeline.remote_tool_policy import (
    REMOTE_READ,
    remote_tool_allowed,
    remote_tool_policy,
)
from pipeline.repo_listing_hooks import MAX_REPOSITORIES, REPO_LISTING_PLUGIN_ENV

ALICE = Principal(subject="https://idp#alice", auth_method="oidc")
NAMESPACE = "team-a"
SENTINEL = object()

_TEMPLATE = """\
class Hook:
    def list_repositories(self, namespace):
@@BODY@@


def build():
    return Hook()
"""


class _Store:
    """Stands in for the record store: no plans, so the default lists nothing."""

    def records(self) -> _Store:
        return self

    def list_plans(self, namespace: str) -> list[str]:
        return []

    def get_manifest(self, namespace: str, plan_name: str) -> dict[str, Any] | None:
        return None


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


@pytest.fixture(autouse=True)
def _no_plugin_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REPO_LISTING_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    fake = _Store()
    monkeypatch.setattr(server, "_store", fake)
    return fake


def _install_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    name = "repo_listing_tool_" + uuid.uuid4().hex
    (tmp_path / f"{name}.py").write_text(
        _TEMPLATE.replace("@@BODY@@", body), encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(REPO_LISTING_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name)


def _call_remote(principal: Principal | None) -> tuple[object, _Recorder]:
    """Drive the remote policy middleware for one ``list_repositories`` call."""
    recorder = _Recorder()
    params = {"name": "list_repositories", "arguments": {}}

    async def _run() -> object:
        if principal is None:
            return await remote_tool_policy(_Ctx(params), recorder)
        with bind_principal(principal):
            return await remote_tool_policy(_Ctx(params), recorder)

    return asyncio.run(_run()), recorder


# --- the tool's shape ----------------------------------------------------------


def test_tool_takes_no_namespace_argument() -> None:
    assert list(inspect.signature(server.list_repositories).parameters) == []


def test_tool_is_registered_on_the_server() -> None:
    assert "list_repositories" in server.mcp._tool_manager._tools


def test_zero_items_answers_ok_with_an_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    _install_plugin(tmp_path, monkeypatch, "        return []")
    with bind_namespace(NAMESPACE):
        assert server.list_repositories() == {"ok": True, "repositories": []}


def test_more_than_the_cap_returns_the_cap_and_marks_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    body = (
        "        return [{'repo': 'git.example.com/team/r%03d' % i, 'level': None, 'state': None}"
        " for i in range(501)]"
    )
    _install_plugin(tmp_path, monkeypatch, body)
    with bind_namespace(NAMESPACE):
        result = server.list_repositories()
    assert len(result["repositories"]) == MAX_REPOSITORIES == 500
    assert result["truncated"] is True


def test_exactly_the_cap_is_not_marked_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    body = (
        "        return [{'repo': 'git.example.com/team/r%03d' % i, 'level': None, 'state': None}"
        " for i in range(500)]"
    )
    _install_plugin(tmp_path, monkeypatch, body)
    with bind_namespace(NAMESPACE):
        result = server.list_repositories()
    assert len(result["repositories"]) == 500
    assert "truncated" not in result


# --- remote transport ----------------------------------------------------------


def test_remote_tool_is_allowed_by_the_policy() -> None:
    assert remote_tool_allowed("list_repositories") is True
    assert "list_repositories" in REMOTE_READ


def test_authenticated_remote_caller_reaches_the_tool() -> None:
    result, recorder = _call_remote(ALICE)
    assert (result, recorder.calls) == (SENTINEL, 1)


def test_unauthenticated_caller_is_refused_the_tool() -> None:
    result, recorder = _call_remote(None)
    assert recorder.calls == 0
    assert result["isError"] is True
