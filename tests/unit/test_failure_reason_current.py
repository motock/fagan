"""The failure_reason on a failed grade names why THIS attempt failed.

Measured 2026-11-22: attempt 1 parked with the empty-branch text; attempt 2
committed real work, its test command exited 127, and the story still showed
attempt 1's text. A grade that fails on the test command must say what this
run exited with, a grade that passes must drop any leftover reason, and an
empty branch whose agent could not authenticate says so with a fixed sentence.

The test command and git are faked the way the existing story-status tests
fake them; the only file written is the agent log the story points at.
"""

import pytest

from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (
    _read_manifest,
    _write_manifest,
)

STALE_REASON = (
    "tests passed but agent branch has no new commits vs main; "
    "agent likely parked without writing code."
)
EMPTY_BRANCH_REASON = STALE_REASON
AUTH_SENTENCE = " The agent could not authenticate to its model provider."
FAKE_SECRET = "sk-FAKE-SECRET-0123456789abcdef"


class _Result:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    return d


def _fake_test_run(monkeypatch, returncode):
    """Only the test command gets `returncode`; git calls always succeed."""

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "git":
            return _Result(0)
        return _Result(returncode, stdout="", stderr="")

    monkeypatch.setattr(p.subprocess, "run", fake_run)


def _story(worktree, **extra):
    base = {"summary": "thing", "status": "in_progress",
            "pid": 4242, "worktree": str(worktree)}
    base.update(extra)
    return base


def _grade(plan_dir, plan, story, monkeypatch, *, returncode, has_commits):
    worktree = plan_dir / f"wt-{plan}"
    worktree.mkdir(exist_ok=True)
    _write_manifest(plan_dir, plan, {"S1": _story(worktree, **story)})
    _fake_test_run(monkeypatch, returncode)
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: has_commits)
    result = p.check_story_status(plan, "S1")
    return result, _read_manifest(plan_dir, plan)["stories"]["S1"]


def test_failed_test_run_replaces_stale_reason_with_exit_code(plan_dir, monkeypatch):
    result, story = _grade(
        plan_dir, "tf", {"failure_reason": STALE_REASON}, monkeypatch,
        returncode=1, has_commits=True,
    )
    assert result["status"] == "failed"
    assert story["status"] == "failed"
    assert story["failure_reason"] == "tests failed (exit 1)"


def test_exit_127_gives_the_command_not_found_reason(plan_dir, monkeypatch):
    _, story = _grade(
        plan_dir, "nf", {"failure_reason": STALE_REASON}, monkeypatch,
        returncode=127, has_commits=True,
    )
    assert story["status"] == "failed"
    assert story["failure_reason"] == (
        "test command not found on the pipeline host (exit 127)"
    )


def test_failed_test_run_never_copies_test_output_into_reason(plan_dir, monkeypatch):
    worktree = plan_dir / "wt-out"
    worktree.mkdir()
    _write_manifest(plan_dir, "out", {"S1": _story(worktree)})

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "git":
            return _Result(0)
        return _Result(1, stdout=FAKE_SECRET, stderr=FAKE_SECRET)

    monkeypatch.setattr(p.subprocess, "run", fake_run)
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    p.check_story_status("out", "S1")
    story = _read_manifest(plan_dir, "out")["stories"]["S1"]
    assert story["failure_reason"] == "tests failed (exit 1)"
    assert FAKE_SECRET not in story["failure_reason"]


def test_passing_grade_after_a_failure_removes_the_reason(plan_dir, monkeypatch):
    result, story = _grade(
        plan_dir, "ok", {"failure_reason": "tests failed (exit 1)"}, monkeypatch,
        returncode=0, has_commits=True,
    )
    assert result["status"] == "tests_passed"
    assert story["status"] == "tests_passed"
    assert "failure_reason" not in story


def test_empty_branch_without_log_keeps_todays_text_exactly(plan_dir, monkeypatch):
    result, story = _grade(
        plan_dir, "nolog", {}, monkeypatch, returncode=0, has_commits=False,
    )
    assert result["reason"] == "empty_agent_branch"
    assert story["failure_reason"] == EMPTY_BRANCH_REASON


@pytest.mark.parametrize(
    "marker",
    [
        "Not logged in",
        "401 Unauthorized",
        "Invalid API key",
        "authentication_error",
        "NOT LOGGED IN",
    ],
)
def test_empty_branch_with_auth_marker_adds_fixed_sentence(plan_dir, monkeypatch, marker):
    log = plan_dir / "agent.log"
    log.write_text(f"[step 0] bash: pwd\nerror: {marker}\n")
    _, story = _grade(
        plan_dir, "auth", {"log": str(log)}, monkeypatch,
        returncode=0, has_commits=False,
    )
    assert story["failure_reason"] == EMPTY_BRANCH_REASON + AUTH_SENTENCE


def test_auth_hint_never_copies_log_text_into_reason(plan_dir, monkeypatch):
    log = plan_dir / "agent.log"
    log.write_text(f"Not logged in\nLLM call failed: token={FAKE_SECRET}\n")
    _, story = _grade(
        plan_dir, "leak", {"log": str(log)}, monkeypatch,
        returncode=0, has_commits=False,
    )
    assert story["failure_reason"] == EMPTY_BRANCH_REASON + AUTH_SENTENCE
    assert FAKE_SECRET not in story["failure_reason"]


def test_auth_marker_beyond_the_last_4000_bytes_adds_nothing(plan_dir, monkeypatch):
    log = plan_dir / "agent.log"
    log.write_text("Not logged in\n" + ("x" * 5000))
    _, story = _grade(
        plan_dir, "big", {"log": str(log)}, monkeypatch,
        returncode=0, has_commits=False,
    )
    assert story["failure_reason"] == EMPTY_BRANCH_REASON


def test_unreadable_log_adds_nothing_and_does_not_raise(plan_dir, monkeypatch):
    unreadable = plan_dir / "is-a-directory"
    unreadable.mkdir()
    _, story = _grade(
        plan_dir, "unread", {"log": str(unreadable)}, monkeypatch,
        returncode=0, has_commits=False,
    )
    assert story["failure_reason"] == EMPTY_BRANCH_REASON
