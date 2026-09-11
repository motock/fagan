"""Tests for the root CHANGELOG.md introduced by the docs-only changelog story.

The story brief requires a CHANGELOG.md at the repo root with a 0.1.0 release
section, a "Known limitations" section (this project's stated value is that
failure modes are named rather than hidden - a changelog that lists only
features would contradict the README), and a link to the v0.1.0 release tag.

These tests deliberately do NOT pin the file's total contents or a hash:
later releases append to this file, and a test pinning its total contents
would have to be edited by every future release story (see
.claude/rules/pipeline-story-schema.md on cumulative artifacts). Each test
asserts membership of a required element instead.

They are RED until CHANGELOG.md exists at the repo root.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def test_changelog_exists_at_repo_root():
    """CHANGELOG.md must be a file at the repo root."""
    assert CHANGELOG.is_file(), (
        f"CHANGELOG.md must exist at the repo root ({REPO_ROOT}); "
        "the docs-only changelog story creates it."
    )


def test_changelog_has_a_0_1_0_section():
    """It must contain a heading line for the 0.1.0 release.

    Match the '## [0.1.0]' prefix rather than the whole line so the release
    date can change without breaking this test.
    """
    assert CHANGELOG.is_file(), "CHANGELOG.md must exist before it can be parsed"
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    matching = [line for line in lines if line.startswith("## [0.1.0]")]
    assert matching, (
        "CHANGELOG.md must contain a '## [0.1.0]' release heading line "
        "(matched on the prefix, not the whole line, so the date can change)"
    )


def test_changelog_documents_known_limitations():
    """It must contain a 'Known limitations' heading.

    This project's stated value is that failure modes are named rather than
    hidden; a changelog that lists only features would contradict the README.
    """
    assert CHANGELOG.is_file(), "CHANGELOG.md must exist before it can be parsed"
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    matching = [
        line
        for line in lines
        if line.lstrip().startswith("#") and "Known limitations" in line
    ]
    assert matching, (
        "CHANGELOG.md must contain a 'Known limitations' heading - "
        "failure modes are documented rather than hidden"
    )


def test_changelog_links_the_release_tag():
    """It must contain the string 'v0.1.0' (the release tag link)."""
    assert CHANGELOG.is_file(), "CHANGELOG.md must exist before it can be parsed"
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "v0.1.0" in text, (
        "CHANGELOG.md must link the release tag (it must contain 'v0.1.0')"
    )