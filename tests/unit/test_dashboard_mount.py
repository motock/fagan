"""Tests for mounting operator-selected dashboard extensions.

A plugin is code running with the server's privileges, chosen ONLY by the
operator environment variable ``PIPELINE_DASHBOARD_PLUGINS=module:attr,...``.
This module grades the mount seam in ``app/dashboard_mount.py``:

* off when the variable is unset — no ``/api/ext`` and no ``/ext`` route;
* a registrar's routes live under ``/api/ext``, which means they sit behind the
  existing ``/api/`` authentication middleware: 200 with a real credential,
  401 without one;
* ``ctx.static`` pages are served WITHOUT authentication, exactly like the core
  pages, and never shadow them;
* every invalid input fails closed with an error that does not echo the
  operator's path (a path may be a secret-bearing deployment detail);
* a bad spec, or a registrar that raises, stops startup — nothing is silently
  skipped.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

import pipeline.server  # noqa: F401  (import cycle: pipeline.server first)
from app import auth
from app import dashboard as d
from app.dashboard_mount import (
    EXT_API_PREFIX,
    EXT_STATIC_PREFIX,
    DashboardContext,
    mount_routes,
)
from app.plugin_loader import PluginError

_REPO_ROOT = Path(__file__).resolve().parents[2]

_REPO_ROOT = Path(__file__).resolve().parents[2]

ENV = "PIPELINE_DASHBOARD_PLUGINS"
_HEADER = "X-Pipeline-Api-Key"

#: A made-up secret planted in plugin source and in operator paths. It must
#: never reach an error message or a log line.
SECRET = "sk-PLUGIN-SECRET-must-not-leak"

_PING_PLUGIN = """
SECRET = <SECRET>


def register(ctx):
    @ctx.router.get("/ping")
    def ping():
        return {"pong": True, "secret": SECRET}
"""

_ONE_PLUGIN = """
def register(ctx):
    @ctx.router.get("/one")
    def one():
        return {"plugin": "one"}
"""

_TWO_PLUGIN = """
def register(ctx):
    @ctx.router.get("/two")
    def two():
        return {"plugin": "two"}
"""

_BOOM_PLUGIN = """
def register(ctx):
    raise RuntimeError("boom")
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _write_plugin(tmp_path, name: str, source: str):
    """Write a plugin module into tmp_path and make it importable."""
    source = textwrap.dedent(source).replace("<SECRET>", repr(SECRET))
    (tmp_path / f"{name}.py").write_text(source)
    importlib.invalidate_caches()
    return f"{name}:register"


def _reload_dashboard(specs: str | None):
    """Reload app.dashboard, which runs mount_routes(app) at import time."""
    if specs is None:
        os.environ.pop(ENV, None)
    else:
        os.environ[ENV] = specs
    importlib.invalidate_caches()
    return importlib.reload(d)


@pytest.fixture
def plugin_path(tmp_path, monkeypatch):
    """Make plugin modules written into tmp_path importable."""
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


@pytest.fixture
def clean_dashboard_env(monkeypatch):
    """Restore app.dashboard's module state after a plugin reload.

    reload() mutates the shared module object, so the baseline app is rebuilt
    once the test is over — otherwise this module's plugins would leak into
    every later test that touches ``d.app``.
    """
    yield
    monkeypatch.delenv(ENV, raising=False)
    importlib.invalidate_caches()
    importlib.reload(d)


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file (see test_dashboard_oidc)."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


def _ext_paths(app) -> list[str]:
    """The plugin JSON routes, in registration order.

    Read from the OpenAPI schema: this FastAPI version keeps a router included
    with ``include_router`` as one opaque entry in ``app.routes``, so the
    schema is the honest, public view of the full paths.
    """
    return [p for p in app.openapi()["paths"] if p.startswith(EXT_API_PREFIX)]


def _static_mount_paths(app) -> list[str]:
    """The static directories mounted at ``/ext/<name>``."""
    return [
        route.path
        for route in app.routes
        if isinstance(route, Mount) and route.path.startswith(EXT_STATIC_PREFIX)
    ]


# --------------------------------------------------------------------------- #
# (1) Off unless the operator asks
# --------------------------------------------------------------------------- #

def test_unset_env_mounts_nothing(monkeypatch):
    """With no plugin configured, mount_routes must add no ext surface."""
    monkeypatch.delenv(ENV, raising=False)
    app = FastAPI()

    mount_routes(app)

    assert _ext_paths(app) == []
    assert _static_mount_paths(app) == []


# --------------------------------------------------------------------------- #
# (2) Plugin routes are authenticated: they live under /api/
# --------------------------------------------------------------------------- #

def test_plugin_route_requires_a_credential(
    plugin_path, key_file, clean_dashboard_env
):
    spec = _write_plugin(plugin_path, "plugin_ping", _PING_PLUGIN)

    module = _reload_dashboard(spec)

    client = TestClient(module.app)
    # tests/unit/conftest.py force-attaches a valid key to every TestClient;
    # pop it so this really is a credential-less request.
    del client.headers[_HEADER]
    assert client.get("/api/ext/ping").status_code == 401

    key = auth.get_or_create_api_key()
    response = client.get("/api/ext/ping", headers={_HEADER: key})
    assert response.status_code == 200
    assert response.json() == {"pong": True, "secret": SECRET}


# --------------------------------------------------------------------------- #
# (3) Static pages: served unauthenticated, and they do not shadow core pages
# --------------------------------------------------------------------------- #

def test_static_page_served_without_auth_and_core_page_unchanged(
    plugin_path, key_file, clean_dashboard_env
):
    static_dir = plugin_path / "demo_site"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>demo extension</html>")

    baseline = TestClient(d.app).get("/")
    assert baseline.status_code == 200

    spec = _write_plugin(
        plugin_path,
        "plugin_static",
        f"""
        def register(ctx):
            ctx.mount_static("demo", {str(static_dir)!r})
        """,
    )
    module = _reload_dashboard(spec)
    client = TestClient(module.app)

    page = client.get("/ext/demo/index.html")
    assert page.status_code == 200
    assert page.text == "<html>demo extension</html>"

    core = client.get("/")
    assert core.status_code == baseline.status_code
    assert core.text == baseline.text


# --------------------------------------------------------------------------- #
# (4) Negative cases: fail closed, and never echo the operator's path
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", ["Bad Name", "../x", "", "UPPER", "a" * 33, "1abc"])
def test_bad_static_name_is_refused(tmp_path, name):
    directory = tmp_path / "site"
    directory.mkdir()
    ctx = DashboardContext(FastAPI())

    with pytest.raises(ValueError) as excinfo:
        ctx.mount_static(name, directory)

    assert str(directory) not in str(excinfo.value)
    assert SECRET not in str(excinfo.value)


def test_missing_directory_is_refused_without_echoing_the_path(tmp_path):
    missing = tmp_path / SECRET / "missing"
    ctx = DashboardContext(FastAPI())

    with pytest.raises(ValueError) as excinfo:
        ctx.mount_static("demo", missing)

    message = str(excinfo.value)
    assert SECRET not in message
    assert str(missing) not in message
    assert str(tmp_path) not in message


def test_a_failed_mount_does_not_poison_the_name(tmp_path):
    """A rejected mount must not consume the name for a later valid mount."""
    good = tmp_path / "good"
    good.mkdir()
    (good / "index.html").write_text("<html>good</html>")
    app = FastAPI()
    ctx = DashboardContext(app)

    with pytest.raises(ValueError):
        ctx.mount_static("demo", tmp_path / "missing")

    ctx.mount_static("demo", good)

    response = TestClient(app).get("/ext/demo/index.html")
    assert response.status_code == 200
    assert response.text == "<html>good</html>"


def test_duplicate_static_name_is_refused_and_first_mount_wins(tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    (first / "index.html").write_text("<html>first</html>")
    second = tmp_path / "second"
    second.mkdir()
    (second / "index.html").write_text("<html>second</html>")
    app = FastAPI()
    ctx = DashboardContext(app)

    ctx.mount_static("demo", first)
    with pytest.raises(ValueError):
        ctx.mount_static("demo", second)

    response = TestClient(app).get("/ext/demo/index.html")
    assert response.text == "<html>first</html>"


def test_symlink_escaping_the_static_directory_is_not_served(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET)
    inside = tmp_path / "inside"
    inside.mkdir()
    (inside / "index.html").write_text("<html>inside</html>")
    (inside / "escape.txt").symlink_to(outside / "secret.txt")

    app = FastAPI()
    DashboardContext(app).mount_static("demo", inside)
    client = TestClient(app)

    assert client.get("/ext/demo/index.html").status_code == 200
    assert client.get("/ext/demo/escape.txt").status_code == 404


# --------------------------------------------------------------------------- #
# (5) Startup fails closed
# --------------------------------------------------------------------------- #

def test_invalid_spec_raises_plugin_error(monkeypatch):
    monkeypatch.setenv(ENV, f"not-a-spec-{SECRET}")

    with pytest.raises(PluginError) as excinfo:
        mount_routes(FastAPI())

    assert SECRET not in str(excinfo.value)


def test_invalid_spec_stops_the_dashboard_import():
    """A bad spec fails startup; nothing is silently skipped."""
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(_REPO_ROOT),
        ENV: f"not-a-spec-{SECRET}",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import app.dashboard"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "PluginError" in result.stderr
    assert SECRET not in result.stderr


def test_registrar_exception_propagates_and_aborts_the_list(plugin_path, monkeypatch):
    ok_a = _write_plugin(plugin_path, "plugin_a", _ONE_PLUGIN)
    boom = _write_plugin(plugin_path, "plugin_boom", _BOOM_PLUGIN)
    ok_b = _write_plugin(plugin_path, "plugin_b", _TWO_PLUGIN)
    monkeypatch.setenv(ENV, f"{ok_a},{boom},{ok_b}")
    app = FastAPI()

    with pytest.raises(RuntimeError, match="boom"):
        mount_routes(app)

    # The list aborted: neither the raising plugin nor the ones around it
    # contributed a route.
    assert _ext_paths(app) == []


# --------------------------------------------------------------------------- #
# (6) Several plugins, in the operator's order
# --------------------------------------------------------------------------- #

def test_two_plugins_register_in_spec_order(plugin_path, monkeypatch):
    one = _write_plugin(plugin_path, "plugin_one", _ONE_PLUGIN)
    two = _write_plugin(plugin_path, "plugin_two", _TWO_PLUGIN)
    monkeypatch.setenv(ENV, f"{one},{two}")
    app = FastAPI()

    mount_routes(app)

    assert _ext_paths(app) == [f"{EXT_API_PREFIX}/one", f"{EXT_API_PREFIX}/two"]
    client = TestClient(app)
    assert client.get("/api/ext/one").json() == {"plugin": "one"}
    assert client.get("/api/ext/two").json() == {"plugin": "two"}
