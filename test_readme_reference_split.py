"""Tests for the README.md / REFERENCE.md documentation split.

This story reorganizes a single README.md into two files: README.md keeps the
conceptual overview, and a new REFERENCE.md receives the deep reference
material moved verbatim. These tests assert the structural success criteria
described in the story brief. They are RED until the split is performed.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
README = REPO_ROOT / "README.md"
REFERENCE = REPO_ROOT / "REFERENCE.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def h2_headings(text: str) -> list[str]:
    """Return the ordered list of H2 (##) heading titles in `text`.

    Headings are matched on lines that start at column 0 with '## '.
    """
    headings: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            headings.append(line[3:].strip())
    return headings


# ---------------------------------------------------------------------------
# REFERENCE.md existence & title
# ---------------------------------------------------------------------------

def test_reference_md_exists():
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"


def test_reference_md_has_title():
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"
    first = REFERENCE.read_text().splitlines()[0]
    assert first.startswith("# "), "REFERENCE.md must start with an H1 title"


def test_reference_md_title_mentions_readme_quickstart():
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"
    head = "\n".join(REFERENCE.read_text().splitlines()[:5])
    assert "README" in head, (
        "REFERENCE.md's header should note it is the detailed reference for "
        "README.md's quickstart"
    )


# ---------------------------------------------------------------------------
# Moved sections landed in REFERENCE.md, in order
# ---------------------------------------------------------------------------

EXPECTED_REFERENCE_SECTIONS = [
    "MCP tools reference",
    "Status lifecycle",
    "Monitoring dashboard",
    "Plan / story schema",
    "Per-role provider/model configuration",
    "Guided decomposition (the tech-lead planner)",
    "TDD-split (test-author phase)",
    "Configuration (environment variables)",
    "End-to-end workflow",
    "Safety",
    "Usage gate & resumability",
    "Unattended operation & logs",
    "Development & testing",
]


def test_reference_md_contains_all_moved_sections():
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"
    text = REFERENCE.read_text()
    headings = h2_headings(text)
    for section in EXPECTED_REFERENCE_SECTIONS:
        assert section in headings, (
            f"REFERENCE.md must contain H2 '## {section}' (found: {headings})"
        )


def test_reference_md_sections_in_order():
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"
    headings = h2_headings(REFERENCE.read_text())
    # The first H2 in REFERENCE.md should be the first moved section.
    moved = [h for h in headings if h in EXPECTED_REFERENCE_SECTIONS]
    assert moved == EXPECTED_REFERENCE_SECTIONS, (
        f"Moved sections must appear in their original order; got {moved}"
    )


def test_reference_md_starts_with_mcp_tools_reference():
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"
    headings = h2_headings(REFERENCE.read_text())
    assert headings, "REFERENCE.md must have at least one H2 heading"
    assert headings[0] == "MCP tools reference", (
        f"First H2 in REFERENCE.md should be 'MCP tools reference', got {headings[0]!r}"
    )


def test_reference_md_ends_with_development_and_testing():
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"
    headings = h2_headings(REFERENCE.read_text())
    assert headings, "REFERENCE.md must have at least one H2 heading"
    assert headings[-1] == "Development & testing", (
        f"Last H2 in REFERENCE.md should be 'Development & testing', "
        f"got {headings[-1]!r}"
    )


# ---------------------------------------------------------------------------
# MCP tools reference H3 subsections preserved
# ---------------------------------------------------------------------------

EXPECTED_MCP_H3_SUBSECTIONS = [
    "Planning",
    "Dispatch & status",
    "Resumability",
    "Decisions",
    "Review & merge",
    "Usage gate",
    "Orchestration",
    "Manual status",
]


def test_reference_md_preserves_mcp_h3_subsections():
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"
    text = REFERENCE.read_text()
    # Find the MCP tools reference section body (up to the next H2).
    lines = text.splitlines()
    in_fence = False
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
        if in_fence:
            continue
        if line.startswith("## MCP tools reference"):
            start = i
            continue
        if start is not None and line.startswith("## "):
            end = i
            break
    assert start is not None, "## MCP tools reference section not found in REFERENCE.md"
    body = "\n".join(lines[start:end])
    h3 = [
        line[3:].strip()
        for line in body.splitlines()
        if line.startswith("### ") and not line.startswith("```")
    ]
    for sub in EXPECTED_MCP_H3_SUBSECTIONS:
        assert any(h3_title == sub or h3_title.startswith(sub + " ") for h3_title in h3), (
            f"MCP tools reference must keep H3 '### {sub}' (found: {h3})"
        )


# ---------------------------------------------------------------------------
# README.md retains the conceptual sections, in order, and gains 'Reference'
# ---------------------------------------------------------------------------

EXPECTED_README_SECTIONS = [
    "Components at a glance",
    "Architecture",
    "Personas (`~/.claude/agents/`)",
    "The overlord and the decision policy",
    "Reference",
    "Prerequisites",
    "License",
]


def test_readme_h2_count_and_order():
    assert README.is_file(), "README.md must exist at the repo root"
    headings = h2_headings(README.read_text())
    assert headings == EXPECTED_README_SECTIONS, (
        f"README.md H2 sections must be exactly {EXPECTED_README_SECTIONS}; "
        f"got {headings}"
    )


def test_readme_has_seven_h2_sections():
    assert README.is_file(), "README.md must exist at the repo root"
    headings = h2_headings(README.read_text())
    assert len(headings) == 7, (
        f"README.md must have exactly 7 H2 sections, got {len(headings)}: {headings}"
    )


def test_readme_reference_section_links_to_reference_md():
    assert README.is_file(), "README.md must exist at the repo root"
    text = README.read_text()
    # The new '## Reference' section must link to REFERENCE.md.
    assert "REFERENCE.md" in text, (
        "README.md must contain a link to REFERENCE.md in its Reference section"
    )
    # And the link should appear within the Reference section body.
    lines = text.splitlines()
    in_fence = False
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
        if in_fence:
            continue
        if line.startswith("## Reference"):
            start = i
            continue
        if start is not None and line.startswith("## "):
            end = i
            break
    assert start is not None, "## Reference section not found in README.md"
    body = "\n".join(lines[start:end])
    assert "REFERENCE.md" in body, (
        "The '## Reference' section must link to REFERENCE.md"
    )


def test_readme_reference_section_sits_between_overlord_and_prerequisites():
    assert README.is_file(), "README.md must exist at the repo root"
    headings = h2_headings(README.read_text())
    assert "The overlord and the decision policy" in headings
    assert "Reference" in headings
    assert "Prerequisites" in headings
    i_overlord = headings.index("The overlord and the decision policy")
    i_ref = headings.index("Reference")
    i_prereq = headings.index("Prerequisites")
    assert i_overlord < i_ref < i_prereq, (
        "## Reference must sit between 'The overlord and the decision policy' "
        "and 'Prerequisites'"
    )


# ---------------------------------------------------------------------------
# README.md no longer contains the moved reference sections
# ---------------------------------------------------------------------------

MOVED_SECTIONS = [
    "MCP tools reference",
    "Status lifecycle",
    "Monitoring dashboard",
    "Plan / story schema",
    "Per-role provider/model configuration",
    "Guided decomposition (the tech-lead planner)",
    "TDD-split (test-author phase)",
    "Configuration (environment variables)",
    "End-to-end workflow",
    "Safety",
    "Usage gate & resumability",
    "Unattended operation & logs",
    "Development & testing",
]


def test_readme_does_not_contain_moved_sections():
    assert README.is_file(), "README.md must exist at the repo root"
    headings = h2_headings(README.read_text())
    for section in MOVED_SECTIONS:
        assert section not in headings, (
            f"README.md must no longer contain H2 '## {section}' "
            f"(still present: {[h for h in headings if h in MOVED_SECTIONS]})"
        )


# ---------------------------------------------------------------------------
# Intro paragraph updated to describe README's narrower scope + point to REFERENCE
# ---------------------------------------------------------------------------

OLD_INTRO_FRAGMENT = (
    "This document is the reference for the whole system: the personas, the "
    "overlord decision protocol, the pipeline MCP tools, the end-to-end "
    "workflow, configuration, and safety controls."
)


def test_readme_intro_no_longer_claims_to_be_the_whole_reference():
    assert README.is_file(), "README.md must exist at the repo root"
    text = README.read_text()
    assert OLD_INTRO_FRAGMENT not in text, (
        "README.md's intro paragraph must be updated; the old whole-system "
        "reference sentence must be removed."
    )


def test_readme_intro_points_to_reference_md():
    assert README.is_file(), "README.md must exist at the repo root"
    # The intro paragraph is before the first H2 heading.
    text = README.read_text()
    first_h2_idx = None
    in_fence = False
    for i, line in enumerate(text.splitlines()):
        if line.startswith("```"):
            in_fence = not in_fence
        if in_fence:
            continue
        if line.startswith("## "):
            first_h2_idx = i
            break
    assert first_h2_idx is not None, "README.md must have at least one H2"
    intro = "\n".join(text.splitlines()[:first_h2_idx])
    assert "REFERENCE.md" in intro, (
        "README.md's intro paragraph should point to REFERENCE.md for the rest"
    )


# ---------------------------------------------------------------------------
# Verbatim move: content bodies are preserved exactly
# ---------------------------------------------------------------------------

def _section_body(path: Path, title: str) -> str:
    """Return the full body of an H2 section (heading line + body up to next H2),
    with surrounding whitespace stripped, as a single string."""
    text = path.read_text()
    lines = text.splitlines()
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if line.startswith(f"## {title}") and start is None:
            start = i
            continue
        if start is not None and line.startswith("## "):
            end = i
            break
    assert start is not None, f"Section '## {title}' not found in {path}"
    return "\n".join(lines[start:end]).strip()


def _original_readme_text() -> str:
    """Return the pre-split README.md content from git (HEAD~1).

    This is the source of truth for every section that was supposed to be moved
    verbatim into REFERENCE.md.
    """
    import subprocess

    result = subprocess.run(
        ["git", "show", "HEAD~2:README.md"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.mark.parametrize("title", EXPECTED_REFERENCE_SECTIONS)
def test_moved_section_body_is_verbatim(title):
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"
    reference_body = _section_body(REFERENCE, title)
    assert reference_body.startswith(f"## {title}"), (
        f"Section body for {title!r} should begin with its own heading"
    )
    # Compare against the original README.md section body byte-for-byte.
    original_text = _original_readme_text()
    original_body = _section_body_from_text(original_text, title)
    assert reference_body == original_body, (
        f"Section body for {title!r} in REFERENCE.md is not byte-for-byte "
        f"identical to the original README.md section. The move was supposed "
        f"to be verbatim, not a summary.\n"
        f"--- original (len={len(original_body)}) ---\n{original_body[:500]}\n"
        f"--- reference (len={len(reference_body)}) ---\n{reference_body[:500]}"
    )


def _section_body_from_text(text: str, title: str) -> str:
    """Return the full body of an H2 section from raw markdown text.

    Finds the heading line by exact match, then extends to the next line
    starting with '## ' (the next H2 heading).
    """
    lines = text.splitlines()
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if line.startswith(f"## {title}") and start is None:
            start = i
            continue
        if start is not None and line.startswith("## "):
            end = i
            break
    assert start is not None, f"Section '## {title}' not found in original README"
    return "\n".join(lines[start:end]).strip()


# ---------------------------------------------------------------------------
# No internal anchor links were broken (none existed, but guard anyway)
# ---------------------------------------------------------------------------

def test_readme_has_no_internal_anchor_links():
    assert README.is_file(), "README.md must exist at the repo root"
    text = README.read_text()
    # Markdown inline anchor links look like [text](#anchor).
    anchors = re.findall(r"\]\(#[^)]+\)", text)
    assert anchors == [], (
        f"README.md should contain no internal anchor links (found: {anchors})"
    )


def test_reference_has_no_internal_anchor_links():
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"
    text = REFERENCE.read_text()
    anchors = re.findall(r"\]\(#[^)]+\)", text)
    assert anchors == [], (
        f"REFERENCE.md should contain no internal anchor links (found: {anchors})"
    )


# ---------------------------------------------------------------------------
# Boundary / negative cases
# ---------------------------------------------------------------------------

def test_reference_md_is_not_empty():
    assert REFERENCE.is_file(), "REFERENCE.md must exist at the repo root"
    assert REFERENCE.read_text().strip() != "", "REFERENCE.md must not be empty"


def test_readme_still_has_title_and_ci_badge():
    assert README.is_file(), "README.md must exist at the repo root"
    head = "\n".join(README.read_text().splitlines()[:5])
    assert head.startswith("# "), "README.md must keep its H1 title"
    assert "badge.svg" in head, "README.md must keep its CI badge"


def test_readme_keeps_license_section():
    assert README.is_file(), "README.md must exist at the repo root"
    headings = h2_headings(README.read_text())
    assert "License" in headings, "README.md must keep its '## License' section"
    assert headings[-1] == "License", (
        "'## License' should remain the final H2 in README.md"
    )