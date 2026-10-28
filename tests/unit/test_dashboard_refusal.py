"""REFUSE-01: a refusal is identifiable, and maps to 403 without the principal.

``pipeline.authorization`` returns ``{"ok": False, "error": ...}`` for a policy
refusal -- the same shape as any other service failure -- so an HTTP adapter
cannot tell "you may not" from "no such plan", and the ``error`` text carries
the caller's subject into the response body. The refusal now also carries
``forbidden`` and ``action``, and ``app.dashboard_refusal.raise_if_forbidden``
turns that into a 403 whose detail names the action only.

Every case asserts the observable contract, not the implementation: the two new
keys exist alongside the unchanged ``ok``/``error``, ordinary failures stay
ordinary, and no refusal ever names the principal in the HTTP detail.

The ``plan_dir`` fixture is copied verbatim from
``tests/unit/test_role_default_enforcement.py``: ``tests/unit/`` has no
``__init__.py``, so a sibling test module cannot be imported from.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import dashboard as d
from app.dashboard_refusal import raise_if_forbidden
from app.identity import Principal
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as psrv
from pipeline.actor import bind_principal
from pipeline.authorization import authorize, refusal_for_host_config

ENFORCE_ENV = "PIPELINE_POLICY_ENFORCE"

#: An authenticated non-operator: what an OIDC bearer token resolves to. The
#: policy allows it nothing on an empty resource, so every action must refuse.
ALICE = Principal(subject="alice", auth_method="oidc")

#: A refusal as a service operation returns one: the ``error`` names the
#: principal, which must not reach the response body.
STORY_REFUSAL = {
    "ok": False,
    "forbidden": True,
    "action": "plan:pause",
    "error": "not authorized: plan:pause on plan 'p' (principal 'alice')",
}


# --------------------------------------------------------------------------- #
# Fixtures -- copied verbatim from tests/unit/test_role_default_enforcement.py
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


def _denied_story_merge(monkeypatch) -> dict:
    """Ask for ``story:merge`` as an authenticated non-operator."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    with bind_principal(ALICE):
        return authorize("story:merge", plan_name="p", story_key="S", manifest={})


def _denied_host_config(monkeypatch, plan_dir) -> dict:
    """Ask for a host-scoped write as an authenticated non-operator."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    with bind_principal(ALICE):
        return refusal_for_host_config("config:write", "the global role defaults")


def _raised(result: dict) -> HTTPException:
    """Call the helper and return the ``HTTPException`` it raised."""
    with pytest.raises(HTTPException) as excinfo:
        raise_if_forbidden(result)
    return excinfo.value


# --------------------------------------------------------------------------- #
# authorize: the refusal is identifiable, the legacy shape is intact
# --------------------------------------------------------------------------- #
def test_denied_authorize_is_marked_forbidden(monkeypatch, plan_dir):
    assert _denied_story_merge(monkeypatch)["forbidden"] is True


def test_denied_authorize_names_the_refused_action(monkeypatch, plan_dir):
    assert _denied_story_merge(monkeypatch)["action"] == "story:merge"


def test_denied_authorize_keeps_the_error_text(monkeypatch, plan_dir):
    assert _denied_story_merge(monkeypatch)["error"].startswith(
        "not authorized: story:merge on plan 'p'"
    )


def test_local_operator_is_allowed(monkeypatch, plan_dir):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    assert authorize("story:merge", plan_name="p", story_key="S", manifest={}) is None


# --------------------------------------------------------------------------- #
# refusal_for_host_config: the same two keys on the host-scoped refusal
# --------------------------------------------------------------------------- #
def test_denied_host_config_is_marked_forbidden(monkeypatch, plan_dir):
    assert _denied_host_config(monkeypatch, plan_dir)["forbidden"] is True


def test_denied_host_config_names_the_refused_action(monkeypatch, plan_dir):
    assert _denied_host_config(monkeypatch, plan_dir)["action"] == "config:write"


# --------------------------------------------------------------------------- #
# The HTTP adapter: 403, action only, no principal
# --------------------------------------------------------------------------- #
def test_refusal_raises_403():
    assert _raised(STORY_REFUSAL).status_code == 403


def test_refusal_detail_names_the_action_only():
    assert _raised(STORY_REFUSAL).detail == "not authorized: plan:pause"


def test_refusal_detail_never_names_the_principal():
    assert "alice" not in _raised(STORY_REFUSAL).detail


def test_refusal_without_an_action_uses_a_generic_label():
    raised = _raised({"ok": False, "forbidden": True})
    assert raised.detail == "not authorized: this action"


# --------------------------------------------------------------------------- #
# Negative cases: anything that is not a refusal is passed through untouched
# --------------------------------------------------------------------------- #
def test_ordinary_failure_is_not_refused():
    assert raise_if_forbidden({"ok": False, "error": "No manifest for plan 'p'"}) is None


def test_success_is_not_refused():
    assert raise_if_forbidden({"ok": True}) is None


def test_missing_result_is_not_refused():
    assert raise_if_forbidden(None) is None
