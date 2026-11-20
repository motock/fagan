"""Tests for the fixed security response headers (app/dashboard_security_headers.py).

Every dashboard response must carry the anti-framing and no-sniff headers:
the index page holds the shared-secret key and the UI can trigger
state-changing requests, so the page must never render inside another
site's frame. ``install`` sets them with ``setdefault``, so a handler that
already chose its own value keeps it.

``app.dashboard`` installs the middleware at import time, so the CORS
preflight test reloads it under the CORS env var and restores it afterwards,
the same way ``test_dashboard_cors.py`` does.
"""
import importlib

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from app import auth
from app import dashboard as d
from app import dashboard_security_headers as sec
from app.dashboard_cors import CORS_ENV

_KEY_HEADER = "X-Pipeline-Api-Key"
_ORIGIN = "https://ui.example.com"
_HEADER_NAMES = sorted(sec.SECURITY_HEADERS)


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so these tests never touch the repo key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def reload_dashboard(monkeypatch):
    """Reload app.dashboard under a given CORS env value, restoring it after."""
    def _apply(value=None):
        if value is None:
            monkeypatch.delenv(CORS_ENV, raising=False)
        else:
            monkeypatch.setenv(CORS_ENV, value)
        return importlib.reload(d)

    yield _apply
    monkeypatch.delenv(CORS_ENV, raising=False)
    importlib.reload(d)


def _assert_security_headers(response):
    for name in _HEADER_NAMES:
        assert response.headers[name] == sec.SECURITY_HEADERS[name], name


# --- every response carries the fixed headers --------------------------------

@pytest.mark.parametrize("header", _HEADER_NAMES)
def test_index_response_carries_each_security_header(key_file, header):
    response = TestClient(d.app).get("/")

    assert response.status_code == 200
    assert response.headers[header] == sec.SECURITY_HEADERS[header]


def test_authenticated_api_response_carries_security_headers(key_file):
    response = TestClient(d.app).get("/api/health")

    assert response.status_code == 200
    _assert_security_headers(response)


def test_unauthenticated_api_refusal_carries_security_headers(key_file):
    client = TestClient(d.app)
    client.headers.pop(_KEY_HEADER, None)

    response = client.get("/api/health")

    assert response.status_code == 401
    _assert_security_headers(response)


def test_foreign_host_refusal_carries_security_headers(key_file):
    response = TestClient(d.app).get("/", headers={"host": "evil.example"})

    assert response.status_code == 403
    _assert_security_headers(response)


def test_static_asset_response_carries_security_headers(key_file):
    response = TestClient(d.app).get("/style.css")

    assert response.status_code == 200
    _assert_security_headers(response)


def test_content_security_policy_pins_the_frame_and_form_policy(key_file):
    response = TestClient(d.app).get("/")

    assert response.headers["Content-Security-Policy"] == (
        "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'"
    )


# --- install keeps a handler's own header (setdefault semantics) ------------

def test_install_keeps_a_value_the_handler_already_set():
    app = FastAPI()

    @app.get("/framed")
    def _framed():
        return Response("ok", headers={"X-Frame-Options": "SAMEORIGIN"})

    @app.get("/plain")
    def _plain():
        return Response("ok")

    sec.install(app)
    client = TestClient(app)

    framed = client.get("/framed")
    plain = client.get("/plain")

    assert framed.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert plain.headers["X-Frame-Options"] == sec.SECURITY_HEADERS["X-Frame-Options"]
    assert framed.headers["X-Content-Type-Options"] == sec.SECURITY_HEADERS["X-Content-Type-Options"]


# --- CORS stays outermost and preflight still answers -----------------------

def test_cors_preflight_for_allowed_origin_still_answers_200(reload_dashboard):
    module = reload_dashboard(_ORIGIN)
    client = TestClient(module.app)
    client.headers.pop(_KEY_HEADER, None)

    response = client.options(
        "/api/plans",
        headers={"Origin": _ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == _ORIGIN
