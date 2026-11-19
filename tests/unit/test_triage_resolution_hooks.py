"""The human's answer to an overlord ruling is recorded automatically.

The overlord's ruling says what the machine decided about a story. When a
human then acts on that story - merging it, closing a merge it made by hand,
or moving it off ``parked``/``pr_open`` - the human has answered the ruling,
and the answer belongs in the decisions stream beside the ruling, the way the
operator's unpark already records one.

``pipeline.triage_outcomes.record_resolution_if_ruled`` is that hook: it
records a ``human resolution`` for a story that has an unanswered ruling, and
writes nothing at all when the story has no ruling or the ruling was already
answered. It never raises: a failed record write is a warning, never a failed
merge or a failed status change.

The three call sites are the real entry points, driven here with git and the
forge mocked at the boundary:

* ``approve_merge`` (a successful merge) -> ``human_action="merge_held"``;
* ``mark_story_done`` (a merge made outside the pipeline) -> ``merge_held``;
* ``set_story_status`` (a story leaving ``parked``/``pr_open``) -> ``park``,
  ``retry`` or ``other``, per the new status.

Every plan directory here is ``tmp_path``, so nothing touches the real plan
directory or the real decisions stream.
"""

from __future__ import annotations

import json
import logging

import pytest

import pipeline.server as p
from pipeline import persistence as ppers
from pipeline import review_merge_binding
from pipeline import triage_outcomes as to

PLAN = "triagehooks"
STORY = "s1"
OTHER_STORY = "s2"
DECIDED_AT = "2026-11-18T09:00:00+00:00"
PR_URL = "https://github.com/example/repo/pull/7"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every plan-directory binding at a throwaway directory."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write_manifest(stories: dict, plan_name: str = PLAN) -> None:
    (p.PLAN_DIR / f"{plan_name}.manifest.json").write_text(
        json.dumps({"plan": plan_name, "stories": stories}, indent=2)
    )


def _read_story(story_key: str = STORY, plan_name: str = PLAN) -> dict:
    raw = (p.PLAN_DIR / f"{plan_name}.manifest.json").read_text()
    return json.loads(raw)["stories"][story_key]


def _seed_ruling(
    story_key: str = STORY,
    *,
    question: str = "failure triage",
    decided_at: str = DECIDED_AT,
    **overrides,
) -> dict:
    """Put one ruling into the plan's decision store and return the record."""
    record = {
        "question": question,
        "story_key": story_key,
        "decided_by": "overlord-triage",
        "decided_at": decided_at,
    }
    record.update(overrides)
    p._store.records().append_decision("default", PLAN, record)
    return record


def _decisions() -> list[dict]:
    path = ppers._decisions_path(PLAN)
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _resolutions() -> list[dict]:
    return [r for r in _decisions() if r.get("question") == "human resolution"]


def _ruling(plan_name: str = PLAN, story_key: str = STORY) -> dict | None:
    return to.latest_failure_ruling(plan_name, story_key)


# --------------------------------------------------------------------------
# record_resolution_if_ruled
# --------------------------------------------------------------------------


def test_it_records_one_resolution_linked_to_the_ruling(plan_dir):
    _seed_ruling()

    record = to.record_resolution_if_ruled(
        PLAN,
        STORY,
        human_action="park",
        new_status="todo",
        principal="operator-1",
    )

    assert record is not None, "a story with an unanswered ruling must get one"
    written = _resolutions()
    assert len(written) == 1, f"expected exactly one resolution, got {written!r}"
    assert written[0]["ruling_ref"] == DECIDED_AT
    assert written[0]["story_key"] == STORY
    assert written[0]["human_action"] == "park"
    assert written[0]["new_status"] == "todo"
    assert written[0]["decided_by"] == "operator-1"


def test_a_second_call_records_nothing_more(plan_dir):
    _seed_ruling()

    to.record_resolution_if_ruled(
        PLAN, STORY, human_action="park", new_status="todo", principal="operator-1"
    )
    second = to.record_resolution_if_ruled(
        PLAN, STORY, human_action="retry", new_status="todo", principal="operator-1"
    )

    assert second is None, "an answered ruling must not be answered twice"
    written = _resolutions()
    assert len(written) == 1, f"expected one resolution, got {written!r}"
    assert written[0]["human_action"] == "park", "the first answer must stand"


def test_a_story_without_a_ruling_records_nothing(plan_dir):
    record = to.record_resolution_if_ruled(
        PLAN, STORY, human_action="park", new_status="todo", principal="operator-1"
    )

    assert record is None
    assert _resolutions() == []


def test_a_ruling_for_another_story_records_nothing(plan_dir):
    _seed_ruling(OTHER_STORY)

    record = to.record_resolution_if_ruled(
        PLAN, STORY, human_action="park", new_status="todo", principal="operator-1"
    )

    assert record is None, "another story's ruling is not this story's ruling"
    assert _resolutions() == []


def test_a_resolution_for_a_different_ruling_does_not_block(plan_dir):
    """Only a resolution for THIS ruling counts as the answer to it."""
    _seed_ruling()
    p._store.records().append_decision(
        "default",
        PLAN,
        {
            "question": "human resolution",
            "story_key": STORY,
            "ruling_ref": "2020-01-01T00:00:00+00:00",
            "human_action": "park",
            "new_status": "todo",
            "decided_by": "someone-else",
            "decided_at": "2020-01-01T00:00:00+00:00",
        },
    )

    record = to.record_resolution_if_ruled(
        PLAN, STORY, human_action="retry", new_status="todo", principal="operator-1"
    )

    assert record is not None, "an unrelated resolution must not answer this ruling"
    written = _resolutions()
    assert len(written) == 2
    assert written[1]["ruling_ref"] == DECIDED_AT


def test_a_failing_record_write_is_swallowed_and_logged(plan_dir, caplog):
    _seed_ruling()

    def boom(*args, **kwargs):
        raise OSError("decisions log is read-only")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(to, "record_human_resolution", boom)
    try:
        with caplog.at_level(logging.WARNING, logger="pipeline"):
            record = to.record_resolution_if_ruled(
                PLAN, STORY, human_action="park", new_status="todo", principal="op"
            )
    finally:
        monkey.undo()

    assert record is None, "a failed record write must not raise"
    assert _resolutions() == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "the failed write must be logged as a warning"
    assert any(STORY in r.getMessage() for r in warnings), (
        f"the warning must name the story, got {[r.getMessage() for r in warnings]!r}"
    )


def test_the_warning_never_carries_a_token_a_body_or_a_diff(plan_dir, caplog):
    """The log line carries identifiers and outcomes only."""
    _seed_ruling()

    def boom(*args, **kwargs):
        raise OSError("token=ghp_secret diff body here")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(to, "record_human_resolution", boom)
    try:
        with caplog.at_level(logging.WARNING, logger="pipeline"):
            to.record_resolution_if_ruled(
                PLAN, STORY, human_action="park", new_status="todo", principal="op"
            )
    finally:
        monkey.undo()

    for record in caplog.records:
        rendered = record.getMessage()
        assert "ghp_secret" not in rendered
        assert "token" not in rendered.lower()
        assert "diff" not in rendered.lower()


# --------------------------------------------------------------------------
# set_story_status wiring
# --------------------------------------------------------------------------


def _parked_story(**extra) -> dict:
    story = {"summary": "a story", "status": "parked"}
    story.update(extra)
    return story


def test_set_story_status_off_parked_records_a_retry(plan_dir):
    _seed_ruling()
    _write_manifest({STORY: _parked_story()})

    result = p.set_story_status(PLAN, STORY, "todo")

    assert result.get("ok") is True
    written = _resolutions()
    assert len(written) == 1, f"expected one resolution, got {written!r}"
    assert written[0]["human_action"] == "retry"
    assert written[0]["new_status"] == "todo"
    assert written[0]["ruling_ref"] == DECIDED_AT


def test_set_story_status_off_a_todo_story_records_nothing(plan_dir):
    _seed_ruling()
    _write_manifest({STORY: {"summary": "a story", "status": "todo"}})

    result = p.set_story_status(PLAN, STORY, "todo")

    assert result.get("ok") is True
    assert _resolutions() == [], "a story that was not parked/pr_open has no ruling to answer"


def test_set_story_status_to_the_same_status_records_nothing(plan_dir):
    _seed_ruling()
    _write_manifest({STORY: _parked_story()})

    result = p.set_story_status(PLAN, STORY, "parked")

    assert result.get("ok") is True
    assert _resolutions() == [], "an unchanged status is not an answer to a ruling"


def test_set_story_status_to_a_failed_story_records_a_park(plan_dir):
    _seed_ruling()
    _write_manifest({STORY: _parked_story()})

    p.set_story_status(PLAN, STORY, "failed")

    written = _resolutions()
    assert len(written) == 1, f"expected one resolution, got {written!r}"
    assert written[0]["human_action"] == "park"


def test_set_story_status_off_pr_open_to_changes_requested_records_a_retry(plan_dir):
    _seed_ruling()
    _write_manifest({STORY: {"summary": "a story", "status": "pr_open"}})

    p.set_story_status(PLAN, STORY, "changes_requested")

    written = _resolutions()
    assert len(written) == 1, f"expected one resolution, got {written!r}"
    assert written[0]["human_action"] == "retry"


def test_set_story_status_to_an_unmapped_status_records_other(plan_dir):
    _seed_ruling()
    _write_manifest({STORY: _parked_story()})

    p.set_story_status(PLAN, STORY, "in_progress")

    written = _resolutions()
    assert len(written) == 1, f"expected one resolution, got {written!r}"
    assert written[0]["human_action"] == "other"


def test_set_story_status_to_an_invalid_status_records_nothing(plan_dir):
    _seed_ruling()
    _write_manifest({STORY: _parked_story()})

    result = p.set_story_status(PLAN, STORY, "blocked")

    assert result.get("ok") is False, "an unknown status is refused"
    assert _resolutions() == [], "a refused status change is not an answer"


def test_set_story_status_on_a_story_without_a_ruling_records_nothing(plan_dir):
    _write_manifest({STORY: _parked_story()})

    result = p.set_story_status(PLAN, STORY, "todo")

    assert result.get("ok") is True
    assert _resolutions() == []


# --------------------------------------------------------------------------
# mark_story_done wiring
# --------------------------------------------------------------------------


def _open_story(**extra) -> dict:
    story = {"summary": "a story", "status": "pr_open", "pr_url": PR_URL}
    story.update(extra)
    return story


def test_mark_story_done_of_an_outside_merge_records_a_merge_held(plan_dir):
    _seed_ruling(
        question="high-risk merge adjudication",
        ruling="park",
        decided_by="overlord-merge",
    )
    _write_manifest(
        {STORY: _open_story(), OTHER_STORY: {"summary": "other", "status": "todo"}}
    )

    result = p.mark_story_done(PLAN, STORY)

    assert result == {"ok": True}, f"the return shape must not change: {result!r}"
    written = _resolutions()
    assert len(written) == 1, f"expected one resolution, got {written!r}"
    assert written[0]["human_action"] == "merge_held"
    assert written[0]["new_status"] == "done"
    assert written[0]["park_class"] == "high_risk_merge"
    assert written[0]["ruling_ref"] == DECIDED_AT


def test_mark_story_done_of_a_story_without_a_pr_records_nothing(plan_dir):
    """No pr_url means the merge was the pipeline's own, not an outside one."""
    _seed_ruling()
    _write_manifest(
        {
            STORY: {"summary": "a story", "status": "pr_open"},
            OTHER_STORY: {"summary": "other", "status": "todo"},
        }
    )

    p.mark_story_done(PLAN, STORY)

    assert _resolutions() == []


def test_mark_story_done_of_an_already_done_story_records_nothing(plan_dir):
    _seed_ruling()
    _write_manifest(
        {
            STORY: {"summary": "a story", "status": "done", "pr_url": PR_URL},
            OTHER_STORY: {"summary": "other", "status": "todo"},
        }
    )

    p.mark_story_done(PLAN, STORY)

    assert _resolutions() == [], "a story the pipeline already merged is not an outside merge"


def test_mark_story_done_without_a_ruling_records_nothing(plan_dir):
    _write_manifest(
        {STORY: _open_story(), OTHER_STORY: {"summary": "other", "status": "todo"}}
    )

    result = p.mark_story_done(PLAN, STORY)

    assert result == {"ok": True}
    assert _resolutions() == []


# --------------------------------------------------------------------------
# approve_merge wiring
# --------------------------------------------------------------------------


def _mergeable_story(**extra) -> dict:
    story = {
        "summary": "a story ready to merge",
        "status": "parked",
        "review_verdict": "APPROVE",
        "review_verdict_sha": "reviewed-sha",
        "risk": "low",
        # A worktree path that does not exist: the merge path then skips the
        # git push/checkout and only the forge calls (mocked below) run.
        "worktree": "/nonexistent-worktree",
    }
    story.update(extra)
    return story


@pytest.fixture
def merge_boundary(monkeypatch):
    """Mock git and the forge at the boundary, so the merge path runs for real."""
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "_rebase_onto_master", lambda wt, branch: {"ok": True})
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_ci_status", lambda *a, **k: {"state": "success", "error": ""})
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(
        p, "_reverify_acceptance", lambda story, wt, key, settings=None: {"state": "pass"}
    )
    monkeypatch.setattr(p, "_reverify_build", lambda wt, settings=None: {"state": "pass"})
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda wt, base: "")
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
    monkeypatch.setattr(review_merge_binding, "branch_head_sha", lambda branch, **k: "head-sha")
    monkeypatch.setattr(
        review_merge_binding, "check_with_worktree", lambda story, head, wt: {"ok": True}
    )


def test_a_successful_approve_merge_records_a_merge_held(plan_dir, merge_boundary):
    _seed_ruling()
    _write_manifest({STORY: _mergeable_story()})

    result = p.approve_merge(PLAN, STORY)

    assert result.get("ok") is True, f"the merge must succeed: {result!r}"
    assert _read_story()["status"] == "done"
    written = _resolutions()
    assert len(written) == 1, f"expected one resolution, got {written!r}"
    assert written[0]["human_action"] == "merge_held"
    assert written[0]["new_status"] == "done"
    assert written[0]["ruling_ref"] == DECIDED_AT


def test_a_refused_approve_merge_records_nothing(plan_dir, merge_boundary):
    """A merge that did not happen is not an answer to a ruling."""
    _seed_ruling()
    _write_manifest({STORY: _mergeable_story(review_verdict="REQUEST_CHANGES")})

    result = p.approve_merge(PLAN, STORY)

    assert result.get("ok") is False
    assert _resolutions() == []


def test_an_approve_merge_without_a_ruling_records_nothing(plan_dir, merge_boundary):
    _write_manifest({STORY: _mergeable_story()})

    result = p.approve_merge(PLAN, STORY)

    assert result.get("ok") is True
    assert _resolutions() == []
