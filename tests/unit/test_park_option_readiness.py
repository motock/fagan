"""Park option choices count toward readiness as their own signal.

A person choosing among park options is not a proceed/park ruling, so the
choices are counted per park class beside the verdict and never change it.
Every test uses synthetic records.
"""

from __future__ import annotations

from pipeline import triage_readiness as tr

_CLASS = "scope_gate"
_PAIRED_KEYS = ("shadow_count", "paired_count", "agree", "false_proceed", "false_park", "false_park_rate", "eligible", "blockers")


def _choice(park_class=_CLASS, *, chose_recommended: bool, story_key: str = "s1") -> dict:
    return {
        "kind": "park_option_chosen",
        "story_key": story_key,
        "option_id": "o1" if not chose_recommended else "o2",
        "action": "amend_scope" if not chose_recommended else "retry",
        "recommended": "o2",
        "chose_recommended": chose_recommended,
        "park_class": park_class,
        "actor": "alice",
        "decided_at": "2026-11-21T00:00:00+00:00",
    }


def _ruling(key: str, *, action: str, park_class: str = _CLASS) -> dict:
    return {
        "question": "failure triage",
        "story_key": key,
        "action": action,
        "park_class": park_class,
        "decided_at": f"2026-11-01T00:00:{key}",
    }


def _human(ruling: dict, human_action: str) -> dict:
    return {
        "question": "human resolution",
        "story_key": ruling["story_key"],
        "ruling_ref": ruling["decided_at"],
        "ruling_action": ruling["action"],
        "park_class": ruling["park_class"],
        "human_action": human_action,
    }


def _paired_mix() -> list[dict]:
    """A park the human agreed with, and a proceed the human parked (a false proceed)."""
    agree = _ruling("A", action=tr.PARK_ACTION)
    proceed = _ruling("B", action="retry")
    return [agree, _human(agree, "park"), proceed, _human(proceed, "park")]


def test_three_agreements_and_one_disagreement_count_four_choices_and_three_agreements() -> None:
    records = [_choice(chose_recommended=True, story_key=f"s{i}") for i in range(3)]
    records.append(_choice(chose_recommended=False, story_key="s3"))

    verdict = tr.readiness(records)[_CLASS]

    assert verdict["park_option_choices"] == 4
    assert verdict["park_option_agreements"] == 3


def test_choices_leave_the_paired_and_false_counts_unchanged() -> None:
    records = _paired_mix()
    without = tr.readiness(records)[_CLASS]

    with_choices = tr.readiness(records + [_choice(chose_recommended=False), _choice(chose_recommended=True)])[_CLASS]

    assert {key: with_choices[key] for key in _PAIRED_KEYS} == {key: without[key] for key in _PAIRED_KEYS}
    assert with_choices["park_option_choices"] == 2


def test_park_options_records_are_not_counted_as_choices() -> None:
    offered = {
        "kind": "park_options",
        "story_key": "s1",
        "park_class": _CLASS,
        "options": [],
        "recommended": "o1",
    }

    verdict = tr.readiness([offered])[_CLASS]

    assert verdict["park_option_choices"] == 0
    assert verdict["park_option_agreements"] == 0


def test_choice_without_a_usable_park_class_is_skipped_without_raising() -> None:
    no_class = {k: v for k, v in _choice(chose_recommended=True).items() if k != "park_class"}
    records = [no_class, _choice(park_class=None, chose_recommended=True), _choice(park_class="", chose_recommended=True)]

    report = tr.readiness(records)

    assert all(verdict["park_option_choices"] == 0 for verdict in report.values())


def test_choice_for_a_class_with_no_rulings_is_still_reported() -> None:
    report = tr.readiness([_choice(park_class="rebase_conflict", chose_recommended=True)])

    assert report["rebase_conflict"]["park_option_choices"] == 1
    assert report["rebase_conflict"]["eligible"] is False
