"""Tests for skip_info(): why a review was skipped, and the way out of it.

The same-SHA/same-fingerprint skip used to return only
``{"ok", "status", "skipped"}``, which told an operator nothing about *why*
nothing happened or how to force a re-review. skip_info() carries the reason,
the truncated shas, the stored verdict and the escape hatch.
"""

import json
import subprocess
from unittest.mock import Mock

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import review_logic as rl
from pipeline.review_orchestrator import skip_info

SHA40 = "deadbeefcafe1234567890abcdef1234567890ab"
SHA12 = "deadbeefcafe"
TO_FORCE = "push a new commit, or clear last_reviewed_sha with patch_story"


def _story(**overrides):
    story = {
        "status": "tests_passed",
        "last_reviewed_sha": SHA40,
        "review_verdict": "approve",
    }
    story.update(overrides)
    return story


def test_skip_info_returns_the_exact_skip_result_dict():
    assert skip_info(_story(), SHA40) == {
        "ok": True,
        "status": "tests_passed",
        "skipped": "unchanged_since_last_review",
        "reason": "head_and_review_logic_unchanged",
        "head": SHA12,
        "last_reviewed": SHA12,
        "stored_verdict": "approve",
        "to_force": TO_FORCE,
    }


def test_skip_info_explains_the_skip_and_the_way_out():
    result = skip_info(_story(), SHA40)

    assert result["reason"] == "head_and_review_logic_unchanged"
    assert result["head"] == SHA12
    assert result["last_reviewed"] == SHA12
    assert result["stored_verdict"] == "approve"
    assert result["to_force"] == TO_FORCE


def test_skip_info_truncates_both_shas_to_twelve_chars():
    other = "0123456789abcdef0123456789abcdef01234567"

    result = skip_info(_story(last_reviewed_sha=other), SHA40)

    assert len(result["head"]) == 12
    assert len(result["last_reviewed"]) == 12
    assert result["head"] == SHA40[:12]
    assert result["last_reviewed"] == other[:12]


def test_stored_verdict_is_empty_when_the_story_has_no_verdict():
    story = _story()
    del story["review_verdict"]

    result = skip_info(story, SHA40)

    assert result["stored_verdict"] == ""
    assert "review_verdict" not in story


def test_skip_info_is_read_only_and_repeatable():
    story = _story()
    before = dict(story)

    first = skip_info(story, SHA40)
    second = skip_info(story, SHA40)

    assert first == second
    assert story == before
    assert len(story) == 3


# --------------------------------------------------------------------------
# The fingerprint half of the condition, through the orchestrator entrypoint.
# --------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _init_repo(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    subprocess.run(["git", "init"], cwd=worktree, check=True)
    for k, v in (("user.email", "a@b.c"), ("user.name", "test")):
        subprocess.run(["git", "config", k, v], cwd=worktree, check=True)
    (worktree / "file.txt").write_text("initial")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, check=True)
    return worktree


def _write_manifest(plan_dir, plan_name, story_key, worktree):
    story = {
        "worktree": str(worktree),
        "status": "tests_passed",
        "acceptance": [{"path": "tests/acceptance_foo.py", "source": "..."}],
    }
    manifest = {"stories": {story_key: story}, "plan": {"name": plan_name}}
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _stale_fingerprint():
    current = rl.review_logic_fingerprint()
    return "0" * 16 if current != "0" * 16 else "1" * 16


def _review_twice(tmp_path, plan_dir, monkeypatch, mutate):
    """Review a story twice on the same HEAD, mutating the stored story in
    between. Returns (second_result, reviewer_mock)."""
    from pipeline.review_orchestrator import review_story

    plan_name, story_key = "test_plan", "story1"
    worktree = _init_repo(tmp_path)
    _write_manifest(plan_dir, plan_name, story_key, worktree)
    reviewer = Mock(return_value="VERDICT: REQUEST_CHANGES\nsome finding")
    monkeypatch.setattr("pipeline.review_orchestrator._run_reviewer", reviewer)
    monkeypatch.setattr("pipeline.review_orchestrator._notify_user", lambda *a, **k: None)

    review_story(plan_name, story_key)

    path = plan_dir / f"{plan_name}.manifest.json"
    manifest = json.loads(path.read_text())
    mutate(manifest["stories"][story_key])
    path.write_text(json.dumps(manifest))

    return review_story(plan_name, story_key), reviewer


def test_same_sha_but_changed_fingerprint_is_not_skipped(tmp_path, plan_dir, monkeypatch):
    def mutate(story):
        # Same HEAD, fingerprint rewritten as if the verdict-forming modules
        # had changed: the sha alone must never be enough to skip.
        story["status"] = "tests_passed"
        story[rl.REVIEW_LOGIC_FINGERPRINT_KEY] = _stale_fingerprint()

    result, reviewer = _review_twice(tmp_path, plan_dir, monkeypatch, mutate)

    assert result.get("skipped") != "unchanged_since_last_review"
    assert result["ok"] is True
    assert reviewer.call_count == 2


def test_same_sha_and_same_fingerprint_skip_carries_the_explanation(
    tmp_path, plan_dir, monkeypatch
):
    def mutate(story):
        story["status"] = "tests_passed"

    result, reviewer = _review_twice(tmp_path, plan_dir, monkeypatch, mutate)

    assert reviewer.call_count == 1
    assert result["skipped"] == "unchanged_since_last_review"
    assert result["reason"] == "head_and_review_logic_unchanged"
    assert result["stored_verdict"] == "REQUEST_CHANGES"
    assert result["head"] == result["last_reviewed"]
    assert result["to_force"] == TO_FORCE
