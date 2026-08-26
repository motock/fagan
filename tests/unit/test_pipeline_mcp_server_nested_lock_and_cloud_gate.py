"""Tests for the pipeline MCP server: the advance_pipeline nested _plan_lock regression and the cloud-aware per-story dispatch gate.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import json
import subprocess

from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
    plan_dir,
)

# ---------- give-up classification (T6) ----------
# The WASM prekey/session story's second attempt (gpt-oss:20b, after being
# split into a smaller story) called `done` with "I'm sorry, I can't
# complete this task" after real research and zero commits (2026-07-07
# web-client-epic retro §3.2). local_agent.py's `done` tool prints its
# summary verbatim as "[step N] DONE: <summary>" - that's the concrete,
# real signal these tests key on, not a fictitious exit protocol.

def test_last_done_summary_extracts_final_done_line(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text(
        "[step 1] bash: ls\n"
        "[step 2] DONE: implemented the feature, tests pass\n"
    )
    assert p._last_done_summary(log) == "implemented the feature, tests pass"


def test_last_done_summary_uses_last_done_line_not_first(tmp_path):
    # A resumed agent appends to the same log across ticks; only the LAST
    # DONE line reflects the current run (mirrors _last_nonempty_line's
    # resumed-log caution for STEP_CAP_MARKERS).
    log = tmp_path / "agent.log"
    log.write_text(
        "[step 2] DONE: first attempt summary\n"
        "=== resumed ===\n"
        "[step 5] DONE: second attempt summary\n"
    )
    assert p._last_done_summary(log) == "second attempt summary"


def test_last_done_summary_empty_when_no_done_line(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("[step 1] bash: ls\n[ended without done — step cap reached]\n")
    assert p._last_done_summary(log) == ""


def test_last_done_summary_empty_when_log_missing(tmp_path):
    assert p._last_done_summary(tmp_path / "no-such-log.log") == ""


def test_is_give_up_summary_matches_explicit_surrender():
    assert p._is_give_up_summary("I'm sorry, I can't complete this task.") is True


def test_is_give_up_summary_is_case_insensitive():
    assert p._is_give_up_summary("I CANNOT COMPLETE THIS TASK after research") is True


def test_is_give_up_summary_does_not_match_genuine_completion():
    assert p._is_give_up_summary("implemented the feature, all tests pass") is False


def test_check_story_status_marks_failure_kind_give_up(plan_dir, monkeypatch):
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 1] bash: grep -r PreKeyBundle .\n"
        "[step 9] DONE: I'm sorry, I can't complete this task.\n"
    )
    _write_manifest(plan_dir, "giveup1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="1 failed"))

    result = p.check_story_status("giveup1", "S1")

    assert result["status"] == "failed"
    manifest = _read_manifest(plan_dir, "giveup1")
    assert manifest["stories"]["S1"]["failure_kind"] == "give_up"


def test_check_story_status_ordinary_failure_has_no_failure_kind(plan_dir, monkeypatch):
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 5] DONE: implemented the feature per the spec\n"
    )
    _write_manifest(plan_dir, "giveup2", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="1 failed"))

    result = p.check_story_status("giveup2", "S1")

    assert result["status"] == "failed"
    manifest = _read_manifest(plan_dir, "giveup2")
    assert "failure_kind" not in manifest["stories"]["S1"]


def test_check_story_status_passes_when_agent_committed_changes(
    plan_dir, monkeypatch,
):
    """Positive case for the empty-branch guard: tests pass AND the
    agent wrote real commits -> status `tests_passed`."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "ok", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = ""
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("ok", "S1")
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "ok")
    assert manifest["stories"]["S1"]["status"] == "tests_passed"
    assert "failure_reason" not in manifest["stories"]["S1"]


def test_check_story_status_records_last_test_check_on_pass(plan_dir, monkeypatch):
    """Diagnostic gap found live 2026-07-22 (MODE-29-REVIEW-STORY-LOCK-GUARD):
    check_story_status's test-run result (command, cwd, returncode, output)
    was only ever returned transiently from the tool call - nothing persisted
    it to the manifest, so a status that later turned out to be wrong
    (tests_passed recorded when the same command deterministically fails when
    re-run by hand) was impossible to diagnose after the fact. Persist it on
    the story as `last_test_check` every time a test run determines status,
    regardless of pass/fail, so a future occurrence has a paper trail."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "diag1", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["pytest", "-q"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, stdout="3 passed", stderr="",
        ),
    )

    result = p.check_story_status("diag1", "S1")
    assert result["status"] == "tests_passed"

    manifest = _read_manifest(plan_dir, "diag1")
    check = manifest["stories"]["S1"]["last_test_check"]
    assert check["cmd"] == ["pytest", "-q"]
    assert check["cwd"] == str(worktree)
    assert check["returncode"] == 0
    assert "3 passed" in check["stdout_tail"]
    assert "ts" in check


def test_check_story_status_gate_appends_own_new_tests_under_tests_dir(
    plan_dir, monkeypatch,
):
    """Mode 42 done-bar blindspot: a no-acceptance story whose deliverable
    lives under tests/ (e.g. tests/benchmark/run_real_repo_task.py) can add
    its own tests/test_*.py file there, but detect_test_command's
    --ignore=tests then hides that file from THIS SAME gate run - so a
    broken implementation can pass its own (never-executed) test and land
    tests_passed. check_story_status must pass the story's own new/modified
    tests/test_*.py paths explicitly so they actually run (see
    _added_pytest_test_paths)."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "diag2", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (wt, ["pytest", "--ignore=tests"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(
        p, "_added_pytest_test_paths",
        lambda wt, key, base: ["tests/benchmark/test_driver.py"]
        if key == "S1" and base == "main" else [],
    )
    # Capture EVERY subprocess.run call, not just the last: the dead-code
    # gate (which runs after tests pass) also shells out to git, so "the
    # last call" is no longer reliably the test command. The test command
    # is always the first call in check_story_status's flow.
    seen_calls = []
    def _fake_run(cmd, **kwargs):
        seen_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="4 passed", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("diag2", "S1")

    assert result["status"] == "tests_passed"
    assert seen_calls[0] == [
        "pytest", "--ignore=tests", str(worktree / "tests/benchmark/test_driver.py")]


def test_check_story_status_gate_skips_own_test_append_with_acceptance_block(
    plan_dir, monkeypatch,
):
    """A story WITH an acceptance block stays scoped to the harness-owned
    oracle only (FM-A) - the own-new-tests augmentation must not fire, since
    that would grade the story on the model's own (possibly buggy)
    assertions instead of the oracle."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "diag3", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree),
               "acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}]},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (wt, ["pytest", "--ignore=tests"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(
        p, "_added_pytest_test_paths",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not be called when acceptance block is present")),
    )
    # Capture EVERY subprocess.run call, not just the last: the dead-code
    # gate (which runs after tests pass) also shells out to git, so "the
    # last call" is no longer reliably the test command. The test command
    # is always the first call in check_story_status's flow.
    seen_calls = []
    def _fake_run(cmd, **kwargs):
        seen_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="1 passed", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)

    result = p.check_story_status("diag3", "S1")

    assert result["status"] == "tests_passed"
    assert seen_calls[0] == [
        "pytest", "--ignore=tests", str(worktree / "test_acceptance.py")]


def test_check_story_status_records_last_test_check_on_fail_without_stderr_attr(
    plan_dir, monkeypatch,
):
    """Same as above, but on the failure path, and with a test double that
    doesn't define .stderr at all (mirrors this file's own `Result` stub
    class used elsewhere) - the diagnostic capture must not crash when the
    subprocess result lacks a stderr attribute."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "diag2", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = "1 failed"
        returncode = 1

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("diag2", "S1")
    assert result["status"] == "failed"

    manifest = _read_manifest(plan_dir, "diag2")
    check = manifest["stories"]["S1"]["last_test_check"]
    assert check["returncode"] == 1
    assert "1 failed" in check["stdout_tail"]
    assert check["stderr_tail"] == ""


def test_check_story_status_handles_git_error_safely(plan_dir, monkeypatch):
    """If `_worktree_has_new_commits` returns False (covers the
    `git log` failure case — broken worktree, missing branch, any git
    hiccup), the gate fires: status `failed`, reason
    `empty_agent_branch`. We never crash the orchestrator on a git
    error, and we never accidentally pass a story because git was
    broken."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("ok\n")
    _write_manifest(plan_dir, "broken", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    # Simulate git log returning non-zero (helper returns False).
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: False)

    class Result:
        stdout = ""
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    # Must not raise. Must mark failed, not tests_passed.
    result = p.check_story_status("broken", "S1")
    assert result["status"] == "failed"
    assert result["reason"] == "empty_agent_branch"


def _css_setup(plan_dir, monkeypatch, *, last_reviewed_sha=None,
               head_sha=None, rework_attempts=0, acceptance=False):
    """Shared scaffolding for the Mode 27 no-new-commit guard tests.

    Builds a worktree + manifest, mocks the pid dead (so check_story_status
    runs the tests), mocks test detection + new-commits guard, and routes
    subprocess.run so `git rev-parse HEAD` returns `head_sha` while every
    other call (the test command) succeeds with returncode 0.
    """
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("ok\n")
    story = {"summary": "thing", "status": "in_progress", "pid": 4242,
            "worktree": str(worktree), "rework_attempts": rework_attempts}
    if acceptance:
        story["acceptance"] = [{"path": "t.py", "source": ""}]
    if last_reviewed_sha is not None:
        story["last_reviewed_sha"] = last_reviewed_sha
    _write_manifest(plan_dir, "plan", {"S1": story})
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def run_mock(*args, **kwargs):
        if "rev-parse" in args[0]:
            return Result(stdout=(head_sha or "") + "\n")
        return Result(returncode=0)

    monkeypatch.setattr(p.subprocess, "run", run_mock)


def test_check_story_status_no_new_commit_since_last_review_routes_to_changes_requested(
    plan_dir, monkeypatch,
):
    """Mode 27: tests pass but HEAD is unchanged since the last
    REQUEST_CHANGES — route to changes_requested (dispatch-eligible) so the
    scheduler redispatches, instead of stalling at tests_passed where Mode
    24's same-SHA skip guard would loop forever. The no-progress retry
    counts against the rework cap."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha="abc123",
               head_sha="abc123", rework_attempts=0)
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "changes_requested"
    assert result["reason"] == "no_new_commit_since_last_review"
    manifest = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert manifest["status"] == "changes_requested"
    assert manifest["rework_attempts"] == 1


def test_check_story_status_new_commit_after_last_review_passes(plan_dir, monkeypatch):
    """Mode 27: when the rework DID produce a new commit (HEAD advanced past
    last_reviewed_sha), fall through to tests_passed so review_story runs on
    the new SHA — the guard must not fire on legitimate progress."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha="abc123",
               head_sha="def456", rework_attempts=1)
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert manifest["status"] == "tests_passed"
    # rework_attempts untouched on the progress path.
    assert manifest["rework_attempts"] == 1


def test_check_story_status_no_last_reviewed_sha_passes(plan_dir, monkeypatch):
    """Mode 27: the guard only applies when a prior REQUEST_CHANGES recorded
    a last_reviewed_sha. A first-run story with no prior review falls
    through to tests_passed unchanged."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha=None, head_sha="any")
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert manifest["status"] == "tests_passed"


def test_check_story_status_no_progress_exhausts_rework_cap_parks(plan_dir, monkeypatch):
    """Mode 27: a stuck agent that keeps producing no new commit must park
    once rework_attempts reaches REWORK_MAX_ATTEMPTS_NO_COMMIT (cap 2).
    rework_attempts starts at 2: one no-progress retry brings attempts to 3,
    which is past the cap, so it parks."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha="abc123",
               head_sha="abc123", rework_attempts=2)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "parked"
    assert result["reason"] == "no_new_commit_rework_budget_exhausted"
    manifest = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert manifest["status"] == "parked"
    assert manifest["rework_attempts"] == 3
    assert "no new commit after 3" in manifest["parked_reason"]


def test_check_story_status_no_progress_cap_is_tighter_than_general_rework_cap(
    plan_dir, monkeypatch,
):
    """REWORK_MAX_ATTEMPTS_NO_COMMIT (2) is intentionally tighter than the
    general REWORK_MAX_ATTEMPTS (3): a story that produced zero commits
    across its FIRST rework cycle (rework_attempts=1 going into this call,
    so attempts becomes 2) must already park here, whereas the general cap
    would have allowed one more cycle. Root-caused 2026-08-20 on
    W2_CHAT_ENTRY_POINT_PLAN (W2-03, W2-05): two stories spent their full
    general-cap budget on this exact zero-commit path with nothing to show
    for the extra cycle."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha="abc123",
               head_sha="abc123", rework_attempts=1)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "parked"
    assert result["reason"] == "no_new_commit_rework_budget_exhausted"


def test_check_story_status_no_progress_cap_uniform_for_oracle_and_escalated_stories(
    plan_dir, monkeypatch,
):
    """The no-new-commit cap applies uniformly regardless of the story's
    acceptance-oracle or escalation status - unlike the general rework cap
    (REWORK_MAX_ATTEMPTS_ORACLE=1, REWORK_MAX_ATTEMPTS_ESCALATED=3), zero
    commits is the same severity of stall signal either way."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha="abc123",
               head_sha="abc123", rework_attempts=1, acceptance=True)
    manifest = _read_manifest(plan_dir, "plan")
    manifest["stories"]["S1"]["escalated"] = True
    manifest_path = plan_dir / "plan.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "parked"
    assert result["reason"] == "no_new_commit_rework_budget_exhausted"


def test_check_story_status_no_progress_exhausts_rework_cap_escalates_to_claude(
    plan_dir, monkeypatch,
):
    """Root-caused live 2026-07-24 (RUFF-016-ADOPTION, MODE40-CI-REWORK-
    FEEDBACK-V2): review_story's three park paths all escalate to Claude
    under PIPELINE_BACKEND_DISPATCH=auto before parking for a human - this
    was the one rework-exhaustion park path in the file missing that hook,
    so a story that hit exactly this "no new commit" guard never got a
    chance at Claude even with auto-escalation enabled. Same cap/inputs as
    test_check_story_status_no_progress_exhausts_rework_cap_parks, but with
    auto-escalation on: must escalate (backend -> claude, escalated=True,
    status -> changes_requested for redispatch) instead of parking."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha="abc123",
               head_sha="abc123", rework_attempts=2)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: True)
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "changes_requested"
    assert result["reason"] == "no_new_commit_escalated_to_claude"
    manifest = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert manifest["status"] == "changes_requested"
    assert manifest["backend"] == "claude"
    assert manifest["escalated"] is True
    # A fresh rework budget for Claude - _escalate_review_to_claude clears
    # the counter, same as its other two call sites.
    assert "rework_attempts" not in manifest
    # A story that has ALREADY been escalated must terminally park on a
    # second rework-cap exhaustion, not escalate again or loop forever -
    # there is no further fallback past Claude.


def test_check_story_status_no_progress_already_escalated_parks_not_loops(
    plan_dir, monkeypatch,
):
    """The escalated=True guard: a story already on Claude that STILL hits
    the no-new-commit rework cap a second time must park for a human, not
    re-escalate (there's nothing past Claude to fall back to)."""
    _css_setup(plan_dir, monkeypatch, last_reviewed_sha="abc123",
               head_sha="abc123", rework_attempts=2)
    manifest = _read_manifest(plan_dir, "plan")
    manifest["stories"]["S1"]["escalated"] = True
    manifest["stories"]["S1"]["backend"] = "claude"
    manifest_path = plan_dir / "plan.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: True)
    result = p.check_story_status("plan", "S1")
    assert result["status"] == "parked"
    assert result["reason"] == "no_new_commit_rework_budget_exhausted"
    manifest = _read_manifest(plan_dir, "plan")["stories"]["S1"]
    assert manifest["status"] == "parked"
    assert manifest["backend"] == "claude"


def test_check_story_status_routes_acceptance_fail_to_review_when_opted_in(
    plan_dir, monkeypatch,
):
    """PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1: a dispatch whose acceptance oracle
    FAILED but which produced real work (new commits on the agent branch) is
    routed to review instead of straight to "failed", so the reviewer sees the
    failing submission and the rework loop re-dispatches the model. Without
    this routing every acceptance-failing cell parked at "failed" before
    reaching review, so the configured rework budget and reviewer never ran
    (observed live: 0/9 mlx cells reached review, zero GLM reviewer usage)."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent did real work\n")
    _write_manifest(plan_dir, "revfail", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class _Fail:
        returncode = 1   # acceptance oracle FAILED
        stdout = "1 failed"
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _Fail())

    monkeypatch.setenv("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "1")

    result = p.check_story_status("revfail", "S1")

    # Routed to reviewable state, NOT terminal "failed".
    assert result["status"] == "tests_passed"
    assert result["tests_passed"] is False
    story = _read_manifest(plan_dir, "revfail")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert story["acceptance_failed_review"] is True


def test_check_story_status_acceptance_fail_stays_failed_without_opt_in(
    plan_dir, monkeypatch,
):
    """Without PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL the default behavior is
    unchanged: a failing-acceptance dispatch with real work lands at terminal
    "failed" (no review, no rework)."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent did real work\n")
    _write_manifest(plan_dir, "nofail", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class _Fail:
        returncode = 1
        stdout = "1 failed"
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _Fail())

    monkeypatch.delenv("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", raising=False)

    result = p.check_story_status("nofail", "S1")

    assert result["status"] == "failed"
    story = _read_manifest(plan_dir, "nofail")["stories"]["S1"]
    assert story["status"] == "failed"
    assert "acceptance_failed_review" not in story


def test_check_story_status_acceptance_fail_stays_failed_for_empty_branch(
    plan_dir, monkeypatch,
):
    """Even with the opt-in set, a failing-acceptance dispatch with NO new
    commits (agent parked without writing code) stays "failed" — re-dispatching
    the same stuck prompt to the same model won't help, so it is not worth a
    review round-trip."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent looped without writing\n")
    _write_manifest(plan_dir, "emptyfail", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: False)

    class _Fail:
        returncode = 1
        stdout = "1 failed"
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _Fail())

    monkeypatch.setenv("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "1")

    result = p.check_story_status("emptyfail", "S1")

    assert result["status"] == "failed"
    story = _read_manifest(plan_dir, "emptyfail")["stories"]["S1"]
    assert story["status"] == "failed"
    assert "acceptance_failed_review" not in story


def test_check_story_status_acceptance_fail_review_no_new_commit_routes_to_changes_requested(
    plan_dir, monkeypatch,
):
    """Mode 27 twin: the acceptance-fail-review opt-in (PIPELINE_REVIEW_ON_
    ACCEPTANCE_FAIL=1) routes a failing-tests-but-real-work dispatch to
    tests_passed/acceptance_failed_review — but if HEAD is unchanged since
    the last REQUEST_CHANGES (the rework redispatch crashed, e.g. on an LLM
    transport error, before writing any fix), review_story's same-SHA skip
    guard would silently decline to re-review forever, stranding the story
    at tests_passed (not dispatch-eligible). Observed live 2026-07-20: 14+
    consecutive silent skip-notifications on one story. The original Mode 27
    guard only checked `passed` (True) before this opt-in branch existed as
    a second way to reach tests_passed; it must also cover this path."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent did real work but crashed\n")
    _write_manifest(plan_dir, "revfail_stuck", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "rework_attempts": 0,
               "last_reviewed_sha": "34a3f38"},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class _Fail:
        returncode = 1
        stdout = "1 failed"

    class _RevParse:
        returncode = 0
        stdout = "34a3f38\n"

    def run_mock(*args, **kwargs):
        if "rev-parse" in args[0]:
            return _RevParse()
        return _Fail()
    monkeypatch.setattr(p.subprocess, "run", run_mock)

    monkeypatch.setenv("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "1")

    result = p.check_story_status("revfail_stuck", "S1")

    assert result["status"] == "changes_requested"
    assert result["reason"] == "no_new_commit_since_last_review"
    story = _read_manifest(plan_dir, "revfail_stuck")["stories"]["S1"]
    assert story["status"] == "changes_requested"
    assert story["rework_attempts"] == 1


def test_check_story_status_acceptance_fail_review_new_commit_still_passes(
    plan_dir, monkeypatch,
):
    """Sibling regression guard: when the acceptance-fail-review opt-in
    fires AND HEAD legitimately advanced past last_reviewed_sha, the guard
    above must not fire — the story reaches tests_passed/
    acceptance_failed_review as before so review_story evaluates the new
    commit."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent did real work\n")
    _write_manifest(plan_dir, "revfail_progress", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "rework_attempts": 1,
               "last_reviewed_sha": "34a3f38"},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class _Fail:
        returncode = 1
        stdout = "1 failed"

    class _RevParse:
        returncode = 0
        stdout = "def456\n"

    def run_mock(*args, **kwargs):
        if "rev-parse" in args[0]:
            return _RevParse()
        return _Fail()
    monkeypatch.setattr(p.subprocess, "run", run_mock)

    monkeypatch.setenv("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "1")

    result = p.check_story_status("revfail_progress", "S1")

    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "revfail_progress")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert story["acceptance_failed_review"] is True
    assert story["rework_attempts"] == 1


