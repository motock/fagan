import json
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.test_scope_run import (
    NO_TESTS_CMD,
    agent_scoped_test_argv,
    changed_test_paths,
    grade_test_cmd,
    is_no_tests_cmd,
    local_scope_kwargs,
)

TEST_DIR = Path("tests")
BASE_TEST_CMD = [sys.executable, "-m", "pytest", "-q"]
FULL_STORY = {"test_scope_resolution": {"test_scope": "full"}}
SCOPED_STORY = {"test_scope_resolution": {"test_scope": "scoped"}}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "-c", "init.defaultBranch=main", "init", "-q")
    _write(root / "tests" / "test_base.py", "def test_base(): pass\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "agent/S1")
    _write(root / "tests" / "unit" / "test_new.py", "def test_new(): pass\n")
    _write(root / "src" / "x.py", "X = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "story")
    _write(root / "tests" / "test_wip.py", "def test_wip(): pass\n")
    _git(root, "add", "tests/test_wip.py")
    _write(root / "tests" / "test_untracked.py", "def test_untracked(): pass\n")
    return root


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "-c", "init.defaultBranch=main", "init", "-q")
    return root


@pytest.fixture
def default_main(monkeypatch):
    monkeypatch.setattr("pipeline.server._default_branch", lambda: "main")


def test_changed_test_paths_returns_committed_staged_and_untracked_tests(repo):
    assert changed_test_paths(repo, "main") == [
        "tests/test_untracked.py",
        "tests/test_wip.py",
        "tests/unit/test_new.py",
    ]


def test_changed_test_paths_excludes_source_and_pre_existing_tests(repo):
    assert "src/x.py" not in changed_test_paths(repo, "main")
    assert "tests/test_base.py" not in changed_test_paths(repo, "main")


def test_changed_test_paths_returns_empty_for_nonexistent_worktree(tmp_path):
    assert changed_test_paths(tmp_path / "missing", "main") == []


def test_changed_test_paths_returns_empty_for_unknown_base_branch(repo):
    assert changed_test_paths(repo, "no-such-branch") == []


def test_changed_test_paths_returns_empty_for_repo_without_commits(empty_repo):
    assert changed_test_paths(empty_repo, "main") == []


@pytest.mark.parametrize("story", [{}, FULL_STORY])
def test_grade_test_cmd_returns_same_object_in_full_mode(story, monkeypatch):
    def _no_subprocess(*args, **kwargs):
        raise AssertionError("full mode must not run git")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    test_cmd = ["pytest", "-q"]
    result = grade_test_cmd(story, "S1", Path("."), TEST_DIR, BASE_TEST_CMD, test_cmd)
    assert result is test_cmd


def test_grade_test_cmd_scoped_orders_acceptance_before_own_tests(repo, default_main):
    story = {**SCOPED_STORY, "acceptance": [{"path": "tests/acceptance_x.py", "source": ""}]}
    cmd = grade_test_cmd(story, "S1", repo, TEST_DIR, BASE_TEST_CMD, ["full"])
    assert cmd == [
        *BASE_TEST_CMD,
        str(repo / "tests/acceptance_x.py"),
        str(repo / "tests/test_untracked.py"),
        str(repo / "tests/test_wip.py"),
        str(repo / "tests/unit/test_new.py"),
    ]


def test_grade_test_cmd_scoped_appends_ci_failure_node_id_on_ci_rework(repo, default_main):
    story = {
        **SCOPED_STORY,
        "ci_rework": True,
        "review_feedback": "FAILED tests/unit/test_a.py::test_x - assert 0",
    }
    cmd = grade_test_cmd(story, "S1", repo, TEST_DIR, BASE_TEST_CMD, ["full"])
    assert cmd[-1] == f"{repo / 'tests/unit/test_a.py'}::test_x"


def test_grade_test_cmd_scoped_ignores_ci_failures_without_ci_rework(repo, default_main):
    story = {
        **SCOPED_STORY,
        "review_feedback": "FAILED tests/unit/test_a.py::test_x - assert 0",
    }
    cmd = grade_test_cmd(story, "S1", repo, TEST_DIR, BASE_TEST_CMD, ["full"])
    assert not any("test_a.py" in part for part in cmd)


def test_grade_test_cmd_scoped_without_scope_returns_no_tests(empty_repo, default_main):
    story = {**SCOPED_STORY}
    cmd = grade_test_cmd(story, "S1", empty_repo, TEST_DIR, BASE_TEST_CMD, ["full"])
    assert is_no_tests_cmd(cmd)
    assert cmd == NO_TESTS_CMD


def test_grade_test_cmd_records_targets_and_no_tests_flag(empty_repo, default_main):
    story = {**SCOPED_STORY}
    grade_test_cmd(story, "S1", empty_repo, TEST_DIR, BASE_TEST_CMD, ["full"])
    grade = story["test_scope_grade"]
    assert grade["targets"] == []
    assert grade["no_tests"] is True
    assert isinstance(grade["ts"], str)


def test_grade_test_cmd_records_no_tests_false_when_targets_present(repo, default_main):
    story = {**SCOPED_STORY, "acceptance": [{"path": "tests/acceptance_x.py", "source": ""}]}
    grade_test_cmd(story, "S1", repo, TEST_DIR, BASE_TEST_CMD, ["full"])
    assert story["test_scope_grade"]["no_tests"] is False


@pytest.mark.parametrize("backend", ["claude", "anthropic", "litellm"])
def test_local_scope_kwargs_empty_for_non_local_backend(backend, repo):
    assert local_scope_kwargs(SCOPED_STORY, backend, repo) == {}


def test_local_scope_kwargs_empty_for_local_backend_in_full_mode(repo):
    assert local_scope_kwargs(FULL_STORY, "ollama", repo) == {}


def test_local_scope_kwargs_returns_spec_for_local_backend_in_scoped_mode(repo, monkeypatch):
    monkeypatch.setattr("pipeline.server._default_branch", lambda: "main")
    story = {
        **SCOPED_STORY,
        "acceptance": [{"path": "tests/acceptance_x.py", "source": ""}],
        "ci_rework": True,
        "review_feedback": "FAILED tests/unit/test_a.py::test_x - assert 0",
    }
    assert local_scope_kwargs(story, "ollama", repo) == {
        "test_scope_spec": {
            "acceptance": ["tests/acceptance_x.py"],
            "ci_failures": ["tests/unit/test_a.py::test_x"],
            "base_branch": "main",
        }
    }


def test_local_scope_kwargs_empty_ci_failures_without_ci_rework(repo, monkeypatch):
    monkeypatch.setattr("pipeline.server._default_branch", lambda: "main")
    story = {**SCOPED_STORY, "review_feedback": "FAILED tests/unit/test_a.py::test_x - assert 0"}
    spec = local_scope_kwargs(story, "mlx", repo)["test_scope_spec"]
    assert spec["ci_failures"] == []


@pytest.mark.parametrize("raw_spec", [None, ""])
def test_agent_scoped_test_argv_returns_none_when_unset(raw_spec, repo):
    assert agent_scoped_test_argv(repo, TEST_DIR, BASE_TEST_CMD, raw_spec) is None


def test_agent_scoped_test_argv_rejects_malformed_json(repo, capsys):
    assert agent_scoped_test_argv(repo, TEST_DIR, BASE_TEST_CMD, "{bad") is None
    assert "test_scope: ignoring malformed LOCAL_AGENT_TEST_SCOPE" in capsys.readouterr().err


@pytest.mark.parametrize(
    "raw_spec",
    [
        json.dumps({"acceptance": "x", "ci_failures": [], "base_branch": "main"}),
        json.dumps({"acceptance": [], "ci_failures": "x", "base_branch": "main"}),
        json.dumps({"acceptance": [], "ci_failures": [], "base_branch": 3}),
        json.dumps(["not", "a", "dict"]),
    ],
)
def test_agent_scoped_test_argv_rejects_wrong_shape(raw_spec, repo, capsys):
    assert agent_scoped_test_argv(repo, TEST_DIR, BASE_TEST_CMD, raw_spec) is None
    assert "test_scope: ignoring malformed LOCAL_AGENT_TEST_SCOPE" in capsys.readouterr().err


def test_agent_scoped_test_argv_builds_absolute_paths_for_valid_spec(repo):
    raw_spec = json.dumps(
        {"acceptance": ["tests/acceptance_x.py"], "ci_failures": [], "base_branch": "main"}
    )
    assert agent_scoped_test_argv(repo, TEST_DIR, BASE_TEST_CMD, raw_spec) == [
        *BASE_TEST_CMD,
        str(repo / "tests/acceptance_x.py"),
        str(repo / "tests/test_untracked.py"),
        str(repo / "tests/test_wip.py"),
        str(repo / "tests/unit/test_new.py"),
    ]


def test_agent_scoped_test_argv_returns_no_tests_when_nothing_in_scope(empty_repo):
    raw_spec = json.dumps({"acceptance": [], "ci_failures": [], "base_branch": "main"})
    assert agent_scoped_test_argv(empty_repo, TEST_DIR, BASE_TEST_CMD, raw_spec) == NO_TESTS_CMD
