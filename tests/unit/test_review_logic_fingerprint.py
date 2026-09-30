"""Tests for the review-logic fingerprint gating the same-SHA review skip."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import review_logic as rl

MODULES = ["scope_gate.py", "review.py", "review_orchestrator.py", "testfiles.py"]


def test_fingerprint_is_16_lowercase_hex():
    assert re.fullmatch(r"[0-9a-f]{16}", rl.review_logic_fingerprint())


def test_fingerprint_key_constant():
    assert rl.REVIEW_LOGIC_FINGERPRINT_KEY == "last_reviewed_logic_fingerprint"


def test_fingerprint_is_stable_across_calls():
    assert rl.review_logic_fingerprint() == rl.review_logic_fingerprint()


def test_fingerprint_distinguishes_swapped_module_contents(tmp_path):
    """Entries are tagged with name and length, so equal-length bytes moved
    between two modules cannot forge the same digest."""
    src = Path(rl.__file__).resolve().parent
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        for name in MODULES:
            shutil.copy(src / name, d / name)
    x, y = b"# aaa\n", b"# bbb\n"
    for d, (sg, rv) in ((a, (x, y)), (b, (y, x))):
        (d / "scope_gate.py").write_bytes(sg)
        (d / "review.py").write_bytes(rv)
    assert rl.review_logic_fingerprint(a) != rl.review_logic_fingerprint(b)


def test_fingerprint_changes_when_a_listed_module_changes(tmp_path):
    src = Path(rl.__file__).resolve().parent
    for name in MODULES:
        shutil.copy(src / name, tmp_path / name)
    before = rl.review_logic_fingerprint(tmp_path)
    with (tmp_path / "scope_gate.py").open("ab") as fh:
        fh.write(b"\n# one extra byte\n")
    assert rl.review_logic_fingerprint(tmp_path) != before


def test_unchanged_is_false_without_last_reviewed_sha():
    story = {rl.REVIEW_LOGIC_FINGERPRINT_KEY: rl.review_logic_fingerprint()}
    assert rl.is_unchanged_since_review(story, "abc123") is False


def test_unchanged_is_false_when_sha_differs():
    story = {
        "last_reviewed_sha": "oldsha",
        rl.REVIEW_LOGIC_FINGERPRINT_KEY: rl.review_logic_fingerprint(),
    }
    assert rl.is_unchanged_since_review(story, "newsha") is False


def test_unchanged_is_false_when_fingerprint_key_absent():
    story = {"last_reviewed_sha": "abc123"}
    assert rl.is_unchanged_since_review(story, "abc123") is False


def test_unchanged_is_false_when_stored_fingerprint_is_stale():
    current = rl.review_logic_fingerprint()
    stale = "0" * 16 if current != "0" * 16 else "1" * 16
    story = {"last_reviewed_sha": "abc123", rl.REVIEW_LOGIC_FINGERPRINT_KEY: stale}
    assert rl.is_unchanged_since_review(story, "abc123") is False


def test_unchanged_is_true_when_sha_and_fingerprint_match():
    story = {
        "last_reviewed_sha": "abc123",
        rl.REVIEW_LOGIC_FINGERPRINT_KEY: rl.review_logic_fingerprint(),
    }
    assert rl.is_unchanged_since_review(story, "abc123") is True


def test_unchanged_is_false_and_does_not_raise_when_fingerprint_unreadable(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("modules unreadable")

    monkeypatch.setattr(rl, "review_logic_fingerprint", boom)
    story = {"last_reviewed_sha": "abc123", rl.REVIEW_LOGIC_FINGERPRINT_KEY: "whatever"}
    assert rl.is_unchanged_since_review(story, "abc123") is False


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
    for k, v in (("user.email", "a@b.c"), ("user.name", "test")):
        subprocess.run(["git", "config", k, v], cwd=worktree, check=True)
    (worktree / "file.txt").write_text("initial")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, check=True)
    return worktree


def create_manifest(plan_dir, plan_name, story_key, worktree):
    story = {
        "worktree": str(worktree),
        "status": "tests_passed",
        "acceptance": [{"path": "tests/acceptance_foo.py", "source": "..."}],
    }
    manifest = {"stories": {story_key: story}, "plan": {"name": plan_name}}
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return manifest_path


def reset_to_tests_passed(plan_dir, plan_name, story_key):
    manifest_path = plan_dir / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stories"][story_key]["status"] = "tests_passed"
    manifest_path.write_text(json.dumps(manifest))


def read_story(plan_dir, plan_name, story_key):
    path = Path(plan_dir) / f"{plan_name}.manifest.json"
    return json.loads(path.read_text())["stories"][story_key]


def write_story(plan_dir, plan_name, story_key, story):
    path = Path(plan_dir) / f"{plan_name}.manifest.json"
    manifest = json.loads(path.read_text())
    manifest["stories"][story_key] = story
    path.write_text(json.dumps(manifest))


@pytest.fixture
def setup_story(plan_dir):
    worktree = init_repo(plan_dir)
    plan_name = "test_plan"
    story_key = "story1"
    create_manifest(plan_dir, plan_name, story_key, worktree)
    return plan_name, story_key, worktree


def test_review_story_skips_when_sha_and_fingerprint_are_current(setup_story, monkeypatch):
    plan_name, story_key, _wt = setup_story
    mock_rev = Mock(return_value="VERDICT: REQUEST_CHANGES\nsome finding")
    monkeypatch.setattr(p, "_run_reviewer", mock_rev)
    p.review_story(plan_name, story_key)
    reset_to_tests_passed(p.PLAN_DIR, plan_name, story_key)
    result2 = p.review_story(plan_name, story_key)
    assert mock_rev.call_count == 1
    assert result2.get("skipped") == "unchanged_since_last_review"


def test_review_story_reviews_when_stored_fingerprint_is_stale(setup_story, monkeypatch):
    """Regression test for the defect: HEAD unchanged, but the logic that
    produced the last verdict is no longer the logic installed."""
    plan_name, story_key, _wt = setup_story
    mock_rev = Mock(return_value="VERDICT: REQUEST_CHANGES\nsome finding")
    monkeypatch.setattr(p, "_run_reviewer", mock_rev)
    p.review_story(plan_name, story_key)
    story = read_story(p.PLAN_DIR, plan_name, story_key)
    assert story[rl.REVIEW_LOGIC_FINGERPRINT_KEY] != "0" * 16
    story[rl.REVIEW_LOGIC_FINGERPRINT_KEY] = "0" * 16
    write_story(p.PLAN_DIR, plan_name, story_key, story)
    reset_to_tests_passed(p.PLAN_DIR, plan_name, story_key)
    result2 = p.review_story(plan_name, story_key)
    assert mock_rev.call_count == 2
    assert "skipped" not in result2


def test_review_story_records_fingerprint_on_request_changes_and_clears_on_approve(
    setup_story, monkeypatch
):
    plan_name, story_key, worktree = setup_story
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: REQUEST_CHANGES\nfinding")
    p.review_story(plan_name, story_key)
    story = read_story(p.PLAN_DIR, plan_name, story_key)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert story.get("last_reviewed_sha") == sha
    assert story.get(rl.REVIEW_LOGIC_FINGERPRINT_KEY) == rl.review_logic_fingerprint()

    (worktree / "file.txt").write_text("changed")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "change"], cwd=worktree, check=True)
    reset_to_tests_passed(p.PLAN_DIR, plan_name, story_key)
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")
    result = p.review_story(plan_name, story_key)
    assert result["verdict"] == "APPROVE"
    story = read_story(p.PLAN_DIR, plan_name, story_key)
    assert "last_reviewed_sha" not in story
    assert rl.REVIEW_LOGIC_FINGERPRINT_KEY not in story
