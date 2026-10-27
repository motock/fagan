"""Tests for OIDC-03: the dashboard authenticates a bearer token under OIDC.

When ``_identity`` is an ``OidcProvider``, the middleware authenticates the
``Authorization: Bearer <jwt>`` header and binds the resulting principal for
the service layer; when it is not, the shared-secret gate runs exactly as
before. The token verifier itself is graded in
``tests/unit/test_oidc_verify.py`` — this module grades the middleware wiring,
so the provider here is a stub, not a real JWKS client:

* a valid bearer token reaches the route *with its principal bound*
  (``actor_id()`` inside the route, not just the status code);
* every missing/malformed/rejected credential is a 401 with the same body and
  an ``X-Correlation-ID`` header;
* the two credential kinds never substitute for each other: the shared secret
  does not authenticate under OIDC, and a bearer token does not authenticate
  under the shared secret;
* the default (shared-secret) path still works untouched.
"""
import inspect

import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth
from app import dashboard as d
from app.identity import IdentityError, Principal, SharedSecretProvider
from app.oidc import OidcProvider
from pipeline.actor import actor_id

_HEADER = "X-Pipeline-Api-Key"
_PROBE_PATH = "/api/_test/oidc"

#: The one bearer token the stub accepts, and the principal it maps to.
_GOOD_TOKEN = "good"
_SUBJECT = "https://issuer.example#alice"


class _StubOidcProvider(OidcProvider):
    """An ``OidcProvider`` that accepts one token without any network I/O.

    Subclasses the real provider because the middleware's bearer branch is
    gated on ``isinstance(_identity, OidcProvider)`` — the gate itself is part
    of what these tests pin.
    """

    def __init__(self) -> None:
        self._principal = Principal(subject=_SUBJECT, auth_method="oidc")

    def authenticate(self, credential: str | None) -> Principal:
        if credential != _GOOD_TOKEN:
            raise IdentityError("invalid token")
        return self._principal


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so these tests never read or
    create this repo's real .dashboard_api_key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def oidc_identity(monkeypatch):
    """Activate the bearer branch by swapping in the stub OIDC provider."""
    provider = _StubOidcProvider()
    monkeypatch.setattr(d, "_identity", provider)
    return provider


@pytest.fixture
def probe_route():
    """Register a test-only /api/ route that reports the bound actor, then
    remove it so no other test module sees the extra route."""
    async def _probe(request: Request):
        return {"actor_id": actor_id()}

    route = APIRoute(_PROBE_PATH, _probe, methods=["GET"])
    d.app.router.routes.insert(0, route)
    try:
        yield
    finally:
        d.app.router.routes.remove(route)


# --------------------------------------------------------------------------- #
# OIDC active: the bearer token authenticates and binds a principal
# --------------------------------------------------------------------------- #

def test_valid_bearer_token_reaches_route_as_its_principal(
    key_file, oidc_identity, probe_route
):
    client = TestClient(d.app)

    response = client.get(_PROBE_PATH, headers={"Authorization": f"Bearer {_GOOD_TOKEN}"})

    assert response.json() == {"actor_id": _SUBJECT}


def test_missing_authorization_header_is_401(key_file, oidc_identity, probe_route):
    client = TestClient(d.app)

    response = client.get(_PROBE_PATH)

    assert response.status_code == 401 and response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize("authorization", ["Token good", "Bearer", "Bearer   "])
def test_malformed_authorization_is_401(
    key_file, oidc_identity, probe_route, authorization
):
    client = TestClient(d.app)

    response = client.get(_PROBE_PATH, headers={"Authorization": authorization})

    assert response.status_code == 401


def test_shared_secret_does_not_authenticate_under_oidc(
    key_file, oidc_identity, probe_route
):
    """The default credential kind must not leak into the bearer branch."""
    client = TestClient(d.app)

    response = client.get(_PROBE_PATH, headers={_HEADER: auth.get_or_create_api_key()})

    assert response.status_code == 401


def test_oidc_401_carries_correlation_id(key_file, oidc_identity, probe_route):
    client = TestClient(d.app)

    response = client.get(_PROBE_PATH)

    assert response.headers["X-Correlation-ID"]


# --------------------------------------------------------------------------- #
# Shared-secret mode (the default): unchanged, and no bearer-token back door
# --------------------------------------------------------------------------- #

def test_bearer_token_does_not_authenticate_under_shared_secret(
    key_file, probe_route
):
    assert isinstance(d._identity, SharedSecretProvider)
    client = TestClient(d.app)

    response = client.get(_PROBE_PATH, headers={"Authorization": "Bearer anything"})

    assert response.status_code == 401


def test_shared_secret_still_reaches_route(key_file, probe_route):
    client = TestClient(d.app)

    response = client.get(_PROBE_PATH, headers={_HEADER: auth.get_or_create_api_key()})

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# The provider is selected, not constructed inline
# --------------------------------------------------------------------------- #

def test_identity_is_built_by_build_identity_provider():
    assert "build_identity_provider(get_or_create_api_key)" in inspect.getsource(d)
