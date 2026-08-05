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


def test_load_task_skips_pycache_and_binary_pyc_in_seed_dir(tmp_path, monkeypatch):
    """Regression test for the ratelimiter_bugfix harness crash: stale
    `__pycache__/*.pyc` compiled-bytecode files inside seed/ are binary and
    crash path.read_text() (no encoding error handling) with
    `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe1`.

    load_task() must (a) NOT raise, and (b) include the real .py source in
    seed_files while skipping the __pycache__ entry entirely (neither the
    directory nor the .pyc file leaks into seed_files).
    """
    task_dir = tmp_path / "tasks" / "ratelimiter_bugfix"
    seed_dir = task_dir / "seed"
    seed_dir.mkdir(parents=True)
    (task_dir / "spec.json").write_text('{"name": "ratelimiter_bugfix", "summary": "x"}')
    (task_dir / "acceptance.py").write_text("")
    (task_dir / "groundtruth.py").write_text("")
    # A real, valid UTF-8 source file that must be loaded.
    (seed_dir / "ratelimiter.py").write_text("class RateLimiter:\n    pass\n")
    # A nested __pycache__ dir holding a binary .pyc with non-UTF-8 bytes
    # (mirrors the live repro: test_ratelimiter.cpython-314-pytest-9.1.0.pyc).
    pycache = seed_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "test_ratelimiter.cpython-314-pytest-9.1.0.pyc").write_bytes(
        b"\x00\xe1\x00not-utf8"
    )
    monkeypatch.setattr(h, "TASKS_DIR", tmp_path / "tasks")

    task = h.load_task("ratelimiter_bugfix")

    assert task["seed_files"] == {"ratelimiter.py": "class RateLimiter:\n    pass\n"}
    # Explicitly assert the __pycache__ entry is absent under any key shape.
    assert not any("__pycache__" in key for key in task["seed_files"])
    assert not any(key.endswith(".pyc") for key in task["seed_files"])


def test_load_task_skips_stray_non_utf8_file_outside_pycache(tmp_path, monkeypatch):
    """Boundary/negative case: a stray binary file that is NOT under
    __pycache__ (e.g. a committed .bin asset) must also be skipped rather
    than crashing the whole cell, while a sibling UTF-8 source still loads.
    """
    task_dir = tmp_path / "tasks" / "mixed_assets"
    seed_dir = task_dir / "seed"
    seed_dir.mkdir(parents=True)
    (task_dir / "spec.json").write_text('{"name": "mixed_assets", "summary": "x"}')
    (task_dir / "acceptance.py").write_text("")
    (task_dir / "groundtruth.py").write_text("")
    (seed_dir / "main.py").write_text("x = 1\n")
    (seed_dir / "asset.bin").write_bytes(b"\xe1\xfe\xff\x00bad")
    monkeypatch.setattr(h, "TASKS_DIR", tmp_path / "tasks")

    task = h.load_task("mixed_assets")

    assert task["seed_files"] == {"main.py": "x = 1\n"}
    assert not any(key.endswith(".bin") for key in task["seed_files"])


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
