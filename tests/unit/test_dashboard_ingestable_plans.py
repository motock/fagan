import pytest
from fastapi.testclient import TestClient

from app import auth
from app import dashboard as d

# Use the same header name as auth tests
_HEADER = "X-Pipeline-Api-Key"

@pytest.fixture
def client_with_key():
    client = TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})
    return client


def test_ingestable_plans_endpoint_returns_service_list(monkeypatch, client_with_key):
    # Mock the service method
    monkeypatch.setattr(d._service, "list_ingestable_plans", lambda: ["foo", "bar"])
    response = client_with_key.get("/api/ingestable-plans")
    assert response.status_code == 200
    assert response.json() == {"plans": ["foo", "bar"]}


def test_ingestable_plans_requires_api_key(client_with_key):
    # Remove header to test auth
    client = TestClient(d.app)
    response = client.get("/api/ingestable-plans")
    assert response.status_code == 401

