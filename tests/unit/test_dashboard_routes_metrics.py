"""Tests for ``GET /api/metrics``: operator-only Prometheus text.

The route reuses the dashboard's existing operator gate (the local operator
principal, or a principal holding ``ROLE_ADMIN``) rather than inventing a
second one, and serves the exposition the pure renderer produces.

Graded here:

* the operator gets 200 and the Prometheus ``text/plain; version=0.0.4``
  content type;
* an authenticated non-admin is refused with 403 and a fixed body;
* OIDC still refuses a request with no credential at all (the middleware's
  answer, not a route special case);
* the route is read-only: a POST is 405;
* the route is in the committed OpenAPI snapshot, so the contract stays aware
  of it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pipeline.server  # noqa: F401  (import cycle: pipeline.server first)
from app import auth, oidc_login
from app import dashboard as d
from app.dashboard_routes_metrics import router
from app.identity import ROLE_ADMIN, Principal
from app.oidc import OidcProvider, OidcSettings

_PATH = "/api/metrics"
_HEADER = "X-Pipeline-Api-Key"
#: What the renderer produces in these tests; the route must serve it verbatim.
_BODY = "# HELP fagan_outbox_records Number of records waiting.\n"
_NOT_AUTHORIZED = {"detail": "not authorized: operator"}
_REPO_ROOT = Path(__file__).resolve().parents[2]


class _StubOidcProvider(OidcProvider):
    """An ``OidcProvider`` marker with no network I/O; the middleware reads it
    only to decide it is in OIDC mode."""

    def __init__(self) -> None:
        super().__init__(
            OidcSettings(
                issuer="https://issuer.example",
                audience="pipeline-dashboard",
                jwks_url="https://issuer.example/jwks.json",
            )
        )

    def authenticate(self, credential: str | None) -> Principal:  # pragma: no cover
        raise AssertionError("the stub middleware path does not authenticate directly")


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point ``app.auth`` at a throwaway key file, never this repo's real one."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def operator_client(key_file, monkeypatch):
    """A client presenting the shared-secret key: the local operator."""
    monkeypatch.setattr("app.dashboard_routes_metrics.collect", dict)
    monkeypatch.setattr("app.dashboard_routes_metrics.render", lambda snapshot: _BODY)
    client = TestClient(d.app)
    client.headers[_HEADER] = key_file.read_text().strip()
    return client


@pytest.fixture
def oidc_identity(monkeypatch):
    """Switch the dashboard middleware into OIDC mode."""
    monkeypatch.setattr(d, "_identity", _StubOidcProvider())
    monkeypatch.setattr(
        d,
        "_oidc_login_config",
        oidc_login.OidcLoginConfig(
            enabled=True,
            client_id="pipeline-dashboard",
            redirect_uri="https://dashboard.example/auth/callback",
            issuer="https://issuer.example",
        ),
    )


@pytest.fixture(autouse=True)
def _clean_oidc_state():
    oidc_login.reset_state()
    yield
    oidc_login.reset_state()


def test_operator_gets_200_and_prometheus_content_type(operator_client) -> None:
    response = operator_client.get(_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")


def test_operator_gets_the_rendered_exposition(operator_client) -> None:
    assert operator_client.get(_PATH).text == _BODY


def test_admin_role_principal_gets_200(key_file, oidc_identity, monkeypatch) -> None:
    monkeypatch.setattr("app.dashboard_routes_metrics.collect", dict)
    monkeypatch.setattr("app.dashboard_routes_metrics.render", lambda snapshot: _BODY)
    monkeypatch.setattr(
        oidc_login,
        "authenticate_api_request",
        lambda request, identity, config: Principal(
            subject="https://issuer.example#carol",
            roles=frozenset({ROLE_ADMIN}),
            auth_method="oidc",
        ),
    )
    response = TestClient(d.app).get(_PATH, headers={"Authorization": "Bearer good"})
    assert response.status_code == 200


def test_authenticated_non_admin_is_refused_with_a_fixed_body(
    key_file, oidc_identity, monkeypatch
) -> None:
    monkeypatch.setattr(
        oidc_login,
        "authenticate_api_request",
        lambda request, identity, config: Principal(
            subject="https://issuer.example#mallory",
            roles=frozenset(),
            auth_method="oidc",
        ),
    )
    response = TestClient(d.app).get(_PATH, headers={"Authorization": "Bearer good"})
    assert response.status_code == 403
    assert response.json() == _NOT_AUTHORIZED


def test_missing_credential_is_refused_under_oidc(
    key_file, oidc_identity, monkeypatch
) -> None:
    def _refuse(request, identity, config):
        raise oidc_login.SessionRefused(401, "authentication required")

    monkeypatch.setattr(oidc_login, "authenticate_api_request", _refuse)
    response = TestClient(d.app).get(_PATH)
    assert response.status_code == 401


def test_missing_credential_is_refused_under_the_shared_secret(operator_client) -> None:
    del operator_client.headers[_HEADER]
    assert operator_client.get(_PATH).status_code == 401


def test_post_is_not_allowed(operator_client) -> None:
    assert operator_client.post(_PATH).status_code == 405


def test_the_router_declares_the_route_under_the_api_prefix() -> None:
    assert any(route.path == "/metrics" for route in router.routes)


def test_the_route_is_in_the_committed_openapi_snapshot() -> None:
    snapshot = json.loads((_REPO_ROOT / "docs/api/openapi.json").read_text())
    assert "/api/metrics" in snapshot["paths"]


def test_the_route_serves_a_get_operation_in_the_snapshot() -> None:
    snapshot = json.loads((_REPO_ROOT / "docs/api/openapi.json").read_text())
    assert "get" in snapshot["paths"]["/api/metrics"]
