"""Tests for the gate-aware rework done-bar (scripts/local_agent.py and
scripts/local_agent_oracle.py).

The two files are kept in sync (see the docstring at local_agent.py:805-818).
Both `_full_suite_result` helpers previously returned a 2-tuple `(passed,
tail)` with NO indication of which gate failed, and every rejection was framed
as a test failure. This suite pins the new 3-tuple `(passed, tail, gate)`
return shape and the gate-aware rejection messages so an agent whose tests
pass but whose lint check fails is told to fix LINT, not to chase a
nonexistent test failure (live incident W1c-08).

External boundaries (subprocess.run / detect_test_command / detect_lint_command)
are MOCKED; the real local_agent loop is never spun.
"""
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


def _run_suite(monkeypatch, mod, test_cmd, lint_cmd, run_impl):
    _install_suite_mocks(monkeypatch, mod, test_cmd, lint_cmd, run_impl)
    return mod._full_suite_result()


# ---------------------------------------------------------------------------
# _full_suite_result return shape / gate labeling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod", BOTH)
def test_full_suite_test_fails_returns_test_gate(monkeypatch, mod):
    """pytest returns nonzero -> (False, tail, 'test'); lint NOT invoked."""
    calls = []

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        return _R(1, err="FAILED test_x.py::test_y - assert 1 == 2")

    result = _run_suite(monkeypatch, mod, ["pytest", "-q"], ("/repo", ["ruff", "check", "."]), _run)
    assert result == (False, "FAILED test_x.py::test_y - assert 1 == 2", "test")
    # pytest runs first; lint is NOT run on a red suite.
    assert calls == [["pytest", "-q"]]


@pytest.mark.parametrize("mod", BOTH)
def test_full_suite_test_passes_lint_fails_returns_lint_gate(monkeypatch, mod):
    """pytest returns 0, ruff returns nonzero -> (False, lint_tail, 'lint')."""
    calls = []

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        if argv[0] == "pytest":
            return _R(0)
        return _R(1, err="test_foo.py:5:1: F401 'os' imported but unused")

    result = _run_suite(monkeypatch, mod, ["pytest", "-q"], ("/repo", ["ruff", "check", "."]), _run)
    assert result == (False, "test_foo.py:5:1: F401 'os' imported but unused", "lint")
    assert calls == [["pytest", "-q"], ["ruff", "check", "."]]


@pytest.mark.parametrize("mod", BOTH)
def test_full_suite_both_pass_returns_none_gate(monkeypatch, mod):
    """both 0 -> (True, '', None)."""
    result = _run_suite(monkeypatch, mod, ["pytest", "-q"], ("/repo", ["ruff", "check", "."]),
                        lambda *a, **k: _R(0))
    assert result == (True, "", None)


@pytest.mark.parametrize("mod", BOTH)
def test_full_suite_no_test_command(monkeypatch, mod):
    """detect_test_command returns None -> (True, '', None)."""
    result = _run_suite(monkeypatch, mod, None, ("/repo", ["ruff", "check", "."]),
                        lambda *a, **k: _R(0))
    assert result == (True, "", None)


@pytest.mark.parametrize("mod", BOTH)
def test_full_suite_tests_pass_no_lint_command(monkeypatch, mod):
    """no lint detected -> (True, '', None)."""
    result = _run_suite(monkeypatch, mod, ["pytest", "-q"], None, lambda *a, **k: _R(0))
    assert result == (True, "", None)


@pytest.mark.parametrize("mod", BOTH)
def test_full_suite_both_fail_reports_test_gate_first(monkeypatch, mod):
    """pytest nonzero AND ruff nonzero -> gate is 'test' (pytest runs first;
    lint skipped on red suite). Assert lint not called."""
    calls = []

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        return _R(1, err="FAILED test_x.py::test_y - assert 1 == 2")

    result = _run_suite(monkeypatch, mod, ["pytest", "-q"], ("/repo", ["ruff", "check", "."]), _run)
    assert result[0] is False
    assert result[2] == "test"
    assert calls == [["pytest", "-q"]]


@pytest.mark.parametrize("mod", BOTH)
def test_full_suite_returns_three_tuple(monkeypatch, mod):
    """The return is a 3-tuple (passed, tail, gate) — guards against a
    regression back to the old 2-tuple shape."""
    result = _run_suite(monkeypatch, mod, ["pytest", "-q"], ("/repo", ["ruff", "check", "."]),
                        lambda *a, **k: _R(0))
    assert isinstance(result, tuple)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# Rejection message path (local_agent._reject_done_for_suite)
# ---------------------------------------------------------------------------

def _reject(monkeypatch, gate, tail="some tail"):
    messages = []
    la._reject_done_for_suite(messages, 7, tail, gate)
    return messages[0]["content"]


def test_rejection_test_gate_message_mentions_pytest_not_ruff(monkeypatch):
    """A 'test' gate keeps the existing pytest-focused wording and must NOT
    mention ruff/lint."""
    msg = _reject(monkeypatch, "test")
    assert "pytest" in msg
    assert "ruff --fix" not in msg
    assert "lint" not in msg.lower().replace("lint gate", "")


def test_rejection_lint_gate_message_tells_agent_to_run_ruff_fix(monkeypatch):
    """A 'lint' gate message (a) states tests PASS, (b) names `ruff check .`,
    (c) instructs `ruff check . --fix`, and (d) says NOT to edit implementation
    logic. It must NOT say 'test suite still fails'."""
    msg = _reject(monkeypatch, "lint")
    assert "tests PASS" in msg
    assert "ruff check ." in msg
    assert "ruff check . --fix" in msg
    assert "Do NOT edit implementation logic" in msg
    assert "test suite still fails" not in msg


def test_rejection_lint_gate_message_does_not_claim_tests_fail(monkeypatch):
    """The lint-gate message must not contain the phrase 'full test suite
    still fails'."""
    msg = _reject(monkeypatch, "lint")
    assert "full test suite still fails" not in msg


def test_rejection_test_gate_keeps_existing_wording(monkeypatch):
    """The 'test' gate message keeps the existing 'full test suite still
    fails' wording and the 'do not call done until pytest passes in full'
    instruction."""
    msg = _reject(monkeypatch, "test")
    assert "full test suite still fails" in msg
    assert "do not call done until pytest passes in full" in msg


def test_rejection_interpolates_the_failure_tail(monkeypatch):
    """The failure excerpt (suite_tail) must be interpolated into the rejection
    message, not appear as the literal text '{suite_tail}'. Without this the
    agent is told a gate failed but never shown *what* failed — defeating the
    done-bar's whole purpose of feeding the excerpt back."""
    for gate in ("test", "lint"):
        msg = _reject(monkeypatch, gate, tail="__TAIL_MARKER_42__")
        assert "__TAIL_MARKER_42__" in msg, f"tail not interpolated for {gate!r} gate"
        assert "{suite_tail}" not in msg, f"literal {{{{suite_tail}}}} leaked for {gate!r} gate"


def test_oracle_rejection_interpolates_full_tail():
    """The oracle's inlined rejection must be an f-string so {full_tail} is
    interpolated into the message, not emitted as literal text (mechanical
    source-level check mirroring the local_agent interpolation test)."""
    src = (_SCRIPTS / "local_agent_oracle.py").read_text()
    assert 'f"Your tests PASS' in src, "oracle lint-gate rejection is not an f-string"
    assert 'f"The acceptance oracle passes' in src, "oracle test-gate rejection is not an f-string"


def test_rejection_print_names_the_gate(monkeypatch, capsys):
    """The print() line announcing the rejection names the gate."""
    la._reject_done_for_suite([], 3, "tail", "lint")
    out = capsys.readouterr().out
    assert "lint gate still fails" in out

    la._reject_done_for_suite([], 3, "tail", "test")
    out = capsys.readouterr().out
    assert "full test suite still fails" in out


# ---------------------------------------------------------------------------
# Oracle inlined rejection message (source-level mechanical check)
# ---------------------------------------------------------------------------

def test_oracle_source_contains_lint_specific_rejection():
    """The oracle's inlined rejection must branch on the gate and carry the
    lint-specific message (tests PASS / ruff check . --fix / do NOT edit
    implementation logic)."""
    src = (_SCRIPTS / "local_agent_oracle.py").read_text()
    assert "ruff check . --fix" in src
    assert "Do NOT edit implementation logic" in src
    assert "tests PASS" in src


def test_oracle_source_keeps_test_gate_wording():
    """The oracle's test-gate rejection keeps the existing 'full test suite
    still fails' wording."""
    src = (_SCRIPTS / "local_agent_oracle.py").read_text()
    assert "full test suite still fails" in src


# ---------------------------------------------------------------------------
# Sync between the two files
# ---------------------------------------------------------------------------

def test_both_files_in_sync():
    """Both _full_suite_result helpers return the same 3-tuple shape with the
    same gate labels for identical inputs — guards against the two files
    drifting."""
    for test_cmd, lint_cmd, run_impl, expected_gate in [
        (["pytest", "-q"], ("/repo", ["ruff", "check", "."]),
         lambda *a, **k: _R(1, err="boom"), "test"),
        (["pytest", "-q"], ("/repo", ["ruff", "check", "."]),
         lambda *a, **k: _R(0) if a[0][0] == "pytest" else _R(1, err="F401"), "lint"),
        (["pytest", "-q"], ("/repo", ["ruff", "check", "."]),
         lambda *a, **k: _R(0), None),
        (None, ("/repo", ["ruff", "check", "."]), lambda *a, **k: _R(0), None),
        (["pytest", "-q"], None, lambda *a, **k: _R(0), None),
    ]:
        gates = []
        for mod in BOTH:
            monkeypatch = pytest.MonkeyPatch()
            try:
                result = _run_suite(monkeypatch, mod, test_cmd, lint_cmd, run_impl)
            finally:
                monkeypatch.undo()
            assert len(result) == 3
            gates.append(result[2])
        assert gates[0] == gates[1] == expected_gate, (
            f"gate drift for test_cmd={test_cmd!r} lint_cmd={lint_cmd!r}: {gates}"
        )


def test_no_remaining_2tuple_return_annotation():
    """The old 2-tuple return annotation on _full_suite_result must be gone
    from both files (it is now a 3-tuple)."""
    for path in (_SCRIPTS / "local_agent.py", _SCRIPTS / "local_agent_oracle.py"):
        src = path.read_text()
        # The _full_suite_result def must not carry the old 2-tuple annotation.
        for line in src.splitlines():
            if "def _full_suite_result" in line:
                assert "tuple[bool, str]" not in line, f"old 2-tuple annotation in {path}"
