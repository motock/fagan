"""Tests for the ``review_inconclusive`` park class (pipeline.triage_park_classes).

A story whose work is pushed and green but whose review was inconclusive is
parked by ``pipeline/review_orchestrator.py`` with a reason that starts with
``review inconclusive after <n> attempts``. Written FIRST (TDD).
"""

from pipeline.triage_park_classes import (
    ALLOWED_ACTIONS,
    ParkClass,
    check_ruling_allowed,
    classify_park,
)

_REASON = "review inconclusive after 2 attempts - needs human review"


def _classify(story: dict, evidence: dict | None = None) -> ParkClass:
    return classify_park(story, evidence or {})


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_reason_classifies_as_review_inconclusive_with_attempt_count():
    park = _classify({"parked_reason": _REASON})
    assert park.name == "review_inconclusive"
    assert park.facts == {"attempts": 2}


def test_overlord_park_reason_is_read_when_parked_reason_is_empty():
    park = _classify({"parked_reason": "", "park_reason": _REASON})
    assert park.name == "review_inconclusive"
    assert park.facts == {"attempts": 2}


def test_reason_without_a_readable_attempt_count_classifies_as_unknown():
    park = _classify({"parked_reason": "review inconclusive after many attempts"})
    assert park.name == "unknown"
    assert park.facts == {}


def test_phrase_only_later_in_the_reason_does_not_classify_as_review_inconclusive():
    park = _classify({"parked_reason": "CI failed; review inconclusive after 2 attempts"})
    assert park.name != "review_inconclusive"


def test_scope_conflict_record_takes_priority_over_review_inconclusive():
    park = _classify({
        "parked_reason": _REASON,
        "scope_conflict": {"paths": ["pipeline/other.py"]},
    })
    assert park.name == "scope_gate"


def test_red_ci_evidence_takes_priority_over_review_inconclusive():
    park = _classify({"parked_reason": _REASON, "ci_rework": True})
    assert park.name == "ci_red_after_rework"


def test_transient_marker_takes_priority_over_review_inconclusive():
    park = _classify({"parked_reason": "review inconclusive after 2 attempts; plan lock contended"})
    assert park.name == "transient_bookkeeping"


def test_high_risk_marker_does_not_outrank_review_inconclusive():
    park = _classify({"parked_reason": f"{_REASON}; high risk held for human review"})
    assert park.name == "review_inconclusive"
    assert park.facts == {"attempts": 2}


def test_non_string_reason_does_not_raise_and_classifies_as_unknown():
    park = _classify({"parked_reason": 42, "park_reason": ["review inconclusive after 2 attempts"]})
    assert park.name == "unknown"


# ---------------------------------------------------------------------------
# Allowed actions
# ---------------------------------------------------------------------------


def test_review_inconclusive_allows_exactly_resume_review_and_park_for_human():
    assert ALLOWED_ACTIONS["review_inconclusive"] == frozenset({"resume_review", "park_for_human"})


def test_review_inconclusive_allows_resume_review_and_park_for_human():
    park = ParkClass(name="review_inconclusive", facts={"attempts": 2})
    assert check_ruling_allowed(park, "resume_review")
    assert check_ruling_allowed(park, "park_for_human")


def test_review_inconclusive_rejects_escalate_model_amend_scope_and_mark_done():
    park = ParkClass(name="review_inconclusive", facts={"attempts": 2})
    assert not check_ruling_allowed(park, "escalate_model")
    assert not check_ruling_allowed(park, "amend_scope")
    assert not check_ruling_allowed(park, "mark_done")
