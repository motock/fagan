"""A high-risk merge ruling made without concluded CI is left out of readiness.

An adjudication record with ``evidence_complete: false`` was made on evidence
that did not conclude. It is not a shadow decision the promotion report can
score, so it is excluded from every count and reported on its own as
``incomplete_evidence``. A record with no ``evidence_complete`` field is history
written before the field existed and is counted exactly as before.
"""

from __future__ import annotations

from pipeline import triage_readiness as tr

_CLASS = "high_risk_merge"
_STORY = "HRM-EV1"
_DECIDED_AT = "2026-11-25T10:00:00+00:00"


def _adjudication(ruling: str = "proceed", **overrides) -> dict:
    record = {
        "question": "high-risk merge adjudication",
        "story_key": _STORY,
        "ruling": ruling,
        "rationale": "risk accepted",
        "decided_by": "overlord",
        "decided_at": _DECIDED_AT,
    }
    record.update(overrides)
    return record


def _human(ruling: dict, human_action: str) -> dict:
    return {
        "question": "human resolution",
        "story_key": ruling["story_key"],
        "ruling_ref": ruling["decided_at"],
        "ruling_action": ruling.get("ruling"),
        "park_class": _CLASS,
        "human_action": human_action,
    }


def _high_risk(decisions: list[dict]) -> dict:
    return tr.readiness(decisions)[_CLASS]


def test_an_incomplete_evidence_ruling_is_excluded_from_shadow_count():
    report = _high_risk([_adjudication(evidence_complete=False)])

    assert report["shadow_count"] == 0
    assert report["paired_count"] == 0


def test_an_incomplete_evidence_ruling_is_reported_as_its_own_count():
    report = _high_risk([_adjudication(evidence_complete=False)])

    assert report["incomplete_evidence"] == 1


def test_an_incomplete_evidence_ruling_never_pairs_with_a_human_answer():
    ruling = _adjudication("proceed", evidence_complete=False)
    report = _high_risk([ruling, _human(ruling, "merge_held")])

    assert report["paired_count"] == 0
    assert report["false_proceed"] == 0
    assert report["incomplete_evidence"] == 1


def test_an_incomplete_evidence_ruling_does_not_count_toward_eligibility():
    rulings = [_adjudication("proceed", evidence_complete=False) for _ in range(50)]

    report = _high_risk(rulings)

    assert report["eligible"] is False
    assert report["incomplete_evidence"] == 50


def test_a_complete_evidence_ruling_is_counted_as_before():
    ruling = _adjudication("park", evidence_complete=True)
    report = _high_risk([ruling, _human(ruling, "merge_held")])

    assert report["shadow_count"] == 1
    assert report["paired_count"] == 1
    assert report["false_park"] == 1
    assert report["incomplete_evidence"] == 0


def test_history_without_the_field_is_counted_as_before():
    ruling = _adjudication("park")
    report = _high_risk([ruling, _human(ruling, "merge_held")])

    assert report["shadow_count"] == 1
    assert report["paired_count"] == 1
    assert report["false_park"] == 1
    assert report["incomplete_evidence"] == 0


def test_an_empty_ruling_is_still_not_counted_as_incomplete_evidence():
    report = _high_risk([_adjudication("", evidence_complete=False)])

    assert report["shadow_count"] == 0
    assert report["incomplete_evidence"] == 0
