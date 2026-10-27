"""Authorization on plan approve/reject (AUTH-05, ADR 0007 / ADR 0009).

Approving or rejecting a plan is the human gate ADR 0007 exists for, so an
unauthenticated caller must not be able to move a plan into ``approved``.
``PlanApprovalMixin._decide_plan`` is the single place both transitions pass
through, and it asks ``pipeline.authorization.authorize`` for a decision
*before* it takes the plan lock or reads the manifest.

The ``plan`` and ``enabled`` fixtures are re-created here rather than imported
from ``tests/unit/test_plan_approval_mixin.py``: pytest fixtures are not
importable across modules. Unlike ``tests/unit/test_plan_approval_actor.py``,
``enabled`` deliberately does *not* touch ``PIPELINE_POLICY_ENFORCE``:
enforcement is exactly what these tests grade.
"""

from __future__ import annotations

import json

import pytest

from app.identity import LOCAL_OPERATOR
from pipeline import persistence, plan_approval, server
from pipeline.actor import ANONYMOUS, bind_principal
from pipeline.authorization import ENFORCE_ENV
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
    """Approval gate on, policy enforcement at its default (on)."""
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")
    monkeypatch.delenv(ENFORCE_ENV, raising=False)


def _read_manifest(tmp_path):
    return json.loads((tmp_path / "p.manifest.json").read_text())


def _decisions(tmp_path):
    path = tmp_path / "p.decisions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _notifications(tmp_path):
    path = tmp_path / "p.notifications.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_anonymous_approve_is_refused_and_writes_nothing(plan, enabled):
    with bind_principal(ANONYMOUS):
        result = PipelineService().approve_plan("p")

    assert result["ok"] is False
    assert "plan:approve" in result["error"]
    assert _read_manifest(plan)["approval"]["status"] == "pending_approval"
    assert _decisions(plan) == []


def test_anonymous_reject_is_refused_and_writes_nothing(plan, enabled):
    with bind_principal(ANONYMOUS):
        result = PipelineService().reject_plan("p", reason="nope")

    assert result["ok"] is False
    assert "plan:reject" in result["error"]
    assert _read_manifest(plan)["approval"]["status"] == "pending_approval"
    assert _decisions(plan) == []


def test_local_operator_approve_still_records_the_decision(plan, enabled):
    with bind_principal(LOCAL_OPERATOR):
        result = PipelineService().approve_plan("p")

    assert result["ok"] is True
    assert result["status"] == "approved"
    assert _read_manifest(plan)["approval"]["status"] == "approved"
    records = _decisions(plan)
    assert len(records) == 1
    assert records[0]["status"] == "approved"
    assert records[0]["decided_by"] == LOCAL_OPERATOR.subject


def test_disabled_gate_short_circuits_before_authorization(plan, monkeypatch):
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "off")
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with bind_principal(ANONYMOUS):
        result = PipelineService().approve_plan("p")

    assert result == {"ok": True, "skipped": "approval_disabled"}
    assert _read_manifest(plan)["approval"]["status"] == "pending_approval"
    assert _decisions(plan) == []
    assert [n for n in _notifications(plan) if n.get("event") == "authorization_denied"] == []


def test_kill_switch_allows_anonymous_approve(plan, enabled, monkeypatch):
    monkeypatch.setenv(ENFORCE_ENV, "0")

    with bind_principal(ANONYMOUS):
        result = PipelineService().approve_plan("p")

    assert result["ok"] is True
    assert result["status"] == "approved"
    assert _read_manifest(plan)["approval"]["status"] == "approved"
    assert len(_decisions(plan)) == 1
