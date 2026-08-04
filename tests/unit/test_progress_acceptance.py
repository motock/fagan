"""Acceptance test for per-story progress parsing (Tier 1).

Exercises the real /checklist endpoint with a worktree containing
.agent_plan.md and .agent_scratchpad.md with a PROGRESS: line.
The oracle grades the run on whether the impl makes these pass.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def worktree_dir(tmp_path, monkeypatch):
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    monkeypatch.setattr(d, "WORKTREE_ROOT", wt_root)
    return wt_root


@pytest.fixture
def client():
    return TestClient(d.app)


def test_checklist_endpoint_returns_parsed_progress(client, plan_dir, worktree_dir):
    """A story with a numbered plan and a PROGRESS: line in the scratchpad
    returns {done, total} in the progress field."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text(
        "1. write tests\n"
        "2. implement\n"
        "3. refactor\n"
    )
    (wt / ".agent_scratchpad.md").write_text(
        "PROGRESS: 1/3\n"
        "done: step 1\n"
        "next: step 2\n"
    )
    (plan_dir / "demo.manifest.json").write_text(json.dumps({
        "epics": {},
        "stories": {
            "S1": {
                "summary": "guided",
                "status": "in_progress",
                "worktree": str(wt),
                "dependencies": [],
            }
        },
    }))

    res = client.get("/api/plans/demo/stories/S1/checklist")
    assert res.status_code == 200
    body = res.json()
    assert body["progress"] is not None, f"Expected progress field, got {body}"
    assert body["progress"]["done"] == 1
    assert body["progress"]["total"] == 3


def test_checklist_endpoint_progress_null_when_no_scratchpad(client, plan_dir, worktree_dir):
    """A story with a plan but no scratchpad returns progress: null."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step\n")
    (plan_dir / "demo.manifest.json").write_text(json.dumps({
        "epics": {},
        "stories": {
            "S1": {
                "summary": "just planned",
                "status": "in_progress",
                "worktree": str(wt),
                "dependencies": [],
            }
        },
    }))

    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["progress"] is None


def test_checklist_endpoint_progress_null_when_no_progress_line(client, plan_dir, worktree_dir):
    """A scratchpad without a PROGRESS: line returns progress: null (fail open)."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step\n")
    (wt / ".agent_scratchpad.md").write_text("done: step 1\n")
    (plan_dir / "demo.manifest.json").write_text(json.dumps({
        "epics": {},
        "stories": {
            "S1": {
                "summary": "old format",
                "status": "in_progress",
                "worktree": str(wt),
                "dependencies": [],
            }
        },
    }))

    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["progress"] is None


def test_get_plan_decorates_in_progress_story_with_progress(client, plan_dir, worktree_dir):
    """The /api/plans/{name} endpoint decorates in_progress stories with
    a progress field when the worktree has parseable plan+scratchpad."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step a\n2. step b\n")
    (wt / ".agent_scratchpad.md").write_text("PROGRESS: 1/2\n")
    (plan_dir / "demo.manifest.json").write_text(json.dumps({
        "epics": {},
        "stories": {
            "S1": {
                "summary": "guided",
                "status": "in_progress",
                "worktree": str(wt),
                "dependencies": [],
            }
        },
    }))

    body = client.get("/api/plans/demo").json()
    stories = body["stories"]
    assert "S1" in stories
    assert stories["S1"].get("progress") is not None
    assert stories["S1"]["progress"]["done"] == 1
    assert stories["S1"]["progress"]["total"] == 2


def test_get_plan_does_not_add_progress_to_todo_story(client, plan_dir, worktree_dir):
    """A todo story (no worktree) must not get a progress field."""
    (plan_dir / "demo.manifest.json").write_text(json.dumps({
        "epics": {},
        "stories": {
            "S1": {
                "summary": "fresh",
                "status": "todo",
                "dependencies": [],
            }
        },
    }))

    body = client.get("/api/plans/demo").json()
    assert "progress" not in body["stories"]["S1"]


def test_checklist_endpoint_does_not_500_on_oversized_progress_digits(client, plan_dir, worktree_dir):
    """A PROGRESS: line with an absurdly long digit run must not crash the
    endpoint (CPython's int() has a max digit-string conversion limit) -
    fail open to progress: null instead of a 500, per _parse_progress's own
    documented "never raises" contract."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step\n")
    (wt / ".agent_scratchpad.md").write_text(f"PROGRESS: {'9' * 5000}/2\n")
    (plan_dir / "demo.manifest.json").write_text(json.dumps({
        "epics": {},
        "stories": {
            "S1": {
                "summary": "oversized progress",
                "status": "in_progress",
                "worktree": str(wt),
                "dependencies": [],
            }
        },
    }))

    res = client.get("/api/plans/demo/stories/S1/checklist")
    assert res.status_code == 200, f"expected fail-open 200, got {res.status_code}"
    assert res.json()["progress"] is None
