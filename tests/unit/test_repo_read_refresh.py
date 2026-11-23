"""A repository read refreshes an existing managed clone at most once a minute.

``read_root_kwargs`` names the managed clone a read serves. Once the clone
exists, a read that finds the last refresh older than ``READ_REFRESH_SECONDS``
fetches the default branch and fast-forwards the clone's checkout, but only when
the clone sits on the default branch with a clean tree and a fast-forward is
possible. Every other case leaves the checkout alone. A refresh never fails a
read, never waits for a lock another reader holds, and logs one warning without
the URL, the path or git's stderr.

The git repositories are real (a bare origin under ``tmp_path``, reached through
a test-only ``file://`` URL); only the monotonic clock is replaced.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

import pytest

from pipeline import access_hooks, repo_clone, repo_read_access
from pipeline.access_hooks import ACCESS_HOOK_PLUGIN_ENV, AccessDecision

_GIT_IDENTITY = ("-c", "user.name=TLC-03", "-c", "user.email=tlc03@example.invalid")


class FakeClock:
    """Stands in for ``time.monotonic``; the test moves ``now`` explicitly."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class DenyHook:
    """Refuses every read; records that it was consulted."""

    def __init__(self) -> None:
        self.allowed = False

    def check(self, namespace, repo, action, risk):
        return AccessDecision(allowed=self.allowed, reason="repo_not_granted")


class RefreshSpy:
    """Wraps the real ``refresh_default_branch`` and records each call."""

    def __init__(self, real) -> None:
        self.real = real
        self.calls: list[float] = []

    def __call__(self, url, environ=None, timeout=600):
        self.calls.append(timeout)
        return self.real(url, environ, timeout)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *_GIT_IDENTITY, *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _rev(rev: str, cwd: Path) -> str:
    return _git("rev-parse", rev, cwd=cwd).stdout.strip()


def _commit_and_push(work: Path, filename: str, message: str) -> str:
    (work / filename).write_text(f"{message}\n", encoding="utf-8")
    _git("add", filename, cwd=work)
    _git("commit", "-m", message, cwd=work)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    return _rev("HEAD", work)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(ACCESS_HOOK_PLUGIN_ENV, raising=False)
    monkeypatch.delenv("PIPELINE_REPO_URL_HOSTS", raising=False)
    monkeypatch.setenv("PIPELINE_REPO_URL_ALLOW_FILE", "1")


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(repo_read_access, "_clock", fake)
    repo_read_access._FAILED_AT.clear()
    repo_read_access._REFRESHED_AT.clear()
    return fake


@pytest.fixture
def repos_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setenv("PIPELINE_REPOS_ROOT", str(root))
    return root


@pytest.fixture(autouse=True)
def spy(monkeypatch) -> RefreshSpy:
    fake = RefreshSpy(repo_clone.refresh_default_branch)
    monkeypatch.setattr(repo_read_access, "refresh_default_branch", fake)
    return fake


@pytest.fixture
def origin(tmp_path: Path) -> tuple[str, Path, Path]:
    """(file URL, bare origin path, seed work tree) with one commit on ``main``."""
    bare = tmp_path / "bare" / "acme" / "widget.git"
    bare.parent.mkdir(parents=True)
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / "seed-work"
    _git("clone", str(bare), str(work), cwd=tmp_path)
    (work / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=work)
    _git("commit", "-m", "seed", cwd=work)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    return f"file://{bare}", bare, work


def _first_read(url: str) -> str:
    """Make the first read, which clones the origin; return the clone's root."""
    return repo_read_access.read_root_kwargs(url)["root"]


def test_commit_pushed_after_the_first_read_is_visible_once_a_minute_has_passed(
    origin, repos_root, clock
):
    url, _bare, work = origin
    root = _first_read(url)
    _commit_and_push(work, "fresh.txt", "fresh")

    clock.now += 10
    repo_read_access.read_root_kwargs(url)
    assert not Path(root, "fresh.txt").exists()

    clock.now += 51
    repo_read_access.read_root_kwargs(url)
    assert Path(root, "fresh.txt").read_text(encoding="utf-8") == "fresh\n"


def test_two_reads_inside_the_window_run_one_fetch(origin, repos_root, clock, spy):
    url, _, _ = origin
    _first_read(url)

    clock.now += 60
    repo_read_access.read_root_kwargs(url)
    clock.now += 10
    repo_read_access.read_root_kwargs(url)

    assert len(spy.calls) == 1


def test_refresh_timeout_is_no_longer_than_twenty_seconds(origin, repos_root, clock, spy):
    url, _, _ = origin
    _first_read(url)

    clock.now += 60
    repo_read_access.read_root_kwargs(url)

    assert spy.calls == [repo_read_access.READ_REFRESH_TIMEOUT_SECONDS]
    assert repo_read_access.READ_REFRESH_TIMEOUT_SECONDS <= 20


def test_access_refusal_runs_no_fetch(origin, repos_root, clock, spy, monkeypatch):
    url, _, _ = origin
    _first_read(url)
    deny = DenyHook()
    monkeypatch.setattr(access_hooks, "_hook", lambda: deny)

    clock.now += 60
    with pytest.raises(repo_read_access.RepoReadRefused):
        repo_read_access.read_root_kwargs(url)

    assert spy.calls == []


def test_failed_fetch_still_answers_the_read_and_logs_one_warning_without_detail(
    origin, repos_root, clock, caplog
):
    url, bare, _ = origin
    root = _first_read(url)
    moved = bare.with_name("moved.git")
    bare.rename(moved)

    clock.now += 60
    with caplog.at_level(logging.DEBUG):
        result = repo_read_access.read_root_kwargs(url)

    assert result == {"root": root}
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    record = warnings[0]
    assert record.event == "repo_read_refresh_failed"
    assert record.repository == "local/acme/widget"
    assert record.error_type == "RepoCloneError"
    rendered = repr(record.__dict__) + record.getMessage()
    assert "file://" not in rendered
    assert str(bare) not in rendered
    assert "fatal" not in rendered
    moved.rename(bare)


def test_failed_fetch_is_not_retried_inside_the_window(origin, repos_root, clock, spy, caplog):
    url, bare, _ = origin
    _first_read(url)
    bare.rename(bare.with_name("moved.git"))

    clock.now += 60
    repo_read_access.read_root_kwargs(url)
    clock.now += 10
    repo_read_access.read_root_kwargs(url)

    assert len(spy.calls) == 1


def test_dirty_tree_is_not_moved(origin, repos_root, clock):
    url, _, work = origin
    root = _first_read(url)
    old = _rev("HEAD", Path(root))
    _commit_and_push(work, "fresh.txt", "fresh")
    Path(root, "README.md").write_text("local edit\n", encoding="utf-8")

    clock.now += 60
    repo_read_access.read_root_kwargs(url)

    assert _rev("HEAD", Path(root)) == old
    assert Path(root, "README.md").read_text(encoding="utf-8") == "local edit\n"
    assert not Path(root, "fresh.txt").exists()


def test_clone_on_a_story_branch_is_not_moved(origin, repos_root, clock):
    url, _, work = origin
    root = _first_read(url)
    clone = Path(root)
    _git("checkout", "-q", "-b", "agent/tlc-03", cwd=clone)
    old = _rev("HEAD", clone)
    _commit_and_push(work, "fresh.txt", "fresh")

    clock.now += 60
    repo_read_access.read_root_kwargs(url)

    assert _rev("HEAD", clone) == old
    assert _git("symbolic-ref", "--short", "HEAD", cwd=clone).stdout.strip() == "agent/tlc-03"
    assert not (clone / "fresh.txt").exists()


def test_detached_head_is_not_moved(origin, repos_root, clock):
    url, _, work = origin
    root = _first_read(url)
    clone = Path(root)
    old = _rev("HEAD", clone)
    _git("checkout", "-q", "--detach", old, cwd=clone)
    _commit_and_push(work, "fresh.txt", "fresh")

    clock.now += 60
    repo_read_access.read_root_kwargs(url)

    assert _rev("HEAD", clone) == old


def test_diverged_default_branch_is_not_fast_forwarded(origin, repos_root, clock):
    url, _, work = origin
    root = _first_read(url)
    clone = Path(root)
    (clone / "local.txt").write_text("local\n", encoding="utf-8")
    _git("add", "local.txt", cwd=clone)
    _git("commit", "-m", "local", cwd=clone)
    local = _rev("HEAD", clone)
    _commit_and_push(work, "fresh.txt", "fresh")

    clock.now += 60
    repo_read_access.read_root_kwargs(url)

    assert _rev("HEAD", clone) == local
    assert not (clone / "fresh.txt").exists()


def test_a_reader_that_cannot_take_the_lock_neither_waits_nor_fetches(
    origin, repos_root, clock, spy
):
    url, _, _ = origin
    root = _first_read(url)
    clock.now += 60
    lock = repo_read_access._lock_for(os.path.normpath(os.path.realpath(root)))
    assert lock.acquire(blocking=False)
    results: list[dict] = []

    def read() -> None:
        results.append(repo_read_access.read_root_kwargs(url))

    try:
        reader = threading.Thread(target=read)
        reader.start()
        reader.join(5)
        assert not reader.is_alive()
    finally:
        lock.release()

    assert results == [{"root": os.path.normpath(os.path.realpath(root))}]
    assert spy.calls == []
