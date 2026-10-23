"""Fail-closed guard: a FRESH dispatch is refused when origin already holds
the story's ``agent/<key>`` branch (docs/plans/LOCAL_FLEET_BRANCH_SAFETY_PLAN.md
section 2). Uses a real local bare repo as ``origin``."""
import json
import subprocess

import pytest

from app import backend
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline.branch_guard import BranchGuardError, remote_branch_exists


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class _FakeProc:
    pid = 4242
    args: list = []  # noqa: RUF012
    returncode = 0

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

    def __exit__(self, *exc):
        return False


@pytest.fixture
def origin_and_repo(tmp_path):
    origin = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], tmp_path)
    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", "main", str(repo)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "README.md").write_text("seed\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "-q", "-u", "origin", "main"], repo)
    return origin, repo


def _push_branch(repo, name):
    _run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{name}"], repo)


@pytest.fixture
def env(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    wt = tmp_path / "worktrees"
    wt.mkdir()
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    from pipeline import persona as pper
    monkeypatch.setattr(p, "AGENTS_DIR", agents)
    monkeypatch.setattr(pper, "AGENTS_DIR", agents)
    monkeypatch.setattr(p, "PLAN_DIR", plans)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    monkeypatch.setattr(pcon, "PLAN_DIR", plans)
    monkeypatch.setattr(p, "WORKTREE_ROOT", wt)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(
        pt, "plane_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    real_popen = backend.subprocess.Popen
    monkeypatch.setattr(
        backend.subprocess,
        "Popen",
        lambda cmd, **kw: _FakeProc() if cmd and cmd[0] == "claude"
        else real_popen(cmd, **kw),
    )
    return plans, wt


def _manifest(plans, repo, status="todo", wt=None):
    story = {"summary": "Do thing", "agent_instructions": "Build it.",
             "status": status, "dependencies": []}
    if wt is not None:
        story["worktree"] = str(wt)
    (plans / "g.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {"S1": story}, "repo_root": str(repo)})
    )


def _story(plans):
    return json.loads((plans / "g.manifest.json").read_text())["stories"]["S1"]


# ---------- remote_branch_exists ----------
def test_should_report_existing_remote_branch(origin_and_repo):
    _origin, repo = origin_and_repo
    _push_branch(repo, "agent/s1")
    assert remote_branch_exists(repo, "agent/s1") is True


def test_should_report_absent_remote_branch(origin_and_repo):
    _origin, repo = origin_and_repo
    assert remote_branch_exists(repo, "agent/s1") is False


def test_should_return_false_without_origin_remote(tmp_path):
    repo = tmp_path / "solo"
    _run(["git", "init", "-q", "-b", "main", str(repo)], tmp_path)
    assert remote_branch_exists(repo, "agent/s1") is False


def test_should_raise_without_leaking_url_when_origin_unreachable(origin_and_repo):
    _origin, repo = origin_and_repo
    secret = "/nonexistent/secret-host-path/repo.git"
    _run(["git", "remote", "set-url", "origin", secret], repo)
    with pytest.raises(BranchGuardError) as exc:
        remote_branch_exists(repo, "agent/s1")
    assert "secret-host-path" not in str(exc.value)


# ---------- dispatch integration ----------
def test_fresh_dispatch_blocked_when_remote_branch_exists(env, origin_and_repo):
    plans, wt = env
    _origin, repo = origin_and_repo
    _push_branch(repo, "agent/s1")
    _manifest(plans, repo)

    result = p.dispatch_story("g", "S1")

    assert result["ok"] is False
    assert "S1" in result["error"] and "agent/s1" in result["error"]
    assert _story(plans)["status"] == "todo"
    assert "agent/s1" in _story(plans)["dispatch_block_reason"]
    assert not (wt / "S1").exists()


def test_blocked_dispatch_emits_structured_event(env, origin_and_repo):
    plans, _wt = env
    _origin, repo = origin_and_repo
    _push_branch(repo, "agent/s1")
    _manifest(plans, repo)

    p.dispatch_story("g", "S1")

    records = [
        json.loads(line)
        for line in (plans / "g.notifications.jsonl").read_text().splitlines()
        if line.strip()
    ]
    blocked = [r for r in records if r.get("event") == "remote_branch_exists"]
    assert blocked and blocked[0]["story_key"] == "S1"
    assert "agent/s1" in blocked[0]["message"]


def test_fresh_dispatch_allowed_when_remote_branch_absent(env, origin_and_repo):
    plans, wt = env
    _origin, repo = origin_and_repo
    _manifest(plans, repo)

    result = p.dispatch_story("g", "S1")

    assert result["ok"] is True
    assert (wt / "S1").exists()


def test_fresh_dispatch_allowed_without_origin_remote(env, origin_and_repo):
    plans, _wt = env
    _origin, repo = origin_and_repo
    _manifest(plans, repo)
    # Without origin the later worktree fetch may still fail; only the guard's
    # own block is under test here.
    _run(["git", "remote", "remove", "origin"], repo)

    p.dispatch_story("g", "S1")

    assert "dispatch_block_reason" not in _story(plans)


def test_fresh_dispatch_blocked_when_origin_unreachable(env, origin_and_repo):
    plans, wt = env
    _origin, repo = origin_and_repo
    _run(["git", "remote", "set-url", "origin", "/nonexistent/secret-host/r.git"], repo)
    _manifest(plans, repo)

    result = p.dispatch_story("g", "S1")

    assert result["ok"] is False
    assert "secret-host" not in json.dumps(result)
    assert "secret-host" not in _story(plans)["dispatch_block_reason"]
    assert _story(plans)["status"] == "todo"
    assert not (wt / "S1").exists()


def test_resume_ignores_existing_remote_branch(env, origin_and_repo, monkeypatch):
    plans, wt = env
    _origin, repo = origin_and_repo
    _push_branch(repo, "agent/s1")
    _run(["git", "worktree", "add", "-b", "agent/s1", str(wt / "S1"), "HEAD"], repo)
    _manifest(plans, repo, status="interrupted", wt=wt / "S1")
    calls = []
    import pipeline.dispatch as d
    monkeypatch.setattr(d, "remote_branch_exists",
                        lambda *a, **k: calls.append(a) or True)

    result = p.dispatch_story("g", "S1")

    assert result["ok"] is True
    assert calls == []
