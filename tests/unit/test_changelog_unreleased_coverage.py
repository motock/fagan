"""Static guards on the CHANGELOG's [Unreleased] section.

The v0.9.0 release notes are cut from ``CHANGELOG.md``'s ``[Unreleased]``
section, so the standalone-server work merged since v0.8.0 has to be
described there before the release commit turns the heading into
``## [0.9.0] - <date>``. These checks read the file as text: no git, no
network, no exact bullet counts and no hashes of the cumulative file.
"""

from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"

UNRELEASED_HEADING = "## [Unreleased]"
NEXT_RELEASE_PREFIX = "## ["

REQUIRED_STRINGS = [
    "repo_url",
    "mcp-token",
    "9728",
    "GHCR",
    "smoke",
    "notification",
    "procps",
    "Standalone server",
]


def _unreleased_section() -> str:
    """Return the ``[Unreleased]`` body, from its heading to the next release."""
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.startswith(UNRELEASED_HEADING)
    )
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith(NEXT_RELEASE_PREFIX):
            end = index
            break
    return "\n".join(lines[start:end])


def test_unreleased_section_is_not_empty():
    section = _unreleased_section()
    assert section.startswith(UNRELEASED_HEADING)
    # A broken extractor would also make every "required string" check fail;
    # assert the section really carries release-note content first.
    assert "### Added" in section
    assert section.count("\n- ") > 10


def test_unreleased_covers_the_standalone_server_work():
    section = _unreleased_section()
    missing = [needle for needle in REQUIRED_STRINGS if needle not in section]
    assert not missing, (
        "CHANGELOG.md's [Unreleased] section does not mention: "
        + ", ".join(missing)
    )


def test_realm_permissions_entry_appears_exactly_once():
    """The Keycloak realm 0644 fix is one Fixed bullet; do not duplicate it."""
    section = _unreleased_section()
    bullets = [
        line for line in section.splitlines() if line.lstrip().startswith("- ") and "0644" in line
    ]
    assert len(bullets) == 1, (
        "expected exactly one [Unreleased] bullet mentioning 0644, found "
        f"{len(bullets)}: {bullets}"
    )


def test_no_release_heading_until_the_release_commit():
    assert "## [0.9.0]" not in CHANGELOG.read_text(encoding="utf-8")


def test_unreleased_heading_order_is_unchanged():
    section = _unreleased_section()
    changed = section.index("### Changed")
    added = section.index("### Added")
    fixed = section.index("### Fixed")
    assert changed < added < fixed, (
        "expected ### Changed before ### Added before ### Fixed in [Unreleased], "
        f"got Changed={changed}, Added={added}, Fixed={fixed}"
    )
