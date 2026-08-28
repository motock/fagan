"""Tests for the GET /api/workspaces list route on app/dashboard.py.

This story adds ONE read-side route to the EXISTING ``app/dashboard.py``,
mirroring the read-side precedent ``GET /api/config/providers``
(app/dashboard.py:492). It needs NO new request model -- a GET takes no
body -- so ``app/dashboard_models.py`` must NOT be touched.

The route, in the exact shape the story brief specifies:

    @app.get("/api/workspaces")
    def list_workspaces_route():
        return {"workspaces": _service.list_workspaces()}

Contract graded here (with the module-level ``_service`` monkeypatched to a
fake, so no real PipelineService or filesystem is involved):

  * GET /api/workspaces is registered on the dashboard app as a GET (not a
    POST), implemented by a module-level function named
    ``list_workspaces_route`` living in ``app.dashboard`` (the EXISTING
    dashboard module, not a new one), whose signature accepts no parameters
    (no request body, hence no new request model).
  * 200 with the service's list under a ``workspaces`` key.
  * An empty service list yields 200 with ``{"workspaces": []}`` -- not an
    error, not 404.
  * Entries pass through unmodified, including entries with
    ``exists=False`` / ``valid=False``.
  * The route adds no filtering or sorting of its own: the response list
    equals the service's list exactly (same order, same contents), and the
    service is called exactly once, with no arguments.

These tests describe behavior for code that does not exist yet on this
branch and must fail (404 / AttributeError / AssertionError) until
app/dashboard.py gains the route.
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d

WORKSPACES_PATH = "/api/workspaces"


class FakeWorkspaceService:
    """Minimal stand-in for pipeline.server.PipelineService.

    Records every ``list_workspaces()`` call (args + kwargs) and returns a
    canned list, so tests can assert both the delegation contract (called
    exactly once, with no arguments) and the pass-through contract (the
    response body is exactly the service's list).
    """

    def __init__(self, workspaces: list[dict[str, Any]] | None = None) -> None:
        self.workspaces: list[dict[str, Any]] = list(workspaces or [])
        self.calls: list[dict[str, Any]] = []

    def list_workspaces(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"args": args, "kwargs": kwargs})
        return list(self.workspaces)


@pytest.fixture
def client() -> TestClient:
    return TestClient(d.app)


def _workspaces_routes() -> list[Any]:
    matches = [
        r for r in d.app.routes if getattr(r, "path", None) == WORKSPACES_PATH
    ]
    assert matches, (
        "expected a route registered at GET /api/workspaces on"
        " app.dashboard.app; routes present:"
        f" {sorted({getattr(r, 'path', str(r)) for r in d.app.routes})}"
    )
    return matches


# --------------------------------------------------------------------------- #
# Route registration / shape
# --------------------------------------------------------------------------- #
def test_workspaces_route_is_registered_as_get_on_the_dashboard_app():
    matches = _workspaces_routes()
    assert len(matches) == 1, (
        f"expected exactly one {WORKSPACES_PATH} route, got {len(matches)}"
    )
    route = matches[0]
    assert "GET" in route.methods
    assert "POST" not in route.methods

    endpoint = route.endpoint
    assert endpoint is not None
    # The brief names the handler exactly `list_workspaces_route`, added to
    # the EXISTING app/dashboard.py module (not a new module).
    assert endpoint.__name__ == "list_workspaces_route"
    assert endpoint.__module__ == "app.dashboard"
    # A GET takes no body: the handler signature must accept no parameters
    # (in particular, no new request model from app/dashboard_models.py).
    assert list(inspect.signature(endpoint).parameters) == []


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_returns_200_with_service_list_under_workspaces_key(client, monkeypatch):
    entries = [
        {"name": "alpha", "path": "/tmp/alpha", "exists": True, "valid": True},
        {"name": "beta", "path": "/tmp/beta", "exists": True, "valid": False},
    ]
    svc = FakeWorkspaceService(entries)
    monkeypatch.setattr(d, "_service", svc)

    resp = client.get(WORKSPACES_PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert body["workspaces"] == entries


def test_route_delegates_to_service_list_workspaces_with_no_arguments(
    client, monkeypatch
):
    svc = FakeWorkspaceService([])
    monkeypatch.setattr(d, "_service", svc)

    resp = client.get(WORKSPACES_PATH)

    assert resp.status_code == 200
    assert len(svc.calls) == 1, f"expected exactly one service call, got {svc.calls}"
    assert svc.calls[0]["args"] == ()
    assert svc.calls[0]["kwargs"] == {}


# --------------------------------------------------------------------------- #
# Boundary: empty / single / many
# --------------------------------------------------------------------------- #
def test_empty_service_list_returns_200_not_404(client, monkeypatch):
    svc = FakeWorkspaceService([])
    monkeypatch.setattr(d, "_service", svc)

    resp = client.get(WORKSPACES_PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert body["workspaces"] == []


def test_single_entry_boundary(client, monkeypatch):
    entries = [{"name": "only", "path": "/tmp/only", "exists": True, "valid": True}]
    svc = FakeWorkspaceService(entries)
    monkeypatch.setattr(d, "_service", svc)

    resp = client.get(WORKSPACES_PATH)

    assert resp.status_code == 200
    assert resp.json()["workspaces"] == entries


# --------------------------------------------------------------------------- #
# Pass-through fidelity
# --------------------------------------------------------------------------- #
def test_entries_pass_through_unmodified_including_false_flags(client, monkeypatch):
    entries = [
        {"name": "gone", "path": "/tmp/gone", "exists": False, "valid": False},
        {
            "name": "invalid-but-present",
            "path": "/tmp/invalid",
            "exists": True,
            "valid": False,
        },
        {"name": "ok", "path": "/tmp/ok", "exists": True, "valid": True},
    ]
    svc = FakeWorkspaceService(entries)
    monkeypatch.setattr(d, "_service", svc)

    resp = client.get(WORKSPACES_PATH)

    assert resp.status_code == 200
    assert resp.json()["workspaces"] == entries


def test_entries_pass_through_unmodified_with_extra_and_falsey_fields(
    client, monkeypatch
):
    entry = {
        "name": "",
        "path": None,
        "exists": False,
        "valid": False,
        "branch": "feature/ünïcode",
        "meta": {"dirty": True, "ahead": 0},
        "tags": [],
    }
    svc = FakeWorkspaceService([entry])
    monkeypatch.setattr(d, "_service", svc)

    resp = client.get(WORKSPACES_PATH)

    assert resp.status_code == 200
    assert resp.json()["workspaces"] == [entry]


# --------------------------------------------------------------------------- #
# No route-level filtering or sorting
# --------------------------------------------------------------------------- #
def test_route_adds_no_filtering_or_sorting_of_its_own(client, monkeypatch):
    # Deliberately NOT sorted by name, and includes entries that any
    # exists/valid filtering would drop: the route must return the service's
    # list verbatim, in the service's order.
    entries = [
        {"name": "zulu", "exists": False, "valid": False},
        {"name": "alpha", "exists": True, "valid": True},
        {"name": "mike", "exists": False, "valid": True},
        {"name": "bravo", "exists": True, "valid": False},
    ]
    svc = FakeWorkspaceService(entries)
    monkeypatch.setattr(d, "_service", svc)

    resp = client.get(WORKSPACES_PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["workspaces"] == entries
    assert [w["name"] for w in body["workspaces"]] == [
        "zulu",
        "alpha",
        "mike",
        "bravo",
    ]
    assert len(body["workspaces"]) == len(entries)
    # Exactly one service call: no second pass for filtering/sorting.
    assert len(svc.calls) == 1