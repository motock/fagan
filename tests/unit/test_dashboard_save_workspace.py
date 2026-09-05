"""Tests for threading workspace through POST /api/plans/{plan_name}/save.

Two changes under test:

1. ``SavePlanRequest`` (app/dashboard_models.py) gains an additive
   ``workspace: str | None = None`` field.
2. ``save_plan`` (app/dashboard.py) resolves the effective workspace as
   ``request.workspace if request.workspace is not None else
   _service.get_active_workspace()`` and passes it positionally to
   ``_service.save_plan(plan_name, request.plan_json, workspace)``.

``pipeline.service.PipelineService.save_plan`` already accepts and validates
a ``workspace`` argument, overwriting the plan's own ``repo_root`` with the
server-resolved path (WS-11) when one is supplied. This module tests only
the dashboard-side wiring: which workspace value reaches the service, and
that a validation failure surfaces as a 400 with a sanitized error and no
plan file written (fail-closed - never fall back to the plan's own
repo_root on a workspace validation failure).

Neither the model field nor the route wiring exists yet on this branch, so
every test in this module must fail until app/dashboard_models.py and
app/dashboard.py are updated: the "explicit workspace wins" / "active
workspace fallback" / "fail closed" tests fail because
``SavePlanRequest`` rejects (or silently drops, since pydantic ignores
unknown fields sent by a client unless config forbids extras - here the
request simply lacks the field so ``request.workspace`` doesn't exist) an
unrecognized ``workspace`` key and the route never resolves the active
workspace at all.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline import ticketing as pt


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect every PLAN_DIR binding the save path touches (the
    dashboard's own copy, plus pipeline.server/persistence/concurrency's)
    to the same tmp directory, mirroring
    tests/unit/test_dashboard_plan_write_routes.py's plan_dir fixture."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(d, "PLAN_DIR", directory)
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


@pytest.fixture(autouse=True)
def _plane_disabled(monkeypatch):
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")


@pytest.fixture
def client():
    return TestClient(d.app)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _init_git_repo(path: Path) -> Path:
    """Create a real, minimal git repo with one commit at *path*."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    _git("commit", "--allow-empty", "-m", "init", cwd=path)
    return path


def _seed_active_workspace(plan_dir: Path, path: str) -> None:
    (plan_dir / "active_workspace.json").write_text(json.dumps({"path": path}))


def _plan(repo_root: str) -> dict:
    return {
        "repo_root": repo_root,
        "epics": [{"summary": "Epic 1", "stories": [{"summary": "Story 1"}]}],
    }


# ---------------------------------------------------------------------------
# Active-workspace fallback
# ---------------------------------------------------------------------------


def test_save_plan_falls_back_to_active_workspace_when_body_omits_it(client, plan_dir, tmp_path):
    repo = _init_git_repo(tmp_path / "active-repo")
    _seed_active_workspace(plan_dir, str(repo))
    plan = _plan("some/model-authored/path")

    res = client.post(
        "/api/plans/withactive/save",
        json={"plan_json": json.dumps(plan)},
    )

    assert res.status_code == 200
    saved = json.loads((plan_dir / "withactive.json").read_text())
    assert saved["repo_root"] == str(repo.resolve())


# ---------------------------------------------------------------------------
# Explicit workspace wins over active workspace
# ---------------------------------------------------------------------------


def test_save_plan_explicit_workspace_wins_over_active_workspace(client, plan_dir, tmp_path):
    active_repo = _init_git_repo(tmp_path / "active-repo")
    explicit_repo = _init_git_repo(tmp_path / "explicit-repo")
    _seed_active_workspace(plan_dir, str(active_repo))
    plan = _plan("some/model-authored/path")

    res = client.post(
        "/api/plans/withexplicit/save",
        json={"plan_json": json.dumps(plan), "workspace": str(explicit_repo)},
    )

    assert res.status_code == 200
    saved = json.loads((plan_dir / "withexplicit.json").read_text())
    assert saved["repo_root"] == str(explicit_repo.resolve())


# ---------------------------------------------------------------------------
# Regression: neither present -> today's behavior unchanged
# ---------------------------------------------------------------------------


def test_save_plan_repo_root_passes_through_when_no_workspace_anywhere(client, plan_dir):
    plan = _plan("some/model-authored/path")

    res = client.post(
        "/api/plans/noworkspace/save",
        json={"plan_json": json.dumps(plan)},
    )

    assert res.status_code == 200
    saved = json.loads((plan_dir / "noworkspace.json").read_text())
    assert saved["repo_root"] == "some/model-authored/path"


# ---------------------------------------------------------------------------
# Fail-closed: explicit invalid workspace
# ---------------------------------------------------------------------------


def test_save_plan_explicit_invalid_workspace_returns_400_and_writes_nothing(client, plan_dir):
    plan = _plan("some/model-authored/path")

    res = client.post(
        "/api/plans/badexplicit/save",
        json={"plan_json": json.dumps(plan), "workspace": "/etc/x"},
    )

    assert res.status_code == 400
    detail = res.json()["detail"]
    assert str(Path("/etc/x").resolve()) not in detail
    assert not (plan_dir / "badexplicit.json").exists()


# ---------------------------------------------------------------------------
# Fail-closed: active workspace present but now invalid
# ---------------------------------------------------------------------------


def test_save_plan_stale_active_workspace_returns_400_and_writes_nothing(client, plan_dir, tmp_path):
    deleted = tmp_path / "no-longer-here"
    _seed_active_workspace(plan_dir, str(deleted))
    plan = _plan("some/model-authored/path")

    res = client.post(
        "/api/plans/staleactive/save",
        json={"plan_json": json.dumps(plan)},
    )

    assert res.status_code == 400
    assert not (plan_dir / "staleactive.json").exists()
