"""Membership test for the failure modes landed by RH-05 (Modes 56-60).

Asserts MEMBERSHIP only. The dataset is cumulative: it may only grow, so this
file must never pin a total entry count (that pin is the defect RH-05 removed
from tests/unit/test_failure_modes_dataset.py).
"""

import json
from pathlib import Path

DATASET = Path(__file__).resolve().parents[2] / "docs" / "failure_modes.json"

NEW_MODES = {"56", "57", "58", "59", "60"}


def _entries():
    return json.loads(DATASET.read_text(encoding="utf-8"))


def test_new_modes_are_present_by_name():
    modes = {entry["mode"] for entry in _entries()}
    assert NEW_MODES <= modes, (
        f"missing modes: {sorted(NEW_MODES - modes)}"
    )


def test_new_modes_have_the_six_required_keys_and_non_empty_values():
    by_mode = {entry["mode"]: entry for entry in _entries()}
    for mode in sorted(NEW_MODES):
        entry = by_mode[mode]
        assert set(entry.keys()) == {
            "mode",
            "date",
            "class",
            "status",
            "fix_ref",
            "guard",
        }
        for key, value in entry.items():
            assert isinstance(value, str) and value != "", (mode, key, value)
