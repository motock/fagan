"""Tests for the four new ACTION values in the ruling parser and executor registry.

Written FIRST (TDD). The parser must accept resolve_rebase, retry_transient,
rebrief and approve_test_edit; an unrecognised value still fails closed to
park_for_human. approve_test_edit is registered to its real executor (ov-5).
"""

import pytest

from pipeline import triage_executors
from pipeline.parsers import TRIAGE_ACTIONS, _parse_ruling
from pipeline.triage_test_edit import execute_approve_test_edit

NEW_ACTIONS = ("resolve_rebase", "retry_transient", "rebrief", "approve_test_edit")


def _ruling_text(action: str) -> str:
    return f"RULING: r\nTIER: hard\nRISK: low\nRATIONALE: why\nNOTIFY_USER: no\nACTION: {action}\n"


@pytest.mark.parametrize("action", NEW_ACTIONS)
def test_parser_accepts_new_action(action):
    assert _parse_ruling(_ruling_text(action))["action"] == action


@pytest.mark.parametrize("action", NEW_ACTIONS)
def test_new_action_is_in_triage_actions(action):
    assert action in TRIAGE_ACTIONS


def test_unrecognised_action_fails_closed_to_park_for_human():
    assert _parse_ruling(_ruling_text("delete_everything"))["action"] == "park_for_human"


def test_missing_action_fails_closed_to_park_for_human():
    text = "RULING: r\nTIER: hard\nRISK: low\nRATIONALE: why\nNOTIFY_USER: no\n"
    assert _parse_ruling(text)["action"] == "park_for_human"


# retry_transient and rebrief have real executors (ov-2); resolve_rebase has
# one (ov-3); approve_test_edit has one (ov-5). The registry has no stub.
def test_approve_test_edit_is_registered_to_its_executor():
    assert triage_executors.EXECUTORS["approve_test_edit"] is execute_approve_test_edit


def test_register_stores_the_decorated_function():
    @triage_executors.register("probe_action")
    def probe(plan_name, story_key, story, ruling, manifest):
        return "probed"

    try:
        assert triage_executors.EXECUTORS["probe_action"] is probe
    finally:
        triage_executors.EXECUTORS.pop("probe_action", None)


def test_executor_registry_has_no_entry_for_existing_action():
    assert "amend_scope" not in triage_executors.EXECUTORS
