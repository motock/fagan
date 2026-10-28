"""Tests for the post-login ``next`` parameter and the ``/auth/status`` probe.

The browser UI needs two things the login routes did not offer on their own: a
way to ask "is login configured here?" without authenticating (``GET
/auth/status``), and a way to come back to the page the user was on after a
``401``-triggered login (``/auth/login?next=``). The ``next`` value is
attacker-influenced, so it is validated as a same-origin absolute path before it
is stored, and it is the *stored* value that the callback redirects to.

Nothing here touches the network: the routes' discovery seam is replaced, and
``/auth/status`` never calls it at all.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import oidc_login, oidc_login_routes

ISSUER = "https://issuer.example"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/authorize"


def _validate(raw: str) -> str:
    """The routes' own ``next`` validator, reached through the module so that a
    missing name is a test failure rather than a collection error."""
    return oidc_login_routes.validate_next_path(raw)


def _config(*, enabled: bool = True) -> oidc_login.OidcLoginConfig:
    """A login-enabled config. No secret file is read by these routes."""
    return oidc_login.OidcLoginConfig(
        enabled=enabled,
        client_id="pipeline-dashboard",
        client_secret_file="/nonexistent/client-secret",
        redirect_uri="https://dashboard.example/auth/callback",
        issuer=ISSUER,
    )


def _app(config: oidc_login.OidcLoginConfig) -> FastAPI:
    app = FastAPI()
    oidc_login.mount_auth_routes(app, config)
    return app


def _state_of(response) -> str:
    """The ``state`` the login redirect minted, taken from its Location URL."""
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query["state"][0]


@pytest.mark.parametrize(
    "raw",
    [
        "/plans/x",
        "/plans/x?tab=2",
        "/",
        "/a/b/../c",
    ],
)
def test_valid_next_path_is_returned_unchanged(raw: str) -> None:
    assert _validate(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        # Protocol-relative: the browser resolves this to https://evil.example.
        "//evil.example",
        # An absolute URL is never a same-origin path.
        "https://evil.example",
        # Browsers normalise a backslash to a slash, so this is "//evil.example".
        "/\\evil.example",
        "javascript:alert(1)",
        "",
        # A control character could split a header or smuggle a second value.
        "/plans\nx",
        # A relative path (no leading slash) would be resolved against the
        # callback URL, not the dashboard root.
        "plans/x",
    ],
)
def test_invalid_next_path_falls_back_to_root(raw: str) -> None:
    assert _validate(raw) == "/"


def test_next_defaults_to_root_on_a_pending_login() -> None:
    pending = oidc_login._PendingLogin(nonce="n", code_verifier="v", expires_at=1.0)
    assert pending.next == "/"


def test_auth_status_reports_login_when_mounted() -> None:
    client = TestClient(_app(_config()))
    # No cookie and no Authorization header: the probe must answer logged-out
    # browsers, which is the whole point of the endpoint.
    response = client.get("/auth/status")
    assert response.status_code == 200
    assert response.json() == {"login": True}


def test_auth_status_is_absent_when_login_is_not_configured() -> None:
    client = TestClient(_app(_config(enabled=False)))
    assert client.get("/auth/status").status_code == 404


@pytest.fixture
def login_ready(monkeypatch):
    """Point discovery at a fixed authorization endpoint and start from an
    empty pending store, so each login attempt is observable on its own."""
    monkeypatch.setattr(oidc_login, "_rate_limited", lambda *_a, **_k: False)
    monkeypatch.setattr(oidc_login, "_now", lambda: 0.0)
    monkeypatch.setattr(
        oidc_login,
        "_discovery",
        lambda _config: {"authorization_endpoint": AUTHORIZATION_ENDPOINT},
    )
    oidc_login._PENDING.clear()
    yield
    oidc_login._PENDING.clear()


def test_login_stores_the_validated_next(login_ready) -> None:
    """The stored value is what the callback redirects to, so it must be the
    validated one - not the raw query value that arrived here."""
    client = TestClient(_app(_config()), base_url="https://testserver")

    valid = client.get(
        "/auth/login", params={"next": "/plans/x?tab=2"}, follow_redirects=False
    )
    assert valid.status_code == 302
    assert oidc_login._PENDING[_state_of(valid)].next == "/plans/x?tab=2"

    # Browsers would resolve this to https://evil.example; the stored value
    # must already be the safe fallback.
    rejected = client.get(
        "/auth/login", params={"next": "//evil.example"}, follow_redirects=False
    )
    assert rejected.status_code == 302
    assert oidc_login._PENDING[_state_of(rejected)].next == "/"


def test_login_without_next_stores_root(login_ready) -> None:
    client = TestClient(_app(_config()), base_url="https://testserver")
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 302
    assert oidc_login._PENDING[_state_of(response)].next == "/"
