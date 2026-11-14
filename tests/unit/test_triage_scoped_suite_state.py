"""``_current_suite_state`` runs the story's scoped command when scoped.

A story in scoped mode carries the argv its last gate run used
(``last_test_check.cmd``) and the targets it graded (``test_scope_grade``).
The triage evidence must run that scoped argv, not the full suite that
times out on this repo. Every other case keeps the full-suite behaviour.

These tests stub ``subprocess.run`` and ``detect_test_command`` so no real
test process runs from inside this suite.
"""

import subprocess
from types import SimpleNamespace

import pytest

from pipeline import triage

FULL_CMD = ["pytest", "-q"]
SCOPED_CMD = ["pytest", "-q", "/wt/tests/unit/test_a.py"]
PASS_SCOPED = "CURRENT STATE: scoped test targets PASS at the worktree's current HEAD."


def _scoped_story(**overrides):
    story = {
        "test_scope_resolution": {"test_scope": "scoped"},
        "last_test_check": {"cmd": list(SCOPED_CMD), "cwd": "/wt"},
        "test_scope_grade": {"targets": ["tests/unit/test_a.py"]},
    }
    story.update(overrides)
    return story


@pytest.fixture
def run_calls(monkeypatch):
    """Stub the suite run and record each argv it was asked to run."""
    recorder = SimpleNamespace(
        calls=[],
        state={"returncode": 0, "stdout": "", "stderr": ""},
    )

    def _run(args, **kwargs):
        recorder.calls.append(list(args))
        return SimpleNamespace(**recorder.state)

    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)
    monkeypatch.setattr(triage, "subprocess.run", _run)
    monkeypatch.setattr(triage, "detect_test_command", lambda p: (p, list(FULL_CMD)))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    return recorder


# ---------------------------------------------------------------------------
# Scoped command selection
# ---------------------------------------------------------------------------


def test_scoped_story_with_targets_runs_the_recorded_scoped_command(run_calls):
    triage._current_suite_state("/wt", _scoped_story())
    assert run_calls.calls == [SCOPED_CMD]


def test_scoped_story_reports_scoped_test_targets_on_pass(run_calls):
    assert triage._current_suite_state("/wt", _scoped_story()) == PASS_SCOPED


def test_scoped_story_failure_names_scoped_test_targets(run_calls):
    run_calls.state.update(returncode=1, stdout="FAILED test_a::test_x\n")
    result = triage._current_suite_state("/wt", _scoped_story())
    assert result.startswith("CURRENT STATE: scoped test targets FAIL")
    assert "rc=1" in result
    assert "FAILED test_a::test_x" in result


def test_plan_resolving_full_runs_the_full_command_despite_recorded_scope(run_calls):
    story = _scoped_story(test_scope_resolution={"test_scope": "full"})
    triage._current_suite_state("/wt", story)
    assert run_calls.calls == [FULL_CMD]


def test_plan_resolving_full_reports_full_suite_on_pass(run_calls):
    story = _scoped_story(test_scope_resolution={"test_scope": "full"})
    assert triage._current_suite_state("/wt", story) == (
        "CURRENT STATE: full test suite PASSES at the worktree's current HEAD."
    )


def test_missing_scope_resolution_runs_the_full_command(run_calls):
    story = _scoped_story()
    del story["test_scope_resolution"]
    triage._current_suite_state("/wt", story)
    assert run_calls.calls == [FULL_CMD]


def test_no_recorded_scoped_command_runs_the_full_command(run_calls):
    story = _scoped_story()
    del story["last_test_check"]
    triage._current_suite_state("/wt", story)
    assert run_calls.calls == [FULL_CMD]


def test_recorded_command_that_is_not_a_list_runs_the_full_command(run_calls):
    story = _scoped_story(last_test_check={"cmd": "pytest -q tests/unit/test_a.py"})
    triage._current_suite_state("/wt", story)
    assert run_calls.calls == [FULL_CMD]


def test_empty_recorded_command_runs_the_full_command(run_calls):
    story = _scoped_story(last_test_check={"cmd": []})
    triage._current_suite_state("/wt", story)
    assert run_calls.calls == [FULL_CMD]


def test_no_graded_targets_runs_the_full_command(run_calls):
    story = _scoped_story(test_scope_grade={"targets": []})
    triage._current_suite_state("/wt", story)
    assert run_calls.calls == [FULL_CMD]


def test_missing_graded_targets_runs_the_full_command(run_calls):
    story = _scoped_story()
    del story["test_scope_grade"]
    triage._current_suite_state("/wt", story)
    assert run_calls.calls == [FULL_CMD]


def test_no_story_runs_the_full_command(run_calls):
    triage._current_suite_state("/wt")
    assert run_calls.calls == [FULL_CMD]


# ---------------------------------------------------------------------------
# Fail-open: the scoped path never asserts a state it did not observe
# ---------------------------------------------------------------------------


def test_scoped_timeout_fails_open_to_silence(run_calls, monkeypatch):
    def _timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=1)

    monkeypatch.setattr(triage, "subprocess.run", _timeout)
    assert triage._current_suite_state("/wt", _scoped_story()) == ""


def test_scoped_os_error_fails_open_to_silence(run_calls, monkeypatch):
    def _missing(args, **kwargs):
        raise OSError("no such binary")

    monkeypatch.setattr(triage, "subprocess.run", _missing)
    assert triage._current_suite_state("/wt", _scoped_story()) == ""


def test_scoped_no_tests_collected_is_silence_not_a_pass(run_calls):
    run_calls.state["returncode"] = 5
    assert triage._current_suite_state("/wt", _scoped_story()) == ""


def test_scoped_run_never_reports_a_pass_for_a_failed_command(run_calls):
    run_calls.state.update(returncode=2, stderr="boom")
    result = triage._current_suite_state("/wt", _scoped_story())
    assert "PASS" not in result
    assert result.startswith("CURRENT STATE: scoped test targets FAIL")
