"""DASH-2: the plan-control routes move VERBATIM into
``app.dashboard_routes_plan`` (same contract as DASH-1, see
test_dashboard_story_routes_split.py).

``app.dashboard`` re-exports each handler and registers it on ``app`` ahead of
the catch-all StaticFiles mount. The new module must not import
``app.dashboard`` at module level; ``_service`` is resolved through it lazily.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from app import dashboard as d
from tests.unit._dashboard_helpers import client  # noqa: F401

NEW_MODULE = "app.dashboard_routes_plan"
REPO_ROOT = Path(__file__).resolve().parents[2]
HANDLERS = [
    "save_plan",
    "decompose_route",
    "ingest_plan",
    "advance_pipeline",
    "advance_all_plans",
    "checkpoint",
]
PATHS = [
    "/api/plans/{plan_name}/save",
    "/api/decompose",
    "/api/plans/{plan_name}/ingest",
    "/api/plans/{plan_name}/advance",
    "/api/plans/advance_all",
    "/api/plans/{plan_name}/stories/{story_key}/checkpoint",
]


def _new():
    return importlib.import_module(NEW_MODULE)


def test_should_import_new_module_cold_without_loading_dashboard():
    code = (
        "import sys, app.dashboard_routes_plan; "
        "sys.exit(1 if 'app.dashboard' in sys.modules else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_should_not_import_dashboard_from_new_module():
    source = (REPO_ROOT / "app/dashboard_routes_plan.py").read_text()
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


class _StubService:
    def __init__(self):
        self.asked = []

    def advance_pipeline(self, plan_name):
        self.asked.append(("advance", plan_name))
        return {"ok": False, "error": "stubbed"}

    def ingest_plan(self, plan_name, **kwargs):
        self.asked.append(("ingest", plan_name))
        return {"ok": True}

    def checkpoint(self, plan_name, story_key, step, summary, next_hint):
        self.asked.append(("checkpoint", plan_name, story_key, step, summary, next_hint))
        return {"ok": True}


def test_should_reach_plan_route_when_patching_dashboard_service(client, monkeypatch):
    stub = _StubService()
    monkeypatch.setattr(d, "_service", stub)

    resp = client.post("/api/plans/p1/advance")

    assert (resp.status_code, stub.asked) == (400, [("advance", "p1")])


def test_should_run_ingest_when_origin_header_absent(client, monkeypatch):
    stub = _StubService()
    monkeypatch.setattr(d, "_service", stub)

    resp = client.post("/api/plans/p1/ingest")

    assert (resp.status_code, stub.asked) == (200, [("ingest", "p1")])


def test_should_refuse_ingest_from_chat_origin(client, monkeypatch):
    stub = _StubService()
    monkeypatch.setattr(d, "_service", stub)

    resp = client.post("/api/plans/p1/ingest", headers={"X-Pipeline-Origin": "chat"})

    assert (resp.status_code, stub.asked) == (403, [])


def test_should_forward_checkpoint_body_fields(client, monkeypatch):
    stub = _StubService()
    monkeypatch.setattr(d, "_service", stub)

    resp = client.post(
        "/api/plans/p1/stories/s1/checkpoint",
        json={"step": "a", "summary": "b", "next_hint": "c"},
    )

    assert (resp.status_code, stub.asked) == (200, [("checkpoint", "p1", "s1", "a", "b", "c")])


def test_should_reject_checkpoint_missing_summary(client, monkeypatch):
    stub = _StubService()
    monkeypatch.setattr(d, "_service", stub)

    resp = client.post("/api/plans/p1/stories/s1/checkpoint", json={"step": "a"})

    assert (resp.status_code, stub.asked) == (400, [])
