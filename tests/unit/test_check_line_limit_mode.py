"""Tests for the --mode warn|block option of scripts/check_line_limit.py.

``block`` (the default) keeps the gate's existing behaviour. ``warn`` prints
every would-be blocking violation with a ``warn:`` prefix and always exits 0,
so the production-file limit can run advisory in CI without being removed.

Like tests/unit/test_check_line_limit.py, these drive the real script against
tmp_path trees and load the module from its file so they read the real
allowlists.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_line_limit.py"


def _load_checker():
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
    for rel, ceiling in allowlist.items():
        _write_lines(root / rel, ceiling)


def _run(root, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def test_block_mode_over_limit_production_file_exits_1(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / "pipeline" / "over_limit.py", 1001)
    result = _run(tmp_path, "--mode", "block")
    assert result.returncode == 1
    assert "pipeline/over_limit.py" in result.stdout


def test_warn_mode_over_limit_production_file_exits_0_with_warn_prefix(
    checker, tmp_path
):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / "pipeline" / "over_limit.py", 1001)
    result = _run(tmp_path, "--mode", "warn")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "warn:" in result.stdout
    assert "pipeline/over_limit.py" in result.stdout


def test_warn_mode_stale_allowlist_entry_exits_0(checker, tmp_path, monkeypatch):
    # The real blocking allowlist is empty, so inject one stale entry through
    # the module under test and drive main() in-process.
    monkeypatch.setitem(checker._BLOCKING_ALLOWLIST, "pipeline/retired.py", 1200)
    _write_lines(tmp_path / "pipeline" / "retired.py", 10)
    code = checker.main(["--mode", "warn", "--root", str(tmp_path)])
    assert code == 0


def test_warn_mode_clean_tree_exits_0(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    result = _run(tmp_path, "--mode", "warn")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK (warn mode)" in result.stdout


def test_warn_mode_summary_counts_over_limit_files(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / "pipeline" / "over_limit.py", 1001)
    result = _run(tmp_path, "--mode", "warn")
    assert "OK (warn mode): 1 file(s) over limit" in result.stdout


def test_default_mode_matches_explicit_block(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    _write_lines(tmp_path / "pipeline" / "over_limit.py", 1001)
    default = _run(tmp_path)
    explicit = _run(tmp_path, "--mode", "block")
    assert default.returncode == explicit.returncode == 1
    assert default.stdout == explicit.stdout


def test_unknown_mode_is_rejected_by_argparse(checker):
    with pytest.raises(SystemExit) as excinfo:
        checker.main(["--mode", "bogus"])
    assert excinfo.value.code == 2


def test_main_warn_mode_returns_0_on_clean_tree(checker, tmp_path):
    _make_clean_tree(tmp_path, checker._BLOCKING_ALLOWLIST)
    assert checker.main(["--mode", "warn", "--root", str(tmp_path)]) == 0


def test_ci_line_limit_step_runs_in_warn_mode():
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "run: python scripts/check_line_limit.py --mode warn" in workflow


def test_ci_line_limit_step_has_no_bare_block_invocation():
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    bare = re.search(
        r"run: python scripts/check_line_limit\.py[ \t]*$",
        workflow,
        re.MULTILINE,
    )
    assert bare is None
