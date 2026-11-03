"""Pins the standalone plan-approval default in the operator docs.

A fresh standalone install turns the `PIPELINE_PLAN_APPROVAL` gate on
(`compose.yaml` resolves `${PIPELINE_PLAN_APPROVAL-1}`, and `install.sh` writes
`1`), while the server-level default outside the standalone stack is unchanged
and documented in REFERENCE.md. Two places describe the standalone behaviour and
must stay in step with the code:

- the `PIPELINE_PLAN_APPROVAL` row of the §4 configuration table in
  `docs/DEPLOY_STANDALONE.md`, and
- the "Plan approval" subsection at the end of §6 of the same file,
- plus the `[Unreleased]` entry in `CHANGELOG.md`.

These tests assert the literals the prose must contain, so changing the default
in the compose file or the installer without updating the docs fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_STANDALONE = ROOT / "docs" / "DEPLOY_STANDALONE.md"
CHANGELOG = ROOT / "CHANGELOG.md"

# The §6 subsection heading, matched as a whole line: §7 already carries the
# dependency story's `#### Plan approval on an existing server`, whose text
# contains this string as a substring.
SECTION_RE = re.compile(r"^### Plan approval$", re.MULTILINE)

# The heading that follows the subsection, i.e. the end of the §6 slice.
NEXT_SECTION = "## 7."

TABLE_ROW_RE = re.compile(r"^\| `PIPELINE_PLAN_APPROVAL` \|", re.MULTILINE)


def _deploy_text() -> str:
    assert DEPLOY_STANDALONE.is_file(), (
        f"docs/DEPLOY_STANDALONE.md must exist ({DEPLOY_STANDALONE})"
    )
    return DEPLOY_STANDALONE.read_text(encoding="utf-8")


def _plan_approval_section() -> str:
    """The `### Plan approval` prose, up to the `## 7.` heading."""
    text = _deploy_text()
    match = SECTION_RE.search(text)
    assert match, (
        "docs/DEPLOY_STANDALONE.md must carry a '### Plan approval' subsection "
        "at the end of '## 6. Using the server'"
    )
    end = text.find(NEXT_SECTION, match.end())
    assert end != -1, (
        "the '### Plan approval' subsection must sit before the "
        "'## 7. Upgrading, backup and token lifespan' heading"
    )
    return text[match.end() : end]


def test_plan_approval_section_documents_behavior_and_disable_switch():
    """The subsection must name the variable, the tools and the spec."""
    section = _plan_approval_section()
    for required in (
        "PIPELINE_PLAN_APPROVAL=0",
        "approve_plan",
        "delegates",
        "specs/PLAN_APPROVAL.md",
    ):
        assert required in section, (
            f"the '### Plan approval' subsection must mention {required!r}; "
            "operators cannot opt out or check the contract without it"
        )


def test_configuration_table_has_a_plan_approval_row():
    """§4 must list the variable in its 5-column table."""
    text = _deploy_text()
    match = TABLE_ROW_RE.search(text)
    assert match, (
        "docs/DEPLOY_STANDALONE.md must contain a §4 table row starting with "
        "'| `PIPELINE_PLAN_APPROVAL` |'"
    )
    row = text[match.start() :].splitlines()[0]
    assert row.count("|") == 6, (
        f"the PIPELINE_PLAN_APPROVAL row must have 5 columns like its "
        f"neighbours (Variable | Purpose | Default | Example | Effect when "
        f"wrong); got: {row!r}"
    )
    assert "`1` (fresh install)" in row, (
        "the row's Default column must state '`1` (fresh install)' - the "
        "standalone stack default; the server-level default stays off"
    )


def test_changelog_unreleased_section_records_the_default_change():
    """The change is externally visible, so `[Unreleased]` must mention it."""
    assert CHANGELOG.is_file(), f"CHANGELOG.md must exist ({CHANGELOG})"
    text = CHANGELOG.read_text(encoding="utf-8")
    start = text.find("## [Unreleased]")
    assert start != -1, "CHANGELOG.md must keep a '## [Unreleased]' section"
    end = text.find("\n## [", start + 1)
    unreleased = text[start:] if end == -1 else text[start:end]
    assert "PIPELINE_PLAN_APPROVAL" in unreleased, (
        "the [Unreleased] section of CHANGELOG.md must record that the "
        "standalone stack now defaults PIPELINE_PLAN_APPROVAL on"
    )


def test_plan_approval_heading_appears_exactly_once():
    """The §7 upgrade text must not duplicate the subsection heading."""
    headings = SECTION_RE.findall(_deploy_text())
    assert len(headings) == 1, (
        "docs/DEPLOY_STANDALONE.md must contain exactly one '### Plan approval' "
        f"heading line; found {len(headings)}. A second one makes the slice "
        "above test the wrong region."
    )
