"""Tests for LG-02: the dashboard accepts an OIDC login session.

LG-01 minted a browser session at the end of the authorization-code flow but
nothing consumed it, so a logged-in browser still had to present the shared
API key. This module grades the middleware half of the hand-off:

* under ``PIPELINE_IDENTITY_PROVIDER=oidc`` the session cookie is a credential:
  a valid, unexpired session yields its principal to ``request.state.principal``
  and to the actor bound for the request;
* an expired (idle or absolute deadline) session is refused with the same 401
  as no credential at all;
* the cookie is an *ambient* credential, so a state-changing request that rides
  it must prove it came from this site (``Sec-Fetch-Site: same-origin`` or an
  ``Origin`` equal to the configured redirect URI's origin). A cross-site
  request is refused with 403 *before* the route runs, so it cannot mutate;
* a bearer token is an explicit credential, not ambient: it wins over the
  cookie, it needs no same-origin proof, and it never silently downgrades to
  the cookie when it is rejected;
* ``POST /auth/logout`` ends the session server-side, so replaying the old
  cookie is a 401 even though the browser may still hold it.

The token verifier is graded elsewhere; the provider here is a stub, matching
``tests/unit/test_dashboard_oidc.py``. Sessions are minted directly through the
module so these tests stay independent of the login flow.
"""

from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import pipeline.server  # noqa: F401  (import cycle: pipeline.server first)
from app import auth, oidc_login
from app import dashboard as d
from app.identity import IdentityError, Principal
from app.oidc import OidcProvider
from pipeline.actor import actor_id

_PROBE_GET = "/api/_test/session-get"
_PROBE_POST = "/api/_test/session-post"

REDIRECT_URI = "https://dashboard.example/auth/callback"
DASHBOARD_ORIGIN = "https://dashboard.example"
ISSUER = "https://issuer.example"

#: What the stub bearer token maps to; deliberately distinct from the session's
#: subject so a test can tell which credential authenticated the request.
_BEARER_TOKEN = "good"
_BEARER_SUBJECT = "https://issuer.example#alice"
_SESSION_SUBJECT = "https://issuer.example#bob"


class _StubOidcProvider(OidcProvider):
    """An ``OidcProvider`` that accepts one token with no network I/O."""

    def __init__(self) -> None:
        self._principal = Principal(subject=_BEARER_SUBJECT, auth_method="oidc")

    def authenticate(self, credential: str | None) -> Principal:
        if credential != _BEARER_TOKEN:
            raise IdentityError("invalid token")
        return self._principal


class _Clock:
    """The single time seam in ``app.oidc_login``."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _clean_state():
    oidc_login.reset_state()
    yield
    oidc_login.reset_state()


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Keep these tests away from this repo's real .dashboard_api_key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(oidc_login, "_now", fake)
    return fake


@pytest.fixture
def oidc_identity(monkeypatch):
    """Select the OIDC provider and a login configuration with an origin."""
    monkeypatch.setattr(d, "_identity", _StubOidcProvider())
    monkeypatch.setattr(
        d,
        "_oidc_login_config",
        oidc_login.OidcLoginConfig(
            enabled=True,
            client_id="pipeline-dashboard",
            redirect_uri=REDIRECT_URI,
            issuer=ISSUER,
        ),
    )


@pytest.fixture
def probe_routes():
    """Test-only /api/ routes: report the bound actor, count POST mutations."""
    mutations: list[int] = []

    async def _get(request: Request):
        return {"actor_id": actor_id()}

    async def _post(request: Request):
        mutations.append(1)
        return {"actor_id": actor_id()}

    routes = [
        APIRoute(_PROBE_GET, _get, methods=["GET"]),
        APIRoute(_PROBE_POST, _post, methods=["POST"]),
    ]
    for route in routes:
        d.app.router.routes.insert(0, route)
    try:
        yield mutations
    finally:
        for route in routes:
            d.app.router.routes.remove(route)


def _new_session(principal: Principal | None = None) -> str:
    return oidc_login._create_session(
        principal or Principal(subject=_SESSION_SUBJECT, auth_method="oidc")
    )


def _cookie_header(session_id: str) -> dict[str, str]:
    return {"Cookie": f"{oidc_login.SESSION_COOKIE}={session_id}"}


# --------------------------------------------------------------------------- #
# The session cookie is a credential
# --------------------------------------------------------------------------- #


def test_session_cookie_principal_reaches_state_and_bound_actor(
    key_file, oidc_identity, probe_routes
):
    session_id = _new_session()

    response = TestClient(d.app).get(_PROBE_GET, headers=_cookie_header(session_id))

    assert response.status_code == 200
    assert response.json() == {"actor_id": _SESSION_SUBJECT}


def test_session_cookie_without_login_configuration_is_401(
    key_file, oidc_identity, probe_routes, monkeypatch
):
    """Deny by default: a cookie nobody can verify is not a credential."""
    monkeypatch.setattr(d, "_oidc_login_config", oidc_login.OidcLoginConfig())
    session_id = _new_session()

    response = TestClient(d.app).get(_PROBE_GET, headers=_cookie_header(session_id))

    assert response.status_code == 401


def test_unknown_session_cookie_is_401(key_file, oidc_identity, probe_routes):
    response = TestClient(d.app).get(
        _PROBE_GET, headers=_cookie_header("not-a-real-session")
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_idle_session_is_401(key_file, oidc_identity, probe_routes, clock):
    session_id = _new_session()
    clock.now += oidc_login.SESSION_IDLE_TIMEOUT_S

    response = TestClient(d.app).get(_PROBE_GET, headers=_cookie_header(session_id))

    assert response.status_code == 401


def test_absolutely_expired_session_is_401(key_file, oidc_identity, probe_routes, clock):
    """Activity inside the idle window must not extend the hard deadline."""
    session_id = _new_session()
    for _ in range(oidc_login.SESSION_ABSOLUTE_TIMEOUT_S // 600 + 1):
        clock.now += 600
        oidc_login.load_session(session_id)

    response = TestClient(d.app).get(_PROBE_GET, headers=_cookie_header(session_id))

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# CSRF: the cookie may only drive a write from this site
# --------------------------------------------------------------------------- #


def test_cross_site_post_with_session_cookie_is_403_and_does_not_mutate(
    key_file, oidc_identity, probe_routes
):
    session_id = _new_session()

    response = TestClient(d.app).post(
        _PROBE_POST,
        headers={**_cookie_header(session_id), "Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert probe_routes == [], "a refused request must not reach the route"


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site", "Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "https://dashboard.example.evil.test"},
    ],
)
def test_cookie_write_without_a_same_origin_proof_is_403(
    key_file, oidc_identity, probe_routes, headers
):
    session_id = _new_session()

    response = TestClient(d.app).post(
        _PROBE_POST, headers={**_cookie_header(session_id), **headers}
    )

    assert response.status_code == 403
    assert probe_routes == []


_WRITE_METHODS = ("PUT", "PATCH", "DELETE")


@pytest.fixture
def method_probe_routes():
    """Test-only /api/ routes for every write method the CSRF rule covers."""
    hits: list[str] = []
    paths = {method: f"/api/_test/session-{method.lower()}" for method in _WRITE_METHODS}

    def _make_handler(route_path: str):
        async def _handler(request: Request):
            hits.append(route_path)
            return {"actor_id": actor_id()}

        return _handler

    routes = [
        APIRoute(path, _make_handler(path), methods=[method])
        for method, path in paths.items()
    ]
    for route in routes:
        d.app.router.routes.insert(0, route)
    try:
        yield paths, hits
    finally:
        for route in routes:
            d.app.router.routes.remove(route)


@pytest.mark.parametrize("method", _WRITE_METHODS)
def test_cross_site_write_with_session_cookie_is_403_for_every_write_method(
    key_file, oidc_identity, method_probe_routes, method
):
    """POST is not special: PUT, PATCH and DELETE need the same proof."""
    paths, hits = method_probe_routes
    session_id = _new_session()

    response = TestClient(d.app).request(
        method,
        paths[method],
        headers={**_cookie_header(session_id), "Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert hits == [], "a refused request must not reach the route"


@pytest.mark.parametrize("method", _WRITE_METHODS)
def test_same_origin_write_with_session_cookie_is_allowed_for_every_write_method(
    key_file, oidc_identity, method_probe_routes, method
):
    paths, hits = method_probe_routes
    session_id = _new_session()

    response = TestClient(d.app).request(
        method,
        paths[method],
        headers={**_cookie_header(session_id), "Origin": DASHBOARD_ORIGIN},
    )

    assert response.status_code == 200
    assert response.json() == {"actor_id": _SESSION_SUBJECT}
    assert hits == [paths[method]]


def test_cookie_write_with_no_origin_and_no_fetch_metadata_is_403(
    key_file, oidc_identity, probe_routes
):
    session_id = _new_session()

    response = TestClient(d.app).post(_PROBE_POST, headers=_cookie_header(session_id))

    assert response.status_code == 403
    assert probe_routes == []


def test_same_origin_post_with_session_cookie_is_allowed(
    key_file, oidc_identity, probe_routes
):
    session_id = _new_session()

    response = TestClient(d.app).post(
        _PROBE_POST,
        headers={**_cookie_header(session_id), "Origin": DASHBOARD_ORIGIN},
    )

    assert response.status_code == 200
    assert response.json() == {"actor_id": _SESSION_SUBJECT}
    assert probe_routes == [1]


def test_sec_fetch_site_same_origin_post_with_session_cookie_is_allowed(
    key_file, oidc_identity, probe_routes
):
    session_id = _new_session()

    response = TestClient(d.app).post(
        _PROBE_POST,
        headers={**_cookie_header(session_id), "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 200
    assert probe_routes == [1]


def test_session_cookie_reads_are_not_subject_to_the_origin_check(
    key_file, oidc_identity, probe_routes
):
    """GET has no side effect to forge, so it needs no same-origin proof."""
    session_id = _new_session()

    response = TestClient(d.app).get(
        _PROBE_GET,
        headers={**_cookie_header(session_id), "Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Bearer tokens are unaffected
# --------------------------------------------------------------------------- #


def test_bearer_write_is_exempt_from_the_origin_check(
    key_file, oidc_identity, probe_routes
):
    response = TestClient(d.app).post(
        _PROBE_POST, headers={"Authorization": f"Bearer {_BEARER_TOKEN}"}
    )

    assert response.status_code == 200
    assert response.json() == {"actor_id": _BEARER_SUBJECT}
    assert probe_routes == [1]


def test_bearer_token_wins_over_a_session_cookie(key_file, oidc_identity, probe_routes):
    session_id = _new_session()

    response = TestClient(d.app).get(
        _PROBE_GET,
        headers={
            **_cookie_header(session_id),
            "Authorization": f"Bearer {_BEARER_TOKEN}",
        },
    )

    assert response.json() == {"actor_id": _BEARER_SUBJECT}


def test_rejected_bearer_does_not_fall_back_to_a_valid_session(
    key_file, oidc_identity, probe_routes
):
    """An explicit credential that fails must not degrade into an ambient one."""
    session_id = _new_session()

    response = TestClient(d.app).get(
        _PROBE_GET,
        headers={**_cookie_header(session_id), "Authorization": "Bearer wrong"},
    )

    assert response.status_code == 401


def test_bearer_token_still_authenticates_without_a_cookie(
    key_file, oidc_identity, probe_routes
):
    response = TestClient(d.app).get(
        _PROBE_GET, headers={"Authorization": f"Bearer {_BEARER_TOKEN}"}
    )

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# POST /auth/logout
# --------------------------------------------------------------------------- #


def _mount_auth(config: oidc_login.OidcLoginConfig):
    """Mount the /auth routes on a bare app so the logout route is reachable."""
    app = _bare_app()
    oidc_login.mount_auth_routes(app, config)
    return app


def _bare_app():
    from fastapi import FastAPI

    return FastAPI()


@pytest.fixture
def auth_config(oidc_identity) -> oidc_login.OidcLoginConfig:
    return oidc_login.OidcLoginConfig(
        enabled=True,
        client_id="pipeline-dashboard",
        redirect_uri=REDIRECT_URI,
        issuer=ISSUER,
    )


def test_logout_post_invalidates_the_session_server_side(auth_config):
    session_id = _new_session()
    client = TestClient(_mount_auth(auth_config), base_url="https://testserver")

    response = client.post("/auth/logout", headers=_cookie_header(session_id))

    assert response.status_code == 200
    assert oidc_login.load_session(session_id) is None


def test_logout_post_clears_the_session_cookie(auth_config):
    session_id = _new_session()
    client = TestClient(_mount_auth(auth_config), base_url="https://testserver")

    response = client.post("/auth/logout", headers=_cookie_header(session_id))

    cleared = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{oidc_login.SESSION_COOKIE}=")
    ]
    assert cleared, "logout must clear the session cookie"
    assert cleared[0].split(";", 1)[0].split("=", 1)[1].strip('"') == ""


def test_old_cookie_is_401_after_logout(key_file, oidc_identity, probe_routes, auth_config):
    session_id = _new_session()
    logout_client = TestClient(
        _mount_auth(auth_config), base_url="https://testserver"
    )
    logout_client.post("/auth/logout", headers=_cookie_header(session_id))

    response = TestClient(d.app).get(_PROBE_GET, headers=_cookie_header(session_id))

    assert response.status_code == 401


def test_logout_returns_the_end_session_url_when_discovered(
    auth_config, monkeypatch
):
    def discovery(config):
        return {
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
            "end_session_endpoint": f"{ISSUER}/logout",
        }

    monkeypatch.setattr(oidc_login, "_discovery", discovery)
    session_id = _new_session()
    client = TestClient(_mount_auth(auth_config), base_url="https://testserver")

    response = client.post("/auth/logout", headers=_cookie_header(session_id))

    assert response.json() == {"end_session_url": f"{ISSUER}/logout"}


def test_logout_omits_the_end_session_url_when_not_discovered(
    auth_config, monkeypatch
):
    def discovery(config):
        return {
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
        }

    monkeypatch.setattr(oidc_login, "_discovery", discovery)
    session_id = _new_session()
    client = TestClient(_mount_auth(auth_config), base_url="https://testserver")

    response = client.post("/auth/logout", headers=_cookie_header(session_id))

    assert response.json() == {"end_session_url": None}


def test_logout_succeeds_when_discovery_is_unavailable(auth_config, monkeypatch):
    def discovery(config):
        raise oidc_login.OidcLoginError("discovery document unavailable")

    monkeypatch.setattr(oidc_login, "_discovery", discovery)
    session_id = _new_session()
    client = TestClient(_mount_auth(auth_config), base_url="https://testserver")

    response = client.post("/auth/logout", headers=_cookie_header(session_id))

    assert response.status_code == 200
    assert response.json() == {"end_session_url": None}
    assert oidc_login.load_session(session_id) is None
