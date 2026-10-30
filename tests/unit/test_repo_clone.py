"""Tests for pipeline/repo_clone.py: URL parsing, managed paths, clone/fetch,
pinning, token handling and the security properties the module promises.

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
import types
from pathlib import Path

import pytest

from pipeline.repo_clone import (
    ClonedRepo,
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


def _fake_subprocess(handler) -> types.SimpleNamespace:
    """A stand-in for the ``subprocess`` module as seen by repo_clone."""
    return types.SimpleNamespace(
        run=handler,
        TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess,
    )


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
    # Embedded whitespace/control characters. urlsplit() drops CR/LF/TAB, so
    # without an explicit check the string that is validated differs from the
    # string git would be handed.
    "https://github.com/o/r\n",
    "https://github.com/o/\tr",
    "https://github.com/o/r\r\n",
    " https://github.com/o/r",
    "https://github.com/o/r\x00",
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


@pytest.mark.parametrize(
    "url",
    [
        "file:///../../etc/widget.git",
        "file:///tmp/../..//evil.git",
        "file://relative/path/acme/widget.git",
        "file:///tmp/acme/widget",
    ],
)
def test_parse_repo_url_rejects_traversing_file_urls(url: str) -> None:
    with pytest.raises(RepoUrlError):
        parse_repo_url(url, environ={"PIPELINE_REPO_URL_ALLOW_FILE": "1"})


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

    assert isinstance(target, ClonedRepo)
    assert target == root / "local" / "acme" / "widget"
    assert (target / ".git").is_dir()
    assert _rev("HEAD", target) == first_sha
    assert target.commit == first_sha

    work = tmp_path / "seed-work"
    second_sha = _push(work, "second.txt", "second")
    assert second_sha != first_sha

    assert ensure_clone(url, environ=env) == target
    # ``fetch`` moves the remote-tracking ref, and the worktree is then pinned
    # onto the fetched commit so a caller cannot read stale or swapped content.
    assert _rev("origin/main", target) == second_sha
    assert _rev("HEAD", target) == second_sha
    assert (target / "second.txt").is_file()
    assert ensure_clone(url, environ=env).commit == second_sha
    assert (bare / "refs" / "heads" / "main").is_file()


def test_ensure_clone_removes_untracked_files_from_the_worktree(
    tmp_path: Path, bare_repo
) -> None:
    url, _, _ = bare_repo
    env = _env(tmp_path / "repos")
    target = ensure_clone(url, environ=env)
    injected = target / "injected.py"
    injected.write_text("print('agent written')\n")

    ensure_clone(url, environ=env)

    assert not injected.exists()


def test_ensure_clone_refuses_symlinked_git_directory(
    tmp_path: Path, bare_repo
) -> None:
    url, _, bare = bare_repo
    root = tmp_path / "repos"
    target = root / "local" / "acme" / "widget"
    target.mkdir(parents=True)
    (target / ".git").symlink_to(bare, target_is_directory=True)

    with pytest.raises(RepoCloneError):
        ensure_clone(url, environ=_env(root))


def test_ensure_clone_refuses_directory_without_git(tmp_path: Path, bare_repo) -> None:
    url, _, _ = bare_repo
    root = tmp_path / "repos"
    target = root / "local" / "acme" / "widget"
    target.mkdir(parents=True)
    (target / "leftover.txt").write_text("not a clone\n")

    with pytest.raises(RepoCloneError):
        ensure_clone(url, environ=_env(root))


def test_ensure_clone_refuses_substituted_origin(tmp_path: Path) -> None:
    first_bare = tmp_path / "bare" / "acme" / "widget.git"
    _seed_bare(first_bare, tmp_path / "seed-one", message="one")
    second_bare = tmp_path / "other" / "acme" / "widget.git"
    _seed_bare(second_bare, tmp_path / "seed-two", message="two")
    root = tmp_path / "repos"
    env = _env(root)
    url = f"file://{first_bare}"
    target = ensure_clone(url, environ=env)

    _git("remote", "set-url", "origin", str(second_bare), cwd=target)

    with pytest.raises(RepoCloneError):
        ensure_clone(url, environ=env)


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


def test_token_file_that_is_not_a_regular_file_is_refused(
    tmp_path: Path, bare_repo
) -> None:
    url, _, _ = bare_repo
    fifo = tmp_path / "token-fifo"
    os.mkfifo(fifo)
    fifo.chmod(0o600)
    env = _env(tmp_path / "repos", PIPELINE_GIT_TOKEN_FILE=str(fifo))

    with pytest.raises(RepoCloneError) as excinfo:
        ensure_clone(url, environ=env)
    assert "PIPELINE_GIT_TOKEN_FILE" in str(excinfo.value)


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


def test_git_child_does_not_inherit_pipeline_secrets(
    tmp_path: Path, bare_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline import repo_clone

    url, _, _ = bare_repo
    token = _token_file(tmp_path, 0o600)
    env = _env(
        tmp_path / "repos",
        PIPELINE_GIT_TOKEN_FILE=str(token),
        PLANE_API_KEY="plane-secret",
        PIPELINE_DASHBOARD_API_KEY="dashboard-secret",
        ANTHROPIC_API_KEY="anthropic-secret",
    )
    seen: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv, **kwargs):
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return subprocess.CompletedProcess(argv, 0, stdout="deadbeef" * 5, stderr="")

    monkeypatch.setattr(repo_clone, "subprocess", _fake_subprocess(fake_run))

    ensure_clone(url, environ=env)

    assert seen, "ensure_clone must run git"
    for argv, child_env in seen:
        assert "PLANE_API_KEY" not in child_env
        assert "PIPELINE_DASHBOARD_API_KEY" not in child_env
        assert "ANTHROPIC_API_KEY" not in child_env
        assert child_env["GIT_TERMINAL_PROMPT"] == "0"
        assert child_env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert child_env["GIT_ASKPASS"].endswith("askpass.sh")
        assert child_env["PIPELINE_GIT_TOKEN_FILE"] == str(token)
        assert FAKE_TOKEN not in " ".join(argv)
        assert FAKE_TOKEN not in " ".join(child_env.values())


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


def test_clone_failure_log_sanitises_credentials_in_stderr(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline import repo_clone

    env = _env(tmp_path / "repos")
    stderr = (
        f"fatal: could not read Username for 'https://{FAKE_TOKEN}@github.com': "
        "terminal prompts disabled\nsecond line\n"
    )

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr=stderr)

    monkeypatch.setattr(repo_clone, "subprocess", _fake_subprocess(fake_run))

    with caplog.at_level(logging.DEBUG), pytest.raises(RepoCloneError) as excinfo:
        ensure_clone("https://github.com/o/r", environ=env)

    message = str(excinfo.value)
    assert "clone" in message
    assert "https://github.com/o/r" in message
    assert FAKE_TOKEN not in message
    failures = [r for r in caplog.records if getattr(r, "event", "") == "repo_git_failure"]
    assert failures, "a failed git command must be logged at WARN"
    logged = failures[0].stderr
    assert "https://***@github.com" in logged
    assert FAKE_TOKEN not in logged
    assert "\n" not in logged  # only git's first stderr line is logged
    assert FAKE_TOKEN not in caplog.text


def test_failed_clone_can_be_retried_then_fetches(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    bare = tmp_path / "bare" / "acme" / "late.git"
    url = f"file://{bare}"
    env = _env(root)

    with pytest.raises(RepoCloneError):
        ensure_clone(url, environ=env)  # clone branch fails: no bare repo yet

    first_sha = _seed_bare(bare, tmp_path / "seed-work")
    target = ensure_clone(url, environ=env)  # still the clone branch
    assert target == root / "local" / "acme" / "late"
    assert target.commit == first_sha

    second_sha = _push(tmp_path / "seed-work", "second.txt", "second")
    assert ensure_clone(url, environ=env).commit == second_sha  # fetch branch


def test_git_timeout_becomes_repo_clone_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline import repo_clone

    def slow_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(repo_clone, "subprocess", _fake_subprocess(slow_run))

    with pytest.raises(RepoCloneError) as excinfo:
        ensure_clone(
            "https://github.com/o/r", environ=_env(tmp_path / "repos"), timeout=5
        )
    assert "timed out" in str(excinfo.value)


def test_missing_git_binary_becomes_repo_clone_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _not_found(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("pipeline.repo_clone.subprocess.run", _not_found)
    with pytest.raises(RepoCloneError):
        ensure_clone("https://github.com/o/r", environ=_env(tmp_path / "repos"))


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
    token = _token_file(tmp_path, 0o600)
    with _askpass(token) as auth_env:
        script = Path(auth_env["GIT_ASKPASS"])
        assert script.stat().st_mode & 0o077 == 0
        assert auth_env["GIT_TERMINAL_PROMPT"] == "0"
        assert auth_env["PIPELINE_GIT_TOKEN_FILE"] == str(token)
        # The token itself is never placed in the child environment.
        assert "PIPELINE_GIT_ASKPASS_TOKEN" not in auth_env
        assert FAKE_TOKEN not in " ".join(auth_env.values())
        assert FAKE_TOKEN not in script.read_text()
        child_env = {"PATH": os.defpath, "PIPELINE_GIT_TOKEN_FILE": str(token)}
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


# --- hardening: an existing clone's local config is not trusted ------------


def _managed_clone(tmp_path: Path) -> tuple[Path, dict[str, str], str]:
    """Clone a seeded bare repo through ensure_clone and return its details."""
    bare = tmp_path / "bare" / "acme" / "widget.git"
    _seed_bare(bare, tmp_path / "seed-work")
    root = tmp_path / "repos"
    env = _env(root)
    url = f"file://{bare}"
    return ensure_clone(url, environ=env), env, url


def _fake_managed_clone(target: Path, origin: str) -> None:
    """Build a managed clone whose local config carries ``origin``."""
    target.mkdir(parents=True)
    _git("init", "-q", str(target))
    _git("remote", "add", "origin", origin, cwd=target)
    _git(
        "config",
        "remote.origin.fetch",
        "+refs/heads/*:refs/remotes/origin/*",
        cwd=target,
    )
    _git("config", "remote.origin.tagopt", "--no-tags", cwd=target)


def test_ensure_clone_refuses_local_insteadof_rewrite(tmp_path: Path) -> None:
    """An insteadOf rewrite changes the URL *fetch* resolves, not origin.url."""
    target, env, url = _managed_clone(tmp_path)
    evil = tmp_path / "evil" / "acme" / "widget.git"
    _seed_bare(evil, tmp_path / "evil-work", message="evil")
    _git("config", "--add", f"url.file://{evil}.insteadOf", url, cwd=target)
    (target / "planted.txt").write_text("attacker content\n")

    with pytest.raises(RepoCloneError) as excinfo:
        ensure_clone(url, environ=env)

    message = str(excinfo.value)
    assert "configuration" in message
    assert "evil" not in message


def test_ensure_clone_refuses_duplicate_origin_url(tmp_path: Path) -> None:
    """A second remote.origin.url is the multi-valued variant of a swap.

    ``git config --get`` returns the *last* value, so the attacker URL is added
    first and the legitimate one last: the old key comparison then passed while
    the fetch used the configuration as a whole.
    """
    target, env, url = _managed_clone(tmp_path)
    other = tmp_path / "other" / "acme" / "widget.git"
    _seed_bare(other, tmp_path / "other-work", message="other")
    _git("config", "--unset-all", "remote.origin.url", cwd=target)
    _git("config", "--add", "remote.origin.url", f"file://{other}", cwd=target)
    _git("config", "--add", "remote.origin.url", url, cwd=target)

    with pytest.raises(RepoCloneError):
        ensure_clone(url, environ=env)


def test_ensure_clone_refuses_core_worktree_outside_root(tmp_path: Path) -> None:
    """clean -xffd must never reach a worktree the clone's config points at."""
    target, env, url = _managed_clone(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    important = outside / "important.txt"
    important.write_text("must survive\n")
    _git("config", "core.worktree", str(outside), cwd=target)

    with pytest.raises(RepoCloneError):
        ensure_clone(url, environ=env)

    assert important.is_file()
    assert important.read_text() == "must survive\n"


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com:8443/acme/widget",
        "https://user:pw@github.com/acme/widget",
    ],
)
def test_ensure_clone_refuses_origin_url_with_userinfo_or_port(
    tmp_path: Path, origin: str
) -> None:
    root = tmp_path / "repos"
    target = root / "github.com" / "acme" / "widget"
    _fake_managed_clone(target, origin)

    with pytest.raises(RepoCloneError) as excinfo:
        ensure_clone("https://github.com/acme/widget", environ=_env(root))

    assert "configuration" in str(excinfo.value)


def test_refresh_argv_forces_worktree_and_uses_the_canonical_url(
    tmp_path: Path, bare_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fetch/reset/clean carry core.worktree and never resolve the origin remote."""
    url, _, _ = bare_repo
    env = _env(tmp_path / "repos")
    ensure_clone(url, environ=env)  # create the clone for real

    from pipeline import repo_clone

    seen: list[list[str]] = []
    real_run = subprocess.run

    def spy(argv, **kwargs):
        seen.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(repo_clone.subprocess, "run", spy)
    ensure_clone(url, environ=env)

    mutations = [
        argv
        for argv in seen
        if {"fetch", "reset", "clean"} & set(argv)
    ]
    assert mutations, "the second call must fetch"
    for argv in mutations:
        joined = " ".join(argv)
        assert "core.worktree=" in joined
        assert "core.bare=false" in argv
    fetches = [argv for argv in seen if "fetch" in argv]
    assert fetches, "the second call must fetch"
    assert url in fetches[0]
    assert "origin" not in fetches[0]
    assert fetches[0][-1] == "+refs/heads/*:refs/remotes/origin/*"
