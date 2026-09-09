"""Tests for pipeline.config_provenance (resolve env var).

Split out of test_config_provenance.py to keep it under the project's
line-count target; shared fixtures/helpers moved to
tests.unit._config_provenance_helpers.
"""
import re as _re
from pathlib import Path

import pytest

from tests.unit._config_provenance_helpers import (  # noqa: F401
    RoleRegistryError,
    RoleResolution,
    _ensure_role_registry_imports,
    _import_module,
    _load_role_registry_imports,
    _write_json,
    _write_plist,
    _write_plist_raw,
)

# ---------------------------------------------------------------------------
# Story 2 (W3a): env-var provenance catalog, resolution, and effective config.
# These tests target the NEW API added in story 2:
#   EnvVarSpec, ENV_VAR_CATALOG, _is_secret, resolve_env_var, effective_env_config.
# The implementation does not exist yet on this branch, so these tests are
# intentionally RED until a follow-up dispatch implements them.
# ---------------------------------------------------------------------------

# The six extra vars (read in other modules) that the catalog must include
# with these exact string defaults.
_EXTRA_CATALOG_VARS = {
    "PIPELINE_BACKEND_DISPATCH": "claude",
    "PIPELINE_LOCAL_PROVIDER": "ollama",
    "PIPELINE_LOCAL_MAX_STEPS": "40",
    "PIPELINE_LOCAL_NUM_CTX": "16384",
    "PIPELINE_LOCAL_TEMPERATURE": "0.3",
    "PIPELINE_LOCAL_MODEL_DEFAULT": "devstral:24b",
}


class TestEnvVarSpec:
    def test_is_frozen_dataclass(self):
        import dataclasses

        mod = _import_module()
        EnvVarSpec = mod.EnvVarSpec
        assert dataclasses.is_dataclass(EnvVarSpec)
        # frozen=True: assigning should raise FrozenInstanceError.
        spec = EnvVarSpec(name="X", default="40")
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "Y"  # type: ignore[misc]

    def test_fields_are_name_and_default(self):
        import dataclasses

        mod = _import_module()
        EnvVarSpec = mod.EnvVarSpec
        names = [f.name for f in dataclasses.fields(EnvVarSpec)]
        assert names == ["name", "default"]

    def test_default_may_be_none(self):
        mod = _import_module()
        EnvVarSpec = mod.EnvVarSpec
        spec = EnvVarSpec(name="X", default=None)
        assert spec.name == "X"
        assert spec.default is None


class TestEnvVarCatalog:
    def test_is_tuple_of_envvarspec(self):
        mod = _import_module()
        catalog = mod.ENV_VAR_CATALOG
        assert isinstance(catalog, tuple)
        assert len(catalog) > 0
        for spec in catalog:
            assert isinstance(spec, mod.EnvVarSpec)

    def test_names_are_unique(self):
        mod = _import_module()
        names = [s.name for s in mod.ENV_VAR_CATALOG]
        assert len(names) == len(set(names)), "duplicate names in catalog"

    def test_six_extra_vars_present_with_exact_defaults(self):
        mod = _import_module()
        by_name = {s.name: s.default for s in mod.ENV_VAR_CATALOG}
        for name, default in _EXTRA_CATALOG_VARS.items():
            assert name in by_name, f"{name} missing from catalog"
            assert by_name[name] == default, (
                f"{name} default mismatch: expected {default!r}, got {by_name[name]!r}"
            )

    def test_defaults_are_strings_or_none_not_coerced(self):
        mod = _import_module()
        for spec in mod.ENV_VAR_CATALOG:
            assert spec.default is None or isinstance(spec.default, str), (
                f"{spec.name} default must be str|None, got {type(spec.default).__name__}"
            )
            # Specifically the numeric-looking ones must remain strings.
            if spec.name in {
                "PIPELINE_LOCAL_MAX_STEPS",
                "PIPELINE_LOCAL_NUM_CTX",
                "PIPELINE_PAUSE_THRESHOLD",
                "PIPELINE_MAX_CONCURRENT_AGENTS",
            }:
                assert isinstance(spec.default, str), (
                    f"{spec.name} default must stay a string, not int"
                )

    def test_anti_drift_every_config_py_env_var_in_catalog(self):
        """Success criterion #1: anti-drift against pipeline/config.py and
        pipeline/escalation.py."""
        mod = _import_module()
        names: set[str] = set()
        for rel in ("pipeline/config.py", "pipeline/escalation.py"):
            src = Path(rel).read_text(encoding="utf-8")
            names |= set(_re.findall(r'os\.environ\.get\("([A-Z_]+)"', src))
        assert names, "sanity: expected to find env vars in config.py/escalation.py"
        catalog_names = {s.name for s in mod.ENV_VAR_CATALOG}
        missing = names - catalog_names
        assert not missing, (
            f"env vars read in pipeline/config.py/escalation.py but missing from catalog: {sorted(missing)}"
        )

    def test_anti_drift_defaults_match_config_py_literals(self):
        """Each config.py env var's catalog default must equal the literal in config.py."""
        mod = _import_module()
        src = Path("pipeline/config.py").read_text(encoding="utf-8")
        # os.environ.get("NAME", "DEFAULT") -> capture name and default literal.
        pattern = r'os\.environ\.get\("([A-Z_]+)",\s*("([^"\\]*(?:\\.[^"\\]*)*)"|\'([^\']*)\'|([^)]+?))\)'
        by_name = {s.name: s.default for s in mod.ENV_VAR_CATALOG}
        for m in _re.finditer(pattern, src):
            name = m.group(1)
            # Determine the literal default value.
            if m.group(3) is not None:
                default = m.group(3)
            elif m.group(4) is not None:
                default = m.group(4)
            else:
                # non-string literal (e.g. str(6*3600) or a bare int) - skip exact
                # match; the catalog stores strings, so we only assert presence.
                assert name in by_name, f"{name} missing from catalog"
                continue
            assert name in by_name, f"{name} missing from catalog"
            assert by_name[name] == default, (
                f"{name}: catalog default {by_name[name]!r} != config.py literal {default!r}"
            )


class TestPipelineAutoEscalateCataloged:
    """Story: PIPELINE_AUTO_ESCALATE must be visible in the config view."""

    def test_name_present_in_catalog(self):
        """The headline requirement: PIPELINE_AUTO_ESCALATE is cataloged."""
        mod = _import_module()
        names = {s.name for s in mod.ENV_VAR_CATALOG}
        assert "PIPELINE_AUTO_ESCALATE" in names, (
            "PIPELINE_AUTO_ESCALATE must be added to ENV_VAR_CATALOG"
        )

    def test_spec_is_envvarspec_instance(self):
        """The entry must follow the shape of its neighbours: an EnvVarSpec."""
        mod = _import_module()
        by_name = {s.name: s for s in mod.ENV_VAR_CATALOG}
        assert "PIPELINE_AUTO_ESCALATE" in by_name, "entry missing"
        spec = by_name["PIPELINE_AUTO_ESCALATE"]
        assert isinstance(spec, mod.EnvVarSpec), (
            "PIPELINE_AUTO_ESCALATE entry must be an EnvVarSpec, like its neighbours"
        )

    def test_default_is_empty_or_unset_state(self):
        """Code default is the empty/unset state (None or empty string)."""
        mod = _import_module()
        by_name = {s.name: s for s in mod.ENV_VAR_CATALOG}
        spec = by_name["PIPELINE_AUTO_ESCALATE"]
        assert spec.default is None or spec.default == "", (
            f"PIPELINE_AUTO_ESCALATE code default must be the empty/unset state, "
            f"got {spec.default!r}"
        )

    def test_not_a_secret(self):
        """The var is not a secret: _is_secret must return False for it."""
        mod = _import_module()
        assert mod._is_secret("PIPELINE_AUTO_ESCALATE") is False, (
            "PIPELINE_AUTO_ESCALATE must not be treated as a secret"
        )

    def test_no_extra_fields_invented(self):
        """EnvVarSpec has exactly the fields its neighbours use (name, default).

        Guards against an implementer inventing new fields on the dataclass.
        """
        mod = _import_module()
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(mod.EnvVarSpec)}
        assert field_names == {"name", "default"}, (
            f"EnvVarSpec must keep the existing field shape, got {field_names}"
        )


class TestResolveEnvVarPipelineAutoEscalate:
    """resolve_env_var behaviour for PIPELINE_AUTO_ESCALATE."""

    def test_unset_reports_default_state_without_raising(self):
        """With an empty environ, resolve_env_var reports the unset/default
        state and does not raise."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "PIPELINE_AUTO_ESCALATE", environ={}, plist_env={}, mcp_env={}
        )
        assert isinstance(result, dict)
        # Unset -> source is the code_default layer.
        assert result["source"] == "code_default", (
            f"unset var should report code_default source, got {result['source']!r}"
        )
        # No restart required when falling back to the code default.
        assert result["restart_required"] is False
        # Not a secret -> not masked.
        assert result["masked"] is False
        # The effective value reflects the empty/unset default.
        assert result["effective"] in (None, ""), (
            f"unset effective should be empty/None, got {result['effective']!r}"
        )

    def test_set_to_one_reports_process_env_as_winning_source(self):
        """With environ={'PIPELINE_AUTO_ESCALATE': '1'} the process-env layer
        is the winning source."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "PIPELINE_AUTO_ESCALATE",
            environ={"PIPELINE_AUTO_ESCALATE": "1"},
            plist_env={},
            mcp_env={},
        )
        assert isinstance(result, dict)
        assert result["source"] == "process_env", (
            f"set var should report process_env as winning source, got {result['source']!r}"
        )
        assert result["effective"] == "1"
        assert result["restart_required"] is True
        # The process_env layer must be present in the layers list.
        layers = {layer["layer"]: layer for layer in result["layers"]}
        assert "process_env" in layers, (
            f"process_env layer must be present, got layers {sorted(layers)}"
        )
        assert layers["process_env"]["value"] == "1"


class TestIsSecret:
    def test_key(self):
        mod = _import_module()
        assert mod._is_secret("PLANE_API_KEY") is True

    def test_token(self):
        mod = _import_module()
        assert mod._is_secret("SOME_TOKEN") is True

    def test_secret(self):
        mod = _import_module()
        assert mod._is_secret("MY_SECRET") is True

    def test_password(self):
        mod = _import_module()
        assert mod._is_secret("DB_PASSWORD") is True

    def test_credential(self):
        mod = _import_module()
        assert mod._is_secret("USER_CREDENTIAL") is True

    def test_non_secret(self):
        mod = _import_module()
        assert mod._is_secret("PIPELINE_LOCAL_MAX_STEPS") is False

    def test_case_sensitive_substring(self):
        # Spec says "contains any of KEY, TOKEN, ...". Substring match.
        mod = _import_module()
        assert mod._is_secret("MONKEY_VALUE") is True  # contains KEY
        assert mod._is_secret("PIPELINE_BACKEND_DISPATCH") is False


# ---------------------------------------------------------------------------
# resolve_env_var
#
# These tests target the single-env-var resolution report added by this story.
# They are intentionally RED until a follow-up dispatch implements
# ``resolve_env_var`` in pipeline.config_provenance.
# ---------------------------------------------------------------------------


class TestResolveEnvVarSignature:
    def test_function_exists(self):
        mod = _import_module()
        assert hasattr(mod, "resolve_env_var"), "resolve_env_var must be defined"

    def test_returns_dict(self):
        mod = _import_module()
        result = mod.resolve_env_var("X", default="40", environ={}, plist_env={}, mcp_env={})
        assert isinstance(result, dict)

    def test_result_keys(self):
        mod = _import_module()
        result = mod.resolve_env_var("X", default="40", environ={}, plist_env={}, mcp_env={})
        expected_keys = {
            "name",
            "effective",
            "source",
            "restart_required",
            "conflict",
            "masked",
            "layers",
        }
        assert set(result.keys()) == expected_keys

    def test_name_echoed(self):
        mod = _import_module()
        result = mod.resolve_env_var("MY_VAR", default="40", environ={}, plist_env={}, mcp_env={})
        assert result["name"] == "MY_VAR"


class TestResolveEnvVarLaunchdPlist:
    def test_success_criteria_1(self):
        """Success criteria 1: launchd plist overrides code default."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={},
        )
        assert result["effective"] == "60"
        assert result["source"] == "launchd_plist"
        assert result["restart_required"] is True
        assert result["conflict"] is False

    def test_layers_include_code_default(self):
        """The layers list includes a code_default entry with the default value."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={},
        )
        layers = result["layers"]
        assert isinstance(layers, list)
        code_default_entries = [l for l in layers if l["layer"] == "code_default"]
        assert len(code_default_entries) == 1
        assert code_default_entries[0]["value"] == "40"
        assert code_default_entries[0]["restart_required"] is False

    def test_layers_include_process_env_and_plist(self):
        """Layers that supply a value appear in the ordered list."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={},
        )
        layers = result["layers"]
        layer_names = [l["layer"] for l in layers]
        assert "process_env" in layer_names
        assert "launchd_plist" in layer_names
        # process_env value is the environ value
        proc = next(l for l in layers if l["layer"] == "process_env")
        assert proc["value"] == "60"
        assert proc["restart_required"] is True
        # plist value
        plist = next(l for l in layers if l["layer"] == "launchd_plist")
        assert plist["value"] == "60"
        assert plist["restart_required"] is True

    def test_layers_ordered(self):
        """Layers appear in order process_env, launchd_plist, mcp_server_env, code_default."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={"X": "60"},
        )
        layer_names = [l["layer"] for l in result["layers"]]
        expected_order = ["process_env", "launchd_plist", "mcp_server_env", "code_default"]
        # Filter to only those present, preserving order.
        present_in_order = [n for n in expected_order if n in layer_names]
        assert present_in_order == layer_names


class TestResolveEnvVarConflict:
    def test_success_criteria_2(self):
        """Success criteria 2: plist and mcp declare different values -> conflict."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={"X": "99"},
        )
        assert result["conflict"] is True

    def test_no_conflict_when_only_plist_declares(self):
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={},
        )
        assert result["conflict"] is False

    def test_no_conflict_when_both_declare_same_value(self):
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={"X": "60"},
        )
        assert result["conflict"] is False

    def test_conflict_when_neither_in_environ(self):
        """Conflict is about plist vs mcp, independent of environ presence."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={},
            plist_env={"X": "60"},
            mcp_env={"X": "99"},
        )
        assert result["conflict"] is True


class TestResolveEnvVarCodeDefault:
    def test_success_criteria_3(self):
        """Success criteria 3: name absent from all layers -> code default."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={},
            plist_env={},
            mcp_env={},
        )
        assert result["effective"] == "40"
        assert result["source"] == "code_default"
        assert result["restart_required"] is False
        assert result["conflict"] is False

    def test_code_default_only_layer(self):
        """When nothing declares the var, only code_default appears in layers."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={},
            plist_env={},
            mcp_env={},
        )
        layer_names = [l["layer"] for l in result["layers"]]
        assert layer_names == ["code_default"]

    def test_default_none(self):
        """Boundary: default=None and name absent -> effective is None."""
        mod = _import_module()
        result = mod.resolve_env_var("X", default=None, environ={}, plist_env={}, mcp_env={})
        assert result["effective"] is None
        assert result["source"] == "code_default"
        assert result["restart_required"] is False

    def test_default_none_layer_value(self):
        """code_default layer value reflects None default."""
        mod = _import_module()
        result = mod.resolve_env_var("X", default=None, environ={}, plist_env={}, mcp_env={})
        code_default = next(l for l in result["layers"] if l["layer"] == "code_default")
        assert code_default["value"] is None


class TestResolveEnvVarProcessEnv:
    def test_success_criteria_4(self):
        """Success criteria 4: in environ, declared by no layer -> process_env."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={},
            mcp_env={},
        )
        assert result["source"] == "process_env"
        assert result["restart_required"] is True

    def test_process_env_when_plist_value_differs(self):
        """environ present but plist declares a different value -> process_env."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "70"},
            mcp_env={},
        )
        assert result["source"] == "process_env"

    def test_process_env_when_mcp_value_differs(self):
        """environ present but mcp declares a different value -> process_env."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={},
            mcp_env={"X": "70"},
        )
        assert result["source"] == "process_env"

    def test_mcp_server_env_source(self):
        """environ present and mcp declares same value (plist absent) -> mcp_server_env."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={},
            mcp_env={"X": "60"},
        )
        assert result["source"] == "mcp_server_env"
        assert result["restart_required"] is True

    def test_plist_takes_precedence_over_mcp_for_source(self):
        """When both plist and mcp declare the same value as environ, source is launchd_plist."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={"X": "60"},
        )
        assert result["source"] == "launchd_plist"


class TestResolveEnvVarSecretMasking:
    def test_success_criteria_5(self):
        """Success criteria 5: secret value is masked everywhere."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "PLANE_API_KEY",
            environ={"PLANE_API_KEY": "sk-live-abc"},
            plist_env={"PLANE_API_KEY": "sk-live-abc"},
            mcp_env={},
        )
        assert result["masked"] is True
        assert "sk-live-abc" not in repr(result)

    def test_masked_effective_value(self):
        mod = _import_module()
        result = mod.resolve_env_var(
            "PLANE_API_KEY",
            environ={"PLANE_API_KEY": "sk-live-abc"},
            plist_env={"PLANE_API_KEY": "sk-live-abc"},
            mcp_env={},
        )
        assert result["effective"] == "***"

    def test_masked_layer_values(self):
        """Every layer value is replaced with '***' for secrets."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "DB_PASSWORD",
            default="fallback",
            environ={"DB_PASSWORD": "secret123"},
            plist_env={"DB_PASSWORD": "secret123"},
            mcp_env={"DB_PASSWORD": "secret123"},
        )
        for layer in result["layers"]:
            assert layer["value"] == "***"
        assert "secret123" not in repr(result)
        assert "fallback" not in repr(result)

    def test_non_secret_not_masked(self):
        mod = _import_module()
        result = mod.resolve_env_var(
            "PIPELINE_LOCAL_MAX_STEPS",
            default="40",
            environ={"PIPELINE_LOCAL_MAX_STEPS": "60"},
            plist_env={"PIPELINE_LOCAL_MAX_STEPS": "60"},
            mcp_env={},
        )
        assert result["masked"] is False
        assert result["effective"] == "60"

    def test_masked_flag_false_for_non_secret(self):
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={},
        )
        assert result["masked"] is False


class TestResolveEnvVarDefaults:
    def test_environ_defaults_to_os_environ(self):
        """When environ is None, os.environ is used."""
        mod = _import_module()
        # Use a name unlikely to be set; default applies.
        result = mod.resolve_env_var(
            "X_VERY_UNLIKELY_NAME_12345", default="40", plist_env={}, mcp_env={}
        )
        # environ defaults to os.environ; this name is not set there.
        assert result["effective"] == "40"
        assert result["source"] == "code_default"
        # Confirm os.environ is the default source by checking a real var if present.
        # (Sanity: function did not raise with environ=None.)
        assert "environ" not in result  # environ itself not echoed

    def test_plist_env_defaults_to_read_plist_env(self):
        """plist_env=None resolves via read_plist_env (returns {} in test env)."""
        mod = _import_module()
        # In the test environment the default plist path does not exist, so
        # read_plist_env() returns {}.
        result = mod.resolve_env_var(
            "X", default="40", environ={}, mcp_env={}
        )
        assert result["source"] == "code_default"

    def test_mcp_env_defaults_to_read_mcp_server_env(self):
        """mcp_env=None resolves via read_mcp_server_env (returns {} in test env)."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X", default="40", environ={}, plist_env={}
        )
        assert result["source"] == "code_default"

    def test_all_defaults_callable(self):
        """Calling with only name and default does not raise."""
        mod = _import_module()
        result = mod.resolve_env_var("X_VERY_UNLIKELY_NAME_67890", default="40")
        assert result["effective"] == "40"
        assert result["source"] == "code_default"


class TestResolveEnvVarLayerShape:
    def test_layer_dict_keys(self):
        """Each layer entry has layer, value, restart_required."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X", default="40", environ={"X": "60"}, plist_env={"X": "60"}, mcp_env={}
        )
        for layer in result["layers"]:
            assert set(layer.keys()) == {"layer", "value", "restart_required", "effective_in_process"}

    def test_env_layers_restart_required_true(self):
        """process_env, launchd_plist, mcp_server_env all have restart_required True."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={"X": "60"},
        )
        for layer in result["layers"]:
            if layer["layer"] != "code_default":
                assert layer["restart_required"] is True

    def test_code_default_restart_required_false(self):
        mod = _import_module()
        result = mod.resolve_env_var(
            "X", default="40", environ={"X": "60"}, plist_env={"X": "60"}, mcp_env={}
        )
        code_default = next(l for l in result["layers"] if l["layer"] == "code_default")
        assert code_default["restart_required"] is False

    def test_top_level_restart_required_matches_winning_source(self):
        """Top-level restart_required equals the flag of the winning source's layer."""
        mod = _import_module()
        # code_default wins -> False
        result = mod.resolve_env_var("X", default="40", environ={}, plist_env={}, mcp_env={})
        assert result["restart_required"] is False
        # process_env wins -> True
        result = mod.resolve_env_var(
            "X", default="40", environ={"X": "60"}, plist_env={}, mcp_env={}
        )
        assert result["restart_required"] is True


class TestResolveEnvVarBoundary:
    def test_empty_string_value(self):
        """Boundary: empty string is a valid value."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X", default="40", environ={"X": ""}, plist_env={"X": ""}, mcp_env={}
        )
        assert result["effective"] == ""
        assert result["source"] == "launchd_plist"

    def test_zero_value(self):
        """Boundary: '0' is a valid value, not falsy-skipped."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X", default="40", environ={"X": "0"}, plist_env={"X": "0"}, mcp_env={}
        )
        assert result["effective"] == "0"
        assert result["source"] == "launchd_plist"

    def test_empty_environ_mapping(self):
        """Boundary: empty environ dict."""
        mod = _import_module()
        result = mod.resolve_env_var("X", default="1", environ={}, plist_env={}, mcp_env={})
        assert result["source"] == "code_default"

    def test_value_present_in_environ_not_in_layers(self):
        """environ value present but neither plist nor mcp declare it."""
        mod = _import_module()
        result = mod.resolve_env_var(
            "X", default="40", environ={"X": "77"}, plist_env={}, mcp_env={}
        )
        assert result["source"] == "process_env"
        assert result["conflict"] is False
        # layers: process_env and code_default only
        layer_names = [l["layer"] for l in result["layers"]]
        assert "process_env" in layer_names
        assert "code_default" in layer_names
        assert "launchd_plist" not in layer_names
        assert "mcp_server_env" not in layer_names


# ---------------------------------------------------------------------------
# Story: effective_env_config — the single-call full diagnostic report.
#
#   effective_env_config(*, environ=None, plist_env=None, mcp_env=None) -> list[dict]
#
# Calls resolve_env_var over every entry in ENV_VAR_CATALOG, sorted by name.
# Reads plist_env/mcp_env from their source files ONCE (not once per var) and
# passes the same values down to every resolve_env_var call.
#
# The implementation does not exist yet on this branch, so these tests are
# intentionally RED until a follow-up dispatch implements them.
# ---------------------------------------------------------------------------


