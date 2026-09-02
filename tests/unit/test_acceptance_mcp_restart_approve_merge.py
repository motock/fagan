"""Acceptance oracle: approve_merge must tell the operator to reconnect when
the merge it just landed changed the MCP server's own source.

Read-only fixture. This drives the real approve_merge entrypoint (not the
helper in isolation) so it fails if the wiring is missing.
"""
import json

import pytest

from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir, plan_name):
    stories = {
        "P1": {
            "summary": "approved but parked",
            "status": "parked",
            "review_verdict": "APPROVE",
            "risk": "medium",
            "worktree": "/x",
        }
    }
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _run(plan_dir, monkeypatch, touched, plan_name):
    notes = []
    _write_manifest(plan_dir, plan_name)
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg: notes.append(msg))
    monkeypatch.setattr(
        p, "_mcp_self_source_touched", lambda worktree, base_ref: list(touched)
    )
    result = p.approve_merge(plan_name, "P1")
    assert result["ok"] is True, result
    assert result["status"] == "done"
    return notes


def _reconnect_notices(notes):
    return [n for n in notes if "/mcp reconnect" in n]


def test_notifies_when_pipeline_server_py_was_merged(plan_dir, monkeypatch):
    notes = _run(plan_dir, monkeypatch, ["pipeline/server.py"], "amsrv")
    hits = _reconnect_notices(notes)
    assert len(hits) == 1, notes
    assert "pipeline/server.py" in hits[0]


def test_notifies_when_only_the_app_module_was_merged(plan_dir, monkeypatch):
    notes = _run(plan_dir, monkeypatch, ["app/pipeline_mcp_server.py"], "amapp")
    hits = _reconnect_notices(notes)
    assert len(hits) == 1, notes
    assert "app/pipeline_mcp_server.py" in hits[0]


def test_both_files_produce_exactly_one_notification(plan_dir, monkeypatch):
    notes = _run(
        plan_dir,
        monkeypatch,
        ["pipeline/server.py", "app/pipeline_mcp_server.py"],
        "amboth",
    )
    hits = _reconnect_notices(notes)
    assert len(hits) == 1, notes
    assert "pipeline/server.py" in hits[0]
    assert "app/pipeline_mcp_server.py" in hits[0]


def test_unrelated_merge_emits_no_reconnect_notice(plan_dir, monkeypatch):
    notes = _run(plan_dir, monkeypatch, [], "amnone")
    assert _reconnect_notices(notes) == []


def test_detection_runs_before_merge_pr_destroys_the_worktree(plan_dir, monkeypatch):
    # _merge_pr removes the worktree and deletes the branch; detecting after it
    # would always come back empty in production.
    order = []
    _write_manifest(plan_dir, "amorder")
    monkeypatch.setattr(
        p, "_merge_pr", lambda wt, key: order.append("merge") or "merged"
    )
    monkeypatch.setattr(p, "_mark_plane_done", lambda key, plan=None: None)
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kwargs: None)

    def _detect(worktree, base_ref):
        order.append("detect")
        return ["pipeline/server.py"]

    monkeypatch.setattr(p, "_mcp_self_source_touched", _detect)

    result = p.approve_merge("amorder", "P1")

    assert result["ok"] is True, result
    assert order == ["detect", "merge"], order
