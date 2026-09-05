"""Tests for two changes to the workspace routes block in app/dashboard.py
(lines 507-517 at the time this story was authored):

1. NEW route ``GET /api/workspace`` -> ``{"active": _service.get_active_workspace()}``.
   Must never 500 on a corrupt ``active_workspace.json`` (the store's own
   tolerance, landed in an earlier story and covered by
   tests/unit/test_store_active_workspace.py, already degrades a corrupt
   file to ``None``) - the route must not add its own try/except.

2. ``POST /api/workspace`` (``set_workspace_route``) full-block replaced so
   that, in addition to the existing ``_service.resolve_workspace`` delegation,
   it calls ``_service.set_active_workspace(result["path"])`` -- but ONLY when
   the resolve result is ``ok``. A rejected/invalid path must leave whatever
   was previously active untouched.

Neither the GET route nor the updated POST behavior exists yet on this
branch. Until app/dashboard.py is updated, the GET tests fail with a 404
(TestClient hitting an unregistered route) and the "active recording"
POST tests fail because ``_service.set_active_workspace`` is never called
(also, ``PipelineService.set_active_workspace``/``get_active_workspace``
must already exist from an earlier story - if they don't, these tests fail
with AttributeError instead, which is the same correct RED state).
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

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect every PLAN_DIR binding the workspace path touches to a tmp
    directory, mirroring tests/unit/test_dashboard_plan_write_routes.py's
    plan_dir fixture. ``_service.get_active_workspace``/``set_active_workspace``
    delegate to ``pipeline.server._store`` (a FileStore), which resolves
    PLAN_DIR through ``pipeline.server.PLAN_DIR`` at call time - patching
    ``p.PLAN_DIR`` is what actually redirects the active_workspace.json file.
    """
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(d, "PLAN_DIR", directory)
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


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


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def test_get_workspace_route_is_registered_as_get():
    routes = {
        (route.path, method)
        for route in d.app.routes
        for method in getattr(route, "methods", set()) or set()
    }
    assert ("/api/workspace", "GET") in routes


def test_set_workspace_route_is_still_registered_as_post():
    routes = {
        (route.path, method)
        for route in d.app.routes
        for method in getattr(route, "methods", set()) or set()
    }
    assert ("/api/workspace", "POST") in routes


# ---------------------------------------------------------------------------
# GET /api/workspace
# ---------------------------------------------------------------------------


def test_get_workspace_returns_active_none_on_fresh_state(client, plan_dir):
    res = client.get("/api/workspace")

    assert res.status_code == 200
    assert res.json() == {"active": None}


def test_get_workspace_delegates_to_service_get_active_workspace(client, plan_dir, monkeypatch):
    monkeypatch.setattr(d._service, "get_active_workspace", lambda: "/some/active/path")

    res = client.get("/api/workspace")

    assert res.status_code == 200
    assert res.json() == {"active": "/some/active/path"}


def test_get_workspace_does_not_500_on_corrupt_active_workspace_json(client, plan_dir):
    (plan_dir / "active_workspace.json").write_text("{not valid json::: garbage")

    res = client.get("/api/workspace")

    assert res.status_code == 200
    assert res.json() == {"active": None}


def test_get_workspace_does_not_500_on_non_dict_active_workspace_json(client, plan_dir):
    (plan_dir / "active_workspace.json").write_text(json.dumps(["not", "a", "dict"]))

    res = client.get("/api/workspace")

    assert res.status_code == 200
    assert res.json() == {"active": None}


# ---------------------------------------------------------------------------
# POST /api/workspace -- happy path, real (non-mocked) resolution
# ---------------------------------------------------------------------------


def test_post_workspace_valid_repo_returns_200_ok_true(client, plan_dir, tmp_path):
    repo = _init_git_repo(tmp_path / "myrepo")

    res = client.post("/api/workspace", json={"path": str(repo)})

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["path"]


def test_get_workspace_after_successful_post_returns_the_resolved_path(client, plan_dir, tmp_path):
    repo = _init_git_repo(tmp_path / "myrepo")

    post_res = client.post("/api/workspace", json={"path": str(repo)})
    assert post_res.status_code == 200
    resolved_path = post_res.json()["path"]

    get_res = client.get("/api/workspace")

    assert get_res.status_code == 200
    assert get_res.json() == {"active": resolved_path}


# ---------------------------------------------------------------------------
# POST /api/workspace -- negative/boundary paths, real (non-mocked) resolution
# ---------------------------------------------------------------------------


def test_post_workspace_denylisted_path_returns_400_with_generic_detail(client, plan_dir):
    res = client.post("/api/workspace", json={"path": "/etc/ws-test"})

    assert res.status_code == 400
    detail = res.json()["detail"]
    assert isinstance(detail, str)
    assert "/etc" not in detail
    assert "/private" not in detail


def test_post_workspace_denylisted_path_does_not_change_active_workspace(client, plan_dir):
    before = client.get("/api/workspace").json()
    assert before == {"active": None}

    res = client.post("/api/workspace", json={"path": "/etc/ws-test"})
    assert res.status_code == 400

    after = client.get("/api/workspace").json()
    assert after == {"active": None}


def test_post_workspace_nonexistent_path_returns_400(client, plan_dir, tmp_path):
    missing = tmp_path / "does-not-exist-anywhere"

    res = client.post("/api/workspace", json={"path": str(missing)})

    assert res.status_code == 400
    assert res.json()["detail"] == "path does not exist"


def test_post_workspace_rejected_path_leaves_previous_active_workspace_untouched(
    client, plan_dir, tmp_path
):
    repo = _init_git_repo(tmp_path / "first-repo")
    first = client.post("/api/workspace", json={"path": str(repo)})
    assert first.status_code == 200
    previous_active = first.json()["path"]

    missing = tmp_path / "does-not-exist-anywhere"
    rejected = client.post("/api/workspace", json={"path": str(missing)})
    assert rejected.status_code == 400

    after = client.get("/api/workspace")
    assert after.status_code == 200
    assert after.json() == {"active": previous_active}


def test_post_workspace_empty_string_path_returns_400(client, plan_dir):
    res = client.post("/api/workspace", json={"path": ""})

    assert res.status_code == 400
    assert res.json()["detail"] == "workspace path must not be empty"


def test_post_workspace_missing_path_field_returns_422(client, plan_dir):
    res = client.post("/api/workspace", json={})

    assert res.status_code == 422


def test_post_workspace_wrong_type_path_returns_422(client, plan_dir):
    res = client.post("/api/workspace", json={"path": {"nested": "value"}})

    assert res.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/workspace -- mocked-service tests pinning the exact
# "record active only on success" semantic and the delegation call shape
# ---------------------------------------------------------------------------


def test_set_workspace_calls_set_active_workspace_with_resolved_path_on_success(
    client, plan_dir, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        d._service,
        "resolve_workspace",
        lambda path, create=False: {"ok": True, "path": "/resolved/from/service", "error": None},
    )
    monkeypatch.setattr(d._service, "set_active_workspace", lambda path: calls.append(path))

    res = client.post("/api/workspace", json={"path": "/whatever"})

    assert res.status_code == 200
    assert calls == ["/resolved/from/service"]


def test_set_workspace_does_not_call_set_active_workspace_on_failure(
    client, plan_dir, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        d._service,
        "resolve_workspace",
        lambda path, create=False: {"ok": False, "path": "", "error": "boom"},
    )
    monkeypatch.setattr(d._service, "set_active_workspace", lambda path: calls.append(path))

    res = client.post("/api/workspace", json={"path": "/whatever"})

    assert res.status_code == 400
    assert calls == []


def test_set_workspace_delegates_path_and_create_flag_to_resolve_workspace(
    client, plan_dir, monkeypatch
):
    calls = []

    def fake_resolve(path, create=False):
        calls.append((path, create))
        return {"ok": True, "path": path, "error": None}

    monkeypatch.setattr(d._service, "resolve_workspace", fake_resolve)
    monkeypatch.setattr(d._service, "set_active_workspace", lambda path: None)

    res = client.post("/api/workspace", json={"path": "/x/y", "create": True})

    assert res.status_code == 200
    assert calls == [("/x/y", True)]


def test_set_workspace_create_defaults_to_false_when_omitted(client, plan_dir, monkeypatch):
    calls = []

    def fake_resolve(path, create=False):
        calls.append((path, create))
        return {"ok": True, "path": path, "error": None}

    monkeypatch.setattr(d._service, "resolve_workspace", fake_resolve)
    monkeypatch.setattr(d._service, "set_active_workspace", lambda path: None)

    res = client.post("/api/workspace", json={"path": "/x/y"})

    assert res.status_code == 200
    assert calls == [("/x/y", False)]


def test_set_workspace_returns_service_result_as_is(client, plan_dir, monkeypatch):
    sentinel = {"ok": True, "path": "/abc", "error": None}
    monkeypatch.setattr(d._service, "resolve_workspace", lambda path, create=False: sentinel)
    monkeypatch.setattr(d._service, "set_active_workspace", lambda path: None)

    res = client.post("/api/workspace", json={"path": "/abc"})

    assert res.status_code == 200
    assert res.json() == sentinel


def test_set_workspace_error_detail_defaults_to_unknown_error_when_service_omits_it(
    client, plan_dir, monkeypatch
):
    monkeypatch.setattr(d._service, "resolve_workspace", lambda path, create=False: {"ok": False})

    res = client.post("/api/workspace", json={"path": "/abc"})

    assert res.status_code == 400
    assert res.json()["detail"] == "Unknown error"
