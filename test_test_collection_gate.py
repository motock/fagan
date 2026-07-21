"""Tests for the test-collection gate used by check_story_status.

ROOT CAUSE these tests guard: pyproject.toml pins ``testpaths`` to an explicit
allowlist of test files. The harness ``tests_passed`` gate in
``check_story_status`` (pipeline/server.py) runs the suite via
``detect_test_command`` + ``subprocess.run``; pytest honors that allowlist, so
any NEW standalone ``test_*.py`` in the repo root is silently never collected.
The gate runs only the allowlisted tests, they pass, and the story is marked
``tests_passed`` even though the agent's own new test file is broken or empty.

These tests exercise the observable contract of ``detect_test_command``: the
command it returns, when run in a worktree, must collect ALL real root
``test_*.py`` files while STILL excluding experiment-artifact dirs
(``tests/benchmark/_runs/`` and similar) that the original allowlist protected.

Each test builds a self-contained throwaway "worktree" in a tmp_path: a
pyproject.toml with the allowlist-style testpaths, plus whatever test files the
scenario needs. It then calls ``detect_test_command`` and runs the returned
command via subprocess, asserting on the collection outcome (returncode and
stdout/stderr content).
"""

import subprocess
import textwrap
from pathlib import Path

from pipeline.build_detect import detect_test_command


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_pyproject(worktree: Path, testpaths: list[str]) -> None:
    """Write a pyproject.toml with an explicit testpaths allowlist, mirroring
    the production config that caused the gap."""
    tp_lines = "\n".join(f'    "{p}",' for p in testpaths)
    worktree.joinpath("pyproject.toml").write_text(textwrap.dedent(f"""\
        [tool.pytest.ini_options]
        # Pinned allowlist so experiment artifacts never get collected by CI.
        testpaths = [
        {tp_lines}
        ]
    """))


def _run_gate(worktree: Path) -> subprocess.CompletedProcess:
    """Run the test-collection gate exactly as check_story_status does:
    detect the command, then run it in the worktree via subprocess.

    Returns the CompletedProcess so callers can assert on returncode / output.
    """
    test_dir, test_cmd = detect_test_command(worktree)
    return subprocess.run(
        test_cmd, cwd=str(test_dir), capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Happy path: a new standalone root test_*.py with a failing assertion MUST
# cause the gate to report tests_failed (returncode != 0), not tests_passed.
# ---------------------------------------------------------------------------

def test_new_failing_root_test_is_collected_and_fails_gate(tmp_path: Path):
    """A new standalone test_*.py in the repo root with a failing assertion
    must cause the gate to report tests_failed (non-zero returncode), not
    tests_passed.  Before the fix the allowlist silently skipped it."""
    worktree = tmp_path / "wt"
    worktree.mkdir()

    # An allowlisted, always-passing test (represents the existing suite).
    existing = worktree / "test_existing.py"
    existing.write_text("def test_ok():\n    assert True\n")

    # A NEW root test file NOT in the allowlist, with a failing assertion.
    new = worktree / "test_new_feature.py"
    new.write_text("def test_deliberately_failing():\n    assert False, 'new file must be collected'\n")

    _write_pyproject(worktree, testpaths=["test_existing.py"])

    result = _run_gate(worktree)

    # The gate must have collected test_new_feature.py and therefore failed.
    assert result.returncode != 0, (
        "Gate passed (returncode 0) — the new root test_*.py was NOT collected. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # And the failure must be because of the new file, not a spurious error.
    combined = result.stdout + result.stderr
    assert "test_new_feature" in combined or "test_deliberately_failing" in combined, (
        f"New test file not referenced in gate output: {combined!r}"
    )


# ---------------------------------------------------------------------------
# Regression guard: a sentinel test under tests/benchmark/_runs/ must NOT be
# collected/run by the gate (the original reason for the allowlist).
# ---------------------------------------------------------------------------

def test_benchmark_runs_artifact_dir_is_excluded(tmp_path: Path):
    """A sentinel test file placed under tests/benchmark/_runs/ must NOT be
    collected/run by the gate.  This is the regression guard for the original
    allowlist purpose (experiment artifacts must never affect CI)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()

    # A passing root test so the gate has something real to run.
    root_test = worktree / "test_root_ok.py"
    root_test.write_text("def test_ok():\n    assert True\n")

    # The experiment-artifact dir with a sentinel test that would FAIL if run.
    artifact_dir = worktree / "tests" / "benchmark" / "_runs" / "arm-0"
    artifact_dir.mkdir(parents=True)
    sentinel = artifact_dir / "test_acceptance.py"
    sentinel.write_text(
        "def test_sentinel_must_not_run():\n    assert False, 'artifact dir leaked into collection'\n"
    )

    _write_pyproject(worktree, testpaths=["test_root_ok.py"])

    result = _run_gate(worktree)

    # The sentinel must NOT have been collected: the gate passes because the
    # only collected test (test_root_ok) passes, and the sentinel's failure
    # never appears.
    assert result.returncode == 0, (
        "Gate failed — the tests/benchmark/_runs/ sentinel was collected. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "test_sentinel_must_not_run" not in combined, (
        f"Sentinel from tests/benchmark/_runs/ appeared in gate output: {combined!r}"
    )
    assert "test_acceptance" not in combined or "test_root_ok" in combined


# ---------------------------------------------------------------------------
# Boundary: an empty new test file (no test functions) is collected but does
# not break the gate (0 tests from it, overall pass).
# ---------------------------------------------------------------------------

def test_empty_new_root_test_does_not_break_gate(tmp_path: Path):
    """An empty new test file (no test functions) is collected but does not
    break the gate — it contributes 0 tests and the overall run passes."""
    worktree = tmp_path / "wt"
    worktree.mkdir()

    root_test = worktree / "test_root_ok.py"
    root_test.write_text("def test_ok():\n    assert True\n")

    empty_new = worktree / "test_empty_new.py"
    empty_new.write_text("# intentionally empty — no test functions\n")

    _write_pyproject(worktree, testpaths=["test_root_ok.py"])

    result = _run_gate(worktree)

    # The empty file must not cause a failure (no collection error, 0 tests).
    assert result.returncode == 0, (
        f"Empty new test file broke the gate: stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Negative: a new test file with a syntax error surfaces as a collection
# error (tests_failed), not silently ignored.
# ---------------------------------------------------------------------------

def test_syntax_error_root_test_surfaces_as_collection_error(tmp_path: Path):
    """A new test file with a syntax error must surface as a collection error
    (tests_failed / non-zero returncode), not be silently ignored."""
    worktree = tmp_path / "wt"
    worktree.mkdir()

    root_test = worktree / "test_root_ok.py"
    root_test.write_text("def test_ok():\n    assert True\n")

    bad = worktree / "test_syntax_error.py"
    bad.write_text("def test_broken(:\n    this is not valid python\n")

    _write_pyproject(worktree, testpaths=["test_root_ok.py"])

    result = _run_gate(worktree)

    # A syntax error in a collected file is a collection error → non-zero.
    assert result.returncode != 0, (
        "Gate passed despite a syntax-error test file in the root — it was "
        f"silently ignored. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "test_syntax_error" in combined or "SyntaxError" in combined, (
        f"Syntax-error file not surfaced in gate output: {combined!r}"
    )


# ---------------------------------------------------------------------------
# Full-suite guard: multiple existing root test files are all collected and
# pass (the fix must not drop previously-allowlisted files).
# ---------------------------------------------------------------------------

def test_all_root_test_files_are_collected(tmp_path: Path):
    """Multiple root test_*.py files (some allowlisted, some new) must all be
    collected.  Here every file passes, so the gate passes — but we verify
    collection breadth by checking that a NEW (non-allowlisted) passing test
    is actually run (its output appears)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()

    a = worktree / "test_alpha.py"
    a.write_text("def test_a():\n    assert True\n")

    b = worktree / "test_beta.py"
    b.write_text("def test_b():\n    assert True\n")

    # A NEW file not in the allowlist, with a uniquely-named passing test.
    c = worktree / "test_gamma_new.py"
    c.write_text("def test_gamma_unique_marker():\n    assert True\n")

    _write_pyproject(worktree, testpaths=["test_alpha.py", "test_beta.py"])

    result = _run_gate(worktree)

    assert result.returncode == 0, (
        f"Gate failed on all-passing suite: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    # The new (non-allowlisted) file must have been collected and run.
    assert "test_gamma_unique_marker" in combined or "test_gamma_new" in combined, (
        f"New passing root test was not collected: {combined!r}"
    )
