"""Tests for the UI/API-only split in ``app/dashboard_ui.py``.

The index handler is the one place where the shared-secret API key is handed
to the browser, so the mode switch is security-sensitive: with
``PIPELINE_DASHBOARD_UI=off`` nothing may be registered at all -- no ``/``
(which injects the key), no static catch-all -- and an unrecognised value must
refuse the UI loudly instead of silently serving it.

The default-mode tests exercise the real ``app.dashboard.app``. The API-only
tests build a second app carrying the dashboard's real ``/api/`` surface (the
same route objects and middleware) and mount the UI into it in the requested
mode, so no test has to mutate ``os.environ`` underneath an already-imported
app.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import api_contract, auth, dashboard_ui, oidc_login
from app import dashboard as d

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "static"
HEADER = "X-Pipeline-Api-Key"
KEY_GLOBAL = "window.__PIPELINE_API_KEY__"
UI_ON = {dashboard_ui.UI_ENV: "on"}
UI_OFF = {dashboard_ui.UI_ENV: "off"}


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so no test touches the real one."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def client(key_file):
    with TestClient(d.app) as test_client:
        yield test_client


def _ui_mode_app(environ):
    """A second app with the real ``/api/`` surface, UI wired per ``environ``.

    Mirrors what ``app.dashboard`` does at import time: register the index (when
    the UI is enabled), then the static catch-all. The catch-all lives in the
    dashboard module, not in ``dashboard_ui``, because it has to stay the last
    registration -- so it is recreated here.
    """
    mode_app = FastAPI(title="api-only probe", **api_contract.docs_kwargs())
    mode_app.user_middleware = list(d.app.user_middleware)
    for route in d.app.router.routes:
        if getattr(route, "path", "").startswith("/api"):
            mode_app.router.routes.append(route)
    dashboard_ui.register_index(
        mode_app, STATIC_DIR, auth.get_or_create_api_key, environ=environ
    )
    if dashboard_ui.ui_enabled(environ):
        mode_app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return mode_app


# ---------------------------------------------------------------------------
# Default mode: today's behaviour, unchanged
# ---------------------------------------------------------------------------
def test_default_mode_serves_the_index_with_the_key_injected(client):
    key = auth.get_or_create_api_key()
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert KEY_GLOBAL in resp.text
    assert key in resp.text


def test_default_mode_serves_static_assets(key_file):
    with TestClient(d.app) as asset_client:
        resp = asset_client.get("/app.js")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# API-only mode: the UI is absent and the key is never handed out
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/", "/app.js", "/style.css"])
def test_off_mode_serves_no_ui_path(key_file, path):
    with TestClient(_ui_mode_app(UI_OFF)) as off_client:
        resp = off_client.get(path)
        assert resp.status_code == 404
        assert auth.get_or_create_api_key() not in resp.text


def test_off_mode_never_injects_the_key_anywhere(key_file):
    key = auth.get_or_create_api_key()
    with TestClient(_ui_mode_app(UI_OFF)) as off_client:
        bodies = [off_client.get(path).text for path in ("/", "/app.js", "/style.css")]
    assert all(key not in body for body in bodies)
    assert all(KEY_GLOBAL not in body for body in bodies)


def test_off_mode_leaves_the_api_surface_working(key_file):
    key = auth.get_or_create_api_key()
    openapi_url = api_contract.docs_kwargs().get("openapi_url") or "/openapi.json"
    with TestClient(_ui_mode_app(UI_OFF)) as off_client:
        health = off_client.get("/api/health", headers={HEADER: key})
        openapi = off_client.get(openapi_url, headers={HEADER: key})
    assert health.status_code == 200
    assert openapi.status_code == 200
    assert "openapi" in openapi.json()


def test_off_mode_keeps_the_oidc_login_routes_registered(key_file, tmp_path):
    """The auth routes are mounted before the UI and must survive API-only mode."""
    config = oidc_login.OidcLoginConfig(
        enabled=True,
        client_id="client-id",
        client_secret_file=str(tmp_path / "client-secret"),
        redirect_uri="https://dashboard.example/auth/callback",
        issuer="https://issuer.example",
    )
    auth_app = FastAPI()
    oidc_login.mount_auth_routes(auth_app, config)
    dashboard_ui.register_index(
        auth_app, STATIC_DIR, auth.get_or_create_api_key, environ=UI_OFF
    )

    paths = {getattr(route, "path", "") for route in auth_app.routes}
    assert "/auth/login" in paths

    with TestClient(auth_app) as auth_client:
        resp = auth_client.get("/auth/login", follow_redirects=False)
    assert resp.status_code != 404


# ---------------------------------------------------------------------------
# The switch itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "environ", [{}, {dashboard_ui.UI_ENV: "on"}, {dashboard_ui.UI_ENV: " ON "}]
)
def test_ui_is_enabled_by_on_or_by_unset(environ):
    assert dashboard_ui.ui_enabled(environ) is True


def test_ui_is_disabled_by_off_and_the_mapping_is_not_mutated():
    environ = {dashboard_ui.UI_ENV: " OFF "}
    assert dashboard_ui.ui_enabled(environ) is False
    assert environ == {dashboard_ui.UI_ENV: " OFF "}


@pytest.mark.parametrize("value", ["maybe", "0", "false", "1"])
def test_invalid_ui_mode_is_refused_without_echoing_the_value(value):
    with pytest.raises(ValueError) as excinfo:
        dashboard_ui.ui_enabled({dashboard_ui.UI_ENV: value})
    message = str(excinfo.value)
    assert dashboard_ui.UI_ENV in message
    assert value not in message


# ---------------------------------------------------------------------------
# Registration order: index first, catch-all last, nothing when disabled
# ---------------------------------------------------------------------------
class _RecordingApp:
    """Stand-in for ``FastAPI`` that records what ``register_index`` registers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.index = None

    def get(self, path, **_kwargs):
        self.calls.append(("get", path))

        def _register(handler):
            self.index = handler
            return handler

        return _register

    def mount(self, path, *_args, **_kwargs):
        self.calls.append(("mount", path))


def test_register_index_registers_the_index_only_when_enabled(key_file):
    reads: list[int] = []

    def read_key():
        reads.append(1)
        return auth.get_or_create_api_key()

    fake = _RecordingApp()
    dashboard_ui.register_index(fake, STATIC_DIR, read_key, environ=UI_OFF)
    assert fake.calls == []
    assert reads == []

    dashboard_ui.register_index(fake, STATIC_DIR, read_key, environ=UI_ON)
    assert fake.calls == [("get", "/")], "only the index; the catch-all is the caller's"
    assert reads == [], "the key must be read per request, never at registration"

    response = fake.index()
    assert reads == [1]
    assert KEY_GLOBAL in response.body.decode()


# ---------------------------------------------------------------------------
# The real app's wiring: the guard in app/dashboard.py, not a mirror of it
# ---------------------------------------------------------------------------
_ROUTE_DUMP = (
    "import json\n"
    "from app import dashboard\n"
    "print(json.dumps([\n"
    "    [getattr(r, 'path', None), getattr(r, 'name', None)]\n"
    "    for r in dashboard.app.router.routes\n"
    "]))\n"
)


def _real_app_routes(ui_mode):
    """Import the real app in a fresh interpreter with PIPELINE_DASHBOARD_UI set."""
    env = {**os.environ, dashboard_ui.UI_ENV: ui_mode}
    result = subprocess.run(
        [sys.executable, "-c", _ROUTE_DUMP],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_real_app_in_off_mode_registers_no_ui_route_and_no_catch_all():
    routes = _real_app_routes("off")
    assert "/" not in [path for path, _name in routes]
    assert [route for route in routes if route[1] == "static"] == []
    assert "/api/health" in [path for path, _name in routes]


def test_real_app_in_on_mode_registers_the_index_then_the_catch_all_last():
    routes = _real_app_routes("on")
    assert routes[-1] == ["", "static"], "the catch-all must remain the last route"
    assert routes[-2] == ["/", "dashboard_index"]


def test_real_app_refuses_an_invalid_ui_mode_at_import():
    result = subprocess.run(
        [sys.executable, "-c", "import app.dashboard"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, dashboard_ui.UI_ENV: "maybe"},
        timeout=180,
    )
    assert result.returncode != 0
    assert dashboard_ui.UI_ENV in result.stderr
    assert "maybe" not in result.stderr
