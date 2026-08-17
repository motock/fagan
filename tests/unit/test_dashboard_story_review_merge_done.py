"""Tests for the dashboard's story review / approve_merge / done HTTP routes.

This story (W1c-05) adds three write routes to app/dashboard.py that delegate
to the `PipelineService` singleton (`_service`), mirroring the one-line MCP
tool delegation in pipeline/server.py:

  * POST /api/plans/{plan_name}/stories/{story_key}/review
        -> _service.review_story(plan_name, story_key)
  * POST /api/plans/{plan_name}/stories/{story_key}/approve_merge
        -> _service.approve_merge(plan_name, story_key)
  * POST /api/plans/{plan_name}/stories/{story_key}/done
        -> _service.mark_story_done(plan_name, story_key)

Each route takes no body. review_story runs a reviewer pass and
approve_merge rebases/pushes/polls CI, so both can run for a long time; the
routes must call the service synchronously (no timeout wrapper) exactly the
way dispatch_story was wired in the sibling story. The routes follow the
dashboard's existing error-handling convention: a result with `ok: False`
(e.g. a nonexistent story_key, or approve_merge on a story that was never
reviewer-approved) surfaces as an HTTP 404.

These tests stub `_service` with a fake so they never touch the real
pipeline (no subprocess, no git worktree, no ticket provider, no CI poll).
"""
import pytest
from fastapi.testclient import TestClient

from app import dashboard as d


class FakeService:
    """Records delegation calls and returns a scripted result per method."""

    def __init__(self):
        self.calls = []
        self.review_result = {"ok": True}
        self.approve_merge_result = {"ok": True}
        self.mark_done_result = {"ok": True}

    def review_story(self, plan_name, story_key):
        self.calls.append(("review_story", plan_name, story_key))
        return self.review_result

    def approve_merge(self, plan_name, story_key):
        self.calls.append(("approve_merge", plan_name, story_key))
        return self.approve_merge_result

    def mark_story_done(self, plan_name, story_key):
        self.calls.append(("mark_story_done", plan_name, story_key))
        return self.mark_done_result


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
# POST /api/plans/{plan_name}/stories/{story_key}/review
# ---------------------------------------------------------------------------


def test_review_route_200_delegates_to_service(client, plan_dir, fake_service):
    """A successful review returns 200 and delegates to
    _service.review_story(plan_name, story_key) with the exact path args."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "pr_open", "dependencies": []},
    })
    fake_service.review_result = {"ok": True, "verdict": "APPROVE"}

    res = client.post("/api/plans/demo/stories/S1/review")

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["verdict"] == "APPROVE"
    assert fake_service.calls == [("review_story", "demo", "S1")]


def test_review_route_takes_no_body(client, plan_dir, fake_service):
    """The review route accepts a request with no body (no required fields)."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "pr_open", "dependencies": []},
    })
    fake_service.review_result = {"ok": True}

    res = client.post("/api/plans/demo/stories/S1/review", json=None)

    assert res.status_code == 200
    assert fake_service.calls == [("review_story", "demo", "S1")]


def test_review_route_404_for_nonexistent_story(client, plan_dir, fake_service):
    """A nonexistent story_key surfaces the service's ok:False result as an
    HTTP 404, following the dashboard's existing error-handling convention."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "pr_open", "dependencies": []},
    })
    fake_service.review_result = {"ok": False, "error": "No such story NOPE"}

    res = client.post("/api/plans/demo/stories/NOPE/review")

    assert res.status_code == 404
    assert res.json()["detail"] == "No such story NOPE"
    assert fake_service.calls == [("review_story", "demo", "NOPE")]


def test_review_route_404_for_nonexistent_plan(client, plan_dir, fake_service):
    """A nonexistent plan also 404s (the service reports ok:False)."""
    fake_service.review_result = {"ok": False, "error": "No such story S1"}

    res = client.post("/api/plans/ghost/stories/S1/review")

    assert res.status_code == 404
    assert fake_service.calls == [("review_story", "ghost", "S1")]


# ---------------------------------------------------------------------------
# POST /api/plans/{plan_name}/stories/{story_key}/approve_merge
# ---------------------------------------------------------------------------


def test_approve_merge_route_200_delegates_to_service(client, plan_dir, fake_service):
    """A successful approve_merge returns 200 and delegates to
    _service.approve_merge(plan_name, story_key)."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "pr_open", "dependencies": []},
    })
    fake_service.approve_merge_result = {"ok": True, "merged": True}

    res = client.post("/api/plans/demo/stories/S1/approve_merge")

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["merged"] is True
    assert fake_service.calls == [("approve_merge", "demo", "S1")]


def test_approve_merge_route_takes_no_body(client, plan_dir, fake_service):
    """The approve_merge route accepts a request with no body."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "pr_open", "dependencies": []},
    })
    fake_service.approve_merge_result = {"ok": True}

    res = client.post("/api/plans/demo/stories/S1/approve_merge", json=None)

    assert res.status_code == 200
    assert fake_service.calls == [("approve_merge", "demo", "S1")]


def test_approve_merge_route_404_when_not_reviewer_approved(
    client, plan_dir, fake_service
):
    """approve_merge on a story that hasn't passed review surfaces the
    service's ok:False result as an HTTP 404 with the service's message."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "todo", "dependencies": []},
    })
    fake_service.approve_merge_result = {
        "ok": False,
        "error": "Story was never reviewer-approved",
    }

    res = client.post("/api/plans/demo/stories/S1/approve_merge")

    assert res.status_code == 404
    assert res.json()["detail"] == "Story was never reviewer-approved"
    assert fake_service.calls == [("approve_merge", "demo", "S1")]


def test_approve_merge_route_404_for_nonexistent_story(client, plan_dir, fake_service):
    """A nonexistent story_key 404s for approve_merge too."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "pr_open", "dependencies": []},
    })
    fake_service.approve_merge_result = {"ok": False, "error": "No such story NOPE"}

    res = client.post("/api/plans/demo/stories/NOPE/approve_merge")

    assert res.status_code == 404
    assert fake_service.calls == [("approve_merge", "demo", "NOPE")]


def test_approve_merge_route_404_for_nonexistent_plan(client, plan_dir, fake_service):
    """A nonexistent plan 404s for approve_merge too."""
    fake_service.approve_merge_result = {"ok": False, "error": "No such story S1"}

    res = client.post("/api/plans/ghost/stories/S1/approve_merge")

    assert res.status_code == 404
    assert fake_service.calls == [("approve_merge", "ghost", "S1")]


# ---------------------------------------------------------------------------
# POST /api/plans/{plan_name}/stories/{story_key}/done
# ---------------------------------------------------------------------------


def test_done_route_200_delegates_to_service(client, plan_dir, fake_service):
    """A successful done returns 200 and delegates to
    _service.mark_story_done(plan_name, story_key)."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "pr_open", "dependencies": []},
    })
    fake_service.mark_done_result = {"ok": True, "plan_completed": False}

    res = client.post("/api/plans/demo/stories/S1/done")

    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert fake_service.calls == [("mark_story_done", "demo", "S1")]


def test_done_route_takes_no_body(client, plan_dir, fake_service):
    """The done route accepts a request with no body."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "pr_open", "dependencies": []},
    })
    fake_service.mark_done_result = {"ok": True}

    res = client.post("/api/plans/demo/stories/S1/done", json=None)

    assert res.status_code == 200
    assert fake_service.calls == [("mark_story_done", "demo", "S1")]


def test_done_route_404_for_nonexistent_story(client, plan_dir, fake_service):
    """A nonexistent story_key surfaces the service's ok:False result as an
    HTTP 404."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a story", "status": "pr_open", "dependencies": []},
    })
    fake_service.mark_done_result = {"ok": False, "error": "No such story NOPE"}

    res = client.post("/api/plans/demo/stories/NOPE/done")

    assert res.status_code == 404
    assert res.json()["detail"] == "No such story NOPE"
    assert fake_service.calls == [("mark_story_done", "demo", "NOPE")]


def test_done_route_404_for_nonexistent_plan(client, plan_dir, fake_service):
    """A nonexistent plan also 404s for done."""
    fake_service.mark_done_result = {"ok": False, "error": "No such story S1"}

    res = client.post("/api/plans/ghost/stories/S1/done")

    assert res.status_code == 404
    assert fake_service.calls == [("mark_story_done", "ghost", "S1")]
