"""Tests for pipeline.config_provenance.

This story adds two read-only file readers that report the effective value
of pipeline config sourced from (a) the launchd scheduler plist's
EnvironmentVariables block and (b) the MCP server's ``env`` block in
~/.claude.json. Both readers are diagnostics, never gates: they return ``{}``
and never raise on any failure.

The module under test does not exist yet on this branch, so this suite is
intentionally RED until a follow-up dispatch implements it.
"""
import ast
import json
import plistlib
import sys
from pathlib import Path

import pytest

# Lazily imported inside tests so collection does not hard-fail before the
# first test runs (the implementation does not exist yet on this branch).
RoleResolution = None
RoleRegistryError = None


def _ensure_role_registry_imports():
    """Import RoleResolution / RoleRegistryError from app.role_registry.

    Done lazily so the suite stays RED (per-test import errors) rather than
    failing at collection time before the implementation exists.
    """
    global RoleResolution, RoleRegistryError
    from app import role_registry

    RoleResolution = role_registry.RoleResolution
    RoleRegistryError = role_registry.RoleRegistryError
    return role_registry

# The module is imported lazily inside fixtures/tests so that collection of
# this file itself does not hard-fail before the first test runs: we want the
# RED state to surface as per-test import errors, not a collection error that
# hides which assertion is unmet.


def _import_module():
    import pipeline.config_provenance as mod

    return mod


# ---------------------------------------------------------------------------
# Helpers to build plist payloads.
# ---------------------------------------------------------------------------

_PLIST_HEADER = b"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
_PLIST_DOCTYPE = b"<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"


def _write_plist(tmp_path: Path, env: dict) -> Path:
    """Write a minimal plist whose top-level dict has an EnvironmentVariables key."""
    payload = {"EnvironmentVariables": env}
    data = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
    p = tmp_path / "scheduler.plist"
    p.write_bytes(data)
    return p


def _write_plist_raw(tmp_path: Path, raw: bytes, name: str = "scheduler.plist") -> Path:
    p = tmp_path / name
    p.write_bytes(raw)
    return p


def _write_json(tmp_path: Path, payload, name: str = "claude.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# read_plist_env
# ---------------------------------------------------------------------------


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
        # xml.parsers.expat.
        tree = ast.parse(self._source())
        allowed = {"json", "os", "plistlib", "pathlib", "xml.parsers.expat"}
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
        # warning-loop region (the top of the file, before the first @dataclass).
        src = Path("app/backend.py").read_text(encoding="utf-8")
        # The warning loop lives at the top of the file, before the first
        # @dataclass decorator. Isolate that region.
        marker = "@dataclass"
        idx = src.find(marker)
        assert idx != -1, "expected a @dataclass in backend.py"
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
        src = Path("pipeline/config_provenance.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app"), (
                        "config_provenance must not import from app (no import cycle)"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("app"), (
                    "config_provenance must not import from app (no import cycle)"
                )


# ---------------------------------------------------------------------------
# Story 2 (W3a): env-var provenance catalog, resolution, and effective config.
# These tests target the NEW API added in story 2:
#   EnvVarSpec, ENV_VAR_CATALOG, _is_secret, resolve_env_var, effective_env_config.
# The implementation does not exist yet on this branch, so these tests are
# intentionally RED until a follow-up dispatch implements them.
# ---------------------------------------------------------------------------

import re as _re

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
        """Success criterion #1: anti-drift against pipeline/config.py."""
        mod = _import_module()
        src = Path("pipeline/config.py").read_text(encoding="utf-8")
        names = set(_re.findall(r'os\.environ\.get\("([A-Z_]+)"', src))
        assert names, "sanity: expected to find env vars in config.py"
        catalog_names = {s.name for s in mod.ENV_VAR_CATALOG}
        missing = names - catalog_names
        assert not missing, (
            f"env vars read in pipeline/config.py but missing from catalog: {sorted(missing)}"
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
            assert set(layer.keys()) == {"layer", "value", "restart_required"}

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


# ===========================================================================
# resolve_role_provenance delegation to app.role_registry.resolve_role
# ===========================================================================
#
# This story wires pipeline.config_provenance.resolve_role_provenance so that
# on its SUCCESS path the authoritative final provider/model come from
# app.role_registry.resolve_role(...).provider / .model rather than being
# independently re-derived. The hand-rolled walk is retained ONLY to label
# provider_source / model_source / restart_required. The two error paths
# (model-not-in-registry, no-model-configured) keep their existing {"error":...}
# return shape and wording: a RoleRegistryError raised by resolve_role is
# caught and translated back into this function's own pre-existing error dict.
#
# The implementation does not exist yet on this branch, so this block is
# intentionally RED until a follow-up dispatch implements it.


def _build_registry(roles=None, providers=None):
    """Build a minimal model_registry.json-shaped dict.

    ``roles`` maps role_name -> {"provider": ..., "model": ...}.
    ``providers`` maps provider_name -> {"models": {friendly: {"tag": tag}}}.
    """
    roles = roles or {}
    providers = providers or {}
    return {"roles": roles, "providers": providers}


def _registry_with_claude_sonnet():
    """A registry where role 'overlord' uses claude/sonnet (friendly -> tag)."""
    return _build_registry(
        roles={"overlord": {"provider": "claude", "model": "sonnet"}},
        providers={
            "claude": {
                "models": {
                    "sonnet": {"tag": "claude-3-5-sonnet"},
                    "haiku": {"tag": "claude-3-5-haiku"},
                    "opus": {"tag": "claude-3-opus"},
                }
            },
            "openai": {
                "models": {
                    "gpt4": {"tag": "gpt-4o"},
                }
            },
        },
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
        the provider/model values still come from resolve_role."""
        mod = _import_module()
        reg = _registry_with_claude_sonnet()
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
        assert result["model"] == expected.model == "claude-3-5-haiku"
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
        assert result["model"] == expected.model == "claude-3-5-sonnet"
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
