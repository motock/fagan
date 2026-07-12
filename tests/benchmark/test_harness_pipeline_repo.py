"""Regression tests for harness.py's PIPELINE_REPO / VENV_PY resolution.

PIPELINE_REPO used to be a fixed relative-path assumption
(BENCH_DIR.parents[1], "two dirs up from this file"). That silently breaks
when harness.py is loaded from inside a git worktree of the pipeline repo:
BENCH_DIR.parents[1] then resolves to the WORKTREE root, which never
contains .venv (worktrees are gitignored and never contain one). Every
downstream consumer of VENV_PY (setup_workspace's pytest-ecosystem fixture,
run_groundtruth) then breaks -- the .venv symlink points at a nonexistent
path and every trial falls back to a bare `pytest` invocation that isn't on
PATH, crashing with FileNotFoundError.

harness._resolve_pipeline_repo mirrors pipeline_mcp_server.py's
_venv_python_for: resolve via `git rev-parse --git-common-dir`, which
always points at the MAIN repo's .git even when invoked from one of its
worktrees, then take that path's parent.

Run: pytest tests/benchmark/test_harness_pipeline_repo.py
"""
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
PIPELINE_REPO = BENCH.parents[1]
VENV_PY = PIPELINE_REPO / ".venv" / "bin" / "python"
PY = str(VENV_PY) if VENV_PY.exists() else sys.executable

if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import harness  # noqa: E402


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "test", cwd=repo)
    (repo / "README.md").write_text("placeholder\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)


# ---------- (a) regression: normal (non-worktree) checkout ----------
def test_resolve_pipeline_repo_normal_checkout_matches_repo_root(tmp_path):
    """A plain checkout (not a worktree) must resolve to the same repo root
    that BENCH_DIR.parents[1] would have produced -- the fix must not change
    behavior in the common case."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    bench_dir = repo / "tests" / "benchmark"
    bench_dir.mkdir(parents=True)

    resolved = harness._resolve_pipeline_repo(bench_dir)

    assert resolved == repo.resolve()
    assert resolved == bench_dir.parents[1].resolve()


# ---------- (b) core regression: git worktree ----------
def test_resolve_pipeline_repo_worktree_resolves_to_main_repo_not_worktree_root(tmp_path):
    """The core regression: when bench_dir sits inside a git worktree of the
    pipeline repo, resolution must land on the MAIN repo root, not the
    worktree root (which has no .venv)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    bench_dir_in_repo = repo / "tests" / "benchmark"
    bench_dir_in_repo.mkdir(parents=True)
    (bench_dir_in_repo / "harness.py").write_text("# placeholder\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "add benchmark dir", cwd=repo)

    worktree = tmp_path / "wt"
    _git("worktree", "add", str(worktree), "-b", "wt-branch", cwd=repo)

    worktree_bench_dir = worktree / "tests" / "benchmark"
    assert worktree_bench_dir.exists()  # sanity: tracked file made it into the worktree
    assert not (worktree / ".venv").exists()  # sanity: worktrees never have .venv

    resolved = harness._resolve_pipeline_repo(worktree_bench_dir)

    assert resolved == repo.resolve()
    assert resolved != worktree.resolve()
    assert resolved != worktree_bench_dir.parents[1].resolve()  # the buggy old behavior


# ---------- (c) fallback when git resolution fails ----------
def test_resolve_pipeline_repo_falls_back_when_git_resolution_fails(tmp_path, monkeypatch):
    """If git resolution fails for any reason, fall back to the old
    BENCH_DIR.parents[1] behavior instead of raising."""
    bench_dir = tmp_path / "a" / "b"
    bench_dir.mkdir(parents=True)

    def _fake_run(*args, **kwargs):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.returncode = 128
        m.stdout = ""
        m.stderr = "fatal: not a git repository"
        return m

    monkeypatch.setattr(harness.subprocess, "run", _fake_run)

    resolved = harness._resolve_pipeline_repo(bench_dir)

    assert resolved == bench_dir.parents[1]


def test_resolve_pipeline_repo_falls_back_when_git_raises(tmp_path, monkeypatch):
    """If invoking git itself raises (e.g. git is not installed), resolution
    must not raise -- it must fall back to the old behavior instead of
    breaking module import."""
    bench_dir = tmp_path / "a" / "b"
    bench_dir.mkdir(parents=True)

    def _raise(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(harness.subprocess, "run", _raise)

    resolved = harness._resolve_pipeline_repo(bench_dir)

    assert resolved == bench_dir.parents[1]


# ---------- (d) VENV_PY resolves to a real interpreter from a worktree ----------
def test_venv_py_resolves_to_real_interpreter_from_worktree(tmp_path):
    """The actual downstream bug this story fixes: VENV_PY, derived from
    PIPELINE_REPO, must point at a real, executable interpreter when
    resolved from a worktree context -- not a path under the worktree root
    that doesn't exist (which previously caused FileNotFoundError when
    subprocess tried to exec it)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    bench_dir_in_repo = repo / "tests" / "benchmark"
    bench_dir_in_repo.mkdir(parents=True)
    (bench_dir_in_repo / "harness.py").write_text("# placeholder\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "add benchmark dir", cwd=repo)

    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_python = venv_bin / "python"
    fake_python.write_text("#!/bin/sh\nexit 0\n")
    fake_python.chmod(0o755)

    worktree = tmp_path / "wt"
    _git("worktree", "add", str(worktree), "-b", "wt-branch2", cwd=repo)
    worktree_bench_dir = worktree / "tests" / "benchmark"

    resolved_repo = harness._resolve_pipeline_repo(worktree_bench_dir)
    resolved_venv_py = resolved_repo / ".venv" / "bin" / "python"

    assert resolved_venv_py.exists()
    completed = subprocess.run([str(resolved_venv_py)], capture_output=True)
    assert completed.returncode == 0


# ---------- boundary: bench_dir is itself the repo root ----------
def test_resolve_pipeline_repo_bench_dir_is_repo_root_is_idempotent(tmp_path):
    """When bench_dir is itself the repo root (git rev-parse --show-toplevel
    would return the same path), resolution must be a no-op."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    resolved = harness._resolve_pipeline_repo(repo)

    assert resolved == repo.resolve()


# ---------- module-level constants ----------
def test_module_level_pipeline_repo_is_a_git_repo_root():
    """Regression guard: the live harness.PIPELINE_REPO (computed at import
    time from the real BENCH_DIR) must itself be a valid git repo root, and
    harness.VENV_PY must be derived from it."""
    assert (harness.PIPELINE_REPO / ".git").exists()
    assert harness.VENV_PY == harness.PIPELINE_REPO / ".venv" / "bin" / "python"
