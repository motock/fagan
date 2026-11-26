"""Worktree preparation phase carved out of ``pipeline.dispatch``.

Holds the ``if not resuming:`` block of ``_dispatch_story_impl``: a fresh
dispatch refuses a taken remote branch and creates the worktree, while a
resumed dispatch runs the fail-open staleness check and rebases a worktree
whose base predates ``origin/<default>``.

The caller holds ``_plan_lock``; nothing here acquires or releases it. Names
tests patch on ``pipeline.dispatch`` are reached through that module at call
time so those patches still land.
"""

import logging
from pathlib import Path
from typing import Any


def prepare_worktree(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    branch: str,
    worktree_path: Path,
    *,
    resuming: bool,
) -> dict[str, Any] | None:
    """Create or refresh the story worktree; return an early-exit result or None."""
    from . import dispatch as _home

    if not resuming:
        block = _home._refuse_if_remote_branch_taken(
            plan_name, story_key, story, branch, manifest
        )
        if block is not None:
            return block
        story.pop("dispatch_block_reason", None)
        return _home._create_fresh_worktree(
            plan_name, branch, worktree_path, story_key=story_key
        )
    # Resumed dispatch (interrupted / changes_requested / existing
    # worktree): the worktree was branched from origin/<default> at
    # some prior point. If origin's default branch has moved since
    # then (e.g. an urgent fix landed via a separate PR between the
    # original dispatch and this resume), the resumed agent's diff
    # would be based on a stale base and could spuriously revert that
    # fix and delete its regression tests - see retro
    # mcp-self-mod-notice_2026-07-31 (PR #213 incident). Detect that
    # here and notify. Fail open: any git error (e.g. a network issue on
    # the fetch) is logged and swallowed so dispatch still proceeds -
    # this is an observability hook, never a gate. Stateless across
    # calls: no flag/cache is set, each resumed dispatch re-fetches
    # and re-checks independently. Only runs for a real git worktree
    # (a `.git` file/dir at the worktree root); a plain directory is
    # not a git worktree and the rev-list probe cannot run there.
    if (worktree_path / ".git").exists():
        return _check_resumed_staleness(
            plan_name, story_key, story, manifest, branch, worktree_path
        )
    return None


def _check_resumed_staleness(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    branch: str,
    worktree_path: Path,
) -> dict[str, Any] | None:
    from . import dispatch as _home

    try:
        with _home._scoped_repo_root(plan_name) as repo_root:
            with _home._try_acquire_git_lock(repo_root) as acquired:
                if acquired:
                    _home.subprocess.run(
                        ["git", "fetch", "origin", _home._default_branch()],
                        cwd=repo_root, check=True, **_home.forge_auth.env_kw(),
                    )
            count_out = _home.subprocess.run(
                [
                    "git",
                    "rev-list",
                    "--count",
                    f"{branch}..origin/{_home._default_branch()}",
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            behind = int(count_out.stdout.strip() or "0")
            if behind > 0:
                return _handle_stale_base(
                    plan_name, story_key, story, manifest, branch, worktree_path, behind
                )
    except Exception:  # observability hook, never a gate
        logging.getLogger("pipeline").warning(
            "staleness check for resumed story %s failed; "
            "dispatching anyway (fail open)", story_key,
            exc_info=True,
        )
    return None


def _handle_stale_base(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    branch: str,
    worktree_path: Path,
    behind: int,
) -> dict[str, Any] | None:
    """Rebase a worktree whose base predates origin/<default>.

    The worktree base predates origin/<default>; a resumed agent would
    otherwise run on a stale base that could revert work merged since the
    worktree was created (live 2026-08-17). Actually rebase the worktree onto
    origin/<default> BEFORE the agent starts. Fail-secure on a rebase conflict
    (park, never run on the stale base); fail-open on any other git/infra
    failure (proceed, today's behavior). _rebase_onto_master never raises.
    """
    from . import dispatch as _home

    result = _home._rebase_onto_master(worktree_path, branch)
    if result["ok"]:
        _home._notify_user(
            plan_name,
            f"story {story_key} worktree base predates "
            f"origin/{_home._default_branch()} by {behind} "
            f"commit(s); rebased onto "
            f"origin/{_home._default_branch()} before resume.",
        )
        logging.getLogger("pipeline").info(
            "story %s worktree base predated "
            "origin/%s by %s commit(s); rebased onto "
            "origin/%s before resume",
            story_key, _home._default_branch(), behind,
            _home._default_branch(),
        )
        _sync = _home._sync_branch_remote(worktree_path, branch)
        if not _sync.get("ok"):
            logging.getLogger("pipeline").warning(
                "remote sync for resumed story %s "
                "failed; dispatching anyway (fail "
                "open): %s",
                story_key, _sync.get("error"),
            )
    elif result["conflict"] and not _home.route_resumed_conflict(
        story, result["error"], _home._default_branch()
    ):
        # Fail-secure: never dispatch an agent on a
        # base that would revert merged work. Park the
        # story for human resolution; a later resume
        # can retry the rebase and proceed if it now
        # succeeds (parked is a retryable state).
        story["status"] = "parked"
        story["parked_reason"] = (
            f"rebase conflict: {result['error']}; "
            f"worktree still behind "
            f"origin/{_home._default_branch()}"
        )
        _home.bound_records.put_manifest(_home._store, plan_name, manifest)
        _home._notify_user(
            plan_name,
            f"story {story_key} parked: rebase conflict "
            f"against origin/{_home._default_branch()} - "
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
            story_key, _home._default_branch(),
        )
        return {
            "status": "parked",
            "reason": "rebase_conflict",
            "parked_reason": story["parked_reason"],
        }
    elif result["conflict"]:
        # Handed to the agent, which runs the rebase itself.
        _home._notify_user(
            plan_name,
            _home.conflict_handoff_notice(story_key, _home._default_branch()),
            event="story_conflict_handed_to_agent", story_key=story_key,
        )
    else:
        # Fail open on a non-conflict git/infra failure
        # (e.g. git fetch timeout): notify and proceed
        # with dispatch, exactly as before.
        _home._notify_user(
            plan_name,
            f"story {story_key} rebase onto "
            f"origin/{_home._default_branch()} failed "
            f"(non-conflict); dispatching anyway "
            f"(fail open): {result['error']}",
        )
        logging.getLogger("pipeline").warning(
            "rebase for resumed story %s failed "
            "(non-conflict); dispatching anyway "
            "(fail open): %s",
            story_key, result["error"],
        )
    return None
