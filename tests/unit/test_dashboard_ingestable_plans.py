"""Tests for GET /api/ingestable-plans (dashboard ingest-picker listing).

Distinct from GET /api/plans: /api/plans lists already-ingested plan
manifests, while /api/ingestable-plans lists raw plan-source files that are
valid ingest targets (see PipelineService.list_ingestable_plans).
"""
from fastapi.testclient import TestClient

from app import dashboard as d

_HEADER = "X-Pipeline-Api-Key"


def test_ingestable_plans_endpoint_returns_service_list(monkeypatch):
    monkeypatch.setattr(d._service, "list_ingestable_plans", lambda: ["foo", "bar"])
    client = TestClient(d.app)

    response = client.get("/api/ingestable-plans")

    assert response.status_code == 200
    assert response.json() == {"plans": ["foo", "bar"]}


def test_ingestable_plans_requires_api_key():
    """Missing X-Pipeline-Api-Key is rejected like any other /api/... route.

    tests/unit/conftest.py attaches the dashboard key header to every
    TestClient by default; drop it to exercise the missing-header path
    (same pattern as tests/unit/test_dashboard_auth.py).
    """
    client = TestClient(d.app)
    client.headers.pop(_HEADER, None)

    response = client.get("/api/ingestable-plans")

    assert response.status_code == 401