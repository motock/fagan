"""The forge selector is documented where operators look for it (FORGE-S3).

``PIPELINE_FORGE`` is user-visible: it decides which code host the pipeline
drives and therefore which CLI the installer requires. A new env var without
its docs is an undocumented knob, so the reference table and the README
prerequisites must both name it in the same change.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REFERENCE = _REPO_ROOT / "REFERENCE.md"
_README = _REPO_ROOT / "README.md"


def test_reference_documents_pipeline_forge():
    text = _REFERENCE.read_text(encoding="utf-8")

    assert "PIPELINE_FORGE" in text
    # The three accepted values, asserted separately: the table cell escapes
    # its pipes as ``\|``, so the raw "auto|github|gitlab" never appears.
    for value in ("auto", "github", "gitlab"):
        assert value in text, value
    # The row itself must carry the resolution rule and the failure mode.
    row = next(
        line for line in text.splitlines() if line.startswith("| `PIPELINE_FORGE`")
    )
    assert "origin" in row.lower(), "the origin-remote resolution rule is missing"
    assert "raise" in row.lower(), "the unknown-value failure mode is missing"


def test_readme_prerequisites_mentions_pipeline_forge():
    lines = _README.read_text(encoding="utf-8").splitlines()

    start = lines.index("## Prerequisites")
    # The prerequisites section ends at the next top-level heading.
    end = next(
        i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")
    )
    section = lines[start:end]

    rows = [line for line in section if "PIPELINE_FORGE" in line]
    assert rows, "PIPELINE_FORGE must appear in the README prerequisites"
    for row in rows:
        # The prerequisites section is a bullet list, so the entry must be a
        # list item inside it - not prose floating outside the section.
        assert row.lstrip().startswith("- "), f"not a list item: {row!r}"
        assert "`PIPELINE_FORGE`" in row, f"the variable must be code-quoted: {row!r}"
