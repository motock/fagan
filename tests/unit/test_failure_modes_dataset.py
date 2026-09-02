"""Structure + spot-check tests for the docs/failure_modes.json dataset.

Pins STRUCTURE and named spot-check values only. Deliberately does NOT assert
byte length or a content hash: the dataset may be re-serialized (whitespace,
key order) without breaking the contract.
"""

import json
import re
from pathlib import Path

DATASET = Path(__file__).resolve().parents[2] / "docs" / "failure_modes.json"

REQUIRED_KEYS = {"mode", "date", "class", "status", "fix_ref", "guard"}
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A value made only of digits and dashes is attempting to be an ISO date; any
# other character (~, /, space, letters) marks a deliberate verbatim non-ISO
# date (e.g. "2026-07-01/02", "~2026-07-02", "n/a (found 2026-08-12)").
ISO_LIKE_RE = re.compile(r"^[\d-]+$")


def _entries():
    return json.loads(DATASET.read_text(encoding="utf-8"))


def test_dataset_is_a_list_of_57_objects_with_exactly_the_six_keys():
    entries = _entries()
    assert isinstance(entries, list)
    assert len(entries) == 57
    for entry in entries:
        assert isinstance(entry, dict)
        assert set(entry.keys()) == REQUIRED_KEYS
        for value in entry.values():
            assert isinstance(value, str)


def test_mode_values_are_unique_strings_including_sub_entries():
    modes = [entry["mode"] for entry in _entries()]
    assert all(isinstance(m, str) for m in modes)
    assert len(set(modes)) == 57
    assert {"16", "16b", "16-recur"} <= set(modes)


def test_all_fields_are_non_empty_strings():
    for entry in _entries():
        for key in ("mode", "date", "class", "status", "fix_ref", "guard"):
            assert isinstance(entry[key], str) and entry[key] != ""


def test_iso_shaped_dates_match_yyyy_mm_dd_exactly():
    for entry in _entries():
        date = entry["date"]
        if ISO_LIKE_RE.fullmatch(date):
            assert ISO_DATE_RE.fullmatch(date), (
                f"mode {entry['mode']}: date {date!r} is digit/dash-only but "
                "not exactly YYYY-MM-DD"
            )


def test_spot_checks_pin_verbatim_values():
    by_mode = {entry["mode"]: entry for entry in _entries()}
    assert by_mode["1"]["guard"] == "none identified"
    assert by_mode["1"]["class"] == "harness-bug"
    assert by_mode["16-recur"]["date"] == "2026-07-11"
    assert by_mode["16-recur"]["status"] == "recurring"
    assert by_mode["16b"]["date"] == "~2026-07-02"
    assert by_mode["16b"]["class"] == "operational"
    assert by_mode["16"]["date"] == "2026-07-01/02"
    assert by_mode["52"]["status"].startswith("NOT fixed")
    assert by_mode["52"]["guard"] == "none (gap, not a guard)"