"""Tests for d530706a: the dashboard serves its OpenAPI spec only behind auth.

On an untouched checkout ``GET /openapi.json``, ``/docs`` and ``/redoc`` all
answered 200 with no credential: the API-key middleware is scoped to the
``/api/`` path prefix, so FastAPI's three documentation endpoints — which give
away every route and schema — sat outside it, readable by anyone who could
reach the dashboard.

The contract pinned here:

* by default the spec lives at ``/api/openapi.json`` and the interactive pages
  are off, so the spec is behind the same gate as every other ``/api/`` route
  (401 without a credential);
* ``PIPELINE_DASHBOARD_PUBLIC_DOCS=1`` — and only that exact value — restores
  FastAPI's public defaults (``/openapi.json``, ``/docs``, ``/redoc``), an
  explicit operator opt-in. ``true``, ``yes``, ``0``, the empty string and
  `` 2 `` are all *not* the opt-in: a typo must fail closed;
* the Host check still runs ahead of the gate, so a foreign Host is 403;
* no credential value is ever echoed in a spec response.

``docs_kwargs`` is graded directly as well as through the app, because the
mapping it returns *is* the FastAPI configuration.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app import api_contract, auth, oidc_login
from app import dashboard as d
from app.identity import IdentityError, Principal
from app.oidc import OidcProvider

_ENV = "PIPELINE_DASHBOARD_PUBLIC_DOCS"
_SPEC_PATH = "/api/openapi.json"
_HEADER = "X-Pipeline-Api-Key"

#: What FastAPI is configured with when the spec is not public.
_CLOSED = {
    "openapi_url": _SPEC_PATH,
    "docs_url": None,
    "redoc_url": None,
}

_REDIRECT_URI = "https://dashboard.example/auth/callback"
_ISSUER = "https://issuer.example"
_GOOD_TOKEN = "good"
_SUBJECT = "https://issuer.example#alice"


def _docs_kwargs():
    """The behaviour under test, fetched without a module-level import.

    Resolved at call time so a module that does not define it yet fails an
    assertion here instead of erroring during collection.
    """
    return getattr(api_contract, "docs_kwargs", None)


def _anonymous_client(app) -> TestClient:
    """A client carrying NO credential.

    ``tests/unit/conftest.py``'s autouse ``_authenticated_test_client`` fixture
    force-attaches a valid dashboard key to every ``TestClient`` in this suite,
    so reaching the denial path means dropping the header — the idiom the other
    auth tests use. Per-request headers still win, so a test can add the key
    back explicitly.
    """
    client = TestClient(app)
    client.headers.pop(_HEADER, None)
    return client


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file, never this repo's real key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture(autouse=True)
def _restore_module_state():
    """Make each test's ``importlib.reload`` invisible to the rest of the suite.

    ``reload`` rebinds attributes on the *same* module object, so another
    module that captured ``from app.dashboard import app`` at import time would
    be left holding a stale app once this module rebuilt it. Snapshotting the
    module namespace and putting it back afterwards restores the exact original
    objects (app, identity provider, service, live refs) without a second
    reload of its own.
    """
    saved = dict(vars(d))
    yield
    vars(d).clear()
    vars(d).update(saved)


@pytest.fixture
def rebuild(monkeypatch):
    """Build a ``TestClient`` for the app as the given environment configures it.

    ``app.dashboard`` builds ``app`` at import time, so a different value needs
    a reload — and a *new* client over the reloaded module. Returning the client
    from here is the point: reloading the module while a stale local
    ``app``/``TestClient`` is still in scope would grade the previous app and
    silently keep the first configuration. ``_restore_app_identity`` puts the
    original objects back afterwards.
    """
    def _build(public_docs: str | None) -> TestClient:
        if public_docs is None:
            monkeypatch.delenv(_ENV, raising=False)
        else:
            monkeypatch.setenv(_ENV, public_docs)
        return _anonymous_client(importlib.reload(d).app)

    return _build


# --------------------------------------------------------------------------- #
# The contract constants
# --------------------------------------------------------------------------- #

def test_env_var_and_spec_path_constants():
    assert getattr(api_contract, "PUBLIC_DOCS_ENV", None) == _ENV
    assert getattr(api_contract, "SPEC_PATH", None) == _SPEC_PATH


# --------------------------------------------------------------------------- #
# Default: the spec is a /api/ resource, so it is gated
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_default_documentation_endpoints_are_404(key_file, path):
    response = _anonymous_client(d.app).get(path)

    assert response.status_code == 404


def test_default_spec_without_credential_is_401(key_file):
    response = _anonymous_client(d.app).get(_SPEC_PATH)

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_default_spec_with_valid_key_serves_the_contract(key_file):
    key = auth.get_or_create_api_key()

    response = _anonymous_client(d.app).get(_SPEC_PATH, headers={_HEADER: key})

    assert response.status_code == 200
    document = response.json()
    assert "/api/health" in document["paths"]


def test_default_spec_never_echoes_the_credential(key_file):
    key = auth.get_or_create_api_key()

    response = _anonymous_client(d.app).get(_SPEC_PATH, headers={_HEADER: key})

    assert response.status_code == 200
    assert key not in response.text


@pytest.mark.parametrize("path", ["/openapi.json", _SPEC_PATH])
def test_foreign_host_is_403_even_with_a_valid_key(key_file, path):
    key = auth.get_or_create_api_key()

    response = _anonymous_client(d.app).get(
        path, headers={_HEADER: key, "Host": "evil.example"}
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


# --------------------------------------------------------------------------- #
# The explicit operator opt-in: PUBLIC_DOCS=1
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", ["/openapi.json", "/docs"])
def test_public_docs_opt_in_restores_the_public_pages(key_file, rebuild, path):
    client = rebuild("1")

    assert client.get(path).status_code == 200


def test_public_docs_opt_in_does_not_authenticate_api_routes(key_file, rebuild):
    """Making the docs public must not make the API public."""
    client = rebuild("1")

    assert client.get("/api/health").status_code == 401


# --------------------------------------------------------------------------- #
# Every near-miss value fails closed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["true", "yes", "0", "", " 2 "])
@pytest.mark.parametrize("path", ["/openapi.json", "/docs"])
def test_non_one_values_keep_the_documentation_off(key_file, rebuild, value, path):
    client = rebuild(value)

    assert client.get(path).status_code == 404


@pytest.mark.parametrize("value", ["true", "yes", "0", "", " 2 "])
def test_non_one_values_leave_the_spec_behind_the_gate(key_file, rebuild, value):
    key = auth.get_or_create_api_key()
    client = rebuild(value)

    assert client.get(_SPEC_PATH).status_code == 401
    assert (
        client.get(_SPEC_PATH, headers={_HEADER: key}).status_code == 200
    )


def test_a_public_run_does_not_poison_the_next_decision(key_file, monkeypatch):
    """No caching: the previous environment's answer must not stick."""
    docs_kwargs = _docs_kwargs()
    assert callable(docs_kwargs), "app.api_contract.docs_kwargs is missing"

    assert docs_kwargs({_ENV: "1"}) == {}
    assert docs_kwargs({_ENV: "true"}) == _CLOSED


# --------------------------------------------------------------------------- #
# docs_kwargs: the mapping FastAPI is configured with
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["1", " 1 ", "\t1\n"])
def test_docs_kwargs_accepts_only_the_exact_one(value):
    docs_kwargs = _docs_kwargs()
    assert callable(docs_kwargs), "app.api_contract.docs_kwargs is missing"

    assert docs_kwargs(environ={_ENV: value}) == {}


@pytest.mark.parametrize("value", ["true", "yes", "0", "", " 2 ", "01", "1.0"])
def test_docs_kwargs_closes_the_spec_for_every_other_value(value):
    docs_kwargs = _docs_kwargs()
    assert callable(docs_kwargs), "app.api_contract.docs_kwargs is missing"

    assert docs_kwargs(environ={_ENV: value}) == _CLOSED


def test_docs_kwargs_closes_the_spec_when_the_variable_is_unset():
    docs_kwargs = _docs_kwargs()
    assert callable(docs_kwargs), "app.api_contract.docs_kwargs is missing"

    # An empty mapping is a valid "nothing configured", not a request to fall
    # back to os.environ (``environ or os.environ`` would get this wrong).
    assert docs_kwargs(environ={}) == _CLOSED


@pytest.mark.parametrize("public", [True, False])
def test_docs_kwargs_reads_os_environ_when_no_mapping_is_given(monkeypatch, public):
    docs_kwargs = _docs_kwargs()
    assert callable(docs_kwargs), "app.api_contract.docs_kwargs is missing"
    if public:
        monkeypatch.setenv(_ENV, "1")
    else:
        monkeypatch.delenv(_ENV, raising=False)

    assert docs_kwargs() == ({} if public else _CLOSED)


# --------------------------------------------------------------------------- #
# OIDC mode: the spec needs a session like any other /api/ route
# --------------------------------------------------------------------------- #

class _StubOidcProvider(OidcProvider):
    """An ``OidcProvider`` that accepts one token with no network I/O."""

    def __init__(self) -> None:
        self._principal = Principal(subject=_SUBJECT, auth_method="oidc")

    def authenticate(self, credential: str | None) -> Principal:
        if credential != _GOOD_TOKEN:
            raise IdentityError("invalid token")
        return self._principal


@pytest.fixture
def oidc_identity(monkeypatch):
    monkeypatch.setattr(d, "_identity", _StubOidcProvider())
    monkeypatch.setattr(
        d,
        "_oidc_login_config",
        oidc_login.OidcLoginConfig(
            enabled=True,
            client_id="pipeline-dashboard",
            redirect_uri=_REDIRECT_URI,
            issuer=_ISSUER,
        ),
    )


def test_oidc_mode_spec_without_a_session_is_401(key_file, oidc_identity):
    response = _anonymous_client(d.app).get(_SPEC_PATH)

    assert response.status_code == 401
    # The OIDC branch's refusal detail is capitalised (oidc_login.SessionRefused),
    # unlike the shared-secret gate's lowercase "unauthorized".
    assert response.json() == {"detail": "Unauthorized"}


def test_oidc_mode_spec_does_not_accept_the_shared_secret(key_file, oidc_identity):
    response = _anonymous_client(d.app).get(
        _SPEC_PATH, headers={_HEADER: auth.get_or_create_api_key()}
    )

    assert response.status_code == 401
