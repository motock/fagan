"""Tests for plan-approval transitions in the audit stream (AUD-03).

An approval or rejection authorizes dispatch of a whole plan, so it is a
governance act that must reach the append-only audit stream and not only the
manifest's ``approval`` block and the JSON-array decisions log:
``plan.approved`` / ``plan.changes_requested`` with ``outcome="allowed"`` carry
the content digest, the deciding principal, the primary ADR 0009 role and
``self_approved``. A digest mismatch refuses the decision and records that
refusal (``outcome="refused"``, ``detail["reason"]="digest_mismatch"``) so the
gap is visible in the governance trail too.

The ``plan`` and ``enabled`` fixtures are re-created here rather than imported
from ``tests/unit/test_plan_approval_actor.py``: pytest fixtures are not
importable across modules. ``pipeline.audit`` reads ``persistence.PLAN_DIR`` at
call time, so redirecting that module attribute in ``plan`` redirects this
stream alongside the manifest and the decisions log.
"""
import json

import pytest

from app.identity import Principal
from pipeline import audit, persistence, plan_approval, server
from pipeline.actor import bind_principal
from pipeline.plan_approval import canonical_digest
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
    ``pipeline.paths`` at load time) and owns the decisions log *and* the audit
    stream, so it must be redirected alongside ``pipeline.server``'s copy.
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


@pytest.fixture
def enabled(monkeypatch):
    """Approval gate on, policy enforcement off via its kill switch.

    These tests grade what a decision *records*, using principals the local
    policy denies on purpose, so enforcement would turn every case into a
    refusal. Keeping enforcement off also keeps this stream to the one record
    under test: the authorization layer records nothing at all while its gate
    is disabled.
    """
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")
    monkeypatch.setenv("PIPELINE_POLICY_ENFORCE", "0")


_ALICE = Principal(subject="alice", roles={"approver"})


def _records(plan_dir):
    """Return every record in ``p``'s audit stream, or ``[]`` when absent."""
    path = plan_dir / "p.audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _only_record(plan_dir):
    """Return the single record in ``p``'s stream, failing loudly on any other count."""
    records = _records(plan_dir)
    if len(records) != 1:
        raise AssertionError(f"expected exactly one audit record, got {len(records)}")
    return records[0]


def _first_record(plan_dir):
    """Return the first record in ``p``'s stream, or ``{}`` when it is empty."""
    records = _records(plan_dir)
    return records[0] if records else {}


def _tamper_authored_content(plan_dir):
    """Change an authored field, leaving the approval digest it was made against."""
    manifest = json.loads((plan_dir / "p.manifest.json").read_text())
    manifest["context"] = "edited after approval"
    (plan_dir / "p.manifest.json").write_text(json.dumps(manifest))


def test_approve_records_allowed(plan, enabled):
    with bind_principal(_ALICE):
        result = PipelineService().approve_plan("p")

    record = _only_record(plan)

    assert (
        record["action"],
        record["outcome"],
        record["detail"],
    ) == (
        "plan.approved",
        "allowed",
        {
            "digest": result["digest"],
            "decided_by": result["decided_by"],
            "role": result["role"],
            "self_approved": result["self_approved"],
        },
    )


def test_reject_records_changes_requested(plan, enabled):
    with bind_principal(_ALICE):
        PipelineService().reject_plan("p", "no")

    record = _only_record(plan)

    assert (record["action"], record["outcome"]) == ("plan.changes_requested", "allowed")


def test_two_decisions_chain(plan, enabled):
    with bind_principal(_ALICE):
        PipelineService().approve_plan("p")
        PipelineService().reject_plan("p", "no")

    assert ([record["seq"] for record in _records(plan)], audit.verify_audit_chain("p")) == (
        [0, 1],
        None,
    )


def test_digest_mismatch_records_refusal(plan, enabled):
    _tamper_authored_content(plan)

    with bind_principal(_ALICE):
        PipelineService().approve_plan("p")

    records = _records(plan)

    assert (
        _first_record(plan).get("outcome"),
        _first_record(plan).get("detail", {}).get("reason"),
        len(records),
    ) == ("refused", "digest_mismatch", 1)


def test_digest_mismatch_still_returns_refusal(plan, enabled):
    _tamper_authored_content(plan)

    with bind_principal(_ALICE):
        result = PipelineService().approve_plan("p")

    assert result == {"ok": False, "error": plan_approval._DIGEST_MISMATCH_ERROR}


def test_gate_disabled_creates_no_audit_file(plan, monkeypatch):
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)

    with bind_principal(_ALICE):
        PipelineService().approve_plan("p")

    assert not audit.audit_path("p").exists()
