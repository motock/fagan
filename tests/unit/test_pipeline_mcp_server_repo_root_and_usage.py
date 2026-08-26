"""Tests for the pipeline MCP server: per-plan repo_root, the usage probe, atomic writes, usage-gate CLI format, and bounded fail-closed usage gating.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import json
import subprocess
from pathlib import Path

import pytest

from app import (
    backend,
    role_registry,
)
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

# ---------- advance_pipeline orchestration ----------

def test_advance_pipeline_does_not_report_skipped_locked_as_dispatched(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "skipplan", {"PIPE-9": {"status":"todo"}})
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: {"ok": True, "skipped": "locked"})
    result = p.advance_pipeline("skipplan")
    assert "PIPE-9" not in result.get("dispatched", [])

# advance_pipeline is a coordinator; the per-story operations (dispatch_story,
# check_story_status, review_story, gh merge) are exercised by their own tests
# above, so here we substitute test doubles to verify routing and gating.
def test_advance_pipeline_dry_run_has_no_side_effects(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "dry-run")
    _write_manifest(plan_dir, "dr", {
        "T1": {"summary": "todo one", "status": "todo", "dependencies": []},
        "P1": {"summary": "ready pr", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
    })

    def _boom(*a, **k):
        raise AssertionError("dry-run must not take actions")

    monkeypatch.setattr(p, "dispatch_story", _boom)
    monkeypatch.setattr(p, "_merge_pr", _boom)

    result = p.advance_pipeline("dr")
    assert result["dry_run"] is True
    assert "T1" in result["would_dispatch"]
    assert "P1" in result["would_merge_decisions"]


def test_advance_pipeline_gated_dispatches_merges_and_parks(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "go", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
        "P1": {"summary": "low approved", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
        "P2": {"summary": "high approved", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "high", "worktree": "/y"},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result = p.advance_pipeline("go")
    assert dispatched == ["T1"]
    assert merged == ["P1"]
    assert "P1" in result["merged"]
    assert "P2" in result["parked"]
    assert "P2" in result["notify"]

    manifest = _read_manifest(plan_dir, "go")
    assert manifest["stories"]["P1"]["status"] == "done"
    assert manifest["stories"]["P2"]["status"] == "parked"


def test_advance_pipeline_merge_transitions_plane_issue_to_done(plan_dir, monkeypatch):
    # A story merged via advance_pipeline's automatic path must move the
    # Plane issue to Done too - otherwise it stays "In Progress" forever,
    # since dispatch_story is the only other place that touches Plane state.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    story_key = "11111111-1111-1111-1111-111111111111"
    _write_manifest(plan_dir, "planedone", {
        story_key: {"summary": "approved", "status": "pr_open",
                     "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(pt, "_get_state", lambda group: f"state-{group}")

    patches = []
    monkeypatch.setattr(pt, "plane_request",
        lambda method, path, **kw: patches.append((method, path, kw)),
    )

    p.advance_pipeline("planedone")

    assert ("PATCH", f"/projects/{pt.PLANE_PROJECT}/work-items/{story_key}/",
            {"json": {"state": "state-completed"}}) in patches


def test_advance_pipeline_merge_tolerates_plane_failure(plan_dir, monkeypatch):
    # Mirrors dispatch_story's resilience: not every plan is Plane-backed, so
    # a Plane error must not block the local merge from completing.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "planefail", {
        "P1": {"summary": "approved", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")),
    )

    result = p.advance_pipeline("planefail")

    assert result["merged"] == ["P1"]
    assert _read_manifest(plan_dir, "planefail")["stories"]["P1"]["status"] == "done"


def test_approve_merge_merges_a_parked_approved_story(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "am", {
        "P1": {"summary": "approved but medium risk", "status": "parked",
               "review_verdict": "APPROVE", "risk": "medium", "worktree": "/x"},
    })
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    plane_calls = []
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: plane_calls.append(key))

    result = p.approve_merge("am", "P1")

    assert result["ok"] is True
    assert result["status"] == "done"
    assert merged == ["P1"]
    assert plane_calls == ["P1"]
    assert _read_manifest(plan_dir, "am")["stories"]["P1"]["status"] == "done"


def test_approve_merge_merges_a_pr_open_approved_story(plan_dir, monkeypatch):
    # A human may approve before the gate even runs (status still pr_open),
    # not only after it's been parked.
    _write_manifest(plan_dir, "am2", {
        "P1": {"summary": "approved, not yet adjudicated", "status": "pr_open",
               "review_verdict": "APPROVE", "risk": "high", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.approve_merge("am2", "P1")

    assert result["ok"] is True
    assert _read_manifest(plan_dir, "am2")["stories"]["P1"]["status"] == "done"


def test_approve_merge_rejects_story_without_approve_verdict(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "am3", {
        "P1": {"summary": "changes requested", "status": "parked",
               "review_verdict": "REQUEST_CHANGES", "risk": "medium", "worktree": "/x"},
    })
    merge_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merge_calls.append(key))

    result = p.approve_merge("am3", "P1")

    assert result["ok"] is False
    assert merge_calls == []
    assert _read_manifest(plan_dir, "am3")["stories"]["P1"]["status"] == "parked"


@pytest.mark.parametrize("status", ["todo", "in_progress", "done", "failed", "interrupted"])
def test_approve_merge_rejects_story_in_non_mergeable_status(plan_dir, monkeypatch, status):
    _write_manifest(plan_dir, "am4", {
        "P1": {"summary": "not ready", "status": status,
               "review_verdict": "APPROVE", "risk": "low", "worktree": "/x"},
    })
    merge_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merge_calls.append(key))

    result = p.approve_merge("am4", "P1")

    assert result["ok"] is False
    assert merge_calls == []


def test_approve_merge_unknown_story_returns_error(plan_dir):
    _write_manifest(plan_dir, "am5", {})
    result = p.approve_merge("am5", "nope")
    assert result["ok"] is False


def test_approve_merge_uses_plan_repo_root(plan_dir, monkeypatch, tmp_path):
    real_repo = tmp_path / "real-repo"
    monkeypatch.setattr(p, "REPO_ROOT", p.Path("/wrong/default/repo"))
    _write_manifest(plan_dir, "am6", {
        "P1": {"summary": "approved", "status": "parked", "review_verdict": "APPROVE",
               "risk": "medium", "worktree": str(tmp_path / "wt")},
    })
    manifest_path = plan_dir / "am6.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repo_root"] = str(real_repo)
    manifest_path.write_text(json.dumps(manifest))

    seen_repo_roots = []

    def _fake_merge_pr(wt, key):
        seen_repo_roots.append(p.REPO_ROOT)
        return "merged"

    monkeypatch.setattr(p, "_merge_pr", _fake_merge_pr)
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    p.approve_merge("am6", "P1")

    assert seen_repo_roots == [real_repo]
    assert p.REPO_ROOT == p.Path("/wrong/default/repo")


def test_advance_pipeline_paused_interrupts_running_and_skips_new_work(
    plan_dir, usage_state_path, monkeypatch,
):
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 10, "paused": True}))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    # This test's manifest has no role_config, so review resolution falls to
    # the registry (pipeline/usage.py's _role_resource_ok). Pin the registry
    # to claude explicitly rather than relying on whatever model_registry.json
    # happens to say on disk - the scenario this test wants is "review runs
    # on Claude and Claude is gated", not "review runs on whatever the live
    # registry currently defaults to".
    monkeypatch.setattr(
        role_registry, "load_registry",
        lambda *a, **k: {"providers": {"claude": {"models": {}}}, "roles": {}},
    )
    _write_manifest(plan_dir, "pause", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
        "R1": {"summary": "running", "status": "in_progress", "pid": 111, "worktree": "/x"},
        "TP1": {"summary": "awaiting review", "status": "tests_passed",
                "worktree": "/y", "risk": "low"},
    })

    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    reviewed = []
    monkeypatch.setattr(
        p, "review_story",
        lambda plan, key: reviewed.append(key) or {"status": "pr_open"},
    )
    interrupted = []
    monkeypatch.setattr(
        p, "interrupt_story",
        lambda plan, key: interrupted.append(key) or {"ok": True},
    )

    result = p.advance_pipeline("pause")

    assert result["paused"] is True
    assert dispatched == []
    assert reviewed == []
    assert interrupted == ["R1"]
    assert "R1" in result["interrupted"]


def test_role_resource_ok_auto_does_not_crash_and_is_ok_when_local_available(
    usage_state_path, monkeypatch,
):
    """PIPELINE_BACKEND_<ROLE>=auto must not reach get_backend with the literal
    'auto' (which raises ValueError). Under auto the role can take work whenever
    the local backend is healthy, even with Claude's usage gate tripped."""
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 95, "paused": True}))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(backend.OllamaDriver, "resource_status",
                        lambda self: {"ok": True, "reason": ""})

    ok, reason = p._role_resource_ok("dispatch")

    assert ok is True
    assert reason == ""


def test_role_resource_ok_auto_gated_only_when_both_backends_unavailable(
    usage_state_path, monkeypatch,
):
    """Under auto, the role is gated only when BOTH local and Claude are down;
    it then surfaces Claude's gate reason."""
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 95, "paused": True}))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    monkeypatch.setattr(backend.OllamaDriver, "resource_status",
                        lambda self: {"ok": False, "reason": "Ollama unreachable"})

    ok, reason = p._role_resource_ok("dispatch")

    assert ok is False
    assert reason == "Claude usage gate tripped"


def test_role_resource_ok_review_uses_plan_role_config_backend_not_claude(
    usage_state_path, monkeypatch,
):
    """A plan that pins review to a local provider (e.g. ollama/glm) must be
    gated by THAT provider's resource_status(), not Claude's usage poller. With
    Claude's gate tripped (session paused) but Ollama healthy, review must be
    ok — this is the mode30 E2E incident: review was permanently deferred as
    review_paused while Claude usage sat at 100% even though review never
    touches Claude."""
    usage_state_path.write_text(json.dumps({"session_pct": 100, "week_pct": 100, "paused": True}))
    monkeypatch.setattr(backend.OllamaDriver, "resource_status",
                        lambda self: {"ok": True, "reason": ""})

    ok, reason = p._role_resource_ok(
        "review", plan_role_config={"review": {"provider": "ollama", "model": "glm"}}
    )

    assert ok is True
    assert reason == ""


def test_role_resource_ok_review_plan_role_config_gates_when_local_down(
    usage_state_path, monkeypatch,
):
    """Negative side: when the plan-pinned review backend (ollama) is down,
    review must be gated with that backend's reason — even if Claude is
    healthy. The gate follows the plan role_config, not the env default."""
    usage_state_path.write_text(json.dumps({"session_pct": 5, "week_pct": 5, "paused": False}))
    monkeypatch.setattr(backend.OllamaDriver, "resource_status",
                        lambda self: {"ok": False, "reason": "Ollama unreachable"})

    ok, reason = p._role_resource_ok(
        "review", plan_role_config={"review": {"provider": "ollama", "model": "glm"}}
    )

    assert ok is False
    assert reason == "Ollama unreachable"


def test_role_resource_ok_review_garbage_plan_provider_fails_open_to_env(
    usage_state_path, monkeypatch,
):
    """A garbage provider in plan role_config must not crash advance_pipeline:
    the gate fails open to the env-based path. With env review=claude and
    Claude's gate tripped, that fallback yields ok=False (Claude's reason) —
    the point is it doesn't raise and it doesn't silently ok=True."""
    usage_state_path.write_text(json.dumps({"session_pct": 100, "week_pct": 100, "paused": True}))
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "claude")

    ok, reason = p._role_resource_ok(
        "review", plan_role_config={"review": {"provider": "not-a-real-backend"}}
    )

    assert ok is False
    assert reason == "Claude usage gate tripped"


def test_role_resource_ok_dispatch_ignores_plan_role_config(
    usage_state_path, monkeypatch,
):
    """dispatch's real backend is per-story via _route_dispatch_backend
    (env-local-first), NOT role_registry — so plan_role_config must NOT
    redirect the dispatch gate to a registry provider. With env dispatch=local
    and a plan role_config that pins review (not dispatch), the dispatch gate
    still checks the local backend and ignores the plan config entirely."""
    usage_state_path.write_text(json.dumps({"session_pct": 100, "week_pct": 100, "paused": True}))
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(backend.OllamaDriver, "resource_status",
                        lambda self: {"ok": True, "reason": ""})

    ok, reason = p._role_resource_ok(
        "dispatch", plan_role_config={"review": {"provider": "ollama", "model": "glm"}}
    )

    assert ok is True
    assert reason == ""


def test_list_ready_stories_resolves_summary_dependencies(plan_dir):
    """Dependencies expressed as a prerequisite's summary string (the documented
    save_plan schema) must resolve against done stories even when the manifest is
    keyed by UUID rather than by summary."""
    _write_manifest(plan_dir, "sdep", {
        "uuid-a": {"summary": "Foundation", "status": "done", "dependencies": []},
        "uuid-b": {"summary": "Builds on foundation", "status": "todo",
                   "dependencies": ["Foundation"]},
        "uuid-c": {"summary": "Blocked", "status": "todo",
                   "dependencies": ["Builds on foundation"]},
    })

    ready = p.list_ready_stories("sdep")

    assert [r["summary"] for r in ready] == ["Builds on foundation"]


def test_advance_pipeline_dispatches_story_with_summary_dependency(plan_dir, monkeypatch):
    """The dispatch tick must treat a satisfied summary-string dependency as met
    and dispatch the dependent story, not silently skip it forever."""
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""))
    _write_manifest(plan_dir, "adep", {
        "uuid-a": {"summary": "Foundation", "status": "done", "dependencies": []},
        "uuid-b": {"summary": "Next", "status": "todo", "dependencies": ["Foundation"]},
    })
    dispatched = []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "in_progress"})

    p.advance_pipeline("adep")

    assert dispatched == ["uuid-b"]


def test_advance_pipeline_local_dispatch_runs_while_claude_review_gated(
    plan_dir, usage_state_path, monkeypatch,
):
    """Step 5: dispatch on local + review on Claude. Claude usage is maxed,
    but local dispatch must still proceed (and not interrupt running local
    agents); only the Claude-backed review is deferred."""
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 10, "paused": True}))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.delenv("PIPELINE_BACKEND_REVIEW", raising=False)  # -> claude
    # Local backend reports healthy without hitting a real Ollama.
    monkeypatch.setattr(backend.OllamaDriver, "resource_status", lambda self: {"ok": True, "reason": ""})
    # This test's manifest has no role_config, so review resolution falls to
    # the registry. Pin it to claude explicitly - the scenario under test is
    # "review runs on Claude and Claude is gated", not whatever the live
    # model_registry.json on disk currently defaults review to.
    monkeypatch.setattr(
        role_registry, "load_registry",
        lambda *a, **k: {"providers": {"claude": {"models": {}}}, "roles": {}},
    )

    _write_manifest(plan_dir, "split", {
        "T1": {"summary": "todo", "status": "todo", "dependencies": []},
        "R1": {"summary": "running", "status": "in_progress", "pid": 111, "worktree": "/x"},
        "TP1": {"summary": "awaiting review", "status": "tests_passed", "worktree": "/y", "risk": "low"},
    })
    dispatched, reviewed, interrupted = [], [], []
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: dispatched.append(key))
    monkeypatch.setattr(p, "review_story", lambda plan, key: reviewed.append(key) or {"status": "pr_open"})
    monkeypatch.setattr(p, "interrupt_story", lambda plan, key: interrupted.append(key) or {"ok": True})
    monkeypatch.setattr(p, "check_story_status", lambda plan, key: {"status": "in_progress"})

    result = p.advance_pipeline("split")

    assert result["dispatch_paused"] is False     # local dispatch not gated by Claude
    assert result["review_paused"] is True         # Claude review deferred
    assert dispatched == ["T1"]                     # dispatch proceeded
    assert interrupted == []                         # running local agent left alone
    assert reviewed == []                            # review deferred


def test_advance_pipeline_paused_still_processes_merges(plan_dir, usage_state_path, monkeypatch):
    usage_state_path.write_text(json.dumps({"session_pct": 95, "week_pct": 10, "paused": True}))
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "pausemerge", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    merged = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")

    result = p.advance_pipeline("pausemerge")
    assert merged == ["P1"]
    assert "P1" in result["merged"]


def test_advance_pipeline_merge_failure_retries_within_budget(plan_dir, monkeypatch):
    # A transient _merge_pr failure (e.g. gh hiccup) must not crash the tick or
    # burn the story: it stays pr_open with a bumped attempt counter so the next
    # tick retries, and the user is notified.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "mergeretry", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x"},
    })
    monkeypatch.setattr(p, "_merge_pr",
                        lambda wt, key: (_ for _ in ()).throw(RuntimeError("gh hiccup")))

    result = p.advance_pipeline("mergeretry")

    story = _read_manifest(plan_dir, "mergeretry")["stories"]["P1"]
    assert story["status"] == "pr_open"
    assert story["merge_attempts"] == 1
    assert result["merged"] == []
    assert result["failed"] == []
    assert "P1" in result["notify"]


def test_advance_pipeline_merge_failure_exhausts_budget(plan_dir, monkeypatch):
    # Once the attempt budget is spent, a persistently failing merge becomes a
    # hard failure that needs human intervention rather than retrying forever.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "MERGE_MAX_ATTEMPTS", 3)
    _write_manifest(plan_dir, "mergegiveup", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "merge_attempts": 2},
    })
    monkeypatch.setattr(p, "_merge_pr",
                        lambda wt, key: (_ for _ in ()).throw(RuntimeError("still broken")))

    result = p.advance_pipeline("mergegiveup")

    story = _read_manifest(plan_dir, "mergegiveup")["stories"]["P1"]
    assert story["status"] == "failed"
    assert story["merge_attempts"] == 3
    assert "still broken" in story.get("merge_error", "")
    assert result["merged"] == []
    assert "P1" in result["failed"]
    assert "P1" in result["notify"]


def test_advance_pipeline_merge_success_clears_attempt_counter(plan_dir, monkeypatch):
    # A merge that finally succeeds after earlier failures must clear the
    # attempt counter so the story records a clean done.
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    _write_manifest(plan_dir, "mergerecover", {
        "P1": {"summary": "approved", "status": "pr_open", "review_verdict": "APPROVE",
               "risk": "low", "worktree": "/x", "merge_attempts": 1},
    })
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)

    result = p.advance_pipeline("mergerecover")

    story = _read_manifest(plan_dir, "mergerecover")["stories"]["P1"]
    assert story["status"] == "done"
    assert "merge_attempts" not in story
    assert result["merged"] == ["P1"]


# ---- Mode 9: rebase-before-merge + CI gate -----------------------------


def test_rebase_onto_master_skips_when_worktree_missing(monkeypatch, tmp_path):
    # A missing/anomalous worktree cannot be rebased; the helper falls back to
    # ok so the gate degrades to CI + the original conflict-at-merge check
    # rather than blocking forever on a path that doesn't exist.
    monkeypatch.setattr(p, "REPO_ROOT", str(tmp_path))
    rb = p._rebase_onto_master(str(tmp_path / "does-not-exist"), "agent/x")
    assert rb["ok"] is True
    assert rb["conflict"] is False


def test_rebase_onto_master_reports_conflict(monkeypatch, tmp_path):
    # When `git rebase` fails on a conflict, the helper aborts the rebase and
    # reports conflict=True so the caller can park rather than retry blindly.
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").mkdir()  # make Path(wt).is_dir() true; git itself is faked

    def _fake_run(argv, cwd, **_):
        class R:
            returncode = 1
            stdout = ""
            stderr = "error: could not apply ... fix conflicts"
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p, "REPO_ROOT", str(tmp_path))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is False
    assert rb["conflict"] is True


def test_rebase_onto_master_uses_default_branch_on_main_repo(tmp_path, monkeypatch):
    # Gap from the live-`gh` probe (PROOF.md note #1): when the repo's default
    # branch is `main` (e.g. a fresh `gh repo create`), the rebase path must
    # rebase onto `origin/main`, not the hardcoded `origin/master`. A `main`-
    # default repo with no `master` ref would otherwise fail the rebase and
    # block every merge-gate attempt. Run against a real tmp git repo so the
    # `git rebase` invocation actually executes against the configured ref.
    # `wt` is a real `git worktree add` off `repo` (not a separate `git init`)
    # because that's the production invariant: the fetch into REPO_ROOT updates
    # `origin/main` for the worktree too, since they share `.git/`.
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    for d in (repo,):
        r = subprocess.run(["git", "init", "-q", "-b", "main", str(d)],
                           check=False, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        subprocess.run(["git", "config", "user.email", "t@e"],
                       cwd=d, capture_output=True, text=True, check=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=d, capture_output=True, text=True, check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)],
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)],
                   cwd=repo, capture_output=True, text=True, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"],
                   cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "remote", "set-head", "origin", "main"],
                   cwd=repo, capture_output=True, text=True, check=True)
    # Real worktree off `repo` so it shares `.git/`. A new commit on
    # `agent/x` from the worktree gives the rebase something to fast-forward.
    r = subprocess.run(["git", "worktree", "add", "-b", "agent/x", str(wt)],
                       check=False, cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    (wt / "new.txt").write_text("agent edit\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "agent edit"], cwd=wt,
                   capture_output=True, text=True, check=True)
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is True, f"rebase failed: {rb}"
    assert rb["conflict"] is False


def _setup_conflict_repo(tmp_path, files_base, agent_edits, master_edits):
    """Build repo+bare origin+worktree with a base commit (`files_base`: full
    file contents), then a divergent commit on the worktree's `agent/x`
    branch (`agent_edits`: full new file contents) and a divergent commit
    pushed to `origin/master` (`master_edits`: full new file contents) - so
    rebasing `agent/x` onto `origin/master` conflicts on every file present
    in both edit dicts. Returns (repo, wt) Paths."""
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)],
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo,
                   capture_output=True, text=True, check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "master", str(origin)],
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=repo,
                   capture_output=True, text=True, check=True)
    for name, content in files_base.items():
        (repo / name).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "master"], cwd=repo,
                   capture_output=True, text=True, check=True)

    r = subprocess.run(["git", "worktree", "add", "-b", "agent/x", str(wt)],
                       check=False, cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for name, content in agent_edits.items():
        (wt / name).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "agent edit"], cwd=wt,
                   capture_output=True, text=True, check=True)

    for name, content in master_edits.items():
        (repo / name).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "master edit"], cwd=repo,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "push", "-q", "origin", "master"], cwd=repo,
                   capture_output=True, text=True, check=True)
    return repo, wt


def test_rebase_auto_resolves_additive_import_conflict(tmp_path, monkeypatch):
    # The primary positive case: two branches each add a distinct import line
    # at the same anchor point in a shared file. Must auto-resolve (union of
    # both added lines) and continue the rebase rather than aborting.
    base = "import os\n\n\ndef foo():\n    pass\n"
    agent = "import os\nimport sys\n\n\ndef foo():\n    pass\n"
    master = "import os\nimport json\n\n\ndef foo():\n    pass\n"
    repo, wt = _setup_conflict_repo(
        tmp_path, {"shared.py": base}, {"shared.py": agent}, {"shared.py": master},
    )
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is True, f"expected auto-resolved rebase, got {rb}"
    assert rb["conflict"] is False
    assert rb.get("auto_resolved") is True
    result = (wt / "shared.py").read_text()
    assert "import sys" in result
    assert "import json" in result


def test_rebase_aborts_when_a_side_modifies_existing_line(tmp_path, monkeypatch):
    # A conflict where one side modifies a PRE-EXISTING line (not a pure
    # addition) must never auto-resolve - unchanged current behavior: abort.
    base = "import os\n\n\ndef foo():\n    pass\n"
    agent = "import os\nimport sys\n\n\ndef foo():\n    pass\n"
    master = "import os as o\n\n\ndef foo():\n    pass\n"
    repo, wt = _setup_conflict_repo(
        tmp_path, {"shared.py": base}, {"shared.py": agent}, {"shared.py": master},
    )
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is False
    assert rb["conflict"] is True
    assert not rb.get("auto_resolved")


def test_rebase_aborts_on_non_import_conflicting_lines(tmp_path, monkeypatch):
    # A conflict on added lines that are NOT import/use statements must never
    # auto-resolve, even though both sides are pure additions.
    base = "import os\n\n\ndef foo():\n    pass\n"
    agent = "import os\nx = 1\n\n\ndef foo():\n    pass\n"
    master = "import os\nx = 2\n\n\ndef foo():\n    pass\n"
    repo, wt = _setup_conflict_repo(
        tmp_path, {"shared.py": base}, {"shared.py": agent}, {"shared.py": master},
    )
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is False
    assert rb["conflict"] is True
    assert not rb.get("auto_resolved")


def test_rebase_aborts_all_or_nothing_across_multiple_files(tmp_path, monkeypatch):
    # A clean additive-import conflict in one file plus a disqualifying
    # conflict in another must abort the WHOLE rebase - no partial per-file
    # resolution.
    base_a = "import os\n\n\ndef foo():\n    pass\n"
    base_b = "import os\n\n\ndef bar():\n    pass\n"
    agent = {
        "a.py": "import os\nimport sys\n\n\ndef foo():\n    pass\n",
        "b.py": "import os\nimport sys\n\n\ndef bar():\n    pass\n",
    }
    master = {
        "a.py": "import os\nimport json\n\n\ndef foo():\n    pass\n",
        # Same anchor line as agent's edit (right after "import os") so this
        # genuinely conflicts, but it MODIFIES the existing line instead of
        # purely adding one - the disqualifying edit for this file.
        "b.py": "import os as o\n\n\ndef bar():\n    pass\n",
    }
    repo, wt = _setup_conflict_repo(
        tmp_path, {"a.py": base_a, "b.py": base_b}, agent, master,
    )
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is False
    assert rb["conflict"] is True
    assert not rb.get("auto_resolved")


def test_auto_resolve_conflict_disqualifies_on_write_failure(tmp_path, monkeypatch):
    # A write failure (ENOSPC, EROFS, quota, etc.) while applying an
    # otherwise-eligible additive-import resolution must disqualify the step
    # (return []) rather than propagate and leave the worktree mid-rebase.
    base = "import os\n\n\ndef foo():\n    pass\n"
    agent = "import os\nimport sys\n\n\ndef foo():\n    pass\n"
    master = "import os\nimport json\n\n\ndef foo():\n    pass\n"
    repo, wt = _setup_conflict_repo(
        tmp_path, {"shared.py": base}, {"shared.py": agent}, {"shared.py": master},
    )
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    subprocess.run(["git", "fetch", "origin"], cwd=wt, capture_output=True, text=True, check=True)
    r = subprocess.run(["git", "rebase", "origin/master"], check=False, cwd=wt, capture_output=True, text=True)
    assert r.returncode != 0, "expected the rebase to conflict"

    def _boom(self, *a, **kw):
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = p._try_auto_resolve_conflict(str(wt))
    assert result == []


def test_rebase_no_conflict_has_no_auto_resolved_key(tmp_path, monkeypatch):
    # Regression check: a normal, non-conflicting rebase must keep returning
    # its existing shape - no `auto_resolved` key at all for the common case.
    repo, wt = _setup_conflict_repo(
        tmp_path,
        {"a.py": "x = 1\n", "b.py": "y = 1\n"},
        {"a.py": "x = 1\nx2 = 2\n"},
        {"b.py": "y = 1\ny2 = 2\n"},
    )
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    rb = p._rebase_onto_master(str(wt), "agent/x")
    assert rb["ok"] is True
    assert rb["conflict"] is False
    assert "auto_resolved" not in rb


def test_ci_status_none_when_gh_unavailable(monkeypatch):
    # No PR / no gh -> state "none" is treated as pass so repos without CI are
    # not blocked. A non-zero gh exit (no checks for the branch) maps here too.
    def _fake_run(argv, **_):
        class R:
            returncode = 1
            stdout = ""
            stderr = "no checks found"
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    ci = p._ci_status("agent/x", sha="")
    assert ci["state"] == "none"


def test_ci_status_fail_on_fail_bucket(monkeypatch):
    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = json.dumps([{"bucket": "fail"}, {"bucket": "pass"}])
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_status("agent/x", sha="")["state"] == "fail"


def test_ci_status_pass_when_all_pass(monkeypatch):
    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = json.dumps([{"bucket": "pass"}, {"bucket": "pass"}])
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_status("agent/x", sha="")["state"] == "pass"


def test_ci_status_pending_times_out(monkeypatch):
    # A check that never reaches a terminal bucket must not hang the tick; it
    # returns "pending" after the timeout so the merge is retried later.
    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = json.dumps([{"bucket": "pending"}])
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p.time, "sleep", lambda _s: None)
    ci = p._ci_status("agent/x", sha="", timeout_s=0)
    assert ci["state"] == "pending"


def test_ci_status_returns_none_when_gh_missing(monkeypatch):
    # `gh` absent/non-executable raises OSError; the helper must honor its
    # never-raises contract and map that to "none" (treat as pass) rather than
    # escaping and crashing the scheduler tick.
    def _raise_run(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'gh'")

    monkeypatch.setattr(p.subprocess, "run", _raise_run)
    ci = p._ci_status("agent/x", sha="")
    assert ci["state"] == "none"
    assert "gh unavailable" in ci["error"]


def test_ci_status_none_when_no_workflows_dir_and_no_checks_reported(
    monkeypatch, tmp_path,
):
    # A repo with no .github/workflows genuinely has no CI: an empty checks
    # list must resolve straight to "none" (treated as pass), not block a
    # merge waiting for checks that will never appear.
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)

    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    ci = p._ci_status("agent/x", sha="")
    assert ci["state"] == "none"


def test_ci_status_pending_not_none_when_workflows_dir_present_but_checks_not_yet_registered(
    monkeypatch, tmp_path,
):
    # A repo WITH .github/workflows that reports zero checks yet must NOT be
    # treated as pass - the workflow run may just not have registered with
    # GitHub yet. Fix for the gap that let PR #48 merge with a red Linux CI
    # job that hadn't shown up in `gh pr checks` at merge time (2026-07-07
    # web-client-epic retro §4). Must poll (not fast-path) and land on
    # "pending", never silently "none"/pass, once the timeout elapses.
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    monkeypatch.setattr(p, "REPO_ROOT", tmp_path)

    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    monkeypatch.setattr(p.time, "sleep", lambda _s: None)
    # A tiny positive timeout (not 0): 0 would let the while loop's deadline
    # already be past on the first condition check, skipping the loop body
    # (and thus the gh call under test) entirely and falling through to
    # "pending" for free - passing even with the pre-fix "none" bug.
    ci = p._ci_status("agent/x", sha="", timeout_s=0.05)
    assert ci["state"] == "pending"


def test_ci_status_cancelled_when_only_cancelled_bucket(monkeypatch):
    # A job cancelled by an abnormal queue delay is a transient event worth
    # one auto-rerun, not a terminal failure - it must be distinguishable
    # from "fail" so callers can retry instead of giving up immediately.
    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = json.dumps([{"bucket": "cancelled"}, {"bucket": "pass"}])
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_status("agent/x", sha="")["state"] == "cancelled"


def test_ci_status_fail_wins_over_cancelled_when_both_present(monkeypatch):
    # A genuine failure alongside an unrelated cancelled job must still be
    # reported as "fail" - cancelled-only auto-rerun must never mask a real
    # test/lint failure.
    def _fake_run(argv, **_):
        class R:
            returncode = 0
            stdout = json.dumps([{"bucket": "cancelled"}, {"bucket": "fail"}])
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_status("agent/x", sha="")["state"] == "fail"


def test_ci_rerun_issues_gh_run_rerun_on_success(monkeypatch):
    # SHA-scoped (Mode 26): the run to rerun is looked up by the exact commit
    # SHA via `gh api .../actions/runs?head_sha=`, not by branch name.
    calls = []

    def _fake_run(argv, **_):
        calls.append(argv)
        class R:
            returncode = 0
            stdout = "12345" if argv[:2] == ["gh", "api"] else ""
            stderr = ""
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_rerun("deadbeef") is True
    assert any(a[:2] == ["gh", "api"] and "head_sha=deadbeef" in a[2] for a in calls)
    assert any(a[:3] == ["gh", "run", "rerun"] and "12345" in a for a in calls)
    assert any("--failed" in a for a in calls)


def test_ci_rerun_returns_false_when_gh_run_list_fails(monkeypatch):
    def _fake_run(argv, **_):
        class R:
            returncode = 1
            stdout = ""
            stderr = "no runs found"
        return R()

    monkeypatch.setattr(p.subprocess, "run", _fake_run)
    assert p._ci_rerun("agent/x") is False


def test_ci_rerun_returns_false_never_raises_when_gh_missing(monkeypatch):
    def _raise_run(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'gh'")

    monkeypatch.setattr(p.subprocess, "run", _raise_run)
    assert p._ci_rerun("agent/x") is False


