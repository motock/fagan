"""Regression: the acceptance-fixture skip must be gated on a STATUS-based
resume, not on worktree existence.

`_dispatch_story_impl` derives `resuming` from
`status in ("interrupted", "changes_requested") or worktree_path.exists()`.
The materialization loop used to skip writing a fixture whenever
`resuming and target.exists()`. That skip exists to protect a genuinely
RESUMED run's mid-run fixture evolution - but "worktree exists" also covers a
story re-dispatched while still `todo` after an earlier attempt died before
changing status. Worktrees are removed only on escalation/merge, so the
leftover directory made the next dispatch look like a resume; that dispatch
then kept the base branch's stale fixture (or a half-written one) instead of
the plan's authoritative source, and the oracle could go green with zero
implementation (Mode 45).

Fix: introduce `resumed_story` (status-based only) and gate the skip on it.
`resuming` keeps its seven other uses untouched.

Fixtures are copied locally per this repo's convention - no shared conftest.
"""
import json
import logging
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


# ---------- Fixtures (copied from test_dispatch_acceptance_overwrite.py) ----------
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


def _make_origin(tmp_path, rel_path, old_content, branch="main"):
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
              repo_root, branch, story):
    _write_manifest(plan_dir, plan_name, story_key, story, repo_root)
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
def test_todo_story_with_existing_worktree_rematerializes_fixture(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """A story re-dispatched while still `todo` has a leftover worktree from a
    dead earlier attempt. That is NOT a resume: the plan's acceptance source is
    authoritative and must overwrite the stale fixture."""
    rel = "test_acceptance_rematerialize.py"
    stale_base = "# BASE: stale fixture from a prior story\nBASE = 1\n"
    plan_source = "# PLAN: authoritative source for this story\nPLAN = 2\n"
    _origin, repo, branch = _make_origin(tmp_path, rel, stale_base)

    # Leftover worktree from an earlier attempt that died before changing status.
    worktree_path = worktree_root / "S1"
    (worktree_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (worktree_path / rel).write_text("# STALE-MARKER: half-written leftover\n")

    story = {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "status": "todo", "dependencies": [],
        "acceptance": [{"path": rel, "source": plan_source}],
    }
    _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch, "remat", "S1",
              repo, branch, story)

    written = (worktree_path / rel).read_text()
    assert "PLAN: authoritative source" in written, (
        "a todo story with a leftover worktree did not rematerialize the "
        "acceptance fixture; the stale base-branch file would be graded "
        "instead -> false green with zero implementation"
    )
    assert "STALE-MARKER" not in written


# ---------- Negative control ----------
def test_changes_requested_story_with_existing_worktree_preserves_fixture(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """A genuine status-based resume must NOT overwrite a fixture the oracle
    may have evolved mid-run."""
    rel = "test_acceptance_rematerialize_resume.py"
    _origin, repo, branch = _make_origin(tmp_path, rel, "# BASE: pre-dispatch source\n")
    worktree_path = worktree_root / "S1"
    (worktree_path / rel).parent.mkdir(parents=True, exist_ok=True)
    evolved = "# EVOLVED mid-run by the oracle; must survive a resume\nEVOLVED = 9\n"
    (worktree_path / rel).write_text(evolved)

    story = {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "status": "changes_requested", "dependencies": [],
        "acceptance": [{"path": rel, "source": "# PLAN source; must NOT overwrite\nPLAN = 0\n"}],
    }
    _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch, "remat_resume", "S1",
              repo, branch, story)

    written = (worktree_path / rel).read_text()
    assert written == evolved, (
        "a changes_requested (status-based resume) dispatch overwrote an "
        "existing acceptance fixture, discarding mid-run test evolution"
    )
    assert "PLAN source" not in written


# ---------- Boundary ----------
def test_todo_story_with_existing_worktree_and_no_fixture_writes_it(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """Worktree exists but the fixture file does not: it must be created from
    the plan source."""
    rel = "test_acceptance_rematerialize_missing.py"
    plan_source = "# PLAN: created because the fixture was absent\nPLAN = 3\n"
    _origin, repo, branch = _make_origin(tmp_path, rel, "# BASE: stale\n")

    worktree_path = worktree_root / "S1"
    worktree_path.mkdir(parents=True)
    assert not (worktree_path / rel).exists()

    story = {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "status": "todo", "dependencies": [],
        "acceptance": [{"path": rel, "source": plan_source}],
    }
    _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch, "remat_missing", "S1",
              repo, branch, story)

    written = (worktree_path / rel).read_text()
    assert "PLAN: created because the fixture was absent" in written


# ---------- Observability ----------
def test_materialization_decision_is_logged(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path, caplog,
):
    """The preserve/materialize decision must be observable in the logs."""
    rel = "test_acceptance_rematerialize_log.py"
    _origin, repo, branch = _make_origin(tmp_path, rel, "# BASE: pre-dispatch source\n")
    worktree_path = worktree_root / "S1"
    (worktree_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (worktree_path / rel).write_text("# EVOLVED mid-run\n")

    story = {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "status": "interrupted", "dependencies": [],
        "acceptance": [{"path": rel, "source": "# PLAN source\nPLAN = 0\n"}],
    }
    caplog.set_level(logging.INFO, logger="pipeline")
    _dispatch(plan_dir, worktree_root, agents_dir, monkeypatch, "remat_log", "S1",
              repo, branch, story)

    assert f"acceptance fixture {rel} preserved" in caplog.text, (
        "the materialization decision was not logged; a preserved fixture is "
        "indistinguishable from a silently skipped one"
    )
