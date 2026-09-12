"""PLANNOTIFY-03: wire notify_if_plan_completed into all THREE mark-done sites.

Every place a story's status transitions to "done" must call
``pipeline.plan_completion.notify_if_plan_completed`` after the manifest is
saved:

  1. pipeline/ci.py::_mark_story_done_impl
  2. pipeline/advance.py::_adjudicate_merges (the scheduler's autonomous
     merge path - the main path in production)
  3. pipeline/merge.py::_approve_merge_impl (the human-triggered approve_merge
     path)

A lazy, function-local import is required at each site (not a module-top
import) so the call works whether the function body is invoked directly or
rebound into pipeline.server's namespace, and so pipeline.plan_completion
stays monkeypatchable at its own module path - see CLAUDE.md's Agent
Workflow / this story's brief for the rationale.

The source-level guard is membership-only per module (not a total count),
since a later story may legitimately add more references.
"""
# ruff: noqa: I001 - import order below is deliberate, not disorganized:
# `from pipeline import server as p` must run BEFORE `import pipeline.ci`
# et al. so pipeline.server (which transitively imports ci/advance/merge at
# module load) finishes initializing first; isort's alphabetical sort would
# put pipeline.ci ahead of pipeline.server and reintroduce the circular
# import this ordering avoids.
import inspect
import json
from pathlib import Path

import pytest

# pipeline.server transitively imports ci/advance/merge/plan_completion at
# module load; import it first so those submodule imports below resolve
# against already-initialized modules instead of tripping a circular import.
from pipeline import server as p

import pipeline.advance as adv
import pipeline.ci as pci
import pipeline.merge as pmerge
import pipeline.plan_completion as ppc
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _NullSetStateProvider,
    _read_manifest,
    _story,
    _write_manifest,
    plan_dir,
)


# ---------------------------------------------------------------------------
# Source-level guard: cheap, catches a partial migration outright.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "module",
    [pci, adv, pmerge],
    ids=["pipeline.ci", "pipeline.advance", "pipeline.merge"],
)
def test_notify_if_plan_completed_referenced_in_module_source(module):
    src = inspect.getsource(module)
    assert "notify_if_plan_completed" in src, (
        f"{module.__name__} must call "
        f"pipeline.plan_completion.notify_if_plan_completed after marking "
        f"a story done; found no reference in its source"
    )


# ---------------------------------------------------------------------------
# 1. pipeline/ci.py::_mark_story_done_impl (via the public mark_story_done)
# ---------------------------------------------------------------------------
def test_mark_story_done_final_story_calls_notify_if_plan_completed(
    plan_dir, monkeypatch  # noqa: F811
):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    calls = []
    monkeypatch.setattr(
        ppc, "notify_if_plan_completed",
        lambda plan_name, manifest: calls.append((plan_name, manifest)),
    )
    _write_manifest(plan_dir, "notify-final", {
        "S1": {"status": "done"},
        "S2": {"status": "todo"},
    })

    result = p.mark_story_done("notify-final", "S2")

    assert result.get("plan_completed") is True
    assert len(calls) == 1
    called_plan_name, called_manifest = calls[0]
    assert called_plan_name == "notify-final"
    assert called_manifest["stories"]["S2"]["status"] == "done"


def test_mark_story_done_non_final_story_still_calls_notify(
    plan_dir, monkeypatch  # noqa: F811
):
    """The detector, not the call site, decides whether the plan is
    complete - so a non-final done transition must still invoke it, and the
    caller's return value must stay unchanged."""
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    calls = []
    monkeypatch.setattr(
        ppc, "notify_if_plan_completed",
        lambda plan_name, manifest: calls.append((plan_name, manifest)),
    )
    _write_manifest(plan_dir, "notify-nonfinal", {
        "S1": {"status": "todo"},
        "S2": {"status": "in_progress"},
    })

    result = p.mark_story_done("notify-nonfinal", "S1")

    assert result == {"ok": True}
    assert len(calls) == 1
    assert calls[0][0] == "notify-nonfinal"


def test_mark_story_done_survives_notify_raising(
    plan_dir, monkeypatch  # noqa: F811
):
    """Defence in depth: even if notify_if_plan_completed somehow raised, the
    mark-done path must not unwrap that into a broken caller result."""
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())

    def _boom(plan_name, manifest):
        raise RuntimeError("boom")

    monkeypatch.setattr(ppc, "notify_if_plan_completed", _boom)
    _write_manifest(plan_dir, "notify-raises", {
        "S1": {"status": "todo"},
    })

    result = p.mark_story_done("notify-raises", "S1")

    assert result.get("ok") is True
    assert result.get("plan_completed") is True
    manifest = _read_manifest(plan_dir, "notify-raises")
    assert manifest["stories"]["S1"]["status"] == "done"


# ---------------------------------------------------------------------------
# 2. pipeline/advance.py::_adjudicate_merges (scheduler autonomous path)
# ---------------------------------------------------------------------------
class _FakeBackend:
    def get_backend(self, role, name=None):
        return self

    def resource_status(self, model_tag=None):
        return {"ok": True, "reason": ""}


def _pr_open_story():
    return {
        "summary": "pr open story",
        "status": "pr_open",
        "worktree": "/nonexistent-plannotify03-worktree",
        "dependencies": [],
    }


def _stub_advance_merge_boundaries(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 4)
    monkeypatch.setattr(
        p, "_role_resource_ok", lambda role, plan_role_config=None: (True, ""),
    )
    monkeypatch.setattr(p, "backend", _FakeBackend())
    monkeypatch.setattr(
        p, "check_story_status", lambda plan, key: {"status": "running"},
    )
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kw: None)
    monkeypatch.setattr(p, "dispatch_story", lambda plan, key: None)
    monkeypatch.setattr(p, "review_story", lambda plan, key: None)
    monkeypatch.setattr(
        p, "interrupt_story", lambda plan, key: {"ok": True},
    )
    monkeypatch.setattr(
        p, "_merge_decision", lambda story: {"action": "merge", "reason": ""},
    )
    monkeypatch.setattr(
        p, "_rebase_and_push_for_merge",
        lambda plan, key, branch, worktree: ("", "abc123"),
    )
    monkeypatch.setattr(
        p, "_merge_gate_ci_status",
        lambda branch, *, sha: {"state": "success", "error": ""},
    )
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(p, "_ci_pending_expired", lambda since: False)
    monkeypatch.setattr(
        p, "_ci_rework_feedback",
        lambda gate_error, attempts: "ci rework feedback",
    )
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda worktree, ref: False,
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_merge_pr", lambda worktree, key: None)
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan: None)
    monkeypatch.setattr(p, "_maybe_record_retro", lambda plan, manifest: None)
    monkeypatch.setattr(p, "_mcp_restart_notice", lambda touched: "restart")
    monkeypatch.setattr(
        p, "_atomic_write_json",
        lambda path, data: Path(path).write_text(json.dumps(data, indent=2)),
    )
    monkeypatch.setattr(adv, "_count_on_device_in_progress_agents", lambda: 0)


def test_advance_merge_path_calls_notify_if_plan_completed(
    plan_dir, monkeypatch  # noqa: F811
):
    _stub_advance_merge_boundaries(monkeypatch)
    calls = []
    monkeypatch.setattr(
        ppc, "notify_if_plan_completed",
        lambda plan_name, manifest: calls.append((plan_name, manifest)),
    )
    _write_manifest(plan_dir, "notify-advance", {"P1": _pr_open_story()})

    result = p.advance_pipeline("notify-advance")

    assert result["ok"] is True
    assert result["merged"] == ["P1"]
    assert len(calls) == 1
    called_plan_name, called_manifest = calls[0]
    assert called_plan_name == "notify-advance"
    assert called_manifest["stories"]["P1"]["status"] == "done"


def test_advance_merge_path_survives_notify_raising(
    plan_dir, monkeypatch  # noqa: F811
):
    _stub_advance_merge_boundaries(monkeypatch)

    def _boom(plan_name, manifest):
        raise RuntimeError("boom")

    monkeypatch.setattr(ppc, "notify_if_plan_completed", _boom)
    _write_manifest(plan_dir, "notify-advance-raises", {"P1": _pr_open_story()})

    result = p.advance_pipeline("notify-advance-raises")

    assert result["ok"] is True
    assert result["merged"] == ["P1"]
    manifest = _read_manifest(plan_dir, "notify-advance-raises")
    assert manifest["stories"]["P1"]["status"] == "done"


# ---------------------------------------------------------------------------
# 3. pipeline/merge.py::_approve_merge_impl (human-triggered approve_merge)
# ---------------------------------------------------------------------------
def _stub_approve_merge_boundaries(monkeypatch):
    monkeypatch.setattr(
        p, "_rebase_onto_master",
        lambda *a, **k: {"ok": True, "auto_resolved": False},
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(
        p, "_ci_status", lambda *a, **k: {"state": "success", "error": ""},
    )
    monkeypatch.setattr(
        p, "_reverify_acceptance", lambda *a, **k: {"state": "pass"},
    )
    monkeypatch.setattr(p, "_reverify_build", lambda *a, **k: {"state": "pass"})
    monkeypatch.setattr(p, "_merge_pr", lambda *a, **k: "merged")
    monkeypatch.setattr(p, "_ci_rerun", lambda *a, **k: None)
    monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda *a, **k: [])
    monkeypatch.setattr(p, "_maybe_record_retro", lambda plan, manifest: None)


def test_approve_merge_calls_notify_if_plan_completed(
    plan_dir, monkeypatch  # noqa: F811
):
    _stub_approve_merge_boundaries(monkeypatch)
    calls = []
    monkeypatch.setattr(
        ppc, "notify_if_plan_completed",
        lambda plan_name, manifest: calls.append((plan_name, manifest)),
    )
    _write_manifest(plan_dir, "notify-approve", {
        "S1": _story(status="parked", review_verdict="APPROVE", worktree="/nonexistent-plannotify03-approve-worktree"),
    })

    result = p.approve_merge("notify-approve", "S1")

    assert result.get("ok") is True, result
    assert len(calls) == 1
    called_plan_name, called_manifest = calls[0]
    assert called_plan_name == "notify-approve"
    assert called_manifest["stories"]["S1"]["status"] == "done"


def test_approve_merge_survives_notify_raising(
    plan_dir, monkeypatch  # noqa: F811
):
    _stub_approve_merge_boundaries(monkeypatch)

    def _boom(plan_name, manifest):
        raise RuntimeError("boom")

    monkeypatch.setattr(ppc, "notify_if_plan_completed", _boom)
    _write_manifest(plan_dir, "notify-approve-raises", {
        "S1": _story(status="parked", review_verdict="APPROVE", worktree="/nonexistent-plannotify03-approve-worktree"),
    })

    result = p.approve_merge("notify-approve-raises", "S1")

    assert result.get("ok") is True, result
    manifest = _read_manifest(plan_dir, "notify-approve-raises")
    assert manifest["stories"]["S1"]["status"] == "done"
