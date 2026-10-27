"""Tests for the actor recorded on plan-approval decisions (ACT-02).

``PlanApprovalMixin._decide_plan`` must name the principal that made the
decision rather than a hardcoded placeholder: ``decided_by`` is the bound
principal's subject, ``role`` is the most privileged ADR 0009 role it holds,
and ``self_approved`` compares the decider against the manifest's
``ingested_by`` (falling back to the decider when no such stamp exists yet).

The ``plan`` and ``enabled`` fixtures are re-created here rather than imported
from ``tests/unit/test_plan_approval_mixin.py``: pytest fixtures are not
importable across modules.
"""
import json

import pytest

from app.identity import Principal
from pipeline import persistence, plan_approval, server
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


@pytest.fixture
def enabled(monkeypatch):
    """Approval gate on, policy enforcement off via its kill switch.

    These tests grade what a decision *records* about its actor, using
    principals the local policy denies on purpose, so enforcement would turn
    every case into a refusal; enforcement itself is graded in
    tests/unit/test_authorization.py.
    """
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")
    monkeypatch.setenv("PIPELINE_POLICY_ENFORCE", "0")


def _read_manifest(tmp_path):
    return json.loads((tmp_path / "p.manifest.json").read_text())


def _decisions(tmp_path):
    path = tmp_path / "p.decisions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _set_ingested_by(tmp_path, subject):
    """Stamp ``ingested_by`` on the manifest, keeping the approval digest valid."""
    manifest = _read_manifest(tmp_path)
    manifest["ingested_by"] = subject
    manifest["approval"]["digest"] = canonical_digest(manifest)
    (tmp_path / "p.manifest.json").write_text(json.dumps(manifest))


def test_decided_by_names_the_bound_principal(plan, enabled):
    principal = Principal(subject="alice", roles={"approver"})

    with bind_principal(principal):
        result = PipelineService().approve_plan("p")

    assert result["ok"] is True
    assert result["decided_by"] == "alice"
    assert _decisions(plan)[0]["decided_by"] == "alice"


def test_role_is_the_only_held_role(plan, enabled):
    principal = Principal(subject="alice", roles={"approver"})

    with bind_principal(principal):
        result = PipelineService().approve_plan("p")

    assert result["role"] == "approver"
    assert _decisions(plan)[0]["role"] == "approver"


def test_role_is_the_most_privileged_held_role(plan, enabled):
    # ``_ROLE_PRECEDENCE`` checks "owner" before "author"; set iteration or
    # alphabetical sorting would wrongly report "author".
    principal = Principal(subject="alice", roles={"author", "owner"})

    with bind_principal(principal):
        result = PipelineService().approve_plan("p")

    assert result["role"] == "owner"


def test_role_falls_back_to_operator_when_no_role_is_held(plan, enabled):
    principal = Principal(subject="alice", roles=set())

    with bind_principal(principal):
        result = PipelineService().approve_plan("p")

    assert result["role"] == "operator"


def test_self_approved_is_false_when_ingested_by_is_another_subject(plan, enabled):
    _set_ingested_by(plan, "bob")
    principal = Principal(subject="alice", roles={"approver"})

    with bind_principal(principal):
        result = PipelineService().approve_plan("p")

    assert result["self_approved"] is False
    assert _decisions(plan)[0]["self_approved"] is False


def test_self_approved_is_true_when_ingested_by_is_absent(plan, enabled):
    # Documented fallback: no manifest carries ``ingested_by`` yet, so the
    # comparison degrades to "the ingester is the decider".
    principal = Principal(subject="alice", roles={"approver"})

    with bind_principal(principal):
        result = PipelineService().approve_plan("p")

    assert result["self_approved"] is True


def test_self_approved_is_true_when_ingested_by_matches_the_decider(plan, enabled):
    _set_ingested_by(plan, "alice")
    principal = Principal(subject="alice", roles={"approver"})

    with bind_principal(principal):
        result = PipelineService().approve_plan("p")

    assert result["self_approved"] is True


def test_each_decision_records_its_own_actor_and_self_approval(plan, enabled):
    alice = Principal(subject="alice", roles={"approver"})
    bob = Principal(subject="bob", roles={"approver"})

    with bind_principal(alice):
        first = PipelineService().approve_plan("p")

    _set_ingested_by(plan, "alice")
    with bind_principal(bob):
        second = PipelineService().approve_plan("p")

    assert first["decided_by"] == "alice"
    assert first["self_approved"] is True
    assert second["decided_by"] == "bob"
    assert second["self_approved"] is False

    records = _decisions(plan)
    assert [r["decided_by"] for r in records] == ["alice", "bob"]
    assert [r["self_approved"] for r in records] == [True, False]


def test_disabled_gate_writes_no_decision_even_with_a_principal_bound(
    plan, monkeypatch
):
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)
    before = (plan / "p.manifest.json").read_bytes()
    principal = Principal(subject="alice", roles={"approver"})

    with bind_principal(principal):
        result = PipelineService().approve_plan("p")

    assert result == {"ok": True, "skipped": "approval_disabled"}
    assert (plan / "p.manifest.json").read_bytes() == before
    assert _decisions(plan) == []
