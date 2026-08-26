"""Tests for the pipeline MCP server: usage-gate log throttling, path-traversal validation, and the MCP tool API surface.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import subprocess
from pathlib import Path

from app import backend
from pipeline import ci as pci
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


def test_rebase_onto_master_returns_not_ok_when_git_missing(monkeypatch, tmp_path):
    # `git` absent/non-executable raises OSError; the helper must not escape it
    # (the loop only wraps _merge_pr in try/except). It reports a non-conflict
    # failure so the caller parks rather than crashing.
    wt = tmp_path / "wt"
    wt.mkdir()

    def _raise_run(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'git'")

    monkeypatch.setattr(p.subprocess, "run", _raise_run)
    monkeypatch.setattr(p, "REPO_ROOT", str(tmp_path))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is False
    assert rb["conflict"] is False


def test_advance_pipeline_push_failure_blocks_merge(plan_dir, monkeypatch, tmp_path):
    # A failed force-push (lease rejected / network / auth) must block before
    # _ci_status and _merge_pr: otherwise the remote HEAD stays stale and the
    # gate squashes pre-rebase code. It counts against merge_attempts and leaves
    # the story pr_open for retry.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    wt = tmp_path / "wt"
    wt.mkdir()
    _write_manifest(plan_dir, "pushfail", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": str(wt)},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt_, br: {"ok": True, "conflict": False, "error": ""})

    def _fake_run(argv, **_):
        class R:
            returncode = 1
            stdout = ""
            stderr = "non-fast-forward (lease rejected)"
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt_, key: merged_calls.append(key))

    result = p.advance_pipeline("pushfail")

    story = _read_manifest(plan_dir, "pushfail")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert merged_calls == []          # _merge_pr must not run on a push fail
    assert result["merged"] == []
    assert result["failed"] == []
    assert "P1" in result["notify"]


def test_approve_merge_push_failure_returns_error(plan_dir, monkeypatch, tmp_path):
    # The manual override must also surface a failed force-push rather than
    # proceeding to merge stale remote code.
    wt = tmp_path / "wt"
    wt.mkdir()
    _write_manifest(plan_dir, "ampush", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": str(wt)},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt_, br: {"ok": True, "conflict": False, "error": ""})

    def _fake_run(argv, **_):
        class R:
            returncode = 1
            stdout = ""
            stderr = "lease rejected"
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt_, key: merged_calls.append(key))

    result = p.approve_merge("ampush", "P1")

    assert result["ok"] is False
    assert "push failed" in result["error"]
    assert merged_calls == []


def test_advance_pipeline_rebase_conflict_retries_within_budget(plan_dir, monkeypatch):
    # A rebase conflict blocks the merge before _merge_pr is ever called; it
    # counts against merge_attempts and leaves the story pr_open for retry.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rbconflict", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": False, "conflict": True,
                                        "error": "conflict in app.js"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.advance_pipeline("rbconflict")

    story = _read_manifest(plan_dir, "rbconflict")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert merged_calls == []          # _merge_pr must not run on a rebase fail
    assert result["merged"] == []
    assert result["failed"] == []
    assert "P1" in result["notify"]


def test_advance_pipeline_rebase_conflict_exhausts_budget(plan_dir, monkeypatch):
    # Once the budget is spent on repeated rebase conflicts, the story fails
    # for human intervention rather than retrying forever.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "rbgiveup", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "merge_attempts": 2},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": False, "conflict": True, "error": "boom"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("rbgiveup")

    story = _read_manifest(plan_dir, "rbgiveup")["stories"]["P1"]
    assert story["status"] == "failed"
    assert story["merge_attempts"] == 3
    assert "rebase:" in story.get("merge_error", "")
    assert "P1" in result["failed"]


def test_advance_pipeline_ci_fail_blocks_merge(plan_dir, monkeypatch):
    # A failing CI check blocks the merge and counts against the budget; the
    # reviewer APPROVE alone is not sufficient to land a red PR.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cifail", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "ruff"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.advance_pipeline("cifail")

    story = _read_manifest(plan_dir, "cifail")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert merged_calls == []
    assert result["merged"] == []
    assert result["failed"] == []


def test_advance_pipeline_ci_fail_routes_to_rework_when_opted_in(plan_dir, monkeypatch):
    # PIPELINE_REWORK_ON_CI_FAIL=1: a definitive CI test failure on an
    # APPROVEd branch (e.g. the agent's own broken self-test, invisible to
    # the acceptance-scoped reviewer) is handed back to the implementer as
    # rework feedback instead of silently retrying the unchanged branch.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cifailrework", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "test_clamp_boundary failed"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.advance_pipeline("cifailrework")

    story = _read_manifest(plan_dir, "cifailrework")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story["merge_attempts"] == 1
    assert "rework_attempts" not in story
    assert "test_clamp_boundary failed" in story["review_feedback"]
    assert merged_calls == []
    assert result["merged"] == []
    assert result["failed"] == []


def test_advance_pipeline_ci_fail_rework_sets_ci_rework_flag(plan_dir, monkeypatch):
    # L1 (REVIEWER_ESCALATION_PLAN.md): when a definitive CI failure is routed
    # to rework, the story must carry a `ci_rework` flag so dispatch_story can
    # raise the agent's done-bar to full-suite-green on the redispatch. Without
    # it the rework round keeps the oracle-green bar and re-fails CI on the same
    # assertion (the gpt-oss token_bucket loop).
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cireworkflag", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "test_x failed"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    p.advance_pipeline("cireworkflag")

    story = _read_manifest(plan_dir, "cireworkflag")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story.get("ci_rework") is True


def test_advance_pipeline_ci_fail_rework_feedback_uses_wired_helper(plan_dir, monkeypatch):
    # Dead-code/wiring guard (MODE40-CI-REWORK-FEEDBACK-V2): the merge-gate
    # CI-fail block must CALL _ci_rework_feedback(gate_error) rather than keep
    # an inline template. The glm-authored unit suite calls the helper in
    # isolation, so an executor that defines _ci_rework_feedback but skips
    # wiring it into the merge-gate ships DEAD CODE and still passes that
    # suite. This integration test closes that gap: it drives advance_pipeline's
    # real CI-fail -> rework path and asserts review_feedback carries the
    # commit-required sentence the helper ALWAYS appends (both lint and
    # non-lint branches). The pre-MODE40 inline template lacks that sentence
    # (it instead says "an incorrect assertion"), so this assertion fails on
    # an unwired/inline merge-gate and passes only when the helper is wired.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cireworkwired", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "test_clamp_boundary failed"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    p.advance_pipeline("cireworkwired")

    story = _read_manifest(plan_dir, "cireworkwired")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    # The helper always appends this commit-required sentence; the old inline
    # template does not. Its presence proves the merge-gate called the helper.
    assert "A NEW COMMIT on your branch is REQUIRED" in story["review_feedback"]
    # And the old inline template's telltale wording must be gone.
    assert "incorrect assertion" not in story["review_feedback"]


def test_advance_pipeline_ci_fail_stays_terminal_without_opt_in(plan_dir, monkeypatch):
    # Without the flag, a definitive CI failure keeps today's exact behavior:
    # merge_attempts increments and the story terminal-fails at the cap - no
    # rework routing.
    monkeypatch.delenv("PIPELINE_REWORK_ON_CI_FAIL", raising=False)
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 1)
    _write_manifest(plan_dir, "cifailnorework", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "boom"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("cifailnorework")

    story = _read_manifest(plan_dir, "cifailnorework")["stories"]["P1"]
    assert story["status"] == "failed"
    assert story["merge_attempts"] == 1
    assert "rework_attempts" not in story
    assert "P1" in result["failed"]


def test_advance_pipeline_ci_fail_rework_exhausted_falls_to_terminal_fail(plan_dir, monkeypatch):
    # Once the merge-CI rework budget is already spent (merge_attempts has
    # reached MERGE_MAX_ATTEMPTS), a further CI fail must not loop forever on
    # rework - it falls through to the existing terminal-fail path.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cifailexhausted", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "merge_attempts": 3},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "still broken"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("cifailexhausted")

    story = _read_manifest(plan_dir, "cifailexhausted")["stories"]["P1"]
    assert story["status"] == "failed"
    assert story["merge_attempts"] == 4  # was 3, +1 in the terminal-fail fall-through
    assert "rework_attempts" not in story
    assert "P1" in result["failed"]


def test_advance_pipeline_ci_fail_rework_counter_survives_review_approve(plan_dir, monkeypatch):
    # Regression (2026-07-17, token_bucket live run): the merge-CI->rework
    # loop MUST be bounded by merge_attempts, not rework_attempts. The review
    # APPROVE path (~line 4189) pops rework_attempts on every pass because the
    # acceptance-scoped reviewer APPROVEs whenever the oracle is green - so a
    # bound on rework_attempts resets to 0 each cycle and the loop never
    # exhausts (observed: four identical "routed to rework (1/3)"
    # notifications, same broken assertion every round). merge_attempts is the
    # merge gate's own counter and is NOT reset by review, so it must advance
    # 1->2->3 across rework -> review APPROVE -> merge-gate cycles, then
    # terminal-fail instead of looping forever.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cicycle", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "fail", "error": "test_x failed"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    def _simulate_rework_then_review_approve():
        # The agent re-dispatched off the rework feedback, the acceptance-
        # scoped reviewer APPROVEd (oracle green), and the review APPROVE
        # path popped rework_attempts. Story returns to pr_open for the
        # merge gate to re-run CI on the next tick.
        st = _read_manifest(plan_dir, "cicycle")["stories"]["P1"]
        st["status"] = "pr_open"
        st["review_verdict"] = "APPROVE"
        st.pop("rework_attempts", None)  # what review APPROVE does (~line 4189)
        _write_manifest(plan_dir, "cicycle", {"P1": st})

    # Tick 1: CI fail -> routed to rework, merge_attempts 0->1.
    p.advance_pipeline("cicycle")
    story = _read_manifest(plan_dir, "cicycle")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story["merge_attempts"] == 1

    # Tick 2: same CI fail after a review APPROVE that reset rework_attempts.
    # The bound must advance to 2/3, NOT reset back to 1/3 (the bug).
    _simulate_rework_then_review_approve()
    p.advance_pipeline("cicycle")
    story = _read_manifest(plan_dir, "cicycle")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story["merge_attempts"] == 2

    # Tick 3: advances to 3/3 (still within budget, routes once more).
    _simulate_rework_then_review_approve()
    p.advance_pipeline("cicycle")
    story = _read_manifest(plan_dir, "cicycle")["stories"]["P1"]
    assert story["status"] == "changes_requested"
    assert story["merge_attempts"] == 3

    # Tick 4: budget exhausted (merge_attempts=3 >= MERGE_MAX_ATTEMPTS=3) ->
    # terminal fail, no further rework routing (no infinite loop).
    _simulate_rework_then_review_approve()
    result = p.advance_pipeline("cicycle")
    story = _read_manifest(plan_dir, "cicycle")["stories"]["P1"]
    assert story["status"] == "failed"
    assert "P1" in result["failed"]


def test_advance_pipeline_transient_push_failure_not_routed_to_rework(plan_dir, monkeypatch):
    # A push/network failure is not a CI verdict at all - it must never
    # consume rework budget even with the opt-in flag set.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cipushfail", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": "network down"})(),
    )
    ci_calls = []
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: ci_calls.append(br))
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("cipushfail")

    story = _read_manifest(plan_dir, "cipushfail")["stories"]["P1"]
    assert ci_calls == []  # push failed before CI was ever consulted
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert "rework_attempts" not in story
    assert result["merged"] == []


def test_advance_pipeline_ci_pending_not_routed_to_rework(plan_dir, monkeypatch):
    # A pending CI result is not a definitive failure - it must keep retrying
    # via the ordinary merge_attempts path, never rework, even with the flag on.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cipendingrework", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "pending", "error": "timeout"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("cipendingrework")

    story = _read_manifest(plan_dir, "cipendingrework")["stories"]["P1"]
    assert story["status"] == "pr_open"
    # Non-blocking S5 contract: a pending CI result yields the tick (sets
    # ci_pending_since) instead of blocking with a gate_error that would
    # consume a merge attempt. It must never be routed to rework.
    assert story.get("ci_pending_since") is not None
    assert "merge_attempts" not in story
    assert "rework_attempts" not in story
    assert result["merged"] == []


def test_advance_pipeline_cancelled_ci_not_routed_to_rework(plan_dir, monkeypatch):
    # A cancelled-only CI result (after its one auto-rerun) carries no
    # code-quality signal - it must fall to the ordinary retry path, not rework.
    monkeypatch.setenv("PIPELINE_REWORK_ON_CI_FAIL", "1")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cicancelrework", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "ci_rerun_attempted": True},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "cancelled", "error": ""})
    monkeypatch.setattr(p, "_ci_rerun", lambda br: True)
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("cicancelrework")

    story = _read_manifest(plan_dir, "cicancelrework")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert "rework_attempts" not in story
    assert result["merged"] == []


def test_advance_pipeline_ci_pending_blocks_merge(plan_dir, monkeypatch):
    # Pending CI must not merge yet; it retries within budget instead.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cipending", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "pending", "error": "timeout"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: None)

    result = p.advance_pipeline("cipending")

    story = _read_manifest(plan_dir, "cipending")["stories"]["P1"]
    assert story["status"] == "pr_open"
    # Non-blocking S5 contract: a pending CI result yields the tick back to the
    # scheduler (sets ci_pending_since) instead of blocking with a gate_error
    # that would consume a merge attempt.
    assert story.get("ci_pending_since") is not None
    assert "merge_attempts" not in story
    assert result["merged"] == []


def test_advance_pipeline_rebase_and_ci_ok_merges(plan_dir, monkeypatch):
    # The happy path: rebase ok + CI pass -> merge proceeds and clears the
    # attempt counter, exactly like a pre-gate merge.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "rbok", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "merge_attempts": 1},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("rbok")

    story = _read_manifest(plan_dir, "rbok")["stories"]["P1"]
    assert story["status"] == "done"
    assert "merge_attempts" not in story
    assert result["merged"] == ["P1"]


def test_advance_pipeline_ci_gate_disabled_skips_ci(plan_dir, monkeypatch):
    # PIPELINE_MERGE_CI_GATE=0 is the documented opt-out: the real _ci_status
    # short-circuits to pass without ever calling gh, so a rebase-ok branch
    # merges even if checks would have failed. Prove gh is not consulted by
    # making any subprocess.run raise.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(pci, "PIPELINE_MERGE_CI_GATE", False)
    # Not under test here, but _advance_pipeline_locked unconditionally
    # resolves the review-role resource gate before reaching the merge/CI
    # logic this test targets. With no role_config on this manifest, that
    # resolution falls to the registry's review provider (real Ollama by
    # default), whose resource_status() shells out to `vm_stat` - exactly
    # the kind of incidental subprocess call this test's _boom_run guard
    # exists to catch. Stub it out so only a genuine CI-gate-path subprocess
    # call would trip the guard.
    monkeypatch.setattr(backend.OllamaDriver, "resource_status", lambda self: {"ok": True, "reason": ""})
    _write_manifest(plan_dir, "cidisabled", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    # Pre-existing gap (fails the same way on unmodified master): the
    # merge-adjudication path resolves the default branch name via
    # _default_branch(), which shells out to `git symbolic-ref`. That call is
    # unrelated to the CI gate this test targets, so stub it directly rather
    # than let it fall through to the _boom_run guard below.
    monkeypatch.setattr(p, "_default_branch", lambda: "master")

    def _boom_run(*a, **k):
        raise AssertionError("subprocess must not run when CI gate is disabled")

    monkeypatch.setattr(p.subprocess, "run", _boom_run)
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("cidisabled")

    story = _read_manifest(plan_dir, "cidisabled")["stories"]["P1"]
    assert story["status"] == "done"
    assert result["merged"] == ["P1"]


def test_advance_pipeline_cancelled_ci_triggers_one_rerun_then_merges(plan_dir, monkeypatch):
    # A cancelled-only CI result is worth exactly one automatic rerun before
    # falling back to the ordinary fail/retry path - not an immediate park.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "cicancel", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    ci_calls = []

    def _fake_ci_status(br, **_):
        ci_calls.append(br)
        if len(ci_calls) == 1:
            return {"state": "cancelled", "error": ""}
        return {"state": "pass", "error": ""}

    rerun_calls = []
    monkeypatch.setattr(p, "_ci_status_once", _fake_ci_status)
    monkeypatch.setattr(p, "_ci_rerun", lambda br: rerun_calls.append(br) or True)
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("cicancel")

    assert len(rerun_calls) == 1
    assert len(ci_calls) == 2
    story = _read_manifest(plan_dir, "cicancel")["stories"]["P1"]
    assert story["status"] == "done"
    assert result["merged"] == ["P1"]


def test_advance_pipeline_cancelled_ci_second_time_does_not_rerun_again(plan_dir, monkeypatch):
    # ci_rerun_attempted, once set, bounds the auto-rerun to exactly once per
    # story - a second cancelled result must fall straight to the ordinary
    # fail/retry path instead of rerunning indefinitely.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "cicancel2", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "ci_rerun_attempted": True},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once",
                        lambda br, **_: {"state": "cancelled", "error": ""})
    rerun_calls = []
    monkeypatch.setattr(p, "_ci_rerun", lambda br: rerun_calls.append(br) or True)
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")

    result = p.advance_pipeline("cicancel2")

    assert rerun_calls == []
    story = _read_manifest(plan_dir, "cicancel2")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert result["merged"] == []


def test_approve_merge_cancelled_ci_triggers_one_rerun_then_merges(plan_dir, monkeypatch):
    # Same one-shot auto-rerun behavior on the human-driven approve_merge
    # path as the scheduler's merge gate.
    _write_manifest(plan_dir, "amcancel", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    ci_calls = []

    def _fake_ci_status(br, **_):
        ci_calls.append(br)
        if len(ci_calls) == 1:
            return {"state": "cancelled", "error": ""}
        return {"state": "pass", "error": ""}

    rerun_calls = []
    monkeypatch.setattr(p, "_ci_status", _fake_ci_status)
    monkeypatch.setattr(p, "_ci_rerun", lambda br: rerun_calls.append(br) or True)
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.approve_merge("amcancel", "P1")

    assert result["ok"] is True
    assert len(rerun_calls) == 1
    assert len(ci_calls) == 2
    story = _read_manifest(plan_dir, "amcancel")["stories"]["P1"]
    assert story["status"] == "done"


def test_reverify_acceptance_reruns_full_suite_without_acceptance_block(monkeypatch, tmp_path):
    # Gap 1: stories without an acceptance block (the common case for
    # real-project stories) get the rebased branch's full test suite
    # re-run before merge, not a silent "none" pass. The 1 MBW in the
    # gpt-oss + glm-5.2:cloud v2 run was only caught because benchmark
    # stories carry acceptance oracles; a real-project story without
    # one had no second check after rebase until this fix. Behavior
    # change flagged per CLAUDE.md Step 4 (this test replaces the old
    # `test_reverify_acceptance_returns_none_without_acceptance_block`,
    # which asserted the silent-pass path).
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, stdout="1 failed", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))

    result = p._reverify_acceptance({"summary": "x"}, str(tmp_path))

    assert result["state"] == "fail"
    assert "failed" in result["error"]
    # The cmd must be the unscoped full suite (no acceptance paths
    # appended), because there is no acceptance block to scope to.
    assert seen_cmd["cmd"] == ["pytest"]


def test_reverify_acceptance_passes_when_full_suite_green(monkeypatch, tmp_path):
    # The "no acceptance block" arm returns pass on rc=0, not "none" -
    # the MBW safety net only catches real failures; a clean suite
    # merges normally.
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))

    result = p._reverify_acceptance({"summary": "x"}, str(tmp_path))

    assert result == {"state": "pass", "error": ""}
    assert seen_cmd["cmd"] == ["pytest"]


def test_reverify_acceptance_full_suite_opt_out_restores_none(monkeypatch, tmp_path):
    # `PIPELINE_REVERIFY_FULL_SUITE=0` restores the old silent-pass
    # behavior for operators with slow test suites who don't want a
    # full-suite re-run at the merge gate. Mirrors the opt-out pattern
    # used by `PIPELINE_MERGE_CI_GATE`.
    def _boom_run(*a, **k):
        raise AssertionError("must not run tests when opted out")
    monkeypatch.setattr(p.subprocess, "run", _boom_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))
    monkeypatch.setenv("PIPELINE_REVERIFY_FULL_SUITE", "0")

    result = p._reverify_acceptance({"summary": "x"}, str(tmp_path))

    assert result == {"state": "none", "error": ""}


def test_reverify_acceptance_scopes_cargo_to_acceptance_test_stem(monkeypatch, tmp_path):
    # FM-A fix: a non-pytest runner with an acceptance block is now scoped to
    # the oracle fixture, not run as the full suite. cargo names integration
    # tests by file stem, so tests/acc.rs -> `cargo test --test acc` runs ONLY
    # the oracle, excluding the implementer's own tests/<name>.rs (previously
    # the full `cargo test` graded the implementer's own tests — the
    # "graded on own buggy tests" failure mode).
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["cargo", "test"]))

    result = p._reverify_acceptance(
        {"summary": "x", "acceptance": [{"path": "tests/acc.rs", "source": "// x"}]},
        str(tmp_path),
    )

    assert result == {"state": "pass", "error": ""}
    assert seen_cmd["cmd"] == ["cargo", "test", "--test", "acc"]


def test_reverify_acceptance_reruns_full_suite_for_unscopeable_runner(monkeypatch, tmp_path):
    # Safety net preserved: runners we can't safely scope (mvn, gradle, make,
    # jest-style npm) fall back to the full suite, so a post-rebase break in a
    # real-project story without a scoping-safe runner still can't slip through.
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["mvn", "test"]))

    result = p._reverify_acceptance(
        {"summary": "x", "acceptance": [{"path": "tests/acc.rs", "source": "// x"}]},
        str(tmp_path),
    )

    assert result == {"state": "pass", "error": ""}
    # mvn can't be safely scoped -> full suite.
    assert seen_cmd["cmd"] == ["mvn", "test"]


def test_reverify_acceptance_appends_own_test_paths_without_acceptance_block(
    monkeypatch, tmp_path,
):
    # Mode 42 done-bar blindspot, merge-gate side: a no-acceptance story
    # whose deliverable lives under tests/ gets its own new tests/test_*.py
    # file appended to the full-suite re-run when story_key is given, so a
    # break the model's own test would have caught can't slip through the
    # last check before merge (see _added_pytest_test_paths).
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))
    monkeypatch.setattr(
        pci, "_added_pytest_test_paths",
        lambda wt, key, base: ["tests/benchmark/test_driver.py"]
        if key == "S1" else [],
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    result = p._reverify_acceptance({"summary": "x"}, str(tmp_path), "S1")

    assert result == {"state": "pass", "error": ""}
    assert seen_cmd["cmd"] == [
        "pytest", str(tmp_path / "tests/benchmark/test_driver.py")]


def test_reverify_acceptance_no_story_key_leaves_full_suite_unscoped(
    monkeypatch, tmp_path,
):
    # Backward-compat default: callers that don't pass story_key (the
    # pre-existing 2-arg call shape) get the plain full suite, unchanged.
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))
    monkeypatch.setattr(
        pci, "_added_pytest_test_paths",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not be called without a story_key")),
    )

    result = p._reverify_acceptance({"summary": "x"}, str(tmp_path))

    assert result == {"state": "pass", "error": ""}
    assert seen_cmd["cmd"] == ["pytest"]


def test_reverify_acceptance_does_not_augment_when_acceptance_block_present(
    monkeypatch, tmp_path,
):
    # A story WITH an acceptance block stays scoped to the oracle only
    # (FM-A) - own-test-path augmentation is only for the no-acceptance
    # full-suite arm.
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))
    monkeypatch.setattr(
        pci, "_added_pytest_test_paths",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not be called when acceptance block is present")),
    )
    story = {"acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}]}

    result = p._reverify_acceptance(story, str(tmp_path), "S1")

    assert result == {"state": "pass", "error": ""}
    assert seen_cmd["cmd"] == ["pytest", str(tmp_path / "test_acceptance.py")]


def test_reverify_acceptance_skips_augmentation_for_non_pytest_runner(
    monkeypatch, tmp_path,
):
    # A non-pytest full-suite command (e.g. mvn) must not have
    # _added_pytest_test_paths' output appended - it only knows how to
    # extend a pytest invocation.
    seen_cmd = {}
    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["mvn", "test"]))
    monkeypatch.setattr(
        pci, "_added_pytest_test_paths",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not be called for a non-pytest runner")),
    )

    result = p._reverify_acceptance({"summary": "x"}, str(tmp_path), "S1")

    assert result == {"state": "pass", "error": ""}
    assert seen_cmd["cmd"] == ["mvn", "test"]


def test_reverify_acceptance_returns_none_for_missing_worktree(monkeypatch):
    def _boom_run(*a, **k):
        raise AssertionError("must not run tests against a missing worktree")

    monkeypatch.setattr(p.subprocess, "run", _boom_run)
    story = {"acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}]}

    result = p._reverify_acceptance(story, "/no/such/worktree")

    assert result == {"state": "none", "error": ""}


def test_reverify_acceptance_fails_when_oracle_red(monkeypatch, tmp_path):
    # The exact case the RLI-3 merged-but-wrong incident needed caught: the
    # acceptance test references behavior the branch never implemented.
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))
    seen_cmd = {}

    def _fake_run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, stdout="AttributeError: no available_tokens", stderr="")

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    story = {"acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}]}

    result = p._reverify_acceptance(story, str(tmp_path))

    assert result["state"] == "fail"
    assert "available_tokens" in result["error"]
    assert seen_cmd["cmd"] == ["pytest", str(tmp_path / "test_acceptance.py")]


def test_reverify_acceptance_passes_when_oracle_green(monkeypatch, tmp_path):
    monkeypatch.setattr(pci, "detect_test_command", lambda wt: (wt, ["pytest"]))
    monkeypatch.setattr(p.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    story = {"acceptance": [{"path": "test_acceptance.py", "source": "def test_x(): pass"}]}

    result = p._reverify_acceptance(story, str(tmp_path))

    assert result == {"state": "pass", "error": ""}


