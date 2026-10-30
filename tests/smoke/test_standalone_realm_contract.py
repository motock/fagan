"""Contract tests for the standalone stack's Keycloak realm template.

``deploy/standalone/realm.json`` is the realm that ``--import-realm`` loads for
the standalone (non-smoke) stack: PKCE for the human-facing clients, no direct
grant anywhere, and no literal secret -- every secret is a ``${env.NAME}``
placeholder that Keycloak substitutes at import time.

Per-user confidential clients for long-lived-credential mode are created by the
installer through the Keycloak admin API at runtime, so they are intentionally
absent from this template.
"""

import json
import re
from pathlib import Path

REALM = Path(__file__).resolve().parents[2] / "deploy/standalone/realm.json"
SECRET_RE = re.compile(r"^\$\{env\.[A-Z0-9_]+\}$")
AUDIENCE = "${env.PIPELINE_OIDC_AUDIENCE}"


def _raw():
    """The template's text, asserted to exist before it is read."""
    assert REALM.is_file(), f"missing standalone realm template: {REALM}"
    return REALM.read_text(encoding="utf-8")


def _realm():
    return json.loads(_raw())


def _clients(realm):
    return realm["clients"]


def _by_id(realm, client_id):
    matches = [c for c in _clients(realm) if c.get("clientId") == client_id]
    assert len(matches) == 1, f"expected exactly one {client_id} client"
    return matches[0]


def test_realm_is_valid_json_without_users():
    realm = _realm()
    assert isinstance(realm, dict)
    assert realm["realm"] == "pipeline"
    # Key-presence only: ``{"users": []}`` must fail, so ``not
    # realm.get("users")`` would be too weak.
    assert "users" not in realm


def test_realm_login_policy_and_token_lifespan():
    realm = _realm()
    assert realm["sslRequired"] == "external"
    assert realm["registrationAllowed"] is False
    assert realm["accessTokenLifespan"] == 900
    # The number must be a number: placeholders only work in strings, so a
    # stringified "900" (or a ${env.*} placeholder) has to fail.
    assert isinstance(realm["accessTokenLifespan"], int)


def test_no_client_allows_direct_access_grants():
    for client in _clients(_realm()):
        assert client.get("directAccessGrantsEnabled") is not True, client["clientId"]


def test_every_client_secret_is_an_env_placeholder():
    for client in _clients(_realm()):
        if "secret" in client:
            assert SECRET_RE.match(client["secret"]), client["clientId"]


def test_exactly_the_confidential_clients_carry_secrets():
    realm = _realm()
    with_secret = sorted(c["clientId"] for c in _clients(realm) if "secret" in c)
    assert with_secret == ["pipeline-dashboard", "pipeline-mcp"]
    assert "secret" not in _by_id(realm, "pipeline-cli")


def test_every_client_requests_the_pipeline_audience():
    for client in _clients(_realm()):
        configs = [
            mapper.get("config", {})
            for mapper in client.get("protocolMappers", [])
            if mapper.get("protocolMapper") == "oidc-audience-mapper"
        ]
        assert any(AUDIENCE in config.values() for config in configs), client["clientId"]


def test_pipeline_cli_is_a_public_pkce_client():
    cli = _by_id(_realm(), "pipeline-cli")
    assert cli["publicClient"] is True
    assert cli["standardFlowEnabled"] is True
    assert cli["directAccessGrantsEnabled"] is False
    assert cli["implicitFlowEnabled"] is False
    assert cli["attributes"]["pkce.code.challenge.method"] == "S256"
    assert cli["redirectUris"] == [
        "http://localhost:${env.PIPELINE_CLI_CALLBACK_PORT}/callback",
        "http://127.0.0.1:19876/mcp/oauth/callback",
    ]
    assert all("*" not in uri for uri in cli["redirectUris"])


def test_dashboard_client_uses_the_public_url():
    dashboard = _by_id(_realm(), "pipeline-dashboard")
    assert dashboard["publicClient"] is False
    assert dashboard["standardFlowEnabled"] is True
    assert dashboard["secret"] == "${env.PIPELINE_DASHBOARD_CLIENT_SECRET}"
    assert dashboard["redirectUris"] == ["${env.PIPELINE_PUBLIC_URL}/auth/callback"]
    assert dashboard["webOrigins"] == ["${env.PIPELINE_PUBLIC_URL}"]


def test_no_smoke_leftovers_in_the_template():
    assert "smoke" not in _raw().lower()
