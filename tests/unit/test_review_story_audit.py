"""``story.review_start``: the audit event that marks a review beginning.

``story.dispatch`` and ``story.merge`` bracket a story's life in the audit
stream, but nothing marked when a *review* started, so no time-in-review could
be computed from the stream later (liveness S9). This story only records the
event; it computes nothing.

The event is emitted AFTER the review call returns, because the original review
may skip - ``unchanged_since_last_review``, ``not_reviewable_state``, or
``locked`` - and a skip is not a review. The consequence is that the record's
own ``ts`` is the END of the call, while ``detail["started_at"]`` carries the
start captured before it. These tests pin every edge of that contract,
including that a broken audit stream can never fail a review.

The service-level tests drive the REAL ``pipeline.server.review_story`` against
a real git repo with only ``_run_reviewer`` stubbed (fixture shape cribbed from
tests/unit/test_review_force.py).
"""

import fcntl
import json
import logging
import os
import subprocess
from datetime import datetime

import pytest

import pipeline.concurrency as pcon
import pipeline.persistence as ppers
import pipeline.server as p
import pipeline.store as pstore
from pipeline import audit, review_audit
from pipeline.review_logic import review_logic_fingerprint

#: Text that must never reach the audit stream: verdict prose and feedback.
VERDICT_TEXT = "LGTM-SENTINEL"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding the code under test reads at a temp dir."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(pstore, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    monkeypatch.delenv("PIPELINE_STORE", raising=False)
    return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _init_repo(tmp_path):
    """A repo on branch ``main`` with a base commit, then branch ``feature``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "pipeline").mkdir()
    (repo / "pipeline" / "foo.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-b", "feature")
    (repo / "pipeline" / "foo.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    return repo


def _head_sha(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write_manifest(plan_dir, plan_name, stories):
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps({"epics": {}, "stories": stories}))


def _audit_records(plan_dir, plan_name, action=None):
    """Return the plan's audit records, oldest first (empty when none)."""
    path = plan_dir / f"{plan_name}.audit.jsonl"
    if not path.exists():
        return []
    records = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    if action is not None:
        records = [r for r in records if r.get("action") == action]
    return records


def _fresh_story(repo, **overrides):
    """A ``tests_passed`` story never reviewed before: an unforced review runs."""
    story = {
        "summary": "Add thing",
        "status": "tests_passed",
        "worktree": str(repo),
        "risk": "low",
    }
    story.update(overrides)
    return story


def _reviewed_story(repo, **overrides):
    """A story already reviewed at HEAD: an unforced review skips as unchanged."""
    head = _head_sha(repo)
    story = _fresh_story(
        repo,
        last_reviewed_sha=head,
        last_reviewed_logic_fingerprint=review_logic_fingerprint(),
        skip_notified_sha=head,
        review_verdict="REQUEST_CHANGES",
        rework_attempts=2,
    )
    story.update(overrides)
    return story


def _quiet_environment(monkeypatch):
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: "https://gh/pr/1")


def _record_reviewer(monkeypatch, output=f"{VERDICT_TEXT}\nVERDICT: APPROVE"):
    calls = []

    def _fake_reviewer(*args, **kwargs):
        calls.append(True)
        return output

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)
    return calls


def _holds_plan_lock(plan_dir, plan_name):
    """Hold the per-plan flock so ``review_story`` skips as locked."""
    fd = os.open(plan_dir / f"{plan_name}.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


# ---------------------------------------------------------------------------
# The event: written once, shaped as specified, for a review that runs
# ---------------------------------------------------------------------------

def test_a_review_that_runs_writes_exactly_one_start_event(plan_dir, monkeypatch):
    _quiet_environment(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _fresh_story(repo)})

    result = p.review_story("pe", "S1")

    assert result.get("ok") is True, result
    assert reviewer_calls == [True], "the review must actually have run"
    records = _audit_records(plan_dir, "pe", action="story.review_start")
    assert len(records) == 1, records
    record = records[0]
    assert record["outcome"] == "started"
    assert record["target"] == {"plan": "pe", "story": "S1"}
    assert record["detail"]["forced"] is False
    # started_at is the START of the call: never later than the record's ts,
    # which is written when the call returns.
    started_at = datetime.fromisoformat(record["detail"]["started_at"])
    assert started_at <= datetime.fromisoformat(record["ts"]), record
    assert started_at.utcoffset() is not None, "started_at must be timezone-aware"
    assert started_at.utcoffset().total_seconds() == 0, "started_at must be UTC"


def test_the_event_carries_no_verdict_diff_or_feedback_text(plan_dir, monkeypatch):
    _quiet_environment(monkeypatch)
    _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _fresh_story(repo)})

    p.review_story("pe", "S1")

    record = _audit_records(plan_dir, "pe", action="story.review_start")[0]
    # Identifiers and the two timing fields, nothing else: a review's verdict,
    # feedback, diff and sha stay out of the stream.
    assert set(record["target"]) == {"plan", "story"}
    assert set(record["detail"]) == {"started_at", "forced"}
    assert VERDICT_TEXT not in json.dumps(record)
    assert "verdict" not in json.dumps(record).lower()


# ---------------------------------------------------------------------------
# Nothing is written for a call that is not a review
# ---------------------------------------------------------------------------

def test_a_skip_as_unchanged_since_last_review_writes_nothing(plan_dir, monkeypatch):
    _quiet_environment(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _reviewed_story(repo)})

    result = p.review_story("pe", "S1")

    assert result["skipped"] == "unchanged_since_last_review", result
    assert reviewer_calls == []
    assert _audit_records(plan_dir, "pe", action="story.review_start") == []


def test_a_skip_as_not_reviewable_state_writes_nothing(plan_dir, monkeypatch):
    _quiet_environment(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _fresh_story(repo, status="in_progress")})

    result = p.review_story("pe", "S1")

    assert result["skipped"] == "not_reviewable_state", result
    assert reviewer_calls == []
    assert _audit_records(plan_dir, "pe", action="story.review_start") == []


def test_a_locked_call_writes_nothing(plan_dir, monkeypatch):
    _quiet_environment(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _fresh_story(repo)})
    fd = _holds_plan_lock(plan_dir, "pe")
    try:
        result = p.review_story("pe", "S1")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["skipped"] == "locked", result
    assert reviewer_calls == []
    assert _audit_records(plan_dir, "pe", action="story.review_start") == []


def test_a_failed_review_result_writes_nothing(plan_dir, monkeypatch):
    """``ok: False`` is a failed review, not a started one."""
    _quiet_environment(monkeypatch)
    monkeypatch.setattr(
        p, "_original_review_story",
        lambda plan_name, story_key: {"ok": False, "error": "reviewer crashed"},
    )
    _write_manifest(plan_dir, "pe", {"S1": {"status": "tests_passed"}})

    result = p.review_story("pe", "S1")

    assert result == {"ok": False, "error": "reviewer crashed"}
    assert _audit_records(plan_dir, "pe", action="story.review_start") == []


# ---------------------------------------------------------------------------
# force=True: two different actions, both recorded
# ---------------------------------------------------------------------------

def test_a_forced_review_records_the_start_event_besides_the_force_entry(
    plan_dir, monkeypatch
):
    _quiet_environment(monkeypatch)
    _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _reviewed_story(repo)})

    p.review_story("pe", "S1", force=True)

    forced = _audit_records(plan_dir, "pe", action="story:force_review")
    started = _audit_records(plan_dir, "pe", action="story.review_start")
    assert len(forced) == 1, "the existing force entry must still be recorded"
    assert len(started) == 1, started
    assert started[0]["detail"]["forced"] is True
    assert started[0]["target"] == {"plan": "pe", "story": "S1"}


# ---------------------------------------------------------------------------
# A broken audit stream must never fail a review
# ---------------------------------------------------------------------------

def test_a_failing_audit_returns_the_result_unchanged_and_warns_once(
    plan_dir, monkeypatch, caplog
):
    _quiet_environment(monkeypatch)
    _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _fresh_story(repo)})

    real_original = p._original_review_story
    captured = {}

    def _capturing_original(plan_name, story_key):
        captured["result"] = real_original(plan_name, story_key)
        return captured["result"]

    monkeypatch.setattr(p, "_original_review_story", _capturing_original)

    def _explode(*args, **kwargs):
        raise RuntimeError("story-42 not found")

    monkeypatch.setattr(review_audit, "record_audit", _explode)

    with caplog.at_level(logging.WARNING, logger="pipeline.review_audit"):
        result = p.review_story("pe", "S1")

    # The review result is the reviewer's own dict, untouched by the failure.
    assert result is captured["result"], "the audit failure must not alter the result"
    assert result.get("ok") is True, result
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "pipeline.review_audit"
    ]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    message = warnings[0].getMessage()
    assert "RuntimeError" in message, message
    # The exception text can carry a story key: log the type only.
    assert "story-42" not in message, message


def test_the_audit_chain_still_verifies_with_the_new_event(plan_dir, monkeypatch):
    _quiet_environment(monkeypatch)
    _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _fresh_story(repo)})

    p.review_story("pe", "S1")

    assert _audit_records(plan_dir, "pe", action="story.review_start")
    assert audit.verify_audit_chain("pe") is None, "the new record must chain"
