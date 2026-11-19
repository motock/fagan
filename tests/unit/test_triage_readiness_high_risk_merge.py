"""The high-risk merge class is reported with its own scoring rule.

A high-risk merge is adjudicated by the overlord, not by the triage sweep:
the ruling is a ``high-risk merge adjudication`` record carrying ``proceed``
or ``park``, and the human's answer is a ``human resolution`` record that
points back at it through ``ruling_ref``. Those rulings are shadow decisions
too, so the promotion report must count them - under their own class,
``high_risk_merge``, and with their own scoring rule, because the human
action that answers a merge adjudication is ``merge_held`` and not ``park``:

* a ``proceed`` ruling that the human held is a false proceed;
* a ``park`` ruling that the human held is a false park;
* every other pairing agrees.

An adjudication with no parseable ruling (``ruling`` empty, the fail-closed
record) is not a ruling at all and is not counted.

Only the ``high_risk_merge`` entry is asserted here; the report's other
classes are covered by their own test module.
"""

from __future__ import annotations

import pytest

from pipeline import triage_readiness as tr

_CLASS = "high_risk_merge"
_STORY = "HRM-S1"
_DECIDED_AT = "2026-11-06T10:00:00+00:00"


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


def _human(ruling: dict, human_action: str, **overrides) -> dict:
    record = {
        "question": "human resolution",
        "story_key": ruling["story_key"],
        "ruling_ref": ruling["decided_at"],
        "ruling_action": ruling.get("ruling"),
        "park_class": _CLASS,
        "human_action": human_action,
    }
    record.update(overrides)
    return record


def _cls(report: dict) -> dict:
    return report[_CLASS]


# --- the class is reported even with nothing on record ----------------------


def test_the_class_is_reported_with_no_records() -> None:
    report = tr.readiness([])

    assert _CLASS in report
    assert _cls(report)["shadow_count"] == 0
    assert _cls(report)["paired_count"] == 0
    assert _cls(report)["eligible"] is False


def test_the_class_is_reported_when_only_other_classes_have_records() -> None:
    report = tr.readiness(
        [
            {
                "question": "failure triage",
                "story_key": _STORY,
                "action": "park_for_human",
                "park_class": "rework",
                "decided_at": _DECIDED_AT,
            }
        ]
    )

    assert _CLASS in report
    assert _cls(report)["shadow_count"] == 0


# --- the scoring rule of the class ------------------------------------------


def test_a_proceed_ruling_the_human_held_agrees() -> None:
    ruling = _adjudication("proceed")
    report = tr.readiness([ruling, _human(ruling, "merge_held")])

    assert _cls(report)["agree"] == 1
    assert _cls(report)["false_proceed"] == 0
    assert _cls(report)["false_park"] == 0


def test_a_proceed_ruling_the_human_parked_is_a_false_proceed() -> None:
    ruling = _adjudication("proceed")
    report = tr.readiness([ruling, _human(ruling, "park")])

    assert _cls(report)["false_proceed"] == 1
    assert _cls(report)["agree"] == 0
    assert _cls(report)["false_park"] == 0


def test_a_proceed_ruling_the_human_rebriefed_is_a_false_proceed() -> None:
    ruling = _adjudication("proceed")
    report = tr.readiness([ruling, _human(ruling, "rebrief")])

    assert _cls(report)["false_proceed"] == 1
    assert _cls(report)["agree"] == 0


def test_a_park_ruling_the_human_held_is_a_false_park() -> None:
    ruling = _adjudication("park")
    report = tr.readiness([ruling, _human(ruling, "merge_held")])

    assert _cls(report)["false_park"] == 1
    assert _cls(report)["agree"] == 0
    assert _cls(report)["false_proceed"] == 0


def test_a_park_ruling_the_human_parked_agrees() -> None:
    ruling = _adjudication("park")
    report = tr.readiness([ruling, _human(ruling, "park")])

    assert _cls(report)["agree"] == 1
    assert _cls(report)["false_park"] == 0
    assert _cls(report)["false_proceed"] == 0


def test_a_park_ruling_the_human_rebriefed_agrees() -> None:
    ruling = _adjudication("park")
    report = tr.readiness([ruling, _human(ruling, "rebrief")])

    assert _cls(report)["agree"] == 1
    assert _cls(report)["false_park"] == 0


# --- counting and pairing ---------------------------------------------------


def test_an_unparseable_adjudication_is_not_counted() -> None:
    ruling = _adjudication("")
    report = tr.readiness([ruling, _human(ruling, "merge_held")])

    assert _cls(report)["shadow_count"] == 0
    assert _cls(report)["paired_count"] == 0
    assert _cls(report)["agree"] == 0


def test_an_adjudication_is_counted_as_a_shadow_decision() -> None:
    report = tr.readiness([_adjudication("proceed")])

    assert _cls(report)["shadow_count"] == 1
    assert _cls(report)["paired_count"] == 0


def test_an_unpaired_adjudication_counts_as_a_shadow_decision_only() -> None:
    report = tr.readiness([_adjudication("proceed")])

    assert _cls(report)["shadow_count"] == 1
    assert _cls(report)["paired_count"] == 0
    assert _cls(report)["false_proceed"] == 0


def test_a_resolution_that_points_at_another_ruling_does_not_pair() -> None:
    ruling = _adjudication("proceed")
    resolution = _human(ruling, "merge_held", ruling_ref="2026-01-01T00:00:00+00:00")
    report = tr.readiness([ruling, resolution])

    assert _cls(report)["shadow_count"] == 1
    assert _cls(report)["paired_count"] == 0


def test_a_resolution_without_a_ruling_ref_does_not_pair() -> None:
    ruling = _adjudication("proceed")
    resolution = _human(ruling, "merge_held")
    resolution.pop("ruling_ref")
    report = tr.readiness([ruling, resolution])

    assert _cls(report)["paired_count"] == 0


def test_two_paired_adjudications_are_both_counted() -> None:
    first = _adjudication("proceed", story_key="HRM-S1", decided_at="2026-11-06T10:00:00+00:00")
    second = _adjudication("park", story_key="HRM-S2", decided_at="2026-11-06T11:00:00+00:00")
    report = tr.readiness(
        [first, _human(first, "merge_held"), second, _human(second, "park")]
    )

    assert _cls(report)["shadow_count"] == 2
    assert _cls(report)["paired_count"] == 2
    assert _cls(report)["agree"] == 2


def test_a_failure_triage_ruling_is_not_counted_in_this_class() -> None:
    report = tr.readiness(
        [
            {
                "question": "failure triage",
                "story_key": _STORY,
                "action": "park_for_human",
                "park_class": "rework",
                "decided_at": _DECIDED_AT,
            }
        ]
    )

    assert _cls(report)["shadow_count"] == 0


def test_the_false_park_rate_of_the_class_is_reported() -> None:
    ruling = _adjudication("park")
    report = tr.readiness([ruling, _human(ruling, "merge_held")])

    assert _cls(report)["false_park_rate"] == pytest.approx(1.0)
