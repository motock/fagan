"""mark_story_done records a PR merged outside the pipeline as ``story_merged``.

PRH-2 (2026-09-24) was merged by hand and closed with mark_story_done. Only
the pipeline's own merge path emits ``story_merged``, so the plan report said
``stories_merged=3`` for a plan whose four PRs had all merged, and PRH-2
could never count as merged in any metric. mark_story_done now emits
``story_merged`` when it closes a story that has a ``pr_url`` and is not
already ``done`` - before the plan-completion report reads the sidecar. A
story the pipeline already merged (status ``done``) is not counted twice.

Drives the real ``pipeline.server.mark_story_done`` MCP entry point and reads
the real notification sidecar and completion report it writes.
"""

import json

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import plan_completion as pcomp
from tests.unit._pipeline_mcp_server_test_helpers import (
    _isolate_usage_state,  # noqa: F401
)

PLAN = "rpa3"
PR = "https://github.com/example/repo/pull/7"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    # plan_completion binds its own PLAN_DIR; unpatched, the completion report
    # and marker would land in the real plan directory.
    monkeypatch.setattr(pcomp, "PLAN_DIR", d)
    return d


def _write(plan_dir, stories):
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"repo_root": "/nonexistent-repo", "stories": stories})
    )


def _merged_records(plan_dir):
    path = plan_dir / f"{PLAN}.notifications.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in records if r.get("event") == "story_merged"]


def test_closing_a_parked_story_with_a_pr_emits_story_merged(plan_dir):
    _write(plan_dir, {"S1": {"summary": "s", "status": "parked", "pr_url": PR, "correlation_id": "cid00000rpa3"}})

    p.mark_story_done(PLAN, "S1")

    records = _merged_records(plan_dir)
    assert len(records) == 1
    assert records[0]["story_key"] == "S1"
    assert records[0]["correlation_id"] == "cid00000rpa3"


def test_the_outside_merge_notice_says_how_it_was_closed(plan_dir):
    _write(plan_dir, {"S1": {"summary": "s", "status": "pr_open", "pr_url": PR}})

    p.mark_story_done(PLAN, "S1")

    assert _merged_records(plan_dir)[0]["message"] == "S1 merged (closed via mark_story_done)"


def test_the_completion_report_counts_the_outside_merge(plan_dir):
    _write(plan_dir, {"S1": {"summary": "s", "status": "parked", "pr_url": PR}})

    p.mark_story_done(PLAN, "S1")

    report = (plan_dir / f"{PLAN}.report.md").read_text()
    assert "stories_merged=1" in report


def test_a_story_the_pipeline_already_merged_is_not_counted_twice(plan_dir):
    _write(plan_dir, {"S1": {"summary": "s", "status": "done", "pr_url": PR}})

    p.mark_story_done(PLAN, "S1")

    assert _merged_records(plan_dir) == []


def test_a_story_with_no_pr_emits_no_merge(plan_dir):
    _write(plan_dir, {"S1": {"summary": "s", "status": "parked"}})

    p.mark_story_done(PLAN, "S1")

    assert _merged_records(plan_dir) == []


def test_the_story_still_ends_done_with_no_parked_reason(plan_dir):
    _write(plan_dir, {"S1": {"summary": "s", "status": "parked", "parked_reason": "x", "pr_url": PR}})

    p.mark_story_done(PLAN, "S1")

    story = json.loads((plan_dir / f"{PLAN}.manifest.json").read_text())["stories"]["S1"]
    assert story["status"] == "done"
    assert "parked_reason" not in story
