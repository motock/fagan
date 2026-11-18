"""The decision path resolves layer routing once per call (hpc-06).

``pipeline/role_routing_scope.py`` is the single place that reads the team's
``role_routing`` setting for a scope. The runtime call sites on the decision
path - the overlord (``pipeline/overlord.py``), the decision service
(``pipeline/service_decisions.py``) and the role-config validation
(``pipeline/service_role_config.py``) - must carry that value to
``resolve_role`` as its OWN argument, and must resolve it ONCE per call, so a
settings change mid-call cannot split the scope across two lookups.

Every settings Store and model registry here is an in-memory stub; the live
files are never read.
"""

from __future__ import annotations

import ast
import contextlib
import types

import pytest

from pipeline import overlord
from pipeline.authorization import ENFORCE_ENV
from pipeline.service import PipelineService

SAFE_DEFAULT = "safe_default"
ROUTING = {"overlord": {"provider": "openai", "model": "gpt-4o-layer"}}
OTHER_ROUTING = {"overlord": {"provider": "anthropic", "model": "claude-layer"}}
PLAN = "hpc-06"
STORY = "HPC06-1"
MANIFEST = {
    "role_config": {},
    "namespace": "acme",
    "team": "core",
    "repo_root": "",
}
SCOPE_HELPERS = (
    "layer_routing_for_plan",
    "layer_routing_for_manifest",
    "layer_routing_kwargs",
)


class FakeStore:
    """Stand-in for the settings Store's ``effective_setting``."""

    def __init__(self):
        self.settings = {}
        self.error = None
        self.calls = []
        #: Value returned from the Nth read onwards (1-indexed), when set.
        self.changes = {}

    def put(self, value, source, *, namespace="acme", team="core", repo=""):
        self.settings[(namespace, team, repo, "role_routing")] = (value, source)

    def effective_setting(self, key, *, namespace, team, repo, org="", request=None):
        self.calls.append(
            {"key": key, "namespace": namespace, "team": team, "repo": repo}
        )
        if self.error is not None:
            raise self.error
        read_number = len(self.calls)
        if read_number in self.changes:
            value, source = self.changes[read_number]
            return {
                "key": key,
                "value": value,
                "source": source,
                "team": team,
                "repo": repo,
            }
        value, source = self.settings.get(
            (namespace, team, repo, key), ({}, SAFE_DEFAULT)
        )
        return {"key": key, "value": value, "source": source, "team": team, "repo": repo}


@pytest.fixture
def store(monkeypatch):
    from pipeline import settings_store

    fake = FakeStore()
    monkeypatch.setattr(settings_store, "effective_setting", fake.effective_setting)
    return fake


@pytest.fixture
def plan_scope(monkeypatch):
    """Point the plan scope at (acme, core) so the lookup is deterministic."""
    from pipeline import ci

    monkeypatch.setattr(
        ci,
        "settings_for_plan",
        lambda name: ci.SettingsContext(namespace="acme", team="core", repo=""),
    )


@pytest.fixture
def resolved(monkeypatch):
    """Record every ``resolve_role`` call the decision path makes."""
    from app import role_registry

    seen = []

    def fake(role, **kwargs):
        seen.append({"role": role, **kwargs})
        return types.SimpleNamespace(provider="openai", model="gpt-4o-layer")

    monkeypatch.setattr(role_registry, "resolve_role", fake)
    monkeypatch.setattr(
        role_registry,
        "load_registry",
        lambda *a, **k: {
            "providers": {"openai": {"models": {"gpt-4o": {"tag": "gpt-4o"}}}},
            "roles": {},
        },
    )
    return seen


@pytest.fixture
def seams(monkeypatch):
    """Stub the server seams the decision path reads."""
    from pipeline import server as p

    monkeypatch.setenv(ENFORCE_ENV, "0")
    monkeypatch.setattr(p, "_plan_role_config", lambda plan_name: {})
    monkeypatch.setattr(p, "_load_policy", lambda: "policy")
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(
        p, "_scoped_repo_root", lambda plan_name: contextlib.nullcontext()
    )
    monkeypatch.setattr(p, "_store", _fake_store())
    _stub_overlord_seams(monkeypatch)


def _fake_store():
    """A store that knows one plan and records nothing."""
    return types.SimpleNamespace(
        append_decision=lambda plan_name, record: None,
        get_manifest=lambda plan_name: dict(MANIFEST),
        save_manifest=lambda plan_name, manifest: None,
        transaction=_transaction,
        records=lambda: types.SimpleNamespace(
            list_plans=lambda namespace: [PLAN],
            get_manifest=lambda namespace, plan_name: dict(MANIFEST),
        ),
    )


@contextlib.contextmanager
def _transaction(plan_name, *args, **kwargs):
    yield True


def _stub_overlord_seams(monkeypatch):
    """Neutralise the persona + backend seams of the real overlord."""
    monkeypatch.setattr(overlord, "_persona_body", lambda persona: "system")
    monkeypatch.setattr(overlord, "_persona_default_model", lambda persona: None)
    monkeypatch.setattr(
        overlord.backend,
        "get_backend",
        lambda role, name=None: types.SimpleNamespace(
            complete=lambda *a, **k: "RULING: yes\nACTION: mark_done"
        ),
    )


def _routing_reads(store):
    return [c for c in store.calls if c["key"] == "role_routing"]


# ------------------------------------------------------------- overlord.py


def test_overlord_takes_layer_routing_as_its_own_argument(monkeypatch, resolved):
    """``_invoke_overlord`` accepts layer routing beside plan_role_config."""
    _stub_overlord_seams(monkeypatch)

    out = overlord._invoke_overlord(
        "prompt",
        plan_role_config={"overlord": {"provider": "anthropic"}},
        layer_routing=ROUTING,
    )

    assert "RULING" in out
    assert resolved[0]["role"] == "overlord"
    assert resolved[0]["layer_routing"] == ROUTING


def test_overlord_keeps_layer_routing_out_of_the_plan_config(monkeypatch, resolved):
    """Layer routing stays a separate argument; the plan config is untouched."""
    _stub_overlord_seams(monkeypatch)

    plan_role_config = {"overlord": {"provider": "anthropic"}}
    overlord._invoke_overlord(
        "prompt", plan_role_config=plan_role_config, layer_routing=ROUTING
    )

    assert resolved[0]["plan_role_config"] == plan_role_config
    assert "openai" not in repr(resolved[0]["plan_role_config"])


def test_overlord_without_layer_routing_keeps_its_pre_existing_shape(
    monkeypatch, resolved
):
    """An unconfigured team adds no layer routing to the resolve_role call."""
    _stub_overlord_seams(monkeypatch)

    overlord._invoke_overlord("prompt", plan_role_config={})

    assert resolved[0]["role"] == "overlord"
    assert not resolved[0].get("layer_routing")


# -------------------------------------------------------- service_decisions.py


def test_decision_resolves_the_role_against_the_plan_routing(
    store, plan_scope, resolved, seams
):
    """The decision resolves the overlord role against the plan's routing."""
    store.put(ROUTING, "team", namespace="acme", team="core", repo="")

    result = PipelineService().request_decision(PLAN, STORY, "q", ["a", "b"])

    assert result["action"] == "mark_done"
    assert resolved, "resolve_role was never called"
    assert resolved[0]["role"] == "overlord"
    assert resolved[0].get("layer_routing") == ROUTING


def test_decision_resolves_the_routing_for_the_plan_scope(
    store, plan_scope, resolved, seams
):
    """The routing is read for THIS plan's (namespace, team) scope."""
    store.put(ROUTING, "team", namespace="acme", team="core", repo="")

    PipelineService().request_decision(PLAN, STORY, "q", ["a", "b"])

    reads = _routing_reads(store)
    assert reads, "the settings Store was never read"
    assert reads[0]["namespace"] == "acme"
    assert reads[0]["team"] == "core"


def test_decision_resolves_the_routing_once_per_call(
    store, plan_scope, resolved, seams
):
    """One settings read per call: a mid-call change cannot split the scope."""
    store.put(ROUTING, "team", namespace="acme", team="core", repo="")

    PipelineService().request_decision(PLAN, STORY, "q", ["a", "b"])

    assert len(_routing_reads(store)) == 1, _routing_reads(store)
    assert resolved[0].get("layer_routing") == ROUTING


def test_decision_without_team_routing_survives(store, plan_scope, resolved, seams):
    """A team that configured nothing keeps the everyday call unchanged."""
    result = PipelineService().request_decision(PLAN, STORY, "q", ["a", "b"])

    assert result["action"] == "mark_done"
    assert _routing_reads(store), "the settings Store was never read"
    assert resolved[0].get("layer_routing") in (None, {})


def test_a_mid_call_settings_change_cannot_split_the_decision_scope(
    store, plan_scope, resolved, seams
):
    """The routing is resolved once, so a later change cannot split the scope."""
    store.put(ROUTING, "team", namespace="acme", team="core", repo="")
    # Every read after the first answers with a DIFFERENT routing value.
    store.changes[2] = (OTHER_ROUTING, "team")

    PipelineService().request_decision(PLAN, STORY, "q", ["a", "b"])

    assert len(_routing_reads(store)) == 1, _routing_reads(store)
    assert resolved[0].get("layer_routing") == ROUTING


def test_decision_survives_a_settings_store_failure(
    store, plan_scope, resolved, seams
):
    """A broken settings Store degrades to no layer routing, never an error."""
    store.error = ConnectionError("store down")

    result = PipelineService().request_decision(PLAN, STORY, "q", ["a", "b"])

    assert result["action"] == "mark_done"
    assert _routing_reads(store), "the settings Store was never read"
    assert resolved[0].get("layer_routing") in (None, {})


# ------------------------------------------------------ service_role_config.py


def test_role_config_validation_resolves_against_the_plan_routing(
    store, plan_scope, resolved, seams
):
    """The pre-save validation resolves the role against the plan's routing."""
    store.put(ROUTING, "team", namespace="acme", team="core", repo="")

    result = PipelineService().set_plan_role_config(
        PLAN, "overlord", provider="openai", model="gpt-4o"
    )

    assert result["ok"] is True
    assert resolved, "resolve_role was never called"
    assert resolved[0].get("layer_routing") == ROUTING


def test_role_config_validation_resolves_the_routing_once_per_call(
    store, plan_scope, resolved, seams
):
    """One settings read per call, so the scope cannot split mid-call."""
    store.put(ROUTING, "team", namespace="acme", team="core", repo="")

    PipelineService().set_plan_role_config(
        PLAN, "overlord", provider="openai", model="gpt-4o"
    )

    assert len(_routing_reads(store)) == 1, _routing_reads(store)


def test_role_config_validation_without_team_routing_survives(
    store, plan_scope, resolved, seams
):
    """An unconfigured team validates exactly as it did before."""
    result = PipelineService().set_plan_role_config(
        PLAN, "overlord", provider="openai", model="gpt-4o"
    )

    assert result["ok"] is True
    assert _routing_reads(store), "the settings Store was never read"
    assert resolved[0].get("layer_routing") in (None, {})


def test_role_config_validation_survives_a_settings_store_failure(
    store, plan_scope, resolved, seams
):
    """A broken settings Store degrades to no layer routing, never an error."""
    store.error = ConnectionError("store down")

    result = PipelineService().set_plan_role_config(
        PLAN, "overlord", provider="openai", model="gpt-4o"
    )

    assert result["ok"] is True
    assert _routing_reads(store), "the settings Store was never read"
    assert resolved[0].get("layer_routing") in (None, {})


# ------------------------------------------------- one lookup, no second copy


def test_scope_module_still_exports_its_three_helpers():
    """The shared scope module keeps its public surface."""
    from pipeline import role_routing_scope

    for name in SCOPE_HELPERS:
        assert callable(getattr(role_routing_scope, name))


def test_scope_helpers_add_no_keyword_for_an_unset_scope():
    """An unset scope adds no keyword at all to a resolve_role call."""
    from pipeline import role_routing_scope

    assert role_routing_scope.layer_routing_kwargs({}) == {}
    assert role_routing_scope.layer_routing_kwargs(None) == {}
    assert role_routing_scope.layer_routing_kwargs(ROUTING) == {"layer_routing": ROUTING}


def _defined_names(module):
    """Every name the module's source mentions or defines."""
    with open(module.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.alias, ast.FunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _defined_function_names(module):
    """Every function the module's source DEFINES."""
    with open(module.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


@pytest.mark.parametrize(
    "module_name",
    ["overlord", "service_decisions", "service_role_config"],
)
def test_no_in_scope_module_reimplements_the_scope_lookup(module_name):
    """The scope lookup lives in role_routing_scope alone - no second copy."""
    from pipeline import service_decisions, service_role_config

    module = {
        "overlord": overlord,
        "service_decisions": service_decisions,
        "service_role_config": service_role_config,
    }[module_name]
    defined = _defined_function_names(module)
    for helper in SCOPE_HELPERS:
        assert helper not in defined, (
            f"{module_name} defines its own {helper} instead of reusing "
            "pipeline.role_routing_scope"
        )


@pytest.mark.parametrize(
    "module_name",
    ["overlord", "service_decisions", "service_role_config"],
)
def test_in_scope_modules_do_not_read_the_settings_store_themselves(module_name):
    """No second lookup: only role_routing_scope reads the Store."""
    from pipeline import service_decisions, service_role_config

    module = {
        "overlord": overlord,
        "service_decisions": service_decisions,
        "service_role_config": service_role_config,
    }[module_name]
    names = _defined_names(module)
    assert "settings_store" not in names, (
        f"{module_name} reads the settings Store itself"
    )
    assert "effective_setting" not in names, (
        f"{module_name} reads the settings Store itself"
    )
