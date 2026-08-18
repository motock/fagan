"""Acceptance oracle: overlord-policy.md must publish the ACTION contract.

`pipeline.overlord._load_policy` ships this file verbatim as the overlord's
system prompt. A field the executor parses but the policy never describes is a
field the model will never emit, so the parser change (C3) is inert without the
policy text. This grades the shipped artifact, not a helper.
"""

from pathlib import Path

_POLICY = Path(__file__).resolve().parents[2] / "overlord-policy.md"

_CONTRACT_LINE = "ACTION: escalate_model | split_story | repo_issue | park_for_human"

_ACTIONS = ("escalate_model", "split_story", "repo_issue", "park_for_human")


def _policy_text() -> str:
    return _POLICY.read_text() if _POLICY.is_file() else ""


def _triage_section() -> str:
    _, _, section = _policy_text().partition("## Failure triage")
    return section


def test_policy_file_is_present():
    assert _POLICY.is_file()


def test_output_contract_declares_the_action_line():
    assert _CONTRACT_LINE in _policy_text()


def test_policy_has_a_failure_triage_section():
    assert _triage_section() != ""


def test_failure_triage_section_names_every_action():
    section = _triage_section()
    missing = [name for name in _ACTIONS if name not in section]
    assert missing == []


def test_failure_triage_section_states_the_fail_closed_default():
    assert "fail closed" in _triage_section().lower()


def test_existing_output_contract_fields_are_retained():
    text = _policy_text()
    absent = [
        field
        for field in ("RULING:", "TIER:", "RISK:", "RATIONALE:", "NOTIFY_USER:")
        if field not in text
    ]
    assert absent == []
