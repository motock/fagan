"""Tests for tiered-task seed-file support: load_task() reading a task's
seed/ directory, and setup_workspace() writing those files into the repo
before the initial commit (so a T2 "modify existing code" task starts from
an existing, committed codebase rather than an empty repo like the T1
katas). Offline - no model/network involved.
"""
import subprocess

import harness as h


def test_load_task_with_no_seed_dir_returns_empty_seed_files(tmp_path, monkeypatch):
    task_dir = tmp_path / "tasks" / "greenfield"
    task_dir.mkdir(parents=True)
    (task_dir / "spec.json").write_text('{"name": "greenfield", "summary": "x"}')
    (task_dir / "acceptance.py").write_text("")
    (task_dir / "groundtruth.py").write_text("")
    monkeypatch.setattr(h, "TASKS_DIR", tmp_path / "tasks")

    task = h.load_task("greenfield")

    assert task["seed_files"] == {}


def test_load_task_reads_seed_directory_into_a_relpath_dict(tmp_path, monkeypatch):
    task_dir = tmp_path / "tasks" / "modify_existing"
    seed_dir = task_dir / "seed"
    seed_dir.mkdir(parents=True)
    (task_dir / "spec.json").write_text('{"name": "modify_existing", "summary": "x"}')
    (task_dir / "acceptance.py").write_text("")
    (task_dir / "groundtruth.py").write_text("")
    (seed_dir / "existing.py").write_text("def f():\n    return 1\n")
    nested = seed_dir / "sub"
    nested.mkdir()
    (nested / "test_existing.py").write_text("from existing import f\n\ndef test_f():\n    assert f() == 1\n")
    monkeypatch.setattr(h, "TASKS_DIR", tmp_path / "tasks")

    task = h.load_task("modify_existing")

    assert task["seed_files"] == {
        "existing.py": "def f():\n    return 1\n",
        "sub/test_existing.py":
            "from existing import f\n\ndef test_f():\n    assert f() == 1\n",
    }


def test_setup_workspace_writes_seed_files_before_init_commit(tmp_path):
    cell = tmp_path / "cell"
    task = {"seed_files": {"existing.py": "def f():\n    return 1\n"}}

    paths = h.setup_workspace(cell, task)

    assert (paths["repo"] / "existing.py").read_text() == "def f():\n    return 1\n"
    # The seed file must be part of the initial commit (git status clean),
    # not left as an uncommitted addition a dispatched agent's first `git
    # diff`/`git status` would see as unexplained noise.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=paths["repo"],
                            capture_output=True, text=True, check=True)
    assert status.stdout.strip() == ""
    log = subprocess.run(["git", "log", "--oneline"], cwd=paths["repo"],
                         capture_output=True, text=True, check=True)
    assert len(log.stdout.strip().splitlines()) == 1  # still one commit, not two


def test_setup_workspace_creates_nested_directories_for_seed_files(tmp_path):
    cell = tmp_path / "cell"
    task = {"seed_files": {"pkg/sub/existing.py": "x = 1\n"}}

    paths = h.setup_workspace(cell, task)

    assert (paths["repo"] / "pkg" / "sub" / "existing.py").read_text() == "x = 1\n"


def test_setup_workspace_without_task_arg_still_works_like_before(tmp_path):
    """Regression guard: existing T1 tasks / any caller that doesn't pass
    task must behave exactly as before this story - an empty scaffold repo,
    no seed files."""
    cell = tmp_path / "cell"

    paths = h.setup_workspace(cell)

    assert not (paths["repo"] / "existing.py").exists()
    assert (paths["repo"] / "pyproject.toml").exists()
