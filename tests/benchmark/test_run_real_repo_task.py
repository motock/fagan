"""Tests for the two mechanical pieces of the real-repo benchmark driver
(`tests/benchmark/run_real_repo_task.py`): `setup_real_repo_workspace` and
`run_groundtruth_in_place`.

This is step 1 of REAL_REPO_INTEGRATION_TEST_PLAN.md: the purely-mechanical,
independently-testable helpers that a later follow-up story wires into a
full task spec + CLI. These tests build a tiny throwaway git fixture repo
inline (NOT the real pipeline repo) so they stay fast, hermetic, and
independent of Ollama / any live model.

The functions under test import and reuse `harness.py`'s working pieces
(PIPELINE_REPO, VENV_PY) the same way `compound_harness.py` does, rather
than editing the 1054-line harness.py.
"""
import os
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import harness  # noqa: E402
import run_real_repo_task as rrt  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers: build a tiny throwaway git repo inside tmp_path.
# ---------------------------------------------------------------------------

def _make_fixture_repo(root: Path) -> str:
    """Create a minimal git repo with two commits and return the HEAD sha.

    This stands in for the real pipeline repo (PIPELINE_REPO) that the real
    driver clones -- but it's tiny and hermetic, so the tests don't depend
    on the actual repo's contents or any live model.
    """
    repo = root / "fixture"
    repo.mkdir(parents=True)
    env = {
        "GIT_AUTHOR_NAME": "bench",
        "GIT_AUTHOR_EMAIL": "bench@local",
        "GIT_COMMITTER_NAME": "bench",
        "GIT_COMMITTER_EMAIL": "bench@local",
    }
    subprocess.run(["git", "init", "-q", "-b", "master", "."], cwd=repo, check=True, env={**os.environ, **env})
    (repo / "README.md").write_text("# fixture\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=repo, check=True, env={**os.environ, **env})
    (repo / "file.txt").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=repo, check=True, env={**os.environ, **env})
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return sha


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# setup_real_repo_workspace
# ---------------------------------------------------------------------------

def test_setup_clones_fixture_and_pins_commit(tmp_path, monkeypatch):
    """setup_real_repo_workspace clones the fixture repo and checks out the
    exact requested commit; all four returned paths exist as directories."""
    sha = _make_fixture_repo(tmp_path)
    fixture = tmp_path / "fixture"

    # Point PIPELINE_REPO at the fixture so the clone source is the tiny
    # throwaway repo, not the real pipeline repo.
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    cell = tmp_path / "cell"
    result = rrt.setup_real_repo_workspace(cell, sha)

    assert set(result) == {"repo", "origin", "plans", "worktrees"}
    for key in ("repo", "origin", "plans", "worktrees"):
        assert result[key].is_dir(), f"{key} should be a directory"

    head = _git(["rev-parse", "HEAD"], result["repo"]).stdout.strip()
    assert head == sha, "cloned repo HEAD must equal the requested base_commit"


def test_setup_origin_remote_is_pushable(tmp_path, monkeypatch):
    """The origin bare remote is genuinely wired up: a new commit in the
    clone can be pushed to origin, proving a real master branch + bare
    remote exist (not just an empty origin.git dir)."""
    sha = _make_fixture_repo(tmp_path)
    fixture = tmp_path / "fixture"
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    cell = tmp_path / "cell"
    result = rrt.setup_real_repo_workspace(cell, sha)
    repo = result["repo"]

    # The checked-out base must be on a branch named master (not detached),
    # so a push to origin master works.
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
    assert branch == "master", f"expected master branch, got {branch!r}"

    # Make a new commit and push it to origin.
    (repo / "new.txt").write_text("new\n")
    env = {
        "GIT_AUTHOR_NAME": "bench",
        "GIT_AUTHOR_EMAIL": "bench@local",
        "GIT_COMMITTER_NAME": "bench",
        "GIT_COMMITTER_EMAIL": "bench@local",
    }
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "third"], cwd=repo, check=True, env={**os.environ, **env})
    # This push must succeed (no CalledProcessError).
    _git(["push", "origin", "master"], repo)


def test_setup_venv_symlink_points_at_pipeline_venv(tmp_path, monkeypatch):
    """The .venv symlink inside the clone points at the real pipeline .venv."""
    sha = _make_fixture_repo(tmp_path)
    fixture = tmp_path / "fixture"
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    # The fixture repo has no .venv; create a fake one so the symlink target
    # exists and resolves (mirrors the real PIPELINE_REPO/.venv).
    fake_venv = fixture / ".venv"
    fake_venv.mkdir(exist_ok=True)

    cell = tmp_path / "cell"
    result = rrt.setup_real_repo_workspace(cell, sha)
    venv_link = result["repo"] / ".venv"
    assert venv_link.is_symlink(), ".venv inside clone should be a symlink"
    target = os.readlink(str(venv_link))
    assert Path(target) == fake_venv or Path(target).resolve() == fake_venv.resolve(), (
        f".venv symlink target {target!r} should point at the pipeline .venv"
    )


def test_setup_nonexistent_commit_raises(tmp_path, monkeypatch):
    """A nonexistent base_commit SHA must raise (CalledProcessError), not
    silently succeed or return a partial result."""
    _make_fixture_repo(tmp_path)
    fixture = tmp_path / "fixture"
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    cell = tmp_path / "cell"
    bogus = "0" * 40
    try:
        rrt.setup_real_repo_workspace(cell, bogus)
    except subprocess.CalledProcessError:
        return  # expected
    raise AssertionError(
        "setup_real_repo_workspace should raise CalledProcessError for a "
        "nonexistent base_commit, but it returned without error"
    )


def test_setup_clears_existing_cell(tmp_path, monkeypatch):
    """If the cell dir already exists, it is cleared first (idempotent)."""
    sha = _make_fixture_repo(tmp_path)
    fixture = tmp_path / "fixture"
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    cell = tmp_path / "cell"
    cell.mkdir(parents=True)
    (cell / "stale.txt").write_text("stale\n")

    result = rrt.setup_real_repo_workspace(cell, sha)
    assert not (cell / "stale.txt").exists(), "pre-existing cell should be cleared"
    head = _git(["rev-parse", "HEAD"], result["repo"]).stdout.strip()
    assert head == sha


# ---------------------------------------------------------------------------
# run_groundtruth_in_place
# ---------------------------------------------------------------------------

def test_run_groundtruth_in_place_passing(tmp_path):
    """A trivially-passing groundtruth source returns passed=True and cleans
    up the throwaway test file afterward."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    name = "test_groundtruth_review_story_lock_guard.py"
    src = "def test_x():\n    assert True\n"

    result = rrt.run_groundtruth_in_place(repo, src, name)

    assert result["ran"] is True
    assert result["passed"] is True
    assert not (repo / name).exists(), "throwaway groundtruth file must be removed"


def test_run_groundtruth_in_place_failing(tmp_path):
    """A failing groundtruth source returns passed=False with a non-empty
    tail, and STILL cleans up the throwaway file (proves cleanup on the
    failure path, not only on success)."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    name = "test_groundtruth_review_story_lock_guard.py"
    src = "def test_x():\n    assert False, 'intentional failure'\n"

    result = rrt.run_groundtruth_in_place(repo, src, name)

    assert result["ran"] is True
    assert result["passed"] is False
    assert result["tail"], "tail should be non-empty for a failing run"
    assert not (repo / name).exists(), "throwaway file must be removed even on failure"


def test_run_groundtruth_in_place_default_name(tmp_path):
    """The default groundtruth_name is
    'test_groundtruth_review_story_lock_guard.py' when omitted."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    src = "def test_x():\n    assert True\n"

    result = rrt.run_groundtruth_in_place(repo, src)

    assert result["ran"] is True
    assert result["passed"] is True
    default = "test_groundtruth_review_story_lock_guard.py"
    assert not (repo / default).exists(), "default-named throwaway file must be removed"


def test_run_groundtruth_in_place_result_shape(tmp_path):
    """The returned dict has exactly the keys ran/passed/tail with the right
    types, matching run_groundtruth's shape so a later scorecard can consume
    it identically."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    result = rrt.run_groundtruth_in_place(repo, "def test_x():\n    assert True\n")
    assert set(result) == {"ran", "passed", "tail"}
    assert isinstance(result["ran"], bool)
    assert isinstance(result["passed"], bool)
    assert isinstance(result["tail"], str)