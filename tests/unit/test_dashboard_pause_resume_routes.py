"""Tests for W1c-03: `POST /api/plans/{plan_name}/pause` and
`POST /api/plans/{plan_name}/resume` on app/dashboard.py.

Both routes delegate to the real `_service.pause_plan` /
`_service.resume_plan` (PipelineService methods, already proven in
tests/unit/test_pipeline_mcp_server.py and test_pipeline_service_seam.py) -
these are integration tests against the actual manifest-mutation side
effect, not mocks of internal application logic. Neither route accepts a
request body. Following the archive/unarchive precedent already in
app/dashboard.py, a missing manifest must surface as an HTTPException
(404-style) rather than a silent no-op or 200.

The implementation does not exist yet on this branch, so this file is
intentionally RED (AttributeError/404-route-not-found on the new paths)
until a later dispatch adds the two routes.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from pipeline import concurrency as pcon
from pipeline import server as pserver


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    # _service.pause_plan/resume_plan run the real PipelineService code
    # path, which reads PLAN_DIR as a free variable in both pipeline.server
    # (FileStore) and pipeline.concurrency (_plan_lock's lock-file path) -
    # both must point at the same tmp dir as the dashboard's own PLAN_DIR,
    # or the route and the service disagree about where the manifest lives.
    monkeypatch.setattr(d, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(pserver, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(pcon, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(d.app)


def _write_manifest(plan_dir, name, stories, paused=False):
    manifest = {"epics": {}, "stories": stories}
    if paused:
        manifest["paused"] = True
    (plan_dir / f"{name}.manifest.json").write_text(json.dumps(manifest))


def _read_manifest(plan_dir, name):
    return json.loads((plan_dir / f"{name}.manifest.json").read_text())


_STORY = {"T1": {"summary": "todo", "status": "todo", "dependencies": []}}


# --- POST /api/plans/{plan_name}/pause: happy path -------------------------


def test_pause_plan_returns_200_for_existing_plan(client, plan_dir):
    _write_manifest(plan_dir, "tobehalted", _STORY)

    res = client.post("/api/plans/tobehalted/pause")

    assert res.status_code == 200


def test_pause_plan_response_body_matches_service_result(client, plan_dir):
    _write_manifest(plan_dir, "tobehalted", _STORY)

    res = client.post("/api/plans/tobehalted/pause")

    # Matches PipelineService.pause_plan's real return shape verbatim
    # (see test_pause_plan_sets_manifest_flag in test_pipeline_mcp_server.py)
    # - the route must delegate to _service, not reimplement the mutation.
    assert res.json() == {"ok": True, "plan_name": "tobehalted", "paused": True}


def test_pause_plan_persists_paused_flag_on_manifest(client, plan_dir):
    _write_manifest(plan_dir, "tobehalted", _STORY)

    client.post("/api/plans/tobehalted/pause")

    assert _read_manifest(plan_dir, "tobehalted")["paused"] is True


def test_pause_plan_no_request_body_required(client, plan_dir):
    """Neither route takes a request body - a bare POST with no body/content
    type must succeed, not 422 for a missing/invalid request payload."""
    _write_manifest(plan_dir, "tobehalted", _STORY)

    res = client.post("/api/plans/tobehalted/pause")

    assert res.status_code != 422


def test_pause_plan_is_idempotent_when_already_paused(client, plan_dir):
    _write_manifest(plan_dir, "already-paused", _STORY, paused=True)

    res = client.post("/api/plans/already-paused/pause")

    assert res.status_code == 200
    assert res.json() == {"ok": True, "plan_name": "already-paused", "paused": True}
    assert _read_manifest(plan_dir, "already-paused")["paused"] is True


# --- POST /api/plans/{plan_name}/pause: not-found -------------------------


def test_pause_plan_404_when_no_manifest(client, plan_dir):
    res = client.post("/api/plans/never-ingested/pause")

    assert res.status_code == 404


def test_pause_plan_404_error_detail_names_the_missing_plan(client, plan_dir):
    res = client.post("/api/plans/never-ingested/pause")

    detail = res.json()["detail"]
    assert isinstance(detail, str)
    assert "never-ingested" in detail
    assert "manifest" in detail.lower()


def test_pause_plan_404_does_not_create_a_manifest(client, plan_dir):
    """A pause on a plan with no manifest must not have a side effect -
    the 404 path should leave PLAN_DIR exactly as it was."""
    client.post("/api/plans/never-ingested/pause")

    assert not (plan_dir / "never-ingested.manifest.json").exists()


# --- POST /api/plans/{plan_name}/resume: happy path ------------------------


def test_resume_plan_returns_200_for_existing_plan(client, plan_dir):
    _write_manifest(plan_dir, "halted3", _STORY, paused=True)

    res = client.post("/api/plans/halted3/resume")

    assert res.status_code == 200


def test_resume_plan_response_body_matches_service_result(client, plan_dir):
    _write_manifest(plan_dir, "halted3", _STORY, paused=True)

    res = client.post("/api/plans/halted3/resume")

    # Matches PipelineService.resume_plan's real return shape verbatim
    # (see test_resume_plan_clears_manifest_flag in test_pipeline_mcp_server.py).
    assert res.json() == {"ok": True, "plan_name": "halted3", "paused": False}


def test_resume_plan_clears_paused_flag_on_manifest(client, plan_dir):
    _write_manifest(plan_dir, "halted3", _STORY, paused=True)

    client.post("/api/plans/halted3/resume")

    assert _read_manifest(plan_dir, "halted3")["paused"] is False


def test_resume_plan_no_request_body_required(client, plan_dir):
    _write_manifest(plan_dir, "halted3", _STORY, paused=True)

    res = client.post("/api/plans/halted3/resume")

    assert res.status_code != 422


def test_resume_plan_is_noop_when_not_paused(client, plan_dir):
    _write_manifest(plan_dir, "neverhalted", _STORY)

    res = client.post("/api/plans/neverhalted/resume")

    assert res.status_code == 200
    assert res.json() == {"ok": True, "plan_name": "neverhalted", "paused": False}
    assert _read_manifest(plan_dir, "neverhalted")["paused"] is False


# --- POST /api/plans/{plan_name}/resume: not-found -------------------------


def test_resume_plan_404_when_no_manifest(client, plan_dir):
    res = client.post("/api/plans/never-ingested/resume")

    assert res.status_code == 404


def test_resume_plan_404_error_detail_names_the_missing_plan(client, plan_dir):
    res = client.post("/api/plans/never-ingested/resume")

    detail = res.json()["detail"]
    assert isinstance(detail, str)
    assert "never-ingested" in detail
    assert "manifest" in detail.lower()


def test_resume_plan_404_does_not_create_a_manifest(client, plan_dir):
    client.post("/api/plans/never-ingested/resume")

    assert not (plan_dir / "never-ingested.manifest.json").exists()


# --- pause/resume are independent of other plans ---------------------------


def test_pause_plan_does_not_affect_other_plans(client, plan_dir):
    _write_manifest(plan_dir, "target", _STORY)
    _write_manifest(plan_dir, "bystander", _STORY)

    client.post("/api/plans/target/pause")

    assert _read_manifest(plan_dir, "target")["paused"] is True
    assert _read_manifest(plan_dir, "bystander").get("paused", False) is False
