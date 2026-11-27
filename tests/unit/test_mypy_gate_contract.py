"""Contract for the pinned mypy gate and its recorded baseline.

mypy runs in the CI `lint` job against pipeline/ and app/. Its exit code gates
the job, so the version is pinned exactly, the CI step runs the configured
command, and the `[[tool.mypy.overrides]]` baseline lists only modules that
already failed when the gate was added. PyYAML is not a dependency, so ci.yml
is read as text (see test_python_floor_consistency.py).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
DEV_REQUIREMENTS = REPO_ROOT / "requirements-dev.txt"

_EXACT_MYPY_PIN = re.compile(r"^mypy==\d+(\.\d+)*$")
_JOB_KEY = re.compile(r"^  [A-Za-z_][\w-]*:\s*$")
_RUN_LINE = re.compile(r"^\s+run:\s*(?P<value>.*?)\s*$")


def _mypy_pin_problem(requirements_text: str) -> str | None:
    """Return why the mypy requirement is not an exact pin, or None if it is."""
    lines = [
        raw.split("#", 1)[0].strip()
        for raw in requirements_text.splitlines()
        if raw.split("#", 1)[0].strip().startswith("mypy")
    ]
    if len(lines) != 1:
        return f"requirements-dev.txt must declare mypy exactly once; found {lines!r}"
    if _EXACT_MYPY_PIN.match(lines[0]) is None:
        return f"mypy must be pinned with '==' to an exact version; found {lines[0]!r}"
    return None


def _lint_job_run_commands(ci_text: str) -> list[str]:
    """Return the `run:` values of every step in the `lint` job, in order."""
    lines = ci_text.splitlines()
    start = lines.index("  lint:")
    end = next(
        (i for i in range(start + 1, len(lines)) if _JOB_KEY.match(lines[i])),
        len(lines),
    )
    commands: list[str] = []
    for line in lines[start + 1 : end]:
        match = _RUN_LINE.match(line)
        if match is not None:
            commands.append(match.group("value"))
    return commands


def _check_mypy_table(config: dict) -> None:
    """Raise AssertionError naming the problem if [tool.mypy] is absent or empty."""
    table = config.get("tool", {}).get("mypy")
    if table is None:
        raise AssertionError("pyproject.toml has no [tool.mypy] table")
    if not table.get("files"):
        raise AssertionError("[tool.mypy] files must be a non-empty list")


def _baseline_modules(config: dict) -> list[str]:
    """Return every module named by an `ignore_errors = true` override."""
    overrides = config.get("tool", {}).get("mypy", {}).get("overrides", [])
    modules: list[str] = []
    for override in overrides:
        if override.get("ignore_errors") is True:
            modules.extend(override.get("module", []))
    return modules


def _check_sorted_unique(modules: list[str]) -> None:
    """Raise AssertionError naming the first duplicate or out-of-order entry."""
    seen: set[str] = set()
    for name in modules:
        if name in seen:
            raise AssertionError(f"duplicate baseline entry: {name!r}")
        seen.add(name)
    if modules != sorted(modules):
        raise AssertionError("baseline modules must be listed in sorted order")


def _missing_modules(modules: list[str]) -> list[str]:
    """Return dotted module names that do not resolve to a .py file in the repo."""
    missing = []
    for name in modules:
        base = REPO_ROOT.joinpath(*name.split("."))
        if not (base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()):
            missing.append(name)
    return missing


def _load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


# --- the pin -----------------------------------------------------------------


def test_should_pin_mypy_exactly_in_dev_requirements() -> None:
    text = DEV_REQUIREMENTS.read_text(encoding="utf-8")
    assert _mypy_pin_problem(text) is None


def test_should_reject_a_floor_constraint_on_mypy() -> None:
    assert _mypy_pin_problem("mypy>=2.4.1\n") is not None


def test_should_reject_a_mypy_line_that_is_missing() -> None:
    problem = _mypy_pin_problem("ruff==0.17.5\n")
    assert problem is not None and "exactly once" in problem


# --- the CI step ----------------------------------------------------------------


def test_should_run_mypy_in_the_lint_job() -> None:
    commands = _lint_job_run_commands(CI_YML.read_text(encoding="utf-8"))
    assert "mypy" in commands


def test_should_run_mypy_after_ruff_in_the_lint_job() -> None:
    commands = _lint_job_run_commands(CI_YML.read_text(encoding="utf-8"))
    assert commands.index("mypy") > commands.index("ruff check .")


# --- the configuration and baseline -----------------------------------------------


def test_should_have_a_mypy_table_with_files() -> None:
    _check_mypy_table(_load_pyproject())


def test_should_reject_a_config_without_a_mypy_table() -> None:
    with pytest.raises(AssertionError, match=r"\[tool\.mypy\]"):
        _check_mypy_table({"tool": {"ruff": {}}})


def test_should_reject_an_empty_files_list() -> None:
    with pytest.raises(AssertionError, match="files must be a non-empty list"):
        _check_mypy_table({"tool": {"mypy": {"files": []}}})


def test_should_list_only_existing_modules_in_the_baseline() -> None:
    modules = _baseline_modules(_load_pyproject())
    assert _missing_modules(modules) == []


def test_should_have_an_empty_baseline() -> None:
    assert _baseline_modules(_load_pyproject()) == []


def test_should_keep_the_baseline_sorted_and_unique() -> None:
    _check_sorted_unique(_baseline_modules(_load_pyproject()))


def test_should_reject_a_duplicate_baseline_entry() -> None:
    with pytest.raises(AssertionError, match=r"duplicate baseline entry: 'pipeline\.ci'"):
        _check_sorted_unique(["pipeline.ci", "pipeline.ci"])


def test_should_reject_an_unsorted_baseline() -> None:
    with pytest.raises(AssertionError, match="sorted order"):
        _check_sorted_unique(["pipeline.b", "pipeline.a"])


def test_should_report_a_stale_baseline_entry() -> None:
    assert _missing_modules(["pipeline.no_such_module"]) == ["pipeline.no_such_module"]
