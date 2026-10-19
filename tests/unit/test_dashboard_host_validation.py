"""Tests for the dashboard's Host-header validation (SEC-3).

The index route injects the API key into the page, so a DNS-rebinding page
must not be able to read it under an attacker-chosen hostname. The middleware
must therefore reject any Host header that names neither the bound ASGI
server host nor a loopback alias, and it must do so before the `/api/` branch
runs (so an attacker cannot even probe the API under a foreign hostname).
"""
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


# --------------------------------------------------------------------------- #
# Unit: host_is_allowed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "host_header, server_host, expected",
    [
        ("127.0.0.1", "127.0.0.1", True),
        ("localhost:8000", "127.0.0.1", True),
        ("[::1]:8000", "127.0.0.1", True),
        ("evil.example", "127.0.0.1", False),
        ("127.0.0.1.evil.example", "127.0.0.1", False),
        ("", "127.0.0.1", False),
        ("testserver", "testserver", True),
    ],
)
def test_host_is_allowed(host_header, server_host, expected):
    assert auth.host_is_allowed(host_header, server_host) is expected


@pytest.mark.parametrize(
    "host_header, server_host, expected",
    [
        # Missing header, missing server scope, and loopback-only fallback.
        (None, "127.0.0.1", False),
        ("127.0.0.1", None, True),
        ("evil.example", None, False),
        # A wildcard is never trusted.
        ("*", "127.0.0.1", False),
    ],
)
def test_host_is_allowed_edge_cases(host_header, server_host, expected):
    assert auth.host_is_allowed(host_header, server_host) is expected


def test_host_is_allowed_does_not_leak_state_between_calls():
    # A host that legitimately names its own server must not stay trusted
    # for a later call whose server host is different.
    assert auth.host_is_allowed("evil.example", "evil.example") is True

    assert auth.host_is_allowed("evil.example", "127.0.0.1") is False


# --------------------------------------------------------------------------- #
# HTTP: the middleware rejects a foreign Host header
# --------------------------------------------------------------------------- #


def test_index_rejects_foreign_host_header(key_file):
    client = TestClient(d.app)

    response = client.get("/", headers={"host": "evil.example"})

    assert response.status_code == 403
    assert auth.get_or_create_api_key() not in response.text


def test_index_allows_loopback_host_header(key_file):
    client = TestClient(d.app)

    response = client.get("/", headers={"host": "127.0.0.1"})

    assert response.status_code == 200


def test_index_allows_default_testclient_host(key_file):
    # TestClient's default host is "testserver", which is also the ASGI
    # server host, so existing behaviour is preserved.
    client = TestClient(d.app)

    response = client.get("/")

    assert response.status_code == 200


def test_api_route_rejects_foreign_host_header_even_with_correct_key(key_file):
    client = TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})

    response = client.get("/api/health", headers={"host": "evil.example"})

    assert response.status_code == 403
