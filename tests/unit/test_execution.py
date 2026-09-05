"""Unit tests for pipeline/execution.py (agent spawn + execution-mode selection).

Written test-first (strict TDD): every test here fails with ImportError /
ModuleNotFoundError until pipeline/execution.py exists with the documented
public surface. One behavioral concern per test.

Execution-mode tests stub os.environ via mock.patch.dict and never assert
against the ambient environment (testing-config-gates rule).
"""

from __future__ import annotations

import inspect
import os
import re
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from app import backend_types
from pipeline import execution

DISPATCH_VAR = "PIPELINE_EXEC_DISPATCH"
REVIEW_VAR = "PIPELINE_EXEC_REVIEW"
SSH_NOT_IMPLEMENTED_MSG = "ssh execution is not implemented yet (B1 later story)"

_POLL_TIMEOUT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.05


def _poll_log(log_path: Path, marker: str | None = None) -> str:
    """Poll log_path until `marker` appears (or, with marker=None, until the
    file is non-empty), then return the content. Returns last-seen content on
    timeout so the caller's assertion failure shows what actually landed."""
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    content = ""
    while time.monotonic() < deadline:
        try:
            content = log_path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        if marker is None:
            if content.strip():
                return content
        elif marker in content:
            return content
        time.sleep(_POLL_INTERVAL_SECONDS)
    return content


def _annotation_name(annotation: object) -> str:
    """Return the bare name of a return annotation, tolerating both the class
    object and the string form produced by `from __future__ import annotations`."""
    if isinstance(annotation, str):
        return annotation
    return getattr(annotation, "__name__", str(annotation))


class TestSpawnLocal:
    def test_returns_handle_whose_pid_is_the_spawned_process_pid(self, tmp_path):
        log_path = tmp_path / "spawn.log"
        # The child prints its own pid, so the log proves which pid ran.
        cmd = [sys.executable, "-c", "import os; print(os.getpid())"]

        handle = execution.spawn_local(
            cmd, cwd=tmp_path, log_path=log_path, append=False
        )

        assert isinstance(handle, backend_types.AgentHandle)
        content = _poll_log(log_path)
        assert content.strip(), f"child never wrote to {log_path}"
        assert handle.pid == int(content.strip())

    def test_streams_cmd_stdout_into_log_path(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('hi')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        content = _poll_log(log_path, "hi")
        assert "hi" in content

    def test_model_field_is_empty_string(self, tmp_path):
        handle = execution.spawn_local(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            log_path=tmp_path / "spawn.log",
            append=False,
        )

        assert handle.model == ""

    def test_append_false_truncates_existing_log(self, tmp_path):
        log_path = tmp_path / "spawn.log"
        log_path.write_text("stale content\n", encoding="utf-8")

        execution.spawn_local(
            [sys.executable, "-c", "print('fresh')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        content = _poll_log(log_path, "fresh")
        assert "fresh" in content
        assert "stale content" not in content

    def test_append_true_appends_to_existing_log(self, tmp_path):
        log_path = tmp_path / "spawn.log"
        log_path.write_text("first line\n", encoding="utf-8")

        execution.spawn_local(
            [sys.executable, "-c", "print('second line')"],
            cwd=tmp_path,
            log_path=log_path,
            append=True,
        )

        content = _poll_log(log_path, "second line")
        assert "second line" in content
        assert "first line" in content

    def test_env_none_inherits_ambient_environ(self, tmp_path):
        log_path = tmp_path / "spawn.log"
        cmd = [
            sys.executable,
            "-c",
            'import os; print(os.environ.get("EXEC_PROBE_VAR", ""))',
        ]

        with mock.patch.dict(os.environ, {"EXEC_PROBE_VAR": "ambient-value"}):
            execution.spawn_local(
                cmd, cwd=tmp_path, log_path=log_path, append=False, env=None
            )

        content = _poll_log(log_path, "ambient-value")
        assert "ambient-value" in content

    def test_env_dict_overrides_value_seen_by_child(self, tmp_path):
        log_path = tmp_path / "spawn.log"
        cmd = [
            sys.executable,
            "-c",
            'import os; print(os.environ.get("EXEC_PROBE_VAR", ""))',
        ]

        with mock.patch.dict(os.environ, {"EXEC_PROBE_VAR": "ambient-value"}):
            execution.spawn_local(
                cmd,
                cwd=tmp_path,
                log_path=log_path,
                append=False,
                env={"EXEC_PROBE_VAR": "override-value"},
            )

        content = _poll_log(log_path, "override-value")
        assert "override-value" in content

    def test_env_dict_replaces_rather_than_merges_ambient(self, tmp_path):
        log_path = tmp_path / "spawn.log"
        cmd = [
            sys.executable,
            "-c",
            'import os; print(os.environ.get("EXEC_PROBE_VAR", "absent"))',
        ]

        with mock.patch.dict(os.environ, {"EXEC_PROBE_VAR": "ambient-value"}):
            execution.spawn_local(
                cmd,
                cwd=tmp_path,
                log_path=log_path,
                append=False,
                env={"UNRELATED_VAR": "x"},
            )

        content = _poll_log(log_path, "absent")
        assert "absent" in content
        assert "ambient-value" not in content


class TestResolveExecutionMode:
    def test_defaults_to_local_when_var_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(DISPATCH_VAR, None)
            assert execution.resolve_execution_mode() == "local"

    def test_defaults_to_local_when_var_empty(self):
        with mock.patch.dict(os.environ, {DISPATCH_VAR: ""}):
            assert execution.resolve_execution_mode() == "local"

    def test_accepts_local(self):
        with mock.patch.dict(os.environ, {DISPATCH_VAR: "local"}):
            assert execution.resolve_execution_mode() == "local"

    def test_accepts_ssh(self):
        with mock.patch.dict(os.environ, {DISPATCH_VAR: "ssh"}):
            assert execution.resolve_execution_mode() == "ssh"

    def test_strips_and_lowercases_value(self):
        with mock.patch.dict(os.environ, {DISPATCH_VAR: "  SSH  "}):
            assert execution.resolve_execution_mode() == "ssh"

    def test_rejects_unknown_value_naming_var_value_and_valid_choices(self):
        with mock.patch.dict(os.environ, {DISPATCH_VAR: "bogus"}), pytest.raises(
            ValueError
        ) as excinfo:
            execution.resolve_execution_mode()

        message = str(excinfo.value)
        assert DISPATCH_VAR in message
        assert "bogus" in message
        assert "local" in message
        assert "ssh" in message

    def test_reads_role_specific_variable_over_dispatch_variable(self):
        with mock.patch.dict(os.environ, {DISPATCH_VAR: "local", REVIEW_VAR: "ssh"}):
            assert execution.resolve_execution_mode("review") == "ssh"

    def test_rejects_unknown_value_for_role_specific_variable(self):
        with mock.patch.dict(os.environ, {REVIEW_VAR: "bogus"}), pytest.raises(
            ValueError
        ) as excinfo:
            execution.resolve_execution_mode("review")

        assert REVIEW_VAR in str(excinfo.value)


class TestSpawnHarness:
    def test_local_mode_delegates_to_local_spawn(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        with mock.patch.dict(os.environ, {DISPATCH_VAR: "local"}):
            handle = execution.spawn_harness(
                [sys.executable, "-c", "print('delegated')"],
                cwd=tmp_path,
                log_path=log_path,
                append=False,
            )

        assert isinstance(handle, backend_types.AgentHandle)
        content = _poll_log(log_path, "delegated")
        assert "delegated" in content

    def test_ssh_mode_dispatches_via_remote_exec(self, tmp_path):
        # B1 remote-SSH-execution driver is now implemented; the old
        # NotImplementedError pin is obsolete (review Blocking 1). ssh mode
        # must dispatch through _spawn_ssh -> `python -m pipeline.remote_exec`.
        log_path = tmp_path / "spawn.log"
        env = {
            DISPATCH_VAR: "ssh",
            "PIPELINE_REMOTE_EXEC_HOST": "gpu-host",
            "PIPELINE_REMOTE_SYNC_ROOT": "/srv/sync",
        }
        with mock.patch.dict(os.environ, env), mock.patch.object(
            execution.subprocess, "check_output", return_value="main\n"
        ), mock.patch.object(
            execution.subprocess,
            "Popen",
            return_value=backend_types.AgentHandle(pid=4242, model=""),
        ) as fake_popen:
            handle = execution.spawn_harness(
                [sys.executable, "-c", "print('should not run locally')"],
                cwd=tmp_path,
                log_path=log_path,
                append=False,
            )

        argv = fake_popen.call_args.args[0]
        assert argv[0:3] == [sys.executable, "-m", "pipeline.remote_exec"]
        assert "--worktree" in argv
        assert "--remote-url" in argv
        assert "--host" in argv
        assert "--spec-file" in argv
        assert handle.pid == 4242

    def test_ssh_mode_does_not_fall_back_to_local_spawn(self, tmp_path):
        # Fail closed: with ssh mode configured but the remote host/sync-root
        # unset, spawn must raise ValueError (never silently run locally).
        log_path = tmp_path / "spawn.log"

        with mock.patch.dict(os.environ, {DISPATCH_VAR: "ssh"}), pytest.raises(
            ValueError
        ):
            execution.spawn_harness(
                [sys.executable, "-c", "print('should not run')"],
                cwd=tmp_path,
                log_path=log_path,
                append=False,
            )

        # Fail closed: a local fallback would have created the log file.
        assert not log_path.exists()

    def test_role_parameter_selects_role_specific_mode(self, tmp_path):
        # role="review" resolves PIPELINE_EXEC_REVIEW; ssh mode there dispatches
        # through the same remote_exec supervisor as the dispatch role.
        env = {
            REVIEW_VAR: "ssh",
            "PIPELINE_REMOTE_EXEC_HOST": "gpu-host",
            "PIPELINE_REMOTE_SYNC_ROOT": "/srv/sync",
        }
        with mock.patch.dict(os.environ, env), mock.patch.object(
            execution.subprocess, "check_output", return_value="main\n"
        ), mock.patch.object(
            execution.subprocess, "Popen", return_value=mock.Mock(pid=4242)
        ) as fake_popen:
            execution.spawn_harness(
                [sys.executable, "-c", "print('should not run')"],
                cwd=tmp_path,
                log_path=tmp_path / "spawn.log",
                append=False,
                role="review",
            )

        argv = fake_popen.call_args.args[0]
        assert argv[0:3] == [sys.executable, "-m", "pipeline.remote_exec"]

    def test_bogus_mode_raises_value_error_fail_closed(self, tmp_path):
        with mock.patch.dict(os.environ, {DISPATCH_VAR: "bogus"}), pytest.raises(
            ValueError
        ) as excinfo:
            execution.spawn_harness(
                [sys.executable, "-c", "print('should not run')"],
                cwd=tmp_path,
                log_path=tmp_path / "spawn.log",
                append=False,
            )

        message = str(excinfo.value)
        assert DISPATCH_VAR in message
        assert "bogus" in message


class TestModuleContract:
    def test_agent_handle_is_imported_from_app_backend_types(self):
        assert execution.AgentHandle is backend_types.AgentHandle

    def test_does_not_import_anything_from_app_backend(self):
        source = Path(execution.__file__).read_text(encoding="utf-8")
        offenders = [
            line
            for line in source.splitlines()
            if re.search(r"^\s*(?:from\s+app\.backend\b|import\s+app\.backend\b)", line)
            or re.search(r"^\s*from\s+app\s+import\b[^#\n]*\bbackend\b", line)
        ]
        assert offenders == []

    def test_spawn_harness_is_defined_exactly_once(self):
        source = Path(execution.__file__).read_text(encoding="utf-8")
        assert source.count("def spawn_harness") == 1

    def test_public_signatures_match_spec(self):
        resolve_sig = inspect.signature(execution.resolve_execution_mode)
        role_param = resolve_sig.parameters["role"]
        assert role_param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert role_param.default == "dispatch"
        assert _annotation_name(resolve_sig.return_annotation) == "str"

        local_sig = inspect.signature(execution.spawn_local)
        assert list(local_sig.parameters) == ["cmd", "cwd", "log_path", "append", "env"]
        for name in ("cwd", "log_path", "append"):
            param = local_sig.parameters[name]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY
            assert param.default is inspect.Parameter.empty
        assert local_sig.parameters["env"].kind is inspect.Parameter.KEYWORD_ONLY
        assert local_sig.parameters["env"].default is None
        assert _annotation_name(local_sig.return_annotation) == "AgentHandle"

        harness_sig = inspect.signature(execution.spawn_harness)
        assert list(harness_sig.parameters) == [
            "cmd",
            "cwd",
            "log_path",
            "append",
            "env",
            "role",
        ]
        assert harness_sig.parameters["env"].default is None
        assert harness_sig.parameters["role"].kind is inspect.Parameter.KEYWORD_ONLY
        assert harness_sig.parameters["role"].default == "dispatch"
        assert _annotation_name(harness_sig.return_annotation) == "AgentHandle"