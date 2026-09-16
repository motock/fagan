"""Regression tests: ``resolve_role_provenance`` must mirror the precedence
chain ``app.role_registry.resolve_role`` applies internally.

Reviewer finding (REG-4 follow-up): ``pipeline/config_provenance.py``'s
hand-rolled local walk still checked ``env:PIPELINE_BACKEND_<ROLE>`` BEFORE
``model_registry.json``, while ``resolve_role`` now checks the registry
first.  With a registry role entry *and* a competing env var the two
functions therefore disagreed: the walk labelled the env var as the winner
(``provider_source="env:PIPELINE_BACKEND_DISPATCH"``) and, because
``restart_required = provider_source.startswith("env:")``, reported a
spurious ``restart_required=True`` - even though the provider actually
returned came from the registry.

These tests pin the two functions' agreement for the registry-vs-env case,
plus the empty-state and plan-still-wins boundaries.  They are written
against the *correct* (post-fix) behavior, so they fail on the buggy walk
with the wrong ``provider_source`` / ``restart_required`` / ``model_source``
values rather than with an import error.
"""
from tests.unit._config_provenance_helpers import _import_module


def _registry_with_ollama_dispatch(model=None):
    """A registry whose ``roles.dispatch.provider`` is ``ollama``.

    ``providers`` declares the ollama/claude models so ``resolve_role`` can
    resolve a friendly model name to a tag when one is supplied.
    """
    role_cfg = {"provider": "ollama"}
    if model is not None:
        role_cfg["model"] = model
    return {
        "roles": {"dispatch": role_cfg},
        "providers": {
            "ollama": {"models": {"llama3": {"tag": "llama3:8b"}}},
            "claude": {"models": {"haiku": {"tag": "claude-3-5-haiku"}}},
        },
    }


def _registry_without_roles():
    """A registry with no ``roles`` block at all (the shipped empty state)."""
    return {
        "roles": {},
        "providers": {
            "ollama": {"models": {"llama3": {"tag": "llama3:8b"}}},
            "claude": {"models": {"haiku": {"tag": "claude-3-5-haiku"}}},
        },
    }


def _resolve_role(role, **kwargs):
    from app import role_registry

    return role_registry.resolve_role(role, **kwargs)


class TestRegistryOutranksEnvInProvenance:
    """registry roles.<role>.provider present + PIPELINE_BACKEND_<ROLE> set
    => the registry wins in BOTH resolve_role and resolve_role_provenance."""

    def test_provenance_agrees_with_resolve_role_when_registry_and_env_compete(self):
        mod = _import_module()
        reg = _registry_with_ollama_dispatch()
        environ = {"PIPELINE_BACKEND_DISPATCH": "local"}

        expected = _resolve_role(
            "dispatch", registry=reg, model_fallback="haiku", environ=environ
        )
        assert expected.provider == "ollama"

        prov = mod.resolve_role_provenance(
            "dispatch", registry=reg, model_fallback="haiku", environ=environ
        )

        # The provider actually returned already came from resolve_role; the
        # bug is in the *label* the local walk attaches to it.
        assert prov["provider"] == expected.provider == "ollama"
        assert prov["provider_source"] == "model_registry.json"
        assert prov["restart_required"] is False
        assert prov["error"] is None

    def test_registry_wins_even_without_a_model_fallback(self):
        """The reviewer's exact reproduction: no model_fallback, so
        resolve_role raises "no model configured" and the provenance walk's
        early-return path reports its own (wrong) provider_value directly.

        Buggy walk: provider="local", provider_source="env:PIPELINE_BACKEND_DISPATCH",
        restart_required=True.  Correct: registry wins."""
        mod = _import_module()
        reg = _registry_with_ollama_dispatch()
        environ = {"PIPELINE_BACKEND_DISPATCH": "local"}

        prov = mod.resolve_role_provenance("dispatch", registry=reg, environ=environ)

        assert prov["provider"] == "ollama"
        assert prov["provider_source"] == "model_registry.json"
        assert prov["restart_required"] is False

    def test_model_source_not_mislabeled_when_registry_wins(self):
        """The model_source branch compares the registry's provider against
        the walk's own provider_value; when the walk wrongly picked the env
        var it mislabelled the model source too."""
        mod = _import_module()
        reg = _registry_with_ollama_dispatch(model="llama3")
        environ = {"PIPELINE_BACKEND_DISPATCH": "local"}

        prov = mod.resolve_role_provenance("dispatch", registry=reg, environ=environ)

        assert prov["provider"] == "ollama"
        assert prov["provider_source"] == "model_registry.json"
        assert prov["model_source"] == "model_registry.json"
        assert prov["model"] == "llama3:8b"
        assert prov["restart_required"] is False
        assert prov["error"] is None

    def test_repeated_calls_return_the_same_correct_values(self):
        """No cross-call state: each call recomputes from config/env, so a
        second identical call must agree with the first (and with
        resolve_role)."""
        mod = _import_module()
        reg = _registry_with_ollama_dispatch()
        environ = {"PIPELINE_BACKEND_DISPATCH": "local"}

        first = mod.resolve_role_provenance(
            "dispatch", registry=reg, model_fallback="haiku", environ=environ
        )
        second = mod.resolve_role_provenance(
            "dispatch", registry=reg, model_fallback="haiku", environ=environ
        )

        assert first == second
        assert first["provider"] == "ollama"
        assert first["provider_source"] == "model_registry.json"
        assert first["restart_required"] is False


class TestProvenanceBoundaries:
    """The fix must keep plan_role_config first and the env var as the
    empty-state fallback."""

    def test_empty_registry_falls_back_to_env(self):
        """No ``roles`` block + env var set => env is the winner and a
        restart IS required (the env var is the empty-state fallback)."""
        mod = _import_module()
        reg = _registry_without_roles()
        environ = {"PIPELINE_BACKEND_DISPATCH": "local"}

        expected = _resolve_role(
            "dispatch", registry=reg, model_fallback="haiku", environ=environ
        )
        assert expected.provider == "local"

        prov = mod.resolve_role_provenance(
            "dispatch", registry=reg, model_fallback="haiku", environ=environ
        )

        assert prov["provider"] == expected.provider == "local"
        assert prov["provider_source"] == "env:PIPELINE_BACKEND_DISPATCH"
        assert prov["restart_required"] is True
        assert prov["error"] is None

    def test_plan_role_config_still_outranks_registry_and_env(self):
        """plan_role_config stays first in the walk: it beats both the
        registry entry and the env var, and needs no restart."""
        mod = _import_module()
        reg = _registry_with_ollama_dispatch()
        plan = {"dispatch": {"provider": "claude"}}
        environ = {"PIPELINE_BACKEND_DISPATCH": "local"}

        expected = _resolve_role(
            "dispatch",
            plan_role_config=plan,
            registry=reg,
            model_fallback="haiku",
            environ=environ,
        )
        assert expected.provider == "claude"

        prov = mod.resolve_role_provenance(
            "dispatch",
            plan_role_config=plan,
            registry=reg,
            model_fallback="haiku",
            environ=environ,
        )

        assert prov["provider"] == expected.provider == "claude"
        assert prov["provider_source"] == "plan_role_config"
        assert prov["restart_required"] is False
        assert prov["error"] is None
