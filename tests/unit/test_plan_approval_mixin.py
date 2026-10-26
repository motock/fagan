"""Tests for the plan-approval transitions on ``PipelineService`` (ENT-LC-02a).

``PlanApprovalMixin`` adds ``approve_plan`` / ``reject_plan`` to the service
seam. The gate stays opt-in: with ``PIPELINE_PLAN_APPROVAL`` unset both methods
are no-ops that report ``skipped: approval_disabled`` and leave the manifest
byte-identical.
"""
import json

import pytest

from pipeline import persistence, plan_approval, server
from pipeline.plan_approval import PlanApprovalMixin, canonical_digest
from pipeline.service import PipelineService


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
        "warnings_seen": ["w1"],
    }
    (tmp_path / "p.manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def _read_manifest(tmp_path):
    return json.loads((tmp_path / "p.manifest.json").read_text())


def _decisions(tmp_path):
    path = tmp_path / "p.decisions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")


def test_approve_moves_pending_approval_to_approved(plan, enabled):
    result = PipelineService().approve_plan("p")

    assert result["ok"] is True
    assert result["status"] == "approved"
    assert _read_manifest(plan)["approval"]["status"] == "approved"


def test_reject_moves_pending_approval_to_changes_requested(plan, enabled):
    result = PipelineService().reject_plan("p", reason="needs tests")

    assert result["ok"] is True
    assert result["status"] == "changes_requested"
    assert _read_manifest(plan)["approval"]["status"] == "changes_requested"


def test_approve_refuses_when_digest_changed(plan, enabled):
    manifest = _read_manifest(plan)
    manifest["context"] = "edited after ingest"
    (plan / "p.manifest.json").write_text(json.dumps(manifest))

    result = PipelineService().approve_plan("p")

    assert result["ok"] is False
    assert "plan content changed; re-ingest before approving" in result["error"]
    assert _read_manifest(plan)["approval"]["status"] == "pending_approval"


def test_reject_refuses_when_digest_changed(plan, enabled):
    manifest = _read_manifest(plan)
    manifest["stories"]["S1"]["summary"] = "edited after ingest"
    (plan / "p.manifest.json").write_text(json.dumps(manifest))

    result = PipelineService().reject_plan("p", reason="nope")

    assert result["ok"] is False
    assert "plan content changed; re-ingest before approving" in result["error"]
    assert _read_manifest(plan)["approval"]["status"] == "pending_approval"


def test_approve_is_skipped_when_approval_disabled(plan, monkeypatch):
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)
    before = (plan / "p.manifest.json").read_bytes()

    result = PipelineService().approve_plan("p")

    assert result == {"ok": True, "skipped": "approval_disabled"}
    assert (plan / "p.manifest.json").read_bytes() == before
    assert _decisions(plan) == []


def test_reject_is_skipped_when_approval_disabled(plan, monkeypatch):
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)
    before = (plan / "p.manifest.json").read_bytes()

    result = PipelineService().reject_plan("p", reason="nope")

    assert result == {"ok": True, "skipped": "approval_disabled"}
    assert (plan / "p.manifest.json").read_bytes() == before
    assert _decisions(plan) == []


def test_approve_appends_exactly_one_decision_record(plan, enabled):
    PipelineService().approve_plan("p")

    records = _decisions(plan)
    assert len(records) == 1
    record = records[0]
    assert record["plan"] == "p"
    assert record["digest"] == canonical_digest(_read_manifest(plan))
    assert record["decided_by"] == "local-operator"
    assert record["role"] == "admin"
    assert record["self_approved"] is True
    assert record["warnings_seen"] == ["w1"]
    assert record["at"].endswith("+00:00")
    assert "reason" not in record


def test_reject_appends_exactly_one_decision_record_with_reason(plan, enabled):
    PipelineService().reject_plan("p", reason="needs tests")

    records = _decisions(plan)
    assert len(records) == 1
    assert records[0]["reason"] == "needs tests"
    assert records[0]["decided_by"] == "local-operator"


def test_decision_record_is_returned_and_persisted_on_manifest(plan, enabled):
    result = PipelineService().approve_plan("p")

    approval = _read_manifest(plan)["approval"]
    assert approval["status"] == "approved"
    assert approval["decided_by"] == "local-operator"
    assert approval["role"] == "admin"
    assert approval["self_approved"] is True
    assert approval["warnings_seen"] == ["w1"]
    assert approval["at"] == result["at"]


def test_approve_rejects_invalid_plan_name(plan, enabled):
    with pytest.raises(ValueError):
        PipelineService().approve_plan("../escape")


def test_invalid_plan_name_is_rejected_even_when_disabled(plan, monkeypatch):
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)

    with pytest.raises(ValueError):
        PipelineService().approve_plan("../escape")


def test_digest_mismatch_appends_no_decision_record(plan, enabled):
    manifest = _read_manifest(plan)
    manifest["context"] = "edited after ingest"
    (plan / "p.manifest.json").write_text(json.dumps(manifest))

    PipelineService().approve_plan("p")

    assert _decisions(plan) == []


def test_reject_without_reason_omits_the_reason_field(plan, enabled):
    PipelineService().reject_plan("p")

    assert "reason" not in _decisions(plan)[0]


def test_approve_reports_missing_plan(plan, enabled):
    result = PipelineService().approve_plan("nope")

    assert result["ok"] is False
    assert "No such plan" in result["error"]


def test_pipeline_service_inherits_the_mixin():
    assert issubclass(PipelineService, PlanApprovalMixin)


def test_service_instances_hold_no_state():
    assert vars(PipelineService()) == {}
