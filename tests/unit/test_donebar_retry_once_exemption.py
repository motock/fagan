"""Regression bar for the retry-once suite exemption (FSU-01).

`_full_suite_result` re-runs a failing full suite ONCE before it rejects
`done`. A green retry proves the failure was not this story's change - a
stale recorded failure, a flake, or a transient collision with another
agent's run - so it must not reject. Live incident: MFR-06, where the
done-gate enforced three smoke tests that passed on re-run, burning the
local attempt's whole step budget and escalating a correct change.

A reproducible failure (red again on the retry) rejects exactly as before;
the returned tail is the FIRST run's, which carries the real failure text.

External boundaries (subprocess.run / detect_test_command / detect_lint_command)
are MOCKED, mirroring tests/unit/test_donebar_gate_aware.py. The real
local_agent loop is never spun.
"""
import contextlib
import importlib.util
import os
from pathlib import Path

import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")

_SCRIPTS = Path(__file__).parent.parent.parent / "scripts"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


la = _load("local_agent", _SCRIPTS / "local_agent.py")
lao = _load("local_agent_oracle", _SCRIPTS / "local_agent_oracle.py")

BOTH = [la, lao]


class _R:
    """Fake subprocess.run result."""

    def __init__(self, rc, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _install_suite_mocks(monkeypatch, mod, test_cmd, lint_cmd, run_impl):
    """Wire detect_test_command / detect_lint_command / _is_heavy / subprocess.run
    on `mod` so `_full_suite_result` can be exercised in isolation."""
    monkeypatch.setattr(mod, "CWD", "/repo")
    monkeypatch.setattr(mod.p, "detect_test_command", lambda cwd: ("/repo", test_cmd))
    monkeypatch.setattr(mod.p, "detect_lint_command", lambda cwd: lint_cmd)
    monkeypatch.setattr(mod.p, "_is_heavy", lambda argv: False)
    monkeypatch.setattr(mod.subprocess, "run", run_impl)


def _sequence_runner(calls, returncodes):
    """A subprocess.run stub returning one rc per call, in order."""
    pending = list(returncodes)

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        rc = pending.pop(0) if pending else returncodes[-1]
        return _R(rc, err="FAILED test_x.py::test_y")

    return _run


@pytest.mark.parametrize("mod", BOTH)
def test_retry_once_exempts_a_transient_suite_failure(monkeypatch, mod):
    """First run red, retry green -> not a rejection: the failure was not the
    story's change. Lint is not detected here, so the result is fully green."""
    calls = []
    _install_suite_mocks(monkeypatch, mod, ["pytest", "-q"], None,
                         _sequence_runner(calls, [1, 0]))

    result = mod._full_suite_result()

    assert result == (True, "", None)
    assert calls == [["pytest", "-q"], ["pytest", "-q"]]


@pytest.mark.parametrize("mod", BOTH)
def test_retry_once_still_rejects_a_reproducible_suite_failure(monkeypatch, mod):
    """Red on both runs -> rejected exactly as before, with the FIRST run's
    failure tail, and lint still never invoked on a red suite."""
    calls = []
    _install_suite_mocks(monkeypatch, mod, ["pytest", "-q"],
                         ("/repo", ["ruff", "check", "."]),
                         _sequence_runner(calls, [1, 1]))

    ok, tail, gate = mod._full_suite_result()

    assert ok is False
    assert gate == "test"
    assert "test_y" in tail
    assert calls == [["pytest", "-q"], ["pytest", "-q"]]


# --- boundary / fail-closed cases -----------------------------------------


@pytest.mark.parametrize("mod", BOTH)
def test_green_first_run_is_not_retried(monkeypatch, mod):
    """A green suite is invoked exactly once - the retry is red-only."""
    calls = []
    _install_suite_mocks(monkeypatch, mod, ["pytest", "-q"], None,
                         _sequence_runner(calls, [0]))

    assert mod._full_suite_result() == (True, "", None)
    assert calls == [["pytest", "-q"]]


@pytest.mark.parametrize("mod", BOTH)
def test_exactly_one_retry_never_a_loop(monkeypatch, mod):
    """Red, red, green -> still rejected after exactly two invocations: the
    retry is a single extra run, never a loop."""
    calls = []
    _install_suite_mocks(monkeypatch, mod, ["pytest", "-q"], None,
                         _sequence_runner(calls, [1, 1, 0]))

    ok, _tail, gate = mod._full_suite_result()

    assert ok is False
    assert gate == "test"
    assert calls == [["pytest", "-q"], ["pytest", "-q"]]


@pytest.mark.parametrize("mod", BOTH)
def test_rejection_tail_is_the_first_runs(monkeypatch, mod):
    """The rejection carries the FIRST run's tail, not the retry's - the first
    run holds the real failure text."""
    calls = []
    outputs = ["FIRST_RUN_FAILURE", "RETRY_FAILURE"]

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        return _R(1, err=outputs[min(len(calls) - 1, 1)])

    _install_suite_mocks(monkeypatch, mod, ["pytest", "-q"], None, _run)

    ok, tail, gate = mod._full_suite_result()

    assert ok is False
    assert gate == "test"
    assert "FIRST_RUN_FAILURE" in tail
    assert "RETRY_FAILURE" not in tail
    assert calls == [["pytest", "-q"], ["pytest", "-q"]]


@pytest.mark.parametrize("mod", BOTH)
def test_no_detected_test_command_stays_green_and_runs_nothing(monkeypatch, mod):
    """The `if not test_cmd: return True, "", None` early return is untouched:
    no test command -> green, and neither pytest nor lint is invoked."""
    calls = []
    _install_suite_mocks(monkeypatch, mod, None, ("/repo", ["ruff", "check", "."]),
                         _sequence_runner(calls, [1]))

    assert mod._full_suite_result() == (True, "", None)
    assert calls == []


@pytest.mark.parametrize("mod", BOTH)
def test_lint_failure_is_never_retried(monkeypatch, mod):
    """Lint is evaluated only once the tests are green, and a lint failure is
    reported as-is - exactly one pytest run and exactly one lint run."""
    calls = []
    _install_suite_mocks(monkeypatch, mod, ["pytest", "-q"],
                         ("/repo", ["ruff", "check", "."]),
                         _sequence_runner(calls, [0, 1]))

    ok, _tail, gate = mod._full_suite_result()

    assert ok is False
    assert gate == "lint"
    assert calls == [["pytest", "-q"], ["ruff", "check", "."]]


@pytest.mark.parametrize("mod", BOTH)
def test_heavy_path_still_retries_once(monkeypatch, mod):
    """The heavy-lock branch is preserved by the hoisted runner: a heavy test
    command still runs under the lock, and still retries once."""
    calls = []
    monkeypatch.setattr(mod, "CWD", "/repo")
    monkeypatch.setattr(mod.p, "detect_test_command", lambda cwd: ("/repo", ["pytest", "-q"]))
    monkeypatch.setattr(mod.p, "detect_lint_command", lambda cwd: None)
    monkeypatch.setattr(mod.p, "_is_heavy", lambda argv: True)
    monkeypatch.setattr(mod.p, "_heavy_lock", lambda: contextlib.nullcontext())
    monkeypatch.setattr(mod.subprocess, "run", _sequence_runner(calls, [1, 0]))

    assert mod._full_suite_result() == (True, "", None)
    assert calls == [["pytest", "-q"], ["pytest", "-q"]]
