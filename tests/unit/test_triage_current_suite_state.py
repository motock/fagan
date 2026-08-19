"""Unit tests for ``pipeline.triage._current_suite_state``.

This helper runs the REAL test suite against a worktree's current HEAD (unlike
``collect_failure_evidence``, which only reads cached/stale state). It is the
fix for the gap found live on story 30e5f9fc-3681-40db-ae54-dd95387dd1e7, where
a story sat parked with an already-passing suite because nothing ever
re-checked.

These tests are written FIRST (TDD). They import ``pipeline.triage``, which
does not yet expose ``_current_suite_state`` (or its new module-level imports
``detect_test_command`` / ``_is_heavy`` / ``_heavy_lock``), so they currently
fail with ``AttributeError`` / ``ImportError`` - the correct RED state. A
later dispatch implements ``pipeline/triage.py`` against them.

CRITICAL: a real subprocess must NEVER run from inside this test module - a
real pytest invocation from inside this repo's own test suite would recurse.
Every test monkeypatches ``pipeline.triage.subprocess.run`` (and the other
helpers the implementation imports into its own namespace) before exercising
the function.
"""
import subprocess

import pytest

from pipeline import triage  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _never_run(*args, **kwargs):
    """A stub for subprocess.run that fails the test if ever invoked."""
    pytest.fail("subprocess.run must NOT be called for this case")


def _never_heavy_lock():
    """A fake _heavy_lock whose __enter__ fails the test if entered."""
    @pytest.fail  # noqa: E731  (not actually used as decorator)
    class _Boom:
        def __enter__(self):
            pytest.fail("_heavy_lock must NOT be entered for this case")
        def __exit__(self, *exc):
            return False
    return _Boom()


def _make_run(returncode=0, stdout="", stderr=""):
    """Build a subprocess.run stub returning a fake CompletedProcess."""
    def _run(*args, **kwargs):
        class _R:
            pass
        r = _R()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r
    return _run


class _RecordingLock:
    """A fake _heavy_lock that records enter/exit order relative to run.

    The implementation must call _heavy_lock().__enter__() BEFORE it calls
    subprocess.run, and __exit__() AFTER. We capture the call order into a
    shared list.
    """

    def __init__(self, log, run_stub):
        self._log = log
        self._run_stub = run_stub

    def __enter__(self):
        self._log.append("lock_enter")
        # Replace subprocess.run with the recording variant now, so the
        # order is: lock_enter, run, lock_exit.
        return self

    def __exit__(self, *exc):
        self._log.append("lock_exit")
        return False


# ---------------------------------------------------------------------------
# Module-level import contract
# ---------------------------------------------------------------------------

def test_module_imports_detect_test_command_from_build_detect():
    """The module must import detect_test_command at module level so it is
    patchable as triage.detect_test_command."""
    assert hasattr(triage, "detect_test_command")


def test_module_imports_is_heavy_from_concurrency():
    assert hasattr(triage, "_is_heavy")


def test_module_imports_heavy_lock_from_concurrency():
    assert hasattr(triage, "_heavy_lock")


def test_module_imports_subprocess():
    """The implementation must `import subprocess` at module level so it is
    patchable as triage.subprocess.run."""
    assert hasattr(triage, "subprocess")


def test_module_imports_os():
    assert hasattr(triage, "os")


def test_module_imports_path_from_pathlib():
    assert hasattr(triage, "Path")


def test_current_suite_state_in_all():
    """_current_suite_state must be exported in __all__ (codebase convention
    for underscore-prefixed helpers that tests reference directly)."""
    assert hasattr(triage, "__all__")
    assert "_current_suite_state" in triage.__all__


def test_current_suite_state_exists():
    assert hasattr(triage, "_current_suite_state")
    assert callable(triage._current_suite_state)


# ---------------------------------------------------------------------------
# Empty / falsy worktree guard
# ---------------------------------------------------------------------------

def test_empty_worktree_returns_empty_and_never_runs(monkeypatch):
    """An empty/falsy worktree must return '' immediately and never fall
    through to running a suite against the current process's own cwd."""
    monkeypatch.setattr(triage, "subprocess.run", _never_run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: pytest.fail("detect_test_command must NOT be called"))
    assert triage._current_suite_state("") == ""


def test_none_worktree_returns_empty_and_never_runs(monkeypatch):
    monkeypatch.setattr(triage, "subprocess.run", _never_run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: pytest.fail("detect_test_command must NOT be called"))
    assert triage._current_suite_state(None) == ""


# ---------------------------------------------------------------------------
# Timeout escape hatch
# ---------------------------------------------------------------------------

def test_timeout_zero_disables_check_and_never_runs(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", "0")
    monkeypatch.setattr(triage, "subprocess.run", _never_run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: pytest.fail("detect_test_command must NOT be called"))
    assert triage._current_suite_state("/some/worktree") == ""


def test_timeout_negative_disables_check_and_never_runs(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", "-5")
    monkeypatch.setattr(triage, "subprocess.run", _never_run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: pytest.fail("detect_test_command must NOT be called"))
    assert triage._current_suite_state("/some/worktree") == ""


def test_timeout_unset_defaults_to_240(monkeypatch):
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)
    captured = {}

    def _run(*args, **kwargs):
        captured["kwargs"] = kwargs
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(triage, "subprocess.run", _run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    triage._current_suite_state("/some/worktree")
    assert "timeout" in captured["kwargs"]
    assert captured["kwargs"]["timeout"] == 240


def test_timeout_garbage_falls_back_to_240(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", "banana")
    captured = {}

    def _run(*args, **kwargs):
        captured["kwargs"] = kwargs
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(triage, "subprocess.run", _run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    # Must not raise.
    triage._current_suite_state("/some/worktree")
    assert captured["kwargs"]["timeout"] == 240


def test_timeout_explicit_value_used(monkeypatch):
    """A valid positive timeout is used verbatim."""
    monkeypatch.setenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", "77")
    captured = {}

    def _run(*args, **kwargs):
        captured["kwargs"] = kwargs
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(triage, "subprocess.run", _run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    triage._current_suite_state("/some/worktree")
    assert captured["kwargs"]["timeout"] == 77


# ---------------------------------------------------------------------------
# Empty test command
# ---------------------------------------------------------------------------

def test_empty_test_command_returns_empty_and_never_runs(monkeypatch):
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)
    monkeypatch.setattr(triage, "subprocess.run", _never_run)
    monkeypatch.setattr(triage, "detect_test_command", lambda p: (p, []))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    assert triage._current_suite_state("/some/worktree") == ""


# ---------------------------------------------------------------------------
# Returncode outcomes
# ---------------------------------------------------------------------------

def test_returncode_zero_yields_pass_string(monkeypatch):
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)
    monkeypatch.setattr(triage, "subprocess.run", _make_run(returncode=0))
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    result = triage._current_suite_state("/some/worktree")
    assert result == "CURRENT STATE: full test suite PASSES at the worktree's current HEAD."


def test_returncode_five_yields_empty(monkeypatch):
    """rc=5 is pytest's 'no tests collected' - inconclusive, not a pass."""
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)
    monkeypatch.setattr(triage, "subprocess.run", _make_run(returncode=5))
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    assert triage._current_suite_state("/some/worktree") == ""


def test_returncode_one_failure_string_contains_rc_and_tail(monkeypatch):
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)
    monkeypatch.setattr(
        triage, "subprocess.run",
        _make_run(returncode=1, stdout="FAILED test_x::test_y\n", stderr="boom"),
    )
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    result = triage._current_suite_state("/some/worktree")
    assert result.startswith("CURRENT STATE: full test suite FAILS")
    assert "rc=1" in result
    assert "FAILED test_x::test_y" in result


def test_failure_string_uses_last_500_chars_of_combined_output(monkeypatch):
    """The failure tail must be (stdout + stderr)[-500:]."""
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)
    big = "A" * 600
    monkeypatch.setattr(
        triage, "subprocess.run",
        _make_run(returncode=2, stdout=big, stderr="B" * 600),
    )
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    result = triage._current_suite_state("/some/worktree")
    # Combined output is 1200 chars; tail is last 500.
    tail = (big + "B" * 600)[-500:]
    assert result.endswith(tail)
    assert "rc=2" in result


# ---------------------------------------------------------------------------
# Exception fail-open
# ---------------------------------------------------------------------------

def test_timeout_expired_returns_empty_no_propagation(monkeypatch):
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)

    def _run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["pytest"], timeout=1)

    monkeypatch.setattr(triage, "subprocess.run", _run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    assert triage._current_suite_state("/some/worktree") == ""


def test_oserror_returns_empty_no_propagation(monkeypatch):
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)

    def _run(*args, **kwargs):
        raise OSError("no such binary")

    monkeypatch.setattr(triage, "subprocess.run", _run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    assert triage._current_suite_state("/some/worktree") == ""


def test_subprocess_error_returns_empty_no_propagation(monkeypatch):
    """Any subprocess.SubprocessError (not just TimeoutExpired) fails open."""
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)

    def _run(*args, **kwargs):
        raise subprocess.SubprocessError("generic")

    monkeypatch.setattr(triage, "subprocess.run", _run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    assert triage._current_suite_state("/some/worktree") == ""


# ---------------------------------------------------------------------------
# Heavy lock wiring
# ---------------------------------------------------------------------------

def test_is_heavy_true_enters_heavy_lock_around_run(monkeypatch):
    """When _is_heavy returns True, _heavy_lock must be entered AROUND the
    subprocess.run call: enter before run, exit after run."""
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)
    log = []

    def _run(*args, **kwargs):
        log.append("run")
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    class _Lock:
        def __enter__(self):
            log.append("lock_enter")
            return self
        def __exit__(self, *exc):
            log.append("lock_exit")
            return False

    monkeypatch.setattr(triage, "subprocess.run", _run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["cargo", "test"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: True)
    monkeypatch.setattr(triage, "_heavy_lock", lambda: _Lock())
    triage._current_suite_state("/some/worktree")
    assert log == ["lock_enter", "run", "lock_exit"], (
        f"expected lock_enter -> run -> lock_exit, got {log!r}"
    )


def test_is_heavy_false_does_not_enter_heavy_lock(monkeypatch):
    """When _is_heavy returns False, _heavy_lock must NOT be entered at all."""
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)

    class _BoomLock:
        def __enter__(self):
            pytest.fail("_heavy_lock must NOT be entered when _is_heavy is False")
        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(triage, "subprocess.run", _make_run(returncode=0))
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", lambda: _BoomLock())
    result = triage._current_suite_state("/some/worktree")
    assert result == "CURRENT STATE: full test suite PASSES at the worktree's current HEAD."


def test_is_heavy_called_with_test_cmd(monkeypatch):
    """_is_heavy must be called with the test_cmd returned by
    detect_test_command (mirrors local_agent_oracle._full_suite_result)."""
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)
    seen = {}

    def _is_heavy(cmd):
        seen["cmd"] = cmd
        return False

    monkeypatch.setattr(triage, "subprocess.run", _make_run(returncode=0))
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-x"]))
    monkeypatch.setattr(triage, "_is_heavy", _is_heavy)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    triage._current_suite_state("/some/worktree")
    assert seen["cmd"] == ["pytest", "-x"]


# ---------------------------------------------------------------------------
# subprocess.run invocation shape
# ---------------------------------------------------------------------------

def test_subprocess_run_called_with_cwd_capture_text_check_false(monkeypatch):
    """The run must mirror local_agent_oracle._full_suite_result exactly:
    cwd=test_dir, capture_output=True, text=True, check=False."""
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)
    captured = {}

    def _run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(triage, "subprocess.run", _run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: (p, ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    triage._current_suite_state("/some/worktree")
    assert captured["args"][0] == ["pytest", "-q"]
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["check"] is False
    assert "timeout" in captured["kwargs"]


def test_subprocess_run_cwd_is_test_dir_from_detect_test_command(monkeypatch):
    """cwd passed to subprocess.run must be the test_dir returned by
    detect_test_command, not the raw worktree."""
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)
    captured = {}

    def _run(*args, **kwargs):
        captured["kwargs"] = kwargs
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(triage, "subprocess.run", _run)
    monkeypatch.setattr(triage, "detect_test_command",
                        lambda p: ("/custom/test/dir", ["pytest", "-q"]))
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    triage._current_suite_state("/some/worktree")
    assert captured["kwargs"]["cwd"] == "/custom/test/dir"


def test_detect_test_command_called_with_path_of_worktree(monkeypatch):
    """detect_test_command must be called with Path(worktree)."""
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)
    seen = {}

    def _dtc(p):
        seen["p"] = p
        return (p, ["pytest", "-q"])

    monkeypatch.setattr(triage, "subprocess.run", _make_run(returncode=0))
    monkeypatch.setattr(triage, "detect_test_command", _dtc)
    monkeypatch.setattr(triage, "_is_heavy", lambda cmd: False)
    monkeypatch.setattr(triage, "_heavy_lock", _never_heavy_lock)
    triage._current_suite_state("/some/worktree")
    # Must be a Path instance wrapping the worktree string.
    assert isinstance(seen["p"], triage.Path)
    assert str(seen["p"]) == "/some/worktree"


# ---------------------------------------------------------------------------
# Docstring contract
# ---------------------------------------------------------------------------

def test_current_suite_state_docstring_mentions_current_head_and_fail_open():
    """The docstring must explain this runs the REAL suite against the
    worktree's CURRENT HEAD and fails open to silence on any problem."""
    doc = triage._current_suite_state.__doc__ or ""
    assert doc, "_current_suite_state must have a docstring"
    low = doc.lower()
    assert "current head" in low
    # Fail-open / never raises contract.
    assert "fail" in low or "never raise" in low or "silence" in low


def test_current_suite_state_never_raises_on_any_input(monkeypatch):
    """The function must NEVER raise - it fails open to '' on any problem."""
    monkeypatch.delenv("PIPELINE_TRIAGE_SUITE_TIMEOUT", raising=False)

    def _boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(triage, "subprocess.run", _boom)
    monkeypatch.setattr(triage, "detect_test_command", _boom)
    monkeypatch.setattr(triage, "_is_heavy", _boom)
    monkeypatch.setattr(triage, "_heavy_lock", _boom)
    # Even with everything exploding, must return a string (empty), not raise.
    result = triage._current_suite_state("/some/worktree")
    assert isinstance(result, str)