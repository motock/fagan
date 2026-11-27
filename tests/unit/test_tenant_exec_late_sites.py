"""Lint and suite probes, the oracle gate, the rebrief re-run, guard-liveness
collection and the reviewer shell run through the command-wrap seam.

Each site asks ``tenant_exec.wrap`` for the argv first and runs what it
returns. The recorder stands in for an operator plugin: it prefixes the argv
with ``WRAPPED`` so a test can tell the wrapped argv from the original. A
refusal (``CommandWrapError``) must stop the command from running at all.
"""

import logging
import subprocess
import sys

import pytest

from app import ollama_prompt_utils as opu
from pipeline import guard_liveness as gl
from pipeline import oracle_gate as og
from pipeline import rebrief as rb
from pipeline import repo_health as rh
from pipeline import tenant_exec

REFUSAL_MARKER = "SECRET-CMD-7731"


@pytest.fixture
def wrap_calls(monkeypatch):
    calls = []

    def _recorder(argv, *, cwd, kind):
        calls.append((list(argv), cwd, kind))
        return ["WRAPPED", *argv]

    monkeypatch.setattr(tenant_exec, "wrap", _recorder)
    return calls


@pytest.fixture
def refusing_wrap(monkeypatch):
    def _refuse(argv, *, cwd, kind):
        raise tenant_exec.CommandWrapError("refused")

    monkeypatch.setattr(tenant_exec, "wrap", _refuse)


@pytest.fixture
def plugin_configured(monkeypatch):
    monkeypatch.setattr(tenant_exec, "is_configured", lambda: True)


def _record_runs(monkeypatch, returncode=0, stdout="", stderr=""):
    """Stub every subprocess.run and record the argv and kwargs of each call."""
    ran = []

    def _fake_run(cmd, **kwargs):
        ran.append((cmd if isinstance(cmd, str) else list(cmd), kwargs))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return ran


def _argvs(ran):
    return [argv for argv, _kwargs in ran]


# --- repo_health: lint probe (kind='lint') --------------------------------


def test_lint_probe_runs_wrapped_lint_command_in_lint_dir(tmp_path, monkeypatch, wrap_calls):
    lint_dir = tmp_path / "lint"
    lint_dir.mkdir()
    lint_cmd = ["ruff", "check", "."]
    monkeypatch.setattr(rh, "detect_lint_command", lambda checkout: (lint_dir, list(lint_cmd)))
    ran = _record_runs(monkeypatch)

    assert rh.lint_baseline_finding(tmp_path) is None

    assert wrap_calls == [(lint_cmd, str(lint_dir), "lint")]
    assert _argvs(ran) == [["WRAPPED", *lint_cmd]]


def test_lint_probe_refused_wrap_returns_probe_failed_and_runs_nothing(
    tmp_path, monkeypatch, refusing_wrap
):
    monkeypatch.setattr(rh, "detect_lint_command", lambda checkout: (tmp_path, ["ruff", "check"]))
    ran = _record_runs(monkeypatch)

    finding = rh.lint_baseline_finding(tmp_path)

    assert finding["kind"] == "lint_probe_failed"
    assert finding["detail"] == "CommandWrapError"
    assert ran == []


# --- repo_health: suite probe (kind='test') -------------------------------


def test_suite_probe_runs_wrapped_test_command_in_test_dir(
    tmp_path, monkeypatch, wrap_calls
):
    test_dir = tmp_path / "suite"
    test_dir.mkdir()
    test_cmd = ["pytest", "-q"]
    monkeypatch.setenv("PIPELINE_TRIAGE_SUITE_PROBE", "1")
    monkeypatch.setattr(rh, "detect_test_command", lambda checkout: (test_dir, list(test_cmd)))
    ran = _record_runs(monkeypatch)

    assert rh.suite_baseline_finding(tmp_path) is None

    assert wrap_calls == [(test_cmd, str(test_dir), "test")]
    assert _argvs(ran) == [["WRAPPED", *test_cmd]]


def test_suite_probe_refused_wrap_returns_probe_failed_and_runs_nothing(
    tmp_path, monkeypatch, refusing_wrap
):
    monkeypatch.setenv("PIPELINE_TRIAGE_SUITE_PROBE", "1")
    monkeypatch.setattr(rh, "detect_test_command", lambda checkout: (tmp_path, ["pytest", "-q"]))
    ran = _record_runs(monkeypatch)

    finding = rh.suite_baseline_finding(tmp_path)

    assert finding["kind"] == "suite_probe_failed"
    assert finding["detail"] == "CommandWrapError"
    assert ran == []


# --- oracle_gate: acceptance fixture validation (kind='test') -------------


def _oracle_story():
    return {
        "acceptance": [{"path": "tests/unit/test_fixture.py", "source": "def test_x():\n    pass\n"}],
    }


def _write_oracle_fixture(checkout):
    # The pre-dispatch gate refuses a fixture absent from the checkout, so these
    # tests must create the file they declare. Without it they would exercise
    # the missing-fixture refusal rather than the wrapped command they target.
    fixture = checkout / "tests" / "unit" / "test_fixture.py"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("def test_x():\n    pass\n")


def test_oracle_gate_runs_wrapped_test_command_in_test_dir(tmp_path, monkeypatch, wrap_calls):
    _write_oracle_fixture(tmp_path)
    test_dir = tmp_path / "suite"
    test_dir.mkdir()
    test_cmd = ["pytest", "-q", "tests/unit/test_fixture.py"]
    monkeypatch.setattr(og, "detect_test_command", lambda checkout: (test_dir, list(test_cmd)))
    monkeypatch.setattr(og, "_scope_test_cmd_to_acceptance", lambda cmd, paths, cwd: None)
    ran = _record_runs(monkeypatch, returncode=1, stdout="1 failed")

    result = og.validate_acceptance_fixtures(_oracle_story(), tmp_path)

    assert wrap_calls == [(test_cmd, str(test_dir), "test")]
    assert _argvs(ran) == [["WRAPPED", *test_cmd]]
    assert result["state"] != "errors"


def test_oracle_gate_refused_wrap_reports_errors_and_runs_nothing(
    tmp_path, monkeypatch, refusing_wrap
):
    _write_oracle_fixture(tmp_path)
    monkeypatch.setattr(og, "detect_test_command", lambda checkout: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(og, "_scope_test_cmd_to_acceptance", lambda cmd, paths, cwd: None)
    ran = _record_runs(monkeypatch)

    result = og.validate_acceptance_fixtures(_oracle_story(), tmp_path)

    assert result["state"] == "errors"
    assert ran == []


# --- rebrief: failing-test re-run (kind='test') ---------------------------


def _rerun_story(cwd):
    return {
        "last_test_check": {
            "cmd": ["python", "-m", "pytest", "-q"],
            "cwd": str(cwd),
            "stdout_tail": "FAILED tests/unit/test_a.py::test_x - AssertionError",
            "stderr_tail": "",
        },
    }


def test_rebrief_rerun_runs_wrapped_pytest_command_in_recorded_cwd(
    tmp_path, monkeypatch, wrap_calls
):
    cwd = tmp_path / "wt"
    cwd.mkdir()
    expected = [
        "python", "-m", "pytest",
        "-vv", "--tb=long", "--no-header", "-p", "no:cacheprovider",
        "tests/unit/test_a.py::test_x",
    ]
    ran = _record_runs(monkeypatch)

    facts = rb._rerun_failing_tests(tmp_path, _rerun_story(cwd))

    assert wrap_calls == [(expected, str(cwd), "test")]
    assert _argvs(ran) == [["WRAPPED", *expected]]
    assert facts


def test_rebrief_rerun_refused_wrap_returns_empty_and_runs_nothing(
    tmp_path, monkeypatch, refusing_wrap
):
    ran = _record_runs(monkeypatch)

    assert rb._rerun_failing_tests(tmp_path, _rerun_story(tmp_path)) == []
    assert ran == []


# --- guard_liveness: pytest collection (kind='validate') ------------------

_COLLECT_ARGV = [
    sys.executable, "-m", "pytest", "--collect-only", "-q",
    "--ignore=tests/benchmark", "--ignore=tests/experiments",
]


def test_collect_runs_wrapped_collection_in_repo_root(tmp_path, monkeypatch, wrap_calls):
    ran = _record_runs(
        monkeypatch,
        stdout="tests/unit/test_a.py::test_x\n1 test collected\n",
    )

    collected = gl.collect_test_files(tmp_path)

    assert wrap_calls == [(_COLLECT_ARGV, str(tmp_path), "validate")]
    assert _argvs(ran) == [["WRAPPED", *_COLLECT_ARGV]]
    assert collected == ["tests/unit/test_a.py"]


def test_collect_refused_wrap_returns_none_without_exception_text(
    tmp_path, monkeypatch, caplog
):
    def _refuse_with_text(argv, *, cwd, kind):
        raise tenant_exec.CommandWrapError("PLUGIN-REFUSAL-TEXT")

    monkeypatch.setattr(tenant_exec, "wrap", _refuse_with_text)
    ran = _record_runs(monkeypatch)

    with caplog.at_level(logging.WARNING):
        collected = gl.collect_test_files(tmp_path)

    assert collected is None
    assert ran == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert all("PLUGIN-REFUSAL-TEXT" not in r.getMessage() for r in caplog.records)


# --- app.ollama_prompt_utils: reviewer shell (kind='review') --------------


def test_reviewer_shell_without_plugin_uses_sh_c_argv_and_skips_wrap(
    tmp_path, monkeypatch, wrap_calls
):
    monkeypatch.setattr(tenant_exec, "is_configured", lambda: False)
    ran = _record_runs(monkeypatch, stdout="hi\n")

    result = opu._bounded_bash_run("echo hi", tmp_path)

    assert wrap_calls == []
    assert len(ran) == 1
    cmd, kwargs = ran[0]
    assert cmd == ["/bin/sh", "-c", "echo hi"]
    assert "shell" not in kwargs
    assert result.stdout == "hi\n"


def test_reviewer_shell_with_plugin_wraps_sh_argv_without_shell_true(
    tmp_path, monkeypatch, plugin_configured, wrap_calls
):
    ran = _record_runs(monkeypatch, stdout="hi\n")

    opu._bounded_bash_run("echo hi", tmp_path)

    assert wrap_calls == [(["/bin/sh", "-c", "echo hi"], str(tmp_path), "review")]
    assert len(ran) == 1
    argv, kwargs = ran[0]
    assert argv == ["WRAPPED", "/bin/sh", "-c", "echo hi"]
    assert "shell" not in kwargs or kwargs["shell"] is False
    assert kwargs["cwd"] == tmp_path


def test_reviewer_shell_refused_wrap_returns_126_and_runs_nothing(
    tmp_path, monkeypatch, plugin_configured, refusing_wrap
):
    ran = _record_runs(monkeypatch)

    result = opu._bounded_bash_run("echo hi", tmp_path)

    assert result.returncode == 126
    assert result.stdout == ""
    assert result.stderr == "command refused"
    assert ran == []


# --- refusals never leak the command text ---------------------------------


def test_refused_commands_leak_no_command_text_into_logs_or_results(
    tmp_path, monkeypatch, plugin_configured, refusing_wrap, caplog
):
    _write_oracle_fixture(tmp_path)
    command = f"echo {REFUSAL_MARKER}"
    monkeypatch.setenv("PIPELINE_TRIAGE_SUITE_PROBE", "1")
    monkeypatch.setattr(rh, "detect_lint_command", lambda checkout: (tmp_path, [command]))
    monkeypatch.setattr(rh, "detect_test_command", lambda checkout: (tmp_path, [command]))
    monkeypatch.setattr(og, "detect_test_command", lambda checkout: (tmp_path, [command]))
    monkeypatch.setattr(og, "_scope_test_cmd_to_acceptance", lambda cmd, paths, cwd: None)
    _record_runs(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        results = [
            rh.lint_baseline_finding(tmp_path),
            rh.suite_baseline_finding(tmp_path),
            og.validate_acceptance_fixtures(_oracle_story(), tmp_path),
            opu._bounded_bash_run(command, tmp_path),
        ]

    assert all(REFUSAL_MARKER not in repr(res) for res in results)
    assert all(REFUSAL_MARKER not in r.getMessage() for r in caplog.records)
