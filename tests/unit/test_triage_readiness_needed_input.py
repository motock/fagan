"""Scoring of a park the human answered with input (ADR 0014, decision 8).

A ``park_for_human`` ruling is a false park only when the human resolved the
story without supplying anything. A human who changed the brief, widened the
scope or approved a test edit answered the park with input the machine could
not have produced, so it agrees, and the agreement is also tallied as
``park_needed_input``. These tests use synthetic records only, so the scoring
is pinned independently of the live decisions log.
"""

from __future__ import annotations

import pytest

from pipeline import triage_readiness as tr

_CLASS = "rebase_conflict"


def _ruling(key: str, *, action: str, park_class: str = _CLASS) -> dict:
    return {
        "question": "failure triage",
        "story_key": key,
        "action": action,
        "park_class": park_class,
        "decided_at": f"2026-11-01T00:00:{key}",
    }


def _human(ruling: dict, human_action: str, **extra) -> dict:
    record = {
        "question": "human resolution",
        "story_key": ruling["story_key"],
        "ruling_ref": ruling["decided_at"],
        "ruling_action": ruling["action"],
        "park_class": ruling["park_class"],
        "human_action": human_action,
        "files_added": [],
    }
    record.update(extra)
    return record


def _pair(key: str, action: str, human_action: str, **extra) -> list[dict]:
    ruling = _ruling(key, action=action)
    return [ruling, _human(ruling, human_action, **extra)]


def _cls(report: dict, name: str = _CLASS) -> dict:
    return report[name]


@pytest.mark.parametrize(
    "human_action", ["rebrief", "amend_scope", "approve_test_edit"]
)
def test_park_answered_with_input_action_is_agreement_needing_input(human_action):
    report = tr.readiness(_pair("A", tr.PARK_ACTION, human_action))
    cls = _cls(report)
    assert cls["agree"] == 1
    assert cls["park_needed_input"] == 1
    assert cls["false_park"] == 0


def test_park_answered_by_other_with_files_added_is_agreement_needing_input():
    report = tr.readiness(
        _pair("A", tr.PARK_ACTION, "other", files_added=["pipeline/x.py"])
    )
    cls = _cls(report)
    assert cls["agree"] == 1
    assert cls["park_needed_input"] == 1
    assert cls["false_park"] == 0


def test_park_answered_by_other_with_brief_changed_is_agreement_needing_input():
    report = tr.readiness(_pair("A", tr.PARK_ACTION, "other", brief_changed=True))
    cls = _cls(report)
    assert cls["agree"] == 1
    assert cls["park_needed_input"] == 1
    assert cls["false_park"] == 0


@pytest.mark.parametrize("brief_changed", ["true", 1])
def test_brief_changed_that_is_not_the_boolean_true_does_not_count_as_input(
    brief_changed,
):
    report = tr.readiness(
        _pair("A", tr.PARK_ACTION, "other", brief_changed=brief_changed)
    )
    cls = _cls(report)
    assert cls["false_park"] == 1
    assert cls["park_needed_input"] == 0


@pytest.mark.parametrize("human_action", ["other", "retry", "resume_review"])
def test_park_answered_without_input_is_a_false_park(human_action):
    report = tr.readiness(_pair("A", tr.PARK_ACTION, human_action))
    cls = _cls(report)
    assert cls["false_park"] == 1
    assert cls["agree"] == 0
    assert cls["park_needed_input"] == 0


def test_park_answered_with_park_is_agreement_without_needed_input():
    report = tr.readiness(_pair("A", tr.PARK_ACTION, "park"))
    cls = _cls(report)
    assert cls["agree"] == 1
    assert cls["park_needed_input"] == 0
    assert cls["false_park"] == 0


def test_human_park_after_a_proceeding_ruling_is_still_a_false_proceed():
    report = tr.readiness(_pair("A", "escalate_model", "park"))
    cls = _cls(report)
    assert cls["false_proceed"] == 1
    assert cls["park_needed_input"] == 0


def test_proceeding_ruling_answered_with_brief_change_is_agreement_without_needed_input():
    report = tr.readiness(_pair("A", "escalate_model", "rebrief", brief_changed=True))
    cls = _cls(report)
    assert cls["agree"] == 1
    assert cls["park_needed_input"] == 0
    assert cls["false_proceed"] == 0


def test_every_class_in_the_empty_report_reports_zero_needed_input():
    report = tr.readiness([])
    assert report
    for verdict in report.values():
        assert verdict["park_needed_input"] == 0


def test_eighteen_needed_input_agreements_and_two_false_parks_are_eligible():
    records: list[dict] = []
    for index in range(18):
        records += _pair(f"N{index}", tr.PARK_ACTION, "rebrief")
    for index in range(2):
        records += _pair(f"F{index}", tr.PARK_ACTION, "retry")
    cls = _cls(tr.readiness(records))
    assert cls["paired_count"] == 20
    assert cls["false_park"] == 2
    assert cls["eligible"] is True


def test_three_false_parks_out_of_twenty_pairs_is_not_eligible():
    records: list[dict] = []
    for index in range(17):
        records += _pair(f"N{index}", tr.PARK_ACTION, "rebrief")
    for index in range(3):
        records += _pair(f"F{index}", tr.PARK_ACTION, "retry")
    cls = _cls(tr.readiness(records))
    assert cls["paired_count"] == 20
    assert cls["false_park"] == 3
    assert cls["eligible"] is False
