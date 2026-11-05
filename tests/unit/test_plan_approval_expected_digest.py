"""Tests for the ``expected_digest`` stale-digest refusal (PAN-06).

An operator approves what they were shown. ``approve_plan`` / ``reject_plan``
therefore accept an optional keyword-only ``expected_digest``: when it is
provided and does not equal the manifest's current canonical digest, the
decision is refused with ``stale_digest`` and *nothing* is written beyond one
refused audit record. ``expected_digest=None`` (every MCP caller) behaves
exactly as before the keyword existed.

DRIFT FROM BRIEF, recorded deliberately: ``reject_plan`` forwards
``status="changes_requested"``, so the refused audit action for a rejection is
``plan.changes_requested`` — the brief's prose says ``plan.rejected``, but its
own code shape ``action=f"plan.{status}"`` yields the former, which is also what
the existing ``digest_mismatch`` refusal already records. Tests pin the real
value rather than inventing a second action name.
"""
import inspect
import json
from pathlib import Path

import pytest

import pipeline.server_tools_lifecycle as lifecycle
from pipeline import (
    audit,
    persistence,
    plan_approval,
    server,
    wake,
)
from pipeline.plan_approval import canonical_digest
from pipeline.service import PipelineService

_DOC = Path(__file__).resolve().parents[2] / "docs" / "specs" / "PLAN_APPROVAL.md"

#: The refusal payload for a stale ``expected_digest``, exactly as specified.
REFUSAL = {
    "ok": False,
    "error": "plan changed since it was displayed; reload before deciding",
    "stale_digest": True,
}

#: The gate-disabled payload, unchanged by this story.
DISABLED = {"ok": True, "skipped": "approval_disabled"}


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
    """Pending-approval manifest with ``PLAN_DIR`` redirected (mixin test shape)."""
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
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")


@pytest.fixture
def notices(monkeypatch):
    """Record every notification side effect, so "sends nothing" is assertable."""
    sent: list[tuple] = []

    def _notify(plan_name, manifest, event):
        sent.append(("notice", plan_name, event))

    def _request_wake(plan_name, reason):
        sent.append(("wake", plan_name, reason))

    monkeypatch.setattr(
        "pipeline.plan_approval_notice.notify_approval_event", _notify
    )
    monkeypatch.setattr(wake, "request_wake", _request_wake)
    return sent


def _read_manifest(tmp_path):
    return json.loads((tmp_path / "p.manifest.json").read_text())


def _decisions(tmp_path):
    path = tmp_path / "p.decisions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _digest(tmp_path):
    """The digest the implementation will compute for the on-disk manifest."""
    return canonical_digest(_read_manifest(tmp_path))


def _audit(tmp_path, name="p"):
    path = audit.audit_path(name)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _stale(tmp_path, name="p"):
    return [r for r in _audit(tmp_path, name) if r["detail"].get("reason") == "stale_digest"]


def _assert_untouched(tmp_path, manifest_bytes, decisions_bytes, notices):
    assert (tmp_path / "p.manifest.json").read_bytes() == manifest_bytes
    decisions_path = tmp_path / "p.decisions.json"
    after = decisions_path.read_bytes() if decisions_path.exists() else None
    assert after == decisions_bytes
    assert notices == []


# --- positives -------------------------------------------------------------


def test_approve_with_matching_expected_digest_approves(plan, enabled):
    digest = _digest(plan)

    result = PipelineService().approve_plan("p", expected_digest=digest)

    assert result["ok"] is True
    assert result["plan"] == "p"
    assert result["status"] == "approved"
    assert result["digest"] == digest
    assert result["decided_by"] == "local-operator"
    assert result["role"] == "admin"
    assert result["self_approved"] is True
    assert result["warnings_seen"] == ["w1"]
    assert _read_manifest(plan)["approval"]["status"] == "approved"
    assert len(_decisions(plan)) == 1
    assert _stale(plan) == []


def test_reject_with_matching_expected_digest_rejects(plan, enabled):
    digest = _digest(plan)

    result = PipelineService().reject_plan(
        "p", "needs tests", expected_digest=digest
    )

    assert result["ok"] is True
    assert result["plan"] == "p"
    assert result["status"] == "changes_requested"
    assert result["digest"] == digest
    assert result["reason"] == "needs tests"
    assert _read_manifest(plan)["approval"]["status"] == "changes_requested"
    assert len(_decisions(plan)) == 1
    assert _stale(plan) == []


# --- refusals --------------------------------------------------------------


def test_approve_with_wrong_digest_is_refused_and_writes_nothing(
    plan, enabled, notices
):
    digest = _digest(plan)
    wrong = "a" * 64
    assert digest != wrong, "fixture digest must differ from the wrong digest"
    before_manifest = (plan / "p.manifest.json").read_bytes()
    assert _decisions(plan) == []
    audit_before = _audit(plan)
    assert _stale(plan) == []

    result = PipelineService().approve_plan("p", expected_digest=wrong)

    assert result == REFUSAL
    _assert_untouched(plan, before_manifest, None, notices)
    assert _read_manifest(plan)["approval"]["status"] == "pending_approval"
    assert _decisions(plan) == []
    assert _audit(plan)[: len(audit_before)] == audit_before
    (record,) = _stale(plan)
    assert record["action"] == "plan.approved"
    assert record["outcome"] == "refused"
    assert record["target"] == {"plan": "p"}
    assert record["detail"] == {"reason": "stale_digest"}


def test_reject_with_wrong_digest_is_refused_and_writes_nothing(
    plan, enabled, notices
):
    digest = _digest(plan)
    wrong = "a" * 64
    assert digest != wrong, "fixture digest must differ from the wrong digest"
    before_manifest = (plan / "p.manifest.json").read_bytes()
    audit_before = _audit(plan)
    assert _stale(plan) == []

    result = PipelineService().reject_plan("p", "schema drift", expected_digest=wrong)

    assert result == REFUSAL
    _assert_untouched(plan, before_manifest, None, notices)
    assert _read_manifest(plan)["approval"]["status"] == "pending_approval"
    assert _decisions(plan) == []
    assert _audit(plan)[: len(audit_before)] == audit_before
    (record,) = _stale(plan)
    # ``reject_plan`` forwards status="changes_requested" (see module docstring).
    assert record["action"] == "plan.changes_requested"
    assert record["outcome"] == "refused"
    assert record["target"] == {"plan": "p"}
    assert record["detail"] == {"reason": "stale_digest"}


def test_empty_string_expected_digest_is_a_mismatch(plan, enabled, notices):
    before_manifest = (plan / "p.manifest.json").read_bytes()

    result = PipelineService().approve_plan("p", expected_digest="")

    assert result == REFUSAL
    _assert_untouched(plan, before_manifest, None, notices)
    assert _read_manifest(plan)["approval"]["status"] == "pending_approval"
    (record,) = _stale(plan)
    assert record["outcome"] == "refused"


# --- unchanged legacy behaviour -------------------------------------------


def test_omitted_expected_digest_behaves_as_before(plan, enabled, notices):
    result = PipelineService().approve_plan("p")

    assert result["ok"] is True
    assert result["status"] == "approved"
    assert result["digest"] == _digest(plan)
    assert len(_decisions(plan)) == 1
    assert _stale(plan) == []


def test_none_expected_digest_behaves_as_before(plan, enabled, notices):
    result = PipelineService().approve_plan("p", expected_digest=None)

    assert result["ok"] is True
    assert result["status"] == "approved"
    assert result["digest"] == _digest(plan)
    assert len(_decisions(plan)) == 1
    assert _stale(plan) == []


def test_disabled_gate_skips_before_the_digest_check(plan, monkeypatch, notices):
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)
    wrong = "a" * 64

    result = PipelineService().approve_plan("p", expected_digest=wrong)

    assert result == DISABLED
    assert "stale_digest" not in result
    assert _stale(plan) == []
    assert notices == []

    # A later, gated decision is unaffected by the skipped call.
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")
    follow_up = PipelineService().approve_plan("p", expected_digest=_digest(plan))
    assert follow_up["ok"] is True
    assert follow_up["status"] == "approved"


def test_mcp_tool_signature_is_unchanged():
    assert list(inspect.signature(lifecycle.approve_plan).parameters) == ["plan_name"]


def test_doc_documents_the_stale_digest_refusal():
    text = _DOC.read_text()

    assert "expected_digest" in text
    assert "stale_digest" in text


# --- follow-up trace -------------------------------------------------------


def test_refused_reject_then_correct_reject_decides_exactly_once(
    plan, enabled, notices
):
    digest = _digest(plan)
    wrong = "a" * 64
    assert digest != wrong, "fixture digest must differ from the wrong digest"
    before_manifest = (plan / "p.manifest.json").read_bytes()

    refused = PipelineService().reject_plan("p", "schema drift", expected_digest=wrong)

    assert refused == REFUSAL
    assert (plan / "p.manifest.json").read_bytes() == before_manifest
    assert _decisions(plan) == []
    assert notices == []
    (record,) = _stale(plan)
    assert record["action"] == "plan.changes_requested"

    decided = PipelineService().reject_plan(
        "p", "schema drift", expected_digest=digest
    )

    assert decided["ok"] is True
    assert decided["status"] == "changes_requested"
    assert _read_manifest(plan)["approval"]["status"] == "changes_requested"
    records = _decisions(plan)
    assert len(records) == 1, "a refused decision must not consume the decision"
    assert records[0]["reason"] == "schema drift"
    assert len(_stale(plan)) == 1
    assert [entry[0] for entry in notices] == ["notice"]
