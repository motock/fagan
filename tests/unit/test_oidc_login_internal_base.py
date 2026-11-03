"""Server-side OIDC fetches through an in-network base URL.

The identity provider is pinned to the public origin (``KC_HOSTNAME``) so the
``iss`` claim and the browser redirect stay correct. Inside the compose
network that public name does not resolve to the proxy, so discovery and the
token exchange have to be issued against the proxy's in-network name instead.
The private-address guard is opened only by the operator opt-in the JWKS fetch
already honors.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import oidc, oidc_login
from app.identity import IdentityError

ISSUER = "https://localhost:8443/realms/pipeline"
INTERNAL = "https://proxy:8443"
PUBLIC_DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
INTERNAL_DISCOVERY = f"{INTERNAL}/realms/pipeline/.well-known/openid-configuration"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN_ENDPOINT = f"{ISSUER}/protocol/openid-connect/token"
CLIENT_ID = "pipeline-dashboard"
REDIRECT_URI = "https://localhost:8443/auth/callback"
CLIENT_SECRET = "s3cr3t-client-secret-value"


@pytest.fixture(autouse=True)
def _clean_state():
    oidc_login.reset_state()
    yield
    oidc_login.reset_state()


def _env(tmp_path, **overrides):
    secret_file = tmp_path / "oidc_client_secret"
    secret_file.write_text(CLIENT_SECRET + "\n", encoding="utf-8")
    secret_file.chmod(0o600)
    env = {
        oidc_login.ENV_CLIENT_ID: CLIENT_ID,
        oidc_login.ENV_CLIENT_SECRET_FILE: str(secret_file),
        oidc_login.ENV_REDIRECT_URI: REDIRECT_URI,
        oidc_login.ENV_ISSUER: ISSUER,
    }
    env.update(overrides)
    return env


def _config(**overrides):
    values = {
        "enabled": True,
        "client_id": CLIENT_ID,
        "client_secret_file": "/dev/null",
        "redirect_uri": REDIRECT_URI,
        "issuer": ISSUER,
    }
    values.update(overrides)
    return oidc_login.OidcLoginConfig(**values)


def _document():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": AUTHORIZATION_ENDPOINT,
        "token_endpoint": TOKEN_ENDPOINT,
        "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
        "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
    }


def test_load_config_reads_internal_base_and_private_opt_in(tmp_path):
    config = oidc_login.load_config(
        _env(
            tmp_path,
            **{
                oidc_login.ENV_INTERNAL_BASE_URL: INTERNAL + "/",
                "PIPELINE_OIDC_ALLOW_PRIVATE_JWKS": "1",
            },
        )
    )

    assert config.internal_base_url == INTERNAL
    assert config.allow_private is True


def test_load_config_rejects_a_non_https_internal_base(tmp_path):
    with pytest.raises(oidc_login.OidcLoginConfigError) as refused:
        oidc_login.load_config(
            _env(tmp_path, **{oidc_login.ENV_INTERNAL_BASE_URL: "http://proxy:8443"})
        )

    assert oidc_login.ENV_INTERNAL_BASE_URL in str(refused.value)


def test_discovery_fetches_internally_but_keeps_public_browser_endpoints(monkeypatch):
    seen = {}

    def fake_fetch(url, *, allow_private=False):
        seen["url"] = url
        seen["allow_private"] = allow_private
        return _document()

    monkeypatch.setattr(oidc_login, "_fetch_json", fake_fetch)

    endpoints = oidc_login._discovery(
        _config(internal_base_url=INTERNAL, allow_private=True)
    )

    assert seen == {"url": INTERNAL_DISCOVERY, "allow_private": True}
    assert endpoints["token_endpoint"] == f"{INTERNAL}/realms/pipeline/protocol/openid-connect/token"
    assert endpoints["jwks_uri"] == f"{INTERNAL}/realms/pipeline/protocol/openid-connect/certs"
    assert endpoints["authorization_endpoint"] == AUTHORIZATION_ENDPOINT
    assert endpoints["end_session_endpoint"].startswith(ISSUER)


def test_discovery_without_an_internal_base_keeps_the_public_origin(monkeypatch):
    seen = {}

    def fake_fetch(url, *, allow_private=False):
        seen["url"] = url
        seen["allow_private"] = allow_private
        return _document()

    monkeypatch.setattr(oidc_login, "_fetch_json", fake_fetch)

    oidc_login._discovery(_config())

    assert seen == {"url": PUBLIC_DISCOVERY, "allow_private": False}


def test_discovery_still_rejects_an_issuer_mismatch(monkeypatch):
    def fake_fetch(url, *, allow_private=False):
        document = _document()
        document["issuer"] = "https://attacker.example/realms/pipeline"
        return document

    monkeypatch.setattr(oidc_login, "_fetch_json", fake_fetch)

    with pytest.raises(oidc_login.OidcLoginError):
        oidc_login._discovery(_config(internal_base_url=INTERNAL, allow_private=True))


def test_callback_passes_the_private_opt_in_to_the_token_exchange(tmp_path, monkeypatch):
    seen = []

    def fake_fetch(url, *, allow_private=False):
        return _document()

    def fake_post(url, form, *, allow_private=False):
        seen.append((url, allow_private))
        raise oidc_login.OidcLoginError("stop after recording the call")

    monkeypatch.setattr(oidc_login, "_fetch_json", fake_fetch)
    monkeypatch.setattr(oidc_login, "_post_form", fake_post)

    secret_file = tmp_path / "oidc_client_secret"
    secret_file.write_text(CLIENT_SECRET + "\n", encoding="utf-8")
    secret_file.chmod(0o600)
    config = _config(
        client_secret_file=str(secret_file),
        internal_base_url=INTERNAL,
        allow_private=True,
    )
    app = FastAPI()
    oidc_login.mount_auth_routes(app, config)
    client = TestClient(app, base_url="https://testserver")

    login = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    response = client.get(
        "/auth/callback",
        params={"state": state, "code": "auth-code-123"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert seen == [(f"{INTERNAL}/realms/pipeline/protocol/openid-connect/token", True)]


def test_callback_verifies_against_the_internal_jwks_with_the_opt_in(tmp_path, monkeypatch):
    seen = {}

    def fake_fetch(url, *, allow_private=False):
        return _document()

    def fake_post(url, form, *, allow_private=False):
        return {"id_token": "an-id-token"}

    def fake_verify(token, settings):
        seen["token"] = token
        seen["settings"] = settings
        raise IdentityError("stop after recording the settings")

    monkeypatch.setattr(oidc_login, "_fetch_json", fake_fetch)
    monkeypatch.setattr(oidc_login, "_post_form", fake_post)
    monkeypatch.setattr(oidc, "verify_token", fake_verify)

    secret_file = tmp_path / "oidc_client_secret"
    secret_file.write_text(CLIENT_SECRET + "\n", encoding="utf-8")
    secret_file.chmod(0o600)
    config = _config(
        client_secret_file=str(secret_file),
        internal_base_url=INTERNAL,
        allow_private=True,
    )
    app = FastAPI()
    oidc_login.mount_auth_routes(app, config)
    client = TestClient(app, base_url="https://testserver")

    login = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    response = client.get(
        "/auth/callback",
        params={"state": state, "code": "auth-code-123"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert seen["token"] == "an-id-token"
    assert seen["settings"].issuer == ISSUER
    assert seen["settings"].audience == CLIENT_ID
    assert seen["settings"].jwks_url == (
        f"{INTERNAL}/realms/pipeline/protocol/openid-connect/certs"
    )
    assert seen["settings"].allow_private_jwks is True
