"""Tests for the team's ``role_routing`` setting in ``collect_backend_status``.

``GET /api/usage`` builds its per-role backend rows with
``pipeline.usage.collect_backend_status()``. Those rows must resolve each role
through the same layer routing every runtime path uses, so a deployment that
keeps its routing in the settings Store (not in the registry file) reports the
routed provider and model instead of an empty pair.

The configuration source (``role_registry.load_registry``), the backend factory
(``backend.get_backend``) and the settings read (``layer_routing_for_manifest``)
are stubbed with synthetic fixtures. No test asserts today's configured
providers, and no test probes a live backend.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import backend as app_backend
from app import dashboard as d
from app import role_registry
from pipeline import actor, role_routing_scope
from pipeline import settings_store as pipeline_settings_store
from pipeline import usage as pipeline_usage
from pipeline.config_provenance import PIPELINE_ROLES
from pipeline.store_records import DEFAULT_NAMESPACE

# The shape model_registry.json uses: no per-role entries, providers carry the
# models. Routing for every role must come from the layer routing argument.
REGISTRY_WITHOUT_ROLES = {
    "roles": {},
    "providers": {"claude": {"models": {"haiku": {"tag": "haiku"}}}},
}

ROW_KEYS = {"role", "provider", "model", "ok", "reason"}


class _FakeDriver:
    def resource_status(self):
        return {"ok": True, "reason": ""}


@pytest.fixture(autouse=True)
def _no_ambient_backend_env(monkeypatch):
    """Drop PIPELINE_BACKEND_* overrides so resolution sees only the stubs."""
    for name in list(os.environ):
        if name.startswith("PIPELINE_BACKEND"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def scope(monkeypatch):
    """Stub the registry file, the backend factory and the settings read.

    ``scope["routing"]`` is what the stubbed settings read returns;
    ``scope["manifests"]`` records every manifest it was called with.
    """
    state = {"routing": {}, "manifests": [], "raise": None}

    def _layer_routing_for_manifest(manifest=None):
        state["manifests"].append(manifest)
        if state["raise"] is not None:
            raise state["raise"]
        return state["routing"]

    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: REGISTRY_WITHOUT_ROLES)
    monkeypatch.setattr(app_backend, "get_backend", lambda *a, **k: _FakeDriver())
    monkeypatch.setattr(
        role_routing_scope, "layer_routing_for_manifest", _layer_routing_for_manifest
    )
    return state


@pytest.fixture
def recording_resolve(monkeypatch):
    """Replace resolve_role with a fake that records every call's kwargs."""
    calls: list[dict] = []

    def _fake(role, **kwargs):
        calls.append({"role": role, **kwargs})
        return SimpleNamespace(provider="fake", model=f"{role}-model")

    monkeypatch.setattr(role_registry, "resolve_role", _fake)
    return calls


def _dispatch_row(rows):
    return next(row for row in rows if row["role"] == "dispatch")


# ---------------------------------------------------------------- positive


def test_should_report_provider_and_model_from_layer_routing_when_registry_has_no_roles(scope):
    scope["routing"] = {"dispatch": {"provider": "claude", "model": "haiku"}}

    row = _dispatch_row(pipeline_usage.collect_backend_status())

    assert row["provider"] == "claude"
    assert row["model"] == "haiku"
    assert row["ok"] is True
    assert row["reason"] == ""


def test_should_forward_explicit_layer_routing_to_resolve_role(recording_resolve):
    routing = {"dispatch": {"provider": "claude", "model": "haiku"}}

    pipeline_usage.collect_backend_status(layer_routing=routing)

    assert recording_resolve, "resolve_role was never called"
    for call in recording_resolve:
        assert call["layer_routing"] == routing


def test_should_read_the_bound_namespace_when_no_layer_routing_is_given(scope):
    with actor.bind_namespace("acme"):
        pipeline_usage.collect_backend_status()

    assert scope["manifests"] == [{"namespace": "acme"}]


def test_should_pass_the_settings_read_result_to_resolve_role(scope, recording_resolve):
    scope["routing"] = {"dispatch": {"provider": "claude", "model": "haiku"}}

    pipeline_usage.collect_backend_status()

    dispatch_calls = [call for call in recording_resolve if call["role"] == "dispatch"]
    assert dispatch_calls[0]["layer_routing"] == scope["routing"]


def test_should_show_the_routed_models_through_the_usage_route(
    scope, monkeypatch, tmp_path
):
    """Grades the wiring: the real collector behind GET /api/usage."""
    scope["routing"] = {"review": {"provider": "claude", "model": "haiku"}}
    monkeypatch.setattr(d, "USAGE_STATE_PATH", tmp_path / "missing_usage_state.json")

    body = TestClient(d.app).get("/api/usage").json()

    row = next(row for row in body["backends"] if row["role"] == "review")
    assert row["provider"] == "claude"
    assert row["model"] == "haiku"


# ---------------------------------------------------------------- negative


def test_should_fail_open_with_a_reason_when_nothing_configures_a_role(scope):
    rows = pipeline_usage.collect_backend_status(layer_routing={})

    row = _dispatch_row(rows)
    assert row["provider"] == ""
    assert row["model"] == ""
    assert row["ok"] is True
    assert row["reason"].startswith("role resolution failed:")


def test_should_not_read_settings_when_layer_routing_is_passed(scope):
    pipeline_usage.collect_backend_status(layer_routing={})
    pipeline_usage.collect_backend_status(layer_routing={"dispatch": {"provider": "claude"}})

    assert scope["manifests"] == []


def test_should_add_no_layer_routing_keyword_when_the_scope_has_none(scope, recording_resolve):
    scope["routing"] = {}

    pipeline_usage.collect_backend_status()

    assert recording_resolve, "resolve_role was never called"
    for call in recording_resolve:
        assert "layer_routing" not in call


def test_should_use_the_default_namespace_outside_a_request(scope):
    pipeline_usage.collect_backend_status()

    assert scope["manifests"] == [{"namespace": DEFAULT_NAMESPACE}]


def test_should_still_return_a_row_per_role_when_the_settings_read_raises(monkeypatch):
    def _raise(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: REGISTRY_WITHOUT_ROLES)
    monkeypatch.setattr(app_backend, "get_backend", lambda *a, **k: _FakeDriver())
    monkeypatch.setattr(pipeline_settings_store, "effective_setting", _raise)

    rows = pipeline_usage.collect_backend_status()

    assert len(rows) == len(PIPELINE_ROLES)
    assert [row["role"] for row in rows] == list(PIPELINE_ROLES)
    assert all(set(row) == ROW_KEYS for row in rows)
