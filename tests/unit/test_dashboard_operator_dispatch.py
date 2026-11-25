"""``GET /api/operator/dispatch``: the operator-only read of the dispatch hold.

Graded here:

* the operator gets 200 with ``hold``, ``running``, ``stories`` and ``scope``;
* the running count and the story list are exact;
* an authenticated non-operator is refused with 403 and a fixed body;
* a POST is 405 (the route is read-only; the hold is written through set_setting);
* a request with no principal is refused with 403 by the handler itself;
* the route is served when the namespace resolver reaches no namespace, so the
  operator is not refused at namespace binding.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import pipeline.namespace_resolver as resolver_module
import pipeline.server  # noqa: F401  (import cycle: pipeline.server first)
from app import auth, oidc_login
from app import dashboard as d
from app import dashboard_routes_operator as route
from app.identity import Principal
from app.oidc import OidcProvider, OidcSettings
from app.plugin_loader import ALLOWLIST_ENV
from pipeline.namespace_resolver import NAMESPACE_RESOLVER_PLUGIN_ENV

_PATH = "/api/operator/dispatch"
_HEADER = "X-Pipeline-Api-Key"
_NOT_AUTHORIZED = {"detail": "not authorized: operator"}


class _StubOidcProvider(OidcProvider):
    def __init__(self) -> None:
        super().__init__(
            OidcSettings(
                issuer="https://issuer.example",
                audience="pipeline-dashboard",
                jwks_url="https://issuer.example/jwks.json",
            )
        )

    def authenticate(self, credential):  # pragma: no cover
        raise AssertionError("the stub middleware path does not authenticate directly")


class _FakeStore:
    """Only the seam the route reads: the store's records."""

    def records(self):
        return self


_SNAPSHOT = {
    "hold": True,
    "running": 2,
    "stories": [{"plan": "plan-a", "story": "S1"}, {"plan": "plan-b", "story": "S9"}],
    "scope": "all_namespaces",
}


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """A throwaway key file, created here so the test never depends on who else creates it."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    auth.get_or_create_api_key()
    return path


@pytest.fixture
def read_snapshot(monkeypatch):
    monkeypatch.setattr(route, "build_store", lambda: _FakeStore())
    monkeypatch.setattr(route, "dispatch_snapshot", lambda store: dict(_SNAPSHOT))


@pytest.fixture
def operator_client(key_file, read_snapshot):
    client = TestClient(d.app)
    client.headers[_HEADER] = key_file.read_text().strip()
    return client


@pytest.fixture
def oidc_identity(monkeypatch):
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


def test_operator_gets_the_hold_running_count_and_stories(operator_client):
    response = operator_client.get(_PATH)
    assert response.status_code == 200
    assert response.json() == _SNAPSHOT


def test_running_count_matches_the_listed_stories(read_snapshot, operator_client):
    body = operator_client.get(_PATH).json()
    assert body["running"] == len(body["stories"])


def test_admin_role_principal_gets_200(read_snapshot, oidc_identity, monkeypatch):
    from app.identity import ROLE_ADMIN

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


def test_authenticated_non_operator_is_refused_with_403(read_snapshot, oidc_identity, monkeypatch):
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


def test_post_is_not_allowed(operator_client):
    assert operator_client.post(_PATH).status_code == 405


def test_a_request_without_a_principal_is_refused_with_403():
    request = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(HTTPException) as refused:
        route.get_operator_dispatch(request)
    assert refused.value.status_code == 403
    assert refused.value.detail == _NOT_AUTHORIZED["detail"]


def test_operator_is_served_when_the_resolver_reaches_no_namespace(
    read_snapshot, key_file, tmp_path, monkeypatch
):
    """The route is served without a bound namespace, as /api/metrics is."""
    name = "operator_empty_nsr_" + uuid.uuid4().hex
    (tmp_path / f"{name}.py").write_text(
        "from pipeline.namespace_resolver import NamespaceAccess\n"
        "\n"
        "\n"
        "class StubResolver:\n"
        "    def accesses(self, principal):\n"
        "        return ()\n"
        "\n"
        "def build():\n"
        "    return StubResolver()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(resolver_module, "_PLUGIN_RESOLVERS", {})
    monkeypatch.setenv(NAMESPACE_RESOLVER_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name)
    client = TestClient(d.app)
    client.headers[_HEADER] = key_file.read_text().strip()

    response = client.get(_PATH)

    assert response.status_code == 200
    assert response.json() == _SNAPSHOT
