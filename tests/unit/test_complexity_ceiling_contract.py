"""Contract for the C901 function-complexity ceiling in the ruff lint gate.

The ceiling measures the function itself, so a large function cannot be
hidden by moving it into its own file. Existing offenders are exempted by
file in pyproject.toml; these tests keep that exemption list honest.
"""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

SPLIT_MODULES = [
    "pipeline/story_status.py",
    "pipeline/advance.py",
    "pipeline/dispatch.py",
    "pipeline/review_orchestrator.py",
]


def _ruff_config() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["tool"]["ruff"]


def _c901_exemptions() -> list[str]:
    per_file = _ruff_config()["lint"].get("per-file-ignores", {})
    return [path for path, rules in per_file.items() if "C901" in rules]


def test_should_enable_c901_with_a_ceiling_of_30():
    lint = _ruff_config()["lint"]

    assert "C901" in lint["extend-select"]
    assert lint["mccabe"]["max-complexity"] == 30
    assert "select" not in lint


def test_should_list_only_existing_files_in_the_exemptions():
    stale = []
    for pattern in _c901_exemptions():
        if not any(REPO_ROOT.glob(pattern)):
            stale.append(pattern)

    assert stale == []


def test_should_not_exempt_the_four_split_modules():
    exempted = set(_c901_exemptions())

    assert exempted.isdisjoint(SPLIT_MODULES)
