"""Integration: test_scope stamping through the real dispatch_story.

Fixtures are copied from tests/unit/test_dispatch_acceptance_overwrite.py
(repo convention: no shared conftest). Only the agent subprocess and the
ticket provider are stubbed; the dispatch path itself runs for real.
"""
import json
import subprocess

import pytest

from app import backend
from pipeline import event_wiring
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt


class _CiForgeStub:
    def has_ci_config(self, repo_root):
        return True


@pytest.fixture(autouse=True)
def _scoped_survives_dispatch(monkeypatch):
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(
        "pipeline.test_scope_dispatch.get_forge", lambda root: _CiForgeStub()
    )


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


class _FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp_path, branch="main"):
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
    return repo, branch


@pytest.fixture
def bus(monkeypatch):
    fake = _FakeBus()
    monkeypatch.setattr(event_wiring, "get_bus", lambda: fake)
    return fake


@pytest.fixture
def claude_cmds(monkeypatch):
    """Capture the argv of every `claude` agent launch; stub the process."""
    cmds = []
    real_popen = backend.subprocess.Popen

    def _popen(cmd, **kw):
        if cmd and cmd[0] == "claude":
            cmds.append(list(cmd))
            return _FakeProc(4242)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _popen)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    return cmds


def _dispatch(plan_dir, repo, branch, monkeypatch, plan_extra=None, plan_name="stamp"):
    manifest = {
        "repo_root": str(repo),
        "epics": {},
        "stories": {"S1": {
            "summary": "Do thing", "agent_instructions": "Build it.",
            "status": "todo", "dependencies": [],
        }},
    }
    manifest.update(plan_extra or {})
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    return p.dispatch_story(plan_name, "S1")


def test_dispatch_persists_default_full_resolution_on_story(
    plan_dir, worktree_root, agents_dir, claude_cmds, bus, monkeypatch, tmp_path,
):
    monkeypatch.delenv("PIPELINE_TEST_SCOPE", raising=False)
    repo, branch = _make_repo(tmp_path)
    _dispatch(plan_dir, repo, branch, monkeypatch)
    story = p._store.get_manifest("stamp")["stories"]["S1"]
    assert story["test_scope_resolution"]["test_scope"] == "full"


def test_dispatch_persists_scoped_when_plan_requests_scoped(
    plan_dir, worktree_root, agents_dir, claude_cmds, bus, monkeypatch, tmp_path,
):
    monkeypatch.delenv("PIPELINE_TEST_SCOPE", raising=False)
    repo, branch = _make_repo(tmp_path)
    _dispatch(plan_dir, repo, branch, monkeypatch, plan_extra={"test_scope": "scoped"})
    story = p._store.get_manifest("stamp")["stories"]["S1"]
    assert story["test_scope_resolution"]["test_scope"] == "scoped"


def test_agent_dispatched_event_carries_test_scope_fields(
    plan_dir, worktree_root, agents_dir, claude_cmds, bus, monkeypatch, tmp_path,
):
    monkeypatch.delenv("PIPELINE_TEST_SCOPE", raising=False)
    repo, branch = _make_repo(tmp_path)
    _dispatch(plan_dir, repo, branch, monkeypatch)
    dispatched = [e for e in bus.events if e.get("type") == "agent_dispatched"]
    assert len(dispatched) == 1
    payload = dispatched[0]["payload"]
    assert {
        "test_scope": payload["test_scope"],
        "test_scope_source": payload["test_scope_source"],
        "test_scope_lock_rejected": payload["test_scope_lock_rejected"],
        "rework": payload["rework"],
    } == {"test_scope": "full", "test_scope_source": "default",
          "test_scope_lock_rejected": False, "rework": "none"}


def test_full_mode_dispatch_command_is_identical_with_and_without_env_full(
    plan_dir, agents_dir, claude_cmds, bus, monkeypatch, tmp_path,
):
    repo, branch = _make_repo(tmp_path)
    monkeypatch.delenv("PIPELINE_TEST_SCOPE", raising=False)
    monkeypatch.setattr(p, "WORKTREE_ROOT", tmp_path / "wt-unset")
    (tmp_path / "wt-unset").mkdir()
    _dispatch(plan_dir, repo, branch, monkeypatch, plan_name="stamp-unset")
    unset_cmd = claude_cmds[-1]

    monkeypatch.setenv("PIPELINE_TEST_SCOPE", "full")
    monkeypatch.setattr(p, "WORKTREE_ROOT", tmp_path / "wt-full")
    (tmp_path / "wt-full").mkdir()
    _dispatch(plan_dir, repo, branch, monkeypatch, plan_name="stamp-full")
    full_cmd = claude_cmds[-1]

    assert full_cmd == unset_cmd
