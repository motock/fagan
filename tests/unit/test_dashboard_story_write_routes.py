"""Tests for the dashboard's story write routes (W1c-01 follow-on).

Adds three POST routes to app/dashboard.py that delegate to the module-level
``_service`` (a ``PipelineService`` singleton):

  * POST /api/plans/{plan_name}/stories/{story_key}/interrupt
        -> _service.interrupt_story(plan_name, story_key)
  * POST /api/plans/{plan_name}/stories/{story_key}/status
        -> _service.set_story_status(plan_name, story_key, status)
        (JSON body {"status": <string>}; missing status is a 422 from
        FastAPI's own body validation, not a custom check)
  * POST /api/plans/{plan_name}/stories/{story_key}/patch
        -> _service.patch_story(plan_name, story_key, fields=...)
        (JSON body is the fields dict; only the fields PipelineService
        accepts may be passed through - see _PATCHABLE_STORY_FIELDS)

These tests describe behavior for code that does not exist yet on this
branch and must fail (ImportError/AttributeError/AssertionError) until
app/dashboard.py is updated.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d

# The exact set of fields PipelineService.patch_story accepts (mirrors
# pipeline/server.py's _PATCHABLE_STORY_FIELDS). The /patch route must only
# pass through these - anything else in the body must be dropped.
_PATCHABLE_FIELDS = {
    "agent_instructions",
    "model",
    "persona",
    "risk",
    "dependencies",
    "acceptance",
    "pr_url",
    "summary",
    "tdd_split",
}


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def mock_service(monkeypatch):
    """Replace the module-level _service singleton with a Mock so the new
    routes can be exercised without touching the real pipeline store."""
    svc = Mock()
    monkeypatch.setattr(d, "_service", svc)
    return svc


def _call_args(call):
    """Return (positional_args, kwargs) for a recorded mock call."""
    return call.args, call.kwargs


def _fields_from_call(call):
    """Extract the fields dict from a patch_story call regardless of whether
    the implementer passed it positionally or as the `fields` keyword."""
    args, kwargs = _call_args(call)
    if "fields" in kwargs:
        return kwargs["fields"]
    assert len(args) >= 3, f"expected plan_name, story_key, fields; got {args!r}"
    return args[2]


# --- route registration ---------------------------------------------------


def test_interrupt_route_registered():
    paths = {
        (route.path, method)
        for route in d.app.routes
        for method in (getattr(route, "methods", None) or set())
        if method != "HEAD"
    }
    assert ("/api/plans/{plan_name}/stories/{story_key}/interrupt", "POST") in paths


def test_status_route_registered():
    paths = {
        (route.path, method)
        for route in d.app.routes
        for method in (getattr(route, "methods", None) or set())
        if method != "HEAD"
    }
    assert ("/api/plans/{plan_name}/stories/{story_key}/status", "POST") in paths


def test_patch_route_registered():
    paths = {
        (route.path, method)
        for route in d.app.routes
        for method in (getattr(route, "methods", None) or set())
        if method != "HEAD"
    }
    assert ("/api/plans/{plan_name}/stories/{story_key}/patch", "POST") in paths


# --- interrupt: happy path ------------------------------------------------


def test_interrupt_200_delegates_to_service(client, mock_service):
    mock_service.interrupt_story.return_value = {
        "ok": True,
        "status": "interrupted",
        "commit": "abc123",
    }

    res = client.post("/api/plans/demo/stories/S1/interrupt")

    assert res.status_code == 200
    assert res.json() == {
        "ok": True,
        "status": "interrupted",
        "commit": "abc123",
    }
    mock_service.interrupt_story.assert_called_once_with("demo", "S1")


# --- status: happy path + validation --------------------------------------


def test_status_200_delegates_to_service(client, mock_service):
    mock_service.set_story_status.return_value = {
        "ok": True,
        "story_key": "S1",
        "status": "done",
    }

    res = client.post(
        "/api/plans/demo/stories/S1/status",
        json={"status": "done"},
    )

    assert res.status_code == 200
    assert res.json() == {"ok": True, "story_key": "S1", "status": "done"}
    mock_service.set_story_status.assert_called_once_with("demo", "S1", "done")


def test_status_422_when_status_missing(client, mock_service):
    """Missing `status` must be a 422 from FastAPI's own body validation, not
    a custom check that returns 200 with an ok:false payload."""
    res = client.post(
        "/api/plans/demo/stories/S1/status",
        json={},
    )

    assert res.status_code == 422
    mock_service.set_story_status.assert_not_called()


def test_status_422_when_body_not_json_object(client, mock_service):
    """A non-object body (e.g. a bare string) is also malformed for a
    {"status": <string>} contract and must be rejected by body validation."""
    res = client.post(
        "/api/plans/demo/stories/S1/status",
        content='"not-an-object"',
        headers={"Content-Type": "application/json"},
    )

    assert res.status_code == 422
    mock_service.set_story_status.assert_not_called()


def test_status_422_when_status_not_a_string(client, mock_service):
    """`status` must be a string; a number must be rejected by body
    validation rather than passed through to the service."""
    res = client.post(
        "/api/plans/demo/stories/S1/status",
        json={"status": 42},
    )

    assert res.status_code == 422
    mock_service.set_story_status.assert_not_called()


# --- patch: happy path + field filtering ----------------------------------


def test_patch_200_delegates_to_service(client, mock_service):
    mock_service.patch_story.return_value = {
        "ok": True,
        "story_key": "S1",
        "story": {"summary": "new", "model": "gpt-4"},
    }

    body = {"model": "gpt-4", "summary": "new summary"}
    res = client.post("/api/plans/demo/stories/S1/patch", json=body)

    assert res.status_code == 200
    assert res.json()["ok"] is True
    mock_service.patch_story.assert_called_once()
    args, _ = _call_args(mock_service.patch_story.call_args)
    assert args[0] == "demo"
    assert args[1] == "S1"
    assert _fields_from_call(mock_service.patch_story.call_args) == body


def test_patch_only_passes_through_patchable_fields(client, mock_service):
    """Non-patchable fields (e.g. `status`, which belongs to set_story_status)
    must be dropped before the body reaches patch_story."""
    mock_service.patch_story.return_value = {"ok": True, "story_key": "S1", "story": {}}

    body = {
        "model": "gpt-4",
        "summary": "new",
        "status": "done",  # not patchable - must be filtered out
        "pid": 12345,  # not patchable - must be filtered out
    }
    res = client.post("/api/plans/demo/stories/S1/patch", json=body)

    assert res.status_code == 200
    mock_service.patch_story.assert_called_once()
    passed = _fields_from_call(mock_service.patch_story.call_args)
    assert passed == {"model": "gpt-4", "summary": "new"}
    assert set(passed) <= _PATCHABLE_FIELDS


def test_patch_empty_body_passes_empty_fields(client, mock_service):
    """An empty body is a valid (no-op) patch - it must reach the service as
    an empty fields dict, not error out."""
    mock_service.patch_story.return_value = {"ok": True, "story_key": "S1", "story": {}}

    res = client.post("/api/plans/demo/stories/S1/patch", json={})

    assert res.status_code == 200
    mock_service.patch_story.assert_called_once()
    assert _fields_from_call(mock_service.patch_story.call_args) == {}


def test_patch_all_non_patchable_fields_passes_empty_fields(client, mock_service):
    """If the body contains only non-patchable fields, the service must still
    be called (with an empty fields dict) rather than erroring."""
    mock_service.patch_story.return_value = {"ok": True, "story_key": "S1", "story": {}}

    res = client.post(
        "/api/plans/demo/stories/S1/patch",
        json={"status": "done", "pid": 7},
    )

    assert res.status_code == 200
    mock_service.patch_story.assert_called_once()
    assert _fields_from_call(mock_service.patch_story.call_args) == {}


def test_patch_passes_every_patchable_field_through(client, mock_service):
    """Every field in _PATCHABLE_FIELDS must be accepted and forwarded."""
    mock_service.patch_story.return_value = {"ok": True, "story_key": "S1", "story": {}}

    body = {field: f"value-{field}" for field in sorted(_PATCHABLE_FIELDS)}
    res = client.post("/api/plans/demo/stories/S1/patch", json=body)

    assert res.status_code == 200
    mock_service.patch_story.assert_called_once()
    assert _fields_from_call(mock_service.patch_story.call_args) == body
