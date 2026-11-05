"""Guard tests for ADR 0012, "The Store holds namespaced records".

The record itself is prose, so these tests pin the parts of it that a later
edit could silently drift: its status line, the two phrases the rest of the
design leans on, its index entry in docs/adr/README.md, and the absence of
secret material. They read the files as text and never parse Markdown.

Path resolution follows tests/unit/test_adr_records.py: resolve the repo root
relative to this file rather than hardcoding an absolute path.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADR = ROOT / "docs/adr/0012-store-contract-v2.md"
README = ROOT / "docs/adr/README.md"


def adr_text() -> str:
    """Return the ADR's text, failing with an assertion when it is missing."""
    assert ADR.exists(), f"missing {ADR}"
    return ADR.read_text(encoding="utf-8")


def test_adr_file_exists():
    """docs/adr/0012-store-contract-v2.md exists."""
    assert ADR.exists(), f"missing {ADR}"
    assert ADR.is_file(), f"not a file: {ADR}"


def test_status_is_proposed():
    """The record carries a Proposed status line."""
    assert "**Status:** Proposed" in adr_text()


def test_status_is_not_accepted():
    """Recording the decision for review does not accept it."""
    assert "**Status:** Accepted" not in adr_text()


def test_core_always_passes_the_default_namespace():
    """Decision 2: core gives the namespace no meaning and always passes "default"."""
    text = adr_text()
    assert "core always" in text
    assert '"default"' in text


def test_alternatives_considered_section_is_present():
    """The record keeps its '### Alternatives considered' section."""
    assert "### Alternatives considered" in adr_text()


def test_manifest_is_the_plan():
    """Decision 5: the manifest is the single plan record."""
    assert "the manifest is the plan" in adr_text()


def test_readme_index_lists_0012_as_proposed():
    """The ADR index lists 0012 with status Proposed on the same line."""
    assert README.exists(), f"missing {README}"
    lines = README.read_text(encoding="utf-8").splitlines()
    matching = [
        line
        for line in lines
        if "0012-store-contract-v2.md" in line and "Proposed" in line
    ]
    assert matching, (
        "docs/adr/README.md has no index line containing both "
        "'0012-store-contract-v2.md' and 'Proposed'"
    )


def test_adr_contains_no_secrets():
    """The record must never carry secret material."""
    text = adr_text()
    for marker in ("BEGIN PRIVATE KEY", "ssh-rsa", "password="):
        assert marker not in text, f"ADR 0012 contains secret-like text: {marker}"
