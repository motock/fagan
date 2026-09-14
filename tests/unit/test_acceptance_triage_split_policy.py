"""Acceptance oracle: overlord-policy.md must publish the SPLIT output line.

Story OPSA-4 adds `SPLIT:` to the output-contract block so the overlord emits
payloads `_parse_ruling` can read. The ACTION contract line is pinned
byte-identical by test_acceptance_triage_policy_contract.py and is extended by
later sibling stories (mark_done / patch_acceptance) - this file must not
require it to change, only that it is left untouched. The Failure triage
section (including the OPSA-2 parked-story matrix) must not be reworded.
"""

import re
from pathlib import Path

_POLICY = Path(__file__).resolve().parents[2] / "overlord-policy.md"

_ACTION_CONTRACT_LINE = (
    "ACTION: escalate_model | split_story | repo_issue | park_for_human"
)

_CONTRACT_FIELDS = ("RULING:", "TIER:", "RISK:", "RATIONALE:", "NOTIFY_USER:", "ACTION:")

_TRIAGE_MATRIX_ANCHORS = (
    "### Parked-story resolution",
    "Autonomy ladder:",
    "| stale bookkeeping",
    "| rework exhaustion with mechanical leftovers",
    "| repeated step-caps on oversized scope",
    "| acceptance fixture demonstrably broken at a clean baseline",
)


def _policy_text() -> str:
    return _POLICY.read_text() if _POLICY.is_file() else ""


def _output_contract_section() -> str:
    return _policy_text().partition("## Output contract")[2]


def _contract_block() -> str:
    """The fenced block that lists the output-contract fields."""
    m = re.search(r"```(.*?)```", _output_contract_section(), re.DOTALL)
    return m.group(1) if m else ""


def _split_line() -> str:
    for line in _contract_block().splitlines():
        if line.strip().startswith("SPLIT:"):
            return line.strip()
    return ""


def _triage_section() -> str:
    return _policy_text().partition("## Failure triage")[2]


def test_policy_file_is_present():
    assert _POLICY.is_file()


def test_output_contract_block_still_lists_every_field():
    block = _contract_block()
    missing = [field for field in _CONTRACT_FIELDS if field not in block]
    assert missing == []


def test_split_line_is_added_to_the_output_contract_block():
    assert _split_line() != "", (
        "the output-contract block must gain a SPLIT: line so the overlord "
        "emits a payload _parse_ruling can read"
    )


def test_split_line_documents_the_double_pipe_separator():
    assert "||" in _split_line()


def test_split_line_shows_exactly_two_child_placeholders():
    payload = _split_line().split(":", 1)[1]
    parts = [part.strip() for part in payload.split("||")]
    assert len(parts) == 2, "the SPLIT line must show exactly two children"
    assert all(parts)


def test_output_contract_says_split_is_emitted_only_for_split_story():
    section = _output_contract_section().replace(_ACTION_CONTRACT_LINE, "")
    assert "split_story" in section, (
        "the SPLIT guidance must name split_story as the trigger"
    )
    assert re.search(
        r"only[\s\S]{0,120}split_story|split_story[\s\S]{0,120}only",
        section,
        re.IGNORECASE,
    ), "the SPLIT guidance must say it is emitted only when ACTION is split_story"


def test_output_contract_requires_two_child_summaries():
    section = _output_contract_section()
    assert re.search(
        r"(?:two|2)\s+(?:child\s+)?(?:summar\w*|children|stor\w*)",
        section,
        re.IGNORECASE,
    ), "the SPLIT guidance must describe exactly two child summaries"


def test_action_contract_line_is_byte_identical():
    assert "\n" + _ACTION_CONTRACT_LINE + "\n" in _policy_text()


def test_failure_triage_section_is_not_reworded_by_this_story():
    section = _triage_section()
    missing = [anchor for anchor in _TRIAGE_MATRIX_ANCHORS if anchor not in section]
    assert missing == []


def test_failure_triage_action_definitions_retained():
    section = _triage_section()
    missing = [
        action
        for action in ("escalate_model", "split_story", "repo_issue", "park_for_human")
        if action not in section
    ]
    assert missing == []


def test_failure_triage_fail_closed_sentence_retained():
    assert "fail closed" in _triage_section().lower()
