"""Tests for pipeline.config_provenance (wiring).

Split out of test_config_provenance.py to keep it under the project's
line-count target; shared fixtures/helpers moved to
tests.unit._config_provenance_helpers.
"""
import ast
import plistlib
import sys
from pathlib import Path

import pytest

from tests.unit._config_provenance_helpers import (  # noqa: F401
    _PLIST_DOCTYPE,
    _PLIST_HEADER,
    RoleRegistryError,
    RoleResolution,
    _import_module,
    _load_role_registry_imports,
    _write_json,
    _write_plist,
    _write_plist_raw,
)


class TestReadPlistEnv:
    def test_happy_path_returns_environment_variables(self, tmp_path):
        p = _write_plist(tmp_path, {"PIPELINE_LOCAL_MAX_STEPS": "60"})
        mod = _import_module()
        result = mod.read_plist_env(p)
        assert result == {"PIPELINE_LOCAL_MAX_STEPS": "60"}

    def test_nonexistent_path_returns_empty_and_does_not_raise(self):
        mod = _import_module()
        result = mod.read_plist_env(Path("/nonexistent/does-not-exist.plist"))
        assert result == {}

    def test_oserror_on_read_returns_empty(self, tmp_path):
        # A directory is not readable as a plist -> OSError/IsADirectoryError.
        d = tmp_path / "adir"
        d.mkdir()
        mod = _import_module()
        result = mod.read_plist_env(d)
        assert result == {}

    def test_expat_error_from_double_dash_comment_returns_empty(self, tmp_path):
        # REAL observed failure: an XML comment containing `--` makes the
        # expat-backed plist parser raise xml.parsers.expat.ExpatError.
        raw = (
            _PLIST_HEADER
            + _PLIST_DOCTYPE
            + b"<plist version=\"1.0\">\n"
            + b"<dict>\n"
            + b"<!-- a -- b -->\n"
            + b"<key>EnvironmentVariables</key>\n"
            + b"<dict>\n"
            + b"<key>X</key><string>1</string>\n"
            + b"</dict>\n"
            + b"</dict>\n"
            + b"</plist>\n"
        )
        p = _write_plist_raw(tmp_path, raw)
        # Sanity: this raw bytes really does raise ExpatError with plain plistlib.
        import xml.parsers.expat

        with pytest.raises(xml.parsers.expat.ExpatError):
            plistlib.loads(p.read_bytes())
        mod = _import_module()
        result = mod.read_plist_env(p)
        assert result == {}

    def test_invalid_file_exception_returns_empty(self, tmp_path):
        # Bytes that are not a plist at all -> plistlib.InvalidFileException.
        p = _write_plist_raw(tmp_path, b"this is not a plist at all")
        mod = _import_module()
        result = mod.read_plist_env(p)
        assert result == {}

    def test_valid_plist_without_environment_variables_returns_empty(self, tmp_path):
        payload = {"SomeOtherKey": "value"}
        data = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
        p = _write_plist_raw(tmp_path, data)
        mod = _import_module()
        result = mod.read_plist_env(p)
        assert result == {}

    def test_environment_variables_present_but_not_a_dict_returns_empty(self, tmp_path):
        payload = {"EnvironmentVariables": "not-a-dict"}
        data = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
        p = _write_plist_raw(tmp_path, data)
        mod = _import_module()
        result = mod.read_plist_env(p)
        assert result == {}

    def test_top_level_not_a_dict_returns_empty(self, tmp_path):
        # A plist whose root is an array, not a dict.
        data = plistlib.dumps(["a", "b"], fmt=plistlib.FMT_XML)
        p = _write_plist_raw(tmp_path, data)
        mod = _import_module()
        result = mod.read_plist_env(p)
        assert result == {}

    def test_integer_value_coerced_to_string(self, tmp_path):
        p = _write_plist(tmp_path, {"PIPELINE_LOCAL_MAX_STEPS": 60})
        mod = _import_module()
        result = mod.read_plist_env(p)
        assert result == {"PIPELINE_LOCAL_MAX_STEPS": "60"}
        assert isinstance(result["PIPELINE_LOCAL_MAX_STEPS"], str)

    def test_empty_environment_variables_returns_empty(self, tmp_path):
        p = _write_plist(tmp_path, {})
        mod = _import_module()
        assert mod.read_plist_env(p) == {}

    def test_default_path_argument_is_none(self):
        # The signature must accept path: Path | None = None.
        mod = _import_module()
        import inspect

        sig = inspect.signature(mod.read_plist_env)
        assert "path" in sig.parameters
        assert sig.parameters["path"].default is None


# ---------------------------------------------------------------------------
# read_mcp_server_env
# ---------------------------------------------------------------------------


class TestReadMcpServerEnv:
    def test_happy_path_returns_env_block(self, tmp_path):
        p = _write_json(
            tmp_path,
            {"mcpServers": {"pipeline": {"env": {"PIPELINE_AUTONOMY": "full"}}}},
        )
        mod = _import_module()
        result = mod.read_mcp_server_env(p)
        assert result == {"PIPELINE_AUTONOMY": "full"}

    def test_malformed_json_returns_empty(self, tmp_path):
        p = tmp_path / "claude.json"
        p.write_text("{not valid json", encoding="utf-8")
        mod = _import_module()
        result = mod.read_mcp_server_env(p)
        assert result == {}

    def test_nonexistent_path_returns_empty(self):
        mod = _import_module()
        result = mod.read_mcp_server_env(Path("/nonexistent/nope.json"))
        assert result == {}

    def test_no_pipeline_entry_returns_empty(self, tmp_path):
        p = _write_json(tmp_path, {"mcpServers": {"other-server": {"env": {"X": "1"}}}})
        mod = _import_module()
        result = mod.read_mcp_server_env(p)
        assert result == {}

    def test_no_mcpservers_key_returns_empty(self, tmp_path):
        p = _write_json(tmp_path, {"unrelated": "payload"})
        mod = _import_module()
        result = mod.read_mcp_server_env(p)
        assert result == {}

    def test_mcpservers_not_a_dict_returns_empty(self, tmp_path):
        p = _write_json(tmp_path, {"mcpServers": ["pipeline"]})
        mod = _import_module()
        result = mod.read_mcp_server_env(p)
        assert result == {}

    def test_server_entry_not_a_dict_returns_empty(self, tmp_path):
        p = _write_json(tmp_path, {"mcpServers": {"pipeline": "not-a-dict"}})
        mod = _import_module()
        result = mod.read_mcp_server_env(p)
        assert result == {}

    def test_server_entry_without_env_key_returns_empty(self, tmp_path):
        p = _write_json(tmp_path, {"mcpServers": {"pipeline": {"command": "x"}}})
        mod = _import_module()
        result = mod.read_mcp_server_env(p)
        assert result == {}

    def test_env_not_a_dict_returns_empty(self, tmp_path):
        p = _write_json(tmp_path, {"mcpServers": {"pipeline": {"env": "not-a-dict"}}})
        mod = _import_module()
        result = mod.read_mcp_server_env(p)
        assert result == {}

    def test_top_level_not_a_dict_returns_empty(self, tmp_path):
        p = _write_json(tmp_path, ["a", "b"])
        mod = _import_module()
        result = mod.read_mcp_server_env(p)
        assert result == {}

    def test_integer_value_coerced_to_string(self, tmp_path):
        p = _write_json(
            tmp_path,
            {"mcpServers": {"pipeline": {"env": {"PIPELINE_LOCAL_MAX_STEPS": 60}}}},
        )
        mod = _import_module()
        result = mod.read_mcp_server_env(p)
        assert result == {"PIPELINE_LOCAL_MAX_STEPS": "60"}
        assert isinstance(result["PIPELINE_LOCAL_MAX_STEPS"], str)

    def test_oserror_on_read_returns_empty(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        mod = _import_module()
        result = mod.read_mcp_server_env(d)
        assert result == {}

    def test_non_utf8_bytes_returns_empty_without_raising(self, tmp_path):
        p = tmp_path / "claude.json"
        p.write_bytes(b"\xc3(\xc3(")
        mod = _import_module()
        result = mod.read_mcp_server_env(p)
        assert result == {}

    def test_empty_env_returns_empty(self, tmp_path):
        p = _write_json(tmp_path, {"mcpServers": {"pipeline": {"env": {}}}})
        mod = _import_module()
        assert mod.read_mcp_server_env(p) == {}

    def test_custom_server_name_argument(self, tmp_path):
        p = _write_json(
            tmp_path,
            {"mcpServers": {"other": {"env": {"FOO": "bar"}}}},
        )
        mod = _import_module()
        result = mod.read_mcp_server_env(p, server_name="other")
        assert result == {"FOO": "bar"}

    def test_default_server_name_is_pipeline(self):
        mod = _import_module()
        import inspect

        sig = inspect.signature(mod.read_mcp_server_env)
        assert sig.parameters["server_name"].default == "pipeline"

    def test_default_path_argument_is_none(self):
        mod = _import_module()
        import inspect

        sig = inspect.signature(mod.read_mcp_server_env)
        assert "path" in sig.parameters
        assert sig.parameters["path"].default is None


# ---------------------------------------------------------------------------
# _scheduler_plist_path / _claude_json_path
# ---------------------------------------------------------------------------


class TestSchedulerPlistPath:
    def test_honours_env_override(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", "/custom/scheduler.plist")
        mod = _import_module()
        assert mod._scheduler_plist_path() == Path("/custom/scheduler.plist")

    def test_falls_back_to_home_launchagents(self, monkeypatch):
        # The conftest autouse fixture strips PIPELINE_* env vars, so the
        # override is unset here.
        monkeypatch.setattr(Path, "home", lambda: Path("/fakehome"))
        mod = _import_module()
        assert mod._scheduler_plist_path() == Path(
            "/fakehome/Library/LaunchAgents/com.claude.pipeline.advance-scheduler.plist"
        )

    def test_lazy_resolution_not_module_constant(self):
        # The path must be resolved inside the function, not bound at import
        # time as a module-level constant. We assert there is no module-level
        # attribute that is a Path holding the default.
        mod = _import_module()
        for name in dir(mod):
            if name.startswith("__"):
                continue
            value = getattr(mod, name, None)
            if isinstance(value, Path) and "LaunchAgents" in str(value):
                pytest.fail(
                    f"module-level Path constant {name!r} would defeat env override"
                )


class TestClaudeJsonPath:
    def test_honours_env_override(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", "/custom/.claude.json")
        mod = _import_module()
        assert mod._claude_json_path() == Path("/custom/.claude.json")

    def test_falls_back_to_home_claude_json(self, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: Path("/fakehome"))
        mod = _import_module()
        assert mod._claude_json_path() == Path("/fakehome/.claude.json")

    def test_lazy_resolution_not_module_constant(self):
        mod = _import_module()
        for name in dir(mod):
            if name.startswith("__"):
                continue
            value = getattr(mod, name, None)
            if isinstance(value, Path) and str(value).endswith(".claude.json"):
                pytest.fail(
                    f"module-level Path constant {name!r} would defeat env override"
                )


# ---------------------------------------------------------------------------
# Import-graph constraint: stdlib only, no pipeline.server / app.backend.
# ---------------------------------------------------------------------------


class TestImportGraph:
    def _source(self):
        p = Path("pipeline/config_provenance.py")
        assert p.exists(), "pipeline/config_provenance.py must exist"
        return p.read_text(encoding="utf-8")

    def test_no_pipeline_server_import(self):
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "pipeline.server", (
                        "config_provenance must not import pipeline.server"
                    )
                    assert not alias.name.startswith("pipeline.server."), (
                        "config_provenance must not import pipeline.server"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "pipeline.server", (
                    "config_provenance must not import pipeline.server"
                )
                assert not (node.module or "").startswith("pipeline.server."), (
                    "config_provenance must not import pipeline.server"
                )

    def test_no_app_backend_import(self):
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "app.backend", (
                        "config_provenance must not import app.backend"
                    )
                    assert not alias.name.startswith("app.backend."), (
                        "config_provenance must not import app.backend"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "app.backend", (
                    "config_provenance must not import app.backend"
                )
                assert not (node.module or "").startswith("app.backend."), (
                    "config_provenance must not import app.backend"
                )

    def test_stdlib_only(self):
        # Every imported module must resolve to a stdlib module (no
        # third-party, no other pipeline/app submodule besides the module's
        # own package). The story allows: json, os, plistlib, pathlib,
        # xml.parsers.expat, and app.role_registry - a verified stdlib-only
        # leaf (json/os/dataclasses/pathlib) that resolve_role_provenance
        # delegates to; it does not pull in app.backend or pipeline.server.
        tree = ast.parse(self._source())
        allowed = {"json", "os", "plistlib", "pathlib", "xml.parsers.expat", "app", "app.role_registry"}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        non_stdlib = {
            name
            for name in imported
            if not (name in allowed or name.split(".")[0] in sys.stdlib_module_names)
        }
        # Allow the module's own relative imports (level>0) which have no
        # module string; those are fine. Anything else must be stdlib.
        assert not non_stdlib, (
            f"config_provenance imports non-stdlib modules: {sorted(non_stdlib)}"
        )

    def test_no_cli_no_cache_no_write_path(self):
        # The module must not define a CLI entrypoint, a cache, or any write
        # path. We assert the absence of obvious write/CLI surface names.
        src = self._source()
        forbidden_substrings = [
            "argparse",
            "click",
            "sys.argv",
            "if __name__",
            "open(",
            ".write(",
            "functools.lru_cache",
            "@lru_cache",
            "shutil.copy",
            "os.remove",
            "os.unlink",
            "Path.unlink",
        ]
        present = [s for s in forbidden_substrings if s in src]
        # plistlib.load/json.load internally open files, but the module's own
        # source should not call open()/.write() directly. Allow none.
        assert not present, (
            f"config_provenance contains forbidden write/CLI/cache surface: {present}"
        )


# ---------------------------------------------------------------------------
# IGNORED_ENV_VARS + ignored_env_vars_present (story: surface transport-only
# env vars that backend.py overwrites on every dispatch).
#
# The module under test does not yet define these names on this branch, so the
# tests below are intentionally RED until a follow-up dispatch implements them.
# ---------------------------------------------------------------------------

# The exact six (var, replacement) pairs, in the order backend.py's warning
# loop currently writes them inline. This is the single source of truth the
# implementation must copy verbatim.
_EXPECTED_IGNORED_ENV_VARS = (
    ("LOCAL_AGENT_MAX_STEPS", "PIPELINE_LOCAL_MAX_STEPS"),
    ("LOCAL_AGENT_NUM_CTX", "PIPELINE_LOCAL_NUM_CTX"),
    ("LOCAL_AGENT_TEMPERATURE", "PIPELINE_LOCAL_TEMPERATURE"),
    ("PIPELINE_TRANSPORT_NUM_CTX", "PIPELINE_LOCAL_NUM_CTX"),
    ("PIPELINE_TRANSPORT_TEMPERATURE", "PIPELINE_LOCAL_TEMPERATURE"),
    ("PIPELINE_TRANSPORT_MAX_STEPS", "PIPELINE_LOCAL_MAX_STEPS"),
)

# The exact reason string the implementation must put in each entry's "reason".
_EXPECTED_REASON = (
    "transport-only value backend.py overwrites on every dispatch "
    "- it has no effect as an input"
)


class TestIgnoredEnvVarsConstant:
    def test_constant_exists_and_is_tuple_of_pairs(self):
        mod = _import_module()
        assert hasattr(mod, "IGNORED_ENV_VARS"), "IGNORED_ENV_VARS must be defined"
        value = mod.IGNORED_ENV_VARS
        assert isinstance(value, tuple), f"IGNORED_ENV_VARS must be a tuple, got {type(value)!r}"
        assert len(value) == 6, f"IGNORED_ENV_VARS must have exactly 6 pairs, got {len(value)}"

    def test_constant_has_exact_pairs_in_order(self):
        mod = _import_module()
        assert mod.IGNORED_ENV_VARS == _EXPECTED_IGNORED_ENV_VARS

    def test_constant_pairs_are_two_tuples_of_str(self):
        mod = _import_module()
        for pair in mod.IGNORED_ENV_VARS:
            assert isinstance(pair, tuple), f"each pair must be a tuple, got {type(pair)!r}"
            assert len(pair) == 2, f"each pair must have 2 elements, got {len(pair)}"
            name, replacement = pair
            assert isinstance(name, str) and isinstance(replacement, str)

    def test_constant_is_annotated_tuple_of_tuples(self):
        # The task requires the annotation `tuple[tuple[str, str], ...]`.
        # We assert the annotation is present in the source.
        src = Path("pipeline/config_provenance.py").read_text(encoding="utf-8")
        assert "IGNORED_ENV_VARS: tuple[tuple[str, str], ...]" in src, (
            "IGNORED_ENV_VARS must be annotated `tuple[tuple[str, str], ...]`"
        )


class TestIgnoredEnvVarsPresent:
    def test_empty_environ_returns_empty_list(self):
        mod = _import_module()
        assert mod.ignored_env_vars_present({}) == []

    def test_none_of_six_set_returns_empty(self):
        mod = _import_module()
        env = {"SOME_OTHER_VAR": "1", "PATH": "/usr/bin"}
        assert mod.ignored_env_vars_present(env) == []

    def test_returns_entries_for_set_vars_with_correct_use_instead(self):
        mod = _import_module()
        env = {"LOCAL_AGENT_NUM_CTX": "1", "PIPELINE_TRANSPORT_MAX_STEPS": "9"}
        result = mod.ignored_env_vars_present(env)
        assert len(result) == 2
        names = [entry["name"] for entry in result]
        assert names == ["LOCAL_AGENT_NUM_CTX", "PIPELINE_TRANSPORT_MAX_STEPS"]
        by_name = {entry["name"]: entry for entry in result}
        assert by_name["LOCAL_AGENT_NUM_CTX"]["use_instead"] == "PIPELINE_LOCAL_NUM_CTX"
        assert by_name["PIPELINE_TRANSPORT_MAX_STEPS"]["use_instead"] == "PIPELINE_LOCAL_MAX_STEPS"

    def test_entry_shape_has_name_use_instead_reason(self):
        mod = _import_module()
        result = mod.ignored_env_vars_present({"LOCAL_AGENT_NUM_CTX": "1"})
        assert len(result) == 1
        entry = result[0]
        assert set(entry.keys()) == {"name", "use_instead", "reason"}
        assert entry["name"] == "LOCAL_AGENT_NUM_CTX"
        assert entry["use_instead"] == "PIPELINE_LOCAL_NUM_CTX"
        assert entry["reason"] == _EXPECTED_REASON

    def test_reason_text_is_exact(self):
        mod = _import_module()
        result = mod.ignored_env_vars_present({"PIPELINE_TRANSPORT_NUM_CTX": "1"})
        assert result[0]["reason"] == _EXPECTED_REASON

    def test_all_six_set_returns_six_entries_in_order(self):
        mod = _import_module()
        env = {name: "x" for name, _ in _EXPECTED_IGNORED_ENV_VARS}
        result = mod.ignored_env_vars_present(env)
        assert len(result) == 6
        assert [e["name"] for e in result] == [name for name, _ in _EXPECTED_IGNORED_ENV_VARS]
        assert [e["use_instead"] for e in result] == [
            repl for _, repl in _EXPECTED_IGNORED_ENV_VARS
        ]

    def test_single_var_set_returns_single_entry(self):
        # Boundary: exactly one of the six set.
        mod = _import_module()
        result = mod.ignored_env_vars_present({"LOCAL_AGENT_TEMPERATURE": "0.5"})
        assert len(result) == 1
        assert result[0]["name"] == "LOCAL_AGENT_TEMPERATURE"
        assert result[0]["use_instead"] == "PIPELINE_LOCAL_TEMPERATURE"

    def test_near_miss_names_not_flagged(self):
        # NEGATIVE: exact-key match only. A near-miss name (extra suffix) and a
        # correct *replacement* var must NOT be flagged.
        mod = _import_module()
        env = {"LOCAL_AGENT_NUM_CTX_EXTRA": "1", "PIPELINE_LOCAL_NUM_CTX": "1"}
        assert mod.ignored_env_vars_present(env) == []

    def test_prefix_match_not_flagged(self):
        # NEGATIVE: a var that merely starts with a flagged name is not a match.
        mod = _import_module()
        env = {"LOCAL_AGENT_NUM_CTX_X": "1"}
        assert mod.ignored_env_vars_present(env) == []

    def test_case_sensitive_exact_match(self):
        # NEGATIVE: env var matching is case-sensitive.
        mod = _import_module()
        env = {"local_agent_num_ctx": "1"}
        assert mod.ignored_env_vars_present(env) == []

    def test_empty_string_value_still_flagged(self):
        # Boundary: a flagged var set to the empty string is still "present".
        mod = _import_module()
        result = mod.ignored_env_vars_present({"LOCAL_AGENT_NUM_CTX": ""})
        assert len(result) == 1
        assert result[0]["name"] == "LOCAL_AGENT_NUM_CTX"

    def test_default_arg_uses_os_environ(self, monkeypatch):
        # When environ is None (the default), the function must read os.environ.
        mod = _import_module()
        # Clear any of the six that happen to be set in this test process.
        for name, _ in _EXPECTED_IGNORED_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        assert mod.ignored_env_vars_present() == []
        monkeypatch.setenv("LOCAL_AGENT_NUM_CTX", "1")
        result = mod.ignored_env_vars_present()
        assert len(result) == 1
        assert result[0]["name"] == "LOCAL_AGENT_NUM_CTX"

    def test_does_not_mutate_input_environ(self):
        mod = _import_module()
        env = {"LOCAL_AGENT_NUM_CTX": "1", "UNRELATED": "2"}
        original = dict(env)
        mod.ignored_env_vars_present(env)
        assert env == original

    def test_returns_list_not_other_sequence(self):
        mod = _import_module()
        result = mod.ignored_env_vars_present({"LOCAL_AGENT_NUM_CTX": "1"})
        assert isinstance(result, list)


class TestBackendWiring:
    """Prove backend.py binds the SHARED object, not a copy, and the inline
    literal is gone from the warning loop."""

    def test_backend_binds_shared_object(self):
        import pipeline.config_provenance as provenance
        from app import backend

        assert hasattr(backend, "IGNORED_ENV_VARS"), (
            "app.backend must import IGNORED_ENV_VARS from pipeline.config_provenance"
        )
        assert backend.IGNORED_ENV_VARS is provenance.IGNORED_ENV_VARS, (
            "app.backend.IGNORED_ENV_VARS must be the SAME object as "
            "pipeline.config_provenance.IGNORED_ENV_VARS, not a copy"
        )

    def test_backend_imports_from_config_provenance(self):
        src = Path("app/backend.py").read_text(encoding="utf-8")
        assert "from pipeline.config_provenance import IGNORED_ENV_VARS" in src, (
            "backend.py must add `from pipeline.config_provenance import IGNORED_ENV_VARS`"
        )

    def test_six_var_names_not_inline_in_warning_loop(self):
        # The six literal var names must no longer appear inline in backend.py's
        # warning loop. We check the source of backend.py for the inline tuple
        # literal that used to hold them. The replacement var names
        # (PIPELINE_LOCAL_*) legitimately appear elsewhere in backend.py, so we
        # only assert the six *transport-only* var names are absent from the
        # warning-loop region (the top of the file, before the Backend protocol).
        src = Path("app/backend.py").read_text(encoding="utf-8")
        # The warning loop lives at the top of the file, before the Backend
        # protocol definition. Isolate that region.
        marker = "class Backend(Protocol):"
        idx = src.find(marker)
        assert idx != -1, "expected a Backend Protocol class in backend.py"
        top_region = src[:idx]
        for name, _ in _EXPECTED_IGNORED_ENV_VARS:
            assert name not in top_region, (
                f"transport-only var {name!r} must not appear inline in "
                f"backend.py's warning loop; it should come from IGNORED_ENV_VARS"
            )

    def test_warning_loop_uses_constant(self):
        src = Path("app/backend.py").read_text(encoding="utf-8")
        # The loop must iterate over IGNORED_ENV_VARS rather than an inline tuple.
        assert "for _var, _real in IGNORED_ENV_VARS" in src, (
            "backend.py warning loop must iterate over IGNORED_ENV_VARS"
        )

    def test_logger_name_unchanged(self):
        src = Path("app/backend.py").read_text(encoding="utf-8")
        assert 'logging.getLogger("pipeline")' in src, (
            'backend.py must keep the logger named "pipeline"'
        )

    def test_warning_message_text_unchanged(self):
        # The warning message text must be byte-for-byte identical to the
        # original. We assert the exact f-string fragments are still present.
        src = Path("app/backend.py").read_text(encoding="utf-8")
        assert (
            "is set in the environment but is a transport-only value that "
            "backend.py overwrites on every dispatch - it has no effect as an input; "
            in src
        )
        assert "set {_real} instead." in src


class TestBackendWarningEmission:
    """Reload app.backend under a controlled environment and assert the
    warning is (or is not) emitted on the 'pipeline' logger."""

    def _reload_backend_with_env(self, monkeypatch, env: dict):
        import importlib
        import logging

        # Ensure a clean-ish import state for app.backend so the import-time
        # warning loop runs again on reload.
        import app.backend

        # Strip any of the six from the real environment first so the baseline
        # is deterministic.
        for name, _ in _EXPECTED_IGNORED_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger("pipeline")
        handler = _Handler(level=logging.WARNING)
        logger.addHandler(handler)
        prev_level = logger.level
        logger.setLevel(logging.WARNING)
        try:
            importlib.reload(app.backend)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prev_level)
        return records

    def test_warning_emitted_when_transport_var_set(self, monkeypatch):
        import logging

        records = self._reload_backend_with_env(
            monkeypatch, {"PIPELINE_TRANSPORT_NUM_CTX": "1"}
        )
        warnings = [r for r in records if r.levelno == logging.WARNING]
        assert any(
            "PIPELINE_TRANSPORT_NUM_CTX" in r.getMessage()
            and "PIPELINE_LOCAL_NUM_CTX" in r.getMessage()
            for r in warnings
        ), (
            "expected a 'pipeline' warning mentioning both "
            "PIPELINE_TRANSPORT_NUM_CTX and PIPELINE_LOCAL_NUM_CTX; got: "
            f"{[r.getMessage() for r in warnings]}"
        )

    def test_no_warning_when_none_of_six_set(self, monkeypatch):
        import logging

        records = self._reload_backend_with_env(monkeypatch, {})
        warnings = [r for r in records if r.levelno == logging.WARNING]
        transport_warnings = [
            r
            for r in warnings
            if any(name in r.getMessage() for name, _ in _EXPECTED_IGNORED_ENV_VARS)
        ]
        assert transport_warnings == [], (
            "expected no transport-only warning when none of the six are set; got: "
            f"{[r.getMessage() for r in transport_warnings]}"
        )


class TestNoImportCycle:
    def test_config_provenance_imports_nothing_from_app(self):
        # app.role_registry is a verified stdlib-only leaf (json/os/
        # dataclasses/pathlib; no import of app.backend or pipeline.server),
        # so resolve_role_provenance delegating to it creates no cycle.
        # Anything else under app/ (app.backend, app.pipeline_mcp_server,
        # ...) remains forbidden.
        src = Path("pipeline/config_provenance.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name == "app.role_registry" or not alias.name.startswith("app"), (
                        "config_provenance must not import from app (no import cycle), "
                        "except the verified-safe app.role_registry leaf"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "app":
                    assert all(a.name == "role_registry" for a in node.names), (
                        "config_provenance may only import role_registry from app "
                        "(no import cycle), not any other app submodule"
                    )
                else:
                    assert not module.startswith("app"), (
                        "config_provenance must not import from app (no import cycle), "
                        "except the verified-safe app.role_registry leaf"
                    )


