"""SQ-09: operator actions request a scheduler wake when they succeed.

An operator who approves, resumes or ingests a plan, or edits a story, expects
the scheduler to pick the change up. Without a wake the dispatcher only notices
on its next reconcile, so each operator entry point on ``PipelineService`` and
``PlanApprovalMixin`` wraps its success return in
``pipeline.wake.after(plan_name, "<method name>", result)``.

These tests patch ``pipeline.wake.request_wake`` with a recorder and drive the
real service methods, pinning both halves of the contract:

* a successful call requests exactly one wake, naming the plan and the method;
* a refused, failed or skipped call requests none -- the helper only wakes on
  a success (``ok`` not ``False`` and no ``skipped``);
* ``reject_plan`` never wakes: a change request is not an advance.

``plan_dir`` mirrors the per-test fixture in
``tests/unit/test_patch_plan_enforcement.py``: a throwaway directory bound to
every module that holds its own ``PLAN_DIR``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# pipeline.server loads before any other pipeline module (import cycle).
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import paths, plan_approval, wake
from pipeline import persistence as ppers
from pipeline import plan_completion as pcomp
from pipeline.actor import ANONYMOUS, bind_principal
from pipeline.plan_approval import canonical_digest
from pipeline.service import PipelineService
from tests.unit._pipeline_mcp_server_test_helpers import (
    _isolate_usage_state,  # noqa: F401
)

PLAN = "demo"
STORY = "S1"
PR = "https://github.com/example/repo/pull/7"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding at a throwaway directory.

    ``pipeline.paths`` is included because :func:`pipeline.wake.request_wake`
    resolves its marker directory from ``paths.PLAN_DIR`` at call time: without
    this patch an unpatched ``request_wake`` would write a marker into the
    developer's (or the CI runner's) real plans directory.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    monkeypatch.setattr(pcomp, "PLAN_DIR", d)
    monkeypatch.setattr(paths, "PLAN_DIR", d)
    return d


@pytest.fixture
def wakes(monkeypatch):
    """Record every ``request_wake`` call instead of writing a marker."""
    recorded: list[tuple[str, str]] = []

    def _record(plan_name: str, reason: str) -> None:
        recorded.append((plan_name, reason))

    monkeypatch.setattr(wake, "request_wake", _record)
    return recorded


def _story(**overrides):
    story = {
        "summary": "demo story",
        "agent_instructions": "do the thing",
        "status": "todo",
        "dependencies": [],
        "blocked_by": [],
    }
    story.update(overrides)
    return story


def _write_manifest(plan_dir: Path, *, stories=None, **overrides) -> dict:
    manifest = {
        "repo_root": "/tmp/repo",
        "role_config": {"default": "sonnet"},
        "stories": {STORY: _story()} if stories is None else stories,
    }
    manifest.update(overrides)
    (plan_dir / f"{PLAN}.manifest.json").write_text(json.dumps(manifest))
    return manifest


def _write_approvable_manifest(plan_dir: Path) -> dict:
    """Write a manifest whose stored approval digest matches its content."""
    (plan_dir / f"{PLAN}.manifest.json").write_text("{}")
    manifest = {
        "repo_root": "/tmp/repo",
        "role_config": {"default": "sonnet"},
        "stories": {STORY: _story()},
    }
    manifest["approval"] = {
        "status": "pending_approval",
        "digest": canonical_digest(manifest),
        "warnings_seen": [],
    }
    (plan_dir / f"{PLAN}.manifest.json").write_text(json.dumps(manifest))
    return manifest


def _write_source(plan_dir: Path, repo_root: Path) -> None:
    """Write the raw plan source ``ingest_plan`` reads."""
    plan = {
        "name": PLAN,
        "repo_root": str(repo_root),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    {
                        "key": "s1",
                        "summary": "S1",
                        "agent_instructions": "Do the thing.",
                    }
                ],
            }
        ],
    }
    (plan_dir / f"{PLAN}.json").write_text(json.dumps(plan))


@pytest.fixture
def ingest_provider(monkeypatch, tmp_path):
    """A recording ticket provider plus the ingest notification seam."""
    from pipeline import ingest as ingest_mod

    class _Provider:
        def create_epic(self, summary):
            return None

        def create_story(self, summary, description, epic_id, agent):
            return None

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _Provider())
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n")
    return repo


# --------------------------------------------------------------------------- #
# Positive: each operator action that advances a plan requests one wake
# --------------------------------------------------------------------------- #
def test_resume_plan_requests_one_wake(plan_dir, wakes) -> None:
    _write_manifest(plan_dir, paused=True)

    with bind_principal(None):
        result = PipelineService().resume_plan(PLAN)

    assert result["ok"] is True, result
    assert wakes == [(PLAN, "resume_plan")]


def test_ingest_plan_requests_one_wake(plan_dir, wakes, ingest_provider) -> None:
    _write_source(plan_dir, ingest_provider)

    with bind_principal(None):
        result = PipelineService().ingest_plan(PLAN)

    assert result.get("ok") is True, result
    assert wakes == [(PLAN, "ingest_plan")]


def test_set_story_status_requests_one_wake(plan_dir, wakes) -> None:
    _write_manifest(plan_dir)

    with bind_principal(None):
        result = PipelineService().set_story_status(PLAN, STORY, "parked")

    assert result["ok"] is True, result
    assert wakes == [(PLAN, "set_story_status")]


def test_patch_story_requests_one_wake(plan_dir, wakes) -> None:
    _write_manifest(plan_dir)

    with bind_principal(None):
        result = PipelineService().patch_story(PLAN, STORY, {"summary": "edited"})

    assert result["ok"] is True, result
    assert wakes == [(PLAN, "patch_story")]


def test_mark_story_done_requests_one_wake(plan_dir, wakes) -> None:
    _write_manifest(plan_dir, stories={STORY: _story(status="pr_open", pr_url=PR)})

    with bind_principal(None):
        result = PipelineService().mark_story_done(PLAN, STORY)

    assert result["ok"] is True, result
    assert wakes == [(PLAN, "mark_story_done")]


def test_approve_plan_requests_one_wake(plan_dir, wakes, monkeypatch) -> None:
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")
    _write_approvable_manifest(plan_dir)

    with bind_principal(None):
        result = PipelineService().approve_plan(PLAN)

    assert result["ok"] is True, result
    assert wakes == [(PLAN, "approve_plan")]


def test_successful_action_reaches_the_real_wake_writer(plan_dir, monkeypatch) -> None:
    """The call site wakes through ``request_wake`` itself, not only the helper.

    ``request_wake`` is left unpatched here, so this pins the marker the
    scheduler actually scans: one file per plan, under the plan directory, with
    the method name as its recorded reason.
    """
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")
    _write_approvable_manifest(plan_dir)

    with bind_principal(None):
        result = PipelineService().approve_plan(PLAN)

    assert result["ok"] is True, result
    marker = plan_dir / wake.WAKE_DIR_NAME / PLAN
    assert marker.is_file()
    assert json.loads(marker.read_text())["reason"] == "approve_plan"
    assert [path.name for path in (plan_dir / wake.WAKE_DIR_NAME).iterdir()] == [PLAN]


# --------------------------------------------------------------------------- #
# Negative: a refusal, failure or skip must not wake the scheduler
# --------------------------------------------------------------------------- #
def test_resume_plan_refusal_records_no_wake(plan_dir, wakes) -> None:
    # No manifest on disk: the pause/resume path refuses.

    with bind_principal(None):
        result = PipelineService().resume_plan(PLAN)

    assert result.get("ok") is False, result
    assert wakes == []


def test_ingest_plan_failure_records_no_wake(plan_dir, wakes, ingest_provider) -> None:
    # No raw plan source: nothing to ingest.

    with bind_principal(None):
        result = PipelineService().ingest_plan(PLAN)

    assert result.get("ok") is False, result
    assert wakes == []


def test_set_story_status_rejects_invalid_status_without_waking(plan_dir, wakes) -> None:
    _write_manifest(plan_dir)

    with bind_principal(None):
        result = PipelineService().set_story_status(PLAN, STORY, "not-a-status")

    assert result.get("ok") is False, result
    assert wakes == []


def test_set_story_status_refuses_unknown_story_without_waking(plan_dir, wakes) -> None:
    _write_manifest(plan_dir)

    with bind_principal(None):
        result = PipelineService().set_story_status(PLAN, "ghost", "parked")

    assert result.get("ok") is False, result
    assert wakes == []


def test_patch_story_patchable_field_refusal_records_no_wake(plan_dir, wakes) -> None:
    _write_manifest(plan_dir)

    with bind_principal(None):
        result = PipelineService().patch_story(PLAN, STORY, {"epics": {}})

    assert result.get("ok") is False, result
    assert wakes == []


def test_mark_story_done_denial_records_no_wake(plan_dir, wakes) -> None:
    _write_manifest(plan_dir, stories={STORY: _story(status="pr_open", pr_url=PR)})

    with bind_principal(ANONYMOUS):
        result = PipelineService().mark_story_done(PLAN, STORY)

    assert result.get("ok") is False, result
    assert wakes == []


def test_approval_disabled_records_no_wake(plan_dir, wakes, monkeypatch) -> None:
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)
    _write_approvable_manifest(plan_dir)

    with bind_principal(None):
        result = PipelineService().approve_plan(PLAN)

    assert result.get("skipped") == "approval_disabled", result
    assert wakes == []


def test_digest_mismatch_records_no_wake(plan_dir, wakes, monkeypatch) -> None:
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")
    manifest = _write_approvable_manifest(plan_dir)
    # Content changed after the operator saw it: the stored digest is stale.
    manifest["stories"][STORY]["agent_instructions"] = "something else entirely"
    (plan_dir / f"{PLAN}.manifest.json").write_text(json.dumps(manifest))

    with bind_principal(None):
        result = PipelineService().approve_plan(PLAN)

    assert result.get("ok") is False, result
    assert wakes == []


def test_reject_plan_records_no_wake(plan_dir, wakes, monkeypatch) -> None:
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")
    _write_approvable_manifest(plan_dir)

    with bind_principal(None):
        result = PipelineService().reject_plan(PLAN, reason="needs tests")

    assert result["ok"] is True, result
    assert wakes == []
