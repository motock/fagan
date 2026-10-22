"""DASH-1: the story-control routes move VERBATIM into
``app.dashboard_routes_story`` (RH-09 precedent, see test_dashboard_split.py).

``app.dashboard`` re-exports each handler and registers it on ``app`` ahead of
the catch-all StaticFiles mount. The new module must not import
``app.dashboard`` at module level; ``_service`` is resolved through it lazily.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app import dashboard as d
from tests.unit._dashboard_helpers import client  # noqa: F401

NEW_MODULE = "app.dashboard_routes_story"
REPO_ROOT = Path(__file__).resolve().parents[2]
HANDLERS = [
    "dispatch_story_route",
    "start_story_route",
    "interrupt_story_route",
    "set_story_status_route",
    "patch_story_route",
    "review_story_route",
    "approve_merge_route",
    "mark_story_done_route",
]
BASE = "/api/plans/{plan_name}/stories/{story_key}"
PATHS = [f"{BASE}/{suffix}" for suffix in (
    "dispatch", "start", "interrupt", "status", "patch", "review", "approve_merge", "done",
)]


def _new():
    return importlib.import_module(NEW_MODULE)


def test_should_import_new_module():
    assert _new().__name__ == NEW_MODULE


def test_should_not_import_dashboard_from_new_module():
    source = (REPO_ROOT / "app/dashboard_routes_story.py").read_text()
    assert "app.dashboard import" not in source.replace("app.dashboard_", "")


@pytest.mark.parametrize("name", HANDLERS)
def test_should_reexport_handler_by_identity(name):
    assert getattr(d, name) is getattr(_new(), name)


@pytest.mark.parametrize("name", HANDLERS)
def test_should_define_handler_in_new_module(name):
    assert getattr(_new(), name).__module__ == NEW_MODULE


def _paths():
    return [getattr(r, "path", None) for r in d.app.routes]


@pytest.mark.parametrize("path", PATHS)
def test_should_keep_route_registered_on_app(path):
    assert path in _paths()


def test_should_keep_static_mount_as_last_route():
    assert getattr(d.app.routes[-1], "name", None) == "static"


def test_should_keep_story_body_models_reexported_from_dashboard():
    assert {"StoryPatchBody", "StoryStatusBody", "SavePlanRequest",
            "DecomposeRequest", "IngestPlanRequest"} <= set(d.__all__)


class _StubService:
    def __init__(self):
        self.asked = []

    def dispatch_story(self, plan_name, story_key):
        self.asked.append((plan_name, story_key))
        return {"ok": False, "error": "stubbed"}

    def set_story_status(self, plan_name, story_key, status):
        self.asked.append((plan_name, story_key, status))
        return {"ok": True}


def test_should_reach_moved_route_when_patching_dashboard_service(client, monkeypatch):
    stub = _StubService()
    monkeypatch.setattr(d, "_service", stub)

    resp = client.post("/api/plans/p1/stories/s1/dispatch")

    assert (resp.status_code, stub.asked) == (404, [("p1", "s1")])


def test_should_reach_moved_route_when_patching_new_module_service(client, monkeypatch):
    stub = _StubService()
    monkeypatch.setattr(_new(), "_service", stub)

    resp = client.post("/api/plans/p2/stories/s2/dispatch")

    assert (resp.status_code, stub.asked) == (404, [("p2", "s2")])


def test_should_reach_body_route_when_patching_dashboard_service(client, monkeypatch):
    stub = _StubService()
    monkeypatch.setattr(d, "_service", stub)

    resp = client.post("/api/plans/p1/stories/s1/status", json={"status": "todo"})

    assert (resp.status_code, stub.asked) == (200, [("p1", "s1", "todo")])


def test_should_reject_status_route_without_body(client):
    assert client.post("/api/plans/p1/stories/s1/status", json={}).status_code == 422
