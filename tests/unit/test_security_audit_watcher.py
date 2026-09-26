"""Scheduler tick: notify when a repo is due a security audit.

Drives the real ``watchers.scan_all_plans`` sweep. The notice is advisory and
off unless a threshold env var is set; only the notification seam, the clock
and the env are stubbed.
"""

import json
import subprocess
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import paths, security_audit, watchers

_COMMITS = "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS"
_DAYS = "PIPELINE_SECURITY_AUDIT_EVERY_DAYS"
_EVENT = "security_audit_due"
_REFERENCE = (Path(__file__).resolve().parents[2] / "REFERENCE.md").read_text()


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for n in range(3):
        (root / "f.txt").write_text(str(n))
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", f"c{n}")
    return root


@pytest.fixture
def plans(tmp_path, monkeypatch):
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(paths, "PLAN_DIR", directory)
    monkeypatch.delenv(_COMMITS, raising=False)
    monkeypatch.delenv(_DAYS, raising=False)
    return directory


@pytest.fixture
def notices(monkeypatch):
    calls = []
    monkeypatch.setattr(watchers, "_notify_user", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(watchers, "_SECURITY_DUE_LAST_EMIT", {})
    return calls


def _write_manifest(plans, name, repo_root, **extra):
    manifest = {"repo_root": str(repo_root), "stories": {}, **extra}
    (plans / f"{name}.manifest.json").write_text(json.dumps(manifest))


def _due(calls):
    return [(a, k) for a, k in calls if k.get("event") == _EVENT]


def _audited(plans, repo, sha, days_ago=0):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    security_audit.record_audit(plans, str(repo), sha, when)


def test_nothing_is_sent_and_git_is_not_touched_when_the_thresholds_are_off(
    plans, notices, repo, monkeypatch
):
    _write_manifest(plans, "planA", repo)

    def _no_git(*a, **k):
        raise AssertionError("git must not run while the feature is off")

    monkeypatch.setattr(security_audit, "commits_since", _no_git)

    watchers.scan_all_plans(_Bus())

    assert _due(notices) == []


def test_a_repo_never_audited_gets_a_due_notice(plans, notices, repo, monkeypatch):
    monkeypatch.setenv(_COMMITS, "50")
    _write_manifest(plans, "planA", repo)

    watchers.scan_all_plans(_Bus())

    found = _due(notices)
    assert len(found) == 1
    (plan_name, message), kwargs = found[0]
    assert plan_name == "planA"
    assert str(repo) in message
    assert "no security audit on record" in message
    assert "record_security_audit" in message
    assert kwargs["dedup_key"].startswith("security_audit_due:")


def test_a_repo_over_the_commit_threshold_gets_a_notice(plans, notices, repo, monkeypatch):
    monkeypatch.setenv(_COMMITS, "2")
    first = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    _audited(plans, repo, first)
    _write_manifest(plans, "planA", repo)

    watchers.scan_all_plans(_Bus())

    assert "2 commits" in _due(notices)[0][0][1]


def test_a_repo_audited_at_head_gets_no_notice(plans, notices, repo, monkeypatch):
    monkeypatch.setenv(_COMMITS, "1")
    _audited(plans, repo, _git(repo, "rev-parse", "HEAD"))
    _write_manifest(plans, "planA", repo)

    watchers.scan_all_plans(_Bus())

    assert _due(notices) == []


def test_a_repo_over_the_day_threshold_gets_a_notice(plans, notices, repo, monkeypatch):
    monkeypatch.setenv(_DAYS, "14")
    _audited(plans, repo, _git(repo, "rev-parse", "HEAD"), days_ago=15)
    _write_manifest(plans, "planA", repo)

    watchers.scan_all_plans(_Bus())

    assert "days" in _due(notices)[0][0][1]


def test_a_second_sweep_inside_the_cooldown_stays_quiet(plans, notices, repo, monkeypatch):
    monkeypatch.setenv(_COMMITS, "50")
    _write_manifest(plans, "planA", repo)

    watchers.scan_all_plans(_Bus())
    watchers.scan_all_plans(_Bus())

    assert len(_due(notices)) == 1


def test_the_notice_repeats_once_the_cooldown_has_passed(plans, notices, repo, monkeypatch):
    monkeypatch.setenv(_COMMITS, "50")
    clock = [1000.0]
    monkeypatch.setattr(watchers, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    _write_manifest(plans, "planA", repo)

    watchers.scan_all_plans(_Bus())
    clock[0] += 24 * 3600 + 1
    watchers.scan_all_plans(_Bus())

    assert len(_due(notices)) == 2


def test_two_plans_on_one_repo_send_one_notice(plans, notices, repo, monkeypatch):
    monkeypatch.setenv(_COMMITS, "50")
    _write_manifest(plans, "planA", repo)
    _write_manifest(plans, "planB", repo)

    watchers.scan_all_plans(_Bus())

    assert len(_due(notices)) == 1
    assert _due(notices)[0][0][0] == "planA"


def test_a_paused_plan_is_not_audited(plans, notices, repo, monkeypatch):
    monkeypatch.setenv(_COMMITS, "50")
    _write_manifest(plans, "planA", repo, paused=True)

    watchers.scan_all_plans(_Bus())

    assert _due(notices) == []


def test_a_plan_whose_repo_root_is_missing_is_skipped(plans, notices, tmp_path, monkeypatch):
    monkeypatch.setenv(_COMMITS, "50")
    _write_manifest(plans, "planA", tmp_path / "gone")

    watchers.scan_all_plans(_Bus())

    assert _due(notices) == []


def test_a_failing_audit_check_never_breaks_the_sweep(plans, notices, repo, monkeypatch):
    monkeypatch.setenv(_COMMITS, "1")
    _audited(plans, repo, "a" * 40)
    _write_manifest(plans, "planA", repo)

    def _boom(*a, **k):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(security_audit, "commits_since", _boom)

    assert watchers.scan_all_plans(_Bus()) == []


def test_reference_documents_the_env_vars_and_the_event():
    assert _COMMITS in _REFERENCE
    assert _DAYS in _REFERENCE
    assert _EVENT in _REFERENCE
