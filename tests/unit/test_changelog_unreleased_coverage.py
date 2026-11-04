"""Static guards on the CHANGELOG's [0.9.0] release notes.

The standalone-server work merged since v0.8.0 had to be described in
``CHANGELOG.md`` before the release commit cut the ``## [0.9.0] - <date>``
heading, and these checks keep that coverage in place once it is cut. They
read the file as text: no git, no network, no exact bullet counts and no
hashes of the cumulative file.
"""

import re
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"

RELEASE_HEADING = "## [0.9.0]"
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


def _release_section() -> str:
    """Return the ``[0.9.0]`` body, from its heading to the next release."""
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.startswith(RELEASE_HEADING)
    )
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith(NEXT_RELEASE_PREFIX):
            end = index
            break
    return "\n".join(lines[start:end])


def test_release_section_is_not_empty():
    section = _release_section()
    assert section.startswith(RELEASE_HEADING)
    # A broken extractor would also make every "required string" check fail;
    # assert the section really carries release-note content first.
    assert "### Added" in section
    assert section.count("\n- ") > 10


def test_release_covers_the_standalone_server_work():
    section = _release_section()
    missing = [needle for needle in REQUIRED_STRINGS if needle not in section]
    assert not missing, (
        "CHANGELOG.md's [0.9.0] section does not mention: "
        + ", ".join(missing)
    )


def test_realm_permissions_entry_appears_exactly_once():
    """The Keycloak realm 0644 fix is one Fixed bullet; do not duplicate it."""
    section = _release_section()
    bullets = [
        line for line in section.splitlines() if line.lstrip().startswith("- ") and "0644" in line
    ]
    assert len(bullets) == 1, (
        "expected exactly one [0.9.0] bullet mentioning 0644, found "
        f"{len(bullets)}: {bullets}"
    )


def test_release_heading_carries_a_date():
    heading = _release_section().splitlines()[0]
    assert re.fullmatch(r"## \[0\.9\.0\] - \d{4}-\d{2}-\d{2}", heading), heading


def test_release_heading_order_is_unchanged():
    section = _release_section()
    changed = section.index("### Changed")
    added = section.index("### Added")
    fixed = section.index("### Fixed")
    assert changed < added < fixed, (
        "expected ### Changed before ### Added before ### Fixed in [0.9.0], "
        f"got Changed={changed}, Added={added}, Fixed={fixed}"
    )
