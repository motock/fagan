"""dispatch_story must refuse a story that is already done or has an open PR.

CYC-3 (2026-09-24) merged at 16:50; an interactive session called
dispatch_story on it at 17:00. The call cut a fresh worktree from a master
that already held the merged work, the oracle gate saw the fixture already
passing, and the story was overwritten to ``blocked_oracle`` - which sent
triage to rule on a finished story twice and park it for a human. The
scheduler tick already dispatches only todo/interrupted/changes_requested;
the direct MCP and dashboard entry point had no status check at all.

These tests drive the real MCP entry point, ``pipeline.server.dispatch_story``.
No git runs: a refused call must return before any worktree work, and the
allowed-status case stops at a stubbed ``_create_fresh_worktree``.
"""

import json

import pytest

from pipeline import concurrency as pcon
from pipeline import dispatch as pdispatch
from pipeline import persistence as ppers
from pipeline import server as p

PLAN = "dsg1"


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
def worktree_calls(monkeypatch):
    calls = []

    def _stub(plan_name, branch, worktree_path):
        calls.append(str(worktree_path))
        return {"ok": False, "error": "stub: stop before git"}

    monkeypatch.setattr(pdispatch, "_create_fresh_worktree", _stub)
    return calls


def _write_manifest(plan_dir, status):
    path = plan_dir / f"{PLAN}.manifest.json"
    story = {"summary": "s", "status": status, "correlation_id": "c0ffee000001"}
    path.write_text(json.dumps({"stories": {"S-1": story}}, indent=2))
    return path


def test_a_done_story_is_refused(plan_dir, worktree_root, worktree_calls):
    _write_manifest(plan_dir, "done")

    result = p.dispatch_story(PLAN, "S-1")

    assert result["ok"] is False
    assert "done" in result["error"]


def test_a_pr_open_story_is_refused(plan_dir, worktree_root, worktree_calls):
    _write_manifest(plan_dir, "pr_open")

    result = p.dispatch_story(PLAN, "S-1")

    assert result["ok"] is False
    assert "pr_open" in result["error"]


def test_the_refusal_names_set_story_status_as_the_way_out(plan_dir, worktree_root, worktree_calls):
    _write_manifest(plan_dir, "done")

    result = p.dispatch_story(PLAN, "S-1")

    assert "set_story_status" in result["error"]


def test_a_refused_dispatch_leaves_the_manifest_byte_identical(plan_dir, worktree_root, worktree_calls):
    path = _write_manifest(plan_dir, "done")
    before = path.read_bytes()

    p.dispatch_story(PLAN, "S-1")

    assert path.read_bytes() == before


def test_a_refused_dispatch_touches_no_worktree(plan_dir, worktree_root, worktree_calls):
    _write_manifest(plan_dir, "done")

    p.dispatch_story(PLAN, "S-1")

    assert worktree_calls == []
    assert list(worktree_root.iterdir()) == []


@pytest.mark.parametrize("status", ["todo", "parked", "failed", "blocked_oracle"])
def test_a_fresh_dispatchable_status_reaches_worktree_setup(plan_dir, worktree_root, worktree_calls, status):
    _write_manifest(plan_dir, status)

    result = p.dispatch_story(PLAN, "S-1")

    assert result == {"ok": False, "error": "stub: stop before git"}
    assert worktree_calls == [str(worktree_root / "S-1")]


def test_an_unknown_story_still_reports_no_such_story(plan_dir, worktree_root, worktree_calls):
    _write_manifest(plan_dir, "done")

    result = p.dispatch_story(PLAN, "NOPE")

    assert result == {"ok": False, "error": "No such story NOPE"}
