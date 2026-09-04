"""Verify bare `pytest` collection is no longer gated by a stale file allowlist.

Background: pyproject.toml's ``[tool.pytest.ini_options]`` used to pin
``testpaths`` to an explicit list of 23 ``test_*.py`` files, while
``tests/unit/`` actually holds ~104 test files. A bare ``pytest`` invocation
(no extra CLI flags) silently collected only those 23 and skipped the rest.
The fix removes the ``testpaths`` array and replaces it with
``addopts = "--ignore=tests/benchmark --ignore=tests/experiments"`` so bare
pytest collects everything under the repo *except* the experiment/benchmark
trees (which contain transient ``test_acceptance.py`` artifacts that must
never be collected).

These tests exercise pyproject.toml's own config as-is: they run
``pytest --collect-only -q`` with NO extra flags, exactly what a bare
``pytest`` invocation sees.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The repo root is two levels up from tests/unit/test_*.py:
#   tests/unit/test_pytest_collection_allowlist.py -> parents[2] == repo root
REPO_ROOT = Path(__file__).resolve().parents[2]


def _bare_pytest_collect() -> subprocess.CompletedProcess[str]:
    """Run exactly what a bare `pytest` invocation collects.

    No --override-ini, no --ignore: the whole point is to exercise
    pyproject.toml's own [tool.pytest.ini_options] config as-is.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def collected():
    """One bare-pytest collection run shared across the assertions below."""
    result = _bare_pytest_collect()
    # Surface stderr in the assertion message if the run itself fails, so a
    # genuine subprocess error is not mistaken for a collection-content
    # failure.
    assert result.returncode == 0, (
        "bare `pytest --collect-only -q` exited non-zero "
        f"(rc={result.returncode}):\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    return result


def test_bare_pytest_collects_conftest_env_isolation(collected):
    """The headline regression: a file that exists under tests/unit/ today
    but was NOT in the old testpaths allowlist must now be collected by a
    bare pytest run. Before the pyproject.toml fix this silently failed."""
    assert "test_conftest_env_isolation.py" in collected.stdout, (
        "test_conftest_env_isolation.py was NOT collected by a bare `pytest` "
        "run. This means pyproject.toml's [tool.pytest.ini_options] is still "
        "gating collection via a stale testpaths allowlist (or an equivalent "
        "restriction) instead of collecting all of tests/unit/.\n"
        f"Collected stdout:\n{collected.stdout}"
    )


def test_bare_pytest_excludes_experiments(collected):
    """tests/experiments/ must never appear in a bare-pytest collection: the
    experiment rig under tests/experiments/local_oracle/ drops a transient
    test_acceptance.py into a per-arm dir at run-time that must never be
    collected. This is the original reason testpaths existed."""
    assert "tests/experiments" not in collected.stdout, (
        "tests/experiments appeared in a bare `pytest` collection - the "
        "experiment tree (with its transient test_acceptance.py artifacts) "
        "must be excluded via --ignore=tests/experiments.\n"
        f"Collected stdout:\n{collected.stdout}"
    )


def test_bare_pytest_excludes_benchmark(collected):
    """tests/benchmark/ must stay excluded from a bare-pytest collection."""
    assert "tests/benchmark" not in collected.stdout, (
        "tests/benchmark appeared in a bare `pytest` collection - it must be "
        "excluded via --ignore=tests/benchmark.\n"
        f"Collected stdout:\n{collected.stdout}"
    )


def test_pyproject_no_stale_testpaths_allowlist():
    """The stale hand-maintained testpaths array must be gone entirely.
    A bare `grep` of pyproject.toml must not find a `testpaths =` key, since
    that is exactly the allowlist that silently rots as new test files are
    added to tests/unit/."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert "testpaths" not in pyproject, (
        "pyproject.toml still contains a `testpaths` entry - the stale "
        "hand-maintained file allowlist must be deleted entirely and "
        "replaced with addopts = \"--ignore=tests/benchmark "
        "--ignore=tests/experiments\"."
    )


def test_pyproject_addopts_ignores_benchmark_and_experiments():
    """pyproject.toml must declare addopts that ignore both the benchmark and
    experiments trees, mirroring the --ignore flags CI/build_detect.py already
    pass explicitly. Both ignores must be present in a single addopts line."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert "addopts" in pyproject, (
        "pyproject.toml's [tool.pytest.ini_options] must declare an `addopts` "
        "key carrying the --ignore flags for benchmark and experiments."
    )
    assert "--ignore=tests/benchmark" in pyproject, (
        "pyproject.toml addopts must include --ignore=tests/benchmark."
    )
    assert "--ignore=tests/experiments" in pyproject, (
        "pyproject.toml addopts must include --ignore=tests/experiments."
    )


def test_pyproject_pythonpath_unchanged():
    """pythonpath = [\".\"] must remain unchanged - it is the pytest>=7 fix
    that lets bare `pytest` (not `python -m pytest`) import app/ and pipeline/
    from the repo root."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'pythonpath = ["."]' in pyproject, (
        "pyproject.toml must still contain `pythonpath = [\".\"]` unchanged."
    )


def test_pyproject_comment_explains_ignore_rationale():
    """The comment above the (now-removed) testpaths block must be updated to
    explain why addopts now mirrors the --ignore flags CI/build_detect.py
    already pass, instead of a hand-maintained file list that silently rots.
    The old 'Pin the testpaths' wording must be gone, and the new rationale
    must reference both the ignore flags and the rot problem."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert "Pin the testpaths" not in pyproject, (
        "The old comment wording 'Pin the testpaths ...' must be removed."
    )
    # The new comment must explain the ignore-based approach and why a
    # hand-maintained file list was abandoned.
    assert "ignore" in pyproject.lower(), (
        "The updated comment must reference the --ignore flags approach."
    )
    assert "rot" in pyproject.lower() or "hand-maintained" in pyproject.lower(), (
        "The updated comment must explain why a hand-maintained file list "
        "was abandoned (it silently rots as new test files are added)."
    )