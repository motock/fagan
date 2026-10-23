"""Fail-closed check that origin does not already hold a story's branch."""

import subprocess
from pathlib import Path

_LS_REMOTE_TIMEOUT_SECONDS = 30


class BranchGuardError(RuntimeError):
    """origin could not be queried. The message never includes the remote URL."""


def _has_origin(repo_root: Path) -> bool:
    result = subprocess.run(
        ["git", "remote"], cwd=repo_root, capture_output=True, text=True
    )
    return result.returncode == 0 and "origin" in result.stdout.split()


def remote_branch_exists(repo_root: Path | str, branch: str) -> bool:
    """True when ``origin`` has ``refs/heads/<branch>``; False when it does not
    or when the repo has no ``origin`` remote.

    Raises BranchGuardError if origin is configured but cannot be queried, so a
    caller can deny by default. git's stderr is deliberately not echoed: it
    carries the remote URL.
    """
    try:
        if not _has_origin(Path(repo_root)):
            return False
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_LS_REMOTE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BranchGuardError(
            f"could not query origin for branch {branch} ({type(exc).__name__})"
        ) from None
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise BranchGuardError(
        f"could not query origin for branch {branch} "
        f"(git ls-remote exit {result.returncode})"
    )
