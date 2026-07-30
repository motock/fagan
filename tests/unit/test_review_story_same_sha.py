import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too (mirrors test_pipeline_mcp_server.py's
    # plan_dir fixture).
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d

# Helper to init a git repo with one commit

def init_repo(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    subprocess.run(["git", "init"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=worktree, check=True)
    (worktree / "file.txt").write_text("initial")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, check=True)
    return worktree

# Create minimal manifest for a story

def create_manifest(plan_dir, plan_name, story_key, worktree):
    manifest = {
        "stories": {
            story_key: {
                "worktree": str(worktree),
                # Mode 29: review_story now gates on status == "tests_passed"
                # (server.py's "Guard: skip if status not tests_passed"), so a
                # story must start reviewable the same way a real dispatched
                # story would be by the time advance_pipeline calls
                # review_story - "todo" predates that guard and would make
                # every call in this file skip immediately.
                "status": "tests_passed",
                "acceptance": [{"path": "tests/acceptance_foo.py", "source": "..."}],
            }
        },
        "plan": {"name": plan_name},
    }
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return manifest_path


def reset_to_tests_passed(plan_dir, plan_name, story_key):
    """Simulate check_story_status's test-gate flipping a reworked story back
    to "tests_passed" once its new commit's tests pass - the real precondition
    advance_pipeline enforces before ever calling review_story() a second
    time. This file calls review_story() directly, twice, to isolate the
    SHA-guard behavior under test without exercising the full dispatch/poll
    cycle, so it must simulate that reset itself."""
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stories"][story_key]["status"] = "tests_passed"
    manifest_path.write_text(json.dumps(manifest))

@pytest.fixture
def setup_story(plan_dir, monkeypatch):
    worktree = init_repo(plan_dir)
    plan_name = "test_plan"
    story_key = "story1"
    create_manifest(plan_dir, plan_name, story_key, worktree)
    return plan_name, story_key, worktree

# Test 1: records last_reviewed_sha on REQUEST_CHANGES

def test_review_story_records_last_reviewed_sha_on_request_changes(setup_story, monkeypatch):
    plan_name, story_key, worktree = setup_story
    # mock reviewer to return REQUEST_CHANGES
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: REQUEST_CHANGES\nsome finding")
    result = p.review_story(plan_name, story_key)
    assert result["ok"] is True
    # load manifest to check SHA
    manifest_path = Path(p.PLAN_DIR) / f"{plan_name}.manifest.json"
    story = json.loads(manifest_path.read_text())["stories"][story_key]
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
                          capture_output=True, text=True).stdout.strip()
    assert story.get("last_reviewed_sha") == sha

# Test 2: skip reviewer when HEAD unchanged since last review

def test_review_story_skips_reviewer_when_head_unchanged_since_last_review(setup_story, monkeypatch):
    plan_name, story_key, _worktree = setup_story
    mock_rev = Mock(return_value="VERDICT: REQUEST_CHANGES\nsome finding")
    monkeypatch.setattr(p, "_run_reviewer", mock_rev)
    p.review_story(plan_name, story_key)  # first call sets SHA
    # Simulate a rework redispatch's tests passing again (real precondition
    # for a second review_story call - see reset_to_tests_passed).
    reset_to_tests_passed(p.PLAN_DIR, plan_name, story_key)
    result2 = p.review_story(plan_name, story_key)  # second call should skip
    assert mock_rev.call_count == 1
    assert result2.get("skipped") == "unchanged_since_last_review"

# Test 3: new commit unblocks re-review

def test_review_story_reviews_again_after_new_commit_lands(setup_story, monkeypatch):
    plan_name, story_key, worktree = setup_story
    mock_rev = Mock(return_value="VERDICT: REQUEST_CHANGES\nsome finding")
    monkeypatch.setattr(p, "_run_reviewer", mock_rev)
    p.review_story(plan_name, story_key)  # first call sets SHA
    # make a new commit
    (worktree / "file.txt").write_text("changed")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "change"], cwd=worktree, check=True)
    # Simulate the rework redispatch's tests passing again (real precondition
    # for a second review_story call - see reset_to_tests_passed).
    reset_to_tests_passed(p.PLAN_DIR, plan_name, story_key)
    result2 = p.review_story(plan_name, story_key)  # should invoke reviewer again
    assert mock_rev.call_count == 2
    assert "skipped" not in result2

# Test 4: APPROVE clears last_reviewed_sha

def test_review_story_approve_clears_last_reviewed_sha(setup_story, monkeypatch):
    plan_name, story_key, worktree = setup_story
    # first REQUEST_CHANGES to set SHA
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: REQUEST_CHANGES\nsome finding")
    p.review_story(plan_name, story_key)
    # a new commit is required for the second call to actually reach the
    # reviewer again - otherwise the same-SHA skip guard under test here
    # would short-circuit before ever invoking it, and there'd be no verdict
    # to assert on (mirrors test 3's pattern).
    (worktree / "file.txt").write_text("changed")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "change"], cwd=worktree, check=True)
    # Simulate the rework redispatch's tests passing again (real precondition
    # for a second review_story call - see reset_to_tests_passed).
    reset_to_tests_passed(p.PLAN_DIR, plan_name, story_key)
    # second call: APPROVE
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")
    result = p.review_story(plan_name, story_key)
    assert result["verdict"] == "APPROVE"
    manifest_path = Path(p.PLAN_DIR) / f"{plan_name}.manifest.json"
    story = json.loads(manifest_path.read_text())["stories"][story_key]
    assert "last_reviewed_sha" not in story
