"""Tests for ``pipeline.repo_health.suite_baseline_finding``.

The suite probe is opt-in behind the ``PIPELINE_TRIAGE_SUITE_PROBE`` env flag
so that the triage sweep does not stall every scheduler tick on the ~8 minute
full-suite run.  These tests monkeypatch
``pipeline.repo_health.detect_test_command`` and
``pipeline.repo_health.subprocess.run`` and set the flag with
``monkeypatch.setenv`` - the real suite is never executed.
"""

import ast
import inspect
import subprocess
from pathlib import Path

import pytest

from pipeline import repo_health

_FLAG = "PIPELINE_TRIAGE_SUITE_PROBE"


class MockCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_run_that_fails():
    """A subprocess.run stub that fails the test if it is ever invoked."""

    def _fail(*args, **kwargs):
        pytest.fail(
            "subprocess.run must not be called when the suite probe is off"
        )

    return _fail


def _enable_flag(monkeypatch, value="1"):
    monkeypatch.setenv(_FLAG, value)


# ---------------------------------------------------------------------------
# Regression: the opt-in explanation must be a *real* docstring, not an
# expression statement discarded after the early return whose text only
# reaches __doc__ via a runtime `.__doc__` mutation gated behind the flag
# (code review, agent/cc446d9f... branch, Blocking #2). These two tests are
# placed before every other test in this file (and use ast/reload rather than
# calling the function) so neither can pass merely because an earlier test
# already flipped the flag on and left the `.__doc__` mutation behind - the
# exact cross-test-pollution failure mode the review identified.
# ---------------------------------------------------------------------------


def test_docstring_is_the_functions_actual_first_statement():
    """ast.get_docstring uses the same mechanism Python itself uses to
    populate __doc__ at function-definition time. It must find the opt-in
    explanation as the function's real first statement - independent of
    whether the function has ever been called - not None because the real
    text is buried after the early-return guard as a discarded expression."""
    tree = ast.parse(inspect.getsource(repo_health.suite_baseline_finding))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    doc = ast.get_docstring(func)
    assert doc is not None, (
        "suite_baseline_finding has no real docstring - the opt-in "
        "explanation is not the function's first statement"
    )
    lowered = doc.lower()
    assert "opt-in" in lowered
    assert "eight minutes" in lowered
    assert "scheduler tick" in lowered


def test_calling_function_does_not_mutate_its_own_docstring(monkeypatch):
    """A real docstring never needs a manual __doc__ assignment. Capture
    __doc__ before and after a call with the probe flag enabled - they must
    be identical. This runs first in the file (before any other test has had
    a chance to enable the flag and trigger the mutation) so doc_before is
    genuinely the pristine, never-called value."""
    doc_before = repo_health.suite_baseline_finding.__doc__
    _enable_flag(monkeypatch, "1")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))
    monkeypatch.setattr(
        repo_health.subprocess, "run", lambda *a, **k: MockCompletedProcess(returncode=0)
    )
    repo_health.suite_baseline_finding("/c")
    doc_after = repo_health.suite_baseline_finding.__doc__
    assert doc_before == doc_after, (
        "suite_baseline_finding.__doc__ changed as a side effect of calling "
        "the function - the docstring must be static, not runtime-mutated"
    )


# ---------------------------------------------------------------------------
# Gate / opt-in behaviour
# ---------------------------------------------------------------------------


def test_flag_unset_returns_none_and_no_run(monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))
    monkeypatch.setattr(repo_health.subprocess, "run", _stub_run_that_fails())
    assert repo_health.suite_baseline_finding("/c") is None


def test_flag_zero_returns_none_and_no_run(monkeypatch):
    _enable_flag(monkeypatch, "0")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))
    monkeypatch.setattr(repo_health.subprocess, "run", _stub_run_that_fails())
    assert repo_health.suite_baseline_finding("/c") is None


def test_flag_off_returns_none_and_no_run(monkeypatch):
    _enable_flag(monkeypatch, "off")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))
    monkeypatch.setattr(repo_health.subprocess, "run", _stub_run_that_fails())
    assert repo_health.suite_baseline_finding("/c") is None


def test_flag_banana_returns_none_and_no_run(monkeypatch):
    # Fail closed on an unrecognized value.
    _enable_flag(monkeypatch, "banana")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))
    monkeypatch.setattr(repo_health.subprocess, "run", _stub_run_that_fails())
    assert repo_health.suite_baseline_finding("/c") is None


def test_flag_whitespace_around_value_is_stripped(monkeypatch):
    # The gate is read with .strip().lower(), so surrounding whitespace and
    # mixed case must still enable the probe.
    _enable_flag(monkeypatch, "  TRUE  ")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))
    monkeypatch.setattr(
        repo_health.subprocess, "run", lambda *a, **k: MockCompletedProcess(returncode=0)
    )
    assert repo_health.suite_baseline_finding("/c") is None


# ---------------------------------------------------------------------------
# Happy path return codes
# ---------------------------------------------------------------------------


def test_flag_one_returncode_zero_returns_none(monkeypatch):
    _enable_flag(monkeypatch, "1")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))
    monkeypatch.setattr(
        repo_health.subprocess, "run", lambda *a, **k: MockCompletedProcess(returncode=0)
    )
    assert repo_health.suite_baseline_finding("/c") is None


def test_flag_one_returncode_five_returns_none(monkeypatch):
    # pytest's "no tests collected" exit code is not a red baseline.
    _enable_flag(monkeypatch, "1")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))
    monkeypatch.setattr(
        repo_health.subprocess, "run", lambda *a, **k: MockCompletedProcess(returncode=5)
    )
    assert repo_health.suite_baseline_finding("/c") is None


def test_flag_yes_returncode_zero_returns_none(monkeypatch):
    _enable_flag(monkeypatch, "yes")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))
    monkeypatch.setattr(
        repo_health.subprocess, "run", lambda *a, **k: MockCompletedProcess(returncode=0)
    )
    assert repo_health.suite_baseline_finding("/c") is None


# ---------------------------------------------------------------------------
# Red baseline finding
# ---------------------------------------------------------------------------


def test_flag_one_returncode_one_red_finding(monkeypatch):
    _enable_flag(monkeypatch, "1")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))
    monkeypatch.setattr(
        repo_health.subprocess,
        "run",
        lambda *a, **k: MockCompletedProcess(returncode=1, stdout="", stderr="FAILED test_x"),
    )
    result = repo_health.suite_baseline_finding("/c")
    assert result is not None
    assert result["kind"] == "suite_baseline_red"
    assert "FAILED test_x" in result["detail"]


def test_red_finding_command_is_joined(monkeypatch):
    _enable_flag(monkeypatch, "1")
    monkeypatch.setattr(
        repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest", "-q", "tests/"])
    )
    monkeypatch.setattr(
        repo_health.subprocess,
        "run",
        lambda *a, **k: MockCompletedProcess(returncode=1, stdout="FAILED test_x", stderr=""),
    )
    result = repo_health.suite_baseline_finding("/c")
    assert result["command"] == "pytest -q tests/"


def test_red_finding_truncates_to_last_800(monkeypatch):
    _enable_flag(monkeypatch, "1")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))
    long_out = "x" * 5000
    monkeypatch.setattr(
        repo_health.subprocess,
        "run",
        lambda *a, **k: MockCompletedProcess(returncode=1, stdout=long_out, stderr=""),
    )
    result = repo_health.suite_baseline_finding("/c")
    assert result["kind"] == "suite_baseline_red"
    assert len(result["detail"]) == 800
    assert result["detail"] == long_out[-800:]


def test_red_finding_combines_stdout_and_stderr(monkeypatch):
    _enable_flag(monkeypatch, "1")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))
    monkeypatch.setattr(
        repo_health.subprocess,
        "run",
        lambda *a, **k: MockCompletedProcess(returncode=1, stdout="HEAD", stderr="TAIL"),
    )
    result = repo_health.suite_baseline_finding("/c")
    assert "HEAD" in result["detail"]
    assert "TAIL" in result["detail"]


# ---------------------------------------------------------------------------
# Probe failure (never raises)
# ---------------------------------------------------------------------------


def test_flag_on_timeout_returns_probe_failed(monkeypatch):
    _enable_flag(monkeypatch, "on")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=600)

    monkeypatch.setattr(repo_health.subprocess, "run", boom)
    result = repo_health.suite_baseline_finding("/c")
    assert result["kind"] == "suite_probe_failed"
    assert result["detail"].startswith("TimeoutExpired:")
    assert result["command"] == ""


def test_oserror_returns_probe_failed(monkeypatch):
    _enable_flag(monkeypatch, "1")
    monkeypatch.setattr(repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest"]))

    def boom(*args, **kwargs):
        raise OSError("no such binary")

    monkeypatch.setattr(repo_health.subprocess, "run", boom)
    result = repo_health.suite_baseline_finding("/c")
    assert result["kind"] == "suite_probe_failed"
    assert result["detail"] == "OSError: no such binary"
    assert result["command"] == ""


# ---------------------------------------------------------------------------
# Delegation details
# ---------------------------------------------------------------------------


def test_detect_test_command_called_with_path(monkeypatch):
    _enable_flag(monkeypatch, "1")
    seen = []

    def fake_detect(checkout):
        seen.append(checkout)
        return Path("/fake/testdir"), ["pytest", "-q"]

    monkeypatch.setattr(repo_health, "detect_test_command", fake_detect)
    monkeypatch.setattr(
        repo_health.subprocess, "run", lambda *a, **k: MockCompletedProcess(returncode=0)
    )
    repo_health.suite_baseline_finding("/some/checkout")
    assert len(seen) == 1
    assert isinstance(seen[0], Path)
    assert str(seen[0]) == "/some/checkout"


def test_subprocess_run_kwargs(monkeypatch):
    _enable_flag(monkeypatch, "1")
    monkeypatch.setattr(
        repo_health, "detect_test_command", lambda c: (Path("/td"), ["pytest", "-q"])
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return MockCompletedProcess(returncode=0)

    monkeypatch.setattr(repo_health.subprocess, "run", fake_run)
    repo_health.suite_baseline_finding("/c", timeout_s=123)
    assert captured["cmd"] == ["pytest", "-q"]
    assert captured["kwargs"]["cwd"] == Path("/td")
    assert captured["kwargs"]["timeout"] == 123
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["check"] is False


# ---------------------------------------------------------------------------
# Static / mechanical requirements
# ---------------------------------------------------------------------------


def test_detect_test_command_imported():
    assert hasattr(repo_health, "detect_test_command")


def test_suite_baseline_finding_in_all():
    assert "suite_baseline_finding" in repo_health.__all__


def test_default_timeout_is_600():
    sig = inspect.signature(repo_health.suite_baseline_finding)
    assert sig.parameters["timeout_s"].default == 600


def test_env_read_is_first_statement():
    # The gate must be read at CALL time as the first statement of the
    # function body - not hoisted into a module-level constant.
    tree = ast.parse(inspect.getsource(repo_health.suite_baseline_finding))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    first = func.body[0]
    assert isinstance(first, ast.Assign), "first statement must be the env read"
    call = first.value
    assert isinstance(call, ast.Call)
    func_name = call.func
    assert isinstance(func_name, ast.Attribute)
    assert func_name.attr == "get"
    assert isinstance(func_name.value, ast.Attribute)
    assert func_name.value.attr == "environ"
    # The env var name must be referenced in the call.
    assert any(
        isinstance(node, ast.Constant) and node.value == _FLAG
        for node in ast.walk(call)
    )


def test_no_module_level_env_constant():
    # The env read must live inside the function, not at module scope.
    module_src = inspect.getsource(repo_health)
    func_src = inspect.getsource(repo_health.suite_baseline_finding)
    assert _FLAG in func_src
    module_without_func = module_src.replace(func_src, "")
    assert _FLAG not in module_without_func


def test_docstring_documents_optin_reason():
    doc = repo_health.suite_baseline_finding.__doc__ or ""
    lowered = doc.lower()
    assert "opt-in" in lowered
    assert "eight minutes" in lowered
    assert "scheduler tick" in lowered
