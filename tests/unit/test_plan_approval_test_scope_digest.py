"""Tests for ``test_scope`` in the plan-approval digest (test-scope-core).

``test_scope`` loosens what runs locally, so changing it must invalidate an
approval. It is hashed only when the key is present: a manifest without it must
keep the digest it had before the setting existed, or every approved plan would
reset on its next edit.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from pipeline import plan_approval
from pipeline.plan_approval import canonical_digest, on_ingest

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "specs" / "PLAN_APPROVAL.md"


def _manifest(**overrides):
    manifest = {
        "repo_root": "/tmp/repo",
        "role_config": {},
        "context": "ctx",
        "team": "core",
        "stories": {
            "S1": {
                "summary": "first",
                "agent_instructions": "do the first thing",
                "dependencies": [],
                "persona": "software-engineer",
                "model": None,
                "acceptance": [],
                "risk": "low",
                "backend": "claude",
                "tdd_split": True,
                "files": ["a.py"],
                "blocked_by": [],
                "description": "first story",
                "status": "todo",
            },
            "S2": {
                "summary": "second",
                "agent_instructions": "do the second thing",
                "dependencies": ["first"],
                "persona": "software-engineer",
                "model": None,
                "acceptance": [],
                "risk": "low",
                "backend": "claude",
                "tdd_split": True,
                "files": ["b.py"],
                "blocked_by": [],
                "description": "second story",
                "status": "todo",
            },
        },
    }
    manifest.update(overrides)
    return manifest


def _legacy_digest(manifest):
    """Re-derive the digest of a manifest without ``test_scope`` from scratch.

    Mirrors the payload ``canonical_digest`` hashes before ``test_scope`` was
    added, so the expected value is computed here rather than pinned as a
    sha256 literal.
    """
    payload = {field: manifest.get(field) for field in plan_approval._PLAN_AUTHORED_FIELDS}
    payload["stories"] = {
        key: {field: story.get(field) for field in plan_approval._STORY_AUTHORED_FIELDS}
        for key, story in manifest["stories"].items()
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _with_story_scope(manifest, scope):
    manifest["stories"]["S1"]["test_scope"] = scope
    return manifest


def test_should_keep_legacy_digest_for_manifest_without_test_scope():
    manifest = _manifest()

    assert canonical_digest(manifest) == _legacy_digest(manifest)


@pytest.mark.parametrize("scope", ["scoped", "full"])
def test_should_change_digest_when_plan_test_scope_added(scope):
    without = _manifest()
    with_scope = _manifest(test_scope=scope)

    assert canonical_digest(with_scope) != canonical_digest(without)


def test_should_change_digest_when_plan_test_scope_changed_from_scoped_to_full():
    scoped = _manifest(test_scope="scoped")
    full = _manifest(test_scope="full")

    assert canonical_digest(scoped) != canonical_digest(full)


def test_should_change_digest_when_story_test_scope_added():
    without = _manifest()
    with_scope = _with_story_scope(_manifest(), "scoped")

    assert canonical_digest(with_scope) != canonical_digest(without)


def test_should_hash_explicit_null_test_scope_as_present_key():
    without = _manifest()
    with_null = _manifest(test_scope=None)

    assert canonical_digest(with_null) != canonical_digest(without)


def test_should_reset_approval_on_ingest_when_test_scope_added():
    legacy = _manifest()
    approved = _manifest(
        approval={
            "status": "approved",
            "digest": canonical_digest(legacy),
            "digest_version": plan_approval.APPROVAL_DIGEST_VERSION,
            "decided_by": "reviewer",
        }
    )
    approved["test_scope"] = "scoped"

    record = on_ingest(approved)

    assert record["status"] == "pending_approval"


def test_should_keep_approval_when_nothing_changed():
    manifest = _manifest(test_scope="scoped")
    manifest["approval"] = {
        "status": "approved",
        "digest": canonical_digest(manifest),
        "digest_version": plan_approval.APPROVAL_DIGEST_VERSION,
    }

    record = on_ingest(manifest)

    assert record["status"] == "approved"


def test_should_not_mutate_manifest():
    manifest = _with_story_scope(_manifest(test_scope="scoped"), "full")
    before = copy.deepcopy(manifest)

    canonical_digest(manifest)

    assert manifest == before


def test_should_document_test_scope_in_digest_spec():
    text = DOC_PATH.read_text(encoding="utf-8")

    assert "`test_scope` at plan and story level only when the key is\n  present" in text
