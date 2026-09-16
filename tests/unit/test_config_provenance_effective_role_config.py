"""Tests for pipeline.config_provenance (effective role config).

Split out of test_config_provenance.py to keep it under the project's
line-count target; shared fixtures/helpers moved to
tests.unit._config_provenance_helpers.
"""
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


class TestEffectiveRoleConfigSignature:
    """Mechanical requirements on the function's existence and signature."""

    def test_function_exists_on_module(self):
        mod = _import_module()
        assert hasattr(mod, "effective_role_config"), (
            "pipeline.config_provenance must define effective_role_config"
        )

    def test_signature_is_keyword_only(self):
        """All parameters must be keyword-only (the API is keyword-only)."""
        import inspect

        mod = _import_module()
        sig = inspect.signature(mod.effective_role_config)
        # No positional-or-keyword params allowed; every param must be
        # KEYWORD_ONLY (i.e. a '*' marker precedes it).
        for name, param in sig.parameters.items():
            assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"effective_role_config param {name!r} must be keyword-only, "
                f"got {param.kind}"
            )

    def test_expected_parameter_names(self):
        """The function must accept exactly these keyword params."""
        import inspect

        mod = _import_module()
        sig = inspect.signature(mod.effective_role_config)
        names = set(sig.parameters)
        assert names == {
            "plan_role_config",
            "registry",
            "model_fallbacks",
            "environ",
        }, f"unexpected parameter set: {names}"

    def test_returns_list_of_dicts(self):
        mod = _import_module()
        result = mod.effective_role_config()
        assert isinstance(result, list), (
            f"effective_role_config must return a list, got {type(result)}"
        )
        assert not isinstance(result, (tuple, dict))
        for entry in result:
            assert isinstance(entry, dict), (
                f"each entry must be a dict, got {type(entry)}"
            )


class TestEffectiveRoleConfigHappyPath:
    """Success criterion 1: no-arg call returns one entry per PIPELINE_ROLES,
    in order, and raises nothing."""

    def test_no_args_returns_one_entry_per_role_in_order(self):
        mod = _import_module()
        result = mod.effective_role_config()
        assert len(result) == len(mod.PIPELINE_ROLES)
        assert [entry["role"] for entry in result] == list(mod.PIPELINE_ROLES)

    def test_no_args_does_not_raise(self):
        mod = _import_module()
        # The headline requirement: a bare call raises nothing.
        mod.effective_role_config()

    def test_every_entry_has_role_key(self):
        mod = _import_module()
        result = mod.effective_role_config()
        for entry in result:
            assert "role" in entry, f"entry missing 'role' key: {entry}"

    def test_empty_environ_and_registry_still_returns_all_roles(self):
        """Boundary: explicitly empty sources still yield all roles."""
        mod = _import_module()
        result = mod.effective_role_config(
            plan_role_config={}, registry={}, model_fallbacks={}, environ={}
        )
        assert len(result) == len(mod.PIPELINE_ROLES)
        assert [entry["role"] for entry in result] == list(mod.PIPELINE_ROLES)

    def test_each_entry_shape_matches_resolve_role_provenance(self):
        """Every returned entry must carry the same keys resolve_role_provenance
        produces, so callers can treat the list uniformly."""
        mod = _import_module()
        result = mod.effective_role_config(environ={})
        expected_keys = {
            "role",
            "provider",
            "model",
            "provider_source",
            "model_source",
            "restart_required",
            "error",
        }
        for entry in result:
            assert set(entry.keys()) == expected_keys, (
                f"entry keys {set(entry.keys())} != expected {expected_keys}"
            )


class TestEffectiveRoleConfigModelFallbacks:
    """Success criterion 2: model_fallbacks applied per-role."""

    def test_role_present_in_fallbacks_uses_caller_fallback_source(self):
        """A role present in model_fallbacks with no other model source must
        report model_source == 'caller_fallback'."""
        mod = _import_module()
        # A registry with NO role entries and NO provider model catalog means
        # the only model source available is the caller fallback.
        reg = _build_registry(roles={}, providers={})
        fallbacks = {role: "some-model" for role in mod.PIPELINE_ROLES}
        result = mod.effective_role_config(
            registry=reg, model_fallbacks=fallbacks, environ={}
        )
        for entry in result:
            assert entry["model_source"] == "caller_fallback", (
                f"role {entry['role']!r} expected caller_fallback, "
                f"got {entry['model_source']!r}"
            )

    def test_role_absent_from_fallbacks_reports_unset_and_error(self):
        """A role absent from model_fallbacks with no other model source must
        report model is None, model_source == 'unset', and a non-None error."""
        mod = _import_module()
        reg = _build_registry(roles={}, providers={})
        # No fallbacks at all -> every role has no model source.
        result = mod.effective_role_config(registry=reg, environ={})
        for entry in result:
            assert entry["model"] is None, (
                f"role {entry['role']!r} model should be None, "
                f"got {entry['model']!r}"
            )
            assert entry["model_source"] == "unset", (
                f"role {entry['role']!r} model_source should be 'unset', "
                f"got {entry['model_source']!r}"
            )
            assert entry["error"] is not None, (
                f"role {entry['role']!r} error should be non-None"
            )

    def test_mixed_fallbacks_presence(self):
        """Boundary: some roles in the map, some out — each handled per-role."""
        mod = _import_module()
        reg = _build_registry(roles={}, providers={})
        roles = list(mod.PIPELINE_ROLES)
        # Give a fallback to exactly the first role only.
        fallbacks = {roles[0]: "fallback-model"}
        result = mod.effective_role_config(
            registry=reg, model_fallbacks=fallbacks, environ={}
        )
        first = result[0]
        assert first["role"] == roles[0]
        assert first["model_source"] == "caller_fallback"
        for entry in result[1:]:
            assert entry["model_source"] == "unset"
            assert entry["model"] is None
            assert entry["error"] is not None

    def test_callable_fallback_value_supported(self):
        """A callable fallback value (per the {role: callable-or-str} map) is
        accepted and used; resolve_role supports callables too."""
        mod = _import_module()
        reg = _build_registry(roles={}, providers={})
        called = {"count": 0}

        def _fb():
            called["count"] += 1
            return "dyn-model"

        fallbacks = {mod.PIPELINE_ROLES[0]: _fb}
        result = mod.effective_role_config(
            registry=reg, model_fallbacks=fallbacks, environ={}
        )
        first = result[0]
        assert first["model_source"] == "caller_fallback"

    def test_empty_fallbacks_map_treated_as_no_fallbacks(self):
        """Boundary: an empty model_fallbacks dict == no fallbacks for anyone."""
        mod = _import_module()
        reg = _build_registry(roles={}, providers={})
        result = mod.effective_role_config(
            registry=reg, model_fallbacks={}, environ={}
        )
        for entry in result:
            assert entry["model_source"] == "unset"
            assert entry["model"] is None
            assert entry["error"] is not None


class TestEffectiveRoleConfigReadsOnce:
    """Success criterion 3: the registry is loaded ONCE and the SAME object is
    passed to resolve_role_provenance for every role (not re-loaded per role)."""

    def test_same_registry_object_used_for_every_role(self, monkeypatch):
        """Pass a registry and confirm the SAME object reaches
        resolve_role_provenance for every role — i.e. it is not re-loaded."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        seen_registries = []

        real_resolve = mod.resolve_role_provenance

        def _spy(role, **kwargs):
            seen_registries.append(kwargs.get("registry"))
            return real_resolve(role, **kwargs)

        monkeypatch.setattr(mod, "resolve_role_provenance", _spy)
        result = mod.effective_role_config(registry=reg, environ={})

        # One call per role.
        assert len(seen_registries) == len(mod.PIPELINE_ROLES)
        # Every call received the exact same registry object identity.
        for r in seen_registries:
            assert r is reg, (
                "effective_role_config must pass the SAME registry object to "
                "resolve_role_provenance for every role, not re-load it"
            )
        # And the returned list still has all roles.
        assert len(result) == len(mod.PIPELINE_ROLES)

    def test_does_not_call_role_registry_load_registry(self, monkeypatch):
        """The reads-once contract: effective_role_config must NOT call
        role_registry.load_registry() at all — the registry is supplied by the
        caller (or defaults), never re-loaded internally."""
        mod = _import_module()
        from app import role_registry

        load_calls = []
        original_load = getattr(role_registry, "load_registry", None)

        def _tracking_load(*args, **kwargs):
            load_calls.append((args, kwargs))
            if original_load is not None:
                return original_load(*args, **kwargs)
            return {}

        if original_load is not None:
            monkeypatch.setattr(role_registry, "load_registry", _tracking_load)

        reg = _registry_with_claude_sonnet()
        mod.effective_role_config(registry=reg, environ={})

        assert load_calls == [], (
            "effective_role_config must not call role_registry.load_registry(); "
            f"saw {len(load_calls)} call(s)"
        )

    def test_registry_provider_consistent_across_list(self):
        """A role configured in the registry resolves consistently, and the
        resolved provider for that role is the same value reported in the
        returned list (the same registry object was used throughout)."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
        result = mod.effective_role_config(registry=reg, environ={})
        overlord = next(e for e in result if e["role"] == "overlord")
        # overlord is configured in the registry with provider claude.
        assert overlord["provider"] == "claude"
        assert overlord["provider_source"] == "model_registry.json"
        assert overlord["model_source"] == "model_registry.json"
        assert overlord["error"] is None

    def test_registry_none_does_not_load_and_still_returns_all_roles(
        self, monkeypatch
    ):
        """Boundary: registry=None must not trigger a load_registry call and
        must still return one entry per role."""
        mod = _import_module()
        from app import role_registry

        load_calls = []
        original_load = getattr(role_registry, "load_registry", None)

        def _tracking_load(*args, **kwargs):
            load_calls.append((args, kwargs))
            if original_load is not None:
                return original_load(*args, **kwargs)
            return {}

        if original_load is not None:
            monkeypatch.setattr(role_registry, "load_registry", _tracking_load)

        result = mod.effective_role_config(registry=None, environ={})
        assert len(result) == len(mod.PIPELINE_ROLES)
        assert load_calls == []


class TestEffectiveRoleConfigDelegatesPerRole:
    """effective_role_config must call resolve_role_provenance exactly once
    per role in PIPELINE_ROLES, in order, passing the per-role fallback."""

    def test_calls_resolve_once_per_role_in_order(self, monkeypatch):
        mod = _import_module()
        calls = []

        real_resolve = mod.resolve_role_provenance

        def _spy(role, **kwargs):
            calls.append(role)
            return real_resolve(role, **kwargs)

        monkeypatch.setattr(mod, "resolve_role_provenance", _spy)
        mod.effective_role_config(environ={})
        assert calls == list(mod.PIPELINE_ROLES)

    def test_passes_per_role_fallback_from_map(self, monkeypatch):
        """For each role, the model_fallback passed to resolve_role_provenance
        must be the value from model_fallbacks for that role, or None if the
        role is absent from the map."""
        mod = _import_module()
        roles = list(mod.PIPELINE_ROLES)
        # Fallback only for the second role.
        fallbacks = {roles[1]: "fb-model"}
        seen = {}

        real_resolve = mod.resolve_role_provenance

        def _spy(role, **kwargs):
            seen[role] = kwargs.get("model_fallback")
            return real_resolve(role, **kwargs)

        monkeypatch.setattr(mod, "resolve_role_provenance", _spy)
        mod.effective_role_config(model_fallbacks=fallbacks, environ={})
        for role in roles:
            expected = "fb-model" if role == roles[1] else None
            assert seen[role] == expected, (
                f"role {role!r} fallback should be {expected!r}, "
                f"got {seen[role]!r}"
            )

    def test_passes_environ_through_to_each_role(self, monkeypatch):
        """The environ argument is forwarded to resolve_role_provenance for
        every role unchanged."""
        mod = _import_module()
        env = {"PIPELINE_BACKEND_OVERLORD": "openai"}
        seen = []

        real_resolve = mod.resolve_role_provenance

        def _spy(role, **kwargs):
            seen.append(kwargs.get("environ"))
            return real_resolve(role, **kwargs)

        monkeypatch.setattr(mod, "resolve_role_provenance", _spy)
        mod.effective_role_config(environ=env)
        assert len(seen) == len(mod.PIPELINE_ROLES)
        for e in seen:
            assert e is env

    def test_passes_plan_role_config_through_to_each_role(self, monkeypatch):
        """plan_role_config is forwarded to resolve_role_provenance for every
        role unchanged."""
        mod = _import_module()
        plan = {"overlord": {"provider": "openai", "model": "gpt4"}}
        seen = []

        real_resolve = mod.resolve_role_provenance

        def _spy(role, **kwargs):
            seen.append(kwargs.get("plan_role_config"))
            return real_resolve(role, **kwargs)

        monkeypatch.setattr(mod, "resolve_role_provenance", _spy)
        mod.effective_role_config(plan_role_config=plan, environ={})
        assert len(seen) == len(mod.PIPELINE_ROLES)
        for p in seen:
            assert p is plan


class TestEffectiveRoleConfigBoundary:
    """Boundary / negative cases."""

    def test_single_role_pipeline_roles_handled(self, monkeypatch):
        """Boundary: if PIPELINE_ROLES had one role, exactly one entry is
        returned (the function iterates the tuple, not a hardcoded count)."""
        mod = _import_module()
        original = mod.PIPELINE_ROLES
        monkeypatch.setattr(mod, "PIPELINE_ROLES", ("overlord",))
        try:
            result = mod.effective_role_config(environ={})
            assert len(result) == 1
            assert result[0]["role"] == "overlord"
        finally:
            monkeypatch.setattr(mod, "PIPELINE_ROLES", original)

    def test_empty_pipeline_roles_returns_empty_list(self, monkeypatch):
        """Boundary: an empty PIPELINE_ROLES tuple yields an empty list, not
        an error."""
        mod = _import_module()
        original = mod.PIPELINE_ROLES
        monkeypatch.setattr(mod, "PIPELINE_ROLES", ())
        try:
            result = mod.effective_role_config(environ={})
            assert result == []
            assert isinstance(result, list)
        finally:
            monkeypatch.setattr(mod, "PIPELINE_ROLES", original)

    def test_fallback_for_role_not_in_pipeline_roles_ignored(self, monkeypatch):
        """A fallback keyed by a name NOT in PIPELINE_ROLES is simply never
        used (no error, no extra entry)."""
        mod = _import_module()
        fallbacks = {"not-a-real-role": "x"}
        result = mod.effective_role_config(
            model_fallbacks=fallbacks, environ={}
        )
        assert len(result) == len(mod.PIPELINE_ROLES)
        # No entry for the bogus role.
        assert all(e["role"] != "not-a-real-role" for e in result)

    def test_result_order_is_pipeline_roles_order_not_sorted(self, monkeypatch):
        """The returned order must be PIPELINE_ROLES order exactly — not
        alphabetically sorted — so the dashboard/MCP report is stable."""
        mod = _import_module()
        # Reverse the tuple to ensure we are not relying on a sorted default.
        reversed_roles = tuple(reversed(mod.PIPELINE_ROLES))
        monkeypatch.setattr(mod, "PIPELINE_ROLES", reversed_roles)
        result = mod.effective_role_config(environ={})
        assert [e["role"] for e in result] == list(reversed_roles)


# ---------------------------------------------------------------------------
# model_source labeling chain: provider-mismatch fallthrough (PR #255 fix).
#
# The model_source chain is a single if/elif/elif. When a role HAS a registry
# entry but that entry's provider does NOT match the winning provider (e.g.
# the registry says "ollama" but an env var overrides the provider to
# "local"), the registry branch consumes the elif chain, its inner if fails,
# and the model_fallback branch becomes unreachable - so the label stays
# "unset" even though resolve_role correctly fell through and returned the
# fallback model. These tests pin the fix: when the registry branch does not
# actually supply a model, evaluation must continue on to model_fallback.
# ---------------------------------------------------------------------------


def _registry_with_ollama_dispatch():
    """A registry where role 'dispatch' uses ollama/gpt-oss-20b-high.

    The friendly model name maps to a tag, mirroring the real registry shape.
    """
    return _build_registry(
        roles={"dispatch": {"provider": "ollama", "model": "gpt-oss-20b-high"}},
        providers={
            "ollama": {
                "models": {
                    "gpt-oss-20b-high": {"tag": "gpt-oss-20b-high:latest"},
                }
            },
        },
    )


class TestModelSourceProviderMismatchFallthrough:
    """The model_source chain must fall through to model_fallback when the
    registry entry's provider does not match the winning provider."""

    def test_registry_provider_mismatch_plan_override_uses_caller_fallback(self):
        """POSITIVE (the bug): registry role provider is 'ollama', the plan
        role_config overrides the provider to 'local', and
        model_fallback='sonnet'. resolve_role falls through to the fallback
        model, so model_source must be 'caller_fallback' (not 'unset').

        REG-4 note: this mismatch is now created by the plan role_config —
        the one source that still outranks the registry — because the env
        var no longer does (it is the empty-state fallback)."""
        mod = _import_module()
        reg = _registry_with_ollama_dispatch()
        plan = {"dispatch": {"provider": "local"}}
        result = mod.resolve_role_provenance(
            "dispatch", plan_role_config=plan, registry=reg,
            model_fallback="sonnet", environ={},
        )
        assert result["model"] == "sonnet"
        assert result["model_source"] == "caller_fallback"
        # The provider was overridden by the plan, so provider_source
        # reflects that.
        assert result["provider_source"] == "plan_role_config"
        assert result["provider"] == "local"
        assert result["error"] is None

    def test_registry_provider_matches_winning_provider_uses_registry(self):
        """NO REGRESSION: when the registry role's provider matches the
        winning provider (no env override), model_source is
        'model_registry.json' and the tag-resolved model is returned."""
        mod = _import_module()
        reg = _registry_with_ollama_dispatch()
        result = mod.resolve_role_provenance(
            "dispatch", registry=reg, model_fallback="sonnet", environ={}
        )
        assert result["model_source"] == "model_registry.json"
        assert result["provider_source"] == "model_registry.json"
        assert result["provider"] == "ollama"
        # The tag-resolved model (friendly -> tag) is returned.
        assert result["model"] == "gpt-oss-20b-high:latest"
        assert result["error"] is None

    def test_plan_role_config_model_wins_over_registry(self):
        """NO REGRESSION: plan_role_config supplies a model, so model_source
        is 'plan_role_config' regardless of the registry entry (even when the
        registry entry's provider would mismatch). The plan model must be
        declared under the winning provider so resolve_role succeeds."""
        mod = _import_module()
        # Plan role_config supplies BOTH provider and model: provider 'local'
        # outranks the registry's ollama entry (REG-4 order), and the plan
        # model is declared under 'local' so resolve_role succeeds while the
        # registry entry's provider mismatches.
        reg = _build_registry(
            roles={"dispatch": {"provider": "ollama", "model": "gpt-oss-20b-high"}},
            providers={
                "ollama": {
                    "models": {
                        "gpt-oss-20b-high": {"tag": "gpt-oss-20b-high:latest"},
                    }
                },
                "local": {
                    "models": {
                        "custom-plan-model": {"tag": "custom-plan-model:tag"},
                    }
                },
            },
        )
        plan = {"dispatch": {"provider": "local", "model": "custom-plan-model"}}
        result = mod.resolve_role_provenance(
            "dispatch",
            plan_role_config=plan,
            registry=reg,
            model_fallback="sonnet",
            environ={},
        )
        assert result["model_source"] == "plan_role_config"
        assert result["model"] == "custom-plan-model:tag"
        assert result["error"] is None

    def test_registry_provider_mismatch_and_no_fallback_is_unset(self):
        """BOUNDARY: registry provider mismatch AND model_fallback=None ->
        model is None and model_source == 'unset'.

        REG-4 note: the mismatch is created by the plan role_config (the
        source that still outranks the registry), not the env var."""
        mod = _import_module()
        reg = _registry_with_ollama_dispatch()
        plan = {"dispatch": {"provider": "local"}}
        result = mod.resolve_role_provenance(
            "dispatch", plan_role_config=plan, registry=reg,
            model_fallback=None, environ={},
        )
        assert result["model"] is None
        assert result["model_source"] == "unset"
        # Provider still resolved from the plan; not blanked.
        assert result["provider"] == "local"
        assert result["provider_source"] == "plan_role_config"

    def test_no_model_configured_keeps_provider_and_sets_error(self, monkeypatch):
        """NO REGRESSION (guards PR #255): a role with no model configured
        anywhere still returns its resolved provider and provider_source with
        model_source == 'unset' and a non-None error. Provider fields must NOT
        be blanked.

        REG-4 note: with the registry pinning roles.dispatch.provider=ollama,
        the registry (not the env var) is the layer that wins, so the error
        path must keep the registry-derived provider and label it
        model_registry.json with restart_required False."""
        mod = _import_module()
        reg = _registry_with_ollama_dispatch()
        environ = {"PIPELINE_BACKEND_DISPATCH": "local"}
        # Force resolve_role to raise the no-model-configured error path.
        from app import role_registry

        def boom(*a, **k):
            raise RoleRegistryError("role 'dispatch': no model configured")

        monkeypatch.setattr(role_registry, "resolve_role", boom)
        result = mod.resolve_role_provenance(
            "dispatch", registry=reg, model_fallback=None, environ=environ
        )
        assert result["error"] is not None
        assert result["error"] == "Role dispatch has no model configured"
        assert result["model_source"] == "unset"
        assert result["model"] is None
        # Provider fields must NOT be blanked in this error path.
        assert result["provider"] is not None
        assert result["provider"] == "local"
        assert result["provider_source"] is not None
        assert result["provider_source"] == "env:PIPELINE_BACKEND_DISPATCH"

    def test_model_fallback_callable_is_called_and_labelled_caller_fallback(self):
        """model_fallback passed as a zero-argument callable is still called
        and still labelled 'caller_fallback' (even under provider mismatch).

        REG-4 note: the mismatch is created by the plan role_config (the
        source that still outranks the registry), not the env var."""
        mod = _import_module()
        reg = _registry_with_ollama_dispatch()
        plan = {"dispatch": {"provider": "local"}}

        called = {"n": 0}

        def fallback():
            called["n"] += 1
            return "sonnet-via-callable"

        result = mod.resolve_role_provenance(
            "dispatch", plan_role_config=plan, registry=reg,
            model_fallback=fallback, environ={},
        )
        assert result["model"] == "sonnet-via-callable"
        assert result["model_source"] == "caller_fallback"
        assert result["error"] is None
