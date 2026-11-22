"""A story whose pull request cannot be opened after APPROVE parks with a reason.

Before this, the failure path notified "will retry next tick" and left the
story in ``tests_passed``, so every advance ran a new paid review of the same
commit. ``pipeline.review_open_pr.handle_open_pr_failure`` owns the bound: an
authentication failure parks at once, any other failure parks on the second
attempt. The raw git/gh stderr never reaches a park reason, a notification or
a log line, because it can carry a URL with embedded credentials.
"""

import json
import logging
import subprocess

import pytest

import pipeline.server as p
from pipeline import persistence as ppers
from pipeline import review_open_pr as rop

PLAN = "openpr-plan"
KEY = "S1"
CID = "abc"

_TOKEN_REASON = rop.AUTH_PARK_REASON
_OTHER_REASON = (
    "approved but the pull request could not be opened after 2 attempts "
    "(CalledProcessError)"
)


@pytest.fixture
def notes(monkeypatch):
    """Capture ``_notify_user`` and ``commit_story`` at the module boundary."""
    calls = []
    commits = []

    def _fake_notify(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(rop, "_notify_user", _fake_notify)
    monkeypatch.setattr(rop, "commit_story", lambda *a, **k: commits.append(a))
    return calls, commits


def _story(**extra):
    story = {"summary": "Add thing", "status": "tests_passed", "risk": "low"}
    story.update(extra)
    return story


def _cpe(stderr=None, stdout=None):
    exc = subprocess.CalledProcessError(1, "git push")
    exc.stderr = stderr
    exc.stdout = stdout
    return exc


def test_first_other_failure_counts_once_and_keeps_the_retry_notice(notes):
    calls, commits = notes
    story = _story()

    result = rop.handle_open_pr_failure(PLAN, KEY, story, _cpe(), correlation_id=CID)

    assert story["pr_open_failures"] == 1
    assert story["status"] == "tests_passed"
    assert result == {"ok": True, "verdict": "APPROVE", "status": "tests_passed"}
    assert len(calls) == 1
    assert calls[0]["args"] == (
        PLAN,
        "S1: review APPROVEd but could not open PR (CalledProcessError); will retry next tick.",
    )
    assert calls[0]["kwargs"] == {"correlation_id": CID}
    assert len(commits) == 1


def test_second_other_failure_parks_with_attempt_count_and_warning(notes):
    calls, commits = notes
    story = _story(pr_open_failures=1)

    result = rop.handle_open_pr_failure(PLAN, KEY, story, _cpe(), correlation_id=CID)

    assert story["status"] == "parked"
    assert story["parked_reason"] == _OTHER_REASON
    assert result == {"ok": False, "verdict": "APPROVE", "status": "parked"}
    assert len(calls) == 1
    assert calls[0]["args"] == (PLAN, f"S1 parked: {_OTHER_REASON}.")
    assert calls[0]["kwargs"] == {
        "severity": "warning",
        "event": "story_parked",
        "correlation_id": CID,
    }
    assert len(commits) == 1


def test_first_failure_with_a_credential_refusal_parks_at_once(notes):
    calls, _ = notes
    story = _story()
    exc = _cpe(stderr="fatal: could not read Username for 'https://github.com': No such device")

    result = rop.handle_open_pr_failure(PLAN, KEY, story, exc)

    assert story["status"] == "parked"
    assert story["parked_reason"] == _TOKEN_REASON
    assert result["ok"] is False
    assert calls[-1]["kwargs"]["event"] == "story_parked"


@pytest.mark.parametrize(
    "marker",
    [
        "could not read Username",
        "Authentication failed",
        "gh auth login",
        "GH_TOKEN",
        "Permission to",
        "403",
    ],
)
def test_each_auth_marker_parks_at_once(notes, marker):
    story = _story()

    rop.handle_open_pr_failure(PLAN, KEY, story, _cpe(stderr=f"error: {marker.upper()}"))

    assert story["status"] == "parked"
    assert story["parked_reason"] == _TOKEN_REASON


def test_os_error_without_stderr_is_an_other_failure(notes):
    story = _story()

    result = rop.handle_open_pr_failure(PLAN, KEY, story, OSError("no git"))

    assert story["pr_open_failures"] == 1
    assert story["status"] == "tests_passed"
    assert result["ok"] is True


@pytest.mark.parametrize("stderr", [None, b"Authentication failed"])
def test_non_string_stderr_is_treated_as_empty_and_does_not_raise(notes, stderr):
    story = _story()

    result = rop.handle_open_pr_failure(PLAN, KEY, story, _cpe(stderr=stderr))

    assert story["pr_open_failures"] == 1
    assert story["status"] == "tests_passed"
    assert result["ok"] is True


@pytest.mark.parametrize(
    "prior_failures, stderr",
    [
        (0, "fatal: unable to access 'https://x-access-token:SECRETVALUE@github.com/o/r/'"),
        (1, "fatal: unable to access 'https://x-access-token:SECRETVALUE@github.com/o/r/'"),
        (0, "fatal: Authentication failed for 'https://x-access-token:SECRETVALUE@github.com/o/r/'"),
    ],
)
def test_stderr_secret_never_reaches_reason_notification_or_log(
    notes, caplog, prior_failures, stderr
):
    calls, _ = notes
    story = _story(pr_open_failures=prior_failures)
    caplog.set_level(logging.DEBUG)

    rop.handle_open_pr_failure(PLAN, KEY, story, _cpe(stderr=stderr, stdout=stderr))

    assert "SECRETVALUE" not in json.dumps(story)
    assert "SECRETVALUE" not in repr(calls)
    assert "SECRETVALUE" not in caplog.text
    assert all("SECRETVALUE" not in r.getMessage() for r in caplog.records)


def test_successful_open_clears_the_failure_count(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(ppers, "PLAN_DIR", tmp_path)
    story = _story(pr_open_failures=1)
    manifest = {"epics": {}, "stories": {KEY: story}}
    (tmp_path / f"{PLAN}.manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: "https://example.test/pr/1")
    monkeypatch.setattr(p, "_post_pr_comment", lambda *a, **k: None)
    monkeypatch.setattr(p, "_reverify_acceptance", lambda *a, **k: {"state": "pass"})
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)

    p.review_story(PLAN, KEY)

    saved = json.loads((tmp_path / f"{PLAN}.manifest.json").read_text())["stories"][KEY]
    assert saved["status"] == "pr_open"
    assert "pr_open_failures" not in saved


def test_two_consecutive_failed_opens_park_the_story_with_correlation_id(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(ppers, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(p, "_reverify_acceptance", lambda *a, **k: {"state": "pass"})

    def _boom_pr(*a, **k):
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(p, "_open_pr", _boom_pr)
    story = _story(correlation_id=CID, worktree=str(tmp_path / "wt"))
    manifest = {"epics": {}, "stories": {KEY: story}}
    (tmp_path / f"{PLAN}.manifest.json").write_text(json.dumps(manifest))

    p.review_story(PLAN, KEY)
    first = json.loads((tmp_path / f"{PLAN}.manifest.json").read_text())["stories"][KEY]
    assert first["status"] == "tests_passed"
    assert first["pr_open_failures"] == 1

    p.review_story(PLAN, KEY)
    second = json.loads((tmp_path / f"{PLAN}.manifest.json").read_text())["stories"][KEY]
    assert second["status"] == "parked"
    assert second["parked_reason"] == _OTHER_REASON

    records = [
        json.loads(line)
        for line in (tmp_path / f"{PLAN}.notifications.jsonl").read_text().splitlines()
        if line.strip()
    ]
    parked = [r for r in records if "parked" in (r.get("message") or "")]
    assert parked, records
    for record in parked:
        assert record.get("correlation_id") == CID
