"""Tests for the dashboard's story dispatch / start HTTP routes.

W1c-02 adds two write routes to app/dashboard.py that delegate to the
`PipelineService` singleton (`_service`), mirroring the one-line MCP tool
delegation in pipeline/server.py:

  * POST /api/plans/{plan_name}/stories/{story_key}/dispatch
        -> _service.dispatch_story(plan_name, story_key)
  * POST /api/plans/{plan_name}/stories/{story_key}/start
        -> _service.mark_story_in_progress(plan_name, story_key)

dispatch_story launches a subprocess and can take a long time, so the route
must call it synchronously (no timeout, no await-blocking workaround) the
same way the MCP tool does. The routes follow the dashboard's existing
error-handling convention: a result with `ok: False` (e.g. a nonexistent
story_key) surfaces as an HTTP 404, matching how the journal/log routes
404 on a missing plan or story.

These tests stub `_service` with a fake so they never touch the real
pipeline (no subprocess, no git worktree, no ticket provider).
"""
import pytest
from fastapi.testclient import TestClient

from app import dashboard as d


class FakeService:
    """Records delegation calls and returns a scripted result per method."""

    def __init__(self):
        self.calls = []
        self.dispatch_result = {"ok": True}
        self.start_result = {"ok": True}

    def dispatch_story(self, plan_name, story_key):
        self.calls.append(("dispatch_story", plan_name, story_key))
        return self.dispatch_result

    def mark_story_in_progress(self, plan_name, story_key):
        self.calls.append(("mark_story_in_progress", plan_name, story_key))
        return self.start_result


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def fake_service(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(d, "_service", fake)
    return fake


def _write_manifest(plan_dir, name, stories):
    import json
    manifest = {"epics": {}, "stories": stories}
    (plan_dir / f"{name}.manifest.json").write_text(json.dumps(manifest))


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "PLAN_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# POST /api/plans/{plan_name}/stories/{story_key}/dispatch
# ---------------------------------------------------------------------------


def test_dispatch_route_200_delegates_to_service(client, plan_dir, fake_service):
    """A successful dispatch returns 200 and delegates to
    _service.dispatch_story(plan_name, story_key) with the exact path args."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "todo", "dependencies": []},
    })
    fake_service.dispatch_result = {"ok": True, "pid": 1234}

    res = client.post("/api/plans/demo/stories/S1/dispatch")

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert fake_service.calls == [("dispatch_story", "demo", "S1")]


def test_dispatch_route_404_for_nonexistent_story(client, plan_dir, fake_service):
    """A nonexistent story_key surfaces the service's ok:False result as an
    HTTP 404, following the dashboard's existing error-handling convention."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "todo", "dependencies": []},
    })
    fake_service.dispatch_result = {"ok": False, "error": "No such story NOPE"}

    res = client.post("/api/plans/demo/stories/NOPE/dispatch")

    assert res.status_code == 404
    assert fake_service.calls == [("dispatch_story", "demo", "NOPE")]


def test_dispatch_route_404_for_nonexistent_plan(client, plan_dir, fake_service):
    """A nonexistent plan also 404s (the service reports ok:False)."""
    fake_service.dispatch_result = {"ok": False, "error": "No such story S1"}

    res = client.post("/api/plans/ghost/stories/S1/dispatch")

    assert res.status_code == 404
    assert fake_service.calls == [("dispatch_story", "ghost", "S1")]


# ---------------------------------------------------------------------------
# POST /api/plans/{plan_name}/stories/{story_key}/start
# ---------------------------------------------------------------------------


def test_start_route_200_delegates_to_service(client, plan_dir, fake_service):
    """A successful start returns 200 and delegates to
    _service.mark_story_in_progress(plan_name, story_key)."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "todo", "dependencies": []},
    })
    fake_service.start_result = {"ok": True}

    res = client.post("/api/plans/demo/stories/S1/start")

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert fake_service.calls == [("mark_story_in_progress", "demo", "S1")]


def test_start_route_404_for_nonexistent_story(client, plan_dir, fake_service):
    """A nonexistent story_key surfaces the service's ok:False result as an
    HTTP 404."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "todo", "dependencies": []},
    })
    fake_service.start_result = {"ok": False, "error": "No such story NOPE"}

    res = client.post("/api/plans/demo/stories/NOPE/start")

    assert res.status_code == 404
    assert fake_service.calls == [("mark_story_in_progress", "demo", "NOPE")]


def test_start_route_404_for_nonexistent_plan(client, plan_dir, fake_service):
    """A nonexistent plan also 404s."""
    fake_service.start_result = {"ok": False, "error": "No such story S1"}

    res = client.post("/api/plans/ghost/stories/S1/start")

    assert res.status_code == 404
    assert fake_service.calls == [("mark_story_in_progress", "ghost", "S1")]
