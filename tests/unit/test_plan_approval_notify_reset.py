"""Reset-driven ``plan_pending_approval`` notices (PAN-04).

``pipeline/plan_approval_notice.py`` words and delivers the plan-approval
notices. Two more emit sites exist besides ingest: when a *saved* patch of an
already-approved plan resets the approval back to ``pending_approval``, the
operator who approved the old content must be told the approval no longer
covers what is on disk.

The contract pinned here:

* ``patch_story`` changing authored content of an approved plan emits exactly
  one ``plan_pending_approval`` notice, and the plan is left pending approval;
* a change that leaves the authored digest untouched (a runtime-only field
  such as ``pr_url``) emits nothing -- no spurious "please review" for a
  patch the approval still covers;
* ``patch_plan`` changing ``role_config`` emits exactly one notice, and an
  identical-value patch emits nothing;
* with ``PIPELINE_PLAN_APPROVAL`` off there is no approval record to reset and
  therefore no notice.

Counts are asserted exactly (``== 1`` / ``== []``): the failure mode this
guards against is a notice per patch, or a notice for a no-op patch.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import persistence
from pipeline import plan_approval as pa
from pipeline import server as p
from pipeline.plan_approval import canonical_digest
from pipeline.service import PipelineService

PENDING = "plan_pending_approval"

_DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "specs" / "PLAN_APPROVAL.md"


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
                "pr_url": None,
            }
        },
    }
    manifest.update(overrides)
    return manifest


def _approved(manifest):
    """Return ``manifest`` carrying an ``approved`` record for its content."""
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


@pytest.fixture
def plan(tmp_path, monkeypatch):
    """Write an approved manifest and point both PLAN_DIR bindings at it."""
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    (tmp_path / "p.manifest.json").write_text(json.dumps(_approved(_manifest())))
    return tmp_path


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.delenv(pa.APPROVAL_ENV, raising=False)


@pytest.fixture
def records(monkeypatch):
    """Record every ``_notify_user`` call; the real one is never invoked."""
    captured: list[dict] = []

    def recorder(
        plan_name,
        message,
        *,
        story_key=None,
        severity="info",
        event=None,
        dedup_key=None,
        correlation_id=None,
        attempt=None,
        role=None,
        provider=None,
        model=None,
    ):
        captured.append(
            {
                "plan_name": plan_name,
                "message": message,
                "event": event,
                "dedup_key": dedup_key,
                "story_key": story_key,
            }
        )

    monkeypatch.setattr(p, "_notify_user", recorder)
    return captured


def _pending(records):
    return [record for record in records if record["event"] == PENDING]


def _read_manifest(plan_dir):
    return json.loads((plan_dir / "p.manifest.json").read_text())


def test_patch_story_authored_change_notifies(plan, enabled, records):
    result = PipelineService().patch_story(
        "p", "S1", {"agent_instructions": "new instructions"}
    )

    assert result["ok"] is True
    assert len(records) == 1
    (record,) = records
    assert record["event"] == PENDING
    assert record["plan_name"] == "p"

    stored = _read_manifest(plan)
    assert stored["approval"]["status"] == "pending_approval"
    assert stored["stories"]["S1"]["agent_instructions"] == "new instructions"
    # The notice describes the saved manifest, not a pre-save in-memory copy:
    # the dedup key embeds the digest of what is now on disk.
    assert record["dedup_key"] == f"{PENDING}:p:{stored['approval']['digest'][:12]}"


def test_patch_story_runtime_only_change_does_not_notify(plan, enabled, records):
    result = PipelineService().patch_story(
        "p", "S1", {"pr_url": "https://example.invalid/pr/1"}
    )

    assert result["ok"] is True
    assert records == []
    stored = _read_manifest(plan)
    assert stored["approval"]["status"] == "approved"
    assert stored["stories"]["S1"]["pr_url"] == "https://example.invalid/pr/1"


def test_patch_plan_role_config_change_notifies(plan, enabled, records):
    result = PipelineService().patch_plan(
        "p", {"role_config": {"engineer": {"model": "opus"}}}
    )

    assert result["ok"] is True
    assert len(records) == 1
    (record,) = records
    assert record["event"] == PENDING
    assert record["plan_name"] == "p"
    stored = _read_manifest(plan)
    assert stored["role_config"] == {"engineer": {"model": "opus"}}
    assert stored["approval"]["status"] == "pending_approval"


def test_patch_plan_identical_role_config_does_not_notify(plan, enabled, records):
    result = PipelineService().patch_plan("p", {"role_config": {}})

    assert result["ok"] is True
    assert records == []
    assert _read_manifest(plan)["approval"]["status"] == "approved"


def test_notifications_disabled_sends_nothing(plan, disabled, records):
    result = PipelineService().patch_story(
        "p", "S1", {"agent_instructions": "new instructions"}
    )

    assert result["ok"] is True
    assert records == []
    # No reset happens with the gate off, so the approved record survives as-is
    # and nothing is announced.
    assert _read_manifest(plan)["approval"]["status"] == "approved"


def test_plan_approval_doc_mentions_reset_notifications():
    document = _DOC_PATH.read_text()

    assert "## 11. Approval notifications" in document
    section = document.split("## 11. Approval notifications", 1)[1]
    assert "patch_story" in section
