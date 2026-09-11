"""Test-first tests for the optional ``line_filter`` seam on the spawn path.

Story: add an OPTIONAL keyword-only ``line_filter`` parameter to
``pipeline.execution.spawn_local`` and ``pipeline.execution.spawn_harness``
so a driver can transform each child output line before it is written to the
log.

Written test-first (strict TDD): every test that exercises the new keyword
fails with ``TypeError: ... unexpected keyword argument 'line_filter'`` (and
the signature/docstring tests fail on their own assertions) until the
implementation lands. The default ``line_filter=None`` path must stay
byte-identical to today, so the pre-existing suite in
tests/unit/test_execution.py stays green unmodified.

The drain loop runs on a background daemon thread, so every log / ``.ts``
sidecar assertion POLLS (mirroring ``_poll_log`` in
tests/unit/test_execution.py) instead of reading the file once.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

from app import backend_types
from pipeline import execution

DISPATCH_VAR = "PIPELINE_EXEC_DISPATCH"

_POLL_TIMEOUT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.05


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _poll_log(log_path: Path, marker: str | None = None) -> str:
    """Poll log_path until `marker` appears (or, with marker=None, until the
    file is non-empty), then return the content. Returns last-seen content on
    timeout so the caller's assertion failure shows what actually landed."""
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    content = ""
    while time.monotonic() < deadline:
        content = _read(log_path)
        if marker is None:
            if content.strip():
                return content
        elif marker in content:
            return content
        time.sleep(_POLL_INTERVAL_SECONDS)
    return content


def _poll_log_exact(log_path: Path, expected: str) -> str:
    """Poll log_path until its content equals `expected` exactly (i.e. the
    drain thread has settled). Returns last-seen content on timeout so the
    caller's assertion failure shows what actually landed."""
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    content = ""
    while time.monotonic() < deadline:
        content = _read(log_path)
        if content == expected:
            return content
        time.sleep(_POLL_INTERVAL_SECONDS)
    return content


def _poll_ts_rows(ts_path: Path, min_rows: int) -> list[str]:
    """Poll the ``.ts`` sidecar until it has at least `min_rows` lines, then
    return its lines. Returns last-seen lines on timeout."""
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    lines: list[str] = []
    while time.monotonic() < deadline:
        lines = _read(ts_path).splitlines()
        if len(lines) >= min_rows:
            return lines
        time.sleep(_POLL_INTERVAL_SECONDS)
    return lines


class TestSpawnLocalLineFilter:
    def test_line_filter_none_writes_decoded_lines_byte_identically(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('a'); print('b')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
            line_filter=None,
        )

        content = _poll_log_exact(log_path, "a\nb\n")
        assert content == "a\nb\n"
        ts_rows = _poll_ts_rows(tmp_path / "spawn.log.ts", 2)
        assert len(ts_rows) == 2

    def test_omitting_line_filter_kwarg_still_works(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('hi')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
        )

        content = _poll_log(log_path, "hi")
        assert "hi" in content
        ts_rows = _poll_ts_rows(tmp_path / "spawn.log.ts", 1)
        assert len(ts_rows) == 1

    def test_filter_transforms_each_line_before_it_is_written(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('hi')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
            line_filter=lambda line: [line.upper()],
        )

        content = _poll_log_exact(log_path, "HI\n")
        assert content == "HI\n"
        assert "hi" not in content

    def test_filter_returning_two_lines_writes_both_and_two_ts_rows(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('one')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
            line_filter=lambda line: ["alpha", "beta"],
        )

        content = _poll_log_exact(log_path, "alpha\nbeta\n")
        assert content == "alpha\nbeta\n"
        ts_rows = _poll_ts_rows(tmp_path / "spawn.log.ts", 2)
        assert len(ts_rows) == 2
        for row in ts_rows:
            datetime.fromisoformat(row)  # raises if unparseable

    def test_filter_line_without_trailing_newline_gets_one_appended(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('x')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
            line_filter=lambda line: [line.strip().upper()],
        )

        content = _poll_log_exact(log_path, "X\n")
        assert content == "X\n"
        ts_rows = _poll_ts_rows(tmp_path / "spawn.log.ts", 1)
        assert len(ts_rows) == 1

    def test_filter_returning_empty_list_writes_nothing_and_no_ts_row(
        self, tmp_path
    ):
        log_path = tmp_path / "spawn.log"

        def drop_skip(line: str) -> list[str]:
            return [] if "skip" in line else [line]

        execution.spawn_local(
            [sys.executable, "-c", "print('skip'); print('keep')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
            line_filter=drop_skip,
        )

        content = _poll_log_exact(log_path, "keep\n")
        assert content == "keep\n"
        assert "skip" not in content
        ts_rows = _poll_ts_rows(tmp_path / "spawn.log.ts", 1)
        assert len(ts_rows) == 1

    def test_filter_returning_empty_string_gets_newline_appended(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "print('x')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
            line_filter=lambda line: [""],
        )

        content = _poll_log_exact(log_path, "\n")
        assert content == "\n"
        ts_rows = _poll_ts_rows(tmp_path / "spawn.log.ts", 1)
        assert len(ts_rows) == 1

    def test_raising_filter_falls_back_to_original_lines_and_keeps_draining(
        self, tmp_path
    ):
        log_path = tmp_path / "spawn.log"

        def boom(line: str) -> list[str]:
            raise RuntimeError("filter exploded")

        execution.spawn_local(
            [sys.executable, "-c", "print('boom'); print('after')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
            line_filter=boom,
        )

        content = _poll_log_exact(log_path, "boom\nafter\n")
        assert content == "boom\nafter\n"
        ts_rows = _poll_ts_rows(tmp_path / "spawn.log.ts", 2)
        assert len(ts_rows) == 2

    def test_filter_raising_on_first_line_only_still_filters_subsequent(
        self, tmp_path
    ):
        log_path = tmp_path / "spawn.log"

        def flaky(line: str) -> list[str]:
            if "bad" in line:
                raise ValueError("first line only")
            return [line.upper()]

        execution.spawn_local(
            [sys.executable, "-c", "print('bad'); print('good')"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
            line_filter=flaky,
        )

        content = _poll_log_exact(log_path, "bad\nGOOD\n")
        assert content == "bad\nGOOD\n"
        assert "good" not in content

    def test_filter_applies_to_merged_stderr_lines_too(self, tmp_path):
        log_path = tmp_path / "spawn.log"

        execution.spawn_local(
            [sys.executable, "-c", "import sys; print('warn', file=sys.stderr)"],
            cwd=tmp_path,
            log_path=log_path,
            append=False,
            line_filter=lambda line: [line.upper()],
        )

        content = _poll_log_exact(log_path, "WARN\n")
        assert content == "WARN\n"


class TestLineFilterContract:
    def test_spawn_local_signature_gains_keyword_only_line_filter(self):
        sig = inspect.signature(execution.spawn_local)
        params = list(sig.parameters)

        assert "line_filter" in params
        original = ["cmd", "cwd", "log_path", "append", "env"]
        positions = [params.index(name) for name in original]
        assert positions == sorted(positions), f"original params reordered: {params}"
        line_filter = sig.parameters["line_filter"]
        assert line_filter.kind is inspect.Parameter.KEYWORD_ONLY
        assert line_filter.default is None
        assert sig.parameters["cmd"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for name in original[1:]:
            assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["env"].default is None

    def test_spawn_harness_signature_gains_keyword_only_line_filter(self):
        sig = inspect.signature(execution.spawn_harness)
        params = list(sig.parameters)

        assert "line_filter" in params
        for name in ("cmd", "cwd", "log_path", "append", "env", "role"):
            assert name in params
        line_filter = sig.parameters["line_filter"]
        assert line_filter.kind is inspect.Parameter.KEYWORD_ONLY
        assert line_filter.default is None
        assert sig.parameters["role"].default == "dispatch"
        for name in ("cwd", "log_path", "append", "env", "role"):
            assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY

    def test_spawn_local_docstring_documents_line_filter_and_qualifies_claim(self):
        doc = execution.spawn_local.__doc__ or ""
        assert "line_filter" in doc
        assert "own bytes are unaffected" in doc, "original docstring claim removed"
        assert "unless" in doc, "bytes claim must be qualified with 'unless'"

    def test_no_new_module_level_functions_and_logic_lives_in_spawn_local(self):
        source = Path(execution.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level_functions = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        assert not any(
            "filter" in name.lower() for name in top_level_functions
        ), f"line_filter logic must stay inside spawn_local, found: {top_level_functions}"
        assert "line_filter" in inspect.getsource(execution.spawn_local)


class TestSpawnHarnessLineFilter:
    def test_local_mode_forwards_line_filter_to_spawn_local(self, tmp_path):
        log_path = tmp_path / "spawn.log"
        stub = backend_types.AgentHandle(pid=1234, model="")

        def flt(line: str) -> list[str]:
            return [line.upper()]

        with mock.patch.dict(os.environ, {DISPATCH_VAR: "local"}), mock.patch.object(
            execution, "spawn_local", return_value=stub
        ) as fake_spawn:
            returned = execution.spawn_harness(
                [sys.executable, "-c", "print('relay')"],
                cwd=tmp_path,
                log_path=log_path,
                append=False,
                line_filter=flt,
            )

        assert fake_spawn.call_count == 1
        assert fake_spawn.call_args.kwargs.get("line_filter") is flt
        assert fake_spawn.call_args.kwargs.get("log_path") == log_path
        assert fake_spawn.call_args.kwargs.get("append") is False
        assert isinstance(returned, backend_types.AgentHandle)

    def test_local_mode_without_line_filter_forwards_none_or_omits_kwarg(
        self, tmp_path
    ):
        stub = backend_types.AgentHandle(pid=1, model="")

        with mock.patch.dict(os.environ, {DISPATCH_VAR: "local"}), mock.patch.object(
            execution, "spawn_local", return_value=stub
        ) as fake_spawn:
            execution.spawn_harness(
                [sys.executable, "-c", "print('x')"],
                cwd=tmp_path,
                log_path=tmp_path / "spawn.log",
                append=False,
            )

        assert fake_spawn.call_count == 1
        assert fake_spawn.call_args.kwargs.get("line_filter", None) is None

    def test_ssh_mode_does_not_forward_line_filter_to_spawn_local(self, tmp_path):
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
        ), mock.patch.object(execution, "spawn_local") as fake_spawn:
            execution.spawn_harness(
                [sys.executable, "-c", "print('remote')"],
                cwd=tmp_path,
                log_path=tmp_path / "spawn.log",
                append=False,
                line_filter=lambda line: [line.upper()],
            )

        fake_spawn.assert_not_called()