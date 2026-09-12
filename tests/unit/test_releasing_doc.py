"""Tests for docs/RELEASING.md (REL02-3) and its README link.

This story is DOCS-ONLY: it adds ``docs/RELEASING.md`` describing the existing
manual release procedure (versioning, pre-flight checks, changelog entry,
tagging/publishing, post-release checks) and ONE link to it from README.md.

These tests deliberately assert only MEMBERSHIP (substrings, heading prefixes,
fenced-block contents) - never README's or RELEASING.md's total contents, byte
length, line count or a whole-file hash - because both files are edited by many
stories over time.

The landmine guard (``test_readme_h2_headings_unchanged``) must pass both
before and after the edit: README's ``## `` heading list is pinned to exactly
11 headings by tests/unit/test_readme_launchd_generator_docs.py and
tests/unit/test_readme_standalone_section.py, so the new link MUST NOT be a new
H2. It has to be an H3 or a line inside an existing section.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
README_PATH = REPO_ROOT / "README.md"
RELEASING_PATH = REPO_ROOT / "docs" / "RELEASING.md"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

#: The exact H2 list pinned by tests/unit/test_readme_launchd_generator_docs.py
#: (EXPECTED_H2_SECTIONS) and tests/unit/test_readme_standalone_section.py
#: (EXPECTED_H2_HEADINGS). The RELEASING.md link MUST NOT add, remove, rename
#: or reorder any of these.
EXPECTED_H2_HEADINGS = [
    "Platform support",
    "Quickstart",
    "Components at a glance",
    "Architecture",
    "Personas (`~/.claude/agents/`)",
    "The overlord and the decision policy",
    "Reference",
    "Prerequisites",
    "Scheduler",
    "Reliability & limitations",
    "License",
]

#: Stable substring prefixes for the five required sections, in the order the
#: story specifies. Each entry is a tuple of acceptable alternatives, matched
#: case-insensitively against the text of a ``## `` heading line, so the
#: implementer may word the rest of each heading freely.
REQUIRED_SECTION_SUBSTRINGS = [
    ("Versioning",),
    ("Pre-flight", "Preflight"),
    ("changelog",),
    ("Tagging",),
    ("After the release", "Post-release", "Post release"),
]

#: The exact commands the story requires, shown as a copy-pasteable block.
REQUIRED_COMMANDS = [
    "git tag -a",
    "git push origin",
    "gh release create",
]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_MD_LINK_RE = re.compile(r"\]\(([^)\s]*RELEASING\.md)\)")


# --- helpers ----------------------------------------------------------------


def _read(path: Path) -> str:
    assert path.exists(), f"expected file not found: {path}"
    assert path.is_file(), f"expected a regular file, got: {path}"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path} exists but is empty"
    return text


def _strip_fenced_code(text: str) -> list[str]:
    """Lines with fenced (```...```) code-block content blanked out, so ``#``
    lines inside code blocks are not mistaken for headings."""
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines.append("")
        elif in_fence:
            lines.append("")
        else:
            lines.append(line)
    return lines


def _headings(stripped: list[str]) -> list[tuple[int, int, str]]:
    """(line_index, level, text) for every ATX heading outside code fences."""
    found: list[tuple[int, int, str]] = []
    for i, line in enumerate(stripped):
        match = _HEADING_RE.match(line)
        if match:
            found.append((i, len(match.group(1)), match.group(2)))
    return found


def _h2_texts(text: str) -> list[str]:
    return [t for _, level, t in _headings(_strip_fenced_code(text)) if level == 2]


def _fenced_blocks(text: str) -> list[str]:
    """Raw contents of every ``` fenced block, fences excluded."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current))
                current = None
        elif current is not None:
            current.append(line)
    return blocks


# --- 1. the file exists -----------------------------------------------------


def test_releasing_doc_exists_and_is_a_file() -> None:
    assert RELEASING_PATH.exists(), (
        f"docs/RELEASING.md is missing; expected it at {RELEASING_PATH}"
    )
    assert RELEASING_PATH.is_file(), f"{RELEASING_PATH} is not a regular file"


def test_releasing_doc_is_not_a_stub() -> None:
    text = _read(RELEASING_PATH)
    assert len(text.splitlines()) >= 20, (
        "docs/RELEASING.md is too short to document the release procedure"
    )


# --- 2. the five required sections -----------------------------------------


@pytest.mark.parametrize(
    "alternatives", REQUIRED_SECTION_SUBSTRINGS, ids=lambda a: a[0]
)
def test_releasing_doc_has_required_section(alternatives: tuple[str, ...]) -> None:
    text = _read(RELEASING_PATH)
    h2_texts = _h2_texts(text)
    assert any(
        any(alt.lower() in t.lower() for alt in alternatives) for t in h2_texts
    ), (
        f"docs/RELEASING.md has no `## ` heading matching any of {alternatives}; "
        f"found H2 headings: {h2_texts}"
    )


def test_releasing_doc_sections_are_in_order() -> None:
    """The five sections must appear in the order the story specifies."""
    text = _read(RELEASING_PATH)
    h2_texts = [t.lower() for t in _h2_texts(text)]
    positions: list[int] = []
    for alternatives in REQUIRED_SECTION_SUBSTRINGS:
        matches = [
            i
            for i, t in enumerate(h2_texts)
            if any(alt.lower() in t for alt in alternatives)
        ]
        assert matches, (
            f"docs/RELEASING.md has no `## ` heading matching any of "
            f"{alternatives}; found H2 headings: {h2_texts}"
        )
        positions.append(matches[0])
    assert positions == sorted(positions), (
        "the required sections are out of order; expected "
        f"{[a[0] for a in REQUIRED_SECTION_SUBSTRINGS]}, got H2 headings {h2_texts}"
    )


# --- 3. the exact commands --------------------------------------------------


@pytest.mark.parametrize("command", REQUIRED_COMMANDS)
def test_releasing_doc_contains_required_command(command: str) -> None:
    text = _read(RELEASING_PATH)
    assert command in text, (
        f"docs/RELEASING.md does not contain the literal command {command!r}"
    )


def test_releasing_doc_commands_are_in_one_copy_pasteable_block() -> None:
    text = _read(RELEASING_PATH)
    blocks = _fenced_blocks(text)
    assert any(
        all(command in block for command in REQUIRED_COMMANDS) for block in blocks
    ), (
        "no single fenced code block in docs/RELEASING.md contains all of "
        f"{REQUIRED_COMMANDS}; the commands must be copy-pasteable together"
    )


def test_releasing_doc_uses_annotated_tag_and_notes_file() -> None:
    text = _read(RELEASING_PATH)
    assert "--notes-file" in text, (
        "docs/RELEASING.md should show `gh release create ... --notes-file <notes>`"
    )
    assert "vX.Y.Z" in text, (
        "docs/RELEASING.md should show the vX.Y.Z tag placeholder in its commands"
    )


# --- 4. changelog entry -----------------------------------------------------


def test_releasing_doc_mentions_changelog() -> None:
    text = _read(RELEASING_PATH)
    assert "CHANGELOG.md" in text, (
        "docs/RELEASING.md must reference CHANGELOG.md in the changelog step"
    )


@pytest.mark.parametrize("category", ["Added", "Fixed", "Changed"])
def test_releasing_doc_explains_changelog_categories(category: str) -> None:
    text = _read(RELEASING_PATH)
    assert category in text, (
        f"docs/RELEASING.md does not explain what belongs in the {category!r} "
        "changelog category"
    )


def test_releasing_doc_says_new_section_goes_above_previous() -> None:
    text = _read(RELEASING_PATH)
    lowered = text.lower()
    assert "above" in lowered, (
        "docs/RELEASING.md must say the new `## [X.Y.Z]` section goes ABOVE the "
        "previous one (CHANGELOG.md is newest-first)"
    )


# --- 5. pre-flight checks ---------------------------------------------------


def test_releasing_doc_preflight_ci_check_command() -> None:
    text = _read(RELEASING_PATH)
    assert "gh run list --branch master --limit 1" in text, (
        "docs/RELEASING.md must give the exact CI check command "
        "`gh run list --branch master --limit 1`"
    )


def test_releasing_doc_preflight_clean_tree_check() -> None:
    text = _read(RELEASING_PATH)
    assert "git status --short" in text, (
        "docs/RELEASING.md must give the clean-working-tree check "
        "`git status --short`"
    )


def test_releasing_doc_preflight_cites_definition_of_done() -> None:
    text = _read(RELEASING_PATH)
    assert "Definition of Done" in text, (
        "docs/RELEASING.md must cite CLAUDE.md's Definition of Done: a local "
        "test run is not a substitute for confirmed-green CI"
    )


def test_releasing_doc_preflight_mentions_open_stories() -> None:
    text = _read(RELEASING_PATH)
    lowered = text.lower()
    assert "open stor" in lowered, (
        "docs/RELEASING.md must include the pre-flight check that no plan has "
        "open stories (a story mid-flight means a feature straddles the tag)"
    )


def test_releasing_doc_preflight_mentions_local_suite() -> None:
    text = _read(RELEASING_PATH)
    lowered = text.lower()
    assert "pytest" in lowered or "test suite" in lowered or "full suite" in lowered, (
        "docs/RELEASING.md must include the pre-flight check that the full "
        "suite passes locally"
    )


# --- 6. versioning / trunk-based framing ------------------------------------


def test_releasing_doc_describes_semver_shaped_versioning() -> None:
    text = _read(RELEASING_PATH)
    lowered = text.lower()
    assert "semver" in lowered, (
        "docs/RELEASING.md must describe the versioning scheme as semver-shaped"
    )
    assert "minor" in lowered and "patch" in lowered, (
        "docs/RELEASING.md must say what bumps a minor vs a patch version"
    )


def test_releasing_doc_mentions_master_branch() -> None:
    text = _read(RELEASING_PATH)
    assert "master" in text, (
        "docs/RELEASING.md must refer to the `master` branch (trunk-based "
        "development)"
    )


# --- 7. post-release checks -------------------------------------------------


def test_releasing_doc_post_release_checks() -> None:
    text = _read(RELEASING_PATH)
    lowered = text.lower()
    assert "gh release view" in text or "renders" in lowered or "render" in lowered, (
        "docs/RELEASING.md must say to check the release renders on GitHub"
    )
    assert "tag" in lowered, (
        "docs/RELEASING.md must say to check the tag is on master"
    )


# --- 8. README link ---------------------------------------------------------


def test_readme_links_to_releasing_doc() -> None:
    text = _read(README_PATH)
    assert "RELEASING.md" in text, (
        "README.md does not mention RELEASING.md; add ONE link to "
        "docs/RELEASING.md (as an H3 or a line inside an existing section)"
    )


def test_readme_link_is_a_markdown_link_to_the_doc() -> None:
    text = _read(README_PATH)
    targets = _MD_LINK_RE.findall(text)
    assert targets, (
        "README.md mentions RELEASING.md but not as a markdown link; expected "
        "something like `[Releasing](docs/RELEASING.md)`"
    )
    assert any("docs/RELEASING.md" in target for target in targets), (
        f"README.md's RELEASING.md link does not point at docs/RELEASING.md: {targets}"
    )


# --- 9. NEGATIVE: the README H2 list is unchanged ---------------------------


def test_readme_h2_headings_unchanged() -> None:
    """The link must NOT be a new ``## `` heading: both sibling README tests
    pin this exact list by equality."""
    text = _read(README_PATH)
    h2_texts = _h2_texts(text)
    assert h2_texts == EXPECTED_H2_HEADINGS, (
        "README.md's `## ` heading list changed. The RELEASING.md link must be "
        "an H3 or a line inside an existing section, never a new H2.\n"
        f"expected: {EXPECTED_H2_HEADINGS}\nactual:   {h2_texts}"
    )
    assert len(h2_texts) == 11, (
        f"README.md must keep exactly 11 `## ` headings, found {len(h2_texts)}"
    )


# --- 10. NEGATIVE: no GitHub Actions release workflow -----------------------


def test_no_release_workflow_added() -> None:
    """This story is docs-only; it must not add a release workflow."""
    if not WORKFLOWS_DIR.exists():
        pytest.fail(f"expected workflows directory at {WORKFLOWS_DIR}")
    release_workflows = [
        p.name for p in WORKFLOWS_DIR.iterdir() if "release" in p.name.lower()
    ]
    assert release_workflows == [], (
        "a release workflow was added under .github/workflows/: "
        f"{release_workflows}; this story is docs-only"
    )
