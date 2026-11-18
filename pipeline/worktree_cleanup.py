"""Remove a finished story's worktree and its local branch.

WHY this module exists separately: ``pipeline.ci._mark_story_done_impl``
already saves the manifest under the plan lock and must not run git inside
that lock (a slow ``git worktree remove`` would block dispatch/ingest for the
plan). The cleanup therefore lives in its own helper the caller runs *after*
the transaction commits, and it is deliberately total: it returns one outcome
token rather than raising, because a leftover directory must never turn a
successfully recorded "done" into an error.

Safety rules encoded here:

* the path must live inside ``WORKTREE_ROOT`` - a mis-recorded ``worktree``
  field pointing at the repo, a home directory or ``/`` must never be
  force-removed;
* a worktree with tracked modifications is kept and only logged (count only,
  never paths or file contents) so a human can salvage the work;
* the remote branch is NOT deleted - the PR merge already did that, and this
  path must make no outward change.
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("pipeline")

# The one tracked path that never counts as "work" worth keeping: the
# pipeline writes the agent log into every worktree.
_IGNORED_STATUS_PATHS = frozenset({"agent.log"})


def _status_paths(worktree: str) -> list[str] | None:
    """Tracked paths that differ in *worktree*, or None when the probe fails.

    ``--untracked-files=no`` is deliberate: build/venv noise is untracked and
    must not pin a finished worktree forever. A failing probe is reported as
    None so the caller can treat it as unsafe ("dirty") rather than remove a
    worktree it could not inspect.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc is None or proc.returncode != 0:
        return None
    paths: list[str] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        # porcelain gives "<XY> <path>"; renames use "old -> new".
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        paths.append(path)
    return paths


def _branch_for(worktree: str, story_key: str) -> str:
    """The branch the worktree is actually on (alias-aware), else the convention."""
    from .pr import _resolve_story_branch

    return _resolve_story_branch(worktree, story_key)


def remove_finished_worktree(
    repo_root, worktree: str, story_key: str, *, worktree_root
) -> str:
    """Remove a finished story's worktree and local branch; never raises.

    Returns exactly one of ``absent``, ``outside_root``, ``dirty``,
    ``removed`` or ``failed`` (see the module docstring for the rules).
    """
    try:
        return _remove_finished_worktree(repo_root, worktree, story_key, worktree_root)
    except Exception:  # noqa: BLE001 - cleanup is best effort, never fatal
        logger.warning(
            "worktree cleanup crashed",
            extra={"story_key": story_key, "outcome": "failed"},
        )
        return "failed"


def _remove_finished_worktree(
    repo_root, worktree: str, story_key: str, worktree_root
) -> str:
    if not worktree:
        return "absent"
    path = Path(worktree)
    if not path.exists():
        return "absent"

    root = Path(str(worktree_root))
    try:
        inside = path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return "outside_root"
    if not inside:
        return "outside_root"

    changed = _status_paths(str(path))
    if changed is None:
        logger.warning(
            "worktree status probe failed; keeping the worktree",
            extra={"story_key": story_key, "outcome": "dirty"},
        )
        return "dirty"
    work_paths = [p for p in changed if p not in _IGNORED_STATUS_PATHS]
    if work_paths:
        logger.warning(
            "worktree has uncommitted changes; keeping it",
            extra={
                "story_key": story_key,
                "outcome": "dirty",
                "changed_path_count": len(changed),
            },
        )
        return "dirty"

    # Resolve the branch BEFORE removal: a removed worktree has no HEAD and
    # the alias suffix (agent/<key>-<suffix>) is only visible from the worktree.
    branch = _branch_for(str(path), story_key)

    try:
        removed = subprocess.run(
            ["git", "worktree", "remove", "--force", str(path)],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        removed = None
    if removed is None or removed.returncode != 0:
        logger.warning(
            "git worktree remove failed; keeping the worktree",
            extra={"story_key": story_key, "outcome": "failed"},
        )
        return "failed"

    # The branch may already be gone (merged outside the pipeline, or a
    # previous partial cleanup): that is still a successful cleanup.
    try:
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        logger.warning(
            "git branch deletion failed; branch may already be gone",
            extra={"story_key": story_key, "outcome": "removed"},
        )
    return "removed"
