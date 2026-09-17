"""
Test suite for the repo-root ``server.json`` MCP Registry manifest.

This test file is intentionally self‑contained and does not depend on any
external schema validation library. It verifies that the manifest contains
only the fields required by the custom‑install‑path shape and that the
``version`` field stays in sync with the most recent release in
``CHANGELOG.md``.
"""

import json
import pathlib
import re

CHANGES_PATH = pathlib.Path("CHANGELOG.md")
SERVER_JSON_PATH = pathlib.Path("server.json")

# Load the manifest once for all tests
with SERVER_JSON_PATH.open("r", encoding="utf-8") as f:
    SERVER_DATA = json.load(f)

# Extract the first version heading from CHANGELOG.md
with CHANGES_PATH.open("r", encoding="utf-8") as f:
    changelog_text = f.read()

FIRST_VERSION_MATCH = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog_text, re.MULTILINE)
assert FIRST_VERSION_MATCH, "CHANGELOG.md does not contain a valid version header"
EXPECTED_VERSION = FIRST_VERSION_MATCH.group(1)

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_required_fields_present_and_non_empty():
    """The manifest must contain the required fields and they must be
    non‑empty strings.
    """
    for key in ("name", "description", "version"):
        assert key in SERVER_DATA, f"{key!r} missing from server.json"
        value = SERVER_DATA[key]
        assert isinstance(value, str), f"{key!r} must be a string"
        assert value, f"{key!r} must not be empty"


def test_name_format():
    """The ``name`` field must match the GitHub‑auth based registry
    convention.
    """
    assert SERVER_DATA["name"] == "io.github.motock/fagan"


def test_repository_fields():
    """The ``repository`` object must contain the expected URL and source.
    """
    repo = SERVER_DATA.get("repository", {})
    assert isinstance(repo, dict), "repository must be a dict"
    assert repo.get("url") == "https://github.com/motock/fagan"
    assert repo.get("source") == "github"


def test_version_matches_changelog():
    """The ``version`` field must match the most recent release in
    ``CHANGELOG.md``.
    """
    assert SERVER_DATA["version"] == EXPECTED_VERSION


def test_no_packages_or_remotes():
    """The manifest must not contain ``packages`` or ``remotes`` fields.
    """
    assert "packages" not in SERVER_DATA
    assert "remotes" not in SERVER_DATA

# ---------------------------------------------------------------------------
# End of test file
# ---------------------------------------------------------------------------
