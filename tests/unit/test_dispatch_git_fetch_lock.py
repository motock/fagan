"""Tests for defense-in-depth around dispatch_story's `git fetch` (the
remaining .git-only write left after Mode 30 core removed the working-tree
mutating `git merge`/`git pull` calls - see
test_dispatch_worktree_from_origin.py). This story adds a non-blocking,
advisory flock around that fetch:

  - Lock free -> fetch proceeds, lock released after (a second acquirer
    succeeds immediately).
  - Lock already held by another process -> fetch is SKIPPED (best-effort,
    never raises/blocks) and the worktree is still created from whatever
    origin ref is already local.
  - `git worktree add` itself is never guarded by this lock (it only creates
    a new worktree directory; it doesn't touch the shared .git index the way
    a rebase of the main branch would).

The lock is advisory only (a raw `git` invocation bypasses it) - that's
accepted as good enough for the common scheduler-vs-human race.

Fixtures/helpers are copied from test_dispatch_worktree_from_origin.py per
this repo's convention - there is no shared conftest.py for these.
"""
import fcntl
import json
import os
import signal
import subprocess
import time
from contextlib import contextmanager

import pytest

from app import backend
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt


# ---------- Fixtures (copied from test_dispatch_worktree_from_origin.py) ----------
@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def _write_manifest(plan_dir, plan_name, stories, repo_root=None):
    manifest = {"epics": {}, "stories": stories}
    if repo_root is not None:
        manifest["repo_root"] = str(repo_root)
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _run(args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def _make_origin_and_repo(tmp_path, branch="main"):
    """Bare `origin` + a real local clone `repo`, both on `branch`, one
    commit deep. Returns (origin, repo, branch)."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", branch, str(origin)], tmp_path)

    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", branch, str(repo)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "README.md").write_text("seed\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "-q", "-u", "origin", branch], repo)
    return origin, repo, branch


def _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra"):
    """Push a new commit to `origin` from a THIRD clone, so `repo` never sees
    it locally until dispatch's fetch pulls it in."""
    other = tmp_path / "other-clone"
    _run(["git", "clone", "-q", str(origin), str(other)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], other)
    _run(["git", "config", "user.name", "t"], other)
    (other / name).write_text("more\n")
    _run(["git", "add", "-A"], other)
    _run(["git", "commit", "-qm", "more"], other)
    _run(["git", "push", "-q", "origin", branch], other)
    return _run(["git", "rev-parse", branch], other).stdout.strip()


def _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch, plan_name, story_key,
              repo_root, branch, run_calls=None):
    """Dispatch against a REAL repo_root. Records every `p.subprocess.run`
    call into `run_calls` (if given) while still delegating to the real
    subprocess.run underneath, so `git worktree add` still actually executes
    and we can inspect exactly which git commands dispatch issued."""
    _write_manifest(plan_dir, plan_name, {
        story_key: {"summary": "Do thing", "agent_instructions": "Build it.",
                    "status": "todo", "dependencies": []},
    }, repo_root=repo_root)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))

    real_run = p.subprocess.run

    def _recording_run(cmd, **kw):
        if run_calls is not None:
            run_calls.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(p.subprocess, "run", _recording_run)

    # backend.subprocess IS the real, global subprocess module (a singleton
    # import, not a copy) - faking its Popen unconditionally would also
    # break the real git commands _recording_run delegates to above. Only
    # fake the actual `claude` CLI invocation.
    real_popen = backend.subprocess.Popen

    def _discriminating_popen(cmd, **kw):
        if cmd and cmd[0] == "claude":
            return _FakeProc(4242)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)
    return p.dispatch_story(plan_name, story_key)

@contextmanager
def _bounded(seconds):
    """Fail the test instead of hanging forever if the lock helper blocks -
    the whole point of LOCK_EX | LOCK_NB is that it must never wait."""
    def _on_alarm(signum, frame):
        raise AssertionError(
            f"operation did not return within {seconds}s - lock helper "
            "appears to be blocking instead of using LOCK_NB"
        )
    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@contextmanager
def _external_holder(lock_path):
    """Simulate another process already holding the advisory lock, by
    flock-ing the same path from a separate file descriptor in this same
    test process (flock contention is per open-file-description, so a
    second fd on the same path behaves exactly like a second process)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------- Unit tests: _try_acquire_git_lock itself ----------
def test_lock_file_path_is_dot_git_pipeline_git_lock(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)

    with _bounded(5), p._try_acquire_git_lock(repo_root) as acquired:
        assert acquired is True
        assert (repo_root / ".git" / ".pipeline-git-lock").exists()
        with p._try_acquire_git_lock(repo_root) as acquired:
            assert acquired is True
            assert (repo_root / ".git" / ".pipeline-git-lock").exists()


def test_lock_free_acquires_true(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)

    with _bounded(5), p._try_acquire_git_lock(repo_root) as acquired:
        assert acquired is True
        with p._try_acquire_git_lock(repo_root) as acquired:
            assert acquired is True


def test_lock_released_on_exit_second_acquirer_succeeds_immediately(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)

    with _bounded(5), p._try_acquire_git_lock(repo_root) as first:
        assert first is True
        with p._try_acquire_git_lock(repo_root) as first:
            assert first is True

    with _bounded(5), p._try_acquire_git_lock(repo_root) as second:
        assert second is True
        with p._try_acquire_git_lock(repo_root) as second:
            assert second is True


def test_lock_held_by_another_process_yields_false_without_blocking(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    lock_path = repo_root / ".git" / ".pipeline-git-lock"

    with _external_holder(lock_path):
        start = time.monotonic()
        with _bounded(5), p._try_acquire_git_lock(repo_root) as acquired:
            assert acquired is False
            with p._try_acquire_git_lock(repo_root) as acquired:
                assert acquired is False
        elapsed = time.monotonic() - start
    assert elapsed < 2, "lock helper blocked instead of failing fast (LOCK_NB)"


def test_lock_released_after_holder_releases_next_acquirer_succeeds(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    lock_path = repo_root / ".git" / ".pipeline-git-lock"

    with _external_holder(lock_path), _bounded(5):
        with p._try_acquire_git_lock(repo_root) as acquired:
            assert acquired is False
        with _bounded(5), p._try_acquire_git_lock(repo_root) as acquired:
            assert acquired is False
            with p._try_acquire_git_lock(repo_root) as acquired:
                assert acquired is False

    with _bounded(5), p._try_acquire_git_lock(repo_root) as acquired:
        assert acquired is True
        with p._try_acquire_git_lock(repo_root) as acquired:
            assert acquired is True


# ---------- Negative / boundary: lock helper must fail OPEN, never raise ----------
def test_missing_git_dir_fails_open_without_raising(tmp_path):
    """Not a git repo at all (no .git). The lock helper has nowhere to put
    its lock file - it must fail open (let the caller proceed with the
    fetch, which will fail or succeed on its own merits) rather than raise."""
    repo_root = tmp_path / "not_a_repo"
    repo_root.mkdir()

    with _bounded(5), p._try_acquire_git_lock(repo_root) as acquired:
        assert acquired is True
        with p._try_acquire_git_lock(repo_root) as acquired:
            assert acquired is True


def test_readonly_git_dir_fails_open_without_raising(tmp_path):
    """.git exists but is read-only, so the lock file can't be created
    there. Must fail open (proceed unprotected), never raise."""
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    git_dir.mkdir(parents=True)
    os.chmod(git_dir, 0o555)
    try:
        with _bounded(5), p._try_acquire_git_lock(repo_root) as acquired:
            assert acquired is True
            with p._try_acquire_git_lock(repo_root) as acquired:
                assert acquired is True
    finally:
        os.chmod(git_dir, 0o755)


def test_nonexistent_repo_root_fails_open_without_raising(tmp_path):
    """repo_root itself doesn't exist on disk at all."""
    repo_root = tmp_path / "does" / "not" / "exist"

    with _bounded(5), p._try_acquire_git_lock(repo_root) as acquired:
        assert acquired is True


# ---------- Integration: dispatch_story's fetch respects the lock ----------
def test_dispatch_fetch_proceeds_and_releases_lock_when_free(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    origin_tip = _push_extra_commit_directly_to_origin(tmp_path, origin, branch)

    result = _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch,
                        "lockfree", "S1", repo, branch)
    assert result["ok"] is True

    # The fetch actually ran (worktree picked up the commit pushed straight
    # to origin, which `repo` never saw except through dispatch's fetch).
    worktree_head = _run(["git", "rev-parse", "HEAD"], worktree_root / "S1").stdout.strip()
    assert worktree_head == origin_tip

    # Lock file was created under .git and is released after dispatch - a
    # fresh acquirer must succeed immediately, non-blocking.
    lock_path = repo / ".git" / ".pipeline-git-lock"
    assert lock_path.exists()
    with _bounded(5):
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def test_dispatch_skips_fetch_when_lock_held_but_still_creates_worktree(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    _origin, repo, branch = _make_origin_and_repo(tmp_path)
    # repo already has an up-to-date origin/<branch> tracking ref from the
    # push above - that's the "already-local origin ref" the worktree must
    # be built from when the fetch is skipped.
    local_origin_ref = _run(["git", "rev-parse", f"origin/{branch}"], repo).stdout.strip()

    lock_path = repo / ".git" / ".pipeline-git-lock"
    run_calls = []
    with _external_holder(lock_path):
        result = _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch,
                            "lockheld", "S1", repo, branch, run_calls=run_calls)

    assert result["ok"] is True
    assert not any(c[:2] == ["git", "fetch"] for c in run_calls), (
        "fetch must be SKIPPED entirely (not attempted and failed) when the "
        "advisory lock is held by another process"
    )

    worktree_path = worktree_root / "S1"
    assert worktree_path.exists()
    worktree_head = _run(["git", "rev-parse", "HEAD"], worktree_path).stdout.strip()
    assert worktree_head == local_origin_ref


def test_dispatch_does_not_raise_or_hang_when_lock_held(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """Boundary: dispatch must be best-effort around the lock - contention
    is an expected, non-exceptional outcome, never surfaced as an error."""
    origin, repo, branch = _make_origin_and_repo(tmp_path)  # noqa
    lock_path = repo / ".git" / ".pipeline-git-lock"

    with _external_holder(lock_path):
        start = time.monotonic()
        with _bounded(10):
            result = _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch,
                                "lockheldnoraise", "S1", repo, branch)
        elapsed = time.monotonic() - start
    assert result["ok"] is True
    assert elapsed < 5, "dispatch blocked waiting on the held lock instead of skipping"


def test_worktree_add_itself_is_never_guarded_by_the_lock(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """Only `git fetch` is lock-guarded. `git worktree add` must still run
    (and succeed) even while the lock is held, since it only creates a new
    worktree directory rather than touching shared .git refs the way a
    fetch does."""
    _origin, repo, branch = _make_origin_and_repo(tmp_path)
    lock_path = repo / ".git" / ".pipeline-git-lock"
    run_calls = []

    with _external_holder(lock_path):
        result = _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch,
                            "worktreenolock", "S1", repo, branch, run_calls=run_calls)

    assert result["ok"] is True
    worktree_path = worktree_root / "S1"
    assert ["git", "worktree", "add", "-b", "agent/s1", str(worktree_path),
            f"origin/{branch}"] in run_calls
    assert worktree_path.exists()
