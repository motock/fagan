"""Unit tests for wiring pipeline/self_modification.py's helpers into
approve_merge (pipeline/server.py).

These are deliberately more granular than the read-only acceptance oracle in
tests/unit/test_acceptance_mcp_restart_approve_merge.py: they pin down the
re-export contract, the exact arguments detection must be called with, and
the negative/boundary cases where detection must NOT run at all (any
pre-merge gate failing before _merge_pr is ever reached).

Written before the implementation exists - expected to fail with an
AttributeError (no such attribute on pipeline.server yet) until the wiring
described in the mcp-self-mod-notice story lands, not a bug in this test
file's own logic.
"""
import json

import pytest

from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import self_modification as sm
from pipeline import server as p


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir, plan_name, story, story_key="P1"):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {story_key: story}}, indent=2)
    )


def _approved_parked_story(worktree="/x"):
    return {
        "summary": "approved but parked",
        "status": "parked",
        "review_verdict": "APPROVE",
        "risk": "medium",
        "worktree": worktree,
    }


# ---------------------------------------------------------------------------
# Re-export contract: server call sites use bare names, so the helpers must
# be the *same objects* the self_modification module defines, reachable as
# module-level attributes of pipeline.server (this is what lets
# monkeypatch.setattr(p, "_mcp_self_source_touched", ...) land on the name
# the call site actually reads).
# ---------------------------------------------------------------------------


def test_mcp_self_source_files_reexported_on_server():
    assert p.MCP_SELF_SOURCE_FILES is sm.MCP_SELF_SOURCE_FILES


def test_mcp_self_source_touched_reexported_on_server():
    assert p._mcp_self_source_touched is sm._mcp_self_source_touched


def test_mcp_restart_notice_reexported_on_server():
    assert p._mcp_restart_notice is sm._mcp_restart_notice


# ---------------------------------------------------------------------------
# Detection call arguments: must be invoked with the story's worktree and
# f"origin/{_default_branch()}" as base_ref.
# ---------------------------------------------------------------------------


def test_detection_is_called_with_worktree_and_default_branch_base_ref(
    plan_dir, monkeypatch
):
    _write_manifest(plan_dir, "amargs", _approved_parked_story(worktree="/x"))
    calls = []

    def _detect(worktree, base_ref):
        calls.append((worktree, base_ref))
        return []

    monkeypatch.setattr(p, "_mcp_self_source_touched", _detect)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: None)

    result = p.approve_merge("amargs", "P1")

    assert result["ok"] is True, result
    assert calls == [("/x", "origin/main")], calls


# ---------------------------------------------------------------------------
# Detection must NOT run when the merge never reaches _merge_pr - it sits
# right before that call, so anything that returns/fails earlier must never
# invoke it (and, transitively, never emit a reconnect notice).
# ---------------------------------------------------------------------------


def test_detection_not_invoked_when_review_verdict_is_not_approve(
    plan_dir, monkeypatch
):
    story = _approved_parked_story()
    story["review_verdict"] = "REQUEST_CHANGES"
    _write_manifest(plan_dir, "amrej", story)
    calls = []
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda wt, br: calls.append((wt, br)) or []
    )
    merge_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merge_calls.append(key))

    result = p.approve_merge("amrej", "P1")

    assert result["ok"] is False, result
    assert calls == []
    assert merge_calls == []


@pytest.mark.parametrize("status", ["todo", "in_progress", "done", "failed"])
def test_detection_not_invoked_when_story_status_not_mergeable(
    plan_dir, monkeypatch, status
):
    story = _approved_parked_story()
    story["status"] = status
    plan_name = f"amstat{status}"
    _write_manifest(plan_dir, plan_name, story)
    calls = []
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda wt, br: calls.append((wt, br)) or []
    )

    result = p.approve_merge(plan_name, "P1")

    assert result["ok"] is False, result
    assert calls == []


def test_detection_not_invoked_when_ci_check_fails(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "amci", _approved_parked_story())
    calls = []
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda wt, br: calls.append((wt, br)) or []
    )
    monkeypatch.setattr(
        p, "_ci_status", lambda branch, sha="": {"state": "fail", "error": "red"}
    )
    merge_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merge_calls.append(key))

    result = p.approve_merge("amci", "P1")

    assert result["ok"] is False, result
    assert "CI failing" in result["error"]
    assert calls == []
    assert merge_calls == []


def test_detection_not_invoked_when_acceptance_reverify_fails(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "amacc", _approved_parked_story())
    calls = []
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda wt, br: calls.append((wt, br)) or []
    )
    monkeypatch.setattr(
        p, "_ci_status", lambda branch, sha="": {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        p,
        "_reverify_acceptance",
        lambda story, worktree, story_key="": {"state": "fail", "error": "broke"},
    )
    merge_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merge_calls.append(key))

    result = p.approve_merge("amacc", "P1")

    assert result["ok"] is False, result
    assert "acceptance reverify fail" in result["error"]
    assert calls == []
    assert merge_calls == []


def test_detection_not_invoked_when_build_reverify_fails(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ambuild", _approved_parked_story())
    calls = []
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda wt, br: calls.append((wt, br)) or []
    )
    monkeypatch.setattr(
        p, "_ci_status", lambda branch, sha="": {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        p,
        "_reverify_acceptance",
        lambda story, worktree, story_key="": {"state": "pass", "error": ""},
    )
    monkeypatch.setattr(
        p, "_reverify_build", lambda worktree: {"state": "fail", "error": "broke"}
    )
    merge_calls = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merge_calls.append(key))

    result = p.approve_merge("ambuild", "P1")

    assert result["ok"] is False, result
    assert "build reverify fail" in result["error"]
    assert calls == []
    assert merge_calls == []


# ---------------------------------------------------------------------------
# Real (unmocked) detection helper wired through the actual call site,
# exercising self_modification's own fail-open behaviour end-to-end for a
# worktree that doesn't exist.
# ---------------------------------------------------------------------------


def test_no_reconnect_notice_when_worktree_missing_uses_real_detection(
    plan_dir, monkeypatch
):
    _write_manifest(plan_dir, "amreal", _approved_parked_story(worktree="/x"))
    notes = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))

    result = p.approve_merge("amreal", "P1")

    assert result["ok"] is True, result
    assert [n for n in notes if "/mcp reconnect" in n] == []


# ---------------------------------------------------------------------------
# Notification ordering and content, once a merge actually succeeds.
# ---------------------------------------------------------------------------


def test_notify_user_called_after_mark_plane_done(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "amorder2", _approved_parked_story())
    order = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(
        p, "_mark_plane_done", lambda key, plan=None: order.append("mark_done")
    )

    def _notify(plan, msg):
        if "/mcp reconnect" in msg:
            order.append("notify")

    monkeypatch.setattr(p, "_notify_user", _notify)
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda wt, br: ["pipeline/server.py"]
    )

    result = p.approve_merge("amorder2", "P1")

    assert result["ok"] is True, result
    assert order == ["mark_done", "notify"], order


def test_reconnect_notice_text_matches_helper_output(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "amtext", _approved_parked_story())
    notes = []
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda wt, br: ["pipeline/server.py"]
    )

    result = p.approve_merge("amtext", "P1")

    assert result["ok"] is True, result
    hits = [n for n in notes if "/mcp reconnect" in n]
    assert len(hits) == 1, notes
    assert hits[0] == p._mcp_restart_notice(["pipeline/server.py"])
