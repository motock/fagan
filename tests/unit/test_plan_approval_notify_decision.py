"""Notices emitted when an approval decision is recorded (PAN-05).

A recorded decision is the moment the operator (or the requester) needs to
know the plan moved: ``approve_plan`` sends ``plan_approved`` and
``reject_plan`` sends ``plan_changes_requested``. Nothing is sent when the
decision was not recorded -- gate disabled, authorization refusal, locked
plan, missing plan or digest mismatch -- because a notice for an unrecorded
decision would announce a transition that never happened.

The notice is worded and delivered by ``pipeline.plan_approval_notice``;
these tests record what reaches ``pipeline.server._notify_user`` (the
delivery seam ``_ServerRef`` resolves at call time) rather than grading the
sentence, which ``tests/unit/test_plan_approval_notice.py`` already pins.
The change-request ``reason`` is reviewer text and must never leave the
manifest: it is delivered over channels the operator does not control.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import persistence, plan_approval, server
from pipeline.actor import ANONYMOUS, bind_principal
from pipeline.authorization import ENFORCE_ENV
from pipeline.plan_approval import canonical_digest
from pipeline.service import PipelineService

_DOC = Path(__file__).resolve().parents[2] / "docs" / "specs" / "PLAN_APPROVAL.md"


def _manifest(**overrides):
    manifest = {
        "repo_root": "/tmp/repo",
        "role_config": {},
        "context": "ctx",
        "stories": {
            "S1": {
                "summary": "do the thing",
                "agent_instructions": "instructions",
                "dependencies": [],
                "persona": "engineer",
                "model": None,
                "acceptance": ["it works"],
                "risk": "low",
                "backend": "claude",
                "tdd_split": True,
                "files": ["a.py"],
                "blocked_by": [],
                "status": "todo",
            }
        },
    }
    manifest.update(overrides)
    return manifest


@pytest.fixture
def plan(tmp_path, monkeypatch):
    """Write a manifest with a pending approval record and point PLAN_DIR at it.

    ``pipeline.persistence`` holds its own ``PLAN_DIR`` binding (imported from
    ``pipeline.paths`` at load time) and owns the decisions log, so it must be
    redirected alongside ``pipeline.server``'s copy.
    """
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    manifest = _manifest()
    manifest["approval"] = {
        "status": "pending_approval",
        "digest": canonical_digest(manifest),
        "digest_version": plan_approval.APPROVAL_DIGEST_VERSION,
        "warnings_seen": ["w1"],
    }
    (tmp_path / "p.manifest.json").write_text(json.dumps(manifest))
    return tmp_path


@pytest.fixture
def enabled(monkeypatch):
    """Approval gate on, policy enforcement off via its kill switch.

    These tests grade which notices a *recorded* decision emits, so the
    authorization layer must let the decision through; the refusal path has
    its own test that turns enforcement back on.
    """
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")
    monkeypatch.setenv(ENFORCE_ENV, "0")


@pytest.fixture
def notices(monkeypatch):
    """Record every delivery ``plan_approval_notice`` makes."""
    recorded = []

    def _stub(plan_name, message, **kwargs):
        recorded.append({"plan_name": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(server, "_notify_user", _stub)
    return recorded


def test_approve_emits_plan_approved(plan, enabled, notices):
    result = PipelineService().approve_plan("p")

    assert result["ok"] is True
    assert [notice["event"] for notice in notices] == ["plan_approved"]
    assert notices[0]["plan_name"] == "p"


def test_reject_emits_plan_changes_requested_without_the_reason(plan, enabled, notices):
    result = PipelineService().reject_plan("p", reason="SENTINEL")

    assert result["ok"] is True
    assert [notice["event"] for notice in notices] == ["plan_changes_requested"]
    assert "SENTINEL" not in json.dumps(notices[0])


def test_digest_mismatch_emits_nothing(plan, enabled, notices):
    manifest = json.loads((plan / "p.manifest.json").read_text())
    manifest["context"] = "edited after approval"
    (plan / "p.manifest.json").write_text(json.dumps(manifest))

    result = PipelineService().approve_plan("p")

    assert result["ok"] is False
    assert notices == []


def test_missing_plan_emits_nothing(plan, enabled, notices):
    (plan / "p.manifest.json").unlink()

    result = PipelineService().approve_plan("p")

    assert result["ok"] is False
    assert notices == []


def test_authorization_refusal_emits_nothing(plan, enabled, notices, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with bind_principal(ANONYMOUS):
        result = PipelineService().approve_plan("p")

    assert result["ok"] is False
    assert notices == []


def test_gate_disabled_emits_nothing(plan, notices, monkeypatch):
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)

    result = PipelineService().approve_plan("p")

    assert result["skipped"] == "approval_disabled"
    assert notices == []


def test_spec_documents_the_change_request_event():
    assert "plan_changes_requested" in _DOC.read_text()
