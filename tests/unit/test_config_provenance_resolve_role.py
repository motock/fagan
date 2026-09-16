"""Tests for pipeline.config_provenance (resolve role).

Split out of test_config_provenance.py to keep it under the project's
line-count target; shared fixtures/helpers moved to
tests.unit._config_provenance_helpers.
"""
import pytest

from tests.unit._config_provenance_helpers import (  # noqa: F401
    RoleRegistryError,
    RoleResolution,
    _build_registry,
    _ensure_role_registry_imports,
    _import_module,
    _load_role_registry_imports,
    _registry_with_claude_sonnet,
    _write_json,
    _write_plist,
    _write_plist_raw,
)


class TestResolveRoleProvenancePositiveAgreement:
    """Where the old hand-rolled walk and resolve_role agree, the returned
    provider/model must match resolve_role's output exactly, and the source
    labels must still come from the local walk."""

    def test_provider_model_match_resolve_role_output(self):
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        environ = {}
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback="haiku", environ=environ
        )
        from app import role_registry

        expected = role_registry.resolve_role(
            "overlord",
            plan_role_config=None,
            registry=reg,
            model_fallback="haiku",
            environ=environ,
        )
        assert result["provider"] == expected.provider
        assert result["model"] == expected.model

    def test_provider_source_labeled_from_local_walk(self):
        """provider_source is still produced by the local walk, not by
        resolve_role (which returns no source label)."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback="haiku", environ={}
        )
        assert result["provider_source"] == "model_registry.json"
        assert result["model_source"] == "model_registry.json"
        assert result["restart_required"] is False
        assert result["error"] is None

    def test_plan_role_config_precedence_agrees(self):
        """plan_role_config provider/model wins; both resolve_role and the
        local walk agree, and the returned values equal resolve_role's."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        plan = {"overlord": {"provider": "openai", "model": "gpt4"}}
        environ = {}
        result = mod.resolve_role_provenance(
            "overlord",
            plan_role_config=plan,
            registry=reg,
            model_fallback="haiku",
            environ=environ,
        )
        from app import role_registry

        expected = role_registry.resolve_role(
            "overlord",
            plan_role_config=plan,
            registry=reg,
            model_fallback="haiku",
            environ=environ,
        )
        assert result["provider"] == expected.provider == "openai"
        assert result["model"] == expected.model == "gpt-4o"
        assert result["provider_source"] == "plan_role_config"
        assert result["model_source"] == "plan_role_config"
        assert result["error"] is None

    def test_env_provider_precedence_marks_restart_required(self):
        """An env-var provider sets restart_required True (local walk), while
        the provider/model values still come from resolve_role. Since REG-4
        the env var is the EMPTY-STATE fallback, so the registry here has no
        'overlord' entry and the env var wins the provider."""
        mod = _import_module()
        reg = _build_registry(
            roles={},
            providers={"openai": {"models": {"gpt4": {"tag": "gpt-4o"}}}},
        )
        environ = {"PIPELINE_BACKEND_OVERLORD": "openai"}
        plan = {"overlord": {"model": "gpt4"}}
        result = mod.resolve_role_provenance(
            "overlord",
            plan_role_config=plan,
            registry=reg,
            model_fallback="haiku",
            environ=environ,
        )
        from app import role_registry

        expected = role_registry.resolve_role(
            "overlord",
            plan_role_config=plan,
            registry=reg,
            model_fallback="haiku",
            environ=environ,
        )
        assert result["provider"] == expected.provider == "openai"
        assert result["model"] == expected.model == "gpt-4o"
        assert result["provider_source"] == "env:PIPELINE_BACKEND_OVERLORD"
        assert result["restart_required"] is True
        assert result["error"] is None

    def test_default_provider_when_nothing_supplied(self):
        """No plan, no env, no registry role -> default 'claude' provider,
        model from fallback. Values match resolve_role."""
        mod = _import_module()
        reg = _build_registry(
            roles={},
            providers={"claude": {"models": {"haiku": {"tag": "claude-3-5-haiku"}}}},
        )
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback="haiku", environ={}
        )
        from app import role_registry

        expected = role_registry.resolve_role(
            "overlord", registry=reg, model_fallback="haiku", environ={}
        )
        assert result["provider"] == expected.provider == "claude"
        # resolve_role uses a string model_fallback as-is (it is not resolved
        # through the provider's models->tag catalog), so the expected value
        # here is the raw fallback "haiku", not its registry tag.
        assert result["model"] == expected.model == "haiku"
        assert result["provider_source"] == "default"
        assert result["model_source"] == "caller_fallback"
        assert result["error"] is None

    def test_callable_model_fallback_agrees(self):
        """A callable model_fallback is supported by resolve_role; the
        returned model must match resolve_role's resolved tag."""
        mod = _import_module()
        reg = _build_registry(
            roles={},
            providers={"claude": {"models": {"sonnet": {"tag": "claude-3-5-sonnet"}}}},
        )
        fallback = lambda: "sonnet"
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback=fallback, environ={}
        )
        from app import role_registry

        expected = role_registry.resolve_role(
            "overlord", registry=reg, model_fallback=fallback, environ={}
        )
        # resolve_role uses a callable model_fallback's return value as-is
        # (not resolved through the provider's models->tag catalog), so the
        # expected value here is the raw fallback "sonnet", not its tag.
        assert result["model"] == expected.model == "sonnet"
        assert result["model_source"] == "caller_fallback"
        assert result["error"] is None


class TestResolveRoleProvenanceDelegationHappened:
    """Monkeypatch resolve_role to return values the hand-rolled walk could
    never produce. If resolve_role_provenance delegates, its returned
    provider/model must equal the monkeypatched values, proving the call site
    is wired in rather than merely coexisting unused."""

    def test_provider_model_follow_monkeypatched_resolve_role(self, monkeypatch):
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        forced = RoleResolution(provider="gemini", model="gemini-1.5-pro")
        from app import role_registry

        monkeypatch.setattr(role_registry, "resolve_role", lambda *a, **k: forced)
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback="haiku", environ={}
        )
        assert result["provider"] == "gemini"
        assert result["model"] == "gemini-1.5-pro"
        assert result["provider"] != "claude"
        assert result["model"] != "claude-3-5-sonnet"

    def test_delegation_passes_environ_through(self, monkeypatch):
        """resolve_role_provenance must forward its `environ` kwarg to
        resolve_role (the whole point of the environ plumbing)."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        seen = {}

        def fake_resolve_role(role, *, plan_role_config=None, model_fallback=None,
                              registry=None, default_provider="claude", environ=None):
            seen["environ"] = environ
            seen["plan_role_config"] = plan_role_config
            seen["registry"] = registry
            seen["model_fallback"] = model_fallback
            seen["default_provider"] = default_provider
            return RoleResolution(provider="claude", model="claude-3-5-sonnet")

        from app import role_registry

        monkeypatch.setattr(role_registry, "resolve_role", fake_resolve_role)
        env = {"PIPELINE_BACKEND_OVERLORD": "openai"}
        mod.resolve_role_provenance(
            "overlord",
            plan_role_config={"overlord": {"model": "sonnet"}},
            registry=reg,
            model_fallback="haiku",
            environ=env,
        )
        assert seen.get("environ") is env
        assert seen.get("plan_role_config") == {"overlord": {"model": "sonnet"}}
        assert seen.get("registry") is reg
        assert seen.get("model_fallback") == "haiku"

    def test_delegation_uses_returned_provider_not_local_value(self, monkeypatch):
        """Even when the local walk computes a provider, the returned dict's
        'provider' must be resolve_role's .provider, not the local value."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        forced = RoleResolution(provider="claude", model="claude-3-5-sonnet")
        from app import role_registry

        monkeypatch.setattr(role_registry, "resolve_role", lambda *a, **k: forced)
        result = mod.resolve_role_provenance(
            "overlord",
            plan_role_config={"overlord": {"provider": "openai", "model": "gpt4"}},
            registry=reg,
            model_fallback="haiku",
            environ={},
        )
        assert result["provider_source"] == "plan_role_config"
        assert result["provider"] == "claude"
        assert result["model"] == "claude-3-5-sonnet"
        assert result["provider"] != "openai"
        assert result["model"] != "gpt-4o"

    def test_delegation_called_exactly_once_per_invocation(self, monkeypatch):
        """resolve_role should be called once per resolve_role_provenance
        call (not zero, not repeatedly)."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        calls = []

        def fake_resolve_role(*a, **k):
            calls.append(k)
            return RoleResolution(provider="claude", model="claude-3-5-sonnet")

        from app import role_registry

        monkeypatch.setattr(role_registry, "resolve_role", fake_resolve_role)
        mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback="haiku", environ={}
        )
        assert len(calls) == 1


class TestResolveRoleProvenanceNegativeErrorCaught:
    """When resolve_role raises RoleRegistryError, resolve_role_provenance
    must NOT propagate it; it must return its existing error dict shape with
    its own unchanged wording."""

    def test_model_not_declared_error_shape_preserved(self, monkeypatch):
        """resolve_role raises 'model X not declared'; resolve_role_provenance
        returns its existing 'not declared in registry' error dict, not a
        raised exception and not resolve_role's message."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        from app import role_registry

        def boom(*a, **k):
            raise RoleRegistryError("model ghost not declared")

        monkeypatch.setattr(role_registry, "resolve_role", boom)
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback="haiku", environ={}
        )
        assert isinstance(result, dict)
        assert "error" in result
        assert "not declared in registry" in result["error"]
        assert "model ghost not declared" not in result["error"]
        assert "overlord" in result["error"]

    def test_model_not_declared_error_dict_full_shape(self, monkeypatch):
        """The error dict keeps the full pre-existing shape (all keys)."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        from app import role_registry

        def boom(*a, **k):
            raise RoleRegistryError("model ghost not declared")

        monkeypatch.setattr(role_registry, "resolve_role", boom)
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback="haiku", environ={}
        )
        for key in ("role", "provider", "model", "provider_source",
                    "model_source", "restart_required", "error"):
            assert key in result, f"missing key {key!r} in error dict"
        assert result["role"] == "overlord"
        assert result["error"] is not None
        assert result["provider"] is None
        assert result["model"] is None
        assert result["provider_source"] is None
        assert result["model_source"] is None
        assert result["restart_required"] is False

    def test_no_model_configured_error_shape_preserved(self, monkeypatch):
        """resolve_role raises 'no model configured'; resolve_role_provenance
        returns its existing 'has no model configured' error dict."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        from app import role_registry

        def boom(*a, **k):
            raise RoleRegistryError("role 'overlord': no model configured (x)")

        monkeypatch.setattr(role_registry, "resolve_role", boom)
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback=None, environ={}
        )
        assert isinstance(result, dict)
        assert "error" in result
        assert result["error"] == "Role overlord has no model configured"
        assert "role 'overlord': no model configured (x)" not in result["error"]

    def test_no_model_configured_error_dict_full_shape(self, monkeypatch):
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        from app import role_registry

        def boom(*a, **k):
            raise RoleRegistryError("role 'overlord': no model configured (x)")

        monkeypatch.setattr(role_registry, "resolve_role", boom)
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback=None, environ={}
        )
        for key in ("role", "provider", "model", "provider_source",
                    "model_source", "restart_required", "error"):
            assert key in result
        assert result["role"] == "overlord"
        assert result["model"] is None
        assert result["error"] == "Role overlord has no model configured"

    def test_role_registry_error_subclass_also_caught(self, monkeypatch):
        """A subclass of RoleRegistryError must also be caught (try/except
        catches the base, so subclasses are covered too)."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        from app import role_registry

        class SubError(RoleRegistryError):
            pass

        def boom(*a, **k):
            raise SubError("model ghost not declared")

        monkeypatch.setattr(role_registry, "resolve_role", boom)
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback="haiku", environ={}
        )
        assert isinstance(result, dict)
        assert "not declared in registry" in result["error"]

    def test_non_role_registry_error_propagates(self, monkeypatch):
        """A non-RoleRegistryError exception must NOT be swallowed; it must
        propagate (the try/except is scoped to RoleRegistryError only)."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        from app import role_registry

        def boom(*a, **k):
            raise RuntimeError("totally unrelated")

        monkeypatch.setattr(role_registry, "resolve_role", boom)
        with pytest.raises(RuntimeError, match="totally unrelated"):
            mod.resolve_role_provenance(
                "overlord", registry=reg, model_fallback="haiku", environ={}
            )


class TestResolveRoleProvenanceMechanicalRequirements:
    """Directly assert the source file contains the wiring the task requires,
    so an implementer cannot ship a green suite without it."""

    def test_resolve_role_provenance_exists(self):
        mod = _import_module()
        assert hasattr(mod, "resolve_role_provenance")

    def test_source_calls_role_registry_resolve_role(self):
        """config_provenance.py must call app.role_registry.resolve_role
        (delegation wired in, not just imported)."""
        import inspect

        mod = _import_module()
        src = inspect.getsource(mod)
        assert "resolve_role(" in src
        assert "role_registry.resolve_role" in src or (
            "from app import role_registry" in src and "resolve_role(" in src
        )

    def test_source_imports_role_registry(self):
        import inspect

        mod = _import_module()
        src = inspect.getsource(mod)
        assert "role_registry" in src

    def test_source_catches_role_registry_error(self):
        """The delegation call must be wrapped in try/except catching
        RoleRegistryError."""
        import inspect

        mod = _import_module()
        src = inspect.getsource(mod)
        assert "RoleRegistryError" in src
        assert "except" in src

    def test_source_passes_environ_to_resolve_role(self):
        """The call to resolve_role must forward environ."""
        import inspect

        mod = _import_module()
        src = inspect.getsource(mod)
        assert "environ=environ" in src or "environ = environ" in src

    def test_source_uses_returned_provider_and_model(self):
        """The success path must use resolve_role's returned .provider/.model
        (not independently re-derived values)."""
        import inspect

        mod = _import_module()
        src = inspect.getsource(mod)
        assert ".provider" in src
        assert ".model" in src

    def test_error_wording_not_changed_model_not_declared(self):
        """The 'not declared in registry' error string must be unchanged."""
        import inspect

        mod = _import_module()
        src = inspect.getsource(mod)
        assert "not declared in registry" in src

    def test_error_wording_not_changed_no_model_configured(self):
        """The 'has no model configured' error string must be unchanged."""
        import inspect

        mod = _import_module()
        src = inspect.getsource(mod)
        assert "has no model configured" in src


class TestResolveRoleProvenanceBoundaryCases:
    def test_empty_registry_and_no_fallback_raises_translated_error(self, monkeypatch):
        """Empty registry, no model_fallback: resolve_role raises
        no-model-configured; resolve_role_provenance returns the error dict."""
        mod = _import_module()
        reg = _build_registry(roles={}, providers={})
        from app import role_registry

        def boom(*a, **k):
            raise RoleRegistryError("role 'overlord': no model configured")

        monkeypatch.setattr(role_registry, "resolve_role", boom)
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback=None, environ={}
        )
        assert result["error"] == "Role overlord has no model configured"

    def test_environ_defaults_to_os_environ_when_none(self, monkeypatch):
        """When environ is None, resolve_role_provenance must default to
        os.environ (and forward that to resolve_role)."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        seen = {}

        def fake_resolve_role(role, *, plan_role_config=None, model_fallback=None,
                              registry=None, default_provider="claude", environ=None):
            seen["environ"] = environ
            return RoleResolution(provider="claude", model="claude-3-5-sonnet")

        from app import role_registry

        monkeypatch.setattr(role_registry, "resolve_role", fake_resolve_role)
        import os

        monkeypatch.setattr(os, "environ", {"PIPELINE_BACKEND_OVERLORD": "openai"})
        mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback="haiku", environ=None
        )
        assert seen["environ"] is not None
        assert seen["environ"].get("PIPELINE_BACKEND_OVERLORD") == "openai"

    def test_registry_defaults_to_empty_dict_when_none(self):
        """When registry is None, the function must not crash before reaching
        resolve_role (it defaults registry to {})."""
        mod = _import_module()
        result = mod.resolve_role_provenance(
            "overlord", registry=None, model_fallback="haiku", environ={}
        )
        assert isinstance(result, dict)
        assert "error" in result

    def test_plan_role_config_defaults_to_empty_dict_when_none(self):
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        result = mod.resolve_role_provenance(
            "overlord", plan_role_config=None, registry=reg,
            model_fallback="haiku", environ={},
        )
        assert isinstance(result, dict)
        assert result["error"] is None

    def test_returned_dict_has_all_required_keys_on_success(self):
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback="haiku", environ={}
        )
        for key in ("role", "provider", "model", "provider_source",
                    "model_source", "restart_required", "error"):
            assert key in result
        assert result["error"] is None


class TestResolveRoleProvenanceNoModelConfiguredShape:
    """The no-model-configured error path keeps the provider and its source
    label, reporting model_source as the string "unset" rather than None.

    This is a distinct diagnostic state from "nothing resolved": the role's
    provider DID resolve, only the model is absent, so a caller can still
    report where the provider came from. The downstream effective_role_config
    aggregator relies on this shape to distinguish an unconfigured-model role
    from one whose provider/model pairing is invalid.
    """

    def test_no_model_configured_keeps_provider_and_marks_model_unset(self):
        mod = _import_module()
        reg = _build_registry(roles={}, providers={})
        result = mod.resolve_role_provenance(
            "test_author", registry=reg, model_fallback=None, environ={}
        )
        assert result["provider"] == "claude"
        assert result["provider_source"] == "default"
        assert result["model"] is None
        assert result["model_source"] == "unset"
        assert result["error"] == "Role test_author has no model configured"

    def test_no_model_configured_preserves_env_provider_source(self):
        """An env-sourced provider keeps its label and restart_required even
        when the model is unresolvable."""
        mod = _import_module()
        reg = _build_registry(roles={}, providers={})
        result = mod.resolve_role_provenance(
            "overlord",
            registry=reg,
            model_fallback=None,
            environ={"PIPELINE_BACKEND_OVERLORD": "ollama"},
        )
        assert result["provider"] == "ollama"
        assert result["provider_source"] == "env:PIPELINE_BACKEND_OVERLORD"
        assert result["restart_required"] is True
        assert result["model_source"] == "unset"
        assert result["error"] is not None

    def test_model_not_declared_still_reports_no_sources(self):
        """The OTHER error path is unchanged: when a named model is not
        declared for the resolved provider, nothing resolved cleanly, so all
        source labels stay None."""
        mod = _import_module()
        reg = _build_registry(
            roles={"overlord": {"provider": "claude", "model": "ghost"}},
            providers={"claude": {"models": {}}},
        )
        result = mod.resolve_role_provenance(
            "overlord", registry=reg, model_fallback=None, environ={}
        )
        assert result["provider"] is None
        assert result["provider_source"] is None
        assert result["model_source"] is None
        assert "not declared in registry" in result["error"]


# ===========================================================================
# effective_role_config — single-call per-role diagnostic report
#
# This story adds ``effective_role_config`` to pipeline.config_provenance:
# a single call that resolves EVERY role in PIPELINE_ROLES, in order, by
# delegating to ``resolve_role_provenance`` once per role. The registry is
# loaded ONCE and threaded down via ``registry=`` (never re-loaded per role).
# The function does not exist yet on this branch, so this suite is RED.
# ===========================================================================


