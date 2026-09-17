"""Contract tests for the repo-root ``server.json`` MCP Registry manifest.

This repo is not distributed via pip/npm (packaging is deliberately deferred -
see docs/specs/PIP_INSTALLABILITY_DESIGN.md), so ``server.json`` uses the
official MCP Registry's documented "custom install path" shape: no ``packages``
array, just metadata pointing at the git repo and its README for install
instructions.

The ``version`` field must always mirror this repo's most recent CHANGELOG.md
release rather than being hand-maintained as a second source of truth, so the
version test below reads *both* sides fresh and never hardcodes a literal
version number - it keeps passing across future releases without modification.

No ``jsonschema`` dependency is used: the handful of fields the official schema
actually requires are hand-checked here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_JSON_PATH = REPO_ROOT / "server.json"
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"

# The only top-level fields this story's manifest is allowed to carry. The
# official schema requires name/description/version; the rest are the
# custom-install-path metadata this repo intentionally ships.
EXPECTED_TOP_LEVEL_FIELDS = {
    "$schema",
    "name",
    "description",
    "repository",
    "version",
    "websiteUrl",
}

# GitHub-auth-based registry names must start with ``io.github.<username>/``.
EXPECTED_NAME = "io.github.motock/fagan"
EXPECTED_REPOSITORY_URL = "https://github.com/motock/fagan"
EXPECTED_REPOSITORY_SOURCE = "github"
EXPECTED_SCHEMA_URL = (
    "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
)
EXPECTED_WEBSITE_URL = "https://github.com/motock/fagan#quickstart"
EXPECTED_DESCRIPTION = (
    "Autonomous coding pipeline: frontier models plan and review, a local model "
    "implements, gated by TDD and an independent merge-time test rerun."
)

# First ``## [X.Y.Z]`` heading in CHANGELOG.md - the most recent release.
_CHANGELOG_VERSION_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _load_server_json() -> dict:
    """Parse server.json, failing loudly (and for the right reason) if absent."""
    assert SERVER_JSON_PATH.is_file(), f"missing manifest: {SERVER_JSON_PATH}"
    with SERVER_JSON_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _changelog_version() -> str:
    """Extract the top-most ``## [X.Y.Z]`` version from CHANGELOG.md."""
    assert CHANGELOG_PATH.is_file(), f"missing changelog: {CHANGELOG_PATH}"
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    match = _CHANGELOG_VERSION_RE.search(text)
    assert match is not None, (
        "CHANGELOG.md has no top-level '## [X.Y.Z]' release heading to mirror"
    )
    return match.group(1)


# --- Case 1: valid JSON, required schema fields present ----------------------


def test_server_json_parses_as_json():
    """server.json must be valid JSON (a malformed file raises here)."""
    data = _load_server_json()
    assert isinstance(data, dict), "server.json must contain a JSON object"


@pytest.mark.parametrize("field", ["name", "description", "version"])
def test_required_schema_fields_are_non_empty_strings(field):
    """name/description/version are the only fields the official schema requires."""
    data = _load_server_json()
    assert field in data, f"server.json is missing required field {field!r}"
    value = data[field]
    assert isinstance(value, str), f"{field!r} must be a string, got {type(value)!r}"
    assert value.strip(), f"{field!r} must be a non-empty string"


def test_version_is_semver():
    """The mirrored version must look like X.Y.Z."""
    version = _load_server_json()["version"]
    assert _SEMVER_RE.match(version), f"version {version!r} is not X.Y.Z semver"


# --- Case 2: registry name format -------------------------------------------


def test_name_is_exact_github_auth_name():
    assert _load_server_json()["name"] == EXPECTED_NAME


def test_name_follows_io_github_username_format():
    """Registry names for GitHub-auth publishers must be io.github.<user>/<name>."""
    name = _load_server_json()["name"]
    assert re.fullmatch(r"io\.github\.[^/\s]+/[^/\s]+", name), (
        f"name {name!r} does not match io.github.<username>/<server>"
    )


# --- Case 3: repository field correctness -----------------------------------


def test_repository_field_is_correct():
    repository = _load_server_json()["repository"]
    assert isinstance(repository, dict), "'repository' must be an object"
    assert repository["url"] == EXPECTED_REPOSITORY_URL
    assert repository["source"] == EXPECTED_REPOSITORY_SOURCE


def test_repository_has_only_url_and_source():
    repository = _load_server_json()["repository"]
    assert set(repository) == {"url", "source"}


# --- Case 4: version stays in sync with CHANGELOG.md ------------------------


def test_version_matches_latest_changelog_release():
    """server.json['version'] must equal CHANGELOG.md's top-most release.

    Both sides are read fresh, so this keeps passing across future releases
    without modification - neither value is compared to a hardcoded literal.
    """
    changelog_version = _changelog_version()
    assert _load_server_json()["version"] == changelog_version


# --- Case 5: custom-install-path shape (no packages/remotes) ----------------


def test_no_packages_or_remotes_field():
    """This repo intentionally uses the custom-install-path shape."""
    data = _load_server_json()
    assert "packages" not in data
    assert "remotes" not in data


def test_no_meta_field():
    """``_meta`` does not apply to this manifest either."""
    assert "_meta" not in _load_server_json()


def test_top_level_fields_are_exactly_the_expected_set():
    """No fields beyond the documented six may appear."""
    assert set(_load_server_json()) == EXPECTED_TOP_LEVEL_FIELDS


# --- Verbatim metadata values -----------------------------------------------


def test_schema_url_is_exact():
    assert _load_server_json()["$schema"] == EXPECTED_SCHEMA_URL


def test_website_url_is_exact():
    assert _load_server_json()["websiteUrl"] == EXPECTED_WEBSITE_URL


def test_description_is_exact():
    assert _load_server_json()["description"] == EXPECTED_DESCRIPTION


# --- Case 6: the declared schema's length bounds ----------------------------
#
# The schema this manifest declares
# (https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json)
# constrains definitions.ServerDetail.properties.description to
# {"type": "string", "minLength": 1, "maxLength": 100}. A manifest whose
# description exceeds maxLength is rejected by mcp-publisher / the registry,
# so the *bound* is what this module must pin - not a hand-copied literal.
#
# Regression: server.json shipped a 141-character description while the
# declared schema caps it at 100, so registry validation rejected the file.
# The suite could not catch it because test_description_is_exact restated the
# 141-char string verbatim, locking the invalid value in instead of asserting
# the constraint.

SCHEMA_DESCRIPTION_MIN_LENGTH = 1
SCHEMA_DESCRIPTION_MAX_LENGTH = 100


def test_description_satisfies_schema_length_bounds():
    """description must satisfy the declared schema's minLength/maxLength.

    The declared MCP Registry schema caps ``description`` at maxLength 100
    (and floors it at minLength 1). Anything longer makes the manifest invalid
    against the very schema it declares, so mcp-publisher/registry validation
    rejects it.
    """
    description = _load_server_json()["description"]
    assert len(description) <= SCHEMA_DESCRIPTION_MAX_LENGTH, (
        f"description is {len(description)} chars but the declared MCP Registry "
        f"schema caps it at maxLength {SCHEMA_DESCRIPTION_MAX_LENGTH}; "
        "mcp-publisher/registry validation rejects the manifest. Shorten it by "
        f"at least {len(description) - SCHEMA_DESCRIPTION_MAX_LENGTH} characters."
    )
    assert len(description) >= SCHEMA_DESCRIPTION_MIN_LENGTH, (
        f"description must be at least {SCHEMA_DESCRIPTION_MIN_LENGTH} character(s), "
        f"got {len(description)}"
    )
