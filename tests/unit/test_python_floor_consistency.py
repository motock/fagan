"""The Python floor must agree across ruff, the CI matrix and the installer.

Asserts invariants between the three sources, never a literal version, so the
floor can move without editing this file as long as all three move together.
PyYAML is not a dependency, so ci.yml is read as text.
"""

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
INSTALL_CHECKS = REPO_ROOT / "scripts" / "install_checks.py"

_MATRIX_LINE = re.compile(r"^\s*python-version:\s*\[(?P<items>[^\]]*)\]\s*$", re.MULTILINE)
_QUOTED_VERSION = re.compile(r"""["'](\d+)\.(\d+)["']""")


def matrix_versions(ci_text: str) -> list[tuple[int, int]]:
    match = _MATRIX_LINE.search(ci_text)
    if match is None:
        raise ValueError("no `python-version: [...]` list found in ci.yml")
    versions = [(int(a), int(b)) for a, b in _QUOTED_VERSION.findall(match.group("items"))]
    if not versions:
        raise ValueError("python-version list in ci.yml is empty")
    return versions


def matrix_minimum(ci_text: str) -> tuple[int, int]:
    return min(matrix_versions(ci_text))


def macos_excluded_versions(ci_text: str) -> list[tuple[int, int]]:
    pairs = re.findall(
        r"-\s*os:\s*macos-latest\s*\n\s*python-version:\s*[\"'](\d+)\.(\d+)[\"']", ci_text
    )
    return [(int(a), int(b)) for a, b in pairs]


def ruff_target() -> tuple[int, int]:
    raw = tomllib.loads(PYPROJECT.read_text())["tool"]["ruff"]["target-version"]
    match = re.fullmatch(r"py(\d)(\d+)", raw)
    assert match, f"unparseable ruff target-version: {raw!r}"
    return int(match.group(1)), int(match.group(2))


def installer_minimum() -> tuple[int, int]:
    match = re.search(r"^_PY_MIN\s*=\s*\((\d+),\s*(\d+)\)", INSTALL_CHECKS.read_text(), re.MULTILINE)
    assert match, "_PY_MIN not found in scripts/install_checks.py"
    return int(match.group(1)), int(match.group(2))


def test_should_match_ruff_target_to_ci_matrix_minimum():
    assert ruff_target() == matrix_minimum(CI_YML.read_text())


def test_should_match_ci_matrix_minimum_to_installer_floor():
    assert matrix_minimum(CI_YML.read_text()) == installer_minimum()


def test_should_leave_exactly_one_macos_version_unexcluded():
    text = CI_YML.read_text()
    remaining = set(matrix_versions(text)) - set(macos_excluded_versions(text))
    assert len(remaining) == 1


def test_should_raise_when_matrix_line_is_absent():
    with pytest.raises(ValueError, match="no `python-version"):
        matrix_minimum("jobs:\n  test:\n    runs-on: ubuntu-latest\n")


def test_should_raise_when_matrix_list_is_empty():
    with pytest.raises(ValueError, match="empty"):
        matrix_minimum("        python-version: []\n")


def test_should_pick_numeric_minimum_not_lexical():
    assert matrix_minimum('        python-version: ["3.9", "3.10"]\n') == (3, 9)
