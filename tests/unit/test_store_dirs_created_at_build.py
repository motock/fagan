"""The plans and worktree directories are created when a store is built.

Contract under test:

* Importing ``pipeline.server`` does not create the worktree root. Under a
  non-file store the plans directory is not created either: the host state
  it owns is file-backed and ensures its root (see ``build_host_state``).
* ``build_store()`` with the file store (unset, or ``PIPELINE_STORE=file``)
  creates both ``PLAN_DIR`` and ``WORKTREE_ROOT``.
* ``build_store()`` with the sqlite or plugin store creates neither.
* ``build_host_state()`` creates ``PLAN_DIR`` and is idempotent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import host_state, server, store

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def missing_dirs(tmp_path, monkeypatch):
    """Point the server's directory bindings at paths that do not exist yet."""
    plans = tmp_path / "plans"
    worktrees = tmp_path / "worktrees"
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(server, "WORKTREE_ROOT", worktrees)
    monkeypatch.delenv("PIPELINE_STORE", raising=False)
    return plans, worktrees


def test_importing_server_does_not_create_worktree_root(tmp_path):
    plans = tmp_path / "plans"
    worktrees = tmp_path / "worktrees"
    env = {
        **os.environ,
        "PLAN_DIR": str(plans),
        "WORKTREE_ROOT": str(worktrees),
        "PIPELINE_STORE": "sqlite",
        "PIPELINE_STORE_PATH": str(tmp_path / "store.db"),
    }

    subprocess.run(
        [sys.executable, "-c", "import pipeline.server"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
    )

    assert not worktrees.exists()


def test_build_store_file_creates_plan_and_worktree_directories(missing_dirs):
    plans, worktrees = missing_dirs

    store.build_store({})

    assert plans.is_dir()
    assert worktrees.is_dir()


def test_build_store_sqlite_creates_neither_directory(missing_dirs, tmp_path):
    plans, worktrees = missing_dirs

    store.build_store({"PIPELINE_STORE": "sqlite", "PIPELINE_STORE_PATH": str(tmp_path / "s.db")})

    assert not plans.exists()
    assert not worktrees.exists()


def test_build_store_plugin_creates_neither_directory(missing_dirs):
    plans, worktrees = missing_dirs

    store.build_store(
        {
            "PIPELINE_STORE": "plugin",
            "PIPELINE_STORE_PLUGIN": "tests.fixtures.store_plugin:build_record_store",
        }
    )

    assert not plans.exists()
    assert not worktrees.exists()


def test_build_host_state_creates_plan_directory_and_is_idempotent(missing_dirs):
    plans, _ = missing_dirs

    host_state.build_host_state()
    host_state.build_host_state()

    assert plans.is_dir()
