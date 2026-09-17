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

No ``jsonschema`` dependency is used, so the schema's *required* fields are
hand-checked here. The declared schema's ``description`` bounds are enforced
too: ``definitions.ServerDetail.properties.description`` is
``{"type": "string", "minLength": 1, "maxLength": 100}``, so the description
tests assert those bounds instead of restating a hand-copied literal (a
141-character description previously shipped and was locked in by an
exact-equality assertion, so registry validation rejected the manifest while
CI stayed green).
"""

from __future__ import annotations

import json
import re
import subprocess
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
# A documented, schema-compliant description (<=100 chars). The description
# tests assert the declared schema's *bounds* rather than pinning this literal,
# so any compliant wording passes; this value only appears in failure messages.
EXPECTED_DESCRIPTION = "MCP server exposing pipeline story tooling to agents."

# The 141-character description this manifest shipped while the declared schema
# caps ``description`` at maxLength 100. Kept only so the regression test can
# assert the invalid value is gone.
OLD_INVALID_DESCRIPTION = (
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
    """The description must be the documented, schema-compliant wording.

    ``EXPECTED_DESCRIPTION`` is a <=100-character value that satisfies the
    declared schema's ``maxLength``. The previous 141-character value was
    restated verbatim here, which locked in a manifest the registry rejects;
    the length assertion below is what actually enforces the contract.
    """
    description = _load_server_json()["description"]
    assert description == EXPECTED_DESCRIPTION, (
        f"description is {description!r} ({len(description)} chars); expected "
        f"{EXPECTED_DESCRIPTION!r} ({len(EXPECTED_DESCRIPTION)} chars). Set "
        "server.json's description to exactly that string - do not edit this "
        "test."
    )
    assert len(description) <= SCHEMA_DESCRIPTION_MAX_LENGTH, (
        f"description is {len(description)} chars but the declared MCP Registry "
        f"schema caps it at maxLength {SCHEMA_DESCRIPTION_MAX_LENGTH}"
    )


def test_description_is_not_the_invalid_over_length_value():
    """Regression: the 141-char description must not come back.

    ``definitions.ServerDetail.properties.description`` in the declared schema
    is ``{"type": "string", "minLength": 1, "maxLength": 100}``, so the old
    141-character wording made mcp-publisher/registry validation reject
    ``server.json``.
    """
    description = _load_server_json()["description"]
    assert description != OLD_INVALID_DESCRIPTION, (
        "server.json still carries the 141-character description that the "
        "declared MCP Registry schema (maxLength 100) rejects"
    )
    assert len(OLD_INVALID_DESCRIPTION) > SCHEMA_DESCRIPTION_MAX_LENGTH, (
        "test fixture drift: OLD_INVALID_DESCRIPTION is no longer over the "
        "schema's maxLength, so this regression test proves nothing"
    )


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


# --- Case 7: the scratchpad-only commits must be dropped from the branch -----
#
# The second blocking review finding has two halves. Untracking the file is only
# the first: this branch also carries two commits whose only change is
# ``.agent_scratchpad.md`` -
#
#     afdc682 docs: update scratchpad for server.json story
#     ba6ad02 docs: update scratchpad for server.json story
#
# They carry no source change, and merging them re-introduces the shared
# scratch-path churn that previously caused spurious merge-gate rebase conflicts
# between unrelated concurrent stories. The review requires dropping them (e.g.
# during the same rebase that untracks the file), while keeping the real
# ``server.json`` change.

SCRATCHPAD_PATH = ".agent_scratchpad.md"

# Tried in order because the pipeline operates across repos that differ on
# default-branch naming and on whether a remote exists at all.
_BASE_BRANCH_CANDIDATES = (
    "origin/HEAD",
    "origin/main",
    "origin/master",
    "main",
    "master",
)


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run git in the repo root, capturing output without raising."""
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _merge_base() -> str:
    """The commit this branch diverged from, or fail loudly if unresolvable."""
    for candidate in _BASE_BRANCH_CANDIDATES:
        result = _git("merge-base", "HEAD", candidate)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise AssertionError(
        "could not resolve a merge base against any of "
        f"{_BASE_BRANCH_CANDIDATES}; this test needs the branch's base commit"
    )


def test_no_branch_commit_touches_the_scratchpad():
    """No commit on this branch may add or modify .agent_scratchpad.md.

    The two ``docs: update scratchpad for server.json story`` commits carry no
    source change and must be dropped from the branch, not merely untracked.
    ``--diff-filter=AM`` deliberately allows the *deletion* commit that
    ``git rm --cached`` produces (the fix itself), while still failing on any
    commit that introduces or edits the file's contents.
    """
    base = _merge_base()
    result = _git(
        "log",
        "--format=%h %s",
        "--diff-filter=AM",
        f"{base}..HEAD",
        "--",
        SCRATCHPAD_PATH,
    )
    assert result.returncode == 0, f"git log failed: {result.stderr}"
    offenders = [line for line in result.stdout.splitlines() if line.strip()]
    assert offenders == [], (
        f"{SCRATCHPAD_PATH} is added or modified by {len(offenders)} commit(s) on "
        f"this branch ({base[:8]}..HEAD): {offenders}. These scratchpad-only "
        "commits carry no source change and re-introduce the spurious merge-gate "
        "rebase conflicts between concurrent stories. Drop them (e.g. during the "
        f"rebase that untracks {SCRATCHPAD_PATH})."
    )


def test_branch_still_carries_the_server_json_change():
    """Dropping the scratchpad commits must not drop the real source change."""
    base = _merge_base()
    result = _git("log", "--format=%h %s", f"{base}..HEAD", "--", "server.json")
    assert result.returncode == 0, f"git log failed: {result.stderr}"
    assert result.stdout.strip(), (
        f"no commit in {base[:8]}..HEAD changes server.json; the branch must "
        "still carry the manifest change after the scratchpad commits are dropped"
    )
