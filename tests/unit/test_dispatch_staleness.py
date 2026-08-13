"""Tests for the resumed-worktree staleness check in `_dispatch_story_impl`
(issue 4258af98): when a story is RESUMED (status `interrupted` or
`changes_requested`, or an existing worktree), dispatch must fetch
`origin/<default>` and compare it against the worktree's own branch. If
origin has moved past the worktree's base, `_notify_user` is called with a
staleness message naming the story. If origin has NOT moved, no staleness
notify fires. If the git check itself errors (e.g. fetch failure), dispatch
fails open - logs a warning and continues, never raising out of
`_dispatch_story_impl`. A FRESH (non-resuming) dispatch never runs this
check at all.

Fixtures/helpers are copied from test_dispatch_worktree_from_origin.py and
test_dispatch_git_fetch_lock.py per this repo's convention - there is no
shared conftest.py for these.
"""
import json
import subprocess

import pytest

from app import (
    backend,
    pipeline_mcp_server,  # noqa: F401  backward compat
)
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
        self.args = []
        self.returncode = 0

    def communicate(self, input=None, timeout=None):
        return ("", "")

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


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
    it locally until a fetch pulls it in. Returns the new origin tip sha.
    Uses a unique clone dir per call so it can be invoked repeatedly."""
    other = tmp_path / f"other-clone-{name}"
    _run(["git", "clone", "-q", str(origin), str(other)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], other)
    _run(["git", "config", "user.name", "t"], other)
    (other / name).write_text("more\n")
    _run(["git", "add", "-A"], other)
    _run(["git", "commit", "-qm", "more"], other)
    _run(["git", "push", "-q", "origin", branch], other)
    return _run(["git", "rev-parse", branch], other).stdout.strip()


def _make_resumed_worktree(tmp_path, worktree_root, repo, branch, story_key="S1"):
    """Create a REAL git worktree at worktree_root/<story_key> on an
    `agent/<story_key>` branch off repo's current HEAD, so the staleness
    check's `git rev-list --count` runs against real refs. Returns the
    worktree path."""
    worktree_path = worktree_root / story_key
    agent_branch = f"agent/{story_key.lower()}"
    _run(
        ["git", "worktree", "add", "-b", agent_branch, str(worktree_path), "HEAD"],
        repo,
    )
    return worktree_path


def _dispatch_resumed(
    plan_dir, worktree_root, agents_dir, monkeypatch, plan_name, story_key,
    repo, branch, status="interrupted", run_calls=None, notify_calls=None,
):
    """Dispatch a RESUMED story against a REAL repo (with a real worktree).
    Records subprocess.run calls into run_calls and _notify_user calls into
    notify_calls while delegating to the real subprocess.run for git."""
    worktree_path = worktree_root / story_key
    _write_manifest(plan_dir, plan_name, {
        story_key: {"summary": "Do thing", "agent_instructions": "Build it.",
                    "status": status, "dependencies": [],
                    "worktree": str(worktree_path)},
    }, repo_root=repo)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))

    real_run = p.subprocess.run

    def _recording_run(cmd, **kw):
        if run_calls is not None:
            run_calls.append(list(cmd))
        return real_run(cmd, **kw)

    monkeypatch.setattr(p.subprocess, "run", _recording_run)

    real_popen = backend.subprocess.Popen

    def _discriminating_popen(cmd, **kw):
        if cmd and cmd[0] == "claude":
            return _FakeProc(4242)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)

    if notify_calls is not None:
        monkeypatch.setattr(p, "_notify_user",
                            lambda plan, msg: notify_calls.append((plan, msg)))

    return p.dispatch_story(plan_name, story_key)


def _notifications(plan_dir, plan_name):
    path = plan_dir / f"{plan_name}.notifications.log"
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line.strip()]


# ---------- Test (1): resumed stale branch triggers notify ----------
def test_resumed_stale_branch_triggers_staleness_notify(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    # Build a resumed worktree off repo's current HEAD (== origin tip now).
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")

    # Push 3 new commits to origin from a third clone so origin/main moves
    # past the worktree's base.
    for i in range(3):
        _push_extra_commit_directly_to_origin(
            tmp_path, origin, branch, name=f"extra{i}")

    notify_calls = []
    run_calls = []
    result = _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "stale1", "S1", repo, branch,
        run_calls=run_calls, notify_calls=notify_calls)

    assert result["ok"] is True
    # A fetch of origin's default branch must have run on the resume path.
    assert any(c[:3] == ["git", "fetch", "origin"] for c in run_calls), (
        "resumed dispatch must fetch origin/<default> to check staleness")
    # _notify_user was called with a staleness message naming the story.
    staleness = [m for (_p, m) in notify_calls
                 if "S1" in m and ("predates" in m or "stale" in m)]
    assert staleness, (
        f"expected a staleness notify naming S1, got: {notify_calls}")


# ---------- Test (2): resumed current branch does NOT notify ----------
def test_resumed_current_branch_does_not_notify_staleness(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    _origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    # origin/main is at the same commit the worktree branched from: no move.
    notify_calls = []
    run_calls = []
    result = _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "current1", "S1", repo, branch,
        run_calls=run_calls, notify_calls=notify_calls)

    assert result["ok"] is True
    staleness = [m for (_p, m) in notify_calls
                 if "predates" in m or "stale" in m]
    assert not staleness, (
        f"origin did not move; no staleness notify expected, got: {notify_calls}")


# ---------- Test (3): git fetch failure fails open ----------
def test_resumed_fetch_failure_fails_open(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    _origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    # Point origin remote at a nonexistent path so the fetch fails.
    _run(["git", "remote", "set-url", "origin",
          "/nonexistent/path/does/not/exist"], repo)

    notify_calls = []
    run_calls = []
    # Must NOT raise - fail open.
    result = _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "fetchfail1", "S1", repo, branch,
        run_calls=run_calls, notify_calls=notify_calls)

    assert result["ok"] is True
    staleness = [m for (_p, m) in notify_calls
                 if "predates" in m or "stale" in m]
    assert not staleness, (
        "fetch failed -> fail open, no staleness notify expected")


# ---------- Test (4): fresh dispatch never runs the staleness check ----------
def test_fresh_dispatch_never_runs_staleness_check(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    # Move origin ahead so a stale-check WOULD fire if it ran - but it must
    # not run on a fresh dispatch.
    _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="x")

    _write_manifest(plan_dir, "fresh1", {
        "S1": {"summary": "Do thing", "agent_instructions": "Build it.",
               "status": "todo", "dependencies": []},
    }, repo_root=repo)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))

    run_calls = []
    real_run = p.subprocess.run

    def _recording_run(cmd, **kw):
        run_calls.append(list(cmd))
        return real_run(cmd, **kw)

    monkeypatch.setattr(p.subprocess, "run", _recording_run)
    monkeypatch.setattr(backend.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(1))

    result = p.dispatch_story("fresh1", "S1")
    assert result["ok"] is True

    # Fresh dispatch fetches origin/<default> exactly once (for worktree
    # creation) but must NEVER run a `git rev-list --count` staleness probe.
    assert not any(c[:3] == ["git", "rev-list", "--count"] for c in run_calls), (
        f"fresh dispatch must not run staleness rev-list probe: {run_calls}")
    notes = _notifications(plan_dir, "fresh1")
    staleness = [n for n in notes if "predates" in n or "stale" in n]
    assert not staleness, (
        f"fresh dispatch must not emit staleness notify: {notes}")