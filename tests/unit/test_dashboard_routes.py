"""Tests for the dashboard's orchestration POST routes.

W1c-07 adds three write routes to app/dashboard.py that delegate to the
`PipelineService` singleton (`_service`):

  * POST /api/plans/{plan_name}/advance
        -> _service.advance_pipeline(plan_name)
  * POST /api/plans/advance_all
        -> _service.advance_all_plans()
  * POST /api/plans/{plan_name}/stories/{story_key}/checkpoint
        -> _service.checkpoint(plan_name, story_key, step, summary, next_hint)

The checkpoint route reads a JSON body with required `step` and `summary`
fields and an optional `next_hint`; a missing required field is a 4xx
validation error. These tests stub `_service` with a fake so they never
touch the real pipeline.
"""
import pytest
from fastapi.testclient import TestClient

from app import dashboard as d


class FakeService:
    """Records delegation calls and returns a scripted result per method."""

    def __init__(self):
        self.calls = []
        self.advance_result = {"ok": True}
        self.advance_all_result = {"ok": True, "plans": {}}
        self.checkpoint_result = {"ok": True}

    def advance_pipeline(self, plan_name):
        self.calls.append(("advance_pipeline", plan_name))
        return self.advance_result

    def advance_all_plans(self):
        self.calls.append(("advance_all_plans",))
        return self.advance_all_result

    def checkpoint(self, plan_name, story_key, step, summary, next_hint):
        self.calls.append(
            ("checkpoint", plan_name, story_key, step, summary, next_hint)
        )
        return self.checkpoint_result


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def fake_service(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(d, "_service", fake)
    return fake


# ---------------------------------------------------------------------------
# POST /api/plans/{plan_name}/advance
# ---------------------------------------------------------------------------


def test_advance_pipeline_200(client, fake_service):
    resp = client.post("/api/plans/demo/advance")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert fake_service.calls == [("advance_pipeline", "demo")]


# ---------------------------------------------------------------------------
# POST /api/plans/advance_all
# ---------------------------------------------------------------------------


def test_advance_all_200(client, fake_service):
    resp = client.post("/api/plans/advance_all")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "plans": {}}
    assert fake_service.calls == [("advance_all_plans",)]


# ---------------------------------------------------------------------------
# POST /api/plans/{plan_name}/stories/{story_key}/checkpoint
# ---------------------------------------------------------------------------


def test_checkpoint_200(client, fake_service):
    resp = client.post(
        "/api/plans/demo/stories/S1/checkpoint",
        json={"step": "s1", "summary": "sum"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert fake_service.calls == [
        ("checkpoint", "demo", "S1", "s1", "sum", "")
    ]


def test_checkpoint_200_with_next_hint(client, fake_service):
    resp = client.post(
        "/api/plans/demo/stories/S1/checkpoint",
        json={"step": "s1", "summary": "sum", "next_hint": "do next"},
    )
    assert resp.status_code == 200
    assert fake_service.calls == [
        ("checkpoint", "demo", "S1", "s1", "sum", "do next")
    ]


def test_checkpoint_missing_step(client, fake_service):
    resp = client.post(
        "/api/plans/demo/stories/S1/checkpoint",
        json={"summary": "sum"},
    )
    assert resp.status_code in (400, 422)
    assert fake_service.calls == []


def test_checkpoint_missing_summary(client, fake_service):
    resp = client.post(
        "/api/plans/demo/stories/S1/checkpoint",
        json={"step": "s1"},
    )
    assert resp.status_code in (400, 422)
    assert fake_service.calls == []
