"""Tests for the ``resume_review`` executor (ov-04), park class ``agent_failed_after_push``.

Written FIRST (TDD). The executor moves a failed story whose branch head has
commits and no review verdict back to ``tests_passed``, which is the state
``review_story`` reviews. It clears the stale review records, refuses anything
outside that class, refuses a risk-high story, and does nothing in shadow mode.

The unpark write goes through ``clear_story_fields``; it is stubbed at the
module seam so no plan store is touched. Notifications are captured.
"""

from __future__ import annotations

import copy

import pytest

from pipeline import triage, triage_executors
from pipeline import triage_resume_review as rr
from pipeline.parsers import TRIAGE_ACTIONS, _parse_ruling

_PLAN = "ov"
_STORY = "ov-4"
_HEAD = "a1b2c3d4e5f6"
_OLD_HEAD = "0ldheadsha00"
_CLEARED = {
    "last_review_findings",
    "last_reviewed_logic_fingerprint",
    "last_reviewed_sha",
    "review_feedback",
    "review_verdict",
    "parked_reason",
}


@pytest.fixture(autouse=True)
def _execute_mode(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MODE", "execute")
    monkeypatch.setattr(triage, "_notify_user", lambda *args, **kwargs: None)


@pytest.fixture
def notices(monkeypatch):
    sent: list[dict] = []

    def fake(plan_name, message, **kwargs):
        sent.append({"plan": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(rr, "_notify_user", fake)
    return sent


@pytest.fixture
def unparks(monkeypatch):
    calls: list[tuple] = []

    def fake_clear(plan_name, story_key, fields, status=None):
        calls.append((plan_name, story_key, sorted(fields), status))
        return {}

    monkeypatch.setattr(rr, "clear_story_fields", fake_clear)
    return calls


def _story(**extra) -> dict:
    story = {
        "status": "failed",
        "summary": "Resume a story whose branch was pushed",
        "branch": "agent/ov-4",
        "risk": "low",
        "last_commit": _HEAD,
        "review_verdict": "",
        "review_feedback": "",
        "last_reviewed_sha": "",
        "last_review_findings": ["pipeline/x.py"],
        "last_reviewed_logic_fingerprint": "fp",
        "rework_attempts": 1,
        "files": ["pipeline/x.py"],
    }
    story.update(extra)
    return story


def _resume(story: dict) -> str:
    return rr.execute_resume_review(_PLAN, _STORY, story, {"action": "resume_review"}, {})


# --- shadow mode touches nothing -------------------------------------------

def test_shadow_mode_returns_shadow_and_touches_nothing(monkeypatch, unparks, notices):
    monkeypatch.delenv("PIPELINE_TRIAGE_MODE", raising=False)
    story = _story()
    before = copy.deepcopy(story)

    assert _resume(story) == "shadow"
    assert story == before
    assert unparks == []
    assert notices == []


def test_shadow_mode_ruling_is_recorded_without_mutation(monkeypatch, unparks):
    monkeypatch.delenv("PIPELINE_TRIAGE_MODE", raising=False)
    monkeypatch.setattr(triage, "_notify_user", lambda *args, **kwargs: None)
    recorded: list[tuple] = []
    monkeypatch.setattr(triage, "_record_execution", lambda *args, **kwargs: recorded.append(args))
    story = _story()
    before = copy.deepcopy(story)

    result = triage._apply_ruling_for_mode(
        _PLAN, _STORY, story, {"action": "resume_review", "rationale": "r"}, {}
    )

    assert result == "shadow"
    assert story == before
    assert unparks == []
    assert recorded and recorded[0][2] == "resume_review"


# --- execute mode resumes the story ----------------------------------------

def test_execute_mode_moves_story_to_tests_passed(unparks):
    story = _story()

    assert _resume(story) == "resume_review"
    assert story["status"] == "tests_passed"
    assert unparks == [(_PLAN, _STORY, sorted(_CLEARED), "tests_passed")]


def test_execute_mode_clears_the_review_records_and_keeps_the_rest(unparks):
    story = _story()

    _resume(story)

    for field in _CLEARED:
        assert field not in story
    assert story["last_commit"] == _HEAD
    assert story["rework_attempts"] == 1
    assert story["files"] == ["pipeline/x.py"]


def test_execute_mode_notifies_without_echoing_the_head(unparks, notices):
    _resume(_story())

    assert len(notices) == 1
    assert notices[0]["event"] == "story_resumed_by_overlord"
    assert _HEAD not in notices[0]["message"]


def test_execute_mode_allows_a_verdict_bound_to_an_older_head(unparks):
    story = _story(review_verdict="REQUEST_CHANGES", review_verdict_sha=_OLD_HEAD)

    assert _resume(story) == "resume_review"
    assert story["status"] == "tests_passed"


# --- refusals --------------------------------------------------------------

def _assert_parked(story: dict, unparks: list, reason_fragment: str) -> None:
    assert story["status"] == "parked"
    assert reason_fragment in story["parked_reason"]
    assert unparks == []


@pytest.mark.parametrize("head", [None, "", 7])
def test_refuses_when_no_commit_is_ahead(unparks, head):
    story = _story(last_commit=head)

    assert _resume(story) == "park_for_human"
    _assert_parked(story, unparks, "resume_review refused")


def test_refuses_when_the_head_already_has_a_verdict(unparks):
    story = _story(review_verdict="APPROVE", review_verdict_sha=_HEAD)

    assert _resume(story) == "park_for_human"
    _assert_parked(story, unparks, "resume_review refused")


def test_refuses_a_verdict_that_cannot_be_bound_to_a_head(unparks):
    story = _story(review_verdict="REQUEST_CHANGES")

    assert _resume(story) == "park_for_human"
    _assert_parked(story, unparks, "resume_review refused")


def test_refuses_when_the_head_was_already_reviewed(unparks):
    story = _story(last_reviewed_sha=_HEAD)

    assert _resume(story) == "park_for_human"
    _assert_parked(story, unparks, "resume_review refused")


@pytest.mark.parametrize("risk", ["high", "High"])
def test_refuses_a_risk_high_story(unparks, risk):
    story = _story(risk=risk)

    assert _resume(story) == "park_for_human"
    _assert_parked(story, unparks, "risk-high")


def test_refuses_a_story_outside_the_park_class(unparks):
    story = _story(status="parked", parked_reason="rebase conflict: CONFLICT (content): Merge conflict in pipeline/x.py")

    assert _resume(story) == "park_for_human"
    _assert_parked(story, unparks, "resume_review refused")


def test_refused_resume_reaches_the_router_as_a_park(unparks):
    story = _story(last_commit=None)

    result = triage.execute_ruling(_PLAN, _STORY, story, {"action": "resume_review", "rationale": "r"}, {})

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert unparks == []


def test_failed_unpark_parks_and_leaves_status_unchanged(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("could not acquire the plan lock")

    monkeypatch.setattr(rr, "clear_story_fields", boom)
    story = _story()

    assert _resume(story) == "park_for_human"
    assert story["status"] == "parked"
    assert "failed to unpark" in story["parked_reason"]


# --- wiring and parsing ----------------------------------------------------

def test_resume_review_is_registered_to_its_executor():
    assert triage_executors.EXECUTORS["resume_review"] is rr.execute_resume_review


def test_ruling_with_resume_review_parses_to_resume_review():
    text = "RULING: r\nTIER: hard\nRISK: low\nRATIONALE: why\nNOTIFY_USER: no\nACTION: resume_review\n"

    assert _parse_ruling(text)["action"] == "resume_review"


def test_resume_review_is_in_triage_actions():
    assert "resume_review" in TRIAGE_ACTIONS
