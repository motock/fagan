"""Tests for pipeline.build_detect._added_pytest_test_paths (Mode 42 fix).

detect_test_command's gated pytest run excludes tests/ via --ignore=tests
(see _apply_pytest_collection_overrides) so the benchmark harness's own
non-graded fixtures don't run in every story's gate. But when a story's
actual deliverable lives under tests/ (e.g. tests/benchmark/*.py), its own
new tests/test_*.py file is excluded by that same flag - both the model and
the local done-bar become blind to it (REAL-REPO-HARNESS-TASK-AND-DRIVER: a
broken run_groundtruth_in_place delegation shipped tests_passed because the
model's own test suite for it never ran against the gate). This function
finds those paths so callers can pass them explicitly (pytest's --ignore
only filters paths it discovers on its own, not ones given as positional
args - see the module docstring for the empirical proof).
"""
import subprocess
from pathlib import Path

from pipeline.build_detect import _added_pytest_test_paths


def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _init_base_repo(tmp: Path) -> None:
    _run(["git", "init", "-b", "main"], tmp)
    (tmp / "README.md").write_text("base\n")
    _run(["git", "add", "."], tmp)
    _run(["git", "commit", "-m", "initial"], tmp)


def _checkout_agent_branch(tmp: Path, story_key: str) -> None:
    _run(["git", "checkout", "-b", f"agent/{story_key.lower()}"], tmp)


def test_new_test_file_under_tests_dir_is_detected(tmp_path):
    _init_base_repo(tmp_path)
    _checkout_agent_branch(tmp_path, "S1")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_new_thing.py").write_text(
        "def test_x():\n    assert True\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "add test"], tmp_path)

    paths = _added_pytest_test_paths(tmp_path, "S1", "main")

    assert paths == ["tests/test_new_thing.py"]


def test_nested_test_file_under_tests_dir_is_detected(tmp_path):
    _init_base_repo(tmp_path)
    _checkout_agent_branch(tmp_path, "S1")
    (tmp_path / "tests" / "benchmark").mkdir(parents=True)
    (tmp_path / "tests" / "benchmark" / "test_driver.py").write_text(
        "def test_y():\n    assert True\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "add nested test"], tmp_path)

    paths = _added_pytest_test_paths(tmp_path, "S1", "main")

    assert paths == ["tests/benchmark/test_driver.py"]


def test_trailing_underscore_test_suffix_is_also_recognized(tmp_path):
    _init_base_repo(tmp_path)
    _checkout_agent_branch(tmp_path, "S1")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "driver_test.py").write_text(
        "def test_z():\n    assert True\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "add test"], tmp_path)

    paths = _added_pytest_test_paths(tmp_path, "S1", "main")

    assert paths == ["tests/driver_test.py"]


def test_non_test_file_under_tests_dir_is_not_included(tmp_path):
    _init_base_repo(tmp_path)
    _checkout_agent_branch(tmp_path, "S1")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "helpers.py").write_text("def helper(): pass\n")
    (tmp_path / "tests" / "fixture_data.json").write_text("{}")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "add non-test files"], tmp_path)

    paths = _added_pytest_test_paths(tmp_path, "S1", "main")

    assert paths == []


def test_root_level_test_file_is_not_included(tmp_path):
    """Root test_*.py is already collected by testpaths=. regardless of
    --ignore=tests - only files under tests/ need this workaround."""
    _init_base_repo(tmp_path)
    _checkout_agent_branch(tmp_path, "S1")
    (tmp_path / "test_root_thing.py").write_text(
        "def test_a():\n    assert True\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "add root test"], tmp_path)

    paths = _added_pytest_test_paths(tmp_path, "S1", "main")

    assert paths == []


def test_modified_existing_test_file_under_tests_dir_is_detected(tmp_path):
    _init_base_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_existing.py").write_text(
        "def test_a():\n    assert True\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "add base test"], tmp_path)

    _checkout_agent_branch(tmp_path, "S1")
    (tmp_path / "tests" / "test_existing.py").write_text(
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "extend test"], tmp_path)

    paths = _added_pytest_test_paths(tmp_path, "S1", "main")

    assert paths == ["tests/test_existing.py"]


def test_deleted_test_file_is_not_included(tmp_path):
    _init_base_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_gone.py").write_text(
        "def test_a():\n    assert True\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "add base test"], tmp_path)

    _checkout_agent_branch(tmp_path, "S1")
    (tmp_path / "tests" / "test_gone.py").unlink()
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "remove test"], tmp_path)

    paths = _added_pytest_test_paths(tmp_path, "S1", "main")

    assert paths == []


def test_no_diff_returns_empty_list(tmp_path):
    _init_base_repo(tmp_path)
    _checkout_agent_branch(tmp_path, "S1")

    paths = _added_pytest_test_paths(tmp_path, "S1", "main")

    assert paths == []


def test_nonexistent_worktree_returns_empty_list_not_raise(tmp_path):
    missing = tmp_path / "does-not-exist"

    paths = _added_pytest_test_paths(missing, "S1", "main")

    assert paths == []


def test_story_key_lowercased_for_branch_lookup(tmp_path):
    _init_base_repo(tmp_path)
    _checkout_agent_branch(tmp_path, "REAL-REPO-HARNESS-TASK-AND-DRIVER")
    (tmp_path / "tests" / "benchmark").mkdir(parents=True)
    (tmp_path / "tests" / "benchmark" / "test_run_real_repo_task.py").write_text(
        "def test_it():\n    assert True\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "add test"], tmp_path)

    paths = _added_pytest_test_paths(
        tmp_path, "REAL-REPO-HARNESS-TASK-AND-DRIVER", "main")

    assert paths == ["tests/benchmark/test_run_real_repo_task.py"]
