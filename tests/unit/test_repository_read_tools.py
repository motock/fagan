"""The read-only repository tools: a remote caller reads a granted clone, nothing else.

``read_repository_file``, ``list_repository_directory`` and ``search_repository``
serve source from a managed clone. Each one checks the repository access hook
before any clone is named, answers a missing clone without running git, is
rate-limited per principal, and writes one host audit record that carries no
path, pattern or content. The access hook and the audit stream are stubbed; the
clone root is a temporary directory and the service is the real one.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.identity import Principal
from pipeline import access_hooks, server, server_tools_read
from pipeline.access_hooks import ACCESS_HOOK_PLUGIN_ENV, AccessDecision
from pipeline.actor import bind_principal
from pipeline.remote_tool_policy import (
    REMOTE_READ,
    remote_tool_allowed,
    remote_tool_policy,
)

ALICE = Principal(subject="https://idp#alice", auth_method="oidc")
SENTINEL = object()

GRANTED = "github.com/a/one"
GRANTED_NO_CLONE = "github.com/a/empty"
UNGRANTED = "github.com/b/two"
UNGRANTED_NO_CLONE = "github.com/b/none"
REFUSED = {"ok": False, "reason": "repository_access_refused"}
NOT_AVAILABLE = {"ok": False, "reason": "repository_not_available"}
INVALID = {"ok": False, "reason": "invalid_repository"}
DEFAULT_LIMIT = 120
MAX_FILE_BYTES = 200_000

_CALLS = {
    "read_repository_file": lambda repo: server.read_repository_file(repo, "README.md"),
    "list_repository_directory": lambda repo: server.list_repository_directory(repo, ""),
    "search_repository": lambda repo: server.search_repository(repo, "needle"),
}
TOOL_NAMES = sorted(_CALLS)


class StubHook:
    """Stands in for the repository access hook; records every consultation."""

    def __init__(self, *, allowed=(GRANTED, GRANTED_NO_CLONE), error=None):
        self.allowed = allowed
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def check(self, namespace, repo, action, risk):
        self.calls.append({"repo": repo, "action": action})
        if self.error is not None:
            raise self.error
        return AccessDecision(allowed=repo in self.allowed, reason="repo_not_granted")


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
def _no_plugin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ACCESS_HOOK_PLUGIN_ENV, raising=False)
    monkeypatch.delenv("PIPELINE_REPO_URL_HOSTS", raising=False)
    monkeypatch.delenv("PIPELINE_REPO_URL_ALLOW_FILE", raising=False)
    monkeypatch.delenv("PIPELINE_REPO_READ_PER_MINUTE", raising=False)


@pytest.fixture(autouse=True)
def hook(monkeypatch: pytest.MonkeyPatch) -> StubHook:
    stub = StubHook()
    monkeypatch.setattr(access_hooks, "_hook", lambda: stub)
    return stub


@pytest.fixture(autouse=True)
def audit_records(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every host audit append; no test writes the real audit chain."""
    records: list[dict[str, Any]] = []
    stub = lambda **kwargs: records.append(kwargs)
    monkeypatch.setattr(access_hooks, "append_host_audit", stub)
    monkeypatch.setattr(server_tools_read, "append_host_audit", stub)
    return records


@pytest.fixture(autouse=True)
def fresh_rate_window() -> None:
    server_tools_read._READ_WINDOWS.clear()


@pytest.fixture
def repos_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A managed-clone root: a clone for the granted repository and an ungranted one."""
    root = tmp_path / "repos"
    for owner, name in (("a", "one"), ("b", "two")):
        clone = root / "github.com" / owner / name
        (clone / "src").mkdir(parents=True)
        (clone / "README.md").write_text("hello one\n", encoding="utf-8")
        (clone / "src" / "app.py").write_text("needle = 1\n", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPOS_ROOT", str(root))
    return root


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record every subprocess the process starts; none is expected here."""
    calls: list[Any] = []

    def record(*args: Any, **kwargs: Any) -> None:
        calls.append(args)
        raise AssertionError("a repository read must not start a subprocess")

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(subprocess, "Popen", record)
    return calls


def _call_remote(name: str, principal: Principal | None) -> tuple[object, _Recorder]:
    """Drive the remote policy middleware for one call of ``name``."""
    recorder = _Recorder()
    params = {"name": name, "arguments": {"repo": GRANTED, "path": "README.md"}}

    async def _run() -> object:
        if principal is None:
            return await remote_tool_policy(_Ctx(params), recorder)
        with bind_principal(principal):
            return await remote_tool_policy(_Ctx(params), recorder)

    return asyncio.run(_run()), recorder


def _call_many(tool: str, repo: str, count: int) -> dict[str, Any]:
    """Make ``count`` calls of ``tool`` on ``repo`` and return the last answer."""
    result: dict[str, Any] = {}
    for _ in range(count):
        result = _CALLS[tool](repo)
    return result


# --- the tools' shape -----------------------------------------------------------


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_tool_is_registered_on_the_server(tool: str) -> None:
    assert tool in server.mcp._tool_manager._tools


# --- a granted repository is served ---------------------------------------------


def test_read_returns_the_service_result_for_a_granted_repository(repos_root: Path) -> None:
    result = server.read_repository_file(GRANTED, "README.md")
    assert result == {"ok": True, "path": "README.md", "content": "hello one\n"}


def test_directory_listing_names_the_entries_of_a_granted_repository(repos_root: Path) -> None:
    result = server.list_repository_directory(GRANTED, "src")
    assert result == {"ok": True, "path": "src", "entries": [{"name": "app.py", "type": "file"}]}


def test_directory_listing_of_the_root_names_the_top_level_entries(repos_root: Path) -> None:
    result = server.list_repository_directory(GRANTED)
    names = [entry["name"] for entry in result["entries"]]
    assert names == ["README.md", "src"]


def test_search_returns_the_matching_line_of_a_granted_repository(repos_root: Path) -> None:
    result = server.search_repository(GRANTED, "needle")
    assert result["ok"] is True
    assert any(line.endswith("src/app.py:1:needle = 1") for line in result["matches"])


def test_search_match_names_its_path_relative_to_the_clone(repos_root: Path) -> None:
    result = server.search_repository(GRANTED, "needle")
    assert "src/app.py:1:needle = 1" in result["matches"]


def test_search_match_never_names_the_server_repos_root(repos_root: Path) -> None:
    result = server.search_repository(GRANTED, "needle")
    assert result["matches"]
    assert not any(str(repos_root) in match for match in result["matches"])


def test_search_returns_at_most_200_results(repos_root: Path) -> None:
    (repos_root / "github.com" / "a" / "one" / "many.txt").write_text(
        "needle\n" * 250, encoding="utf-8"
    )
    result = server.search_repository(GRANTED, "needle")
    assert len(result["matches"]) == 200
    assert result["truncated"] is True


def test_file_at_exactly_the_size_limit_is_served(repos_root: Path) -> None:
    body = "a" * MAX_FILE_BYTES
    (repos_root / "github.com" / "a" / "one" / "big.txt").write_text(body, encoding="utf-8")
    result = server.read_repository_file(GRANTED, "big.txt")
    assert result["content"] == body


def test_file_over_the_size_limit_is_refused_unread(repos_root: Path) -> None:
    (repos_root / "github.com" / "a" / "one" / "big.txt").write_text(
        "a" * (MAX_FILE_BYTES + 1), encoding="utf-8"
    )
    result = server.read_repository_file(GRANTED, "big.txt")
    assert result == {"ok": False, "reason": "file_too_large"}


# --- the access check -----------------------------------------------------------


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_ungranted_repository_is_refused_with_a_clone_present(
    repos_root: Path, tool: str
) -> None:
    assert _CALLS[tool](UNGRANTED) == REFUSED


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_ungranted_repository_is_refused_without_a_clone(repos_root: Path, tool: str) -> None:
    assert _CALLS[tool](UNGRANTED_NO_CLONE) == REFUSED


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_refusal_is_the_same_with_and_without_a_clone(repos_root: Path, tool: str) -> None:
    assert _CALLS[tool](UNGRANTED) == _CALLS[tool](UNGRANTED_NO_CLONE)


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_a_hook_that_raises_refuses_the_repository(repos_root: Path, hook: StubHook, tool: str) -> None:
    hook.error = RuntimeError("plugin failure")
    assert _CALLS[tool](GRANTED) == REFUSED


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_granted_repository_without_a_clone_is_not_available(repos_root: Path, tool: str) -> None:
    assert _CALLS[tool](GRANTED_NO_CLONE) == NOT_AVAILABLE


def test_no_git_command_runs_for_a_granted_repository_without_a_clone(
    repos_root: Path, git_calls: list[Any]
) -> None:
    for tool in TOOL_NAMES:
        _CALLS[tool](GRANTED_NO_CLONE)
    assert git_calls == []


# --- the repository identifier --------------------------------------------------


_BAD_REPOSITORIES = [
    "https://github.com/a/one",
    "user:token@github.com/a/one",
    "github.com/a/../one",
    "github.com/a/one two",
    "github.com/a/one?ref=main",
    "",
    None,
    42,
]


@pytest.mark.parametrize("tool", TOOL_NAMES)
@pytest.mark.parametrize("repo", _BAD_REPOSITORIES, ids=repr)
def test_malformed_repository_is_invalid_and_never_reaches_the_hook(
    repos_root: Path, hook: StubHook, tool: str, repo: Any
) -> None:
    result = {
        "read_repository_file": lambda: server.read_repository_file(repo, "README.md"),
        "list_repository_directory": lambda: server.list_repository_directory(repo, ""),
        "search_repository": lambda: server.search_repository(repo, "needle"),
    }[tool]()
    assert result == INVALID
    assert hook.calls == []


def test_audit_of_a_malformed_repository_does_not_echo_its_credentials(
    repos_root: Path, audit_records: list[dict[str, Any]]
) -> None:
    server.read_repository_file("user:token@github.com/a/one", "README.md")
    assert "token" not in repr(audit_records)


# --- paths that escape the clone -------------------------------------------------


@pytest.mark.parametrize("path", ["../x", "/etc/hosts", "src/../../x"])
def test_path_escaping_the_clone_is_refused_with_a_generic_reason(
    repos_root: Path, path: str
) -> None:
    assert server.read_repository_file(GRANTED, path) == {"ok": False, "reason": "invalid_path"}


def test_symlink_out_of_the_clone_is_refused_and_the_outside_file_is_not_read(
    repos_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE SECRET\n", encoding="utf-8")
    os.symlink(outside, repos_root / "github.com" / "a" / "one" / "leak.txt")
    result = server.read_repository_file(GRANTED, "leak.txt")
    assert result == {"ok": False, "reason": "invalid_path"}
    assert "OUTSIDE SECRET" not in repr(result)


def test_symlinked_directory_out_of_the_clone_is_refused(repos_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    os.symlink(outside, repos_root / "github.com" / "a" / "one" / "linkdir")
    assert server.list_repository_directory(GRANTED, "linkdir") == {
        "ok": False,
        "reason": "invalid_path",
    }


# --- rate limit -------------------------------------------------------------------


def test_request_121_in_a_minute_is_rate_limited(repos_root: Path) -> None:
    _call_many("list_repository_directory", GRANTED, DEFAULT_LIMIT)
    result = server.list_repository_directory(GRANTED, "")
    assert result["ok"] is False
    assert result["reason"] == "rate_limited"
    assert result["retry_after_seconds"] >= 1


@pytest.mark.parametrize("raw", ["abc", "0", "-5", ""])
def test_malformed_limit_uses_the_default_of_120(
    repos_root: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("PIPELINE_REPO_READ_PER_MINUTE", raw)
    _call_many("list_repository_directory", GRANTED, DEFAULT_LIMIT - 1)
    assert server.list_repository_directory(GRANTED, "")["ok"] is True
    assert server.list_repository_directory(GRANTED, "")["reason"] == "rate_limited"


def test_configured_limit_is_honoured(repos_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIPELINE_REPO_READ_PER_MINUTE", "2")
    _call_many("list_repository_directory", GRANTED, 2)
    assert server.list_repository_directory(GRANTED, "")["reason"] == "rate_limited"


def test_refused_calls_count_toward_the_limit(repos_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIPELINE_REPO_READ_PER_MINUTE", "2")
    _call_many("list_repository_directory", UNGRANTED, 2)
    assert server.list_repository_directory(GRANTED, "")["reason"] == "rate_limited"


# --- audit ---------------------------------------------------------------------------


def test_audit_record_names_the_tool_repository_and_outcome(
    repos_root: Path, audit_records: list[dict[str, Any]]
) -> None:
    server.read_repository_file(GRANTED, "README.md")
    (record,) = [r for r in audit_records if r["action"] == "repo_read"]
    assert record["outcome"] == "ok"
    assert record["target"] == {"repository": GRANTED}
    assert record["detail"]["tool"] == "read_repository_file"
    assert "correlation_id" in record["detail"]


def test_refused_call_is_audited_with_its_refusal_outcome(
    repos_root: Path, audit_records: list[dict[str, Any]]
) -> None:
    server.list_repository_directory(UNGRANTED, "")
    outcomes = [r["outcome"] for r in audit_records if r.get("action") == "repo_read"]
    assert outcomes == ["repository_access_refused"]


def test_audit_record_carries_no_pattern_or_matched_content(
    repos_root: Path, audit_records: list[dict[str, Any]]
) -> None:
    server.search_repository(GRANTED, "needle")
    assert "needle" not in repr(audit_records)


def test_audit_record_carries_no_path_or_file_content(
    repos_root: Path, audit_records: list[dict[str, Any]]
) -> None:
    server.read_repository_file(GRANTED, "README.md")
    assert "README" not in repr(audit_records)
    assert "hello one" not in repr(audit_records)


def test_a_failing_audit_append_does_not_change_the_answer(
    repos_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(**kwargs: Any) -> None:
        raise OSError("audit volume full")

    monkeypatch.setattr(server_tools_read, "append_host_audit", broken)
    assert server.read_repository_file(GRANTED, "README.md")["ok"] is True


# --- remote transport -----------------------------------------------------------------


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_repository_tool_is_a_remote_read(tool: str) -> None:
    assert tool in REMOTE_READ
    assert remote_tool_allowed(tool) is True


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_authenticated_remote_caller_reaches_the_repository_tool(tool: str) -> None:
    result, recorder = _call_remote(tool, ALICE)
    assert (result, recorder.calls) == (SENTINEL, 1)


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_unauthenticated_remote_caller_is_refused_the_repository_tool(tool: str) -> None:
    result, recorder = _call_remote(tool, None)
    assert recorder.calls == 0
    assert result["isError"] is True
