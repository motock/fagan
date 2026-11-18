"""Planner/decompose roles resolve through the team's ``role_routing`` (hpc-02).

Every ``resolve_role`` call inside ``pipeline/planner.py`` - the planner role
(``_run_planner`` and both rework helpers), the decompose role, and the
"sizing tier" classification the decomposer derives from the *dispatch* role -
must take the plan's layer routing as its own argument, exactly as the
dispatch path does (see ``pipeline/role_routing_scope.py``, hpc-01).

The Store and the model registry are in-memory stubs here; no live settings
file and no live ``model_registry.json`` is ever read.
"""

from __future__ import annotations

import logging

import pytest

from pipeline import ci, planner

SAFE_DEFAULT = "safe_default"
PLAN = "hpc-02"

#: Stub registry: enough providers/models for the planner's known-provider
#: check, the two decomposer tiers, and one layer-pinned model per family.
REGISTRY = {
    "providers": {
        "claude": {
            "models": {"opus": {"tag": "claude-opus-4", "class": "frontier"}},
        },
        "ollama": {
            "models": {
                "glm": {"tag": "glm-5.2:cloud", "class": "cloud"},
                "gpt-oss": {"tag": "gpt-oss:20b", "class": "local"},
            },
        },
    },
    "roles": {},
}

PLANNER_ROUTING = {"planner": {"provider": "ollama", "model": "gpt-oss"}}
DECOMPOSE_ROUTING = {
    "decompose": {"provider": "ollama", "model": "gpt-oss"},
    "dispatch": {"provider": "ollama", "model": "gpt-oss"},
}


class FakeStore:
    """Stand-in for the settings Store's ``effective_setting``."""

    def __init__(self):
        self.settings = {}
        self.error = None
        self.calls = []

    def put(self, value, source, *, namespace="acme", team="local", repo=""):
        self.settings[(namespace, team, repo, "role_routing")] = (value, source)

    def effective_setting(self, key, *, namespace, team, repo, org="", request=None):
        self.calls.append(
            {"key": key, "namespace": namespace, "team": team, "repo": repo}
        )
        if self.error is not None:
            raise self.error
        value, source = self.settings.get(
            (namespace, team, repo, key), ({}, SAFE_DEFAULT)
        )
        return {"key": key, "value": value, "source": source, "team": team, "repo": repo}


class FakeBackend:
    """Captures every ``get_backend(role, name=...)``/``complete()`` call."""

    def __init__(self, text="# checklist\n- step one"):
        self.text = text
        self.resolved = []
        self.calls = []

    def get_backend(self, role, name=None):
        self.resolved.append({"role": role, "provider": name})
        backend = self

        class _Completer:
            def complete(self, prompt, **kwargs):
                backend.calls.append({"role": role, "provider": name, **kwargs})
                return backend.text

        return _Completer()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Keep the live environment's role env vars out of the resolution."""
    for var in (
        "PIPELINE_LOCAL_PLANNER_MODEL",
        "PIPELINE_BACKEND_PLANNER",
        "PIPELINE_BACKEND_DECOMPOSE",
        "PIPELINE_BACKEND_DISPATCH",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def store(monkeypatch):
    from pipeline import settings_store

    fake = FakeStore()
    monkeypatch.setattr(settings_store, "effective_setting", fake.effective_setting)
    return fake


@pytest.fixture
def registry(monkeypatch):
    """Stub the registry *file*: the real resolve_role still runs over it."""
    monkeypatch.setattr(planner.role_registry, "load_registry", lambda: REGISTRY)


@pytest.fixture
def backend(monkeypatch):
    """Stub the backend boundary and the persona file reads."""
    fake = FakeBackend()
    monkeypatch.setattr(planner.backend, "get_backend", fake.get_backend)
    monkeypatch.setattr(planner, "_persona_body", lambda name: "PERSONA")
    monkeypatch.setattr(
        planner, "_persona_default_model", lambda name: "claude-opus-4"
    )
    return fake


@pytest.fixture
def plan_manifest(monkeypatch):
    """``layer_routing_for_plan`` loads the plan's manifest through ci."""
    monkeypatch.setattr(
        ci,
        "settings_for_plan",
        lambda name: ci.SettingsContext(namespace="acme", team="core", repo=""),
    )


@pytest.fixture
def unknown_plan(monkeypatch):
    monkeypatch.setattr(
        ci,
        "settings_for_plan",
        lambda name: ci.SettingsContext(namespace="default", team="local", repo=""),
    )


def _record_resolve(monkeypatch):
    """Record the kwargs every planner-side resolve_role receives."""
    seen = []
    real = planner.role_registry.resolve_role

    def wrapper(role, **kwargs):
        seen.append({"role": role, **kwargs})
        return real(role, **kwargs)

    monkeypatch.setattr(planner.role_registry, "resolve_role", wrapper)
    return seen


def _planner_kwargs(**extra):
    return {
        "dispatch_backend": "claude",
        "local_model": "claude-opus-4",
        **extra,
    }


# ------------------------------------------------------ planner role (3 callers)


def test_run_planner_resolves_the_planner_role_through_layer_routing(
    store, registry, backend, plan_manifest
):
    store.put(PLANNER_ROUTING, "team", namespace="acme", team="core")

    text = planner._run_planner(
        "do the thing",
        **_planner_kwargs(),
        layer_routing=_routing_for_plan(PLAN),
    )

    assert text
    assert backend.resolved == [{"role": "planner", "provider": "ollama"}]
    assert backend.calls[0]["model"] == "gpt-oss:20b"


def test_rework_planner_resolves_the_planner_role_through_layer_routing(
    store, registry, backend, plan_manifest
):
    store.put(PLANNER_ROUTING, "team", namespace="acme", team="core")

    text = planner._run_rework_planner(
        "fix the bug",
        **_planner_kwargs(),
        layer_routing=_routing_for_plan(PLAN),
    )

    assert text
    assert backend.resolved == [{"role": "planner", "provider": "ollama"}]
    assert backend.calls[0]["model"] == "gpt-oss:20b"


def test_rework_requires_new_tests_resolves_through_layer_routing(
    store, registry, backend, plan_manifest
):
    store.put(PLANNER_ROUTING, "team", namespace="acme", team="core")
    backend.text = "YES"

    assert planner._rework_requires_new_tests(
        "add a regression test",
        **_planner_kwargs(),
        layer_routing=_routing_for_plan(PLAN),
    )
    assert backend.resolved == [{"role": "planner", "provider": "ollama"}]
    assert backend.calls[0]["model"] == "gpt-oss:20b"


# ------------------------------------------------------- sizing tier (dispatch)


@pytest.mark.parametrize(
    ("routing", "tier"),
    [
        ({"dispatch": {"provider": "ollama", "model": "gpt-oss"}}, "local"),
        ({"dispatch": {"provider": "ollama", "model": "glm"}}, "cloud-oss"),
        ({"dispatch": {"provider": "claude", "model": "opus"}}, "claude"),
        ({}, "claude"),  # no routing: unchanged registry/env default
    ],
)
def test_dispatch_strength_tier_reads_the_dispatch_roles_layer_routing(
    registry, routing, tier
):
    assert planner._dispatch_strength_tier(layer_routing=routing) == tier


def test_sizing_tier_forwards_the_plan_role_config_and_routing(monkeypatch, registry):
    seen = _record_resolve(monkeypatch)
    routing = {"dispatch": {"provider": "ollama", "model": "gpt-oss"}}

    planner._dispatch_strength_tier(
        plan_role_config={"dispatch": {"provider": "ollama", "model": "glm"}},
        layer_routing=routing,
    )

    assert seen[0]["role"] == "dispatch"
    assert seen[0]["layer_routing"] == routing
    # plan role_config still wins over the layer, as resolve_role documents.
    assert seen[0]["plan_role_config"] == {
        "dispatch": {"provider": "ollama", "model": "glm"}
    }


# --------------------------------------------------------------- decompose role


def test_decompose_role_and_sizing_tier_both_use_the_layer_routing(
    store, registry, backend, plan_manifest
):
    store.put(DECOMPOSE_ROUTING, "team", namespace="acme", team="core")
    routing = _routing_for_plan(PLAN)

    text, error = planner._run_decompose_detailed("build a thing", layer_routing=routing)

    assert error is None and text
    assert backend.resolved == [{"role": "decompose", "provider": "ollama"}]
    assert backend.calls[0]["model"] == "gpt-oss:20b"
    # The sizing tier follows the *dispatch* role's layer routing (local).
    assert "Target implementer: local" in backend.calls[0]["system"]


def test_run_decompose_forwards_the_layer_routing(store, registry, backend, plan_manifest):
    store.put(DECOMPOSE_ROUTING, "team", namespace="acme", team="core")

    assert planner._run_decompose("build a thing", layer_routing=_routing_for_plan(PLAN))
    assert backend.resolved == [{"role": "decompose", "provider": "ollama"}]
    assert backend.calls[0]["model"] == "gpt-oss:20b"


# ------------------------------------------------------- unset scope: no change


def test_unset_layer_routing_adds_no_keyword_to_resolve_role(
    monkeypatch, registry, backend
):
    seen = _record_resolve(monkeypatch)

    planner._run_planner("do the thing", **_planner_kwargs(), layer_routing={})

    assert seen and "layer_routing" not in seen[0]
    # ... and the everyday resolution is unchanged.
    assert backend.resolved == [{"role": "planner", "provider": "ollama"}]
    assert backend.calls[0]["model"] == "glm-5.2:cloud"


# ------------------------------------------------------------- negatives (never
# a gate: a settings read must not break a planner/decompose call)


def test_store_failure_degrades_to_the_registry_default(
    store, registry, backend, plan_manifest, caplog
):
    store.error = ConnectionError("store down")

    with caplog.at_level(logging.WARNING):
        routing = _routing_for_plan(PLAN)
        text = planner._run_planner(
            "do the thing", **_planner_kwargs(), layer_routing=routing
        )

    assert routing == {}
    assert "store down" not in caplog.text
    assert "role_routing settings read failed" in caplog.text
    assert text
    assert backend.resolved == [{"role": "planner", "provider": "ollama"}]
    assert backend.calls[0]["model"] == "glm-5.2:cloud"


def test_missing_plan_degrades_to_the_registry_default(
    store, registry, backend, unknown_plan
):
    routing = _routing_for_plan("no-such-plan")

    assert routing == {}
    assert planner._run_decompose("build a thing", layer_routing=routing)
    assert backend.resolved == [{"role": "decompose", "provider": "claude"}]
    assert backend.calls[0]["model"] == "claude-opus-4"


def _routing_for_plan(plan_name: str):
    from pipeline.role_routing_scope import layer_routing_for_plan

    return layer_routing_for_plan(plan_name)
