"""Tests for the dashboard's workspace route (POST /api/workspace).

Adds ONE request model and ONE route across two existing files:

  * app/dashboard_models.py gains ``WorkspaceRequest`` (``path: str``,
    ``create: bool = False``), beside the existing request models.
  * app/dashboard.py imports it in the (alphabetically sorted)
    ``from app.dashboard_models import (...)`` block - immediately after
    ``StoryStatusBody`` - and gains:

        @app.post("/api/workspace")
        def set_workspace_route(request: WorkspaceRequest):
            result = _service.resolve_workspace(request.path, create=request.create)
            if not result.get("ok"):
                raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
            return result

    mirroring the existing POST /api/config/roles/{role} precedent
    (app/dashboard.py:507): the route contains NO business logic - it
    delegates to the module-level ``_service`` and maps a falsy ``ok``
    to HTTP 400 with the service's error message.

These tests describe behavior for code that does not exist yet on this
branch and must fail (ImportError/AttributeError/AssertionError) until
both files are updated.
"""
from __future__ import annotations

import inspect
import re

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from app.dashboard_models import WorkspaceRequest

# A path string a validating route would be tempted to strip/normalize:
# leading/trailing whitespace, traversal dots, doubled slashes, unicode.
_RAW_UNVALIDATED_PATH = "  ../weird PATH/ünïcode//dots..  "


class FakeWorkspaceService:
    """Recording spy standing in for the PipelineService singleton.

    Records every ``resolve_workspace`` call exactly as the service would
    have seen it (effective argument values, positional or keyword) and
    returns a configurable result dict.
    """

    def __init__(self, result: dict | None = None):
        self.calls: list[dict] = []
        self.result = {"ok": True} if result is None else result

    def resolve_workspace(self, path, create=False):
        self.calls.append({"path": path, "create": create})
        return self.result

    @property
    def last_call(self) -> dict:
        assert self.calls, "expected _service.resolve_workspace to have been called"
        return self.calls[-1]


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def fake_service(monkeypatch):
    """Replace the module-level _service singleton with the recording spy."""
    svc = FakeWorkspaceService()
    monkeypatch.setattr(d, "_service", svc)
    return svc


# --- model: app/dashboard_models.WorkspaceRequest --------------------------


def test_workspace_request_model_has_path_required_and_create_default_false():
    """WorkspaceRequest exists with path: str (required) and create: bool = False."""
    fields = WorkspaceRequest.model_fields
    assert "path" in fields, "WorkspaceRequest must declare a `path` field"
    assert fields["path"].is_required(), "`path` must be a required field"
    assert "create" in fields, "WorkspaceRequest must declare a `create` field"
    assert fields["create"].default is False, "`create` must default to False"


def test_workspace_request_model_construction():
    body = WorkspaceRequest(path="some/plan")
    assert body.path == "some/plan"
    assert body.create is False
    assert WorkspaceRequest(path="some/plan", create=True).create is True


# --- import wiring: dashboard.py's sorted dashboard_models import block ----


def test_dashboard_imports_workspace_request_after_story_status_body():
    """WorkspaceRequest must be added to dashboard.py's existing
    alphabetically-sorted `from app.dashboard_models import (...)` block,
    after StoryStatusBody (anchored relative to that fixed name - not an
    exact-match assertion on the whole block)."""
    src = inspect.getsource(d)
    match = re.search(r"from app\.dashboard_models import \((.*?)\)", src, re.DOTALL)
    assert match, "dashboard.py must import its request models from app.dashboard_models"
    names = [line.strip().rstrip(",") for line in match.group(1).splitlines() if line.strip()]
    assert "WorkspaceRequest" in names, (
        "WorkspaceRequest must be imported from app.dashboard_models in app/dashboard.py"
    )
    assert "StoryStatusBody" in names
    assert names.index("WorkspaceRequest") > names.index("StoryStatusBody"), (
        "WorkspaceRequest sorts after StoryStatusBody in the import block"
    )


# --- route registration and shape ------------------------------------------


def test_workspace_route_registered_as_post_api_workspace():
    post_paths = [
        r.path for r in d.app.routes if "POST" in getattr(r, "methods", set())
    ]
    assert "/api/workspace" in post_paths, "POST /api/workspace must be registered"


def test_workspace_route_handler_is_named_set_workspace_route():
    route = next(
        r
        for r in d.app.routes
        if getattr(r, "path", None) == "/api/workspace"
        and "POST" in getattr(r, "methods", set())
    )
    assert route.endpoint.__name__ == "set_workspace_route"


def test_workspace_route_delegates_to_service_without_business_logic():
    source = inspect.getsource(d.set_workspace_route)
    assert "resolve_workspace" in source, "route must delegate to _service.resolve_workspace"
    assert "request.create" in source, "route must pass request.create through to the service"


# --- happy path -------------------------------------------------------------


def test_valid_path_returns_200_and_service_result_body(client, fake_service):
    service_body = {
        "ok": True,
        "path": "/home/operator/.claude/plans/my-plan",
        "created": False,
        "worktree": "/home/operator/.claude/worktrees/my-plan",
    }
    fake_service.result = service_body
    response = client.post("/api/workspace", json={"path": "/home/operator/.claude/plans/my-plan"})
    assert response.status_code == 200
    assert response.json() == service_body
    assert fake_service.last_call["path"] == "/home/operator/.claude/plans/my-plan"


# --- create passthrough ------------------------------------------------------


def test_create_defaults_to_false_when_not_sent(client, fake_service):
    fake_service.result = {"ok": True}
    response = client.post("/api/workspace", json={"path": "a/b"})
    assert response.status_code == 200
    assert fake_service.last_call["create"] is False


def test_create_true_is_passed_through_to_service(client, fake_service):
    fake_service.result = {"ok": True, "created": True}
    response = client.post("/api/workspace", json={"path": "a/b", "create": True})
    assert response.status_code == 200
    assert fake_service.last_call["create"] is True
    assert fake_service.last_call["path"] == "a/b"


# --- service failure mapping -------------------------------------------------


def test_service_not_ok_returns_400_with_service_error_in_detail(client, fake_service):
    fake_service.result = {"ok": False, "error": "no such workspace: a/b"}
    response = client.post("/api/workspace", json={"path": "a/b"})
    assert response.status_code == 400
    assert response.json()["detail"] == "no such workspace: a/b"


def test_service_not_ok_without_error_key_falls_back_to_unknown_error(client, fake_service):
    fake_service.result = {"ok": False}
    response = client.post("/api/workspace", json={"path": "a/b"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown error"


# --- request validation (FastAPI/pydantic, not the route) --------------------


def test_missing_path_field_returns_422(client, fake_service):
    response = client.post("/api/workspace", json={})
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(
        err.get("loc", [None])[-1] == "path" for err in errors
    ), f"expected a validation error naming `path`, got {errors!r}"
    assert fake_service.calls == [], "service must not be called on a 422"


def test_non_string_path_returns_422(client, fake_service):
    response = client.post("/api/workspace", json={"path": 123})
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(
        err.get("loc", [None])[-1] == "path" for err in errors
    ), f"expected a validation error naming `path`, got {errors!r}"
    assert fake_service.calls == [], "service must not be called on a 422"


def test_null_path_returns_422(client, fake_service):
    response = client.post("/api/workspace", json={"path": None})
    assert response.status_code == 422
    assert fake_service.calls == [], "service must not be called on a 422"


# --- no route-level path validation ------------------------------------------


def test_empty_string_path_reaches_service_and_maps_to_400(client, fake_service):
    """An empty path is not rejected by the route itself: it reaches the
    service, and only the service's ok=False turns it into a 400."""
    fake_service.result = {"ok": False, "error": "path must not be empty"}
    response = client.post("/api/workspace", json={"path": ""})
    assert fake_service.calls, "empty-string path must still reach the service"
    assert fake_service.last_call["path"] == ""
    assert response.status_code == 400
    assert response.json()["detail"] == "path must not be empty"


def test_raw_path_reaches_service_unmodified_no_route_validation(client, fake_service):
    """The route contains no path validation/normalization of its own: a
    recording spy must observe the raw input string, byte for byte."""
    fake_service.result = {"ok": True}
    response = client.post(
        "/api/workspace", json={"path": _RAW_UNVALIDATED_PATH, "create": True}
    )
    assert response.status_code == 200
    assert len(fake_service.calls) == 1
    assert fake_service.last_call["path"] == _RAW_UNVALIDATED_PATH, (
        "route must pass the raw path through unmodified "
        f"(got {fake_service.last_call['path']!r})"
    )
    assert fake_service.last_call["create"] is True