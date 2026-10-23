"""Cross-plan ``blocked_by`` gates readiness identically on both read paths.

``PipelineService.list_ready_stories`` and the scheduler tick in
``pipeline.advance`` each compute eligibility; these tests pin that both
honour ``blocked_by`` and agree with each other.
"""

import json

import pytest

import pipeline.advance as advance_module
from pipeline.service import PipelineService

_PLAN = "current"
_NEIGHBOUR = "neighbour"


class _DryRunAutonomy:
    def _value(self):
        return "dry-run"


def _write_manifest(plan_dir, plan, stories):
    manifest = {"name": plan, "paused": False, "stories": stories}
    (plan_dir / f"{plan}.manifest.json").write_text(json.dumps(manifest))


def _story(status="todo", **extra):
    return {"summary": "s", "status": status, "dependencies": [], **extra}


def _seed(plan_dir, blocked_by=..., neighbour_status="todo", neighbour=True):
    story = _story()
    if blocked_by is not ...:
        story["blocked_by"] = blocked_by
    _write_manifest(plan_dir, _PLAN, {"A": story})
    if neighbour:
        _write_manifest(plan_dir, _NEIGHBOUR, {"N": _story(neighbour_status)})


def _service_ready(plan_dir):
    return [s["key"] for s in PipelineService().list_ready_stories(_PLAN)]


def _tick_ready(plan_dir, monkeypatch):
    monkeypatch.setattr(advance_module, "_role_resource_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(advance_module, "PIPELINE_AUTONOMY", _DryRunAutonomy())
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)
    result = advance_module._advance_pipeline_locked_impl(_PLAN)
    return result["would_dispatch"]


def test_should_be_ready_when_neighbour_story_is_done(plan_dir, monkeypatch):
    _seed(plan_dir, ["neighbour:N"], neighbour_status="done")
    assert _service_ready(plan_dir) == ["A"]
    assert _tick_ready(plan_dir, monkeypatch) == ["A"]


@pytest.mark.parametrize("status", ["todo", "in_progress", "pr_open", "parked"])
def test_should_not_be_ready_when_neighbour_story_unfinished(plan_dir, monkeypatch, status):
    _seed(plan_dir, ["neighbour:N"], neighbour_status=status)
    assert _service_ready(plan_dir) == []
    assert _tick_ready(plan_dir, monkeypatch) == []


def test_should_not_be_ready_when_referenced_plan_missing(plan_dir, monkeypatch):
    _seed(plan_dir, ["ghost:N"])
    assert _service_ready(plan_dir) == []
    assert _tick_ready(plan_dir, monkeypatch) == []


def test_should_not_be_ready_when_referenced_story_missing(plan_dir, monkeypatch):
    _seed(plan_dir, ["neighbour:nope"], neighbour_status="done")
    assert _service_ready(plan_dir) == []
    assert _tick_ready(plan_dir, monkeypatch) == []


def test_should_be_ready_when_blocked_by_absent(plan_dir, monkeypatch):
    _seed(plan_dir)
    assert _service_ready(plan_dir) == ["A"]
    assert _tick_ready(plan_dir, monkeypatch) == ["A"]


def test_should_be_ready_when_blocked_by_empty(plan_dir, monkeypatch):
    _seed(plan_dir, [])
    assert _service_ready(plan_dir) == ["A"]
    assert _tick_ready(plan_dir, monkeypatch) == ["A"]


def test_should_not_deadlock_when_blocked_by_names_own_plan(plan_dir, monkeypatch):
    # The own plan is excluded from the lookup, so a self-reference is an
    # unknown plan -> a gap. It must be reported identically on both paths
    # and must not raise or hang.
    _seed(plan_dir, ["current:A"], neighbour=False)
    assert _service_ready(plan_dir) == _tick_ready(plan_dir, monkeypatch)


def test_should_report_same_ready_set_on_both_surfaces(plan_dir, monkeypatch):
    _write_manifest(
        plan_dir,
        _PLAN,
        {
            "free": _story(),
            "open": _story(blocked_by=["neighbour:N"]),
            "shut": _story(blocked_by=["neighbour:M"]),
        },
    )
    _write_manifest(
        plan_dir, _NEIGHBOUR, {"N": _story("done"), "M": _story("in_progress")}
    )
    assert _service_ready(plan_dir) == _tick_ready(plan_dir, monkeypatch) == ["free", "open"]
