"""CIGATE-3: the merge gate records its CI result as ``pr_checks`` for every story.

Both merge paths (scheduler ``_advance_pipeline_locked`` and
``_approve_merge_impl``) must leave ``story['pr_checks']`` non-null whenever the
CI gate ran - passing, failing, pending or unreadable - regardless of risk.
Assertions deliberately cover only the ``state`` value and non-null-ness.
"""

import json

import pytest

from pipeline import merge
import pipeline.server as p
from pipeline.server import _advance_pipeline_locked


def _write_manifest(plan_dir, stories):
    (plan_dir / "go.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_story(plan_dir):
    return json.loads((plan_dir / "go.manifest.json").read_text())["stories"]["P1"]


def _story(**overrides):
    base = {
        "summary": "ready pr",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "risk": "low",
        "worktree": "/nonexistent-worktree",
    }
    base.update(overrides)
    return base


@pytest.fixture
def boundaries(monkeypatch):
    merged = []
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "medium")
    monkeypatch.setattr(p, "_reverify_acceptance", lambda s, w, k: {"state": "pass"})
    monkeypatch.setattr(p, "_reverify_build", lambda w: {"state": "pass"})
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: merged.append(key) or "merged")
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
    monkeypatch.setattr(p, "_rebase_onto_master", lambda wt, br: {"ok": True, "error": ""})
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda wt, base: "")
    if hasattr(p, "_maybe_record_retro"):
        monkeypatch.setattr(p, "_maybe_record_retro", lambda *a, **k: None)
    return merged


def _stub_ci(monkeypatch, state, error=""):
    result = {"state": state, "error": error}
    monkeypatch.setattr(p, "_ci_status_once", lambda *a, **k: dict(result))
    monkeypatch.setattr(p, "_ci_status", lambda *a, **k: dict(result))


def test_should_record_pass_pr_checks_for_medium_risk_scheduler_merge(
    plan_dir, boundaries, monkeypatch
):
    _stub_ci(monkeypatch, "pass")
    _write_manifest(plan_dir, {"P1": _story(risk="medium")})

    _advance_pipeline_locked("go")

    story = _read_story(plan_dir)
    assert story["status"] == "done"
    assert story["pr_checks"] is not None
    assert story["pr_checks"]["state"] == "pass"


def test_should_record_pass_pr_checks_for_low_risk_manual_merge(
    plan_dir, boundaries, monkeypatch
):
    _stub_ci(monkeypatch, "pass")
    _write_manifest(plan_dir, {"P1": _story(status="parked")})

    result = merge._approve_merge_impl("go", "P1")

    story = _read_story(plan_dir)
    assert result["ok"] is True
    assert story["status"] == "done"
    assert story["pr_checks"]["state"] == "pass"


def test_should_record_fail_pr_checks_and_block_scheduler_merge(
    plan_dir, boundaries, monkeypatch
):
    _stub_ci(monkeypatch, "fail", "tests red")
    _write_manifest(plan_dir, {"P1": _story(risk="medium")})

    _advance_pipeline_locked("go")

    story = _read_story(plan_dir)
    assert boundaries == []
    assert story["status"] != "done"
    assert story["pr_checks"]["state"] == "fail"


def test_should_record_fail_pr_checks_and_block_manual_merge(
    plan_dir, boundaries, monkeypatch
):
    _stub_ci(monkeypatch, "fail", "tests red")
    _write_manifest(plan_dir, {"P1": _story(status="parked")})

    result = merge._approve_merge_impl("go", "P1")

    story = _read_story(plan_dir)
    assert result["ok"] is False
    assert boundaries == []
    assert story["pr_checks"]["state"] == "fail"


def test_should_record_pending_pr_checks_and_block_scheduler_merge(
    plan_dir, boundaries, monkeypatch
):
    _stub_ci(monkeypatch, "pending", "running")
    _write_manifest(plan_dir, {"P1": _story(risk="medium")})

    _advance_pipeline_locked("go")

    story = _read_story(plan_dir)
    assert boundaries == []
    assert story["status"] == "pr_open"
    assert story["pr_checks"]["state"] == "pending"


def test_should_record_pending_pr_checks_and_block_manual_merge(
    plan_dir, boundaries, monkeypatch
):
    _stub_ci(monkeypatch, "pending", "running")
    _write_manifest(plan_dir, {"P1": _story(status="parked")})

    result = merge._approve_merge_impl("go", "P1")

    story = _read_story(plan_dir)
    assert result["ok"] is False
    assert boundaries == []
    assert story["pr_checks"]["state"] == "pending"


def test_should_not_overwrite_existing_pr_checks_in_high_risk_populate(monkeypatch):
    monkeypatch.setattr(
        p,
        "_ci_status_once",
        lambda *a, **k: pytest.fail("populate must skip when pr_checks is set"),
    )
    story = {"pr_checks": {"state": "pass", "error": ""}}

    merge._populate_pr_checks_once(story, "P1")

    assert story["pr_checks"]["state"] == "pass"


def test_should_not_fabricate_pr_checks_when_gate_fails_before_ci(
    plan_dir, boundaries, monkeypatch
):
    _stub_ci(monkeypatch, "pass")
    monkeypatch.setattr(
        p, "_rebase_onto_master", lambda wt, br: {"ok": False, "error": "conflict"}
    )
    _write_manifest(plan_dir, {"P1": _story(status="parked")})

    result = merge._approve_merge_impl("go", "P1")

    story = _read_story(plan_dir)
    assert result["ok"] is False
    assert result["error"].startswith("rebase failed")
    assert story.get("pr_checks") is None
