"""Tests for pipeline.usage.collect_backend_status.

``collect_backend_status`` is a READ-ONLY diagnostic: for every role in
``config_provenance.PIPELINE_ROLES`` it reports which backend serves the role
and whether that backend can take work right now.  It must never raise and
must never influence dispatch gating (fail open).

Per CLAUDE.md's testing rules these tests stub the configuration source
(``role_registry.load_registry`` / ``role_registry.resolve_role``) and the
backend factory (``backend.get_backend``) with synthetic fixtures - they never
assert today's configured providers and never touch a live inference endpoint
or spawn the real ``claude`` CLI.
"""

from __future__ import annotations

import inspect

import pytest

import pipeline.usage as pipeline_usage
from pipeline.config_provenance import PIPELINE_ROLES

_UNSET = object()


# --------------------------------------------------------------------------- #
# Synthetic fixtures: fake registry, fake resolve_role, fake drivers
# --------------------------------------------------------------------------- #


class _Resolution(dict):
    """resolve_role result usable with both mapping and attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(name) from exc


class _Registry(dict):
    """Synthetic stand-in for the loaded role registry."""


class _RecordingDriver:
    """Fake driver whose resource_status() is countable and scriptable."""

    def __init__(self, provider, status=_UNSET, error=None):
        self.provider = provider
        self._status = {"ok": True, "reason": ""} if status is _UNSET else status
        self._error = error
        self.calls = 0
        self.call_args = []

    def resource_status(self, *args, **kwargs):
        self.calls += 1
        self.call_args.append((args, kwargs))
        if self._error is not None:
            raise self._error
        return self._status


def _install(
    monkeypatch,
    registry_mapping,
    *,
    default=("fake-default", "fake-default-model"),
    driver_status=_UNSET,
    driver_error=None,
    get_backend_error=None,
):
    """Stub role_registry + backend.get_backend.

    Returns ``(registry, drivers, resolve_calls, backend_calls)`` so tests can
    count ``resource_status`` invocations and inspect resolution arguments.
    """
    registry = _Registry(registry_mapping)
    drivers = {}
    resolve_calls = []
    backend_calls = []

    def fake_load_registry():
        return registry

    def fake_resolve_role(role, plan_role_config=None, registry=None, **kw):
        resolve_calls.append(
            {"role": role, "plan_role_config": plan_role_config, "registry": registry}
        )
        mapping = registry if registry is not None else {}
        if role in mapping:
            provider, model = mapping[role]
        else:
            provider, model = default
        return _Resolution({"provider": provider, "model": model})

    def fake_get_backend(role, name=None, **kw):
        backend_calls.append({"role": role, "name": name})
        if get_backend_error is not None:
            raise get_backend_error
        if name not in drivers:
            drivers[name] = _RecordingDriver(name, status=driver_status, error=driver_error)
        return drivers[name]

    monkeypatch.setattr(pipeline_usage.role_registry, "load_registry", fake_load_registry)
    monkeypatch.setattr(pipeline_usage.role_registry, "resolve_role", fake_resolve_role)
    monkeypatch.setattr(pipeline_usage.backend, "get_backend", fake_get_backend)
    return registry, drivers, resolve_calls, backend_calls


# --------------------------------------------------------------------------- #
# Existence / signature / __all__
# --------------------------------------------------------------------------- #


def test_collect_backend_status_exists_and_is_exported():
    assert callable(pipeline_usage.collect_backend_status)
    # Membership, never exact equality: __all__ is cumulative.
    assert "collect_backend_status" in pipeline_usage.__all__


def test_signature_takes_optional_plan_role_config():
    sig = inspect.signature(pipeline_usage.collect_backend_status)
    assert "plan_role_config" in sig.parameters
    assert sig.parameters["plan_role_config"].default is None


# --------------------------------------------------------------------------- #
# Positive
# --------------------------------------------------------------------------- #


def test_one_row_per_pipeline_role_in_order(monkeypatch):
    mapping = {role: ("fake", f"{role}-model") for role in PIPELINE_ROLES}
    _install(monkeypatch, mapping)
    result = pipeline_usage.collect_backend_status()
    assert isinstance(result, list)
    assert [row["role"] for row in result] == list(PIPELINE_ROLES)


def test_rows_have_exactly_the_documented_keys(monkeypatch):
    _install(monkeypatch, {role: ("fake", "m") for role in PIPELINE_ROLES})
    rows = pipeline_usage.collect_backend_status()
    assert rows
    for row in rows:
        assert isinstance(row, dict)
        assert set(row.keys()) == {"role", "provider", "model", "ok", "reason"}
        assert isinstance(row["ok"], bool)
        assert isinstance(row["reason"], str)


def test_row_provider_and_model_match_stub_registry(monkeypatch):
    mapping = {role: (f"prov-{role}", f"model-{role}") for role in PIPELINE_ROLES}
    _install(monkeypatch, mapping)
    rows = {row["role"]: row for row in pipeline_usage.collect_backend_status()}
    for role in PIPELINE_ROLES:
        assert rows[role]["provider"] == mapping[role][0]
        assert rows[role]["model"] == mapping[role][1]


def test_tripped_gate_is_reported_not_hidden(monkeypatch):
    _install(
        monkeypatch,
        {role: ("fake", "m") for role in PIPELINE_ROLES},
        driver_status={"ok": False, "reason": "gate tripped"},
    )
    rows = pipeline_usage.collect_backend_status()
    assert len(rows) == len(PIPELINE_ROLES)
    for row in rows:
        assert row["ok"] is False
        assert row["reason"] == "gate tripped"


def test_single_provider_probed_exactly_once(monkeypatch):
    mapping = {role: ("fake", "m") for role in PIPELINE_ROLES}
    _, drivers, _, _ = _install(monkeypatch, mapping)
    pipeline_usage.collect_backend_status()
    assert sum(driver.calls for driver in drivers.values()) == 1


def test_two_distinct_providers_probed_exactly_twice(monkeypatch):
    mapping = {
        role: ("alpha" if role in ("overlord", "planner") else "beta", "m")
        for role in PIPELINE_ROLES
    }
    _, drivers, _, _ = _install(monkeypatch, mapping)
    pipeline_usage.collect_backend_status()
    assert sum(driver.calls for driver in drivers.values()) == 2


def test_probe_result_reused_across_roles_on_same_provider(monkeypatch):
    mapping = {
        role: ("alpha" if role in ("overlord", "planner") else "beta", "m")
        for role in PIPELINE_ROLES
    }
    registry = _Registry(mapping)
    alpha = _RecordingDriver("alpha", status={"ok": False, "reason": "gate tripped"})
    beta = _RecordingDriver("beta", status={"ok": True, "reason": ""})

    def fake_load_registry():
        return registry

    def fake_resolve_role(role, plan_role_config=None, registry=None, **kw):
        provider, model = mapping[role]
        return _Resolution({"provider": provider, "model": model})

    def fake_get_backend(role, name=None, **kw):
        return {"alpha": alpha, "beta": beta}[name]

    monkeypatch.setattr(pipeline_usage.role_registry, "load_registry", fake_load_registry)
    monkeypatch.setattr(pipeline_usage.role_registry, "resolve_role", fake_resolve_role)
    monkeypatch.setattr(pipeline_usage.backend, "get_backend", fake_get_backend)

    rows = {row["role"]: row for row in pipeline_usage.collect_backend_status()}
    assert alpha.calls == 1
    assert beta.calls == 1
    assert rows["overlord"]["ok"] is False
    assert rows["overlord"]["reason"] == "gate tripped"
    assert rows["review"]["ok"] is True


def test_resolution_receives_plan_role_config_and_registry(monkeypatch):
    plan_cfg = {"overlord": {"provider": "x"}}
    registry, _drivers, resolve_calls, backend_calls = _install(
        monkeypatch, {role: ("fake", "m") for role in PIPELINE_ROLES}
    )
    pipeline_usage.collect_backend_status(plan_role_config=plan_cfg)
    assert len(resolve_calls) == len(PIPELINE_ROLES)
    by_role = {call["role"]: call for call in resolve_calls}
    assert by_role["overlord"]["plan_role_config"] is plan_cfg
    assert by_role["overlord"]["registry"] is registry
    # get_backend must be asked for the resolved provider by name.
    assert backend_calls
    assert all(call["name"] is not None for call in backend_calls)


def test_resource_status_called_with_no_arguments(monkeypatch):
    mapping = {role: ("fake", "m") for role in PIPELINE_ROLES}
    _, drivers, _, _ = _install(monkeypatch, mapping)
    pipeline_usage.collect_backend_status()
    for driver in drivers.values():
        assert driver.calls == 1
        for args, kwargs in driver.call_args:
            assert args == ()
            assert kwargs == {}  # in particular: no model_tag


# --------------------------------------------------------------------------- #
# Negative / boundary
# --------------------------------------------------------------------------- #


def test_driver_probe_error_fails_open(monkeypatch):
    _install(
        monkeypatch,
        {role: ("fake", "m") for role in PIPELINE_ROLES},
        driver_error=RuntimeError("probe exploded"),
    )
    rows = pipeline_usage.collect_backend_status()  # must not raise
    assert len(rows) == len(PIPELINE_ROLES)
    for row in rows:
        assert row["ok"] is True
        assert isinstance(row["reason"], str) and row["reason"]
        assert "probe exploded" in row["reason"]


def test_unknown_provider_backend_error_fails_open(monkeypatch):
    _install(
        monkeypatch,
        {role: ("ghost", "m") for role in PIPELINE_ROLES},
        get_backend_error=NotImplementedError("unknown backend 'ghost'"),
    )
    rows = pipeline_usage.collect_backend_status()  # must not raise
    assert len(rows) == len(PIPELINE_ROLES)
    for row in rows:
        assert row["ok"] is True
        assert isinstance(row["reason"], str) and row["reason"]
        assert "unknown backend 'ghost'" in row["reason"]


def test_empty_registry_still_returns_row_per_role(monkeypatch):
    _install(monkeypatch, {})
    rows = pipeline_usage.collect_backend_status()  # must not raise
    assert [row["role"] for row in rows] == list(PIPELINE_ROLES)
    for row in rows:
        assert row["provider"] == "fake-default"
        assert row["ok"] is True


@pytest.mark.parametrize(
    "bad_status",
    [None, {}, {"reason": "no ok key"}, "garbage", 42],
)
def test_malformed_driver_status_fails_open(monkeypatch, bad_status):
    _install(
        monkeypatch,
        {role: ("fake", "m") for role in PIPELINE_ROLES},
        driver_status=bad_status,
    )
    rows = pipeline_usage.collect_backend_status()  # must not raise
    assert len(rows) == len(PIPELINE_ROLES)
    for row in rows:
        assert row["ok"] is True
        assert isinstance(row["reason"], str) and row["reason"]


def test_resolution_failure_fails_open(monkeypatch):
    registry = _Registry({})

    def fake_load_registry():
        return registry

    def fake_resolve_role(role, plan_role_config=None, registry=None, **kw):
        if role == "dispatch":
            raise KeyError("dispatch missing from registry")
        return _Resolution({"provider": "fake", "model": "m"})

    monkeypatch.setattr(pipeline_usage.role_registry, "load_registry", fake_load_registry)
    monkeypatch.setattr(pipeline_usage.role_registry, "resolve_role", fake_resolve_role)
    monkeypatch.setattr(
        pipeline_usage.backend,
        "get_backend",
        lambda role, name=None, **kw: _RecordingDriver(name or "fake"),
    )

    rows = {row["role"]: row for row in pipeline_usage.collect_backend_status()}
    assert len(rows) == len(PIPELINE_ROLES)
    assert rows["dispatch"]["ok"] is True
    assert rows["dispatch"]["reason"]
    assert rows["overlord"]["provider"] == "fake"