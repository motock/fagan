"""Modes 57-60 were marked FIXED on 2026-10 (story FMC-1).

Asserts only what that sync changed; the catalog is cumulative.
"""

import json
from pathlib import Path

DATASET = Path(__file__).resolve().parents[2] / "docs" / "failure_modes.json"
REQUIRED_KEYS = {"mode", "date", "class", "status", "fix_ref", "guard"}


def _by_mode():
    entries = json.loads(DATASET.read_text(encoding="utf-8"))
    return {e["mode"]: e for e in entries}


def test_should_mark_mode_57_fixed_with_commit_ref():
    entry = _by_mode()["57"]
    assert entry["status"] == "FIXED"
    assert "352b75b" in entry["fix_ref"]


def test_should_mark_mode_58_fixed_with_commit_ref():
    entry = _by_mode()["58"]
    assert entry["status"] == "FIXED"
    assert "25151b7" in entry["fix_ref"]


def test_should_mark_mode_59_fixed_with_commit_ref():
    entry = _by_mode()["59"]
    assert entry["status"] == "FIXED"
    assert "83d9766" in entry["fix_ref"]


def test_should_mark_mode_60_fixed_with_commit_ref():
    entry = _by_mode()["60"]
    assert entry["status"] == "FIXED"
    assert "7258867" in entry["fix_ref"]


def test_should_keep_exactly_six_keys_on_modes_57_to_60():
    by_mode = _by_mode()
    for_keys = [set(by_mode[m]) for m in ("57", "58", "59", "60")]
    assert all(keys == REQUIRED_KEYS for keys in for_keys)


def test_should_not_leave_empty_fix_ref_on_modes_57_to_60():
    by_mode = _by_mode()
    refs = [by_mode[m]["fix_ref"].strip() for m in ("57", "58", "59", "60")]
    assert all(refs)
