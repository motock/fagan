"""Acceptance oracle: the merge-gate acceptance reverify must refuse a worktree
whose acceptance fixture no longer matches the digest recorded at dispatch.

This is the hole PR #210 merged through: _reverify_acceptance runs whatever
fixture file is in the worktree, so rewriting the grader makes the gate pass.
The refusal must happen WITHOUT running the (rewritten) tests at all.
"""
import hashlib
import subprocess
import sys
from pathlib import Path

from pipeline import ci

SOURCE = "def test_x():\n    assert True\n"
DIGEST = hashlib.sha256(SOURCE.encode()).hexdigest()
# A genuine rewrite of the grader's original assertion (not a pure append) -
# the PR #210 hole these tests guard against. A pure append is now legitimate
# for non-TDD-split stories (see test_acceptance_oracle_tamper_append.py); a
# mid-line rewrite of the original region is and remains tampering.
REWRITE = SOURCE.replace("assert True", "assert False")


def _story():
    return {
        "acceptance": [{"path": "oracle.py", "source": SOURCE}],
        "acceptance_digests": {"oracle.py": DIGEST},
    }


def test_untouched_fixture_is_not_reported(tmp_path):
    (tmp_path / "oracle.py").write_text(SOURCE)
    assert ci._acceptance_tampered(_story(), str(tmp_path)) == []


def test_modified_fixture_is_reported(tmp_path):
    (tmp_path / "oracle.py").write_text(REWRITE)
    assert ci._acceptance_tampered(_story(), str(tmp_path)) == ["oracle.py"]


def test_deleted_fixture_is_reported(tmp_path):
    assert ci._acceptance_tampered(_story(), str(tmp_path)) == ["oracle.py"]


def test_story_without_digests_reports_nothing(tmp_path):
    assert ci._acceptance_tampered({"acceptance": []}, str(tmp_path)) == []


def test_reverify_fails_a_tampered_worktree_without_running_tests(tmp_path, monkeypatch):
    (tmp_path / "oracle.py").write_text(REWRITE)

    def boom(*a, **k):
        raise AssertionError(
            "the rewritten oracle must never be executed - the gate must refuse first"
        )

    monkeypatch.setattr(ci.subprocess, "run", boom)
    result = ci._reverify_acceptance(_story(), str(tmp_path))
    assert result["state"] == "fail"
    assert "oracle.py" in result["error"]


def test_reverify_still_runs_tests_for_an_untouched_worktree(tmp_path, monkeypatch):
    (tmp_path / "oracle.py").write_text(SOURCE)
    called = {}

    class R:
        returncode = 0
        stdout = "1 passed"
        stderr = ""

    def fake_run(cmd, **kwargs):
        called["ran"] = True
        return R()

    monkeypatch.setattr(ci.subprocess, "run", fake_run)
    result = ci._reverify_acceptance(_story(), str(tmp_path))
    assert called.get("ran"), "an untouched oracle must still be executed"
    assert result["state"] == "pass"


# --- last_test_check write-back (issue 488715aa) ---------------------------
#
# The merge gate's OWN fresh re-verification must be persisted onto the story.
# Without the write-back the manifest's diagnostic last_test_check field can
# show a stale snapshot from an earlier poll (e.g. the portable no-op recorded
# before a build marker like pyproject.toml existed) even though the story
# merged on a later, real test run this gate performed and then discarded.


def _git_worktree(tmp_path):
    """A real git repo with one commit, so `git rev-parse HEAD` works."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _head(worktree):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _detect_returning(worktree, cmd):
    def _detect(_cwd):
        return Path(worktree), list(cmd)

    return _detect


def _prepare(tmp_path, monkeypatch, cmd):
    """Real git worktree + a real, controlled test command."""
    monkeypatch.delenv("PIPELINE_REVERIFY_FULL_SUITE", raising=False)
    worktree = _git_worktree(tmp_path)
    monkeypatch.setattr(ci, "detect_test_command", _detect_returning(worktree, cmd))
    return worktree


def test_reverify_persists_a_failing_run_onto_story(tmp_path, monkeypatch):
    cmd = [sys.executable, "-c", "import sys; print('1 failed'); sys.exit(1)"]
    worktree = _prepare(tmp_path, monkeypatch, cmd)
    story = {"acceptance": []}

    result = ci._reverify_acceptance(story, str(worktree))

    # Return shape is unchanged (additive side effect only).
    assert result["state"] == "fail"
    assert result["error"] == "1 failed"
    assert story["last_test_check"]["returncode"] == 1
    assert story["last_test_check"]["cmd"] == cmd
    assert story["last_test_check"]["cwd"] == str(worktree)


def test_reverify_persists_a_passing_run_onto_story(tmp_path, monkeypatch):
    cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
    worktree = _prepare(tmp_path, monkeypatch, cmd)
    story = {"acceptance": []}

    result = ci._reverify_acceptance(story, str(worktree))

    assert result == {"state": "pass", "error": ""}
    assert story["last_test_check"]["returncode"] == 0
    assert story["last_test_check"]["cmd"] == cmd


def test_reverify_persists_the_worktree_head_sha(tmp_path, monkeypatch):
    cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
    worktree = _prepare(tmp_path, monkeypatch, cmd)
    story = {"acceptance": []}

    ci._reverify_acceptance(story, str(worktree))

    assert story["last_test_check"]["sha"] == _head(worktree)
    assert story["last_test_check"]["sha"]


def test_reverify_overwrites_a_stale_noop_snapshot(tmp_path, monkeypatch):
    """Live incident: the manifest kept the portable no-op from an earlier poll
    even though the gate went on to run (and pass on) a real test command."""
    cmd = [sys.executable, "-c", "import sys; sys.exit(1)"]
    worktree = _prepare(tmp_path, monkeypatch, cmd)
    (worktree / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    stale = {
        "cmd": [sys.executable, "-c", "pass"],
        "cwd": "/tmp/wt",
        "returncode": 0,
        "stdout_tail": "",
        "stderr_tail": "",
        "ts": "2024-01-01T00:00:00+00:00",
        "sha": "deadbeef",
    }
    story = {"acceptance": [], "last_test_check": dict(stale)}

    result = ci._reverify_acceptance(story, str(worktree))

    assert result["state"] == "fail"
    assert story["last_test_check"]["cmd"] == cmd
    assert story["last_test_check"]["cmd"] != stale["cmd"]
    assert story["last_test_check"]["returncode"] == 1
    assert story["last_test_check"]["sha"] == _head(worktree)
    assert story["last_test_check"]["sha"] != "deadbeef"

