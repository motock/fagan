"""A failed `git fetch origin` must fail the rebase instead of rebasing onto a stale base.

Concurrent fetches lose the `cannot lock ref` race (75 of 90 in a probe), and
network/auth errors fail too. _rebase_onto_master used to discard the fetch
result, so the merge gate rebased onto a stale origin/<default>. The fetch is now
retried (3 attempts, 1s then 2s backoff) and a final failure is returned as
ok=False, conflict=False without leaking the fetch's stderr (it can embed a
tokenised remote URL).
"""

import json
import logging
import subprocess
from types import SimpleNamespace

import pytest

import pipeline.merge as merge_mod
import pipeline.rebase as rebase_mod
import pipeline.server as p
from pipeline.server import _advance_pipeline_locked

SECRET_STDERR = "fatal: unable to access 'https://x-access-token:SECRETTOKEN@github.com/o/r.git/'"


class _FakeGit:
    """Stands in for the `subprocess` module as seen by pipeline.rebase."""

    CompletedProcess = subprocess.CompletedProcess

    def __init__(self, fetch_returncodes, real_passthrough=None):
        self._fetch_returncodes = list(fetch_returncodes)
        self._real = real_passthrough
        self.calls = []

    def run(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        if argv[:3] == ["git", "fetch", "origin"]:
            rc = self._fetch_returncodes.pop(0)
            return subprocess.CompletedProcess(argv, rc, "", SECRET_STDERR if rc else "")
        if self._real is not None:
            return self._real(argv, *args, **kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def fetches(self):
        return [c for c in self.calls if c[:3] == ["git", "fetch", "origin"]]

    def rebases(self):
        return [c for c in self.calls if c[:2] == ["git", "rebase"]]


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(rebase_mod, "time", SimpleNamespace(sleep=recorded.append))
    return recorded


@pytest.fixture
def rebase_env(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr("pipeline.git_ops._commit_wip", lambda *a, **k: None)
    monkeypatch.setattr(rebase_mod, "_ensure_on_branch", lambda wt, br: {"ok": True, "error": ""})
    return tmp_path


def _use_git(monkeypatch, fake):
    monkeypatch.setattr(rebase_mod, "subprocess", fake)


def test_should_fail_without_rebasing_when_all_three_fetch_attempts_fail(rebase_env, sleeps, monkeypatch):
    git = _FakeGit([128, 128, 128])
    _use_git(monkeypatch, git)

    result = rebase_mod._rebase_onto_master(str(rebase_env), "agent/s1")

    assert result["ok"] is False
    assert result["conflict"] is False
    assert result["error"] == "git fetch origin failed (exit 128)"
    assert git.rebases() == []


def test_should_attempt_fetch_exactly_three_times_before_giving_up(rebase_env, sleeps, monkeypatch):
    git = _FakeGit([1, 1, 1])
    _use_git(monkeypatch, git)

    rebase_mod._rebase_onto_master(str(rebase_env), "agent/s1")

    assert len(git.fetches()) == 3


def test_should_back_off_one_then_two_seconds_between_fetch_attempts(rebase_env, sleeps, monkeypatch):
    _use_git(monkeypatch, _FakeGit([1, 1, 1]))

    rebase_mod._rebase_onto_master(str(rebase_env), "agent/s1")

    assert sleeps == [1, 2]


def test_should_rebase_when_fetch_fails_once_then_succeeds(rebase_env, sleeps, monkeypatch):
    git = _FakeGit([1, 0])
    _use_git(monkeypatch, git)

    result = rebase_mod._rebase_onto_master(str(rebase_env), "agent/s1")

    assert result == {"ok": True, "conflict": False, "error": ""}
    assert len(git.fetches()) == 2
    assert len(git.rebases()) == 1
    assert sleeps == [1]


def test_should_fetch_once_and_not_sleep_when_first_fetch_succeeds(rebase_env, sleeps, monkeypatch):
    git = _FakeGit([0])
    _use_git(monkeypatch, git)

    result = rebase_mod._rebase_onto_master(str(rebase_env), "agent/s1")

    assert result["ok"] is True
    assert len(git.fetches()) == 1
    assert sleeps == []


def test_should_not_leak_fetch_stderr_in_error_or_logs(rebase_env, sleeps, monkeypatch, caplog):
    _use_git(monkeypatch, _FakeGit([128, 128, 128]))

    with caplog.at_level(logging.DEBUG):
        result = rebase_mod._rebase_onto_master(str(rebase_env), "agent/s1")

    assert "SECRETTOKEN" not in result["error"]
    assert "SECRETTOKEN" not in caplog.text
    assert all("SECRETTOKEN" not in str(v) for r in caplog.records for v in vars(r).values())


def test_should_log_one_warning_with_returncode_and_attempts(rebase_env, sleeps, monkeypatch, caplog):
    _use_git(monkeypatch, _FakeGit([128, 128, 128]))

    with caplog.at_level(logging.WARNING):
        rebase_mod._rebase_onto_master(str(rebase_env), "agent/s1")

    fetch_warnings = [r for r in caplog.records if getattr(r, "returncode", None) == 128]
    assert len(fetch_warnings) == 1
    assert fetch_warnings[0].levelno == logging.WARNING
    assert fetch_warnings[0].attempts == 3


# ---------- merge-gate integration ----------

def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path):
    repo = tmp_path / "story-repo"
    repo.mkdir()
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git("init", "-q", "-b", "master", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("base\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-q", "origin", "master", cwd=repo)
    _git("checkout", "-q", "-b", "agent/s1", cwd=repo)
    (repo / "g.txt").write_text("story\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "story", cwd=repo)
    return repo


def test_merge_gate_counts_a_failed_fetch_as_a_merge_attempt_without_pushing(
    plan_dir, tmp_path, sleeps, monkeypatch
):
    repo = _init_repo(tmp_path)
    real_run = subprocess.run
    git = _FakeGit([128] * 3, real_passthrough=real_run)
    pushes = []

    def merge_run(argv, *args, **kwargs):
        if argv[:2] == ["git", "push"]:
            pushes.append(list(argv))
        return real_run(argv, *args, **kwargs)

    _use_git(monkeypatch, git)
    monkeypatch.setattr(merge_mod, "subprocess", SimpleNamespace(run=merge_run))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "REPO_ROOT", repo)
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_ci_status_once", lambda *a, **k: pytest.fail("CI polled after failed fetch"))
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda wt, base: "")
    story = {
        "summary": "ready to merge",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "risk": "low",
        "worktree": str(repo),
    }
    (plan_dir / "go.manifest.json").write_text(json.dumps({"epics": {}, "stories": {"S1": story}}))

    _advance_pipeline_locked("go")

    after = json.loads((plan_dir / "go.manifest.json").read_text())["stories"]["S1"]
    assert after["merge_attempts"] == 1
    assert after["status"] == "pr_open"
    assert pushes == []
