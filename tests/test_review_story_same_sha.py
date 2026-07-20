import json
import subprocess
from pathlib import Path
import pytest
from unittest.mock import Mock

import pipeline.server as p

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
                "status": "todo",
                "acceptance": True,
            }
        },
        "plan": {"name": plan_name},
    }
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return manifest_path

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

# Test 2: skip reviewer when HEAD unchanged

def test_review_story_skips_reviewer_when_head_unchanged_since_last_review(setup_story, monkeypatch):
    plan_name, story_key, worktree = setup_story
    mock_rev = Mock(return_value="VERDICT: REQUEST_CHANGES\nsome finding")
    monkeypatch.setattr(p, "_run_reviewer", mock_rev)
    p.review_story(plan_name, story_key)  # first call sets SHA
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
    result2 = p.review_story(plan_name, story_key)  # should invoke reviewer again
    assert mock_rev.call_count == 2
    assert "skipped" not in result2

# Test 4: APPROVE clears last_reviewed_sha

def test_review_story_approve_clears_last_reviewed_sha(setup_story, monkeypatch):
    plan_name, story_key, worktree = setup_story
    # first REQUEST_CHANGES to set SHA
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: REQUEST_CHANGES\nsome finding")
    p.review_story(plan_name, story_key)
    # second APPROVE
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    result = p.review_story(plan_name, story_key)
    assert result["verdict"] == "APPROVE"
    manifest_path = Path(p.PLAN_DIR) / f"{plan_name}.manifest.json"
    story = json.loads(manifest_path.read_text())["stories"][story_key]
    assert "last_reviewed_sha" not in story
