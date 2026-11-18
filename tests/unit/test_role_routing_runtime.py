"""Runtime wiring for the team ``role_routing`` setting (hpc-01).

Two halves:

* ``pipeline.role_routing_scope`` - reading the setting for a manifest/plan
  without ever raising into the dispatch path.
* the ``layer_routing=`` argument every runtime ``resolve_role`` call must
  forward, so a team's routing actually applies where roles are resolved.

Every settings Store and model registry here is an in-memory stub. The live
files are never read.
"""

from __future__ import annotations

import ast
import contextlib
import logging
import types

import pytest

from pipeline import (
    advance,
    dispatch,
    dispatch_attempt,
    dispatch_routing,
    escalation,
    rebrief,
    review,
    review_orchestrator,
    test_author,
    usage,
)
from pipeline.settings_resolve import SAFE_DEFAULT_SOURCE

SAFE_DEFAULT = SAFE_DEFAULT_SOURCE
ROUTING = {"dispatch": {"provider": "openai", "model": "gpt-4o-layer"}}


class _Stop(Exception):
    """Sentinel raised by a recorder to stop the function under test."""


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


@pytest.fixture
def store(monkeypatch):
    from pipeline import settings_store

    fake = FakeStore()
    monkeypatch.setattr(settings_store, "effective_setting", fake.effective_setting)
    return fake


@pytest.fixture
def scope():
    from pipeline import role_routing_scope

    return role_routing_scope


def _recorder(monkeypatch, module, outcome):
    """Replace ``module.role_registry.resolve_role`` with a recording stub."""
    seen = []

    def fake(role, **kwargs):
        seen.append({"role": role, **kwargs})
        if callable(outcome):
            return outcome(role, kwargs)
        raise _Stop

    monkeypatch.setattr(module.role_registry, "resolve_role", fake)
    return seen


# ---------------------------------------------------------------- scope module


def test_manifest_returns_the_team_value(store, scope):
    store.put(ROUTING, "team", namespace="acme", team="core", repo="")
    manifest = {"namespace": "acme", "team": "core"}

    assert scope.layer_routing_for_manifest(manifest) == ROUTING


def test_safe_default_source_returns_empty_dict(store, scope):
    assert scope.layer_routing_for_manifest({"namespace": "acme"}) == {}


def test_safe_default_source_wins_over_truthy_value(store, scope):
    store.put({"x": 1}, SAFE_DEFAULT, namespace="acme")

    assert scope.layer_routing_for_manifest({"namespace": "acme"}) == {}


def test_store_failure_returns_empty_dict_and_logs_one_warning(store, scope, caplog):
    store.error = ConnectionError("store down")
    manifest = {"namespace": "acme", "team": "core", "repo_root": "/w/api"}

    with caplog.at_level(logging.WARNING):
        assert scope.layer_routing_for_manifest(manifest) == {}

    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1
    assert records[0].error_type == "ConnectionError"
    blob = records[0].getMessage() + repr(records[0].args)
    for leaked in ("acme", "core", "/w/api"):
        assert leaked not in blob


def test_store_failure_is_not_memoised(store, scope, caplog):
    manifest = {"namespace": "acme", "team": "core", "repo": ""}
    store.error = ConnectionError("store down")
    with caplog.at_level(logging.WARNING):
        assert scope.layer_routing_for_manifest(manifest) == {}

    store.error = None
    store.put(ROUTING, "team", namespace="acme", team="core", repo="")

    assert scope.layer_routing_for_manifest(manifest) == ROUTING


def test_unknown_plan_returns_empty_dict(store, scope, monkeypatch):
    from pipeline import ci

    monkeypatch.setattr(
        ci,
        "settings_for_plan",
        lambda name: ci.SettingsContext(namespace="default", team="local", repo=""),
    )

    assert scope.layer_routing_for_plan("no-such-plan") == {}


def test_known_plan_returns_its_value(store, scope, monkeypatch):
    from pipeline import ci

    monkeypatch.setattr(
        ci,
        "settings_for_plan",
        lambda name: ci.SettingsContext(namespace="acme", team="core", repo=""),
    )
    store.put(ROUTING, "team", namespace="acme", team="core", repo="")

    assert scope.layer_routing_for_plan("hpc-01") == ROUTING


def test_read_is_side_effect_free_and_defaults_the_context(store, scope):
    store.put(ROUTING, "namespace", namespace="acme", team="local", repo="")
    before = dict(store.settings)

    assert scope.layer_routing_for_manifest({"namespace": "acme"}) == ROUTING

    assert store.settings == before
    assert store.calls == [
        {"key": "role_routing", "namespace": "acme", "team": "local", "repo": ""}
    ]


# ------------------------------------------------------- dispatch_routing wiring


def test_resolve_dispatch_target_forwards_layer_routing(monkeypatch):
    seen = _recorder(
        monkeypatch,
        dispatch_routing,
        lambda role, kw: types.SimpleNamespace(provider="openai", model="gpt-4o-layer"),
    )
    plan_role_config = {"dispatch": {"provider": "openai"}}

    provider, model = dispatch_routing._resolve_dispatch_target(
        {}, plan_role_config=plan_role_config, layer_routing=ROUTING
    )

    assert seen[0]["layer_routing"] == ROUTING
    assert seen[0]["plan_role_config"] == plan_role_config
    assert (provider, model) == ("openai", "gpt-4o-layer")


def test_resolve_dispatch_target_does_not_merge_layers_into_plan_config(monkeypatch):
    seen = _recorder(
        monkeypatch,
        dispatch_routing,
        lambda role, kw: types.SimpleNamespace(provider="openai", model="gpt-4o-layer"),
    )

    dispatch_routing._resolve_dispatch_target({}, layer_routing=ROUTING)

    for kwargs in seen:
        plan_cfg = kwargs["plan_role_config"] or {}
        assert "layer" not in repr(plan_cfg).lower()
        assert "role_routing" not in plan_cfg


def test_dispatch_fallback_provider_forwards_layer_routing(monkeypatch):
    seen = _recorder(
        monkeypatch,
        dispatch_routing,
        lambda role, kw: types.SimpleNamespace(provider="openai", model="unpinned"),
    )

    assert (
        dispatch_routing._dispatch_fallback_provider(None, layer_routing=ROUTING)
        == "openai"
    )
    assert seen[0]["layer_routing"] == ROUTING


def test_empty_registry_retry_still_carries_layer_routing(monkeypatch):
    calls = []

    def fake(role, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise dispatch_routing.role_registry.RoleRegistryError("malformed")
        return types.SimpleNamespace(provider="openai", model="gpt-4o-layer")

    monkeypatch.setattr(dispatch_routing.role_registry, "resolve_role", fake)

    assert (
        dispatch_routing._dispatch_fallback_provider(None, layer_routing=ROUTING)
        == "openai"
    )
    assert len(calls) == 2
    assert calls[1]["registry"] == {"providers": {}, "roles": {}}
    assert calls[1]["layer_routing"] == ROUTING


# --------------------------------------------------------------- review wiring


def _review_kwargs():
    return {
        "plan_role_config": {"review": {"provider": "openai"}},
        "layer_routing": ROUTING,
    }


def _call_quietly(fn, *args, **kwargs):
    """Call ``fn``, swallowing the recorder's stop (and any later failure)."""
    with contextlib.suppress(Exception):
        fn(*args, **kwargs)


def test_run_reviewer_accepts_and_forwards_layer_routing(monkeypatch):
    seen = _recorder(monkeypatch, review, None)

    _call_quietly(review._run_reviewer, "/tmp/wt", "branch", **_review_kwargs())

    assert seen[0]["role"] == "review"
    assert seen[0]["layer_routing"] == ROUTING


def test_run_security_reviewer_accepts_and_forwards_layer_routing(monkeypatch):
    seen = _recorder(monkeypatch, review, None)
    kwargs = _review_kwargs()
    kwargs["plan_role_config"] = {"security": {"provider": "openai"}}

    _call_quietly(review._run_security_reviewer, "/tmp/wt", "branch", **kwargs)

    assert seen[0]["role"] == "security"
    assert seen[0]["layer_routing"] == ROUTING


# ------------------------------------------------------- single-site callers


def test_resolve_test_author_backend_forwards_layer_routing(monkeypatch):
    monkeypatch.setattr(
        test_author.role_registry, "load_registry", lambda *a, **k: {"roles": {}}
    )
    seen = _recorder(monkeypatch, test_author, None)

    _call_quietly(
        test_author._resolve_test_author_backend,
        "local",
        "qwen",
        plan_role_config={"test_author": {"provider": "openai"}},
        layer_routing=ROUTING,
    )

    assert seen[0]["role"] == "test_author"
    assert seen[0]["layer_routing"] == ROUTING


def test_diagnosis_role_forwards_layer_routing(monkeypatch):
    monkeypatch.setattr(
        rebrief.role_registry, "load_registry", lambda *a, **k: {"roles": {}}
    )
    seen = _recorder(monkeypatch, rebrief, None)

    _call_quietly(
        rebrief._run_diagnosis_role,
        "evidence",
        {},
        {"diagnosis": {"provider": "openai"}},
        layer_routing=ROUTING,
    )

    assert seen[0]["role"] == "diagnosis"
    assert seen[0]["layer_routing"] == ROUTING


def test_escalation_target_forwards_layer_routing(monkeypatch):
    monkeypatch.delenv("PIPELINE_ESCALATION_BACKEND", raising=False)
    monkeypatch.delenv("PIPELINE_ESCALATION_MODEL", raising=False)
    seen = _recorder(monkeypatch, escalation, None)

    _call_quietly(escalation._escalation_target, layer_routing=ROUTING)

    assert seen[0]["role"] == "dispatch"
    assert seen[0]["layer_routing"] == ROUTING


def test_role_resource_ok_forwards_layer_routing(monkeypatch):
    monkeypatch.setattr(
        usage.role_registry, "load_registry", lambda *a, **k: {"roles": {}}
    )
    seen = _recorder(monkeypatch, usage, None)

    _call_quietly(
        usage._role_resource_ok,
        "review",
        plan_role_config={"review": {"provider": "openai"}},
        layer_routing=ROUTING,
    )

    assert seen[0]["role"] == "review"
    assert seen[0]["layer_routing"] == ROUTING


def _layer_routing_keywords(module, func_name):
    """Every ``func_name(...)`` call in ``module``'s source and its kwargs.

    A call may forward the setting either literally (``layer_routing=<expr>``)
    or as ``**layer_routing_kwargs(<expr>)``, which adds the keyword only when
    the resolved scope actually set one. Both forms are reported as
    ``{"layer_routing": "<expr source>"}`` so an assertion pins the EXPRESSION
    the call site forwards, not the spelling it uses to forward it.
    """
    with open(module.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = []
    for node in ast.walk(tree):
        is_named_call = isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        if is_named_call and node.func.id == func_name:
            kwargs = {}
            for kw in node.keywords:
                if kw.arg is not None:
                    kwargs[kw.arg] = ast.unparse(kw.value)
                    continue
                value = kw.value
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "layer_routing_kwargs"
                    and len(value.args) == 1
                ):
                    kwargs["layer_routing"] = ast.unparse(value.args[0])
            found.append(kwargs)
    return found

def test_dispatch_resolves_the_plan_routing_for_its_target():
    calls = _layer_routing_keywords(dispatch, "_resolve_dispatch_target")

    assert calls, "dispatch.py no longer calls _resolve_dispatch_target"
    for kwargs in calls:
        assert kwargs.get("layer_routing") == "layer_routing_for_plan(plan_name)"


def test_advance_resolves_the_manifest_routing_for_its_targets():
    calls = _layer_routing_keywords(advance, "_resolve_dispatch_target")

    assert len(calls) >= 3
    for kwargs in calls:
        assert kwargs.get("layer_routing") == "layer_routing_for_manifest(manifest)"


# ---------------------------------------------------------------------------
# Review path: the reviewer must resolve the SAME scope its resource gate did
# ---------------------------------------------------------------------------


def _assigned_from(module, name):
    """Every expression assigned to ``name`` at any scope in ``module``'s source."""
    with open(module.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    values = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            values.append(ast.unparse(node.value))
    return values


def test_review_orchestrator_resolves_the_plans_routing_once():
    values = _assigned_from(review_orchestrator, "layer_routing")

    assert "layer_routing_for_plan(plan_name)" in values, (
        "review_orchestrator must resolve the plan's layer routing; the review "
        "resource gate (usage._role_resource_ok) honors it, so the reviewer "
        "must not be served by a different backend than the gate checked"
    )


def test_every_reviewer_call_forwards_the_layer_routing():
    for func_name in ("_run_reviewer", "_run_security_reviewer"):
        calls = _layer_routing_keywords(review_orchestrator, func_name)

        assert calls, f"review_orchestrator no longer calls {func_name}"
        for kwargs in calls:
            assert kwargs.get("layer_routing") == "layer_routing", (
                f"{func_name} called without layer_routing: a routed team's "
                "review role would silently fall through to the registry"
            )


def test_review_story_resolves_routing_before_any_reviewer_call():
    with open(review_orchestrator.__file__, encoding="utf-8") as handle:
        source = handle.read()

    resolve_idx = source.index("layer_routing = layer_routing_for_plan(plan_name)")
    first_reviewer_idx = source.index("_run_reviewer(")
    assert resolve_idx < first_reviewer_idx, (
        "the routing must be resolved before the first reviewer call"
    )


# ---------------------------------------------------------------------------
# Diagnosis path: every execution-path diagnose_failure call forwards scope
# ---------------------------------------------------------------------------


def test_dispatch_attempt_step_cap_rebrief_forwards_the_routing(monkeypatch):
    seen = []
    monkeypatch.setattr(
        dispatch_attempt, "collect_attempt_facts", lambda *a, **k: "FACTS"
    )
    monkeypatch.setattr(
        dispatch_attempt, "collect_failure_evidence", lambda *a, **k: "EVIDENCE"
    )
    monkeypatch.setattr(
        dispatch_attempt, "compose_rebriefed_instructions", lambda instr, diag: instr
    )
    monkeypatch.setattr(
        dispatch_attempt, "compose_attempt_facts", lambda instr, facts: instr
    )
    monkeypatch.setattr(
        dispatch_attempt, "detect_unsatisfiable_signal", lambda evidence: None
    )
    monkeypatch.setattr(
        dispatch_attempt,
        "diagnose_failure",
        lambda *a, **k: seen.append({"args": a, "kwargs": k}) or None,
    )
    from pipeline import role_routing_scope

    monkeypatch.setattr(
        role_routing_scope, "layer_routing_for_plan", lambda plan: ROUTING
    )

    dispatch_attempt._rebrief_step_cap_struggle(
        {}, "/w", {"diagnosis": {"provider": "openai"}},
        plan_name="hpc-01", story_key="hpc-01-a",
    )

    assert seen, "the step-cap rebrief no longer calls diagnose_failure"
    assert seen[0]["kwargs"]["layer_routing"] == ROUTING, (
        "the diagnosis role must resolve through the plan's layer routing"
    )
    assert seen[0]["args"][2] == {"diagnosis": {"provider": "openai"}}, (
        "plan_role_config must still be forwarded ahead of the routing"
    )


def test_step_cap_rebrief_pins_the_forwarded_expression():
    calls = _layer_routing_keywords(dispatch_attempt, "diagnose_failure")

    assert calls, "dispatch_attempt no longer calls diagnose_failure"
    for kwargs in calls:
        assert kwargs.get("layer_routing") == "layer_routing_for_plan(plan_name) if plan_name else {}"


def test_escalation_diagnoses_forward_the_manifest_routing():
    calls = _layer_routing_keywords(escalation, "diagnose_failure")

    assert len(calls) == 2, (
        "escalation.py has two diagnosis sites (escalation to Claude and the "
        "local fallback model); both must forward the scope"
    )
    expressions = sorted(kwargs.get("layer_routing") for kwargs in calls)
    assert expressions == ["_lr", "layer_routing_for_manifest(manifest)"], (
        "both escalation diagnoses must forward the manifest's layer routing"
    )


def test_escalation_forwarded_scope_is_resolved_from_the_manifest():
    values = _assigned_from(escalation, "_lr")

    assert "layer_routing_for_manifest(manifest)" in values, (
        "_escalate_to_claude must compute the scope it forwards from the manifest"
    )

