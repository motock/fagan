"""Fresh-worktree setup carved out of ``pipeline.dispatch``.

Holds the git setup (fetch + ``git worktree add``) and venv provisioning for a
story's first dispatch. Every free name it reads that is a module-level binding
of ``pipeline.dispatch`` is resolved lazily through ``_ModuleRef``, so
``monkeypatch.setattr(pipeline.dispatch, "NAME", ...)`` still lands.
"""

import subprocess
from pathlib import Path
from typing import Any

from .module_ref import _ModuleRef

_scoped_repo_root = _ModuleRef("pipeline.dispatch", "_scoped_repo_root")
_try_acquire_git_lock = _ModuleRef("pipeline.dispatch", "_try_acquire_git_lock")
_default_branch = _ModuleRef("pipeline.dispatch", "_default_branch")
_exclude_worktree_logs_from_tracking = _ModuleRef(
    "pipeline.dispatch", "_exclude_worktree_logs_from_tracking"
)
_provision_worktree_venv = _ModuleRef(
    "pipeline.dispatch", "_provision_worktree_venv"
)


def _create_fresh_worktree(
    plan_name: str, branch: str, worktree_path: Path
) -> dict[str, Any] | None:
    """Create the story branch's worktree from ``origin/<default>``.

    Runs the fresh-dispatch git setup (fetch the default branch, then
    ``git worktree add -b <branch>``) and provisions the new worktree's own
    virtualenv. Returns the structured ``{"ok": False, "error": ...}`` dict when
    git setup fails (never raises ``CalledProcessError``), otherwise ``None``.
    """
    try:
        with _scoped_repo_root(plan_name) as repo_root:
            with _try_acquire_git_lock(repo_root) as acquired:
                if acquired:
                    subprocess.run(
                        ["git", "fetch", "origin", _default_branch()],
                        cwd=repo_root,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
            subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree_path),
                    f"origin/{_default_branch()}",
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
    except subprocess.CalledProcessError as e:
        # A fresh dispatch's git setup (fetch + worktree add) can fail
        # for reasons outside the story itself - e.g. repo_root has no
        # usable 'origin' remote, or git can't authenticate. Left
        # uncaught, this used to escape all the way to an unhandled
        # 500 with an EMPTY body (no JSON), which broke every caller's
        # response.json() with "Expecting value: line 1 column 1 (char
        # 0)" - reproduced live twice via chat's dispatch_story tool
        # call. Return a structured, actionable failure instead.
        stderr = (e.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        return {
            "ok": False,
            "error": (
                f"git setup failed for repo_root {str(repo_root)!r} "
                f"(command {' '.join(e.cmd)!r}, exit {e.returncode})"
                f"{detail}"
            ),
        }
    _exclude_worktree_logs_from_tracking(Path(repo_root))
    # A fresh worktree has no .venv (gitignored) - give it its own
    # complete one now rather than let it fall back to (and
    # potentially mutate) the shared main-repo venv other
    # concurrently-dispatched stories may be using. See
    # _provision_worktree_venv's docstring for the failure mode
    # this closes (root-caused live on RUFF-016-ADOPTION).
    # No-ops for non-Python projects or ones without a
    # requirements file.
    _provision_worktree_venv(worktree_path)
    return None
