"""advance_pipeline's own merge loop must give the same MCP-self-modification
restart notice that approve_merge already gives (PR #215). This file covers
the wiring for the *other* merge path: the autonomous/scheduler loop inside
advance_pipeline.

Mocking pattern copied from test_advance_pipeline_merge_success_clears_attempt_counter
in test_pipeline_mcp_server.py: patch PIPELINE_AUTONOMY="gated",
PIPELINE_RISK_THRESHOLD="low", a manifest with one pr_open/APPROVE/risk:low
story with worktree "/x" (not a real dir, so the rebase/CI/reverify gate is
skipped entirely and the loop falls straight through to _merge_pr), and patch
_merge_pr / _mark_plane_done directly on pipeline.server.

advance_pipeline emits other, unrelated notifications during the same tick
(rebase/CI/merge-attempt messages), so assertions filter collected messages
for the ones containing the literal "/mcp reconnect" substring rather than
asserting on the total notification count.
"""
import json

import pytest

from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p

# ---------- Fixtures ----------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


def _approved_story(worktree="/x", risk="low"):
    return {
        "summary": "approved",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "risk": risk,
        "worktree": worktree,
    }


def _gate_autonomy(monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    # Isolate from host resource state: the merge-path wiring under test is
    # independent of whether dispatch/review backends are currently available.
    # Without this, a gated backend (exhausted Claude usage, unreachable
    # Ollama, ...) leaks dispatch_paused/review_paused into summary["notify"]
    # and fires an extra "Dispatch backend gated" _notify_user call, breaking
    # the notify-list-untouched assertion. Mirrors the pattern at
    # test_pipeline_mcp_server.py:4787.
    monkeypatch.setattr(
        p, "_role_resource_ok", lambda role, plan_role_config=None: (True, "")
    )


def _reconnect_notices(notes):
    return [n for n in notes if "/mcp reconnect" in n]


# ---------- Import re-export sanity (prerequisite already merged) ----------


def test_server_module_reexports_self_modification_helpers():
    # Prerequisite story (PR #214/#215) already re-exports both names on
    # pipeline.server; this story must not remove or shadow them.
    assert p._mcp_self_source_touched is not None
    assert p._mcp_restart_notice is not None


# ---------- Happy path: single-file detection ----------


def test_notifies_when_pipeline_server_py_was_merged(plan_dir, monkeypatch):
    _gate_autonomy(monkeypatch)
    _write_manifest(plan_dir, "apsrv", {"P1": _approved_story()})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    notes = []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kwargs: notes.append(msg)
    )
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda worktree, base_ref: ["pipeline/server.py"]
    )

    result = p.advance_pipeline("apsrv")

    assert result["merged"] == ["P1"], result
    hits = _reconnect_notices(notes)
    assert len(hits) == 1, notes
    assert "pipeline/server.py" in hits[0]
    story = _read_manifest(plan_dir, "apsrv")["stories"]["P1"]
    assert story["status"] == "done"


def test_notifies_when_only_the_app_module_was_merged(plan_dir, monkeypatch):
    _gate_autonomy(monkeypatch)
    _write_manifest(plan_dir, "apapp", {"P1": _approved_story()})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))
    monkeypatch.setattr(
        p,
        "_mcp_self_source_touched",
        lambda worktree, base_ref: ["app/pipeline_mcp_server.py"],
    )

    result = p.advance_pipeline("apapp")

    assert result["merged"] == ["P1"], result
    hits = _reconnect_notices(notes)
    assert len(hits) == 1, notes
    assert "app/pipeline_mcp_server.py" in hits[0]


def test_both_files_touched_produce_exactly_one_notification(plan_dir, monkeypatch):
    _gate_autonomy(monkeypatch)
    _write_manifest(plan_dir, "apboth", {"P1": _approved_story()})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))
    monkeypatch.setattr(
        p,
        "_mcp_self_source_touched",
        lambda worktree, base_ref: ["pipeline/server.py", "app/pipeline_mcp_server.py"],
    )

    result = p.advance_pipeline("apboth")

    assert result["merged"] == ["P1"], result
    hits = _reconnect_notices(notes)
    assert len(hits) == 1, notes
    assert "pipeline/server.py" in hits[0]
    assert "app/pipeline_mcp_server.py" in hits[0]


# ---------- Boundary: empty detection result ----------


def test_empty_detection_emits_no_reconnect_notice_and_notify_list_untouched(
    plan_dir, monkeypatch
):
    _gate_autonomy(monkeypatch)
    _write_manifest(plan_dir, "apnone", {"P1": _approved_story()})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda worktree, base_ref: [])

    result = p.advance_pipeline("apnone")

    assert result["merged"] == ["P1"], result
    assert _reconnect_notices(notes) == []
    # In this trivial merge path (worktree "/x" is not a real dir, so the
    # rebase/CI/reverify gate is skipped) there is nothing else that would
    # notify - a nonempty summary["notify"] here would mean an unrequested
    # notification snuck in alongside the (absent) reconnect notice.
    assert result["notify"] == []


# ---------- Ordering: detection must run before _merge_pr destroys the worktree ----------


def test_detection_runs_before_merge_pr(plan_dir, monkeypatch):
    _gate_autonomy(monkeypatch)
    _write_manifest(plan_dir, "aporder", {"P1": _approved_story()})
    order = []
    monkeypatch.setattr(
        p, "_merge_pr", lambda wt, key: order.append("merge") or "merged"
    )
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kwargs: None)

    def _detect(worktree, base_ref):
        order.append("detect")
        return ["pipeline/server.py"]

    monkeypatch.setattr(p, "_mcp_self_source_touched", _detect)

    result = p.advance_pipeline("aporder")

    assert result["merged"] == ["P1"], result
    assert order == ["detect", "merge"], order


def test_detection_called_with_worktree_and_origin_default_branch(plan_dir, monkeypatch):
    _gate_autonomy(monkeypatch)
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    _write_manifest(plan_dir, "apargs", {"P1": _approved_story(worktree="/some/worktree")})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kwargs: None)
    calls = []

    def _detect(worktree, base_ref):
        calls.append((worktree, base_ref))
        return []

    monkeypatch.setattr(p, "_mcp_self_source_touched", _detect)

    result = p.advance_pipeline("apargs")

    assert result["merged"] == ["P1"], result
    assert calls == [("/some/worktree", "origin/main")], calls


# ---------- Negative: a failed merge must not notify ----------


def test_merge_pr_exception_suppresses_reconnect_notice_even_if_touched(
    plan_dir, monkeypatch
):
    # Detection runs before _merge_pr per the ordering requirement above, so
    # it can legitimately return a nonempty list even when the merge itself
    # then fails. The notify call sits after the merge succeeds (after
    # _mark_plane_done), so a raising _merge_pr must produce zero reconnect
    # notices - the merge never actually landed.
    _gate_autonomy(monkeypatch)
    _write_manifest(
        plan_dir,
        "apfail",
        {"P1": {**_approved_story(), "merge_attempts": 0}},
    )

    def _boom(wt, key):
        raise RuntimeError("gh pr merge failed: conflict")

    monkeypatch.setattr(p, "_merge_pr", _boom)
    mark_plane_calls = []
    monkeypatch.setattr(
        p, "_mark_plane_done", lambda key, plan=None: mark_plane_calls.append(key)
    )
    notes = []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kwargs: notes.append(msg)
    )
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda worktree, base_ref: ["pipeline/server.py"]
    )

    result = p.advance_pipeline("apfail")

    assert result["merged"] == []
    assert _reconnect_notices(notes) == []
    assert mark_plane_calls == []
    story = _read_manifest(plan_dir, "apfail")["stories"]["P1"]
    assert story["status"] == "pr_open"


# ---------- Multiple stories: only the merged, touched one is notified ----------


def test_only_the_merged_story_with_touched_files_gets_notified(plan_dir, monkeypatch):
    _gate_autonomy(monkeypatch)
    _write_manifest(
        plan_dir,
        "apmulti",
        {
            "P1": _approved_story(worktree="/x1"),
            "P2": _approved_story(worktree="/x2"),
        },
    )
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    notes = []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kwargs: notes.append(msg)
    )

    def _detect(worktree, base_ref):
        return ["pipeline/server.py"] if worktree == "/x1" else []

    monkeypatch.setattr(p, "_mcp_self_source_touched", _detect)

    result = p.advance_pipeline("apmulti")

    assert set(result["merged"]) == {"P1", "P2"}, result
    hits = _reconnect_notices(notes)
    assert len(hits) == 1, notes
    assert "pipeline/server.py" in hits[0]


# ---------- summary["notify"] bookkeeping ----------


def test_notified_story_key_recorded_in_summary_notify(plan_dir, monkeypatch):
    _gate_autonomy(monkeypatch)
    _write_manifest(plan_dir, "apsummary", {"P1": _approved_story()})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kwargs: None)
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda worktree, base_ref: ["pipeline/server.py"]
    )

    result = p.advance_pipeline("apsummary")

    assert result["merged"] == ["P1"], result
    assert "P1" in result["notify"], result
