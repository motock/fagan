"""Tests for resetting plan approval when authored content changes (ENT-LC-03).

Editing authored content through ``patch_story`` / ``patch_plan`` must not
leave a stale ``approved`` record behind: the approval is tied to a digest of
the authored content, so any change through those tools resets the record to
``pending_approval`` and clears the decision metadata. The gate stays opt-in:
with ``PIPELINE_PLAN_APPROVAL`` unset both patch paths are byte-identical to
before this story and never add an ``approval`` key.
"""
import json

import pytest

from pipeline import persistence, plan_approval, server
from pipeline.plan_approval import canonical_digest, reset_if_changed
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


def _approved(manifest):
    """Return ``manifest`` with an ``approved`` record matching its content.

    The record mirrors what ``PlanApprovalMixin._decide_plan`` actually writes
    (``plan``/``status``/``digest``/``decided_by``/``at``/``correlation_id``/
    ``role``/``self_approved``/``warnings_seen``), so the reset assertions below cannot
    pass vacuously against a field production never writes.
    """
    manifest["approval"] = {
        "plan": "p",
        "status": "approved",
        "digest": canonical_digest(manifest),
        "decided_by": "local-operator",
        "at": "2026-01-01T00:00:00+00:00",
        "correlation_id": "",
        "role": "local-operator",
        "self_approved": True,
        "warnings_seen": ["w1"],
    }
    return manifest


#: Decision metadata that must not survive a reset.
_DECISION_FIELDS = (
    "decided_by",
    "at",
    "correlation_id",
    "role",
    "self_approved",
    "reason",
)


@pytest.fixture
def plan(tmp_path, monkeypatch):
    """Write an approved manifest and point both PLAN_DIR bindings at it."""
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    (tmp_path / "p.manifest.json").write_text(json.dumps(_approved(_manifest())))
    return tmp_path


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)


def _read_manifest(tmp_path):
    return json.loads((tmp_path / "p.manifest.json").read_text())


# --- reset_if_changed -------------------------------------------------------


def test_reset_if_changed_resets_on_authored_change(enabled):
    manifest = _approved(_manifest())
    manifest["stories"]["S1"]["agent_instructions"] = "new instructions"

    assert reset_if_changed(manifest) is True

    approval = manifest["approval"]
    assert approval["status"] == "pending_approval"
    assert approval["digest"] == canonical_digest(manifest)
    for field in _DECISION_FIELDS:
        assert field not in approval
    # Audit-only metadata that is not a decision is preserved.
    assert approval["warnings_seen"] == ["w1"]


def test_reset_if_changed_is_a_noop_when_digest_matches(enabled):
    manifest = _approved(_manifest())
    before = json.loads(json.dumps(manifest))

    assert reset_if_changed(manifest) is False
    assert manifest == before


def test_reset_if_changed_is_a_noop_when_approval_disabled(disabled):
    manifest = _approved(_manifest())
    manifest["stories"]["S1"]["agent_instructions"] = "new instructions"
    before = json.loads(json.dumps(manifest))

    assert reset_if_changed(manifest) is False
    assert manifest == before


def test_reset_if_changed_records_pending_when_no_approval_exists(enabled):
    """Fail closed: enabled with no record yet must not imply approval."""
    manifest = _manifest()

    assert reset_if_changed(manifest) is True
    assert manifest["approval"]["status"] == "pending_approval"
    assert manifest["approval"]["digest"] == canonical_digest(manifest)


def test_reset_if_changed_clears_a_changes_requested_reason(enabled):
    """A ``changes_requested`` reason must not outlive the decision it explains."""
    manifest = _approved(_manifest())
    manifest["approval"].update(
        {"status": "changes_requested", "reason": "needs more detail"}
    )
    manifest["stories"]["S1"]["agent_instructions"] = "new instructions"

    assert reset_if_changed(manifest) is True

    approval = manifest["approval"]
    assert approval["status"] == "pending_approval"
    assert "reason" not in approval
    assert "at" not in approval


def test_approved_fixture_matches_the_record_decide_plan_writes(plan, enabled):
    """Guard against vacuous assertions: the fixture must mirror production.

    ``_approved()`` fabricates the record the reset assertions inspect. If it
    drifted from what ``PlanApprovalMixin._decide_plan`` actually writes, those
    assertions could pass while a real decision field survived a reset.
    """
    pending = _manifest()
    pending["approval"] = plan_approval.on_ingest(pending)
    (plan / "p.manifest.json").write_text(json.dumps(pending))

    decided = PipelineService().approve_plan("p")

    assert decided["ok"] is True
    assert set(decided) == set(_approved(_manifest())["approval"]) | {"ok"}
    assert decided["at"] == _read_manifest(plan)["approval"]["at"]

# --- patch_story ------------------------------------------------------------


def test_patch_story_resets_approval_when_instructions_change(plan, enabled):
    result = PipelineService().patch_story(
        "p", "S1", {"agent_instructions": "new instructions"}
    )

    assert result["ok"] is True
    manifest = _read_manifest(plan)
    assert manifest["stories"]["S1"]["agent_instructions"] == "new instructions"
    approval = manifest["approval"]
    assert approval["status"] == "pending_approval"
    for field in _DECISION_FIELDS:
        assert field not in approval


def test_patch_story_keeps_approval_when_value_is_unchanged(plan, enabled):
    result = PipelineService().patch_story(
        "p", "S1", {"agent_instructions": "instructions"}
    )

    assert result["ok"] is True
    assert _read_manifest(plan)["approval"]["status"] == "approved"


def test_patch_story_reset_digest_matches_patched_manifest(plan, enabled):
    PipelineService().patch_story("p", "S1", {"agent_instructions": "new"})

    manifest = _read_manifest(plan)
    assert manifest["approval"]["digest"] == canonical_digest(manifest)


def test_patch_story_adds_no_approval_key_when_disabled(plan, disabled):
    manifest = _manifest()
    (plan / "p.manifest.json").write_text(json.dumps(manifest))

    result = PipelineService().patch_story(
        "p", "S1", {"agent_instructions": "new instructions"}
    )

    assert result["ok"] is True
    patched = _read_manifest(plan)
    assert "approval" not in patched
    assert patched["stories"]["S1"]["agent_instructions"] == "new instructions"


def test_patch_story_leaves_existing_approval_untouched_when_disabled(plan, disabled):
    before = _read_manifest(plan)

    PipelineService().patch_story("p", "S1", {"agent_instructions": "new"})

    after = _read_manifest(plan)
    assert after["approval"] == before["approval"]


# --- patch_plan -------------------------------------------------------------


def test_patch_plan_resets_approval_when_role_config_changes(plan, enabled):
    result = PipelineService().patch_plan(
        "p", {"role_config": {"engineer": {"model": "opus"}}}
    )

    assert result["ok"] is True
    manifest = _read_manifest(plan)
    assert manifest["role_config"] == {"engineer": {"model": "opus"}}
    approval = manifest["approval"]
    assert approval["status"] == "pending_approval"
    for field in _DECISION_FIELDS:
        assert field not in approval
    assert approval["digest"] == canonical_digest(manifest)


def test_patch_plan_keeps_approval_when_value_is_unchanged(plan, enabled):
    result = PipelineService().patch_plan("p", {"role_config": {}})

    assert result["ok"] is True
    assert _read_manifest(plan)["approval"]["status"] == "approved"


def test_patch_plan_adds_no_approval_key_when_disabled(plan, disabled):
    (plan / "p.manifest.json").write_text(json.dumps(_manifest()))

    result = PipelineService().patch_plan(
        "p", {"role_config": {"engineer": {"model": "opus"}}}
    )

    assert result["ok"] is True
    patched = _read_manifest(plan)
    assert "approval" not in patched
    assert patched["role_config"] == {"engineer": {"model": "opus"}}
