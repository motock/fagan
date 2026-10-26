"""Plan-approval surfacing in the dashboard plan summary (ENT-LC-05).

Contract under test:

* ``_plan_summary`` gains an ``"approval"`` key **only** when the manifest
  carries an approval record. The value is a six-field projection of that
  record: ``status``, ``digest``, ``decided_by``, ``at``, ``self_approved``
  and ``warnings_seen``.
* When no approval record exists the summary is byte-identical to the
  pre-ENT-LC-05 response: no ``approval`` key, not even ``None``.
* Every pre-existing summary key keeps its value and its name.
* ``docs/specs/PLAN_APPROVAL.md`` documents the gate, the lifecycle and the
  digest/reset rules.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import app.dashboard_helpers as d

#: A representative manifest with no approval record. ``manifest_path`` is
#: patched to raise ``OSError`` so ``updated_at`` is the deterministic ``0.0``
#: fallback rather than a filesystem mtime.
PLAIN_MANIFEST = {"id": "p1", "status": "draft"}

#: The exact pre-ENT-LC-05 summary for ``PLAIN_MANIFEST``.
BASELINE_SUMMARY = {
    "name": "p1",
    "paused": False,
    "story_count": 0,
    "status_counts": {},
    "aggregate": {
        "dispatch_attempts": 0,
        "rework_attempts": 0,
        "merge_attempts": 0,
        "escalated": 0,
        "failure_reasons": {},
        "by_backend": {},
    },
    "updated_at": 0.0,
    "archived": False,
    "repo_root": None,
}

APPROVAL_RECORD = {
    "status": "approved",
    "digest": "sha256:abc123",
    "decided_by": "alice",
    "at": "2026-10-26T12:00:00Z",
    "self_approved": True,
    "warnings_seen": ["w1"],
}

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "specs" / "PLAN_APPROVAL.md"


@pytest.fixture(autouse=True)
def _no_manifest_mtime(monkeypatch):
    """Make ``updated_at`` deterministic (the OSError fallback, 0.0)."""

    def _raise(_plan_name):
        raise OSError("manifest vanished")

    monkeypatch.setattr(d._store, "manifest_path", _raise)


def test_approval_key_present():
    """A manifest with an approval record surfaces its six fields verbatim."""
    manifest = {**PLAIN_MANIFEST, "approval": dict(APPROVAL_RECORD)}

    result = d._plan_summary("p1", manifest)

    assert result["approval"] == {
        "status": "approved",
        "digest": "sha256:abc123",
        "decided_by": "alice",
        "at": "2026-10-26T12:00:00Z",
        "self_approved": True,
        "warnings_seen": ["w1"],
    }


def test_no_approval_key_when_absent():
    """No approval record means no ``approval`` key and an unchanged summary."""
    result = d._plan_summary("p1", dict(PLAIN_MANIFEST))

    assert "approval" not in result
    assert result == BASELINE_SUMMARY


def test_existing_plan_summary_keys_unchanged():
    """Adding ``approval`` must not drop, rename or alter an existing key."""
    baseline = d._plan_summary("p1", dict(PLAIN_MANIFEST))
    with_approval = d._plan_summary(
        "p1", {**PLAIN_MANIFEST, "approval": dict(APPROVAL_RECORD)}
    )

    for key, value in baseline.items():
        assert with_approval[key] == value
    assert set(with_approval) == set(baseline) | {"approval"}


def test_plan_approval_docs():
    """The spec doc is the documentation surface for the approval lifecycle."""
    assert DOC_PATH.is_file(), f"missing spec doc: {DOC_PATH}"
    text = DOC_PATH.read_text(encoding="utf-8")

    for term in (
        "PIPELINE_PLAN_APPROVAL",
        "off",
        "on",
        "default off",
        "unknown",
        "raises",
        "draft",
        "pending_approval",
        "approved",
        "changes_requested",
        "digest",
        "reset",
        "approve_plan",
        "reject_plan",
        "self-approval",
        "local mode",
        "warnings_seen",
    ):
        assert term in text, f"spec doc must mention {term!r}"
