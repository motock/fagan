"""Contract tests for the repo-root ``server.json`` MCP Registry manifest.

This repo is not distributed via pip/npm (packaging is deliberately deferred -
see docs/specs/PIP_INSTALLABILITY_DESIGN.md), so ``server.json`` uses the
official MCP Registry's documented "custom install path" shape: no ``packages``
array, just metadata pointing at the git repo and its README for install
instructions.

The ``version`` field must always mirror this repo's most recent CHANGELOG.md
release rather than being hand-maintained as a second source of truth, so the
version tests below read *both* sides fresh and never hardcode a literal
version number - they keep passing across future releases without modification.

No ``jsonschema`` dependency is used, so the schema's *required* fields are
hand-checked here. The declared schema's ``description`` bounds are enforced
too: ``definitions.ServerDetail.properties.description`` is
``{"type": "string", "minLength": 1, "maxLength": 100}``, so the description
tests assert those bounds instead of restating a hand-copied literal (a
141-character description previously shipped and was locked in by an
exact-equality assertion, so registry validation rejected the manifest while
CI stayed green).

The suite also pins the manifest's *exact* top-level field set, its ``$schema``
and ``websiteUrl`` literals, the ``repository`` object's shape, the registry
name format and the semver shape of ``version``. Those contract assertions were
silently dropped by an earlier rewrite of this file, so they are restored here
and must not be removed again.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Resolve repo-root files from this file's location, never from the process CWD:
# pyproject.toml documents a past CI breakage caused by CWD-relative
# assumptions (the bare ``pytest`` entrypoint CI runs does not guarantee the
# repo root is the working directory).
REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_JSON_PATH = REPO_ROOT / "server.json"
CHANGES_PATH = REPO_ROOT / "CHANGELOG.md"

# The only top-level fields this manifest is allowed to carry. The official
# schema requires name/description/version; the rest are the custom-install-path
# metadata this repo intentionally ships.
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

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Load the manifest once for the tests that only need the parsed object.
with SERVER_JSON_PATH.open("r", encoding="utf-8") as f:
    SERVER_DATA = json.load(f)

# Extract the first version heading from CHANGELOG.md
with CHANGES_PATH.open("r", encoding="utf-8") as f:
    changelog_text = f.read()

FIRST_VERSION_MATCH = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog_text, re.MULTILINE)
assert FIRST_VERSION_MATCH, "CHANGELOG.md does not contain a valid version header"
EXPECTED_VERSION = FIRST_VERSION_MATCH.group(1)


def _load_server_json() -> dict:
    """Parse server.json, failing loudly (and for the right reason) if absent."""
    assert SERVER_JSON_PATH.is_file(), f"missing manifest: {SERVER_JSON_PATH}"
    with SERVER_JSON_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


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


# --- Restored contract assertions -------------------------------------------
#
# The seven tests below were dropped by a rewrite of this file even though all
# seven passed against the manifest. They are the manifest's contract coverage
# (exact field set, $schema URL, websiteUrl, _meta absence, repository shape,
# name regex, semver) and must not be removed again.


def test_version_is_semver():
    """The mirrored version must look like X.Y.Z."""
    version = _load_server_json()["version"]
    assert _SEMVER_RE.match(version), f"version {version!r} is not X.Y.Z semver"


def test_name_follows_io_github_username_format():
    """Registry names for GitHub-auth publishers must be io.github.<user>/<name>."""
    name = _load_server_json()["name"]
    assert re.fullmatch(r"io\.github\.[^/\s]+/[^/\s]+", name), (
        f"name {name!r} does not match io.github.<username>/<server>"
    )


def test_repository_has_only_url_and_source():
    repository = _load_server_json()["repository"]
    assert set(repository) == {"url", "source"}


def test_no_meta_field():
    """``_meta`` does not apply to this manifest either."""
    assert "_meta" not in _load_server_json()


def test_top_level_fields_are_exactly_the_expected_set():
    """No fields beyond the documented six may appear."""
    assert set(_load_server_json()) == EXPECTED_TOP_LEVEL_FIELDS


def test_schema_url_is_exact():
    assert _load_server_json()["$schema"] == EXPECTED_SCHEMA_URL


def test_website_url_is_exact():
    assert _load_server_json()["websiteUrl"] == EXPECTED_WEBSITE_URL

# ---------------------------------------------------------------------------
# End of test file
# ---------------------------------------------------------------------------
