"""Tests for ``PIPELINE_OIDC_IDP_HINT``: the operator-chosen identity provider.

When the variable names a Keycloak identity-provider alias, ``/auth/login``
adds ``kc_idp_hint`` to the authorization redirect so the browser goes straight
to that provider. The value is operator configuration: it is validated at load,
and it is never read from the request.

Nothing here touches the network: the routes' discovery seam is replaced, as in
``test_oidc_login_next.py``.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import oidc_login

ISSUER = "https://issuer.example"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/authorize"
HINT_ENV = "PIPELINE_OIDC_IDP_HINT"


def _config(*, enabled: bool = True, idp_hint: str = "") -> oidc_login.OidcLoginConfig:
    """A login-enabled config. No secret file is read by these routes."""
    return oidc_login.OidcLoginConfig(
        enabled=enabled,
        client_id="pipeline-dashboard",
        client_secret_file="/nonexistent/client-secret",
        redirect_uri="https://dashboard.example/auth/callback",
        issuer=ISSUER,
        idp_hint=idp_hint,
    )


def _app(config: oidc_login.OidcLoginConfig) -> FastAPI:
    app = FastAPI()
    oidc_login.mount_auth_routes(app, config)
    return app


def _location_query(response) -> dict[str, list[str]]:
    """The query parameters of the login redirect's Location URL."""
    return parse_qs(urlsplit(response.headers["location"]).query)


@pytest.fixture
def secret_file(tmp_path):
    """A readable, owner-only client secret, as ``load_config`` requires."""
    path = tmp_path / "client-secret"
    path.write_text("s3cret\n", encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def _base_env(secret_file: str, **extra: str) -> dict[str, str]:
    """A complete login environment, plus any extra variables."""
    env = {
        "PIPELINE_OIDC_CLIENT_ID": "pipeline-dashboard",
        "PIPELINE_OIDC_CLIENT_SECRET_FILE": secret_file,
        "PIPELINE_OIDC_REDIRECT_URI": "https://dashboard.example/auth/callback",
        "PIPELINE_OIDC_ISSUER": ISSUER,
    }
    env.update(extra)
    return env


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


def test_should_add_kc_idp_hint_when_configured(login_ready) -> None:
    client = TestClient(_app(_config(idp_hint="google")), base_url="https://testserver")

    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 302
    query = _location_query(response)
    assert query["kc_idp_hint"] == ["google"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] and query["nonce"]


def test_should_omit_kc_idp_hint_when_unset(login_ready) -> None:
    client = TestClient(_app(_config()), base_url="https://testserver")

    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 302
    assert "kc_idp_hint" not in _location_query(response)


def test_should_ignore_an_idp_hint_in_the_request_query(login_ready) -> None:
    unset = TestClient(_app(_config()), base_url="https://testserver")
    hostile = unset.get(
        "/auth/login",
        params={"kc_idp_hint": "evil", "idp": "evil"},
        follow_redirects=False,
    )
    assert "kc_idp_hint" not in _location_query(hostile)

    configured = TestClient(_app(_config(idp_hint="google")), base_url="https://testserver")
    hostile_with_hint = configured.get(
        "/auth/login",
        params={"kc_idp_hint": "evil", "idp": "evil"},
        follow_redirects=False,
    )
    assert _location_query(hostile_with_hint)["kc_idp_hint"] == ["google"]


@pytest.mark.parametrize(
    "raw",
    [
        # Keycloak aliases are lowercase; an uppercase value is a different string.
        "Google",
        # Whitespace or a path separator could corrupt the redirect or name another route.
        "bad hint",
        "a/b",
        # One character past the 64-character limit.
        "a" * 65,
        # The first character must be alphanumeric.
        "-lead",
    ],
)
def test_should_refuse_a_malformed_idp_hint(secret_file, raw: str) -> None:
    with pytest.raises(oidc_login.OidcLoginConfigError) as raised:
        oidc_login.load_config(_base_env(secret_file, **{HINT_ENV: raw}))

    message = str(raised.value)
    assert HINT_ENV in message
    assert raw not in message


def test_should_accept_a_64_character_idp_hint(secret_file) -> None:
    value = "a" * 64

    config = oidc_login.load_config(_base_env(secret_file, **{HINT_ENV: value}))

    assert config.idp_hint == value


def test_should_treat_a_whitespace_only_idp_hint_as_unset(secret_file) -> None:
    config = oidc_login.load_config(_base_env(secret_file, **{HINT_ENV: "   "}))

    assert config.idp_hint == ""


def test_should_ignore_an_idp_hint_when_login_is_not_configured() -> None:
    config = oidc_login.load_config({HINT_ENV: "google"})

    assert config.enabled is False
    assert config.idp_hint == ""
