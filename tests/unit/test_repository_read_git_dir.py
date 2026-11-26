"""Repository reads keep ``.git`` out of view, and accept a URL as the repository name.

A managed clone's ``.git`` directory is git's own store: its config, hooks and
object database are not source a remote caller should read, list or search.
Every read that names a managed clone (the MCP read tools and the
``/api/workspace`` routes given a ``repo_url``) refuses a path with a ``.git``
component, omits a ``.git`` entry from a listing and returns no search match
from inside one. The process-global active workspace keeps its behaviour.

The tools also take ``https://host/owner/name`` (optionally ending in ``.git``
or a trailing slash) as the repository name; it is normalised to the bare form
before validation and the audit record carries the normalised name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline import access_hooks, server, server_tools_read, workspace_fs
from pipeline.access_hooks import ACCESS_HOOK_PLUGIN_ENV, AccessDecision
from pipeline.workspace import WorkspaceSecurityError

GRANTED = "github.com/a/one"
INVALID = {"ok": False, "reason": "invalid_repository"}
INVALID_PATH = {"ok": False, "reason": "invalid_path"}


class AllowAll:
    """Stands in for the repository access hook; grants every repository."""

    def check(self, namespace, repo, action, risk):
        return AccessDecision(allowed=True, reason="granted")


@pytest.fixture(autouse=True)
def _no_plugin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ACCESS_HOOK_PLUGIN_ENV, raising=False)
    monkeypatch.delenv("PIPELINE_REPO_URL_HOSTS", raising=False)
    monkeypatch.delenv("PIPELINE_REPO_URL_ALLOW_FILE", raising=False)
    monkeypatch.delenv("PIPELINE_REPO_READ_PER_MINUTE", raising=False)
    monkeypatch.setattr(access_hooks, "_hook", lambda: AllowAll())


@pytest.fixture(autouse=True)
def audit_records(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    stub = lambda **kwargs: records.append(kwargs)
    monkeypatch.setattr(access_hooks, "append_host_audit", stub)
    monkeypatch.setattr(server_tools_read, "append_host_audit", stub)
    return records


@pytest.fixture(autouse=True)
def fresh_rate_window() -> None:
    server_tools_read._READ_WINDOWS.clear()


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A managed clone with a real-looking ``.git`` store and ``.github`` / ``.gitignore``."""
    root = tmp_path / "repos"
    path = root / "github.com" / "a" / "one"
    (path / ".git" / "hooks").mkdir(parents=True)
    (path / ".git" / "config").write_text("needle = in-config\n", encoding="utf-8")
    (path / ".github").mkdir()
    (path / ".github" / "ci.yml").write_text("needle: in-ci\n", encoding="utf-8")
    (path / ".gitignore").write_text("needle-ignored\n", encoding="utf-8")
    (path / "README.md").write_text("hello one\n", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPOS_ROOT", str(root))
    return path


# --- workspace_fs: the one place that decides ----------------------------------


@pytest.mark.parametrize(
    "relative",
    [".git", ".git/config", ".GIT/config", ".Git/hooks", "a/.git/x", "a/.GIT", "%2egit/config", "a%2F.git%2Fx"],
)
def test_resolver_refuses_a_git_component_when_hiding_git(tmp_path: Path, relative: str) -> None:
    with pytest.raises(WorkspaceSecurityError):
        workspace_fs.resolve_within_workspace(str(tmp_path), relative, hide_git=True)


@pytest.mark.parametrize("relative", [".git/config", ".GIT/config"])
def test_resolver_keeps_git_paths_when_not_hiding_git(tmp_path: Path, relative: str) -> None:
    resolved = workspace_fs.resolve_within_workspace(str(tmp_path), relative)
    assert resolved == (tmp_path / relative).resolve()


@pytest.mark.parametrize(
    "relative",
    [".gitignore", ".github", ".github/ci.yml", "git/config", "x.git", "a/.gitx/y", "."],
)
def test_resolver_allows_names_that_only_look_like_git(tmp_path: Path, relative: str) -> None:
    workspace_fs.resolve_within_workspace(str(tmp_path), relative, hide_git=True)


# --- read_repository_file ---------------------------------------------------------


@pytest.mark.parametrize("path", [".git/config", ".GIT/config", "a/.git/x", ".Git"])
def test_read_refuses_a_git_path_with_invalid_path(clone: Path, path: str) -> None:
    assert server.read_repository_file(GRANTED, path) == INVALID_PATH


def test_read_of_git_config_returns_no_content(clone: Path) -> None:
    result = server.read_repository_file(GRANTED, ".git/config")
    assert "in-config" not in repr(result)


def test_read_of_gitignore_is_served(clone: Path) -> None:
    result = server.read_repository_file(GRANTED, ".gitignore")
    assert result == {"ok": True, "path": ".gitignore", "content": "needle-ignored\n"}


def test_read_of_a_file_under_github_is_served(clone: Path) -> None:
    result = server.read_repository_file(GRANTED, ".github/ci.yml")
    assert result["ok"] is True
    assert result["content"] == "needle: in-ci\n"


# --- list_repository_directory ----------------------------------------------------


def test_listing_of_the_root_omits_the_git_entry(clone: Path) -> None:
    names = [e["name"] for e in server.list_repository_directory(GRANTED)["entries"]]
    assert ".git" not in names


def test_listing_of_the_root_keeps_gitignore_and_github(clone: Path) -> None:
    names = [e["name"] for e in server.list_repository_directory(GRANTED)["entries"]]
    assert ".gitignore" in names
    assert ".github" in names
    assert "README.md" in names


def _require_case_sensitive_filesystem(clone: Path) -> None:
    """Skip when .GIT and the fixture's .git are the same directory (macOS, Windows)."""
    if (clone / ".GIT").exists():
        pytest.skip("case-insensitive filesystem: .GIT is the fixture's .git directory")


def test_listing_omits_an_upper_case_git_entry(clone: Path) -> None:
    _require_case_sensitive_filesystem(clone)
    (clone / ".GIT").mkdir()
    names = [e["name"] for e in server.list_repository_directory(GRANTED)["entries"]]
    assert ".GIT" not in names


def test_listing_of_the_git_directory_itself_is_refused(clone: Path) -> None:
    assert server.list_repository_directory(GRANTED, ".git") == INVALID_PATH


def test_listing_of_a_nested_git_directory_is_refused(clone: Path) -> None:
    (clone / "sub" / ".git").mkdir(parents=True)
    assert server.list_repository_directory(GRANTED, "sub/.git") == INVALID_PATH


# --- search_repository -------------------------------------------------------------


def test_search_finds_nothing_inside_the_git_directory(clone: Path) -> None:
    result = server.search_repository(GRANTED, "in-config")
    assert result == {"ok": True, "matches": [], "truncated": False}


def test_search_finds_nothing_inside_an_upper_case_git_directory(clone: Path) -> None:
    _require_case_sensitive_filesystem(clone)
    (clone / ".GIT").mkdir()
    (clone / ".GIT" / "config").write_text("needle-upper\n", encoding="utf-8")
    result = server.search_repository(GRANTED, "needle-upper")
    assert result == {"ok": True, "matches": [], "truncated": False}


def test_search_finds_nothing_inside_a_nested_git_directory(clone: Path) -> None:
    (clone / "sub" / ".git").mkdir(parents=True)
    (clone / "sub" / ".git" / "x").write_text("needle-nested\n", encoding="utf-8")
    assert server.search_repository(GRANTED, "needle-nested")["matches"] == []


def test_search_still_finds_matches_in_github_and_gitignore(clone: Path) -> None:
    matches = server.search_repository(GRANTED, "needle")["matches"]
    assert ".github/ci.yml:1:needle: in-ci" in matches
    assert ".gitignore:1:needle-ignored" in matches
    assert not any(".git/" in m for m in matches)


# --- the URL form of the repository name --------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/a/one",
        "https://github.com/a/one.git",
        "https://github.com/a/one/",
        "https://github.com/a/one.git/",
    ],
)
def test_https_form_reads_the_same_file_as_the_bare_form(clone: Path, url: str) -> None:
    assert server.read_repository_file(url, "README.md") == server.read_repository_file(
        GRANTED, "README.md"
    )


def test_https_form_lists_the_same_entries_as_the_bare_form(clone: Path) -> None:
    assert server.list_repository_directory("https://github.com/a/one") == (
        server.list_repository_directory(GRANTED)
    )


def test_https_form_searches_the_same_matches_as_the_bare_form(clone: Path) -> None:
    assert server.search_repository("https://github.com/a/one.git", "needle") == (
        server.search_repository(GRANTED, "needle")
    )


def test_audit_of_an_https_form_carries_the_normalised_name(
    clone: Path, audit_records: list[dict[str, Any]]
) -> None:
    server.read_repository_file("https://github.com/a/one.git/", "README.md")
    (record,) = [r for r in audit_records if r["action"] == "repo_read"]
    assert record["target"] == {"repository": GRANTED}
    assert record["outcome"] == "ok"


@pytest.mark.parametrize(
    "repo",
    [
        "http://github.com/a/one",
        "https://user@github.com/a/one",
        "https://user:token@github.com/a/one",
        "https://github.com/a/one?ref=main",
        "https://github.com/a/one#readme",
        "https://github.com/a/one/extra",
        "https://github.com/a/one//",
        "https://github.com/a/one.git.git/x",
        "https://",
        "https:///a/one",
        "https://github.com:8443/a/one",
        "https://github.com/a/../one",
        "",
        "not a url",
        "github.com/a/one/",
    ],
    ids=repr,
)
def test_malformed_or_non_https_url_forms_are_invalid_and_never_reach_the_hook(
    clone: Path, monkeypatch: pytest.MonkeyPatch, repo: str
) -> None:
    reached: list[str] = []

    class Recording(AllowAll):
        def check(self, namespace, repo, action, risk):
            reached.append(repo)
            return super().check(namespace, repo, action, risk)

    monkeypatch.setattr(access_hooks, "_hook", lambda: Recording())
    assert server.read_repository_file(repo, "README.md") == INVALID
    assert reached == []


def test_audit_of_a_refused_url_form_does_not_echo_its_credentials(
    clone: Path, audit_records: list[dict[str, Any]]
) -> None:
    server.read_repository_file("https://user:token@github.com/a/one", "README.md")
    assert "token" not in repr(audit_records)


# --- the active workspace keeps its behaviour ---------------------------------------


def test_active_workspace_read_without_repo_url_still_lists_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(server._service, "get_active_workspace", lambda: str(tmp_path))
    names = [e["name"] for e in server._service.list_workspace_directory("")["entries"]]
    assert ".git" in names
