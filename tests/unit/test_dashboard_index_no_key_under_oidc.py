"""The dashboard index page carries no shared-secret key under OIDC.

Under ``PIPELINE_IDENTITY_PROVIDER=oidc`` the browser authenticates with its
session cookie and no ``/api/`` route accepts the shared secret, so serving
``.dashboard_api_key`` inside ``GET /`` would publish a full-control credential
for no use. These tests grade the unit (``dashboard_ui.index_response`` with
``read_key=None``) and the wiring in ``app/dashboard.py``, and keep shared-secret
mode as the negative control.
"""
import importlib
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, dashboard_ui
from app import dashboard as dashboard_module

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
PLACEHOLDER = "<!--PIPELINE_API_KEY-->"
INJECTED_NAME = "__PIPELINE_API_KEY__"
SENTINEL_KEY = "sentinel-shared-secret-7f3a9c"
BASE_URL = "http://127.0.0.1:8000"

_OIDC_ENV = {
    "PIPELINE_IDENTITY_PROVIDER": "oidc",
    "PIPELINE_OIDC_ISSUER": "https://idp.example.com/realms/x",
    "PIPELINE_OIDC_AUDIENCE": "pipeline",
    "PIPELINE_OIDC_JWKS_URL": "https://idp.example.com/jwks",
}


# --------------------------------------------------------------------------- #
# Unit: index_response with and without a key source
# --------------------------------------------------------------------------- #


def test_index_without_key_source_omits_the_injected_script():
    body = dashboard_ui.index_response(STATIC_DIR, None).body.decode()

    assert INJECTED_NAME not in body


def test_index_without_key_source_removes_the_placeholder():
    body = dashboard_ui.index_response(STATIC_DIR, None).body.decode()

    assert PLACEHOLDER not in body


def test_index_with_key_source_still_injects_the_key():
    body = dashboard_ui.index_response(STATIC_DIR, lambda: "abc").body.decode()

    assert 'window.__PIPELINE_API_KEY__="abc"' in body


def test_index_reads_the_key_once_per_request():
    reads: list[int] = []

    def read_key() -> str:
        reads.append(1)
        return "abc"

    dashboard_ui.index_response(STATIC_DIR, read_key)
    dashboard_ui.index_response(STATIC_DIR, read_key)

    assert reads == [1, 1]


# --------------------------------------------------------------------------- #
# Integration: the real app wiring, in each identity mode
# --------------------------------------------------------------------------- #


@contextmanager
def _dashboard_under(env: dict[str, str], key_path: Path):
    """Reload app.dashboard under ``env`` with the key file at ``key_path``.

    The module is reloaded again on exit, after the environment is restored, so
    other tests see the default (shared-secret) app.
    """
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("PIPELINE_IDENTITY_PROVIDER", raising=False)
            for name, value in env.items():
                mp.setenv(name, value)
            mp.setattr(auth, "API_KEY_PATH", key_path)
            yield importlib.reload(dashboard_module)
    finally:
        importlib.reload(dashboard_module)


def test_index_under_oidc_serves_no_key(tmp_path):
    key_path = tmp_path / ".dashboard_api_key"
    key_path.write_text(SENTINEL_KEY)

    with _dashboard_under(_OIDC_ENV, key_path) as module:
        response = TestClient(module.app, base_url=BASE_URL).get("/")

    assert response.status_code == 200
    assert SENTINEL_KEY not in response.text
    assert INJECTED_NAME not in response.text


def test_index_under_oidc_never_reads_the_key_store(tmp_path):
    key_path = tmp_path / ".dashboard_api_key"
    key_path.write_text(SENTINEL_KEY)
    reads: list[int] = []

    def recording_read_key() -> str:
        reads.append(1)
        return SENTINEL_KEY

    with _dashboard_under(_OIDC_ENV, key_path) as module:
        module.get_or_create_api_key = recording_read_key
        TestClient(module.app, base_url=BASE_URL).get("/")

    assert reads == []


def test_index_in_shared_secret_mode_still_serves_the_key(tmp_path):
    key_path = tmp_path / ".dashboard_api_key"
    key_path.write_text(SENTINEL_KEY)

    with _dashboard_under({}, key_path) as module:
        response = TestClient(module.app, base_url=BASE_URL).get("/")

    assert response.status_code == 200
    assert SENTINEL_KEY in response.text
