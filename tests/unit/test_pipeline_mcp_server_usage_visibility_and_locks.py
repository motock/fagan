"""Tests for the pipeline MCP server: usage-gate blind-state visibility, merge adjudication, and advance_pipeline/dispatch_story concurrency locks.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import json
import subprocess

from pipeline import ci as pci
from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
    plan_dir,
    usage_state_path,
)

# ---------- _reverify_build (T4) ----------
# Neither the reviewer nor the dispatched agent's own "tests pass" report is
# proof the project actually builds - PR #48 shipped with `npm run build`
# broken (a real, pre-existing bug: Node's `crypto` module can't bundle for a
# browser target) because nobody ran it before merge (2026-07-07
# web-client-epic retro §3.1).

def test_reverify_build_fails_when_build_command_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(pci, "detect_build_command", lambda wt: (wt, ["npm", "run", "build"]))
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="Error: Can't resolve 'crypto'"),
    )

    result = p._reverify_build(str(tmp_path))

    assert result["state"] == "fail"
    assert "crypto" in result["error"]


def test_reverify_build_passes_when_build_command_exits_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(pci, "detect_build_command", lambda wt: (wt, ["npm", "run", "build"]))
    monkeypatch.setattr(
        p.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    result = p._reverify_build(str(tmp_path))

    assert result == {"state": "pass", "error": ""}


def test_reverify_build_skips_when_no_build_command_detected(monkeypatch, tmp_path):
    # A repo without a build step (most real-project stories) must merge
    # freely - "none" is a skip, not a block.
    def _boom_run(*a, **k):
        raise AssertionError("must not run anything when no build command is detected")
    monkeypatch.setattr(pci, "detect_build_command", lambda wt: None)
    monkeypatch.setattr(p.subprocess, "run", _boom_run)

    result = p._reverify_build(str(tmp_path))

    assert result == {"state": "none", "error": ""}


def test_reverify_build_returns_none_for_missing_worktree(monkeypatch):
    def _boom_run(*a, **k):
        raise AssertionError("must not attempt a build against a missing worktree")
    monkeypatch.setattr(p.subprocess, "run", _boom_run)

    result = p._reverify_build("/no/such/worktree")

    assert result == {"state": "none", "error": ""}


def test_reverify_build_opt_out_restores_none(monkeypatch, tmp_path):
    # PIPELINE_MERGE_BUILD_GATE=0 restores the old silent-skip behavior for
    # operators with slow builds who don't want a build re-run at the merge
    # gate. Mirrors the opt-out pattern used by PIPELINE_MERGE_CI_GATE and
    # PIPELINE_REVERIFY_FULL_SUITE.
    def _boom_run(*a, **k):
        raise AssertionError("must not build when opted out")
    monkeypatch.setattr(pci, "detect_build_command", lambda wt: (wt, ["npm", "run", "build"]))
    monkeypatch.setattr(p.subprocess, "run", _boom_run)
    monkeypatch.setattr(pci, "PIPELINE_MERGE_BUILD_GATE", False)

    result = p._reverify_build(str(tmp_path))

    assert result == {"state": "none", "error": "build gate disabled"}


def test_advance_pipeline_build_reverify_fail_blocks_merge(plan_dir, monkeypatch):
    # A reviewer APPROVE + green CI + passing tests is not sufficient to land
    # a branch whose build, re-run independently right before merge, fails.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "buildfail", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_build",
                        lambda wt: {"state": "fail", "error": "Error: Can't resolve 'crypto'"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.advance_pipeline("buildfail")

    story = _read_manifest(plan_dir, "buildfail")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert merged_calls == []
    assert result["merged"] == []


def test_advance_pipeline_build_reverify_pass_merges(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "buildok", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_build", lambda wt: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("buildok")

    story = _read_manifest(plan_dir, "buildok")["stories"]["P1"]
    assert story["status"] == "done"
    assert result["merged"] == ["P1"]


def test_approve_merge_build_reverify_fail_returns_error(plan_dir, monkeypatch):
    # The manual override must also refuse to land a branch whose build,
    # re-run independently right before merge, fails.
    _write_manifest(plan_dir, "ambuild", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_build",
                        lambda wt: {"state": "fail", "error": "build broke"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.approve_merge("ambuild", "P1")

    assert result["ok"] is False
    assert "build reverify fail" in result["error"]
    assert merged_calls == []


def test_advance_pipeline_acceptance_reverify_fail_blocks_merge(plan_dir, monkeypatch):
    # A reviewer APPROVE + green CI is not sufficient to land a branch whose
    # acceptance oracle, re-run independently right before merge, is red.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "accfail", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x",
               "acceptance": [{"path": "test_acceptance.py", "source": "x"}]},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "fail", "error": "AttributeError"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.advance_pipeline("accfail")

    story = _read_manifest(plan_dir, "accfail")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert merged_calls == []
    assert result["merged"] == []


def test_advance_pipeline_full_suite_reverify_fail_blocks_merge(plan_dir, monkeypatch):
    # Gap 1: the no-acceptance-block path runs the full test suite at the
    # merge gate. A story that broke a sibling's module after rebase is
    # now caught here, even without a harness-owned acceptance oracle.
    # Mirrors the existing `test_advance_pipeline_acceptance_reverify_fail_blocks_merge`
    # but for the no-acceptance case (which is the common one for real
    # projects; the benchmark tasks all carry oracles and were already
    # covered).
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "fsfail", {
        "P1": {"summary": "approved but rebased-broken", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x",
               # NO acceptance block - ordinary TDD story.
               },
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "fail", "error": "ModuleNotFoundError: shared"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.advance_pipeline("fsfail")

    story = _read_manifest(plan_dir, "fsfail")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert merged_calls == []
    assert result["merged"] == []


def test_advance_pipeline_acceptance_reverify_pass_merges(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "accok", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x",
               "acceptance": [{"path": "test_acceptance.py", "source": "x"}]},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("accok")

    story = _read_manifest(plan_dir, "accok")["stories"]["P1"]
    assert story["status"] == "done"
    assert result["merged"] == ["P1"]


def test_approve_merge_acceptance_reverify_fail_returns_error(plan_dir, monkeypatch):
    # The manual override must also refuse to land a branch whose acceptance
    # oracle fails on reverification, even with a prior reviewer APPROVE.
    _write_manifest(plan_dir, "amacc", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x",
               "acceptance": [{"path": "test_acceptance.py", "source": "x"}]},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status_once", lambda br, **_: {"state": "pass", "error": ""})
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda story, wt, *a, **k: {"state": "fail", "error": "AttributeError"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.approve_merge("amacc", "P1")

    assert result["ok"] is False
    assert "acceptance reverify fail" in result["error"]
    assert merged_calls == []


def test_approve_merge_rebase_conflict_returns_error(plan_dir, monkeypatch):
    # The manual override must also refuse to land a conflicting branch; it
    # surfaces the rebase failure rather than calling _merge_pr.
    _write_manifest(plan_dir, "amrb", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": False, "conflict": True, "error": "boom"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.approve_merge("amrb", "P1")

    assert result["ok"] is False
    assert "rebase failed" in result["error"]
    assert merged_calls == []


def test_approve_merge_ci_fail_returns_error(plan_dir, monkeypatch):
    # The manual override must also refuse to land a CI-red PR.
    _write_manifest(plan_dir, "amci", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br, **k: {"ok": True, "conflict": False, "error": ""})
    monkeypatch.setattr(p, "_ci_status",
                        lambda br, **_: {"state": "fail", "error": "ruff"})
    merged_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged_calls.append(key))

    result = p.approve_merge("amci", "P1")

    assert result["ok"] is False
    assert "CI failing" in result["error"]
    assert merged_calls == []


def test_approve_merge_returns_error_on_merge_failure(plan_dir, monkeypatch):
    # The manual override surfaces a merge failure as a structured error to the
    # human invoking it rather than raising an unhandled exception.
    _write_manifest(plan_dir, "ammergefail", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr",
                        lambda wt, key: (_ for _ in ()).throw(RuntimeError("gh down")))

    result = p.approve_merge("ammergefail", "P1")

    assert result["ok"] is False
    assert "gh down" in result["error"]
    assert _read_manifest(plan_dir, "ammergefail")["stories"]["P1"]["status"] == "parked"


def test_advance_pipeline_redispatches_changes_requested(plan_dir, monkeypatch):
    # A story the reviewer sent back must be dispatch-eligible so the next tick
    # picks it up and reworks it - otherwise it freezes forever.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    _write_manifest(plan_dir, "cr", {
        "S1": {"summary": "rework me", "status": "changes_requested",
               "dependencies": [], "worktree": "/x",
               "review_feedback": "fix the bug", "rework_attempts": 1},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    p.advance_pipeline("cr")

    assert dispatched == ["S1"]


def test_advance_pipeline_dispatch_failure_retries_within_budget(plan_dir, monkeypatch):
    # A raising dispatch_story (bad git pull, backend hiccup) must not crash the
    # tick: the story keeps its dispatch-eligible status, its attempt counter is
    # bumped, and the user is notified so the next tick retries.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "DISPATCH_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "dispretry", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })
    monkeypatch.setattr(p, "dispatch_story",
                        lambda plan, key: (_ for _ in ()).throw(RuntimeError("git pull failed")))

    result = p.advance_pipeline("dispretry")

    story = _read_manifest(plan_dir, "dispretry")["stories"]["T1"]
    assert story["status"] == "todo"
    assert story["dispatch_attempts"] == 1
    assert "T1" not in result["failed"]
    assert "T1" in result["notify"]


def test_advance_pipeline_dispatch_failure_exhausts_budget(plan_dir, monkeypatch):
    # A persistently failing launch becomes a hard failure (terminal: failed is
    # not dispatch-eligible) rather than retrying every tick forever.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "DISPATCH_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "dispgiveup", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": [],
               "dispatch_attempts": 2},
    })
    monkeypatch.setattr(p, "dispatch_story",
                        lambda plan, key: (_ for _ in ()).throw(RuntimeError("still broken")))

    result = p.advance_pipeline("dispgiveup")

    story = _read_manifest(plan_dir, "dispgiveup")["stories"]["T1"]
    assert story["status"] == "failed"
    assert story["dispatch_attempts"] == 3
    assert "still broken" in story.get("dispatch_error", "")
    assert "T1" in result["failed"]
    assert "T1" in result["notify"]


def test_check_story_status_failed_launch_exhausts_budget(plan_dir, tmp_path, monkeypatch):
    # An empty agent.log past the startup grace window is a failed launch.
    # Within budget it stays interrupted (redispatched); once the budget is
    # spent it becomes a terminal failure. The grace window protects
    # legitimate-but-slow startups (Ollama -np 1 queueing) from being
    # mis-classified — zero it here so this test still exercises the
    # failed-launch path without racing the wall clock.
    monkeypatch.setattr(p, "DISPATCH_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(p, "DISPATCH_STARTUP_GRACE_SECONDS", 0)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("")
    _write_manifest(plan_dir, "launchgiveup", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatch_attempts": 2},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command",
                        lambda wt: (_ for _ in ()).throw(AssertionError("must not run tests")))

    result = p.check_story_status("launchgiveup", "S1")

    assert result["status"] == "failed"
    story = _read_manifest(plan_dir, "launchgiveup")["stories"]["S1"]
    assert story["status"] == "failed"
    assert story["dispatch_attempts"] == 3


def test_check_story_status_successful_run_clears_dispatch_attempts(plan_dir, tmp_path, monkeypatch):
    # Once a launch actually produces output and the tests run, the failed-launch
    # counter is cleared so earlier infra blips don't count against a clean run.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("the agent did real work\n")
    _write_manifest(plan_dir, "launchclear", {
        "S1": {"summary": "thing", "status": "in_progress", "pid": 4242,
               "worktree": str(worktree), "dispatch_attempts": 2},
    })
    monkeypatch.setattr(p.os, "kill",
                        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    class _Done:
        returncode = 0
        stdout = "ok"
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (str(worktree), ["true"]))
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: _Done())

    result = p.check_story_status("launchclear", "S1")

    assert result["status"] == "tests_passed"
    assert "dispatch_attempts" not in _read_manifest(plan_dir, "launchclear")["stories"]["S1"]


def test_plane_set_state_retries_then_succeeds(monkeypatch):
    # A transient Plane failure is retried within budget rather than dropped.
    monkeypatch.setattr(pt, "PLANE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pt, "_resolve_issue_uuid", lambda key: "uuid-1")
    monkeypatch.setattr(pt, "_get_state", lambda group: f"state-{group}")
    calls = []

    def _flaky(method, path, **kw):
        calls.append(path)
        if len(calls) < 2:
            raise RuntimeError("502")
        return {}
    monkeypatch.setattr(pt, "plane_request", _flaky)

    assert p._plane_set_state("S1", "started") is True
    assert len(calls) == 2


def test_plane_set_state_gives_up_after_budget_and_notifies(plan_dir, monkeypatch):
    # A persistent Plane outage gives up after the budget WITHOUT raising (Plane
    # is best-effort) and records the drop durably instead of a silent print.
    monkeypatch.setattr(pt, "PLANE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pt, "_resolve_issue_uuid", lambda key: "uuid-1")
    monkeypatch.setattr(pt, "_get_state", lambda group: f"state-{group}")
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("plane down")))
    notes = []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kwargs: notes.append(msg)
    )

    result = p._plane_set_state("S1", "completed", plan_name="pl")

    assert result is False
    assert len(notes) == 1
    assert "plane down" in notes[0]


def test_count_in_progress_agents_counts_across_plans(plan_dir, monkeypatch):
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: None)
    _write_manifest(plan_dir, "cnt1", {
        "A1": {"summary": "a", "status": "in_progress", "pid": 1},
        "A2": {"summary": "b", "status": "in_progress", "pid": 2},
        "A3": {"summary": "c", "status": "todo"},
    })
    _write_manifest(plan_dir, "cnt2", {
        "B1": {"summary": "d", "status": "in_progress", "pid": 3},
        "B2": {"summary": "e", "status": "done"},
    })
    assert p._count_in_progress_agents() == 3


def test_count_in_progress_agents_ignores_status_without_pid(plan_dir):
    # A story can be marked in_progress by mark_story_in_progress without
    # ever having been dispatched (no pid) - must not count as a running agent.
    _write_manifest(plan_dir, "cnt3", {
        "A1": {"summary": "a", "status": "in_progress"},
    })
    assert p._count_in_progress_agents() == 0


def test_count_in_progress_agents_skips_dead_pids(plan_dir, monkeypatch):
    # A story can be stuck at in_progress with a pid whose process already
    # exited (e.g. another plan whose own advance_pipeline tick never ran
    # again to notice) - the count must not include it, otherwise it would
    # permanently consume a concurrency slot. The reap itself is a separate
    # step (see test_reap_zombie_in_progress_stories below).
    _write_manifest(plan_dir, "cnt4", {
        "A1": {"summary": "alive", "status": "in_progress", "pid": 111},
        "A2": {"summary": "dead", "status": "in_progress", "pid": 222},
    })

    def _fake_kill(pid, sig):
        if pid == 222:
            raise ProcessLookupError

    monkeypatch.setattr(p.os, "kill", _fake_kill)

    assert p._count_in_progress_agents() == 1


def test_reap_zombie_in_progress_stories(plan_dir, monkeypatch):
    """The reap helper reaps in_progress-with-dead-pid stories back to todo,
    drops their pid, and writes the manifest back to disk. Idempotent: a
    second call on a clean manifest reaps 0.
    """
    _write_manifest(plan_dir, "zap", {
        "Z1": {"summary": "alive", "status": "in_progress", "pid": 111},
        "Z2": {"summary": "dead", "status": "in_progress", "pid": 222},
        "Z3": {"summary": "todo-already", "status": "todo"},
        "Z4": {"summary": "in-progress-no-pid", "status": "in_progress"},
    })

    def _fake_kill(pid, sig):
        if pid == 222:
            raise ProcessLookupError

    monkeypatch.setattr(p.os, "kill", _fake_kill)

    reaped = p._reap_zombie_in_progress_stories()
    assert reaped == 1, "only Z2 (dead pid) should be reaped"
    after = json.loads((plan_dir / "zap.manifest.json").read_text())
    assert after["stories"]["Z1"] == {"summary": "alive", "status": "in_progress", "pid": 111}
    assert after["stories"]["Z2"] == {"summary": "dead", "status": "todo"}
    assert after["stories"]["Z3"] == {"summary": "todo-already", "status": "todo"}
    # Z4 had no pid → not a zombie (just status drift), not reaped.
    assert after["stories"]["Z4"] == {"summary": "in-progress-no-pid", "status": "in_progress"}

    # Second call is idempotent.
    assert p._reap_zombie_in_progress_stories() == 0
    # Manifest unchanged on the no-op reap (no write).
    after2 = json.loads((plan_dir / "zap.manifest.json").read_text())
    assert after == after2


def test_advance_all_plans_does_not_pre_reap_zombies(plan_dir, monkeypatch):
    """Regression guard (2026-06-28): running an external reap pass before
    the polling phase silently leaves dead-pid stories re-dispatching
    forever — the polling phase never sees them, so dispatch_attempts is
    never bumped and the test is never run. advance_all_plans must NOT
    pre-reap; the polling phase handles dead pids via check_story_status
    which falls through to test-running on a dead pid.
    """
    _write_manifest(plan_dir, "zom", {
        "Z1": {"summary": "dead", "status": "in_progress", "pid": 999},
    })

    def _fake_kill(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(p.os, "kill", _fake_kill)

    # Stub advance_pipeline to verify it sees the manifest BEFORE any reap.
    captured = {}

    def _fake_advance(plan_name):
        captured.setdefault("calls", []).append(plan_name)
        manifest = json.loads((plan_dir / "zom.manifest.json").read_text())
        captured.setdefault("saw_z1", []).append(
            manifest["stories"]["Z1"]["status"]
        )
        return {"ok": True, "stub": True}

    monkeypatch.setattr(p, "advance_pipeline", _fake_advance)

    result = p.advance_all_plans()
    assert "zom" in result["plans"]
    # advance_pipeline must have seen Z1 as in_progress (not pre-reaped to todo)
    # so its polling phase can run check_story_status and bump dispatch_attempts.
    assert captured["saw_z1"] == ["in_progress"]
    # And there's no "reaped_zombies" in the response — reap is no longer
    # wired into advance_all_plans.
    assert "reaped_zombies" not in result


def test_advance_pipeline_caps_dispatch_at_max_concurrent_agents(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    _write_manifest(plan_dir, "cap1", {
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
        "T2": {"summary": "two", "status": "todo", "dependencies": []},
        "T3": {"summary": "three", "status": "todo", "dependencies": []},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    result = p.advance_pipeline("cap1")
    assert dispatched == ["T1", "T2"]
    assert result["dispatched"] == ["T1", "T2"]


def test_advance_pipeline_cap_accounts_for_already_running_agents(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: None)
    _write_manifest(plan_dir, "cap2", {
        "R1": {"summary": "running", "status": "in_progress", "pid": 111, "worktree": "/x"},
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
        "T2": {"summary": "two", "status": "todo", "dependencies": []},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    monkeypatch.setattr(
        p, "check_story_status", lambda plan, key: {"status": "running"},
    )

    result = p.advance_pipeline("cap2")
    assert dispatched == ["T1"]
    assert result["dispatched"] == ["T1"]


def test_advance_pipeline_zero_max_concurrent_agents_means_unlimited(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 0)
    _write_manifest(plan_dir, "cap3", {
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
        "T2": {"summary": "two", "status": "todo", "dependencies": []},
        "T3": {"summary": "three", "status": "todo", "dependencies": []},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    p.advance_pipeline("cap3")
    assert dispatched == ["T1", "T2", "T3"]


def test_advance_pipeline_not_paused_redispatches_interrupted_stories(
    plan_dir, usage_state_path, monkeypatch,
):
    usage_state_path.write_text(json.dumps({"session_pct": 20, "week_pct": 10, "paused": False}))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    _write_manifest(plan_dir, "resume", {
        "S1": {"summary": "interrupted one", "status": "interrupted",
               "worktree": "/x", "dependencies": []},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    p.advance_pipeline("resume")
    assert dispatched == ["S1"]


def test_advance_pipeline_plan_paused_skips_dispatch_review_and_merge(plan_dir, monkeypatch):
    # A plan-level pause (pause_plan) must stop a plan from being advanced
    # at all -- unlike the usage gate, it does not even adjudicate merges,
    # since the human asked for this specific plan to stop moving.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    (plan_dir / "halted.manifest.json").write_text(json.dumps({
        "epics": {},
        "paused": True,
        "stories": {
            "T1": {"summary": "todo", "status": "todo", "dependencies": []},
            "R1": {"summary": "running", "status": "in_progress", "pid": 111, "worktree": "/x"},
            "TP1": {"summary": "awaiting review", "status": "tests_passed",
                    "worktree": "/y", "risk": "low"},
            "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
                   "risk": "low", "worktree": "/z"},
        },
    }))

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    reviewed = []
    monkeypatch.setattr(
        p, "review_story",
        lambda plan, key: reviewed.append(key) or {"status": "pr_open"},
    )
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    interrupted = []
    monkeypatch.setattr(
        p, "interrupt_story",
        lambda plan, key: interrupted.append(key) or {"ok": True},
    )

    result = p.advance_pipeline("halted")

    assert result == {"ok": True, "skipped": "plan_paused"}
    assert dispatched == []
    assert reviewed == []
    assert merged == []
    assert interrupted == ["R1"]


def test_advance_pipeline_plan_paused_with_no_running_story_is_a_pure_noop(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    (plan_dir / "halted2.manifest.json").write_text(json.dumps({
        "epics": {},
        "paused": True,
        "stories": {"T1": {"summary": "todo", "status": "todo", "dependencies": []}},
    }))
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))

    result = p.advance_pipeline("halted2")

    assert result == {"ok": True, "skipped": "plan_paused"}
    assert dispatched == []


def test_pause_plan_sets_manifest_flag(plan_dir):
    _write_manifest(plan_dir, "tobehalted", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })

    result = p.pause_plan("tobehalted")

    assert result == {"ok": True, "plan_name": "tobehalted", "paused": True}
    assert _read_manifest(plan_dir, "tobehalted")["paused"] is True


def test_resume_plan_clears_manifest_flag(plan_dir):
    (plan_dir / "halted3.manifest.json").write_text(json.dumps({
        "epics": {}, "paused": True,
        "stories": {"T1": {"summary": "todo", "status": "todo", "dependencies": []}},
    }))

    result = p.resume_plan("halted3")

    assert result == {"ok": True, "plan_name": "halted3", "paused": False}
    assert _read_manifest(plan_dir, "halted3")["paused"] is False


def test_pause_plan_no_such_manifest_returns_error(plan_dir):
    result = p.pause_plan("never-ingested")
    assert result == {"ok": False, "error": "No manifest for never-ingested"}


def test_resume_plan_no_such_manifest_returns_error(plan_dir):
    result = p.resume_plan("never-ingested")
    assert result == {"ok": False, "error": "No manifest for never-ingested"}


def test_resume_plan_when_not_paused_is_a_noop(plan_dir):
    _write_manifest(plan_dir, "neverhalted", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
    })

    result = p.resume_plan("neverhalted")

    assert result == {"ok": True, "plan_name": "neverhalted", "paused": False}
    assert _read_manifest(plan_dir, "neverhalted")["paused"] is False


def test_advance_all_plans_runs_every_manifest(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "p1", {})
    _write_manifest(plan_dir, "p2", {})

    calls = []
    monkeypatch.setattr(
        p, "advance_pipeline",
        lambda plan_name: calls.append(plan_name) or {"ok": True, "plan": plan_name},
    )

    result = p.advance_all_plans()

    assert result["ok"] is True
    assert sorted(calls) == ["p1", "p2"]
    assert result["plans"]["p1"]["ok"] is True
    assert result["plans"]["p2"]["ok"] is True


def test_advance_all_plans_isolates_failures_and_continues(plan_dir, monkeypatch):
    """One plan crashing (e.g. a bad repo_root, a missing dependency tool)
    must not abort the whole batch -- other plans still need their tick."""
    _write_manifest(plan_dir, "p1", {})
    _write_manifest(plan_dir, "p2", {})

    def _fake_advance(plan_name):
        if plan_name == "p1":
            raise RuntimeError("boom")
        return {"ok": True, "plan": plan_name}

    monkeypatch.setattr(p, "advance_pipeline", _fake_advance)

    result = p.advance_all_plans()

    assert result["ok"] is True
    assert result["plans"]["p1"]["ok"] is False
    assert "boom" in result["plans"]["p1"]["error"]
    assert result["plans"]["p2"]["ok"] is True


def test_advance_all_plans_with_no_manifests_returns_empty(plan_dir):
    result = p.advance_all_plans()
    assert result == {"ok": True, "plans": {}}


def test_advance_all_plans_ignores_unignested_plan_json(plan_dir, monkeypatch):
    (plan_dir / "p3.json").write_text(json.dumps({"epics": []}))

    calls = []
    monkeypatch.setattr(
        p, "advance_pipeline",
        lambda plan_name: calls.append(plan_name) or {"ok": True},
    )

    result = p.advance_all_plans()
    assert calls == []
    assert result["plans"] == {}


def test_check_story_status_passing_tests_is_not_done(plan_dir, monkeypatch):
    """"done" must mean merged. A story whose tests just passed is only
    ready for review — conflating the two lets it both skip review (never
    retried, since advance_pipeline only re-checks "in_progress" stories)
    and falsely satisfy other stories' dependency gate before it merges."""
    _write_manifest(plan_dir, "cs", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(plan_dir / "wt")},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    # Pretend the agent wrote real commits — this test is about the
    # `tests_passed` vs `done` distinction, not the empty-branch gate
    # (covered separately by test_check_story_status_*_no_commits).
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)

    class Result:
        stdout = ""
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("cs", "S1")
    assert result["status"] == "tests_passed"
    manifest = _read_manifest(plan_dir, "cs")
    assert manifest["stories"]["S1"]["status"] == "tests_passed"


def test_check_story_status_fails_when_agent_made_no_commits(
    plan_dir, monkeypatch,
):
    """The empty-branch guard: tests passing against an untouched
    worktree (e.g. main's suite against an empty branch because devstral
    parked in a repetition loop without writing code) is NOT the task
    being done. Mark `failed`, not `tests_passed`, so the dashboard
    doesn't count empty branches as success."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("[step 0] bash: pwd\n")
    _write_manifest(plan_dir, "fp", {
        "S1": {"summary": "thing", "status": "in_progress",
               "pid": 4242, "worktree": str(worktree)},
    })
    monkeypatch.setattr(p.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: False)

    class Result:
        stdout = ""
        returncode = 0

    monkeypatch.setattr(p.subprocess, "run", lambda *a, **k: Result())

    result = p.check_story_status("fp", "S1")
    assert result["status"] == "failed"
    assert result["reason"] == "empty_agent_branch"
    manifest = _read_manifest(plan_dir, "fp")
    story = manifest["stories"]["S1"]
    assert story["status"] == "failed"
    assert "no new commits" in story["failure_reason"]


