"""Tests for pipeline/repo_clone.py: URL parsing, managed paths, clone/fetch
and token handling.

Every test passes an explicit ``environ`` dict instead of mutating
``os.environ``; the suite runs them in parallel and a leaked PIPELINE_* var
would poison unrelated tests. The clone tests use a real bare repo under
``tmp_path`` reached through a ``file://`` URL, which is why the test-only
``PIPELINE_REPO_URL_ALLOW_FILE=1`` escape hatch exists.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from pipeline.repo_clone import (
    RepoCloneError,
    RepoUrlError,
    _askpass,
    _sanitize,
    ensure_clone,
    managed_path,
    parse_repo_url,
)

FAKE_TOKEN = "ghp_FAKE_TOKEN_VALUE_123"

_GIT_IDENTITY = ("-c", "user.name=SA-04", "-c", "user.email=sa04@example.invalid")


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _rev(rev: str, cwd: Path) -> str:
    return _git("rev-parse", rev, cwd=cwd).stdout.strip()


def _seed_bare(bare: Path, work: Path, message: str = "seed") -> str:
    """Create ``bare`` as a bare repo with one commit on ``main``; return its sha."""
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git("init", "--bare", "-b", "main", str(bare))
    work.mkdir(parents=True, exist_ok=True)
    _git("clone", str(bare), str(work))
    (work / "README.md").write_text(f"{message}\n")
    _git(*_GIT_IDENTITY, "add", "README.md", cwd=work)
    _git(*_GIT_IDENTITY, "commit", "-m", message, cwd=work)
    _git(*_GIT_IDENTITY, "push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    return _rev("HEAD", work)


def _push(work: Path, filename: str, message: str) -> str:
    """Commit a new file in ``work`` and push it to its bare origin; return the sha."""
    (work / filename).write_text(f"{message}\n")
    _git(*_GIT_IDENTITY, "add", filename, cwd=work)
    _git(*_GIT_IDENTITY, "commit", "-m", message, cwd=work)
    _git(*_GIT_IDENTITY, "push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    return _rev("HEAD", work)


@pytest.fixture()
def bare_repo(tmp_path: Path) -> tuple[str, str, Path]:
    """(file URL, first commit sha, bare path) for a bare repo with one commit."""
    bare = tmp_path / "bare" / "acme" / "widget.git"
    sha = _seed_bare(bare, tmp_path / "seed-work")
    return f"file://{bare}", sha, bare


def _env(root: Path, **extra: str) -> dict[str, str]:
    env = {
        "PIPELINE_REPOS_ROOT": str(root),
        "PIPELINE_REPO_URL_ALLOW_FILE": "1",
    }
    env.update(extra)
    return env


def _token_file(tmp_path: Path, mode: int = 0o600) -> Path:
    token = tmp_path / "git-token"
    token.write_text(f"{FAKE_TOKEN}\n")
    token.chmod(mode)
    return token


# --- parse_repo_url: acceptances -------------------------------------------


def test_parse_repo_url_accepts_https_and_strips_git_suffix() -> None:
    assert parse_repo_url("https://github.com/Org/Repo.git", environ={}) == (
        "github.com",
        "Org",
        "Repo",
    )
    assert parse_repo_url("https://github.com/Org/Repo", environ={}) == (
        "github.com",
        "Org",
        "Repo",
    )


def test_parse_repo_url_lowercases_host_and_defaults_port() -> None:
    assert parse_repo_url("https://GitHub.com:443/Org/Repo.git", environ={}) == (
        "github.com",
        "Org",
        "Repo",
    )


def test_parse_repo_url_honours_custom_host_list() -> None:
    env = {"PIPELINE_REPO_URL_HOSTS": " GitHub.com , git.example.com ,,"}
    assert parse_repo_url("https://git.example.com/o/r", environ=env) == (
        "git.example.com",
        "o",
        "r",
    )
    with pytest.raises(RepoUrlError):
        parse_repo_url("https://bitbucket.org/o/r", environ=env)


# --- parse_repo_url: rejections --------------------------------------------

REJECTED_URLS = [
    "http://github.com/o/r",
    "ssh://github.com/o/r",
    "git@github.com:o/r",
    "https://gitlab.com/o/r",
    "https://user:pw@github.com/o/r",
    "https://user@github.com/o/r",
    "https://github.com/o/r?x=1",
    "https://github.com/o/r#f",
    "https://github.com/o",
    "https://github.com/o/r/extra",
    "https://github.com/../r",
    "https://github.com/o/..",
    "https://github.com:8443/o/r",
    "https://github.com:abc/o/r",
    "https://github.com/o/%2e%2e",
    "https://github.com//r",
    "",
]


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_parse_repo_url_rejects_malformed_urls(url: str) -> None:
    with pytest.raises(RepoUrlError):
        parse_repo_url(url, environ={"PIPELINE_REPO_URL_HOSTS": "github.com"})


def test_parse_repo_url_rejects_file_url_without_allowance(bare_repo) -> None:
    url, _, _ = bare_repo
    with pytest.raises(RepoUrlError):
        parse_repo_url(url, environ={})


def test_parse_repo_url_accepts_file_url_only_with_allowance(bare_repo) -> None:
    url, _, _ = bare_repo
    assert parse_repo_url(url, environ={"PIPELINE_REPO_URL_ALLOW_FILE": "1"}) == (
        "local",
        "acme",
        "widget",
    )
    with pytest.raises(RepoUrlError):
        parse_repo_url(url, environ={"PIPELINE_REPO_URL_ALLOW_FILE": "0"})


# --- managed_path ----------------------------------------------------------


def test_managed_path_maps_host_owner_repo(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    path = managed_path(
        "https://github.com/Org/Repo.git", environ={"PIPELINE_REPOS_ROOT": str(root)}
    )
    assert path == root / "github.com" / "Org" / "Repo"


def test_managed_path_requires_repos_root() -> None:
    with pytest.raises(RepoUrlError) as excinfo:
        managed_path("https://github.com/o/r", environ={})
    assert "PIPELINE_REPOS_ROOT" in str(excinfo.value)


def test_managed_path_rejects_escape_through_symlinked_component(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "github.com").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RepoUrlError):
        managed_path(
            "https://github.com/Org/Repo", environ={"PIPELINE_REPOS_ROOT": str(root)}
        )


def test_managed_path_does_not_create_directories(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    managed_path("https://github.com/o/r", environ={"PIPELINE_REPOS_ROOT": str(root)})
    assert not root.exists()


# --- ensure_clone: clone then fetch ----------------------------------------


def test_ensure_clone_clones_then_fetches(tmp_path: Path, bare_repo) -> None:
    url, first_sha, bare = bare_repo
    root = tmp_path / "repos"
    env = _env(root)

    target = ensure_clone(url, environ=env)

    assert target == root / "local" / "acme" / "widget"
    assert (target / ".git").is_dir()
    assert _rev("HEAD", target) == first_sha

    work = tmp_path / "seed-work"
    second_sha = _push(work, "second.txt", "second")
    assert second_sha != first_sha

    assert ensure_clone(url, environ=env) == target
    # fetch moves the remote-tracking ref; the checked-out tree intentionally
    # stays at the first commit (no pull/checkout is performed).
    assert _rev("origin/main", target) == second_sha
    assert (bare / "refs" / "heads" / "main").is_file()


# --- credentials -----------------------------------------------------------


def test_token_file_group_or_world_readable_is_refused(
    tmp_path: Path, bare_repo
) -> None:
    url, _, _ = bare_repo
    root = tmp_path / "repos"
    env = _env(root, PIPELINE_GIT_TOKEN_FILE=str(_token_file(tmp_path, 0o644)))
    with pytest.raises(RepoCloneError):
        ensure_clone(url, environ=env)
    assert not (root / "local" / "acme" / "widget" / ".git").exists()


def test_token_never_reaches_git_config_or_logs(
    tmp_path: Path, bare_repo, caplog: pytest.LogCaptureFixture
) -> None:
    url, _, _ = bare_repo
    root = tmp_path / "repos"
    env = _env(root, PIPELINE_GIT_TOKEN_FILE=str(_token_file(tmp_path, 0o600)))

    with caplog.at_level(logging.DEBUG):
        target = ensure_clone(url, environ=env)
        ensure_clone(url, environ=env)

    assert (target / ".git").is_dir()
    config_text = (target / ".git" / "config").read_text()
    assert FAKE_TOKEN not in config_text
    assert "x-access-token" not in config_text
    assert FAKE_TOKEN not in caplog.text


def test_askpass_temp_dirs_are_cleaned_up_even_on_failure(
    tmp_path: Path, bare_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _, _ = bare_repo
    root = tmp_path / "repos"
    env = _env(root, PIPELINE_GIT_TOKEN_FILE=str(_token_file(tmp_path, 0o600)))
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def _spy(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", _spy)

    ensure_clone(url, environ=env)  # clone
    ensure_clone(url, environ=env)  # fetch
    missing = f"file://{tmp_path}/bare/acme/gone.git"
    with pytest.raises(RepoCloneError):
        ensure_clone(missing, environ=env)  # fails inside the askpass window

    assert len(created) >= 3
    assert [path for path in created if path.exists()] == []


# --- failure handling ------------------------------------------------------


def test_clone_failure_raises_repo_clone_error_without_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "repos"
    missing_url = f"file://{tmp_path}/bare/acme/gone.git"
    env = _env(root, PIPELINE_GIT_TOKEN_FILE=str(_token_file(tmp_path, 0o600)))

    with caplog.at_level(logging.WARNING), pytest.raises(RepoCloneError) as excinfo:
        ensure_clone(missing_url, environ=env)

    message = str(excinfo.value)
    assert "clone" in message
    assert "gone.git" in message
    assert FAKE_TOKEN not in message
    assert FAKE_TOKEN not in caplog.text
    assert "ghp_" not in caplog.text


def test_failed_clone_can_be_retried_then_fetches(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    bare = tmp_path / "bare" / "acme" / "late.git"
    url = f"file://{bare}"
    env = _env(root)

    with pytest.raises(RepoCloneError):
        ensure_clone(url, environ=env)
    target = root / "local" / "acme" / "late"
    assert not (target / ".git").exists()

    first_sha = _seed_bare(bare, tmp_path / "late-work")
    assert ensure_clone(url, environ=env) == target
    assert _rev("HEAD", target) == first_sha

    second_sha = _push(tmp_path / "late-work", "second.txt", "second")
    assert ensure_clone(url, environ=env) == target
    assert _rev("origin/main", target) == second_sha


def test_git_timeout_becomes_repo_clone_error(
    tmp_path: Path, bare_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _, _ = bare_repo

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr("pipeline.repo_clone.subprocess.run", _timeout)
    with pytest.raises(RepoCloneError) as excinfo:
        ensure_clone(url, environ=_env(tmp_path / "repos"), timeout=1)
    assert "timed out" in str(excinfo.value)


def test_missing_git_binary_becomes_repo_clone_error(
    tmp_path: Path, bare_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _, _ = bare_repo

    def _not_found(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr("pipeline.repo_clone.subprocess.run", _not_found)
    with pytest.raises(RepoCloneError):
        ensure_clone(url, environ=_env(tmp_path / "repos"))


def test_sanitize_redacts_credentials_in_https_urls() -> None:
    line = (
        "fatal: could not read Username for "
        "'https://ghp_FAKE123@github.com': terminal prompts disabled"
    )
    cleaned = _sanitize(line)
    assert "ghp_FAKE123" not in cleaned
    assert "https://***@github.com" in cleaned


# --- askpass helper script -------------------------------------------------


def test_askpass_script_answers_username_then_token(tmp_path: Path) -> None:
    env = {"PIPELINE_GIT_TOKEN_FILE": str(_token_file(tmp_path, 0o600))}
    with _askpass(env) as auth_env:
        script = Path(auth_env["GIT_ASKPASS"])
        assert script.stat().st_mode & 0o077 == 0
        assert auth_env["GIT_TERMINAL_PROMPT"] == "0"
        assert auth_env["PIPELINE_GIT_ASKPASS_TOKEN"] == FAKE_TOKEN
        child_env = dict(os.environ, **auth_env)
        prompt = "Username for 'https://github.com': "
        user = subprocess.run(
            [str(script), prompt],
            check=True,
            capture_output=True,
            text=True,
            env=child_env,
        ).stdout.strip()
        secret = subprocess.run(
            [str(script), "Password for 'https://github.com': "],
            check=True,
            capture_output=True,
            text=True,
            env=child_env,
        ).stdout.strip()
    assert user == "x-access-token"
    assert secret == FAKE_TOKEN
    assert not script.exists()
