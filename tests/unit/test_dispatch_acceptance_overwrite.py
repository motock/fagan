"""Regression: a FRESH dispatch must overwrite an acceptance fixture whose path
collides with a file a prior story already merged to the base branch.

The materialization block (`pipeline/server.py`) skips writing a fixture if
the target file already exists, intending to protect a *resumed* run's
mid-run test evolution. But the skip was not gated on `resuming`, so a fresh
dispatch whose fixture path collides with a pre-existing base-branch file
silently graded the stale (already-satisfied) file instead of the plan's
authoritative source. This produced a false green with zero implementation
(observed live on TRANSPORT-ALIAS-READERS: story 1 merged
`test_acceptance_transport_alias.py`; story 2's combined fixture at the same
path was never written, so the oracle graded story 1's 2-test setter fixture
and declared story 2 done with no reader migration).

Fix: only skip the write when `resuming` is true. On a fresh dispatch the
plan's acceptance source is authoritative and must always win.

Fixtures are copied locally per this repo's convention - no shared conftest.
"""
import json
import subprocess

import pytest

from app import (
    backend,
    pipeline_mcp_server,  # noqa: F401  backward compat
)
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt


# ---------- Fixtures (copied from test_pipeline_mcp_server.py) ----------
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


def _run(args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def _make_origin_with_colliding_fixture(tmp_path, rel_path, old_content, branch="main"):
    """Bare `origin` + local clone `repo`, both on `branch`, with `rel_path`
    committed holding `old_content` - simulating a prior story that already
    merged an acceptance fixture at this path."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", branch, str(origin)], tmp_path)

    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", branch, str(repo)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "README.md").write_text("seed\n")
    fixture = repo / rel_path
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(old_content)
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init with colliding fixture"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "-q", "-u", "origin", branch], repo)
    return origin, repo, branch


def _write_manifest(plan_dir, plan_name, story_key, story, repo_root):
    manifest = {"repo_root": str(repo_root), "epics": {}, "stories": {story_key: story}}
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch, plan_name, story_key,
              repo_root, branch):
    _write_manifest(plan_dir, plan_name, story_key, {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "status": "todo", "dependencies": [],
    }, repo_root)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))

    real_popen = backend.subprocess.Popen

    def _discriminating_popen(cmd, **kw):
        if cmd and cmd[0] == "claude":
            return _FakeProc(4242)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)
    return p.dispatch_story(plan_name, story_key)


# ---------- The regression ----------
def test_fresh_dispatch_overwrites_colliding_acceptance_fixture(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """A fresh dispatch must overwrite an acceptance fixture whose path already
    exists on the base branch (a prior story merged it). The plan's source is
    authoritative on a fresh dispatch; the stale file must not be graded."""
    rel = "test_acceptance_collide.py"
    old_content = "# STALE: setter-only fixture from a prior story\nOLD = 1\n"
    new_content = "# FRESH: combined fixture from this story's plan\nNEW = 2\n"
    _origin, repo, branch = _make_origin_with_colliding_fixture(tmp_path, rel, old_content)

    story = {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "status": "todo", "dependencies": [],
        "acceptance": [{"path": rel, "source": new_content}],
    }
    _write_manifest(plan_dir, "collide", "S1", story, repo_root=repo)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    real_popen = backend.subprocess.Popen
    monkeypatch.setattr(backend.subprocess, "Popen",
        lambda cmd, **kw: _FakeProc(4242) if cmd and cmd[0] == "claude" else real_popen(cmd, **kw))

    p.dispatch_story("collide", "S1")

    written = (worktree_root / "S1" / rel).read_text()
    assert "FRESH: combined fixture" in written, (
        "fresh dispatch did not overwrite the colliding acceptance fixture; "
        "the stale base-branch file would be graded instead -> false green"
    )
    assert "STALE: setter-only" not in written


def test_resuming_dispatch_preserves_existing_acceptance_fixture(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """A resumed dispatch must NOT overwrite an acceptance fixture that already
    exists in the worktree - the oracle may have evolved it mid-run and
    overwriting would discard that evolution."""
    rel = "test_acceptance_resume.py"
    _origin, repo, branch = _make_origin_with_colliding_fixture(
        tmp_path, rel, "# BASE: pre-dispatch source\n")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir(parents=True)
    evolved = "# EVOLVED mid-run by the oracle; must survive a resume\nEVOLVED = 9\n"
    (worktree_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (worktree_path / rel).write_text(evolved)

    story = {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "status": "interrupted", "dependencies": [],
        "acceptance": [{"path": rel, "source": "# PLAN source; must NOT overwrite\nPLAN = 0\n"}],
    }
    _write_manifest(plan_dir, "resume", "S1", story, repo_root=repo)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    real_popen = backend.subprocess.Popen
    monkeypatch.setattr(backend.subprocess, "Popen",
        lambda cmd, **kw: _FakeProc(4242) if cmd and cmd[0] == "claude" else real_popen(cmd, **kw))

    p.dispatch_story("resume", "S1")

    written = (worktree_path / rel).read_text()
    assert "EVOLVED mid-run" in written, (
        "resumed dispatch overwrote an existing acceptance fixture, discarding "
        "mid-run test evolution"
    )
    assert "PLAN source" not in written