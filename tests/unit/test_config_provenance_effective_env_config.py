"""Tests for pipeline.config_provenance (effective env config).

Split out of test_config_provenance.py to keep it under the project's
line-count target; shared fixtures/helpers moved to
tests.unit._config_provenance_helpers.
"""
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


class TestEffectiveEnvConfigSignature:
    def test_function_exists(self):
        """The module must define effective_env_config."""
        mod = _import_module()
        assert hasattr(mod, "effective_env_config"), (
            "pipeline.config_provenance must define effective_env_config"
        )

    def test_signature_keyword_only_and_returns_list(self):
        """effective_env_config(*, environ=None, plist_env=None, mcp_env=None).

        All parameters must be keyword-only and default to None.
        """
        import inspect

        mod = _import_module()
        sig = inspect.signature(mod.effective_env_config)
        params = sig.parameters
        # No positional-only or positional params allowed: every param must be
        # KEYWORD_ONLY (kind == KEYWORD_ONLY).
        for pname, p in params.items():
            assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"effective_env_config param {pname!r} must be keyword-only"
            )
        assert set(params) == {"environ", "plist_env", "mcp_env"}, (
            f"unexpected params: {set(params)}"
        )
        for pname in ("environ", "plist_env", "mcp_env"):
            assert params[pname].default is None, (
                f"effective_env_config param {pname!r} must default to None"
            )


class TestEffectiveEnvConfigHappyPath:
    def test_one_entry_per_catalog_var_sorted_by_name(self):
        """Success criterion 1: exactly one entry per catalog var, sorted by name."""
        mod = _import_module()
        catalog = mod.ENV_VAR_CATALOG
        result = mod.effective_env_config(
            environ={}, plist_env={}, mcp_env={}
        )
        assert isinstance(result, list)
        assert len(result) == len(catalog), (
            f"expected {len(catalog)} entries (one per catalog var), "
            f"got {len(result)}"
        )
        # Names of returned entries must equal the catalog names sorted.
        expected_names = sorted(spec.name for spec in catalog)
        got_names = [entry["name"] for entry in result]
        assert got_names == expected_names, (
            f"entries not sorted by name: expected {expected_names}, got {got_names}"
        )

    def test_each_entry_is_a_resolve_env_var_result(self):
        """Every entry must be a dict shaped like resolve_env_var's output."""
        mod = _import_module()
        result = mod.effective_env_config(
            environ={}, plist_env={}, mcp_env={}
        )
        required_keys = {
            "name",
            "effective",
            "source",
            "restart_required",
            "conflict",
            "masked",
            "layers",
        }
        for entry in result:
            assert isinstance(entry, dict)
            assert required_keys.issubset(entry.keys()), (
                f"entry missing keys: {required_keys - set(entry.keys())}"
            )

    def test_uses_each_catalog_default_when_nothing_set(self):
        """When environ/plist/mcp are all empty, effective == spec.default."""
        mod = _import_module()
        catalog = mod.ENV_VAR_CATALOG
        result = mod.effective_env_config(
            environ={}, plist_env={}, mcp_env={}
        )
        by_name = {entry["name"]: entry for entry in result}
        for spec in catalog:
            entry = by_name[spec.name]
            assert entry["source"] == "code_default"
            # Secrets are masked to "***"; non-secrets show the default.
            if entry["masked"]:
                assert entry["effective"] == "***"
            else:
                assert entry["effective"] == spec.default

    def test_environ_overrides_show_process_env_source(self):
        """A var present in environ (but not plist/mcp) resolves to process_env."""
        mod = _import_module()
        # Pick the first non-secret catalog var to set in environ.
        target = next(
            spec for spec in mod.ENV_VAR_CATALOG if not mod._is_secret(spec.name)
        )
        result = mod.effective_env_config(
            environ={target.name: "OVERRIDE"}, plist_env={}, mcp_env={}
        )
        entry = next(e for e in result if e["name"] == target.name)
        assert entry["source"] == "process_env"
        assert entry["effective"] == "OVERRIDE"
        assert entry["restart_required"] is True

    def test_plist_value_reflected(self):
        """A var declared by plist (and matching environ) shows launchd_plist."""
        mod = _import_module()
        target = next(
            spec for spec in mod.ENV_VAR_CATALOG if not mod._is_secret(spec.name)
        )
        result = mod.effective_env_config(
            environ={target.name: "PLISTVAL"},
            plist_env={target.name: "PLISTVAL"},
            mcp_env={},
        )
        entry = next(e for e in result if e["name"] == target.name)
        assert entry["source"] == "launchd_plist"

    def test_mcp_value_reflected(self):
        """A var declared by mcp (and matching environ) shows mcp_server_env."""
        mod = _import_module()
        target = next(
            spec for spec in mod.ENV_VAR_CATALOG if not mod._is_secret(spec.name)
        )
        result = mod.effective_env_config(
            environ={target.name: "MCPVAL"},
            plist_env={},
            mcp_env={target.name: "MCPVAL"},
        )
        entry = next(e for e in result if e["name"] == target.name)
        assert entry["source"] == "mcp_server_env"

    def test_secret_vars_are_masked(self):
        """Any catalog var whose name matches _is_secret must be masked."""
        mod = _import_module()
        secret_names = {
            spec.name for spec in mod.ENV_VAR_CATALOG if mod._is_secret(spec.name)
        }
        if not secret_names:
            pytest.skip("catalog has no secret vars to test masking")
        result = mod.effective_env_config(
            environ={name: "leak" for name in secret_names},
            plist_env={},
            mcp_env={},
        )
        for entry in result:
            if entry["name"] in secret_names:
                assert entry["masked"] is True
                assert entry["effective"] == "***"


class TestEffectiveEnvConfigMissingFiles:
    """Success criterion 2: raises nothing when plist and ~/.claude.json absent."""

    def test_no_raise_when_source_files_missing(self, monkeypatch, tmp_path):
        mod = _import_module()
        # Control the environment instead of assuming it is empty: this
        # test asserts the code_default path, so any catalogued var that
        # happens to be exported by the surrounding process (CI sets
        # AGENTS_DIR; see .github/workflows/ci.yml) would otherwise
        # resolve to process_env and fail. See
        # .claude/rules/testing-config-gates.md.
        for spec in mod.ENV_VAR_CATALOG:
            monkeypatch.delenv(spec.name, raising=False)
        # Point both path env vars at nonexistent paths.
        missing_plist = tmp_path / "no-such-scheduler.plist"
        missing_json = tmp_path / "no-such-claude.json"
        monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(missing_plist))
        monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(missing_json))
        # No environ override -> defaults flow; plist/mcp read from missing files.
        result = mod.effective_env_config()
        assert isinstance(result, list)
        assert len(result) == len(mod.ENV_VAR_CATALOG)
        # Every entry resolves to code_default (nothing set anywhere).
        for entry in result:
            assert entry["source"] == "code_default"

    def test_ambient_catalogued_env_var_does_not_leak_into_defaults(
        self, monkeypatch, tmp_path
    ):
        """Simulates CI's ambient AGENTS_DIR: a catalogued var genuinely
        present in the process env must surface as source='process_env';
        the sibling test's cleanup loop is what keeps such a var out of
        the code_default path."""
        mod = _import_module()
        monkeypatch.setenv("AGENTS_DIR", str(tmp_path / "agents"))
        for spec in mod.ENV_VAR_CATALOG:
            if spec.name != "AGENTS_DIR":
                monkeypatch.delenv(spec.name, raising=False)
        monkeypatch.setenv(
            "PIPELINE_SCHEDULER_PLIST_PATH", str(tmp_path / "none.plist")
        )
        monkeypatch.setenv(
            "PIPELINE_CLAUDE_JSON_PATH", str(tmp_path / "none.json")
        )
        result = mod.effective_env_config()
        agents = next(e for e in result if e["name"] == "AGENTS_DIR")
        assert agents["source"] == "process_env"
        # Follow-up call: the env still holds AGENTS_DIR (the scoped loop
        # never touched it), so a second call must report the same source.
        result2 = mod.effective_env_config()
        agents2 = next(e for e in result2 if e["name"] == "AGENTS_DIR")
        assert agents2["source"] == "process_env"

    def test_no_raise_when_source_files_missing_with_environ(self, monkeypatch, tmp_path):
        """Even with environ set, missing files must not raise."""
        mod = _import_module()
        missing_plist = tmp_path / "no-such-scheduler.plist"
        missing_json = tmp_path / "no-such-claude.json"
        monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(missing_plist))
        monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(missing_json))
        target = next(
            spec for spec in mod.ENV_VAR_CATALOG if not mod._is_secret(spec.name)
        )
        monkeypatch.setenv(target.name, "FROMPROCESS")
        result = mod.effective_env_config()
        entry = next(e for e in result if e["name"] == target.name)
        assert entry["source"] == "process_env"


class TestEffectiveEnvConfigReadsOnce:
    """Success criterion 3: read_plist_env/read_mcp_server_env called exactly once."""

    def test_read_plist_env_called_once(self, monkeypatch, tmp_path):
        mod = _import_module()
        # Provide real (empty) source files so the readers succeed.
        plist_path = _write_plist(tmp_path, {})
        json_path = _write_json(tmp_path, {"mcpServers": {"pipeline": {"env": {}}}})
        monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(plist_path))
        monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(json_path))

        call_count = {"n": 0}
        real_read = mod.read_plist_env

        def counting_read(path=None):
            call_count["n"] += 1
            return real_read(path)

        monkeypatch.setattr(mod, "read_plist_env", counting_read)
        mod.effective_env_config()
        assert call_count["n"] == 1, (
            f"read_plist_env must be called exactly once per "
            f"effective_env_config() call; got {call_count['n']}"
        )

    def test_read_mcp_server_env_called_once(self, monkeypatch, tmp_path):
        mod = _import_module()
        plist_path = _write_plist(tmp_path, {})
        json_path = _write_json(tmp_path, {"mcpServers": {"pipeline": {"env": {}}}})
        monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(plist_path))
        monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(json_path))

        call_count = {"n": 0}
        real_read = mod.read_mcp_server_env

        def counting_read(path=None, server_name="pipeline"):
            call_count["n"] += 1
            return real_read(path, server_name)

        monkeypatch.setattr(mod, "read_mcp_server_env", counting_read)
        mod.effective_env_config()
        assert call_count["n"] == 1, (
            f"read_mcp_server_env must be called exactly once per "
            f"effective_env_config() call; got {call_count['n']}"
        )

    def test_read_once_regardless_of_catalog_size(self, monkeypatch, tmp_path):
        """The read-once contract must hold even if the catalog is large."""
        mod = _import_module()
        plist_path = _write_plist(tmp_path, {})
        json_path = _write_json(tmp_path, {"mcpServers": {"pipeline": {"env": {}}}})
        monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(plist_path))
        monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(json_path))

        plist_calls = {"n": 0}
        mcp_calls = {"n": 0}
        real_plist = mod.read_plist_env
        real_mcp = mod.read_mcp_server_env

        def counting_plist(path=None):
            plist_calls["n"] += 1
            return real_plist(path)

        def counting_mcp(path=None, server_name="pipeline"):
            mcp_calls["n"] += 1
            return real_mcp(path, server_name)

        monkeypatch.setattr(mod, "read_plist_env", counting_plist)
        monkeypatch.setattr(mod, "read_mcp_server_env", counting_mcp)
        mod.effective_env_config()
        catalog_size = len(mod.ENV_VAR_CATALOG)
        assert catalog_size > 1, "catalog must have more than one var for this test"
        assert plist_calls["n"] == 1
        assert mcp_calls["n"] == 1

    def test_explicit_plist_env_skips_read_plist_env(self, monkeypatch, tmp_path):
        """When plist_env is passed explicitly, read_plist_env is NOT called."""
        mod = _import_module()
        plist_path = _write_plist(tmp_path, {})
        json_path = _write_json(tmp_path, {"mcpServers": {"pipeline": {"env": {}}}})
        monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(plist_path))
        monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(json_path))

        plist_calls = {"n": 0}
        mcp_calls = {"n": 0}
        real_plist = mod.read_plist_env
        real_mcp = mod.read_mcp_server_env

        def counting_plist(path=None):
            plist_calls["n"] += 1
            return real_plist(path)

        def counting_mcp(path=None, server_name="pipeline"):
            mcp_calls["n"] += 1
            return real_mcp(path, server_name)

        monkeypatch.setattr(mod, "read_plist_env", counting_plist)
        monkeypatch.setattr(mod, "read_mcp_server_env", counting_mcp)
        mod.effective_env_config(environ={}, plist_env={}, mcp_env={})
        assert plist_calls["n"] == 0, (
            "read_plist_env must not be called when plist_env is passed explicitly"
        )
        assert mcp_calls["n"] == 0, (
            "read_mcp_server_env must not be called when mcp_env is passed explicitly"
        )


class TestEffectiveEnvConfigBoundary:
    def test_empty_environ_plist_mcp(self):
        """Boundary: all three sources empty -> every entry is code_default."""
        mod = _import_module()
        result = mod.effective_env_config(
            environ={}, plist_env={}, mcp_env={}
        )
        assert len(result) == len(mod.ENV_VAR_CATALOG)
        for entry in result:
            assert entry["source"] == "code_default"
            assert entry["restart_required"] is False

    def test_single_catalog_var_overridden(self):
        """Boundary: only one var overridden in environ."""
        mod = _import_module()
        target = mod.ENV_VAR_CATALOG[0]
        result = mod.effective_env_config(
            environ={target.name: "X"}, plist_env={}, mcp_env={}
        )
        assert len(result) == len(mod.ENV_VAR_CATALOG)
        entry = next(e for e in result if e["name"] == target.name)
        assert entry["source"] == "process_env"
        # All other entries remain code_default.
        others = [e for e in result if e["name"] != target.name]
        for e in others:
            assert e["source"] == "code_default"

    def test_all_vars_overridden_in_environ(self):
        """Boundary: every catalog var set in environ -> all process_env."""
        mod = _import_module()
        environ = {spec.name: "V" for spec in mod.ENV_VAR_CATALOG}
        result = mod.effective_env_config(
            environ=environ, plist_env={}, mcp_env={}
        )
        assert len(result) == len(mod.ENV_VAR_CATALOG)
        for entry in result:
            if entry["masked"]:
                assert entry["effective"] == "***"
            else:
                assert entry["effective"] == "V"
            assert entry["source"] == "process_env"

    def test_conflict_detected_when_plist_and_mcp_differ(self):
        """A var declared differently by plist and mcp (both matching environ
        with one) should surface conflict=True on that entry."""
        mod = _import_module()
        target = next(
            spec for spec in mod.ENV_VAR_CATALOG if not mod._is_secret(spec.name)
        )
        result = mod.effective_env_config(
            environ={target.name: "PLISTVAL"},
            plist_env={target.name: "PLISTVAL"},
            mcp_env={target.name: "MCPVAL"},
        )
        entry = next(e for e in result if e["name"] == target.name)
        assert entry["conflict"] is True

    def test_returns_list_not_tuple_or_dict(self):
        """The return type must be a list (per the API signature)."""
        mod = _import_module()
        result = mod.effective_env_config(
            environ={}, plist_env={}, mcp_env={}
        )
        assert isinstance(result, list)
        assert not isinstance(result, (tuple, dict))
        assert not isinstance(result, (tuple, dict))
