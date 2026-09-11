"""Docs-only guard tests for docs/adr/ (Architecture Decision Records).

The ADR set is a cumulative artifact: later stories legitimately add new
numbered records and grow the index table in docs/adr/README.md.  These tests
therefore assert *membership per file* (every numbered ADR is indexed, every
record carries Status/Date and the three Nygard sections) and never the index's
exact table contents or an exact record count.

Path resolution mirrors tests/unit/test_readme_reference_split.py: resolve the
repo root relative to this file rather than hardcoding an absolute path.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = REPO_ROOT / "docs" / "adr"
README_PATH = ADR_DIR / "README.md"
TEMPLATE_PATH = ADR_DIR / "template.md"

# The glob that defines "a numbered ADR record".  template.md deliberately does
# not match it (it does not start with a digit).
NUMBERED_ADR_GLOB = "0*.md"

NYGARD_SECTIONS = ("## Context", "## Decision", "## Consequences")


def _numbered_adr_files() -> list[Path]:
    """Return the numbered ADR records, sorted, via the '0*.md' glob.

    Fails with a pointed message rather than returning an empty list, so the
    per-file tests below can never pass vacuously against a missing/empty
    docs/adr/ directory.
    """
    files = sorted(ADR_DIR.glob(NUMBERED_ADR_GLOB))
    assert files, (
        f"no files matched {ADR_DIR}/{NUMBERED_ADR_GLOB} - the ADR records are "
        f"missing (docs/adr/ must ship numbered records such as "
        f"0001-....md; see docs/adr/README.md)"
    )
    return files


def test_adr_directory_exists():
    """docs/adr/ is a directory."""
    assert ADR_DIR.exists(), f"missing directory: {ADR_DIR}"
    assert ADR_DIR.is_dir(), f"not a directory: {ADR_DIR}"


def test_every_adr_has_status_and_date():
    """Each numbered ADR carries a '**Status:**' and a '**Date:**' line."""
    for adr in _numbered_adr_files():
        text = adr.read_text(encoding="utf-8")
        assert "**Status:**" in text, f"{adr.name} is missing '**Status:**'"
        assert "**Date:**" in text, f"{adr.name} is missing '**Date:**'"


def test_every_adr_has_the_nygard_sections():
    """Each numbered ADR contains the Context/Decision/Consequences sections."""
    for adr in _numbered_adr_files():
        text = adr.read_text(encoding="utf-8")
        for section in NYGARD_SECTIONS:
            assert section in text, f"{adr.name} is missing the '{section}' section"


def test_index_lists_every_adr_file():
    """Every numbered ADR's basename appears in docs/adr/README.md.

    Membership is asserted per file, never as an exact table or count: the
    index legitimately grows as later stories add records.  This is the guard
    that makes a future ADR added without an index entry fail.
    """
    assert README_PATH.exists(), f"missing ADR index: {README_PATH}"
    index_text = README_PATH.read_text(encoding="utf-8")
    for adr in _numbered_adr_files():
        assert adr.name in index_text, (
            f"{adr.name} is not listed in docs/adr/README.md - every numbered "
            f"ADR must have an index entry"
        )


def test_template_is_not_counted_as_a_record():
    """template.md exists but is outside the '0*.md' record glob."""
    assert TEMPLATE_PATH.exists(), f"missing ADR template: {TEMPLATE_PATH}"
    matched_names = [p.name for p in ADR_DIR.glob(NUMBERED_ADR_GLOB)]
    assert "template.md" not in matched_names, (
        "docs/adr/template.md must not be matched by the "
        f"'{NUMBERED_ADR_GLOB}' glob used to enumerate ADR records - the "
        "template cannot be graded as a record"
    )