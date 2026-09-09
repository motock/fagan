"""Tests for POST /api/decompose accepting an optional ``workspace`` in the
request body, with an active-workspace fallback (mirrors the WS-11 pattern
already applied to the save-plan route -- see
``tests/unit/test_dashboard_save_workspace.py``).

Scope (see the dispatch brief): ``app/dashboard_models.py``'s
``DecomposeRequest`` gains an additive optional ``workspace: str | None =
None`` field, and ``app/dashboard.py``'s ``decompose_route`` resolves

    workspace = request.workspace if request.workspace is not None
                else _service.get_active_workspace()

and passes it to ``_service.decompose_plan(request.request, workspace)``.
The service-level validation/stamping behaviour itself is covered by
``tests/unit/test_service_decompose_workspace.py`` and is NOT re-tested here;
the backend (``pipeline.service._run_decompose`` and
``pipeline.workspace.validate_workspace``) is stubbed the same way that
file stubs it, so no real LLM decompose call or real filesystem/git
workspace check is ever exercised.

Success criteria pinned here:

* explicit ``workspace`` in the body -> the returned plan's ``repo_root``
  equals the VALIDATED RESOLVED path (never the raw input);
* no ``workspace`` in the body but an active workspace persisted ->
  ``repo_root`` stamped from the active workspace;
* neither -> the plan is returned exactly as the (stubbed) model authored
  it (no ``repo_root`` injection);
* invalid workspace -> 400 and the detail contains no resolved absolute
  paths (fail-closed, sanitized).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from pipeline import server as p


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture(autouse=True)
def _stub_decompose_backend(monkeypatch):
    """Stub the decompose backend and workspace validation at their real
    integration points (module-level names on ``pipeline.server``, the
    convention established by test_service_decompose_workspace.py).

    The fake backend always authors a plan WITHOUT a ``repo_root`` key, so
    any ``repo_root`` observed in a response can only have come from the
    workspace stamping path under test.
    """
    plan = {"epics": [{"title": "Epic", "stories": [{"title": "Story"}]}]}
    raw = "```json\n" + json.dumps(plan) + "\n```"
    monkeypatch.setattr(p, "_run_decompose_detailed", lambda request, **k: (raw, None))
    monkeypatch.setattr(
        "pipeline.service.validate_workspace",
        lambda path: (
            {"ok": True, "path": f"/resolved/{path}", "error": None}
            if path != "/bad/workspace"
            else {"ok": False, "path": "", "error": "workspace not found"}
        ),
        raising=False,
    )


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect every PLAN_DIR binding the active-workspace path touches to a
    tmp directory (mirrors tests/unit/test_dashboard_workspace_active.py's
    plan_dir fixture). ``_service.get_active_workspace`` delegates to
    ``pipeline.server._store`` (a FileStore), which resolves PLAN_DIR through
    ``pipeline.server.PLAN_DIR`` at call time - patching ``p.PLAN_DIR`` is
    what actually redirects the active_workspace.json file."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(d, "PLAN_DIR", directory)
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def _active_workspace_store(plan_dir):
    """Seed/clear helper: tests write ``{"path": ...}`` here to persist an
    active workspace (the FileStore's on-disk contract)."""
    return plan_dir / "active_workspace.json"


# ---------------------------------------------------------------------------
# Explicit workspace in the body
# ---------------------------------------------------------------------------

def test_decompose_with_explicit_workspace_stamps_validated_repo_root(client):
    res = client.post("/api/decompose", json={"request": "goal", "workspace": "/tmp/ws"})

    assert res.status_code == 200
    plan = res.json()["plan"]
    # The VALIDATED RESOLVED path, never the raw untrusted input.
    assert plan["repo_root"] == "/resolved//tmp/ws"


def test_decompose_with_explicit_workspace_does_not_leak_raw_path(client):
    res = client.post("/api/decompose", json={"request": "goal", "workspace": "/tmp/ws"})

    assert res.status_code == 200
    assert res.json()["plan"]["repo_root"] != "/tmp/ws"


# ---------------------------------------------------------------------------
# Active-workspace fallback
# ---------------------------------------------------------------------------

def test_decompose_without_body_workspace_falls_back_to_active_workspace(
    client, _active_workspace_store
):
    _active_workspace_store.write_text(json.dumps({"path": "/tmp/active-ws"}))
    res = client.post("/api/decompose", json={"request": "goal"})

    assert res.status_code == 200
    assert res.json()["plan"]["repo_root"] == "/resolved//tmp/active-ws"


def test_decompose_with_explicit_workspace_beats_active_workspace(
    client, _active_workspace_store
):
    _active_workspace_store.write_text(json.dumps({"path": "/tmp/active-ws"}))
    res = client.post(
        "/api/decompose", json={"request": "goal", "workspace": "/tmp/explicit"}
    )

    assert res.status_code == 200
    assert res.json()["plan"]["repo_root"] == "/resolved//tmp/explicit"


def test_decompose_with_empty_string_workspace_is_forwarded_not_fallback(
    client, _active_workspace_store
):
    # The route's guard is ``is not None`` (same as the save route): an
    # empty string is PRESENT, so it is forwarded to the service for
    # validation rather than silently swapped for the active workspace.
    # Here no active workspace is persisted at all, and the stub validator
    # accepts the forwarded value, proving no fallback occurred.
    res = client.post("/api/decompose", json={"request": "goal", "workspace": ""})

    assert res.status_code == 200
    assert res.json()["plan"]["repo_root"] == "/resolved/"


# ---------------------------------------------------------------------------
# No workspace anywhere: plan returned exactly as authored
# ---------------------------------------------------------------------------

def test_decompose_with_no_workspace_returns_plan_as_authored(client, plan_dir):
    res = client.post("/api/decompose", json={"request": "goal"})

    assert res.status_code == 200
    plan = res.json()["plan"]
    assert plan == {"epics": [{"title": "Epic", "stories": [{"title": "Story"}]}]}
    assert "repo_root" not in plan


# ---------------------------------------------------------------------------
# Invalid workspace: fail closed, sanitized
# ---------------------------------------------------------------------------

def test_decompose_with_invalid_workspace_returns_400_without_paths(client):
    res = client.post("/api/decompose", json={"request": "goal", "workspace": "/bad/workspace"})

    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "workspace not found" in detail
    # Sanitized: no resolved absolute paths in the error detail.
    assert "/bad/workspace" not in detail
    assert not any(
        token.startswith("/resolved") for token in detail.split()
    )


def test_decompose_with_invalid_active_workspace_returns_400(
    client, _active_workspace_store
):
    # The fallback path must fail closed too: a stale persisted active
    # workspace that no longer validates yields 400, not an unstamped plan.
    _active_workspace_store.write_text(json.dumps({"path": "/bad/workspace"}))
    res = client.post("/api/decompose", json={"request": "goal"})

    assert res.status_code == 400
    assert "workspace not found" in res.json()["detail"]


# ---------------------------------------------------------------------------
# Request-model shape
# ---------------------------------------------------------------------------

def test_decompose_request_workspace_field_is_optional():
    # Additive optional field: absent -> None, present -> carried through.
    from app.dashboard_models import DecomposeRequest

    assert DecomposeRequest(request="goal").workspace is None
    assert DecomposeRequest(request="goal", workspace="/tmp/ws").workspace == "/tmp/ws"