"""Tests for opt-in CORS on the dashboard API (app/dashboard_cors.py).

Cross-origin access is HEADER-credential only: ``allow_credentials`` is
``False`` and no cookie can ever be carried, because the OIDC browser session
is a ``__Host-`` cookie that is same-origin by design. CORS is opt-in -- with
``PIPELINE_DASHBOARD_CORS_ORIGINS`` unset the app installs no CORS middleware
at all (the secure default) and no response carries an
``Access-Control-Allow-Origin`` header.

``app.dashboard`` installs the middleware at import time, so each request test
sets the variable and reloads the module, then reloads it again with the
variable removed in teardown: a stale middleware stack would otherwise leak
into the other dashboard tests.
"""
import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import dashboard as d
from app.dashboard_cors import CORS_ENV, install, parse_origins

_KEY_HEADER = "X-Pipeline-Api-Key"
_ORIGIN = "https://ui.example.com"


@pytest.fixture
def reload_dashboard(monkeypatch):
    """Reload app.dashboard under a given CORS_ENV value, restoring it after."""
    def _apply(value=None):
        if value is None:
            monkeypatch.delenv(CORS_ENV, raising=False)
        else:
            monkeypatch.setenv(CORS_ENV, value)
        return importlib.reload(d)

    yield _apply
    monkeypatch.delenv(CORS_ENV, raising=False)
    importlib.reload(d)


def _credentialless(module):
    """A client with no API key: the conftest fixture force-attaches one."""
    client = TestClient(module.app)
    client.headers.pop(_KEY_HEADER, None)
    return client


def _has_cors_middleware(module):
    return any(m.cls.__name__ == "CORSMiddleware" for m in module.app.user_middleware)


# (1) Unset: the secure default, no middleware and no headers.
def test_unset_installs_no_cors_middleware(reload_dashboard):
    module = reload_dashboard(None)

    assert not _has_cors_middleware(module)
    res = TestClient(module.app).get("/api/plans", headers={"Origin": _ORIGIN})
    assert res.status_code == 200
    assert "access-control-allow-origin" not in res.headers


# (2) An allowed origin's preflight is answered without any credential.
def test_preflight_for_allowed_origin_needs_no_credential(reload_dashboard):
    module = reload_dashboard(_ORIGIN)

    res = _credentialless(module).options(
        "/api/plans",
        headers={"Origin": _ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == _ORIGIN


# (3) An actual authenticated GET is readable cross-origin, header-credentialed.
def test_actual_request_with_api_key_is_allowed(reload_dashboard):
    module = reload_dashboard(_ORIGIN)

    res = TestClient(module.app).get("/api/plans", headers={"Origin": _ORIGIN})

    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == _ORIGIN
    assert "origin" in res.headers["vary"].lower()
    assert "access-control-allow-credentials" not in res.headers


# (4) An origin outside the allowlist gets nothing.
def test_preflight_for_unknown_origin_is_not_allowed(reload_dashboard):
    module = reload_dashboard(_ORIGIN)

    res = _credentialless(module).options(
        "/api/plans",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"},
    )

    assert "access-control-allow-origin" not in res.headers


# (5) CORS must not weaken the API-key gate.
def test_api_key_gate_still_rejects_credentialless_cross_origin_get(reload_dashboard):
    module = reload_dashboard(_ORIGIN)

    res = _credentialless(module).get("/api/plans", headers={"Origin": _ORIGIN})

    assert res.status_code == 401
    # The origin may READ the refusal (so its client can show the error), but
    # nothing it sends can be authorized by a cookie: credentials stay off.
    assert "access-control-allow-credentials" not in res.headers


# (6) The correlation id is readable by the cross-origin page.
def test_correlation_id_header_is_exposed(reload_dashboard):
    module = reload_dashboard(_ORIGIN)

    res = TestClient(module.app).get("/api/plans", headers={"Origin": _ORIGIN})

    assert "x-correlation-id" in res.headers["access-control-expose-headers"].lower()


# (9) The Host check is independent of CORS: a foreign Host is still refused.
def test_foreign_host_is_refused_even_with_allowed_origin(reload_dashboard):
    module = reload_dashboard(_ORIGIN)

    res = TestClient(module.app).get(
        "/api/plans",
        headers={"Origin": _ORIGIN, "Host": "evil.example.com"},
    )

    assert res.status_code == 403


# (7) Negative parse cases: the message never echoes the rejected entry.
@pytest.mark.parametrize("bad", [
    "*",
    "https://*.example.com",
    "http://ui.example.com",
    "https://ui.example.com/path",
    "https://user@ui.example.com",
    "https://ui.example.com:0",
    "https://ui.example.com:70000",
    "ui.example.com",
    "null",
])
def test_parse_origins_rejects_invalid_entries(bad):
    with pytest.raises(ValueError) as excinfo:
        parse_origins({CORS_ENV: bad})

    assert str(excinfo.value) == f"{CORS_ENV} has an invalid entry"
    assert bad not in str(excinfo.value)


def test_parse_origins_rejects_more_than_twenty_entries():
    value = ",".join(f"https://h{i}.example.com" for i in range(21))

    with pytest.raises(ValueError) as excinfo:
        parse_origins({CORS_ENV: value})

    assert str(excinfo.value) == f"{CORS_ENV} has an invalid entry"


# (8) Accepted shapes, and duplicates collapse in first-seen order.
def test_parse_origins_unset_or_blank_is_empty():
    assert parse_origins({}) == []
    assert parse_origins({CORS_ENV: "   "}) == []


@pytest.mark.parametrize("good", [
    "https://ui.example.com",
    "https://ui.example.com:8443",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
])
def test_parse_origins_accepts_exact_origins(good):
    assert parse_origins({CORS_ENV: good}) == [good]


def test_parse_origins_strips_and_collapses_duplicates_in_order():
    value = (
        " https://ui.example.com , https://ui.example.com:8443 ,"
        " http://localhost:5173 , http://127.0.0.1:3000 , https://ui.example.com "
    )

    assert parse_origins({CORS_ENV: value}) == [
        "https://ui.example.com",
        "https://ui.example.com:8443",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
    ]


def test_install_is_a_noop_for_an_empty_list():
    app = FastAPI()
    before = len(app.user_middleware)

    install(app, {})

    assert len(app.user_middleware) == before
