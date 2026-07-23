"""
Test that ``setup_real_repo_workspace`` pushes the base commit to the
bare origin immediately after creation.

The original implementation omitted the push step, leaving the bare repo
empty.  This test reproduces that failure by asserting the SHA of the
``master`` ref in the bare repository matches the requested ``base_commit``.
"""

import subprocess
from pathlib import Path

import pytest

# Import harness constants and the implementation under test
import harness
import run_real_repo_task as rrt


def _make_fixture_repo(tmp_path: Path) -> str:
    """Create a tiny git repo with two commits and return the SHA of the first commit.

    The function mirrors the helper used in ``tests/benchmark/test_run_real_repo_task.py``.
    It returns the SHA string that will be passed as ``base_commit`` to the
    workspace setup.
    """
    repo = tmp_path / "fixture"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "bench@local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "bench"], cwd=repo, check=True)

    (repo / "a.txt").write_text("first\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=repo, check=True)
    first_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    (repo / "b.txt").write_text("second\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=repo, check=True)

    return first_sha


@pytest.mark.parametrize("base_commit", ["first"])
def test_origin_contains_base_commit_after_setup(tmp_path: Path, monkeypatch):
    # Create fixture repo and pin to its first commit
    sha = _make_fixture_repo(tmp_path)
    fixture = tmp_path / "fixture"

    # Point harness constants to the fixture so the implementation clones it
    monkeypatch.setattr(harness, "PIPELINE_REPO", fixture)
    monkeypatch.setattr(rrt, "PIPELINE_REPO", fixture)

    cell = tmp_path / "cell"
    result = rrt.setup_real_repo_workspace(cell, sha)
    origin = result["origin"]
    rev = subprocess.run(["git", "-C", str(origin), "rev-parse", "refs/heads/master"], check=True, capture_output=True, text=True).stdout.strip()
    assert rev == sha
