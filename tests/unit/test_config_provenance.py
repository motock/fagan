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


class TestResolveEnvVar:
    def test_plist_overrides_code_default(self):
        """Success criterion #2."""
        mod = _import_module()
        r = mod.resolve_env_var(
            "X", default="40", environ={"X": "60"}, plist_env={"X": "60"}, mcp_env={}
        )
        assert r["name"] == "X"
        assert r["effective"] == "60"
        assert r["source"] == "launchd_plist"
        assert r["restart_required"] is True
        assert r["conflict"] is False
        layers = r["layers"]
        # layers must include a code_default entry with value "40".
        code_default = [l for l in layers if l["layer"] == "code_default"]
        assert len(code_default) == 1
        assert code_default[0]["value"] == "40"
        assert code_default[0]["restart_required"] is False

    def test_conflict_when_plist_and_mcp_differ(self):
        """Success criterion #3."""
        mod = _import_module()
        r = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={"X": "99"},
        )
        assert r["conflict"] is True

    def test_no_conflict_when_plist_and_mcp_same(self):
        mod = _import_module()
        r = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={"X": "60"},
        )
        assert r["conflict"] is False

    def test_absent_from_all_layers_uses_code_default(self):
        """Success criterion #4: negative/boundary."""
        mod = _import_module()
        r = mod.resolve_env_var(
            "X", default="40", environ={}, plist_env={}, mcp_env={}
        )
        assert r["effective"] == "40"
        assert r["source"] == "code_default"
        assert r["restart_required"] is False
        assert r["conflict"] is False

    def test_absent_with_none_default(self):
        mod = _import_module()
        r = mod.resolve_env_var(
            "X", default=None, environ={}, plist_env={}, mcp_env={}
        )
        assert r["effective"] is None
        assert r["source"] == "code_default"
        assert r["restart_required"] is False

    def test_in_environ_no_layer_declares_it(self):
        """Success criterion #5."""
        mod = _import_module()
        r = mod.resolve_env_var(
            "X", default="40", environ={"X": "60"}, plist_env={}, mcp_env={}
        )
        assert r["source"] == "process_env"
        assert r["restart_required"] is True
        assert r["effective"] == "60"

    def test_mcp_source_when_environ_matches_mcp_only(self):
        mod = _import_module()
        r = mod.resolve_env_var(
            "X", default="40", environ={"X": "60"}, plist_env={}, mcp_env={"X": "60"}
        )
        assert r["source"] == "mcp_server_env"
        assert r["restart_required"] is True

    def test_plist_takes_priority_over_mcp_for_source(self):
        # When both plist and mcp declare the same value as environ, plist wins
        # for the source label (per the ordered if/elif in the spec).
        mod = _import_module()
        r = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={"X": "60"},
        )
        assert r["source"] == "launchd_plist"

    def test_source_is_process_env_when_environ_value_differs_from_layers(self):
        mod = _import_module()
        r = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "77"},
            plist_env={"X": "60"},
            mcp_env={"X": "60"},
        )
        assert r["source"] == "process_env"

    def test_layers_order_and_restart_flags(self):
        mod = _import_module()
        r = mod.resolve_env_var(
            "X",
            default="40",
            environ={"X": "60"},
            plist_env={"X": "60"},
            mcp_env={"X": "99"},
        )
        layers = r["layers"]
        order = [l["layer"] for l in layers]
        # Every layer that supplies a value, in this fixed order.
        expected_order = ["process_env", "launchd_plist", "mcp_server_env", "code_default"]
        assert order == expected_order
        for l in layers:
            if l["layer"] == "code_default":
                assert l["restart_required"] is False
            else:
                assert l["restart_required"] is True
        # Values per layer.
        by_layer = {l["layer"]: l["value"] for l in layers}
        assert by_layer["process_env"] == "60"
        assert by_layer["launchd_plist"] == "60"
        assert by_layer["mcp_server_env"] == "99"
        assert by_layer["code_default"] == "40"

    def test_layers_omit_layers_that_supply_nothing(self):
        mod = _import_module()
        r = mod.resolve_env_var(
            "X", default="40", environ={"X": "60"}, plist_env={}, mcp_env={}
        )
        layers = r["layers"]
        present = {l["layer"] for l in layers}
        # process_env and code_default supply values; plist/mcp do not.
        assert "process_env" in present
        assert "code_default" in present
        assert "launchd_plist" not in present
        assert "mcp_server_env" not in present

    def test_layers_always_include_code_default(self):
        # code_default always supplies a value (the default), even if None.
        mod = _import_module()
        r = mod.resolve_env_var(
            "X", default=None, environ={"X": "60"}, plist_env={}, mcp_env={}
        )
        layers = r["layers"]
        code_default = [l for l in layers if l["layer"] == "code_default"]
        assert len(code_default) == 1
        assert code_default[0]["value"] is None

    def test_returned_keys(self):
        mod = _import_module()
        r = mod.resolve_env_var(
            "X", default="40", environ={"X": "60"}, plist_env={}, mcp_env={}
        )
        assert set(r.keys()) == {
            "name",
            "effective",
            "source",
            "restart_required",
            "conflict",
            "masked",
            "layers",
        }

    def test_masked_default_false_for_non_secret(self):
        mod = _import_module()
        r = mod.resolve_env_var(
            "X", default="40", environ={"X": "60"}, plist_env={}, mcp_env={}
        )
        assert r["masked"] is False

    def test_secret_masks_all_values(self):
        """Success criterion #6."""
        mod = _import_module()
        r = mod.resolve_env_var(
            "PLANE_API_KEY",
            environ={"PLANE_API_KEY": "sk-live-abc"},
            plist_env={"PLANE_API_KEY": "sk-live-abc"},
            mcp_env={},
        )
        assert r["masked"] is True
        assert "sk-live-abc" not in repr(r)
        assert r["effective"] == "***"
        for l in r["layers"]:
            assert l["value"] == "***"

    def test_secret_with_default_masked(self):
        mod = _import_module()
        r = mod.resolve_env_var(
            "SOME_TOKEN",
            default="secret-default",
            environ={},
            plist_env={},
            mcp_env={},
        )
        assert r["masked"] is True
        assert r["effective"] == "***"
        assert "secret-default" not in repr(r)

    def test_secret_conflict_still_masked(self):
        mod = _import_module()
        r = mod.resolve_env_var(
            "API_KEY",
            default="d",
            environ={"API_KEY": "v1"},
            plist_env={"API_KEY": "v1"},
            mcp_env={"API_KEY": "v2"},
        )
        assert r["conflict"] is True
        assert r["masked"] is True
        assert "v1" not in repr(r)
        assert "v2" not in repr(r)

    def test_default_environ_is_os_environ(self):
        """environ defaults to os.environ."""
        mod = _import_module()
        import os

        # Use a var unlikely to collide; set it in os.environ and don't pass environ.
        name = "PIPELINE_PROVENANCE_TEST_VAR_XYZ"
        os.environ[name] = "from-os-environ"
        try:
            r = mod.resolve_env_var(name, default="d", plist_env={}, mcp_env={})
            assert r["effective"] == "from-os-environ"
            assert r["source"] == "process_env"
        finally:
            del os.environ[name]


class TestEffectiveEnvConfig:
    def test_returns_one_entry_per_catalog_var_sorted_by_name(self):
        """Success criterion #7."""
        mod = _import_module()
        result = mod.effective_env_config(
            environ={}, plist_env={}, mcp_env={}
        )
        assert isinstance(result, list)
        catalog_names = [s.name for s in mod.ENV_VAR_CATALOG]
        result_names = [r["name"] for r in result]
        assert result_names == sorted(catalog_names)
        assert len(result) == len(mod.ENV_VAR_CATALOG)
        # No duplicates.
        assert len(result_names) == len(set(result_names))

    def test_raises_nothing_when_files_missing(self, tmp_path, monkeypatch):
        """Success criterion #7: nonexistent plist / claude.json raise nothing."""
        mod = _import_module()
        missing_plist = tmp_path / "does-not-exist.plist"
        missing_json = tmp_path / "does-not-exist.json"
        monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(missing_plist))
        monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(missing_json))
        # Use a clean environ so no real env var interferes.
        result = mod.effective_env_config(environ={})
        assert isinstance(result, list)
        assert len(result) == len(mod.ENV_VAR_CATALOG)
        for r in result:
            assert r["source"] == "code_default"
            assert r["conflict"] is False

    def test_reads_source_files_once(self, tmp_path, monkeypatch):
        """Spec: reads the source files ONCE, not once per var."""
        mod = _import_module()
        plist_path = _write_plist(tmp_path, {"PIPELINE_LOCAL_MAX_STEPS": "60"})
        json_path = _write_json(
            tmp_path,
            {"mcpServers": {"pipeline": {"env": {"PIPELINE_LOCAL_MAX_STEPS": "60"}}}},
        )
        monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(plist_path))
        monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(json_path))

        call_count = {"plist": 0, "mcp": 0}
        orig_plist = mod.read_plist_env
        orig_mcp = mod.read_mcp_server_env

        def counting_plist(path=None):
            call_count["plist"] += 1
            return orig_plist(path)

        def counting_mcp(path=None, server_name="pipeline"):
            call_count["mcp"] += 1
            return orig_mcp(path, server_name)

        monkeypatch.setattr(mod, "read_plist_env", counting_plist)
        monkeypatch.setattr(mod, "read_mcp_server_env", counting_mcp)

        result = mod.effective_env_config(environ={})
        assert len(result) == len(mod.ENV_VAR_CATALOG)
        assert call_count["plist"] == 1, "plist should be read exactly once"
        assert call_count["mcp"] == 1, "mcp json should be read exactly once"

    def test_passes_resolved_plist_and_mcp_down(self, tmp_path, monkeypatch):
        """When plist_env/mcp_env are passed explicitly, the file readers are not called."""
        mod = _import_module()
        call_count = {"plist": 0, "mcp": 0}
        monkeypatch.setattr(
            mod,
            "read_plist_env",
            lambda *a, **k: call_count.__setitem__("plist", call_count["plist"] + 1) or {},
        )
        monkeypatch.setattr(
            mod,
            "read_mcp_server_env",
            lambda *a, **k: call_count.__setitem__("mcp", call_count["mcp"] + 1) or {},
        )
        result = mod.effective_env_config(
            environ={},
            plist_env={"PIPELINE_LOCAL_MAX_STEPS": "60"},
            mcp_env={},
        )
        assert call_count["plist"] == 0
        assert call_count["mcp"] == 0
        # The passed plist value should be reflected.
        steps = next(r for r in result if r["name"] == "PIPELINE_LOCAL_MAX_STEPS")
        # environ is empty so effective == default ("40"); but layers should
        # include the plist layer with value "60".
        layers = {l["layer"]: l["value"] for l in steps["layers"]}
        assert layers.get("launchd_plist") == "60"

    def test_each_entry_is_a_resolve_env_var_result(self):
        mod = _import_module()
        result = mod.effective_env_config(environ={}, plist_env={}, mcp_env={})
        for r in result:
            assert set(r.keys()) == {
                "name",
                "effective",
                "source",
                "restart_required",
                "conflict",
                "masked",
                "layers",
            }

    def test_uses_catalog_defaults(self):
        mod = _import_module()
        result = mod.effective_env_config(environ={}, plist_env={}, mcp_env={})
        by_name = {r["name"]: r for r in result}
        # A catalog var with a known default should surface that default.
        assert by_name["PIPELINE_LOCAL_MAX_STEPS"]["effective"] == "40"
        assert by_name["PIPELINE_LOCAL_MAX_STEPS"]["source"] == "code_default"


class TestReportFormat:
    """The plan doc asks for a human report like:
    `PIPELINE_LOCAL_MAX_STEPS = 60 (from launchd plist, overriding code default 40)`.
    The module need not produce that exact string, but the data needed to build
    it must be present and consistent. This guards the data contract.
    """

    def test_report_data_contract(self):
        mod = _import_module()
        r = mod.resolve_env_var(
            "PIPELINE_LOCAL_MAX_STEPS",
            default="40",
            environ={"PIPELINE_LOCAL_MAX_STEPS": "60"},
            plist_env={"PIPELINE_LOCAL_MAX_STEPS": "60"},
            mcp_env={},
        )
        # effective value
        assert r["effective"] == "60"
        # source label
        assert r["source"] == "launchd_plist"
        # the overridden code default is recoverable from layers
        code_default = next(l for l in r["layers"] if l["layer"] == "code_default")
        assert code_default["value"] == "40"