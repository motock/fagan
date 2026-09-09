"""Tests for the exact pytest pin in requirements-dev.txt.

pytest's exit code gates every merge (the CI test job and the pipeline's own
test gate), and .claude/rules/code-review.md requires pinning the exact
version of any tool whose exit code gates a merge: a floor constraint (>=)
lets an upstream release silently change what "clean" means mid-flight.
requirements-dev.txt already follows that rule for ruff and pytest-xdist;
these tests hold pytest to the same standard and guard the survivor lines
(both -r includes, the leading comment block, and the existing exact pins).

The pinned literal is compared against the version of the *running* pytest
(``pytest.__version__``), so the pin can only satisfy these tests if it names
a version that is actually installed and passing today - not an aspirational
one.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEV_REQUIREMENTS = _REPO_ROOT / "requirements-dev.txt"
_RUNTIME_REQUIREMENTS = _REPO_ROOT / "requirements.txt"

# Tools whose exit code gates a merge: pytest (CI test job + pipeline test
# gate), pytest-xdist and ruff (lint gate). None of them may carry a range
# constraint in requirements-dev.txt.
_MERGE_GATED_TOOLS = ("pytest", "pytest-xdist", "ruff")

# The exact-pin shape this story requires: pytest==<major>.<minor>[.<patch>].
_EXACT_PYTEST_PIN_RE = re.compile(r"^pytest==(\d+\.\d+(?:\.\d+)?)", re.MULTILINE)
# The floor constraint these tests exist to eliminate.
_PYTEST_FLOOR_RE = re.compile(r"^pytest>=", re.MULTILINE)

# Survivor-list pins that must not be touched by this story.
_SURVIVOR_PINS = {"ruff": "0.16.5", "pytest-xdist": "3.8.0"}


def _read_requirements(path: Path) -> str:
    if not path.exists():
        raise AssertionError(f"expected {path.name} to exist at {_REPO_ROOT}")
    return path.read_text(encoding="utf-8")


def _requirement_lines(text: str) -> list[str]:
    """Return requirement lines only: blanks, comments and -r includes dropped."""
    return [
        stripped
        for raw in text.splitlines()
        if (stripped := raw.strip())
        and not stripped.startswith("#")
        and not stripped.startswith("-r")
    ]


def _split_name_constraint(line: str) -> tuple[str, str]:
    """Split 'pytest==9.1.1  # why' into ('pytest', '==9.1.1').

    The trailing comment is stripped before parsing so a '>' inside comment
    prose can never be mistaken for a range constraint.
    """
    requirement = line.split("#", 1)[0].strip()
    match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$", requirement)
    if match is None:
        return "", ""
    return match.group(1), match.group(2).strip()


def _lines_for_tool(text: str, tool: str) -> list[str]:
    """Return requirement lines whose package name is exactly ``tool``.

    Exact-name matching keeps 'pytest-xdist' lines out of the 'pytest' result.
    """
    return [
        line
        for line in _requirement_lines(text)
        if _split_name_constraint(line)[0] == tool
    ]


def test_pytest_is_pinned_exactly() -> None:
    """A ``pytest==X.Y[.Z]`` line exists: the exact pin, not a floor."""
    text = _read_requirements(_DEV_REQUIREMENTS)
    pytest_lines = _lines_for_tool(text, "pytest")
    assert len(pytest_lines) == 1, (
        "requirements-dev.txt must declare pytest exactly once; found "
        f"{pytest_lines!r}"
    )
    match = _EXACT_PYTEST_PIN_RE.search(text)
    assert match is not None, (
        "requirements-dev.txt must contain a line matching "
        "^pytest==\\d+\\.\\d+(\\.\\d+)? (exact pin, not a floor); the pytest "
        f"requirement line found was {pytest_lines!r}"
    )


def test_pytest_pin_is_well_formed() -> None:
    """The pin line is exactly 'pytest==<version>' before its comment.

    Boundary case: catches malformed pins such as 'pytest==9.1.1.1' (four
    components) or stray tokens between the version and the comment, which
    the search-based regex alone would silently accept as a prefix match.
    """
    text = _read_requirements(_DEV_REQUIREMENTS)
    pin_lines = [ln for ln in text.splitlines() if ln.strip().startswith("pytest==")]
    assert len(pin_lines) == 1, (
        f"expected exactly one 'pytest==' line; found {pin_lines!r}"
    )
    requirement = pin_lines[0].split("#", 1)[0].strip()
    assert re.fullmatch(r"pytest==\d+\.\d+(\.\d+)?", requirement) is not None, (
        f"the pytest pin must be exactly 'pytest==<major>.<minor>[.<patch>]'; "
        f"found {requirement!r}"
    )


def test_pytest_floor_constraint_is_gone() -> None:
    """No ``pytest>=`` line may survive in requirements-dev.txt."""
    text = _read_requirements(_DEV_REQUIREMENTS)
    match = _PYTEST_FLOOR_RE.search(text)
    assert match is None, (
        "pytest must not use a floor constraint (>=) - its exit code gates "
        f"merges; offending text: {match.group(0)!r}"
    )


def test_ruff_and_pytest_xdist_remain_exactly_pinned() -> None:
    """Survivor-list regression guard: ruff and pytest-xdist keep == pins."""
    text = _read_requirements(_DEV_REQUIREMENTS)
    for tool, version in _SURVIVOR_PINS.items():
        lines = _lines_for_tool(text, tool)
        assert lines == [f"{tool}=={version}"], (
            f"the {tool} survivor line must remain exactly "
            f"'{tool}=={version}' (unmodified, exact pin); found {lines!r}"
        )


def test_recursive_includes_survive() -> None:
    """Survivor-list regression guard: both -r include lines remain."""
    text = _read_requirements(_DEV_REQUIREMENTS)
    stripped_lines = [ln.strip() for ln in text.splitlines()]
    for include in ("-r requirements.txt", "-r requirements-dashboard.txt"):
        assert include in stripped_lines, (
            f"requirements-dev.txt must keep the {include!r} include line; "
            f"lines found: {stripped_lines!r}"
        )


def test_leading_comment_block_survives() -> None:
    """Survivor-list regression guard: the leading comment block remains."""
    text = _read_requirements(_DEV_REQUIREMENTS)
    lines = text.splitlines()
    assert lines, "requirements-dev.txt must not be empty"
    first_line = lines[0].strip()
    assert first_line.startswith("#"), (
        "requirements-dev.txt must keep its leading comment block; first line "
        f"is {first_line!r}"
    )
    assert "Development dependencies" in first_line, (
        "the leading comment block must be left unmodified; first line is "
        f"{first_line!r}"
    )


def test_pinned_version_matches_running_pytest() -> None:
    """The pinned literal equals the version of the running pytest."""
    text = _read_requirements(_DEV_REQUIREMENTS)
    match = _EXACT_PYTEST_PIN_RE.search(text)
    assert match is not None, (
        "no exact pytest pin found; cannot compare against the running pytest"
    )
    pinned = match.group(1)
    running = pytest.__version__
    assert pinned == running, (
        f"requirements-dev.txt pins pytest=={pinned} but the running pytest is "
        f"{running}; the pin must name the version actually installed and "
        "passing today, not an aspirational one"
    )


def test_requirements_txt_declares_no_pytest() -> None:
    """pytest stays a dev-only dependency: no pytest line in requirements.txt."""
    text = _read_requirements(_RUNTIME_REQUIREMENTS)
    offenders = [
        stripped
        for raw in text.splitlines()
        if (stripped := raw.strip()) and stripped.startswith("pytest")
    ]
    assert not offenders, (
        "requirements.txt must not declare pytest (it is a dev dependency "
        f"owned by requirements-dev.txt); offending lines: {offenders!r}"
    )


@pytest.mark.parametrize("tool", _MERGE_GATED_TOOLS)
def test_no_range_constraint_on_merge_gated_tools(tool: str) -> None:
    """No ``>`` / ``>=`` (or other non-exact) constraint on a gated tool."""
    text = _read_requirements(_DEV_REQUIREMENTS)
    lines = _lines_for_tool(text, tool)
    assert lines, f"requirements-dev.txt must still declare {tool}"
    for line in lines:
        constraint = _split_name_constraint(line)[1]
        assert ">" not in constraint, (
            f"{tool} must not use a range constraint ('>' or '>=') - its exit "
            f"code gates merges; found {line!r}"
        )
        assert constraint.startswith("=="), (
            f"{tool} must be pinned exactly with '=='; found {line!r}"
        )


def test_pytest_pin_line_explains_why_it_is_exact() -> None:
    """The pytest pin carries a trailing comment stating why it is exact."""
    text = _read_requirements(_DEV_REQUIREMENTS)
    pin_lines = [ln for ln in text.splitlines() if ln.strip().startswith("pytest==")]
    assert len(pin_lines) == 1, (
        f"expected exactly one 'pytest==' line; found {pin_lines!r}"
    )
    line = pin_lines[0]
    assert "#" in line, (
        "the pytest pin must carry a trailing comment explaining why it is "
        f"pinned exactly (mcp-pin style); found {line!r}"
    )
    comment = line.split("#", 1)[1].lower().replace("-", " ")
    assert "exit code" in comment, (
        "the pytest pin comment must state why the pin is exact (its exit "
        "code gates merges), matching the mcp pin style in requirements.txt; "
        f"found comment {comment!r}"
    )