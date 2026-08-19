"""Acceptance oracle: `_parse_ruling` must surface a normalized `action`.

C3 of docs/plans/OVERLORD_FAILURE_TRIAGE_PLAN.md: `_parse_ruling` validates
nothing and defaults every missing field to "". The new ACTION field must fail
CLOSED to "park_for_human" so an absent, malformed, or unrecognized value
degrades to today's park-and-notify behaviour rather than acting on a value the
executor does not understand.
"""

import pytest

from pipeline.parsers import _parse_ruling

_BASE = (
    "RULING: do the thing\n"
    "TIER: routine\n"
    "RISK: low\n"
    "RATIONALE: because it is safe\n"
    "NOTIFY_USER: no\n"
)


@pytest.mark.parametrize(
    "action",
    ["escalate_model", "split_story", "repo_issue", "park_for_human"],
)
def test_recognized_action_is_returned_verbatim(action):
    assert _parse_ruling(_BASE + f"ACTION: {action}\n")["action"] == action


def test_action_is_case_and_whitespace_normalized():
    ruling = _parse_ruling(_BASE + "ACTION:   Escalate_Model  \n")
    assert ruling["action"] == "escalate_model"


def test_missing_action_fails_closed_to_park():
    assert _parse_ruling(_BASE)["action"] == "park_for_human"


def test_unrecognized_action_fails_closed_to_park():
    assert _parse_ruling(_BASE + "ACTION: delete_the_repo\n")["action"] == "park_for_human"


def test_empty_action_fails_closed_to_park():
    assert _parse_ruling(_BASE + "ACTION:\n")["action"] == "park_for_human"


def test_empty_text_fails_closed_to_park():
    assert _parse_ruling("")["action"] == "park_for_human"


def test_existing_contract_fields_are_unchanged():
    ruling = _parse_ruling(_BASE + "ACTION: repo_issue\n")
    assert ruling["ruling"] == "do the thing"
    assert ruling["tier"] == "routine"
    assert ruling["risk"] == "low"
    assert ruling["rationale"] == "because it is safe"
    assert ruling["notify_user"] is False
