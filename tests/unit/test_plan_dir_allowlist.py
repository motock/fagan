"""Guard ADR 0012's shrink-only allowlist of modules still touching PLAN_DIR.

Scans only ``.py`` files under pipeline/, app/ and scripts/; ``pipeline/store.py``,
``pipeline/persistence.py`` and ``pipeline/paths.py`` are the file store's own
implementation and legitimately stay on the allowlist after the migration.
Each entry is a ceiling, not an exact pin, so a file that drops references
passes without editing the JSON, while a new user or a file that grows past its
ceiling fails.
"""

import json
from pathlib import Path

import pytest

from tests.unit._plan_dir_references import count_references

_SCANNED_ROOTS = ("pipeline", "app", "scripts")
_BASELINE_PATH = Path(__file__).with_name("plan_dir_allowlist.json")


def _measure(root: Path) -> dict[str, int]:
    found: dict[str, int] = {}
    for name in _SCANNED_ROOTS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            count = count_references(path.read_text(encoding="utf-8"), rel)
            if count:
                found[rel] = count
    return found


def _problems(found: dict[str, int], baseline: dict[str, int]) -> list[str]:
    problems: list[str] = []
    for rel, count in sorted(found.items()):
        ceiling = baseline.get(rel)
        if ceiling is None:
            problems.append(f"new direct PLAN_DIR user: {rel}")
        elif count > ceiling:
            problems.append(f"grew past its ceiling: {rel} ({count} > {ceiling})")
    for rel in sorted(baseline):
        if found.get(rel, 0) == 0:
            problems.append(f"stale entry, remove it: {rel}")
    return problems


def _tree_with_references(root: Path, text: str) -> Path:
    pipeline = root / "pipeline"
    pipeline.mkdir(exist_ok=True)
    (pipeline / "a.py").write_text(text, encoding="utf-8")
    return pipeline


def test_should_pass_when_every_reference_is_baselined(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIR = 1\nx = PLAN_DIR\n")
    assert _problems(_measure(tmp_path), {"pipeline/a.py": 5}) == []


def test_should_fail_on_a_new_file_that_references_plan_dir(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIR = 1\n")
    assert _problems(_measure(tmp_path), {}) == [
        "new direct PLAN_DIR user: pipeline/a.py"
    ]


def test_should_fail_when_a_baselined_file_grows_past_its_ceiling(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIR = 1\n" * 6)
    assert _problems(_measure(tmp_path), {"pipeline/a.py": 5}) == [
        "grew past its ceiling: pipeline/a.py (6 > 5)"
    ]


def test_should_pass_when_a_baselined_file_shrinks_but_still_references_plan_dir(
    tmp_path,
):
    _tree_with_references(tmp_path, "PLAN_DIR = 1\nx = PLAN_DIR\n")
    assert _problems(_measure(tmp_path), {"pipeline/a.py": 5}) == []


def test_should_fail_on_a_baseline_entry_whose_file_has_no_reference_left(tmp_path):
    _tree_with_references(tmp_path, "x = 1\n")
    assert _problems(_measure(tmp_path), {"pipeline/a.py": 5}) == [
        "stale entry, remove it: pipeline/a.py"
    ]


def test_should_fail_on_a_baseline_entry_whose_file_is_missing(tmp_path):
    assert _problems(_measure(tmp_path), {"pipeline/gone.py": 2}) == [
        "stale entry, remove it: pipeline/gone.py"
    ]


def test_should_ignore_non_python_files(tmp_path):
    pipeline = _tree_with_references(tmp_path, "PLAN_DIR = 1\n")
    (pipeline / "a.sh").write_text("PLAN_DIR=/tmp/x\n", encoding="utf-8")
    (pipeline / "a.md").write_text("PLAN_DIR is documented here\n", encoding="utf-8")
    assert set(_measure(tmp_path)) == {"pipeline/a.py"}


def test_should_ignore_directories_outside_the_scanned_roots(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIR = 1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.py").write_text("PLAN_DIR = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "x.py").write_text("PLAN_DIR = 1\n", encoding="utf-8")
    assert set(_measure(tmp_path)) == {"pipeline/a.py"}


def test_should_match_word_boundaries_only(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIRECTORY = 1\nMY_PLAN_DIR = 2\n")
    assert _measure(tmp_path) == {}


def test_should_count_each_reference_node_not_just_lines(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIR = 1 + PLAN_DIR\n")
    assert _measure(tmp_path) == {"pipeline/a.py": 2}


def test_should_not_count_a_plan_dir_mention_in_a_comment(tmp_path):
    _tree_with_references(tmp_path, "# PLAN_DIR is documented here\nx = 1\n")
    assert _measure(tmp_path) == {}


def test_should_not_count_a_plan_dir_mention_in_a_docstring(tmp_path):
    _tree_with_references(tmp_path, '"""Uses PLAN_DIR internally."""\nx = 1\n')
    assert _measure(tmp_path) == {}


def test_should_not_count_the_plan_dir_environment_variable_name(tmp_path):
    _tree_with_references(tmp_path, "import os\nos.environ['PLAN_DIR']\n")
    assert _measure(tmp_path) == {}


def test_should_count_a_bare_name_reference(tmp_path):
    _tree_with_references(tmp_path, "x = PLAN_DIR\n")
    assert _measure(tmp_path) == {"pipeline/a.py": 1}


def test_should_count_an_attribute_reference(tmp_path):
    _tree_with_references(tmp_path, "from pipeline import paths\nx = paths.PLAN_DIR\n")
    assert _measure(tmp_path) == {"pipeline/a.py": 1}


def test_should_count_a_parameter_named_plan_dir(tmp_path):
    _tree_with_references(tmp_path, "def f(plan_dir):\n    return plan_dir\n")
    assert _measure(tmp_path) == {"pipeline/a.py": 2}


def test_should_count_a_keyword_argument_named_plan_dir(tmp_path):
    _tree_with_references(tmp_path, "f(plan_dir=1)\n")
    assert _measure(tmp_path) == {"pipeline/a.py": 1}


def test_should_fail_loudly_on_a_syntax_error_naming_the_file(tmp_path):
    _tree_with_references(tmp_path, "def broken(:\n")
    with pytest.raises(ValueError, match="pipeline/a.py"):
        _measure(tmp_path)


def test_should_match_the_committed_baseline_for_the_real_tree():
    repo_root = Path(__file__).resolve().parents[2]
    baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    problems = _problems(_measure(repo_root), baseline)
    assert problems == [], "\n".join(problems)
