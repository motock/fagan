"""Tests for the pipeline MCP server: escalation retarget, local-first routing, and the unwinnable-as-scoped safety override.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import fcntl
import os

from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _RATE_LIMIT_MSG,
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
    agents_dir,
    plan_dir,
)

# ---------- Non-rate-limited UNKNOWN must not burn the rework budget ----------

def test_review_story_unknown_leaves_rework_and_feedback_untouched(plan_dir, agents_dir, monkeypatch):
    # A genuinely inconclusive (non-rate-limited) UNKNOWN verdict must not be
    # treated like REQUEST_CHANGES: no rework_attempts, no review_feedback
    # (which would otherwise redispatch the agent blind on empty feedback),
    # and no changes_requested status.
    _write_manifest(plan_dir, "unk_untouched", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "no verdict line here")
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR on UNKNOWN")))

    result = p.review_story("unk_untouched", "S1")

    assert result["verdict"] == "UNKNOWN"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "unk_untouched")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert "rework_attempts" not in story
    assert "review_feedback" not in story
    assert story["review_inconclusive_count"] == 1


def test_review_story_unknown_notifies_user_will_retry(plan_dir, agents_dir, monkeypatch):
    _write_manifest(plan_dir, "unk_notify", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "no verdict line here")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    p.review_story("unk_notify", "S1")

    assert any("inconclusive" in n.lower() for n in notes)


def test_review_story_unknown_parks_after_max_inconclusive_attempts(plan_dir, agents_dir, monkeypatch):
    # Default max is 2: a second consecutive UNKNOWN must park the story for
    # human review rather than retrying forever - and must never reach
    # APPROVE/pr_open. review_verdict stays UNKNOWN throughout.
    _write_manifest(plan_dir, "unk_park", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    pr_calls = []
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "no verdict line here")
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: pr_calls.append(1))
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result1 = p.review_story("unk_park", "S1")
    assert result1["status"] == "tests_passed"

    result2 = p.review_story("unk_park", "S1")

    assert result2["verdict"] == "UNKNOWN"
    assert result2["status"] == "parked"
    story = _read_manifest(plan_dir, "unk_park")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["review_verdict"] == "UNKNOWN"
    assert story["review_inconclusive_count"] == 2
    assert "inconclusive after 2 attempts" in story["parked_reason"]
    assert pr_calls == [], "an UNKNOWN verdict must never open a PR"
    assert any("parked" in n.lower() for n in notes)
    assert story["last_inconclusive_output_excerpt"] == "no verdict line here"


def test_review_story_unknown_park_excerpt_notes_empty_response(plan_dir, agents_dir, monkeypatch):
    # When the reviewer returns a genuinely empty string (e.g. a swallowed
    # backend exception at the generic except-Exception fallback), the park
    # excerpt must say so explicitly rather than persisting an empty string
    # a human investigator could mistake for "field wasn't set".
    _write_manifest(plan_dir, "unk_park_empty", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "")
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no PR on UNKNOWN")))
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: None)

    p.review_story("unk_park_empty", "S1")
    p.review_story("unk_park_empty", "S1")

    story = _read_manifest(plan_dir, "unk_park_empty")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["last_inconclusive_output_excerpt"] == "(empty response)"


def test_review_story_conclusive_verdict_after_unknown_resets_and_reworks(plan_dir, agents_dir, monkeypatch):
    # A real REQUEST_CHANGES following a prior UNKNOWN must carry the actual
    # feedback, start rework_attempts fresh from 0 (the UNKNOWN must not have
    # silently pre-incremented it), and clear the inconclusive counter.
    _write_manifest(plan_dir, "unk_then_real", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    calls = []

    def _stub(wt, br, **k):
        calls.append(1)
        if len(calls) == 1:
            return "no verdict line here"
        return "The error path is untested.\nVERDICT: REQUEST_CHANGES"

    monkeypatch.setattr(p, "_run_reviewer", _stub)

    result1 = p.review_story("unk_then_real", "S1")
    assert result1["verdict"] == "UNKNOWN"
    story = _read_manifest(plan_dir, "unk_then_real")["stories"]["S1"]
    assert story["review_inconclusive_count"] == 1

    result2 = p.review_story("unk_then_real", "S1")

    assert result2["verdict"] == "REQUEST_CHANGES"
    assert result2["status"] == "changes_requested"
    story = _read_manifest(plan_dir, "unk_then_real")["stories"]["S1"]
    assert story["review_feedback"] == "The error path is untested.\nVERDICT: REQUEST_CHANGES"
    assert story["rework_attempts"] == 1
    assert story["review_inconclusive_count"] == 0


def test_review_story_unknown_rate_limited_still_defers_not_inconclusive(plan_dir, agents_dir, monkeypatch):
    # Regression: a rate-limited UNKNOWN must keep taking the existing FM-B
    # deferral path, not the new inconclusive-retry path - it must not
    # increment review_inconclusive_count at all.
    _write_manifest(plan_dir, "unk_rl_regression", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: _RATE_LIMIT_MSG)
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR")))

    result = p.review_story("unk_rl_regression", "S1")

    assert result.get("deferred") == "rate_limited"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "unk_rl_regression")["stories"]["S1"]
    assert "review_inconclusive_count" not in story
    assert "rework_attempts" not in story


def test_review_story_unknown_inconclusive_max_one_parks_on_first_attempt(plan_dir, agents_dir, monkeypatch):
    # Boundary: PIPELINE_REVIEW_INCONCLUSIVE_MAX=1 parks on the very first
    # inconclusive verdict rather than waiting for a second.
    monkeypatch.setattr(p, "REVIEW_INCONCLUSIVE_MAX", 1)
    _write_manifest(plan_dir, "unk_max_one", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "no verdict line here")
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR on UNKNOWN")))

    result = p.review_story("unk_max_one", "S1")

    assert result["status"] == "parked"
    story = _read_manifest(plan_dir, "unk_max_one")["stories"]["S1"]
    assert story["status"] == "parked"
    assert story["review_inconclusive_count"] == 1


# ---------- Escalate-to-Claude on rework/inconclusive exhaustion (auto mode) ----------
#
# Turning the review-side loop 100% autonomous means the two remaining
# park-for-a-human paths (rework budget exhausted, review inconclusive
# exhausted) need a fallback too - PIPELINE_BACKEND_DISPATCH=auto already
# escalates a failed DISPATCH to Claude; these tests extend the same
# philosophy to a local reviewer that can't converge. Unlike the dispatch
# escalation, this does NOT wipe the worktree/branch - the existing code is
# very often already correct (this session's benchmark runs showed most of
# these parks hold ground-truth-correct implementations a local reviewer
# just couldn't cleanly resolve), so Claude reviews/reworks the SAME
# worktree in place rather than starting over.

def test_review_story_rework_exhausted_escalates_to_claude_under_auto(
    plan_dir, agents_dir, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rvesc", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "local", "rework_attempts": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: "still bad\nVERDICT: REQUEST_CHANGES")

    result = p.review_story("rvesc", "S1")

    story = _read_manifest(plan_dir, "rvesc")["stories"]["S1"]
    assert story["status"] != "parked"
    assert story["status"] == "changes_requested"
    assert story["backend"] == "claude"
    assert story["escalated"] is True
    # Fresh budget for Claude - the local count must not carry over and
    # silently exhaust immediately on the very next cycle.
    assert "rework_attempts" not in story
    assert result["status"] == "changes_requested"


def test_review_story_rework_exhausted_parks_when_already_escalated(
    plan_dir, agents_dir, monkeypatch,
):
    """Regression/terminal guard: a story already escalated (i.e. Claude
    itself is now failing to satisfy review) must park for real - there is
    no further fallback past Claude, so this must not loop forever."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rvescdone", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "claude", "escalated": True, "rework_attempts": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, backend_name=None, **k: "still bad\nVERDICT: REQUEST_CHANGES")

    result = p.review_story("rvescdone", "S1")

    story = _read_manifest(plan_dir, "rvescdone")["stories"]["S1"]
    assert story["status"] == "parked"
    assert result["status"] == "parked"


def test_review_story_rework_exhausted_parks_when_auto_disabled(
    plan_dir, agents_dir, monkeypatch,
):
    """Regression guard: without PIPELINE_BACKEND_DISPATCH=auto, behavior is
    unchanged from before this story - park for a human, no escalation."""
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rvnoauto", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "local", "rework_attempts": 2},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, **k: "still bad\nVERDICT: REQUEST_CHANGES")

    result = p.review_story("rvnoauto", "S1")

    story = _read_manifest(plan_dir, "rvnoauto")["stories"]["S1"]
    assert story["status"] == "parked"
    assert "escalated" not in story
    assert result["status"] == "parked"


def test_review_story_inconclusive_exhausted_escalates_to_claude_under_auto(
    plan_dir, agents_dir, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p, "REVIEW_INCONCLUSIVE_MAX", 2)
    _write_manifest(plan_dir, "unkesc", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "local", "review_inconclusive_count": 1},
    })
    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "no verdict line here")

    result = p.review_story("unkesc", "S1")

    story = _read_manifest(plan_dir, "unkesc")["stories"]["S1"]
    assert story["status"] != "parked"
    assert story["status"] == "tests_passed"
    assert story["backend"] == "claude"
    assert story["escalated"] is True
    assert "review_inconclusive_count" not in story
    assert result["status"] == "tests_passed"


def test_review_story_inconclusive_exhausted_parks_when_already_escalated(
    plan_dir, agents_dir, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(p, "REVIEW_INCONCLUSIVE_MAX", 2)
    _write_manifest(plan_dir, "unkescdone", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "claude", "escalated": True,
               "review_inconclusive_count": 1},
    })
    monkeypatch.setattr(p, "_run_reviewer",
                        lambda wt, br, backend_name=None, **k: "no verdict line here")

    result = p.review_story("unkescdone", "S1")

    story = _read_manifest(plan_dir, "unkescdone")["stories"]["S1"]
    assert story["status"] == "parked"
    assert result["status"] == "parked"


def test_review_story_escalated_story_reviews_via_claude_backend(
    plan_dir, agents_dir, monkeypatch,
):
    """Once escalated, EVERY subsequent review call for that story must go
    to Claude regardless of the global PIPELINE_BACKEND_REVIEW setting -
    review is normally resolved purely from the env var, with no per-story
    override, so this is the one seam that must explicitly check
    story['escalated'] and force backend_name='claude'."""
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "local")
    _write_manifest(plan_dir, "escreview", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "claude", "escalated": True},
    })
    captured = {}

    def _fake_reviewer(wt, br, backend_name=None, **k):
        captured["backend_name"] = backend_name
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("escreview", "S1")

    assert captured["backend_name"] == "claude"


def test_review_story_escalated_story_reviews_via_escalation_target(
    plan_dir, agents_dir, monkeypatch,
):
    """PIPELINE_ESCALATION_BACKEND retargets the escalated-review seam away from
    Claude: once a story is escalated and the operator has retargeted
    escalation to a non-Claude backend, every subsequent review for that story
    must go to the escalation target - not hardcoded Claude (which may be
    usage-capped and unavailable). Default (env unset) still forces Claude,
    as the prior test asserts."""
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "local")
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    _write_manifest(plan_dir, "escrevtgt", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low",
               "backend": "claude", "escalated": True},
    })
    captured = {}

    def _fake_reviewer(wt, br, backend_name=None, **k):
        captured["backend_name"] = backend_name
        return "VERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)
    monkeypatch.setattr(p, "_open_pr", lambda wt, key, story: "https://gh/pr/1")

    p.review_story("escrevtgt", "S1")

    assert captured["backend_name"] == "ollama"


# ---------- FM-A: acceptance oracle gates check_story_status when present ----------

def _setup_oracle_story(plan_dir, plan_name, worktree, acceptance=None, extra=None):
    """Write a manifest story with the given acceptance block and worktree."""
    story = {
        "summary": "Implement thing",
        "status": "in_progress",
        "pid": 4242,
        "worktree": str(worktree),
    }
    if acceptance is not None:
        story["acceptance"] = acceptance
    if extra:
        story.update(extra)
    _write_manifest(plan_dir, plan_name, {"S1": story})


def test_check_story_status_with_acceptance_runs_only_oracle_tests(plan_dir, monkeypatch):
    # FM-A: when a story has an acceptance block, check_story_status must run
    # only the oracle test files — not the model's self-written tests.
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("did work\n")

    acceptance = [{"path": "tests/test_oracle.py", "source": "def test_ok(): pass"}]
    _setup_oracle_story(plan_dir, "fm_a_oracle", worktree, acceptance=acceptance)

    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_last_nonempty_line", lambda f: "")

    captured_cmds = []

    class _Pass:
        returncode = 0
        stdout = "1 passed"

    def _fake_run(cmd, **kw):
        captured_cmds.append(list(cmd))
        return _Pass()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (str(worktree), ["pytest"]))

    result = p.check_story_status("fm_a_oracle", "S1")

    assert result["status"] == "tests_passed"
    # The command actually run must include the oracle path, not be a bare suite run.
    test_cmds = [c for c in captured_cmds if "pytest" in c[0] or "pytest" in (c[1] if len(c) > 1 else "")]
    assert test_cmds, "pytest must have been called"
    assert any("tests/test_oracle.py" in " ".join(cmd) for cmd in captured_cmds), (
        "oracle path must appear in the pytest command when acceptance is set"
    )


def test_check_story_status_with_acceptance_fails_when_oracle_fails(plan_dir, monkeypatch):
    # When the oracle tests fail, status must be `failed` even if the model's
    # own tests would pass.
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("did work\n")

    acceptance = [{"path": "tests/test_oracle.py", "source": "def test_spec(): assert False"}]
    _setup_oracle_story(plan_dir, "fm_a_fail", worktree, acceptance=acceptance)

    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_last_nonempty_line", lambda f: "")

    class _Fail:
        returncode = 1
        stdout = "1 failed"

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _Fail())
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (str(worktree), ["pytest"]))

    result = p.check_story_status("fm_a_fail", "S1")
    assert result["status"] == "failed"


def test_check_story_status_without_acceptance_runs_whole_suite(plan_dir, monkeypatch):
    # Regression: stories without an acceptance block must still run the full suite.
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("did work\n")
    _setup_oracle_story(plan_dir, "fm_a_nosuite", worktree, acceptance=None)

    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "_last_nonempty_line", lambda f: "")

    captured_cmds = []

    class _Pass:
        returncode = 0
        stdout = "all passed"

    def _fake_run(cmd, **kw):
        captured_cmds.append(list(cmd))
        return _Pass()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (str(worktree), ["pytest"]))

    result = p.check_story_status("fm_a_nosuite", "S1")

    assert result["status"] == "tests_passed"
    test_cmds = [c for c in captured_cmds if "pytest" in c[0] or (len(c) > 1 and "pytest" in c[1])]
    # Must not have scoped to any specific file (no path args beyond bare pytest).
    assert any(c == ["pytest"] for c in test_cmds), (
        "whole-suite run must be bare pytest when no acceptance block"
    )


# ---------- issue 24eb6c5b: lock approve_merge + refresh parked_reason ----------

def test_approve_merge_returns_retriable_busy_when_lock_held(plan_dir, monkeypatch):
    """approve_merge must not proceed on stale state when the plan lock is
    already held by another context (scheduler tick). It returns a clean,
    retriable error instead of crashing or silently succeeding."""
    _write_manifest(plan_dir, "ambusy", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    lock_path = plan_dir / "ambusy.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.approve_merge("ambusy", "P1")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert result["ok"] is False
    assert result.get("retriable") is True
    assert "busy" in result["error"].lower() or "retry" in result["error"].lower()
    assert merged == []
    # Story must be untouched.
    assert _read_manifest(plan_dir, "ambusy")["stories"]["P1"]["status"] == "parked"


def test_approve_merge_rereads_manifest_inside_lock(plan_dir, monkeypatch):
    """approve_merge must re-read the manifest from disk AFTER acquiring the
    lock, so it merges against the freshest on-disk state, not a pre-lock
    stale copy. We mutate an unrelated field on disk before the call and
    confirm the merge proceeds using the fresh manifest (the worktree path
    is taken from the on-disk manifest, so we change it and verify the
    merge used the fresh value)."""
    _write_manifest(plan_dir, "amfresh", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/old/wt"},
    })

    seen_worktrees = []
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, sha: {"state": "pass"})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass"})
    monkeypatch.setattr(p, "_reverify_build", lambda wt: {"state": "pass"})

    def _capture_merge(wt, key):
        seen_worktrees.append(wt)
        return "merged"
    monkeypatch.setattr(p, "_merge_pr", _capture_merge)
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    # Mutate the on-disk manifest to a fresh worktree path BEFORE calling
    # approve_merge. If approve_merge uses a pre-lock stale copy, it will
    # pass "/old/wt" to _merge_pr; if it re-reads inside the lock, it will
    # pass "/fresh/wt".
    _write_manifest(plan_dir, "amfresh", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/fresh/wt"},
    })

    result = p.approve_merge("amfresh", "P1")

    assert result["ok"] is True
    assert seen_worktrees == ["/fresh/wt"]


def test_approve_merge_revalidates_status_inside_lock(plan_dir, monkeypatch):
    """If the story's status changed on disk while waiting for the lock
    (e.g. a scheduler tick already merged it), approve_merge must detect the
    stale state and return the validation error rather than proceeding."""
    _write_manifest(plan_dir, "amstale", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    # Simulate the on-disk state changing to 'done' before approve_merge
    # acquires the lock (the pre-lock read sees 'parked', but the in-lock
    # re-read sees 'done').
    _write_manifest(plan_dir, "amstale", {
        "P1": {"summary": "approved", "status": "done", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })

    result = p.approve_merge("amstale", "P1")

    assert result["ok"] is False
    assert merged == []


def test_approve_merge_revalidates_review_verdict_inside_lock(plan_dir, monkeypatch):
    """If the review_verdict changed on disk while waiting for the lock,
    approve_merge must detect it and refuse rather than merging unapproved work."""
    _write_manifest(plan_dir, "amverdict", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    # On-disk verdict changed to REQUEST_CHANGES before the lock was acquired.
    _write_manifest(plan_dir, "amverdict", {
        "P1": {"summary": "changes requested", "status": "parked",
               "review_verdict": "REQUEST_CHANGES", "risk": "medium", "worktree": "/x"},
    })

    result = p.approve_merge("amverdict", "P1")

    assert result["ok"] is False
    assert merged == []


def test_merge_gate_park_sets_parked_reason(plan_dir, monkeypatch):
    """When the scheduler's merge-adjudication loop parks a pr_open story
    (non-merge decision), it must set parked_reason to the decision's reason
    string, matching the review-park sites."""
    _write_manifest(plan_dir, "mgpark", {
        "P1": {"summary": "approved but high risk", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "high", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "full")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    # advance_pipeline will adjudicate the merge; high risk -> park.
    p.advance_pipeline("mgpark")

    story = _read_manifest(plan_dir, "mgpark")["stories"]["P1"]
    assert story["status"] == "parked"
    assert story.get("parked_reason") == "high risk held for human review"


def test_approve_merge_clears_parked_reason_on_done(plan_dir, monkeypatch):
    """A story leaving 'parked' status via successful approve_merge must no
    longer carry a stale parked_reason key."""
    _write_manifest(plan_dir, "amclear", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x", "parked_reason": "high risk held for human review"},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.approve_merge("amclear", "P1")

    assert result["ok"] is True
    story = _read_manifest(plan_dir, "amclear")["stories"]["P1"]
    assert story["status"] == "done"
    assert "parked_reason" not in story


def test_scheduler_merge_clears_parked_reason_on_done(plan_dir, monkeypatch):
    """When the scheduler's merge loop successfully merges a pr_open story
    that previously carried a parked_reason, the reason must be cleared."""
    _write_manifest(plan_dir, "smclear", {
        "P1": {"summary": "approved low risk", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x",
               "parked_reason": "stale reason from a prior park"},
    })
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "full")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, sha: {"state": "pass"})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass"})
    monkeypatch.setattr(p, "_reverify_build", lambda wt: {"state": "pass"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    p.advance_pipeline("smclear")

    story = _read_manifest(plan_dir, "smclear")["stories"]["P1"]
    assert story["status"] == "done"
    assert "parked_reason" not in story


def test_set_story_status_clears_parked_reason_on_unpark(plan_dir, monkeypatch):
    """set_story_status transitioning a story OUT of 'parked' to an active
    status must clear parked_reason so a stale reason doesn't survive."""
    _write_manifest(plan_dir, "sss", {
        "P1": {"summary": "parked", "status": "parked",
               "parked_reason": "high risk held for human review"},
    })

    result = p.set_story_status("sss", "P1", "interrupted")

    assert result["ok"] is True
    story = _read_manifest(plan_dir, "sss")["stories"]["P1"]
    assert story["status"] == "interrupted"
    assert "parked_reason" not in story


# ---------- Auto-retry review on transient backend 500 ----------

_TRANSIENT_500_MSG = "500 Internal Server Error: upstream crashed mid-request"


def test_is_transient_backend_error_detects_500():
    assert p._is_transient_backend_error("HTTP 500 Internal Server Error")


def test_is_transient_backend_error_detects_internal_server_error():
    assert p._is_transient_backend_error("internal server error: something broke")


def test_is_transient_backend_error_detects_connection_reset():
    assert p._is_transient_backend_error("Connection reset by peer")


def test_is_transient_backend_error_detects_connection_refused():
    assert p._is_transient_backend_error("Connection refused while contacting backend")


def test_is_transient_backend_error_false_for_rate_limit_banner():
    """The two detectors must not double-handle the same input."""
    assert not p._is_transient_backend_error(_RATE_LIMIT_MSG)


def test_is_transient_backend_error_false_for_normal_review():
    normal = (
        "I reviewed the diff. The implementation looks correct.\n"
        "VERDICT: APPROVE\n"
    )
    assert not p._is_transient_backend_error(normal)


def test_is_rate_limited_false_for_transient_500():
    """Conversely, a transient-500 must not be treated as a rate-limit."""
    assert not p._is_rate_limited(_TRANSIENT_500_MSG)


def test_review_story_transient_500_retry_resolves_to_approve(
    plan_dir, agents_dir, monkeypatch
):
    """First reviewer call returns a transient-500 UNKNOWN; the single retry
    returns VERDICT: APPROVE. The story must end up approved with
    review_inconclusive_count NOT incremented."""
    _write_manifest(plan_dir, "transient_ok", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })

    call_count = {"n": 0}

    def _reviewer(wt, br, backend_name=None, **k):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _TRANSIENT_500_MSG
        return "Looks good.\nVERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _reviewer)
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: "https://pr/1")
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    result = p.review_story("transient_ok", "S1")

    assert result["verdict"] == "APPROVE"
    assert call_count["n"] == 2  # original + one retry
    story = _read_manifest(plan_dir, "transient_ok")["stories"]["S1"]
    assert story["review_verdict"] == "APPROVE"
    assert story.get("review_inconclusive_count", 0) == 0


def test_review_story_transient_500_retry_also_fails_increments_inconclusive_once(
    plan_dir, agents_dir, monkeypatch
):
    """Both the original and the retry return transient-500 UNKNOWN. The retry
    must happen exactly once (2 total calls) and review_inconclusive_count
    must increment by exactly 1, falling through to the existing inconclusive
    path unchanged."""
    _write_manifest(plan_dir, "transient_fail", {
        "S1": {"summary": "Add thing", "status": "tests_passed",
               "worktree": str(plan_dir / "wt"), "risk": "low"},
    })

    call_count = {"n": 0}

    def _reviewer(wt, br, backend_name=None, **k):
        call_count["n"] += 1
        return _TRANSIENT_500_MSG  # always fails

    monkeypatch.setattr(p, "_run_reviewer", _reviewer)
    monkeypatch.setattr(p, "_open_pr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no PR")))
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    result = p.review_story("transient_fail", "S1")

    assert call_count["n"] == 2  # original + exactly one retry
    assert result["verdict"] == "UNKNOWN"
    story = _read_manifest(plan_dir, "transient_fail")["stories"]["S1"]
    assert story["review_inconclusive_count"] == 1  # incremented exactly once
    assert story["status"] == "tests_passed"


