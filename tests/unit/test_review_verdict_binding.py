"""Bind the review verdict to the sha the reviewer actually graded (D-C).

``bind_verdict`` records, next to ``review_verdict``, the sha captured BEFORE
the reviewer was invoked (``review_verdict_sha``) and when the verdict was
recorded (``review_verdict_ts``). A missing sha (no worktree, unreadable git)
is stored as ``""`` - "unbound, untrusted" - never ``None`` (JSON would write
``null`` and an ``== ""`` trust check would miss it).
"""

import json
import logging
import subprocess
from datetime import UTC, datetime

import pytest

import pipeline.server as p
from pipeline import persistence as ppers
from pipeline.review_verdict_binding import bind_verdict

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
LOG_MSG = "review head moved during review"


# --- bind_verdict unit behaviour -------------------------------------------


def test_bind_sets_all_three_fields():
    story = {"id": "S7"}

    assert bind_verdict(story, "APPROVE", "aaaa111", NOW) is None

    assert story["review_verdict"] == "APPROVE"
    assert story["review_verdict_sha"] == "aaaa111"
    assert story["review_verdict_ts"] == "2026-11-05T12:00:00+00:00"


@pytest.mark.parametrize("sha", [None, ""])
def test_missing_sha_is_stored_as_empty_string(sha):
    story = {"id": "S7"}

    bind_verdict(story, "APPROVE", sha, NOW)

    assert story["review_verdict_sha"] == ""
    assert story["review_verdict_ts"] == "2026-11-05T12:00:00+00:00"


@pytest.mark.parametrize("verdict", ["APPROVE", "REQUEST_CHANGES", "UNKNOWN"])
def test_verdict_is_stored_verbatim(verdict):
    story = {}

    bind_verdict(story, verdict, "aaaa111", NOW)

    assert story["review_verdict"] == verdict


def test_second_call_replaces_the_first():
    """A later round must overwrite the binding, including a first-call ''."""
    story = {"id": "S7"}

    bind_verdict(story, "APPROVE", None, NOW)
    bind_verdict(
        story,
        "REQUEST_CHANGES",
        "aaaa111",
        datetime(2026, 11, 5, 12, 5, tzinfo=UTC),
    )

    assert story == {
        "id": "S7",
        "review_verdict": "REQUEST_CHANGES",
        "review_verdict_sha": "aaaa111",
        "review_verdict_ts": "2026-11-05T12:05:00+00:00",
    }


# --- the real review_story path -------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
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


def head_sha(worktree):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(worktree, message):
    (worktree / "file.txt").write_text(message)
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=worktree, check=True)


def create_manifest(plan_dir, plan_name, story_key, worktree):
    story = {
        "worktree": str(worktree),
        "status": "tests_passed",
        "acceptance": [{"path": "tests/acceptance_foo.py", "source": "..."}],
    }
    manifest = {"stories": {story_key: story}, "plan": {"name": plan_name}}
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def read_story(plan_dir, plan_name, story_key):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())["stories"][
        story_key
    ]


def head_moved_records(caplog):
    return [r for r in caplog.records if r.getMessage() == LOG_MSG]


def test_review_story_binds_verdict_to_the_sha_the_reviewer_saw(
    plan_dir, monkeypatch, caplog
):
    """The stub commits, so HEAD moves during the review. The binding must
    still name the pre-review sha - the only sha the reviewer could see."""
    worktree = init_repo(plan_dir)
    create_manifest(plan_dir, "test_plan", "story1", worktree)
    before_sha = head_sha(worktree)

    def _reviewer(*args, **kwargs):
        commit(worktree, "reviewer moved head")
        return "VERDICT: REQUEST_CHANGES\nsome finding"

    monkeypatch.setattr(p, "_run_reviewer", _reviewer)
    caplog.set_level(logging.INFO, logger="pipeline")

    p.review_story("test_plan", "story1")

    story = read_story(plan_dir, "test_plan", "story1")
    after_sha = head_sha(worktree)
    assert after_sha != before_sha  # the race really happened
    assert story["review_verdict"] == "REQUEST_CHANGES"
    assert story["review_verdict_sha"] == before_sha
    assert story["review_verdict_ts"]
    # ... while last_reviewed_sha keeps its existing meaning: the head now.
    assert story["last_reviewed_sha"] == after_sha

    records = head_moved_records(caplog)
    assert len(records) == 1
    assert before_sha not in records[0].getMessage()
    assert after_sha not in records[0].getMessage()


def test_no_head_moved_log_when_head_is_stable(plan_dir, monkeypatch, caplog):
    worktree = init_repo(plan_dir)
    create_manifest(plan_dir, "test_plan", "story1", worktree)
    monkeypatch.setattr(
        p, "_run_reviewer", lambda *a, **k: "VERDICT: REQUEST_CHANGES\nsome finding"
    )
    caplog.set_level(logging.INFO, logger="pipeline")

    p.review_story("test_plan", "story1")

    assert head_moved_records(caplog) == []
    story = read_story(plan_dir, "test_plan", "story1")
    assert story["review_verdict_sha"] == story["last_reviewed_sha"]


def test_legacy_manifest_without_binding_fields_still_loads_and_merges(plan_dir):
    """A manifest written before this field existed must keep working."""
    legacy = {
        "stories": {
            "story1": {
                "status": "tests_passed",
                "review_verdict": "APPROVE",
                "last_reviewed_sha": "oldsha",
            }
        },
        "plan": {"name": "test_plan"},
    }
    path = plan_dir / "test_plan.manifest.json"
    path.write_text(json.dumps(legacy))

    loaded = json.loads(path.read_text())["stories"]["story1"]
    assert "review_verdict_sha" not in loaded
    assert "review_verdict_ts" not in loaded
    # An absent binding reads as unbound, exactly like an empty one.
    assert loaded.get("review_verdict_sha", "") == ""

    bind_verdict(loaded, loaded["review_verdict"], None, NOW)

    assert loaded["last_reviewed_sha"] == "oldsha"
    assert loaded["review_verdict"] == "APPROVE"
    assert loaded["review_verdict_sha"] == ""
    assert loaded["review_verdict_ts"] == "2026-11-05T12:00:00+00:00"
