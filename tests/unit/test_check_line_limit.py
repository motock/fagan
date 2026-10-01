"""Tests for scripts/check_line_limit.py, the two-tier 1000-line CI gate.

The gate is deliberately two-tier: production Python (repo-root *.py,
pipeline/**, app/**, scripts/**) is BLOCKING, everything else (tests, docs,
.css, .mjs) is WARN-ONLY. A fail-closed-everywhere gate would be red the day
it landed (nine of the fifteen over-limit text files are docs/CSS/.mjs/tests)
and would block legitimate doc growth.

These tests drive the real script through its CLI against tmp_path trees, and
read the real allowlists out of the script module rather than hardcoding them.
They deliberately assert membership and behaviour only -- never the allowlist's
total size, full-set equality, entry count, or a file hash. The blocking
allowlist is a registry that six follow-up split stories each remove one entry
from; a count assertion here would make every one of them unsatisfiable.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_line_limit.py"


def _load_checker():
    """Load scripts/check_line_limit.py as a module so tests read the real
    allowlists instead of a copy that can drift."""
    spec = importlib.util.spec_from_file_location("check_line_limit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load_checker()


def _write_lines(path, n):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n" * n, encoding="utf-8")


def _make_clean_tree(root, allowlist):
    """Materialise every allowlisted path at exactly its recorded ceiling, so
    the tree is clean apart from whatever the test adds."""
    for rel, ceiling in allowlist.items():
        _write_lines(root / rel, ceiling)


def _run(root, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def test_blocking_allowlist_entries_are_production_paths_over_the_limit(checker):
    # Each split story retires its own entry, so assert the registry's shape
    # (production prefix, recorded count above the limit), never a specific
    # entry or the total.
    for path, recorded in checker._BLOCKING_ALLOWLIST.items():
        assert path.startswith(checker.BLOCKING_PREFIXES) or "/" not in path, path
        assert recorded > checker.DEFAULT_LIMIT, path


def test_production_file_at_limit_passes(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / "pipeline" / "at_limit.py", 1000)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_production_file_one_over_limit_fails(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / "pipeline" / "over_limit.py", 1001)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "pipeline/over_limit.py" in result.stdout


def test_allowlisted_at_ceiling_passes(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_allowlisted_over_ceiling_fails(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    if not checker._BLOCKING_ALLOWLIST:
        pytest.skip("blocking allowlist is empty (all splits landed)")
    rel = next(iter(checker._BLOCKING_ALLOWLIST))
    ceiling = checker._BLOCKING_ALLOWLIST[rel]
    _write_lines(tmp_path / rel, ceiling + 1)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert rel in result.stdout


def test_allowlisted_missing_file_fails(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    if not checker._BLOCKING_ALLOWLIST:
        pytest.skip("blocking allowlist is empty (all splits landed)")
    rel = next(iter(checker._BLOCKING_ALLOWLIST))
    (tmp_path / rel).unlink()
    result = _run(tmp_path)
    assert result.returncode == 1
    assert rel in result.stdout
    assert "remove" in result.stdout.lower()


def test_allowlisted_under_limit_fails(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    if not checker._BLOCKING_ALLOWLIST:
        pytest.skip("blocking allowlist is empty (all splits landed)")
    rel = next(iter(checker._BLOCKING_ALLOWLIST))
    _write_lines(tmp_path / rel, 500)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert rel in result.stdout
    assert "remove" in result.stdout.lower()


def test_markdown_over_limit_is_warn_only(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / "docs" / "big.md", 1500)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docs/big.md" in result.stdout
    assert "warn-only" in result.stdout


def test_css_and_mjs_over_limit_are_warn_only(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / "static" / "big.css", 1500)
    _write_lines(tmp_path / "static" / "big.mjs", 1500)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "static/big.css" in result.stdout
    assert "static/big.mjs" in result.stdout


def test_test_file_over_limit_is_warn_only(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / "tests" / "unit" / "test_big.py", 1500)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/unit/test_big.py" in result.stdout


def test_repo_root_production_py_over_limit_fails(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / "root_script.py", 1001)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "root_script.py" in result.stdout


def test_non_text_extension_ignored(checker, tmp_path):
    # docs/screenshots/demo.gif really does have >1000 "lines"; an extension
    # allowlist excludes it structurally, a binary denylist would not.
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / "docs" / "screenshots" / "demo.gif", 1500)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "demo.gif" not in result.stdout


def test_excluded_directory_ignored(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / ".venv" / "lib" / "big.py", 1500)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ".venv" not in result.stdout


def test_limit_flag(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / "pipeline" / "small.py", 5)
    result = _run(tmp_path, "--limit", "5")
    assert result.returncode == 0, result.stdout + result.stderr
    _write_lines(tmp_path / "pipeline" / "small.py", 6)
    result = _run(tmp_path, "--limit", "5")
    assert result.returncode == 1
    assert "pipeline/small.py" in result.stdout


def test_clean_tree_prints_summary(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "warn-only" in result.stdout


def test_real_repo_tree_is_green():
    # Day-one-green assertion: a real run against the real repo, not a fixture.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(REPO_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
