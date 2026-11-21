"""Tests for fcf-01: GET /auth/logout also ends the identity provider session.

Without it the provider's SSO cookie survives, so the next ``/auth/login``
completes silently. The redirect target is derived from configuration only and
no token is ever placed in the URL.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pipeline.server  # noqa: F401  (import cycle: pipeline.server first)
from app import oidc_login
from app.identity import Principal

REDIRECT_URI = "https://dashboard.example/auth/callback"
SITE_ROOT = "https://dashboard.example/"
ISSUER = "https://issuer.example"
END_SESSION = f"{ISSUER}/protocol/openid-connect/logout"
REALM = Path(__file__).resolve().parents[2] / "deploy" / "standalone" / "realm.json"


@pytest.fixture(autouse=True)
def _clean_state():
    oidc_login.reset_state()
    yield
    oidc_login.reset_state()


@pytest.fixture
def config() -> oidc_login.OidcLoginConfig:
    return oidc_login.OidcLoginConfig(
        enabled=True,
        client_id="pipeline-dashboard",
        redirect_uri=REDIRECT_URI,
        issuer=ISSUER,
    )


def _stub_discovery(monkeypatch, endpoint):
    def discovery(_config):
        doc = {"authorization_endpoint": f"{ISSUER}/auth"}
        if endpoint is not None:
            doc["end_session_endpoint"] = endpoint
        return doc

    monkeypatch.setattr(oidc_login, "_discovery", discovery)


def _logout(config, path="/auth/logout", session_id=None):
    app = FastAPI()
    oidc_login.mount_auth_routes(app, config)
    client = TestClient(app, base_url="https://testserver")
    headers = {"Cookie": f"{oidc_login.SESSION_COOKIE}={session_id}"} if session_id else {}
    return client.get(path, headers=headers, follow_redirects=False)


def _new_session() -> str:
    return oidc_login._create_session(Principal(subject=f"{ISSUER}#bob", auth_method="oidc"))


def _query(response) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(response.headers["location"]).query)


def test_should_redirect_get_logout_to_the_end_session_endpoint_when_discovered(
    config, monkeypatch
):
    _stub_discovery(monkeypatch, END_SESSION)

    response = _logout(config)

    assert response.status_code == 302
    parts = urllib.parse.urlsplit(response.headers["location"])
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "issuer.example", "/protocol/openid-connect/logout")


def test_should_send_client_id_and_the_site_root_as_post_logout_redirect_uri(
    config, monkeypatch
):
    _stub_discovery(monkeypatch, END_SESSION)

    query = _query(_logout(config))

    assert query["client_id"] == ["pipeline-dashboard"]
    assert query["post_logout_redirect_uri"] == [SITE_ROOT]


def test_should_not_send_an_id_token_hint(config, monkeypatch):
    _stub_discovery(monkeypatch, END_SESSION)

    response = _logout(config, session_id=_new_session())

    assert "id_token_hint" not in _query(response)
    assert "id_token" not in response.headers["location"]


def test_should_keep_the_endpoints_existing_query_parameters(config, monkeypatch):
    _stub_discovery(monkeypatch, f"{END_SESSION}?tenant=acme")

    query = _query(_logout(config))

    assert query["tenant"] == ["acme"]
    assert query["client_id"] == ["pipeline-dashboard"]


def test_should_redirect_get_logout_to_root_when_no_end_session_endpoint(
    config, monkeypatch
):
    _stub_discovery(monkeypatch, None)

    response = _logout(config)

    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_should_redirect_get_logout_to_root_when_discovery_fails(config, monkeypatch):
    def discovery(_config):
        raise oidc_login.OidcLoginError("discovery document unavailable")

    monkeypatch.setattr(oidc_login, "_discovery", discovery)
    session_id = _new_session()

    response = _logout(config, session_id=session_id)

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert session_id not in oidc_login._SESSIONS


def test_should_end_the_local_session_before_redirecting_to_the_provider(
    config, monkeypatch
):
    _stub_discovery(monkeypatch, END_SESSION)
    session_id = _new_session()

    response = _logout(config, session_id=session_id)

    assert response.headers["location"].startswith(END_SESSION)
    assert session_id not in oidc_login._SESSIONS
    assert oidc_login.SESSION_COOKIE in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_should_ignore_request_supplied_redirect_targets(config, monkeypatch):
    _stub_discovery(monkeypatch, END_SESSION)
    baseline = _logout(config).headers["location"]

    response = _logout(
        config,
        path="/auth/logout?post_logout_redirect_uri=https://evil.example/&next=https://evil.example/",
    )

    assert response.headers["location"] == baseline
    assert "evil.example" not in response.headers["location"]


def test_should_list_the_site_root_as_a_post_logout_redirect_uri_in_the_standalone_realm():
    realm = json.loads(REALM.read_text())
    client = next(c for c in realm["clients"] if c["clientId"] == "pipeline-dashboard")

    uris = client["attributes"]["post.logout.redirect.uris"].split("##")

    assert "${env.PIPELINE_PUBLIC_URL}/auth/callback" in uris
    assert "${env.PIPELINE_PUBLIC_URL}/" in uris
