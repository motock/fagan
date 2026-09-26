"""When is a repo due a security audit: thresholds, commit counting, decision."""

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import security_audit

_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
_SHA = "a" * 40
_COMMITS = "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS"
_DAYS = "PIPELINE_SECURITY_AUDIT_EVERY_DAYS"


def _state(days_ago=0, seconds_ago=0):
    when = _NOW - timedelta(days=days_ago, seconds=seconds_ago)
    return {"repo_root": "/r", "last_audited_sha": _SHA, "last_audited_at": when.isoformat()}


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    for n in range(3):
        (tmp_path / "f.txt").write_text(str(n))
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", f"c{n}")
    return tmp_path


# ---------- audit_thresholds ----------
def test_thresholds_are_off_by_default():
    assert security_audit.audit_thresholds({}) == (0, 0)


def test_thresholds_read_both_settings():
    assert security_audit.audit_thresholds({_COMMITS: "50", _DAYS: "14"}) == (50, 14)


@pytest.mark.parametrize("raw", ["", "abc", "-3", "1.5"])
def test_an_invalid_threshold_disables_that_check(raw):
    assert security_audit.audit_thresholds({_COMMITS: raw, _DAYS: "14"}) == (0, 14)


# ---------- commits_since ----------
def test_commits_since_counts_commits_after_the_audited_sha(repo):
    first = _git(repo, "rev-list", "--max-parents=0", "HEAD")

    assert security_audit.commits_since(str(repo), first) == 2


def test_commits_since_is_zero_at_head(repo):
    assert security_audit.commits_since(str(repo), _git(repo, "rev-parse", "HEAD")) == 0


def test_commits_since_is_none_for_an_unknown_sha(repo):
    assert security_audit.commits_since(str(repo), "f" * 40) is None


def test_commits_since_is_none_outside_a_git_repo(tmp_path):
    assert security_audit.commits_since(str(tmp_path), _SHA) is None


# ---------- audit_due_reason ----------
def test_nothing_is_due_when_both_checks_are_off():
    assert security_audit.audit_due_reason(None, None, _NOW, 0, 0) is None


def test_a_repo_with_no_audit_on_record_is_due_once_any_check_is_on():
    reason = security_audit.audit_due_reason(None, None, _NOW, 50, 0)

    assert "no security audit on record" in reason


def test_the_commit_threshold_is_inclusive():
    assert "commits" in security_audit.audit_due_reason(_state(), 50, _NOW, 50, 0)


def test_one_commit_under_the_threshold_is_not_due():
    assert security_audit.audit_due_reason(_state(), 49, _NOW, 50, 0) is None


def test_the_day_threshold_is_inclusive():
    assert "days" in security_audit.audit_due_reason(_state(days_ago=14), 0, _NOW, 0, 14)


def test_one_second_under_the_day_threshold_is_not_due():
    state = _state(days_ago=13, seconds_ago=86399)

    assert security_audit.audit_due_reason(state, 0, _NOW, 0, 14) is None


def test_a_disabled_check_never_fires_even_when_far_over():
    assert security_audit.audit_due_reason(_state(days_ago=400), 9999, _NOW, 0, 0) is None


def test_a_lost_audited_commit_is_due():
    reason = security_audit.audit_due_reason(_state(), None, _NOW, 50, 14)

    assert "no longer in" in reason


@pytest.mark.parametrize("stamp", ["garbage", "2026-09-01T00:00:00", ""])
def test_an_unusable_audit_timestamp_counts_as_no_audit_on_record(stamp):
    state = {"repo_root": "/r", "last_audited_sha": _SHA, "last_audited_at": stamp}

    reason = security_audit.audit_due_reason(state, 0, _NOW, 0, 14)

    assert "no security audit on record" in reason
