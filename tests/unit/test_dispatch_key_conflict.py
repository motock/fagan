"""dispatch_story must refuse a story whose key another plan still holds:
the branch and worktree derive from the key alone, so dispatching would
resume (and push to) the other plan's worktree."""
import json

import pytest

from app import backend
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline import ticketing as pt

ERROR = "story key collides with another plan"


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


@pytest.fixture
def env(plan_dir, worktree_root, monkeypatch):
    run_calls, popen_calls, notes = [], [], []
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: run_calls.append(cmd))
    monkeypatch.setattr(
        backend.subprocess, "Popen",
        lambda cmd, **kw: popen_calls.append(cmd) or type("P", (), {"pid": 1})(),
    )
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kw: notes.append((plan, msg, kw)))
    return run_calls, popen_calls, notes


def _write(plan_dir, plan, stories):
    (plan_dir / f"{plan}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}))


def _story(status="todo"):
    return {"summary": "x", "agent_instructions": "Build.", "status": status,
            "dependencies": []}


def _status(plan_dir, plan, key):
    return json.loads(
        (plan_dir / f"{plan}.manifest.json").read_text())["stories"][key]["status"]


def test_should_refuse_key_held_by_in_progress_story_in_another_plan(env, plan_dir):
    run_calls, popen_calls, notes = env
    _write(plan_dir, "mine", {"TOPO-X": _story("todo")})
    _write(plan_dir, "other", {"TOPO-X": _story("in_progress")})

    result = p.dispatch_story("mine", "TOPO-X")

    assert result == {"ok": False, "error": ERROR, "plan": "other",
                      "other_key": "TOPO-X"}
    assert not any("worktree" in c for c in run_calls)
    assert popen_calls == []
    assert _status(plan_dir, "mine", "TOPO-X") == "todo"
    assert [n[2]["event"] for n in notes] == ["story_key_conflict"]


def test_should_refuse_key_differing_only_in_case(env, plan_dir):
    _write(plan_dir, "mine", {"TOPO-X": _story("todo")})
    _write(plan_dir, "other", {"topo-x": _story("in_progress")})

    result = p.dispatch_story("mine", "TOPO-X")

    assert result["ok"] is False
    assert result["other_key"] == "topo-x"


def test_should_dispatch_when_other_plan_done_and_worktree_gone(env, plan_dir):
    _write(plan_dir, "mine", {"TOPO-X": _story("todo")})
    _write(plan_dir, "other", {"TOPO-X": _story("done")})

    assert p.dispatch_story("mine", "TOPO-X")["ok"] is True


def test_should_refuse_when_other_plan_done_but_worktree_exists(
    env, plan_dir, worktree_root,
):
    (worktree_root / "TOPO-X").mkdir()
    _write(plan_dir, "mine", {"TOPO-X": _story("todo")})
    _write(plan_dir, "other", {"TOPO-X": _story("done")})

    assert p.dispatch_story("mine", "TOPO-X")["error"] == ERROR


def test_should_refuse_resumed_dispatch_with_conflicting_key(env, plan_dir):
    _, popen_calls, _ = env
    _write(plan_dir, "mine", {"TOPO-X": _story("interrupted")})
    _write(plan_dir, "other", {"TOPO-X": _story("in_progress")})

    result = p.dispatch_story("mine", "TOPO-X")

    assert result["error"] == ERROR
    assert popen_calls == []
    assert _status(plan_dir, "mine", "TOPO-X") == "interrupted"


def test_should_dispatch_when_no_other_plans_exist(env, plan_dir):
    _write(plan_dir, "mine", {"TOPO-X": _story("todo")})

    assert p.dispatch_story("mine", "TOPO-X")["ok"] is True


def test_should_not_leak_filesystem_paths_in_error_or_notification(
    env, plan_dir, tmp_path,
):
    _, _, notes = env
    _write(plan_dir, "mine", {"TOPO-X": _story("todo")})
    _write(plan_dir, "other", {"TOPO-X": _story("in_progress")})

    result = p.dispatch_story("mine", "TOPO-X")

    leaked = [s for s in (json.dumps(result), notes[0][1]) if str(tmp_path) in s]
    assert leaked == []
