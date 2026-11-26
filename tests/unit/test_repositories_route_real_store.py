"""``GET /api/repositories`` must answer with the server's real store.

The route passes the store to ``repo_listing_hooks.list_repositories_result``.
``dashboard_routes._store`` is the store object itself, so the route must hand
that object over; it must not call a ``_value()`` accessor that only the lazy
server-reference proxy provides.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from app import dashboard_routes
from app.identity import ROLE_AUTHOR
from pipeline import namespace_access as access_module
from pipeline import repo_listing_hooks
from pipeline.namespace_access import NAMESPACE_HEADER
from pipeline.namespace_resolver import NamespaceAccess

NAMESPACE = "team-a"
REPO = "github.com/acme/widgets"


class _Resolver:
    def accesses(self, principal):
        return (NamespaceAccess(NAMESPACE, frozenset({ROLE_AUTHOR})),)


class _ListingHook:
    def list_repositories(self, namespace):
        return [{"repo": REPO, "level": None, "state": None}]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(access_module, "active_namespace_resolver", lambda: _Resolver())
    monkeypatch.setattr(repo_listing_hooks, "_hook", lambda: _ListingHook())
    return TestClient(d.app)


def test_should_list_repositories_when_the_server_store_is_a_plain_store(client, monkeypatch):
    monkeypatch.setattr(dashboard_routes, "_store", object())

    response = client.get("/api/repositories", headers={NAMESPACE_HEADER: NAMESPACE})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "repositories": [{"repo": REPO, "level": None, "state": None}],
    }
