import json
import subprocess
from pathlib import Path

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline.review_logic import REVIEW_LOGIC_FINGERPRINT_KEY, review_logic_fingerprint

PLAN = "test_plan"
KEY = "story1"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def head_sha(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "a@b.c"],
        ["git", "config", "user.name", "test"],
    ):
        subprocess.run(cmd, cwd=worktree, check=True)
    (worktree / "file.txt").write_text("initial")
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True
    ).stdout.strip()
    return worktree, sha


def _write_manifest(plan_dir, worktree, sha, **extra):
    story = {
        "worktree": str(worktree),
        "status": "tests_passed",
        "last_reviewed_sha": sha,
        REVIEW_LOGIC_FINGERPRINT_KEY: review_logic_fingerprint(),
        **extra,
    }
    path = plan_dir / f"{PLAN}.manifest.json"
    path.write_text(json.dumps({"stories": {KEY: story}, "plan": {"name": PLAN}}))
    return path


@pytest.fixture
def notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kw: sent.append(msg))
    return sent


def _skip_messages(notifications):
    return [m for m in notifications if "review skipped" in m]


def test_should_notify_once_across_repeated_unchanged_sha_skips(plan_dir, head_sha, notifications):
    worktree, sha = head_sha
    _write_manifest(plan_dir, worktree, sha)

    first = p.review_story(PLAN, KEY)
    second = p.review_story(PLAN, KEY)

    assert len(_skip_messages(notifications)) == 1
    assert first["skipped"] == "unchanged_since_last_review"
    assert second["skipped"] == "unchanged_since_last_review"


def test_should_notify_when_previously_notified_sha_differs_from_head(plan_dir, head_sha, notifications):
    worktree, sha = head_sha
    _write_manifest(plan_dir, worktree, sha, skip_notified_sha="0" * 40)

    p.review_story(PLAN, KEY)

    assert len(_skip_messages(notifications)) == 1


def test_should_persist_skip_notified_sha_in_manifest(plan_dir, head_sha, notifications):
    worktree, sha = head_sha
    path = _write_manifest(plan_dir, worktree, sha)

    p.review_story(PLAN, KEY)

    story = json.loads(Path(path).read_text())["stories"][KEY]
    assert story["skip_notified_sha"] == sha
