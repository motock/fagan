"""REFUSE-02: every refused dashboard route answers 403 and never names the principal.

``pipeline.authorization`` marks an authorization refusal with ``forbidden`` and
``action``. ``app.dashboard_refusal.raise_if_forbidden`` turns that marker into
``HTTPException(403, "not authorized: <action>")``. These tests pin that every
dashboard route whose service call can be refused calls the helper immediately
after the service call:

* each of the 11 routes answers 403 for a refusal (never 404/400/200);
* no response body contains the refused principal's subject;
* the detail names the action only;
* a non-refusal failure keeps the route's pre-existing status;
* end to end, with the real service and policy, a refused
  ``POST /api/config/roles/{role}`` leaves ``model_registry.json`` byte-identical.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import dashboard as d
from app.identity import Principal
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as psrv

_HEADER = "X-Pipeline-Api-Key"
_PLAN = "demo"
_STORY = "S"
_ROLE = "review"
_PRINCIPAL = "alice"
_REGISTRY_PATH_ENV = "PIPELINE_MODEL_REGISTRY_PATH"
_ENFORCE_ENV = "PIPELINE_POLICY_ENFORCE"


# --------------------------------------------------------------------------- #
# Fixtures and helpers -- copied from tests/unit/test_dashboard_decision_enforcement.py
# --------------------------------------------------------------------------- #
@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so these tests never read or
    create this repo's real .dashboard_api_key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


class _FixedPrincipalProvider:
    """An identity provider that authenticates every accepted credential as
    one fixed principal."""

    def __init__(self, principal):
        self._principal = principal

    def authenticate(self, credential):
        return self._principal


def _client():
    return TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})


# --------------------------------------------------------------------------- #
# The 11 refused routes
# --------------------------------------------------------------------------- #
ROUTES = [
    pytest.param(
        f"/api/plans/{_PLAN}/stories/{_STORY}/dispatch", None, "story.dispatch",
        id="dispatch",
    ),
    pytest.param(
        f"/api/plans/{_PLAN}/stories/{_STORY}/interrupt", None, "story.interrupt",
        id="interrupt",
    ),
    pytest.param(
        f"/api/plans/{_PLAN}/stories/{_STORY}/patch",
        {"agent_instructions": "x"},
        "story.patch",
        id="patch",
    ),
    pytest.param(
        f"/api/plans/{_PLAN}/stories/{_STORY}/approve_merge", None,
        "story.approve_merge",
        id="approve-merge",
    ),
    pytest.param(
        f"/api/plans/{_PLAN}/stories/{_STORY}/done", None, "story.done",
        id="done",
    ),
    pytest.param(
        f"/api/plans/{_PLAN}/ingest", None, "plan.ingest", id="ingest",
    ),
    pytest.param(
        f"/api/plans/{_PLAN}/ingest", {"overwrite": True}, "plan.ingest",
        id="ingest-overwrite",
    ),
    pytest.param(
        f"/api/config/roles/{_ROLE}",
        {"provider": "ollama", "model": "glm"},
        "config.write",
        id="set-role-default",
    ),
    pytest.param(
        f"/api/config/plans/{_PLAN}/roles/{_ROLE}",
        {"provider": "ollama", "model": "glm"},
        "config.write",
        id="set-plan-role-config",
    ),
    pytest.param(
        f"/api/plans/{_PLAN}/pause", None, "plan.pause", id="pause",
    ),
    pytest.param(
        f"/api/plans/{_PLAN}/resume", None, "plan.resume", id="resume",
    ),
]


def _refusal(action):
    return {
        "ok": False,
        "forbidden": True,
        "action": action,
        "error": f"not authorized: {action} on plan '{_PLAN}' (principal '{_PRINCIPAL}')",
    }


class _StubService:
    """A service whose every method returns one fixed result."""

    def __init__(self, result):
        self.result = result

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(*args, **kwargs):
            return self.result

        return _call


class _StubStore:
    """A store that reports one existing plan."""

    def list_manifests(self):
        return [_PLAN]


def _install_refusing_service(monkeypatch, action):
    monkeypatch.setattr(d, "_service", _StubService(_refusal(action)))
    monkeypatch.setattr(d, "_store", _StubStore())


def _post(path, payload):
    return _client().post(path, json=payload) if payload is not None else _client().post(path)


# --------------------------------------------------------------------------- #
# Every refused route answers 403
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,payload,action", ROUTES)
def test_refusal_returns_403(key_file, monkeypatch, path, payload, action):
    _install_refusing_service(monkeypatch, action)

    assert _post(path, payload).status_code == 403


# --------------------------------------------------------------------------- #
# The refusal body never names the principal
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,payload,action", ROUTES)
def test_refusal_body_does_not_name_the_principal(
    key_file, monkeypatch, path, payload, action
):
    _install_refusing_service(monkeypatch, action)

    assert _PRINCIPAL not in _post(path, payload).text


# --------------------------------------------------------------------------- #
# The refusal detail names the action only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,payload,action", ROUTES)
def test_refusal_detail_names_only_the_action(
    key_file, monkeypatch, path, payload, action
):
    _install_refusing_service(monkeypatch, action)

    assert _post(path, payload).json()["detail"] == f"not authorized: {action}"


# --------------------------------------------------------------------------- #
# A non-refusal failure keeps the route's pre-existing status
# --------------------------------------------------------------------------- #
@pytest.fixture
def ordinary_failure(monkeypatch):
    """A service failure with no ``forbidden`` key -- not a refusal."""
    monkeypatch.setattr(d, "_service", _StubService({"ok": False, "error": "No such story"}))
    monkeypatch.setattr(d, "_store", _StubStore())


def test_dispatch_ordinary_failure_still_404(key_file, ordinary_failure):
    response = _post(f"/api/plans/{_PLAN}/stories/{_STORY}/dispatch", None)

    assert response.status_code == 404


def test_patch_ordinary_failure_still_400(key_file, ordinary_failure):
    response = _post(
        f"/api/plans/{_PLAN}/stories/{_STORY}/patch", {"agent_instructions": "x"}
    )

    assert response.status_code == 400


def test_pause_ordinary_failure_still_200(key_file, ordinary_failure):
    response = _post(f"/api/plans/{_PLAN}/pause", None)

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# End to end: the real service and policy refuse a denied principal
# --------------------------------------------------------------------------- #
@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect every PLAN_DIR binding the write paths touch to one tmp dir."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(d, "PLAN_DIR", directory)
    monkeypatch.setattr(psrv, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    """Point the model registry at a tmp file so writes never touch the real
    repo model_registry.json."""
    path = tmp_path / "registry.json"
    monkeypatch.setenv(_REGISTRY_PATH_ENV, str(path))
    return path


@pytest.fixture
def seeded_registry(registry_path):
    """A registry with two providers and a couple of declared roles."""
    registry = {
        "providers": {
            "claude": {
                "models": {
                    "opus": {"tag": "opus"},
                    "sonnet": {"tag": "sonnet"},
                    "haiku": {"tag": "haiku"},
                }
            },
            "ollama": {
                "models": {
                    "glm": {"tag": "glm-5.2:cloud"},
                    "devstral": {"tag": "devstral:24b"},
                }
            },
        },
        "roles": {
            "review": {"provider": "ollama", "model": "glm"},
            "planner": {"provider": "claude", "model": "sonnet"},
        },
    }
    registry_path.write_text(json.dumps(registry, indent=2))
    return registry


@pytest.fixture
def alice_principal(monkeypatch):
    """Authenticate every dashboard request as a principal the policy denies."""
    provider = _FixedPrincipalProvider(
        Principal(subject=_PRINCIPAL, auth_method="oidc")
    )
    monkeypatch.setattr(d, "_identity", provider)
    return provider


def _set_review_role_default():
    return _client().post(
        f"/api/config/roles/{_ROLE}",
        json={"provider": "ollama", "model": "devstral"},
    )


def test_real_policy_refusal_is_403(
    key_file, plan_dir, seeded_registry, alice_principal, monkeypatch
):
    monkeypatch.delenv(_ENFORCE_ENV, raising=False)

    assert _set_review_role_default().status_code == 403


def test_real_policy_refusal_leaves_registry_byte_identical(
    key_file, plan_dir, seeded_registry, registry_path, alice_principal, monkeypatch
):
    monkeypatch.delenv(_ENFORCE_ENV, raising=False)
    before = registry_path.read_bytes()

    _set_review_role_default()
    _set_review_role_default()

    assert registry_path.read_bytes() == before
