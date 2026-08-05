"""Acceptance test for the off-task-path detection helpers (Mode 31 guard,
part 1 of 2). Pure-function tests only -- wiring these into main()'s
mutating-tool-call loop is a separate follow-up story. The oracle grades
the run on whether the impl makes these pass.
"""
import importlib.util
import os
from pathlib import Path

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_spec = importlib.util.spec_from_file_location(
    "local_agent", str(Path(__file__).parent.parent.parent / "scripts" / "local_agent.py")
)
la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(la)


def test_expected_task_paths_extracts_backtick_quoted_paths():
    task = "Edit `pipeline/server.py` and `app/dashboard.py` per the brief."
    assert la._expected_task_paths(task) == {"pipeline/server.py", "app/dashboard.py"}


def test_expected_task_paths_extracts_bold_markdown_paths():
    task = "1. **pipeline/server.py** (~line 1336)\n2. **app/dashboard.py**"
    assert la._expected_task_paths(task) == {"pipeline/server.py", "app/dashboard.py"}


def test_expected_task_paths_returns_empty_set_for_no_paths_named():
    assert la._expected_task_paths("Fix the bug in the login flow.") == set()


def test_expected_task_paths_returns_empty_set_for_empty_task():
    assert la._expected_task_paths("") == set()


def test_is_off_task_path_false_for_exact_match():
    assert la._is_off_task_path("pipeline/server.py", {"pipeline/server.py"}) is False


def test_is_off_task_path_false_for_dot_slash_prefixed_relative_form():
    assert la._is_off_task_path("./pipeline/server.py", {"pipeline/server.py"}) is False


def test_is_off_task_path_false_for_shared_basename():
    assert la._is_off_task_path("src/server.py", {"pipeline/server.py"}) is False


def test_is_off_task_path_true_for_unrelated_file():
    assert la._is_off_task_path(
        "scripts/unrelated_thing.py", {"pipeline/server.py", "app/dashboard.py"}
    ) is True


def test_is_off_task_path_fails_open_when_expected_is_empty():
    assert la._is_off_task_path("anything/at/all.py", set()) is False


def test_is_off_task_path_false_for_empty_path():
    assert la._is_off_task_path("", {"pipeline/server.py"}) is False
