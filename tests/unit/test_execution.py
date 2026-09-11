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
from datetime import datetime, timedelta
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


def _poll_log_line_count(log_path: Path, expected_lines: int) -> str:
    """Poll log_path until it has at least `expected_lines` non-empty lines,
    then return the content. Returns last-seen content on timeout so the
    caller's assertion failure shows what actually landed."""
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    content = ""
    while time.monotonic() < deadline:
        try:
            content = log_path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        if len([ln for ln in content.splitlines() if ln.strip()]) >= expected_lines:
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

    def test_writes_a_ts_sidecar_file_with_one_line_per_log_line(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('a'); print('b')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        content = _poll_log(log_path, "b")
        assert content.splitlines() == ["a", "b"]
        ts_path = tmp_path / "spawn.log.ts"
        ts_lines = _poll_log(ts_path).splitlines()
        assert len(ts_lines) == 2

    def test_ts_sidecar_lines_are_parseable_iso8601(self, tmp_path):
        from datetime import datetime
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('hi')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        _poll_log(log_path, "hi")
        ts_content = _poll_log(tmp_path / "spawn.log.ts")
        line = ts_content.splitlines()[0]
        datetime.fromisoformat(line)  # raises if unparseable

    def test_ts_sidecar_append_false_truncates_existing_sidecar(self, tmp_path):
        log_path = tmp_path / "spawn.log"
        ts_path = tmp_path / "spawn.log.ts"
        ts_path.write_text("stale-ts-line\n", encoding="utf-8")

        execution.spawn_local(
            [sys.executable, "-c", "print('fresh')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        _poll_log(log_path, "fresh")
        ts_content = _poll_log(ts_path)
        assert "stale-ts-line" not in ts_content

    def test_ts_sidecar_append_true_appends_to_existing_sidecar(self, tmp_path):
        log_path = tmp_path / "spawn.log"
        log_path.write_text("first line\n", encoding="utf-8")
        ts_path = tmp_path / "spawn.log.ts"
        ts_path.write_text("2020-01-01T00:00:00+00:00\n", encoding="utf-8")

        execution.spawn_local(
            [sys.executable, "-c", "print('second line')"],
            cwd=tmp_path,
            log_path=log_path,
            append=True,
        )

        _poll_log(log_path, "second line")
        ts_content = _poll_log(ts_path)
        assert "2020-01-01T00:00:00+00:00" in ts_content
        assert len(ts_content.splitlines()) == 2

    def test_ts_sidecar_timestamps_are_utc_with_explicit_zero_offset(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('utc-probe')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        _poll_log(log_path, "utc-probe")
        ts_content = _poll_log_line_count(tmp_path / "spawn.log.ts", 1)
        parsed = datetime.fromisoformat(ts_content.splitlines()[0])
        assert parsed.tzinfo is not None, "timestamp must be tz-aware, not naive"
        assert parsed.utcoffset() == timedelta(0), "timestamp must be UTC (zero offset)"

    def test_ts_sidecar_line_count_matches_agent_log_line_count(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('l1'); print('l2'); print('l3')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        log_content = _poll_log(log_path, "l3")
        ts_content = _poll_log_line_count(tmp_path / "spawn.log.ts", 3)
        assert len(log_content.splitlines()) == 3
        assert len(ts_content.splitlines()) == 3

    def test_ts_sidecar_order_matches_log_line_order(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('first'); print('second')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        log_lines = _poll_log(log_path, "second").splitlines()
        ts_lines = _poll_log_line_count(tmp_path / "spawn.log.ts", 2).splitlines()
        assert log_lines == ["first", "second"]
        assert len(ts_lines) == 2
        first_ts = datetime.fromisoformat(ts_lines[0])
        second_ts = datetime.fromisoformat(ts_lines[1])
        assert first_ts <= second_ts, "timestamps must follow log-line order"

    def test_ts_sidecar_created_empty_when_child_writes_nothing(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        ts_path = tmp_path / "spawn.log.ts"
        deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not ts_path.exists():
            time.sleep(_POLL_INTERVAL_SECONDS)
        assert ts_path.exists(), "sidecar must exist even with zero child output"
        assert ts_path.read_text(encoding="utf-8") == ""

    def test_ts_sidecar_replaces_invalid_utf8_bytes_without_raising(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'bad:\\xff\\n')",
            ],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        content = _poll_log_line_count(log_path, 1)
        assert "bad:" in content
        try:
            strict = log_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            strict = None
        assert strict is not None, "agent.log must be valid UTF-8 (errors='replace')"
        assert "\ufffd" in strict, "invalid child bytes must decode as U+FFFD"
        ts_content = _poll_log_line_count(tmp_path / "spawn.log.ts", 1)
        assert len(ts_content.splitlines()) == 1

    def test_stderr_output_is_relayed_and_timestamped_too(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "import sys; sys.stderr.write('err-line\\n')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        content = _poll_log(log_path, "err-line")
        assert "err-line" in content
        ts_content = _poll_log_line_count(tmp_path / "spawn.log.ts", 1)
        assert len(ts_content.splitlines()) == 1

    def test_ts_sidecar_path_is_derived_from_log_path_in_nested_dir(self, tmp_path):
        log_dir = tmp_path / "worktree"
        log_dir.mkdir()
        log_path = log_dir / "agent.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('nested')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        _poll_log(log_path, "nested")
        ts_content = _poll_log_line_count(log_dir / "agent.log.ts", 1)
        assert len(ts_content.splitlines()) == 1

    def test_spawn_local_returns_before_child_output_is_relayed(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        handle = execution.spawn_local(
            [sys.executable, "-c", "import time; time.sleep(1.0); print('late')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        assert isinstance(handle, backend_types.AgentHandle)
        assert handle.pid > 0
        early = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        assert "late" not in early, "spawn_local must not block on the drain loop"

        content = _poll_log(log_path, "late")
        assert "late" in content

    def test_drain_relay_uses_background_daemon_thread_not_fd_redirect(self):
        fn_src = inspect.getsource(execution.spawn_local)
        assert "threading.Thread" in fn_src
        assert "daemon=True" in fn_src
        assert "subprocess.PIPE" in fn_src
        assert "stderr=subprocess.STDOUT" in fn_src
        assert "datetime.now(timezone.utc)" in fn_src
        assert "stdout=log_file" not in fn_src
        assert "stderr=log_file" not in fn_src

    def test_module_imports_threading_and_utc_datetime(self):
        source = Path(execution.__file__).read_text(encoding="utf-8")
        assert re.search(r"^import threading$", source, re.MULTILINE) is not None
        assert (
            re.search(r"^from datetime import datetime, timezone$", source, re.MULTILINE)
            is not None
        )

    def test_spawn_local_signature_unchanged(self):
        # LOG-04 deliberately extended the AGENTLOGTS-1 signature with the
        # keyword-only line_filter seam plus the internal _fd_redirect
        # routing switch (which keeps spawn_harness's default local path on
        # the byte-identical fd-redirect spawn). The original five
        # parameters keep their order, kinds, and defaults; the new
        # keyword-only parameters are pinned by
        # tests/unit/test_execution_line_filter.py.
        sig = inspect.signature(execution.spawn_local)
        assert list(sig.parameters) == [
            "cmd",
            "cwd",
            "log_path",
            "append",
            "env",
            "line_filter",
            "_fd_redirect",
        ]
        assert sig.parameters["cmd"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for name in ("cwd", "log_path", "append", "env", "line_filter", "_fd_redirect"):
            assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["env"].default is None
        assert sig.parameters["line_filter"].default is None
        assert sig.parameters["_fd_redirect"].default is False
        assert _annotation_name(sig.return_annotation) == "AgentHandle"


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
        assert list(local_sig.parameters) == [
            "cmd",
            "cwd",
            "log_path",
            "append",
            "env",
            "line_filter",
            "_fd_redirect",
        ]
        for name in ("cwd", "log_path", "append"):
            param = local_sig.parameters[name]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY
            assert param.default is inspect.Parameter.empty
        assert local_sig.parameters["env"].kind is inspect.Parameter.KEYWORD_ONLY
        assert local_sig.parameters["env"].default is None
        assert local_sig.parameters["line_filter"].kind is inspect.Parameter.KEYWORD_ONLY
        assert local_sig.parameters["line_filter"].default is None
        assert local_sig.parameters["_fd_redirect"].kind is inspect.Parameter.KEYWORD_ONLY
        assert local_sig.parameters["_fd_redirect"].default is False
        assert _annotation_name(local_sig.return_annotation) == "AgentHandle"

        harness_sig = inspect.signature(execution.spawn_harness)
        assert list(harness_sig.parameters) == [
            "cmd",
            "cwd",
            "log_path",
            "append",
            "env",
            "role",
            "line_filter",
        ]
        assert harness_sig.parameters["env"].default is None
        assert harness_sig.parameters["role"].kind is inspect.Parameter.KEYWORD_ONLY
        assert harness_sig.parameters["role"].default == "dispatch"
        assert _annotation_name(harness_sig.return_annotation) == "AgentHandle"