"""Tests for the dashboard's shared-secret API key gate (app/auth.py).

The gate is registered as an application-level FastAPI dependency, so every
route is covered uniformly. These tests drive the real `/api/health` route
rather than calling `require_api_key` directly, so a regression that removes
the app-level wiring (and not just the function) still fails here.
"""
import os
import stat

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import dashboard as d

_HEADER = "X-Pipeline-Api-Key"


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so these tests never read or
    create this repo's real .dashboard_api_key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


def test_request_without_api_key_header_is_rejected():
    client = TestClient(d.app)
    # tests/unit/conftest.py attaches the header to every TestClient by
    # default; drop it to exercise the missing-header path.
    client.headers.pop(_HEADER, None)

    response = client.get("/api/health")

    assert response.status_code == 401


def test_request_with_wrong_api_key_is_rejected():
    client = TestClient(d.app)
    client.headers[_HEADER] = "not-the-real-key"

    response = client.get("/api/health")

    assert response.status_code == 401


def test_request_with_correct_api_key_succeeds():
    client = TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})

    response = client.get("/api/health")

    assert response.status_code == 200


def test_get_or_create_api_key_is_idempotent(key_file):
    first = auth.get_or_create_api_key()

    assert auth.get_or_create_api_key() == first


def test_generated_key_file_is_owner_readable_only(key_file):
    auth.get_or_create_api_key()

    mode = os.stat(key_file).st_mode

    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
