"""Integration tests for the review_finding_target_guard in pipeline.server.review_story.

Mode 28 (2026-07-21): the existing same-SHA guard only catches a review call
where HEAD is byte-identical to the last reviewed commit. A dispatch
watchdog checkpoint commit changes HEAD's SHA trivially (a WIP commit)
without the underlying content changing in any way that addresses the
reviewer's own prior Blocking findings, so the same-SHA guard is slipped,
the reviewer re-runs, and APPROVEs a diff that still has its own previously
flagged Blocking issues unaddressed -- 'merged-but-incomplete'.

These tests verify the new guard that, on an APPROVE, checks that every
file path recorded in the prior REQUEST_CHANGES cycle's Blocking findings
was actually touched by the diff since last_reviewed_sha. If any tracked
path was NOT touched, the APPROVE is downgraded to a REQUEST_CHANGES cycle.

Uses real git subprocess calls against a tmp_path worktree (mirrors
test_review_story_same_sha.py's init_repo helper), only mocking the
reviewer backend call itself.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline.config import REWORK_MAX_ATTEMPTS


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


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


def create_manifest(plan_dir, plan_name, story_key, worktree, **extra):
    manifest = {
        "stories": {
            story_key: {
                "worktree": str(worktree),
                "status": "tests_passed",
                "acceptance": [{"path": "tests/acceptance_foo.py", "source": "..."}],
                **extra,
            }
        },
        "plan": {"name": plan_name},
    }
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return manifest_path


def load_story(plan_dir, plan_name, story_key):
    manifest_path = Path(plan_dir) / f"{plan_name}.manifest.json"
    return json.loads(manifest_path.read_text())["stories"][story_key]


def reset_to_tests_passed(plan_dir, plan_name, story_key):
    """Simulate check_story_status's test-gate flipping a reworked story back
    to 'tests_passed' once its new commit's tests pass - the real precondition
    advance_pipeline enforces before ever calling review_story() a second
    time. Mirrors test_review_story_same_sha.py."""
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stories"][story_key]["status"] = "tests_passed"
    manifest_path.write_text(json.dumps(manifest))


def head_sha(worktree):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
        capture_output=True, text=True).stdout.strip()


def commit(worktree, filename, content, msg="change"):
    (worktree / filename).write_text(content)
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=worktree, check=True)


# (a) first-ever review (no last_reviewed_sha) with APPROVE proceeds normally

def test_first_review_approve_proceeds_normally(plan_dir, tmp_path, monkeypatch):
    plan_name, story_key = "P1", "S1"
    worktree = init_repo(tmp_path)
    create_manifest(plan_dir, plan_name, story_key, worktree)
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")
    result = p.review_story(plan_name, story_key)
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    story = load_story(plan_dir, plan_name, story_key)
    assert story["status"] == "pr_open"
    assert "last_reviewed_sha" not in story


# (b) prior REQUEST_CHANGES tracked foo.py, new commit touched foo.py, APPROVE proceeds

def test_approve_proceeds_when_tracked_file_was_touched(plan_dir, tmp_path, monkeypatch):
    plan_name, story_key = "P1", "S1"
    worktree = init_repo(tmp_path)
    create_manifest(plan_dir, plan_name, story_key, worktree, acceptance=[])
    # First cycle: REQUEST_CHANGES with a Blocking finding on foo.py.
    monkeypatch.setattr(
        p, "_run_reviewer",
        lambda *a, **k: "VERDICT: REQUEST_CHANGES\n- Blocking: foo.py: needs guard")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")
    monkeypatch.setattr(p, "_post_pr_comment", Mock())
    p.review_story(plan_name, story_key)
    story = load_story(plan_dir, plan_name, story_key)
    assert story["status"] == "changes_requested"
    assert story.get("last_review_findings") == ["foo.py"]
    # Rework: touch foo.py (the tracked file) and land a new commit.
    commit(worktree, "foo.py", "fixed", msg="fix foo")
    reset_to_tests_passed(plan_dir, plan_name, story_key)
    # Second cycle: APPROVE.
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")
    result = p.review_story(plan_name, story_key)
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    story = load_story(plan_dir, plan_name, story_key)
    assert story["status"] == "pr_open"
    # last_review_findings cleared on a clean APPROVE.
    assert story.get("last_review_findings", []) == []
    assert "last_reviewed_sha" not in story


# (c) tracked file NOT touched by the new commit -> APPROVE downgraded

def test_approve_downgraded_when_tracked_file_not_touched(plan_dir, tmp_path, monkeypatch):
    plan_name, story_key = "P1", "S1"
    worktree = init_repo(tmp_path)
    create_manifest(plan_dir, plan_name, story_key, worktree, acceptance=[])
    # First cycle: REQUEST_CHANGES with a Blocking finding on foo.py.
    monkeypatch.setattr(
        p, "_run_reviewer",
        lambda *a, **k: "VERDICT: REQUEST_CHANGES\n- Blocking: foo.py: needs guard")
    monkeypatch.setattr(p, "_open_pr", Mock(return_value="https://gh/pr/1"))
    monkeypatch.setattr(p, "_post_pr_comment", Mock())
    p.review_story(plan_name, story_key)
    story = load_story(plan_dir, plan_name, story_key)
    assert story.get("last_review_findings") == ["foo.py"]
    prior_attempts = story.get("rework_attempts", 0)
    # Rework: a WIP checkpoint commit touching an UNRELATED file only.
    commit(worktree, "unrelated.txt", "wip", msg="wip checkpoint")
    reset_to_tests_passed(plan_dir, plan_name, story_key)
    # Second cycle: reviewer returns APPROVE anyway (the bug under test).
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    open_pr = Mock(return_value="https://gh/pr/1")
    monkeypatch.setattr(p, "_open_pr", open_pr)
    post_comment = Mock()
    monkeypatch.setattr(p, "_post_pr_comment", post_comment)
    result = p.review_story(plan_name, story_key)
    # Downgraded to REQUEST_CHANGES, which now opens the PR and posts the
    # reviewer's findings so a human can see what the gate flagged.
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    open_pr.assert_called_once()
    post_comment.assert_called_once()
    args, _ = post_comment.call_args
    assert "foo.py" in args[1]
    story = load_story(plan_dir, plan_name, story_key)
    assert story["status"] == "changes_requested"
    assert story["status"] != "pr_open"
    # review_feedback names the untouched file.
    assert "foo.py" in story.get("review_feedback", "")
    # rework_attempts increments by exactly 1.
    assert story["rework_attempts"] == prior_attempts + 1
    # The untouched file is carried forward so the next cycle still requires it.
    assert "foo.py" in story.get("last_review_findings", [])


# (d) repeating (c) until the rework cap parks the story

def test_repeated_downgrade_parks_at_rework_cap(plan_dir, tmp_path, monkeypatch):
    plan_name, story_key = "P1", "S1"
    worktree = init_repo(tmp_path)
    create_manifest(plan_dir, plan_name, story_key, worktree)
    # First cycle: REQUEST_CHANGES with a Blocking finding on foo.py.
    monkeypatch.setattr(
        p, "_run_reviewer",
        lambda *a, **k: "VERDICT: REQUEST_CHANGES\n- Blocking: foo.py: needs guard")
    p.review_story(plan_name, story_key)
    # Now drive repeated cycles where the reviewer keeps APPROVEing but the
    # tracked file foo.py is never touched (only unrelated WIP commits land).
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", Mock(return_value="https://gh/pr/1"))
    # The first REQUEST_CHANGES already consumed one rework attempt. Drive
    # additional review cycles until the cap is reached and the story parks.
    # REWORK_MAX_ATTEMPTS is the cap for a plain (non-escalated, non-oracle)
    # story; this story carries acceptance, so its cap is REWORK_MAX_ATTEMPTS
    # only when acceptance is absent. To exercise the plain-story cap we
    # rebuild the manifest without acceptance.
    create_manifest(plan_dir, plan_name, story_key, worktree, acceptance=[])
    # Re-seed the first REQUEST_CHANGES cycle on the plain story.
    monkeypatch.setattr(
        p, "_run_reviewer",
        lambda *a, **k: "VERDICT: REQUEST_CHANGES\n- Blocking: foo.py: needs guard")
    p.review_story(plan_name, story_key)
    story = load_story(plan_dir, plan_name, story_key)
    assert story.get("last_review_findings") == ["foo.py"]
    attempts = story.get("rework_attempts", 0)
    # Each subsequent cycle: land an unrelated WIP commit, reset to
    # tests_passed, then review with an APPROVE that must be downgraded.
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", Mock(return_value="https://gh/pr/1"))
    while story.get("status") != "parked":
        commit(worktree, f"unrelated_{attempts}.txt", "wip", msg="wip checkpoint")
        reset_to_tests_passed(plan_dir, plan_name, story_key)
        p.review_story(plan_name, story_key)
        story = load_story(plan_dir, plan_name, story_key)
        attempts = story.get("rework_attempts", 0)
        # Safety valve: never loop forever.
        if attempts > REWORK_MAX_ATTEMPTS + 2:
            pytest.fail("rework_attempts exceeded cap without parking")
    assert story["status"] == "parked"
    assert "parked_reason" in story
    assert "foo.py" in story.get("last_review_findings", [])


# (e) prior REQUEST_CHANGES had no parseable Blocking targets -> APPROVE no-op

def test_approve_proceeds_when_no_trackable_findings(plan_dir, tmp_path, monkeypatch):
    plan_name, story_key = "P1", "S1"
    worktree = init_repo(tmp_path)
    create_manifest(plan_dir, plan_name, story_key, worktree)
    # First cycle: REQUEST_CHANGES with prose findings that don't follow the
    # new format -> no trackable targets.
    monkeypatch.setattr(
        p, "_run_reviewer",
        lambda *a, **k: "VERDICT: REQUEST_CHANGES\nsome prose finding without a file path")
    monkeypatch.setattr(p, "_open_pr", Mock(return_value="https://gh/pr/1"))
    monkeypatch.setattr(p, "_post_pr_comment", Mock())
    p.review_story(plan_name, story_key)
    story = load_story(plan_dir, plan_name, story_key)
    assert story.get("last_review_findings") == []
    # Land a new commit so the same-SHA guard doesn't short-circuit.
    commit(worktree, "file.txt", "changed", msg="change")
    reset_to_tests_passed(plan_dir, plan_name, story_key)
    # Second cycle: APPROVE must proceed normally (nothing to verify).
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")
    result = p.review_story(plan_name, story_key)
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    story = load_story(plan_dir, plan_name, story_key)
    assert story["status"] == "pr_open"


# (f2) tracked finding is a TEST FILE and the current full suite is green
# (review_story only reaches APPROVE when status == "tests_passed") -> a
# correct fix that lands in the implementation the test exercises, not the
# test file itself, must not be downgraded. Root-caused live 2026-07-24 on
# MODE40-CI-REWORK-FEEDBACK-V2: a gate-synthesized review flagged
# test_ci_rework_feedback.py (the file where the assertion failed), the
# agent correctly fixed the bug in pipeline/feedback.py (the file the test
# exercises), the full suite went green and a real reviewer said APPROVE,
# but this guard downgraded it back to REQUEST_CHANGES anyway because
# test_ci_rework_feedback.py's own bytes were untouched - burning the
# story's entire rework budget on a finding that was already resolved.

def test_approve_proceeds_when_tracked_test_file_untouched_but_suite_passes(
        plan_dir, tmp_path, monkeypatch):
    plan_name, story_key = "P1", "S1"
    worktree = init_repo(tmp_path)
    create_manifest(plan_dir, plan_name, story_key, worktree, acceptance=[])
    # First cycle: REQUEST_CHANGES flags a failing TEST file (the shape a
    # gate-synthesized review or an LLM reviewer produces for a red test).
    monkeypatch.setattr(
        p, "_run_reviewer",
        lambda *a, **k: "VERDICT: REQUEST_CHANGES\n- Blocking: test_foo.py: assertion fails")
    monkeypatch.setattr(p, "_open_pr", Mock(return_value="https://gh/pr/1"))
    monkeypatch.setattr(p, "_post_pr_comment", Mock())
    p.review_story(plan_name, story_key)
    story = load_story(plan_dir, plan_name, story_key)
    assert story.get("last_review_findings") == ["test_foo.py"]
    # Rework: fix lands in the IMPLEMENTATION the test exercises, not the
    # test file itself. The story only reaches "tests_passed" (the
    # precondition for review_story to even run) once the full suite -
    # including test_foo.py - is green, so this is a real, verified fix.
    commit(worktree, "impl.py", "fixed the bug", msg="fix impl")
    reset_to_tests_passed(plan_dir, plan_name, story_key)
    # Second cycle: a real reviewer's independent verdict is APPROVE.
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    open_pr = Mock(return_value="https://gh/pr/1")
    monkeypatch.setattr(p, "_open_pr", open_pr)
    result = p.review_story(plan_name, story_key)
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    open_pr.assert_called_once()
    story = load_story(plan_dir, plan_name, story_key)
    assert story["status"] == "pr_open"
    assert story.get("last_review_findings", []) == []


# (f3) mixed findings: a tracked TEST file (untouched, suite green - exempt)
# alongside a tracked NON-test file (also untouched) -> APPROVE is still
# downgraded, and only the real non-test finding is named. Proves the
# test-file exemption is scoped narrowly, not a blanket bypass of the guard.

def test_approve_still_downgraded_for_untouched_non_test_file_alongside_exempt_test_file(
        plan_dir, tmp_path, monkeypatch):
    plan_name, story_key = "P1", "S1"
    worktree = init_repo(tmp_path)
    create_manifest(plan_dir, plan_name, story_key, worktree, acceptance=[])
    monkeypatch.setattr(
        p, "_run_reviewer",
        lambda *a, **k: (
            "VERDICT: REQUEST_CHANGES\n"
            "- Blocking: test_foo.py: assertion fails\n"
            "- Blocking: server.py: logic bug unrelated to the test failure"
        ))
    monkeypatch.setattr(p, "_open_pr", Mock(return_value="https://gh/pr/1"))
    monkeypatch.setattr(p, "_post_pr_comment", Mock())
    p.review_story(plan_name, story_key)
    story = load_story(plan_dir, plan_name, story_key)
    assert set(story.get("last_review_findings", [])) == {"test_foo.py", "server.py"}
    # Rework: fixes the test (suite goes green) but never touches server.py.
    commit(worktree, "impl.py", "fixed the test failure", msg="fix impl")
    reset_to_tests_passed(plan_dir, plan_name, story_key)
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    open_pr = Mock(return_value="https://gh/pr/1")
    monkeypatch.setattr(p, "_open_pr", open_pr)
    post_comment = Mock()
    monkeypatch.setattr(p, "_post_pr_comment", post_comment)
    result = p.review_story(plan_name, story_key)
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    open_pr.assert_called_once()
    post_comment.assert_called_once()
    args, _ = post_comment.call_args
    assert "server.py" in args[1]
    story = load_story(plan_dir, plan_name, story_key)
    feedback = story.get("review_feedback", "")
    assert "server.py" in feedback
    assert "test_foo.py" not in feedback


# (f) git-diff subprocess failure does NOT block the APPROVE (fail-open)

def test_approve_fail_open_on_git_diff_error(plan_dir, tmp_path, monkeypatch):
    plan_name, story_key = "P1", "S1"
    worktree = init_repo(tmp_path)
    create_manifest(plan_dir, plan_name, story_key, worktree)
    # First cycle: REQUEST_CHANGES tracking foo.py.
    monkeypatch.setattr(
        p, "_run_reviewer",
        lambda *a, **k: "VERDICT: REQUEST_CHANGES\n- Blocking: foo.py: needs guard")
    monkeypatch.setattr(p, "_open_pr", Mock(return_value="https://gh/pr/1"))
    monkeypatch.setattr(p, "_post_pr_comment", Mock())
    p.review_story(plan_name, story_key)
    story = load_story(plan_dir, plan_name, story_key)
    assert story.get("last_review_findings") == ["foo.py"]
    # Land a new commit so the same-SHA guard doesn't short-circuit.
    commit(worktree, "unrelated.txt", "wip", msg="wip checkpoint")
    reset_to_tests_passed(plan_dir, plan_name, story_key)
    # Point last_reviewed_sha at a SHA that does not exist in the repo so the
    # real `git diff --name-only <sha> HEAD` exits non-zero -> fail-open.
    story = load_story(plan_dir, plan_name, story_key)
    story["last_reviewed_sha"] = "0" * 40
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stories"][story_key]["last_reviewed_sha"] = "0" * 40
    manifest_path.write_text(json.dumps(manifest))
    # Second cycle: APPROVE must proceed despite the git-diff failure.
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")
    result = p.review_story(plan_name, story_key)
    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    story = load_story(plan_dir, plan_name, story_key)
    assert story["status"] == "pr_open"