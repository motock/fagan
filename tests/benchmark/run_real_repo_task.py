"""
Utility functions for the real‑repo integration harness.

This module implements two small, independently testable helpers used by the
real‑repo benchmark driver.  The implementation closely mirrors the logic in
``harness.setup_workspace`` and ``harness.run_groundtruth`` but is adapted to
work with a full clone of the pipeline repository instead of a synthetic
scaffold.

The functions are intentionally minimal – they perform only what the tests
exercise, without any additional side‑effects.  They rely on the constants
``PIPELINE_REPO`` and ``VENV_PY`` defined in ``tests/benchmark/harness.py``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Import constants from harness – use the same import style as compound_harness.py
from harness import PIPELINE_REPO, VENV_PY


def setup_real_repo_workspace(cell: Path, base_commit: str) -> dict[str, Path]:
    """Create a throwaway workspace that contains a full clone of the pipeline repo.

    Parameters
    ----------
    cell:
        The directory in which to create the workspace.  Any existing contents are
        removed before creation.
    base_commit:
        A commit SHA (or ref) that the cloned repository will be checked out at.

    Returns
    -------
    dict[str, Path]
        Mapping with keys ``repo``, ``origin``, ``plans`` and ``worktrees``.  The
        values are :class:`~pathlib.Path` objects pointing to the corresponding
        directories inside *cell*.
    """
    # Ensure a clean cell directory
    if cell.exists():
        shutil.rmtree(cell)

    repo = cell / "repo"
    origin = cell / "origin.git"
    plans = cell / "plans"
    worktrees = cell / "worktrees"

    for d in (repo, origin, plans, worktrees):
        d.mkdir(parents=True)

    # Clone the pipeline repo locally – ``--local`` keeps it a copy of the same
    # working tree without network traffic.
    subprocess.run(["git", "clone", "--local", str(PIPELINE_REPO), str(repo)], check=True, capture_output=True)

    # Pin to the requested commit.  This detaches HEAD.
    subprocess.run(["git", "-C", str(repo), "checkout", base_commit], check=True, capture_output=True)

    # Create a real ``master`` branch pointing at that commit so pushes work.
    subprocess.run(["git", "-C", str(repo), "branch", "--force", "master", "HEAD"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "master"], check=True, capture_output=True)

    # Initialise the bare origin in the sibling directory.  The ``-q`` flag keeps
    # output quiet; ``-b master`` ensures the remote has a default branch.
    subprocess.run(["git", "init", "--bare", "-q", "-b", "master", "."], cwd=str(origin), check=True, capture_output=True)

    # Update the existing ``origin`` remote to point at the new bare repo.
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", str(origin)], check=True, capture_output=True)
    # Push master to the newly created origin to populate it with the base commit.
    subprocess.run(["git", "-C", str(repo), "push", "-q", "-u", "origin", "master"], check=True, capture_output=True)


def run_groundtruth_in_place(repo: Path, groundtruth_source: str, groundtruth_name: str = "test_groundtruth_review_story_lock_guard.py") -> dict:
    """Run a ground‑truth test file inside *repo* and clean up.

    The function writes ``groundtruth_source`` to ``repo / groundtruth_name``,
    executes it with the pipeline virtualenv's pytest, then removes the file
    regardless of success or failure.  It returns a dict compatible with
    :func:`harness.run_groundtruth`.
    """
    test_file = repo / groundtruth_name
    test_file.write_text(groundtruth_source)

    try:
        result = subprocess.run(
            [str(VENV_PY), "-m", "pytest", groundtruth_name, "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        # Ensure the throwaway file is removed even if pytest crashes.
        test_file.unlink(missing_ok=True)

    tail = (result.stdout + result.stderr)[-700:]
    return {"ran": True, "passed": result.returncode == 0, "tail": tail}
