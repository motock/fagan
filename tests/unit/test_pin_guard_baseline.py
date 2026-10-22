"""Enforce the frozen-pin guard over tests/ with a shrink-only baseline.

Scans only ``.py`` files under tests/; ``.mjs`` pins are out of scope.
``pin_guard_baseline.json`` lists the pins that already existed; a new pin
fails, and so does a baseline entry whose pin is gone (ceilings, not
exemptions — mirrors scripts/check_line_limit.py).
"""

import json
from pathlib import Path

from pipeline.pin_guard import scan_source

_TESTS_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _TESTS_DIR.parent
_BASELINE_PATH = Path(__file__).with_name("pin_guard_baseline.json")
_SHA = "a" * 64


def _measure(root: Path, tests_dir: Path) -> dict[str, dict[int, str]]:
    found: dict[str, dict[int, str]] = {}
    for path in sorted(tests_dir.rglob("*.py")):
        violations = scan_source(path.read_text(encoding="utf-8"))
        if violations:
            rel = path.relative_to(root).as_posix()
            found[rel] = {v.line: v.rule for v in violations}
    return found


def _problems(
    found: dict[str, dict[int, str]], baseline: dict[str, list[int]]
) -> list[str]:
    problems = []
    for rel, lines in found.items():
        for line, rule in lines.items():
            if line not in baseline.get(rel, []):
                problems.append(f"new pin: {rel}:{line} ({rule})")
    for rel, lines in baseline.items():
        for line in lines:
            if line not in found.get(rel, {}):
                problems.append(f"stale entry — remove it: {rel}:{line}")
    return problems


def _tree_with_pin(tmp_path: Path) -> Path:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text(f'DIGEST = "{_SHA}"\n', encoding="utf-8")
    return tests


def test_should_pass_when_every_pin_is_baselined(tmp_path):
    tests = _tree_with_pin(tmp_path)
    found = _measure(tmp_path, tests)
    assert _problems(found, {"tests/test_x.py": [1]}) == []


def test_should_fail_on_pin_missing_from_baseline(tmp_path):
    tests = _tree_with_pin(tmp_path)
    found = _measure(tmp_path, tests)
    assert _problems(found, {}) == ["new pin: tests/test_x.py:1 (sha256_literal)"]


def test_should_fail_on_stale_baseline_entry(tmp_path):
    tests = _tree_with_pin(tmp_path)
    (tests / "test_x.py").write_text("DIGEST = 1\n", encoding="utf-8")
    found = _measure(tmp_path, tests)
    assert _problems(found, {"tests/test_x.py": [1]}) == [
        "stale entry — remove it: tests/test_x.py:1"
    ]


def test_should_ignore_non_python_files(tmp_path):
    tests = _tree_with_pin(tmp_path)
    (tests / "test_y.mjs").write_text(f'const d = "{_SHA}";\n', encoding="utf-8")
    assert set(_measure(tmp_path, tests)) == {"tests/test_x.py"}


def test_should_match_committed_baseline_for_real_tests():
    baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    problems = _problems(_measure(_REPO_ROOT, _TESTS_DIR), baseline)
    assert problems == [], "\n".join(problems)
