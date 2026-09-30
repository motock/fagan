"""Stale-base check for a resumed story dispatch.

Extracted from pipeline/dispatch.py's _dispatch_story_impl. The moved body
resolves its free names against THIS module's globals, so every name it reads
that is a module-level binding of pipeline.dispatch is bound here as a
_ModuleRef: the reference reads pipeline.dispatch's attribute at call time, so
a monkeypatch.setattr on either module (or on pipeline.server) keeps landing.
"""

import logging
import subprocess
from pathlib import Path
from typing import Any

from .module_ref import _ModuleRef

_scoped_repo_root = _ModuleRef("pipeline.dispatch", "_scoped_repo_root")
_try_acquire_git_lock = _ModuleRef("pipeline.dispatch", "_try_acquire_git_lock")
_default_branch = _ModuleRef("pipeline.dispatch", "_default_branch")
_rebase_onto_master = _ModuleRef("pipeline.dispatch", "_rebase_onto_master")
_notify_user = _ModuleRef("pipeline.dispatch", "_notify_user")
_sync_branch_remote = _ModuleRef("pipeline.dispatch", "_sync_branch_remote")
_atomic_write_json = _ModuleRef("pipeline.dispatch", "_atomic_write_json")


def _rebase_stale_resumed_worktree(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    branch: str,
    worktree_path: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """Rebase a resumed worktree that is behind origin/<default>.

    Returns the parked-story result dict on a rebase conflict (fail-secure:
    the caller must return it); returns None in every other case, including
    any git/infra failure (fail-open: dispatch proceeds).
    """
    if (worktree_path / ".git").exists():
        try:
            with _scoped_repo_root(plan_name) as repo_root:
                with _try_acquire_git_lock(repo_root) as acquired:
                    if acquired:
                        subprocess.run(
                            ["git", "fetch", "origin", _default_branch()],
                            cwd=repo_root,
                            check=True,
                        )
                count_out = subprocess.run(
                    [
                        "git",
                        "rev-list",
                        "--count",
                        f"{branch}..origin/{_default_branch()}",
                    ],
                    cwd=repo_root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                behind = int(count_out.stdout.strip() or "0")
                if behind > 0:
                    # The worktree base predates origin/<default>; a
                    # resumed agent would otherwise run on a stale base
                    # that could revert work merged since the worktree
                    # was created (live 2026-08-17). Actually rebase the
                    # worktree onto origin/<default> BEFORE the agent
                    # starts. Fail-secure on a rebase conflict (park,
                    # never run on the stale base); fail-open on any
                    # other git/infra failure (proceed, today's
                    # behavior). _rebase_onto_master never raises.
                    result = _rebase_onto_master(worktree_path, branch)
                    if result["ok"]:
                        _notify_user(
                            plan_name,
                            f"story {story_key} worktree base predates "
                            f"origin/{_default_branch()} by {behind} "
                            f"commit(s); rebased onto "
                            f"origin/{_default_branch()} before resume.",
                        )
                        logging.getLogger("pipeline").info(
                            "story %s worktree base predated "
                            "origin/%s by %s commit(s); rebased onto "
                            "origin/%s before resume",
                            story_key, _default_branch(), behind,
                            _default_branch(),
                        )
                        _sync = _sync_branch_remote(worktree_path, branch)
                        if not _sync.get("ok"):
                            logging.getLogger("pipeline").warning(
                                "remote sync for resumed story %s "
                                "failed; dispatching anyway (fail "
                                "open): %s",
                                story_key, _sync.get("error"),
                            )
                    elif result["conflict"]:
                        # Fail-secure: never dispatch an agent on a
                        # base that would revert merged work. Park the
                        # story for human resolution; a later resume
                        # can retry the rebase and proceed if it now
                        # succeeds (parked is a retryable state).
                        story["status"] = "parked"
                        story["parked_reason"] = (
                            f"rebase conflict: {result['error']}; "
                            f"worktree still behind "
                            f"origin/{_default_branch()}"
                        )
                        _atomic_write_json(manifest_path, manifest)
                        _notify_user(
                            plan_name,
                            f"story {story_key} parked: rebase conflict "
                            f"against origin/{_default_branch()} - "
                            f"{result['error']}",
                            event="story_parked",
                            story_key=story_key,
                            **(
                                {"correlation_id": story["correlation_id"]}
                                if story.get("correlation_id")
                                else {}
                            ),
                        )
                        logging.getLogger("pipeline").warning(
                            "story %s parked: rebase conflict against "
                            "origin/%s; worktree still behind",
                            story_key, _default_branch(),
                        )
                        return {
                            "status": "parked",
                            "reason": "rebase_conflict",
                            "parked_reason": story["parked_reason"],
                        }
                    else:
                        # Fail open on a non-conflict git/infra failure
                        # (e.g. git fetch timeout): notify and proceed
                        # with dispatch, exactly as before.
                        _notify_user(
                            plan_name,
                            f"story {story_key} rebase onto "
                            f"origin/{_default_branch()} failed "
                            f"(non-conflict); dispatching anyway "
                            f"(fail open): {result['error']}",
                        )
                        logging.getLogger("pipeline").warning(
                            "rebase for resumed story %s failed "
                            "(non-conflict); dispatching anyway "
                            "(fail open): %s",
                            story_key, result["error"],
                        )
        except Exception:  # observability hook, never a gate
            logging.getLogger("pipeline").warning(
                "staleness check for resumed story %s failed; "
                "dispatching anyway (fail open)", story_key,
                exc_info=True,
            )
    return None
