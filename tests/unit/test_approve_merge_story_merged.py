"""fcf-02: approve_merge records ``story_merged`` like advance_merge does.

story_metrics counts merges only from ``story_merged`` notifications, so a
story merged through approve_merge must emit one, and only on success.
"""
# ruff: noqa: I001
import pytest

from pipeline import server as p

import pipeline.plan_completion as ppc
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _read_manifest,
    _story,
    _write_manifest,
    plan_dir,
)
from tests.unit.test_plan_completion_wiring import _stub_approve_merge_boundaries

_WORKTREE = "/nonexistent-fcf02-worktree"


def _parked(**over):
    return _story(
        status="parked",
        review_verdict="APPROVE",
        review_verdict_sha="stub-head",
        worktree=_WORKTREE,
        **over,
    )


@pytest.fixture
def notices(monkeypatch):
    calls = []

    def _capture(plan_name, message, **kwargs):
        calls.append({"plan_name": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(p, "_notify_user", _capture)
    monkeypatch.setattr(ppc, "notify_if_plan_completed", lambda *a, **k: False)
    return calls


def _merged(notices):
    return [n for n in notices if n.get("event") == "story_merged"]


def test_should_emit_story_merged_when_approve_merge_succeeds(
    plan_dir, monkeypatch, notices  # noqa: F811
):
    _stub_approve_merge_boundaries(monkeypatch)
    _write_manifest(plan_dir, "fcf02-ok", {"S1": _parked()})

    result = p.approve_merge("fcf02-ok", "S1")

    assert result.get("ok") is True, result
    merged = _merged(notices)
    assert len(merged) == 1
    assert merged[0]["plan_name"] == "fcf02-ok"
    assert merged[0]["story_key"] == "S1"
    assert merged[0]["message"] == "S1 merged"
    assert "correlation_id" not in merged[0]


def test_should_carry_the_correlation_id_on_story_merged_when_the_story_has_one(
    plan_dir, monkeypatch, notices  # noqa: F811
):
    _stub_approve_merge_boundaries(monkeypatch)
    _write_manifest(
        plan_dir, "fcf02-corr", {"S1": _parked(correlation_id="corr-123")}
    )

    result = p.approve_merge("fcf02-corr", "S1")

    assert result.get("ok") is True, result
    merged = _merged(notices)
    assert len(merged) == 1
    assert merged[0]["correlation_id"] == "corr-123"


def test_should_emit_story_merged_before_the_plan_completion_check(
    plan_dir, monkeypatch, notices  # noqa: F811
):
    _stub_approve_merge_boundaries(monkeypatch)
    seen_at_completion = []

    def _completion(plan_name, manifest):
        seen_at_completion.append(len(_merged(notices)))
        return False

    monkeypatch.setattr(ppc, "notify_if_plan_completed", _completion)
    _write_manifest(plan_dir, "fcf02-order", {"S1": _parked()})

    result = p.approve_merge("fcf02-order", "S1")

    assert result.get("ok") is True, result
    assert seen_at_completion == [1]


def test_should_not_emit_story_merged_when_the_merge_fails(
    plan_dir, monkeypatch, notices  # noqa: F811
):
    _stub_approve_merge_boundaries(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("gh merge failed")

    monkeypatch.setattr(p, "_merge_pr", _boom)
    _write_manifest(plan_dir, "fcf02-fail", {"S1": _parked()})

    result = p.approve_merge("fcf02-fail", "S1")

    assert result.get("ok") is False
    assert _merged(notices) == []
    assert _read_manifest(plan_dir, "fcf02-fail")["stories"]["S1"]["status"] == "parked"


@pytest.mark.parametrize("status", ["todo", "in_progress", "done"])
def test_should_not_emit_story_merged_for_a_story_that_is_not_parked_or_pr_open(
    plan_dir, monkeypatch, notices, status  # noqa: F811
):
    _stub_approve_merge_boundaries(monkeypatch)
    _write_manifest(
        plan_dir, "fcf02-status", {"S1": _story(status=status, worktree=_WORKTREE)}
    )

    result = p.approve_merge("fcf02-status", "S1")

    assert result.get("ok") is False
    assert _merged(notices) == []
