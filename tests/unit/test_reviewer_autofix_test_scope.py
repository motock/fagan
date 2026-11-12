"""Reviewer self-fix verification honours test_scope (tsc-12).

A scoped story must run only its own scoped tests after a reviewer's
APPROVE_WITH_FIX, never the repo's full suite on the pipeline host. A full
or unresolved story must run exactly the detected command, unchanged.
"""
import subprocess

import pytest

from pipeline import server as p
from pipeline.test_scope_run import NO_TESTS_CMD

DETECTED_CMD = ["pytest", "-q"]
ACCEPTANCE_PATH = "tests/unit/test_fixture_under_review.py"


def _init_repo(path):
    p.subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    p.subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=path, check=True)
    p.subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _head_sha(path):
    return p.subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _commit_self_fix(path):
    """Create a real base commit, then a one-line reviewer self-fix on top."""
    _init_repo(path)
    (path / "a.py").write_text("x = 1\n")
    p.subprocess.run(["git", "add", "."], cwd=path, check=True)
    p.subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    before_sha = _head_sha(path)
    (path / "a.py").write_text("x = 2\n")
    p.subprocess.run(["git", "commit", "-aq", "-m", "fix"], cwd=path, check=True)
    return before_sha


def _record_test_runs(monkeypatch, returncode=0, output="ok"):
    """Stub the test subprocess only; git calls stay real. Returns the list
    of non-git commands the harness asked to run, in order."""
    real_run = p.subprocess.run
    calls = []

    def _fake_run(cmd, **kw):
        if cmd and cmd[0] == "git":
            return real_run(cmd, **kw)
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode, stdout=output, stderr="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    return calls


def _stub_detect_and_branch(monkeypatch):
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, list(DETECTED_CMD)))
    monkeypatch.setattr(p, "_default_branch", lambda: "master")


def _scoped_story(acceptance=True):
    story = {
        "key": "S1",
        "risk": "low",
        "test_scope_resolution": {"test_scope": "scoped"},
    }
    if acceptance:
        story["acceptance"] = [{"path": ACCEPTANCE_PATH, "source": "def test_x(): pass\n"}]
    return story


def _no_changed_test_files(monkeypatch):
    import pipeline.test_scope_run as tsr

    monkeypatch.setattr(tsr, "changed_test_paths", lambda worktree, base: [])


def test_full_mode_without_resolution_runs_exactly_detected_command(tmp_path, monkeypatch):
    before_sha = _commit_self_fix(tmp_path)
    _stub_detect_and_branch(monkeypatch)
    calls = _record_test_runs(monkeypatch)

    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), {"risk": "low"}, "VERDICT: APPROVE_WITH_FIX", before_sha,
    )

    assert calls == [DETECTED_CMD]
    assert verdict == "APPROVE"
    assert feedback.endswith("full test suite passed]")


@pytest.mark.parametrize("scope", ["full", "bogus", ""])
def test_non_scoped_resolution_runs_exactly_detected_command(tmp_path, monkeypatch, scope):
    before_sha = _commit_self_fix(tmp_path)
    _stub_detect_and_branch(monkeypatch)
    calls = _record_test_runs(monkeypatch)
    story = {"risk": "low", "test_scope_resolution": {"test_scope": scope}}

    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), story, "VERDICT: APPROVE_WITH_FIX", before_sha,
    )

    assert calls == [DETECTED_CMD]
    assert verdict == "APPROVE"
    assert feedback.endswith("full test suite passed]")


def test_scoped_with_acceptance_runs_detected_pytest_on_acceptance_path(tmp_path, monkeypatch):
    before_sha = _commit_self_fix(tmp_path)
    _stub_detect_and_branch(monkeypatch)
    _no_changed_test_files(monkeypatch)
    calls = _record_test_runs(monkeypatch)

    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), _scoped_story(), "VERDICT: APPROVE_WITH_FIX", before_sha,
    )

    assert calls == [[*DETECTED_CMD, str(tmp_path / ACCEPTANCE_PATH)]]
    assert verdict == "APPROVE"
    assert feedback.endswith("scoped tests passed (test_scope=scoped)]")
    assert "full test suite" not in feedback


def test_scoped_failing_scoped_tests_downgrades_to_request_changes(tmp_path, monkeypatch):
    before_sha = _commit_self_fix(tmp_path)
    _stub_detect_and_branch(monkeypatch)
    _no_changed_test_files(monkeypatch)
    _record_test_runs(monkeypatch, returncode=1, output="1 failed")

    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), _scoped_story(), "VERDICT: APPROVE_WITH_FIX", before_sha,
    )

    assert verdict == "REQUEST_CHANGES"
    assert "failed the scoped tests" in feedback
    assert "failed the full test suite" not in feedback


def test_scoped_with_nothing_in_scope_runs_no_tests_command(tmp_path, monkeypatch):
    before_sha = _commit_self_fix(tmp_path)
    _stub_detect_and_branch(monkeypatch)
    _no_changed_test_files(monkeypatch)
    calls = _record_test_runs(monkeypatch)

    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), _scoped_story(acceptance=False), "VERDICT: APPROVE_WITH_FIX", before_sha,
    )

    assert calls == [NO_TESTS_CMD]
    assert verdict == "APPROVE"
    assert "no tests in scope" in feedback
    assert "CI runs the full suite" in feedback


def test_scoped_medium_risk_rejected_before_any_subprocess(tmp_path, monkeypatch):
    before_sha = _commit_self_fix(tmp_path)
    _stub_detect_and_branch(monkeypatch)
    _no_changed_test_files(monkeypatch)
    calls = _record_test_runs(monkeypatch)
    story = _scoped_story()
    story["risk"] = "medium"

    verdict, feedback = p._verify_reviewer_auto_fix(
        str(tmp_path), story, "VERDICT: APPROVE_WITH_FIX", before_sha,
    )

    assert calls == []
    assert verdict == "REQUEST_CHANGES"
    assert "risk: low" in feedback
