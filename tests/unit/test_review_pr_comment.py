"""Tests for the PR comment posting logic in `review_story`.

These tests verify that when a story is assigned a `changes_requested` status,
a PR is opened (if a worktree exists) and a review comment is posted to it.
It also ensures that the comment contains the raw findings, not the agent-facing
feedback preamble, and that it respects the rework cap and autofix paths.
"""

import json
import os
import subprocess
import pytest
from unittest.mock import Mock

import pipeline.server as p
from pipeline import persistence as ppers
from pipeline import concurrency as pcon

# ---------------------------------------------------------------------------
# Fixtures (replicated from sibling test files)
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    return d


@pytest.fixture
def worktree_dir(tmp_path):
    d = tmp_path / "wt"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_manifest_with_story(plan_dir, plan_name, story_key, story, *,
                                top_level=None):
    """Write a manifest containing a single story, plus optional top-level
    keys."""
    manifest = {"epics": {}, "stories": {story_key: story}}
    if top_level:
        manifest.update(top_level)
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _make_story(plan_dir, *, status, worktree=None, **extra):
    """Build a minimal story dict in the given state."""
    story = {
        "summary": "Add thing",
        "status": status,
        "worktree": str(plan_dir / "wt") if worktree is None else worktree,
        "risk": "low",
    }
    story.update(extra)
    return story


def _init_git_worktree(tmp_path, *, dirty=False, bad_message="wip(x): checkpoint-1"):
    """Initialize a real git repository in a temporary directory."""
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    subprocess.run(["git", "init"], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=wt, check=True, capture_output=True)

    (wt / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "file.txt"], cwd=wt, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", bad_message], cwd=wt, check=True, capture_output=True)

    if dirty:
        (wt / "file.txt").write_text("dirty")
        subprocess.run(["git", "add", "file.txt"], cwd=wt, check=True, capture_output=True)

    return wt


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_genuine_request_changes(plan_dir, agents_dir, worktree_dir, monkeypatch):
    """Test that a genuine REQUEST_CHANGES verdict opens a PR and posts a comment."""
    plan_name = "test_plan"
    story_key = "story_1"
    reviewer_output = "VERDICT: REQUEST_CHANGES\nFindings here"

    story = _make_story(plan_dir, status="tests_passed", worktree=worktree_dir, attempts=1)
    _write_manifest_with_story(plan_dir, plan_name, story_key, story)

    monkeypatch.setattr(p, "_run_reviewer", lambda *args, **kwargs: reviewer_output)
    open_pr = Mock(return_value="https://gh/pr/1")
    monkeypatch.setattr(p, "_open_pr", open_pr)
    post_comment = Mock()
    monkeypatch.setattr(p, "_post_pr_comment", post_comment)

    p.review_story(plan_name, story_key)

    # Re-read story to check updates
    updated_story = json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())["stories"][story_key]

    assert updated_story["status"] == "changes_requested"
    assert updated_story["pr_url"] == "https://gh/pr/1"
    assert updated_story["review_feedback"] == reviewer_output
    open_pr.assert_called_once()
    # Body should contain findings and cycle number (attempts=1)
    args, _ = post_comment.call_args
    assert "Findings here" in args[1]
    assert "1" in args[1]


def test_open_pr_raises_error(plan_dir, agents_dir, worktree_dir, monkeypatch):
    """Test that errors during PR opening/commenting are caught and don't crash the pipeline."""
    plan_name = "test_plan"
    story_key = "story_1"
    reviewer_output = "VERDICT: REQUEST_CHANGES\nFindings here"

    story = _make_story(plan_dir, status="tests_passed", worktree=worktree_dir, attempts=1)
    _write_manifest_with_story(plan_dir, plan_name, story_key, story)

    monkeypatch.setattr(p, "_run_reviewer", lambda *args, **kwargs: reviewer_output)
    monkeypatch.setattr(p, "_open_pr", Mock(side_effect=subprocess.CalledProcessError(1, "cmd")))
    post_comment = Mock()
    monkeypatch.setattr(p, "_post_pr_comment", post_comment)

    p.review_story(plan_name, story_key)

    updated_story = json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())["stories"][story_key]
    assert updated_story["status"] == "changes_requested"
    assert updated_story["review_feedback"] == reviewer_output
    post_comment.assert_not_called()


def test_inconclusive_request_changes(plan_dir, agents_dir, worktree_dir, monkeypatch):
    """Test that an inconclusive REQUEST_CHANGES (no findings) does not open a PR."""
    plan_name = "test_plan"
    story_key = "story_1"
    reviewer_output = "VERDICT: REQUEST_CHANGES"  # No findings

    story = _make_story(plan_dir, status="tests_passed", worktree=worktree_dir, attempts=1)
    _write_manifest_with_story(plan_dir, plan_name, story_key, story)

    monkeypatch.setattr(p, "_run_reviewer", lambda *args, **kwargs: reviewer_output)
    open_pr = Mock()
    monkeypatch.setattr(p, "_open_pr", open_pr)
    post_comment = Mock()
    monkeypatch.setattr(p, "_post_pr_comment", post_comment)

    p.review_story(plan_name, story_key)

    updated_story = json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())["stories"][story_key]
    assert updated_story["status"] == "tests_passed"
    open_pr.assert_not_called()
    post_comment.assert_not_called()


def test_commit_hygiene_autofix_success(plan_dir, agents_dir, worktree_dir, monkeypatch):
    """Test that a successful autofix for commit hygiene does not open a PR."""
    plan_name = "test_plan"
    story_key = "story_1"
    reviewer_output = "VERDICT: REQUEST_CHANGES\nCommit message issue"

    # Create a real git repo with a bad commit message
    wt = _init_git_worktree(worktree_dir, bad_message="wip(x): checkpoint-1")
    story = _make_story(plan_dir, status="tests_passed", worktree=wt, attempts=1)
    _write_manifest_with_story(plan_dir, plan_name, story_key, story)

    # Mock _run_reviewer to return a commit-message-only REQUEST_CHANGES
    # In reality, the autofix logic in review_story will handle this.
    # For this test, we want to see that if it's an autofix success, it doesn't open PR.

    monkeypatch.setattr(p, "_run_reviewer", lambda *args, **kwargs: reviewer_output)
    open_pr = Mock()
    monkeypatch.setattr(p, "_open_pr", open_pr)
    post_comment = Mock()
    monkeypatch.setattr(p, "_post_pr_comment", post_comment)

    p.review_story(plan_name, story_key)

    updated_story = json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())["stories"][story_key]
    # If autofix worked, status should be tests_passed
    assert updated_story["status"] == "tests_passed"
    open_pr.assert_not_called()
    post_comment.assert_not_called()


def test_rework_cap_exhausted(plan_dir, agents_dir, worktree_dir, monkeypatch):
    """Test that exceeding the rework cap results in a 'parked' status without opening a PR."""
    plan_name = "test_plan"
    story_key = "story_1"
    reviewer_output = "VERDICT: REQUEST_CHANGES\nFindings here"

    # Set attempts to rework_cap
    story = _make_story(plan_dir, status="tests_passed", worktree=worktree_dir, attempts=5)
    # Assuming REWORK_MAX_ATTEMPTS is 5
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)

    _write_manifest_with_story(plan_dir, plan_name, story_key, story)

    monkeypatch.setattr(p, "_run_reviewer", lambda *args, **kwargs: reviewer_output)
    open_pr = Mock()
    monkeypatch.setattr(p, "_open_pr", open_pr)
    post_comment = Mock()
    monkeypatch.setattr(p, "_post_pr_comment", post_comment)

    p.review_story(plan_name, story_key)

    updated_story = json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())["stories"][story_key]
    assert updated_story["status"] == "parked"
    open_pr.assert_not_called()
    post_comment.assert_not_called()


def test_comment_uses_raw_findings_not_feedback(plan_dir, agents_dir, worktree_dir, monkeypatch):
    """Test that the PR comment uses raw findings and not the agent-facing feedback preamble."""
    plan_name = "test_plan"
    story_key = "story_1"
    # Findings text with oracle preamble
    reviewer_output = "VERDICT: REQUEST_CHANGES\nFindings here\nNOTE: the acceptance oracle is currently PASSING"

    story = _make_story(plan_dir, status="tests_passed", worktree=worktree_dir, attempts=1)
    _write_manifest_with_story(plan_dir, plan_name, story_key, story)

    monkeypatch.setattr(p, "_run_reviewer", lambda *args, **kwargs: reviewer_output)
    open_pr = Mock(return_value="https://gh/pr/1")
    monkeypatch.setattr(p, "_open_pr", open_pr)
    post_comment = Mock()
    monkeypatch.setattr(p, "_post_pr_comment", post_comment)

    p.review_story(plan_name, story_key)

    updated_story = json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())["stories"][story_key]

    # The comment body should NOT contain the oracle preamble
    args, _ = post_comment.call_args
    comment_body = args[1]
    assert "Findings here" in comment_body
    assert "acceptance oracle is currently PASSING" not in comment_body

    # But the review_feedback SHOULD contain it
    assert "acceptance oracle is currently PASSING" in updated_story["review_feedback"]
