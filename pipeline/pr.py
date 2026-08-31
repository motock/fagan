"""PR open / merge helpers for the pipeline MCP server.

_open_pr pushes the story's branch and opens a PR via the gh CLI. _merge_pr
squash-merges the PR and cleans up the worktree/branches. Both are patched
via p.<name> by tests; server call sites use bare names -> re-export ->
patch lands.

_merge_decision stays in pipeline_mcp_server.py because it reads
PIPELINE_AUTONOMY / PIPELINE_RISK_THRESHOLD / _RISK_ORDER as free variables
that tests patch via p.<name> across many functions (advance_pipeline_locked
included); moving it would require migrating those patches too.

_format_review_comment and _post_pr_comment provide helpers for automated
review comments via the gh CLI.
"""

import subprocess
from typing import Any


def _resolve_story_branch(worktree: str, story_key: str) -> str:
    """Return the branch _open_pr/_merge_pr must operate on for this story.

    Normally that is the convention branch agent/<key>, but a rework round
    can leave the worktree checked out on an alias branch named
    agent/<key>-<suffix> (e.g. agent/la-verify-followup). Pushing/merging the
    convention name then targets a stale, already-merged branch and `gh pr
    create` fails with "No commits between master and agent/<key>", so the
    worktree's actual HEAD branch wins whenever it is such an alias.

    Fails open to the convention branch when the worktree cannot be probed
    (missing directory, non-repo, git absent): the merge gate calls this with
    story worktrees that may legitimately not exist yet, and a probe failure
    must degrade to the old convention-branch behaviour, never raise.
    """
    convention = f"agent/{story_key.lower()}"
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree, check=False, capture_output=True, text=True,
        )
    except OSError:
        # cwd missing/unusable (or git not executable): fail open.
        return convention
    if proc.returncode != 0:
        return convention
    head = proc.stdout.strip()
    if not head or head == "HEAD":          # detached HEAD
        return convention
    if head.startswith(convention + "-"):   # alias suffix, e.g. agent/s1-followup
        return head
    return convention


def _open_pr(worktree: str, story_key: str, story: dict[str, Any]) -> str:
    """Push the story's branch and open a PR for it via the gh CLI.

    External boundary: spawns `git`/`gh`. Tests mock this function (or
    subprocess.run) rather than hitting a real remote.
    """
    branch = _resolve_story_branch(worktree, story_key)
    title = f"{story_key}: {story['summary']}"
    body = story.get("pr_body") or (
        f"Automated PR for {story_key} produced by the agent pipeline."
    )

    # --force-with-lease: this branch is rebased onto the default branch
    # before every resumed dispatch (see _rebase_onto_master), which rewrites
    # its commit SHAs. If review_story already pushed once (e.g. an earlier
    # APPROVE opened a PR, then a later rework round rebased again), a plain
    # push is rejected as non-fast-forward on every subsequent retry - the
    # branch is exclusively owned by this pipeline's own dispatched agent, so
    # there's no external pusher to race and a force-with-lease push is safe.
    subprocess.run(
        ["git", "push", "--force-with-lease", "-u", "origin", branch],
        cwd=worktree, check=True, capture_output=True, text=True,
    )
    try:
        proc = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body, "--head", branch],
            cwd=worktree, check=True, capture_output=True, text=True,
        )
        return proc.stdout.strip()
    except subprocess.CalledProcessError as e:
        # A dispatched agent's own Bash access can include `gh pr create`,
        # so a PR may already exist by the time review_story gets here.
        # Recover its URL instead of failing the whole pipeline tick.
        if "already exists" not in (e.stderr or ""):
            raise
        proc = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
            cwd=worktree, check=True, capture_output=True, text=True,
        )
        return proc.stdout.strip()


def _merge_pr(worktree: str, story_key: str) -> str:
    """Squash-merge the story's PR, then remove its worktree and branches.

    External boundary: spawns `git`/`gh`. Tests mock this function (or
    subprocess.run) rather than hitting a real remote.

    Deliberately does not pass --delete-branch to `gh pr merge`: that asks
    gh to switch the local checkout away from the branch being deleted,
    which fails here because the branch is checked out in its own worktree
    while REPO_ROOT has another branch checked out (the normal state for
    this pipeline's one-worktree-per-story model). Branch/worktree cleanup
    is done explicitly below, from REPO_ROOT, after the merge succeeds.
    """
    # Lazy import: REPO_ROOT is a server module-level global patched by tests
    # via p.REPO_ROOT; the lazy import at call time sees the patched value.
    from .server import REPO_ROOT
    branch = _resolve_story_branch(worktree, story_key)
    proc = subprocess.run(
        ["gh", "pr", "merge", branch, "--squash"],
        cwd=worktree, check=True, capture_output=True, text=True,
    )
    result = proc.stdout.strip()

    subprocess.run(["git", "worktree", "remove", "--force", worktree],
                    check=False, cwd=REPO_ROOT, capture_output=True, text=True)
    subprocess.run(["git", "branch", "-D", branch],
                    check=False, cwd=REPO_ROOT, capture_output=True, text=True)
    subprocess.run(["git", "push", "origin", "--delete", branch],
                    check=False, cwd=REPO_ROOT, capture_output=True, text=True)

    return result


def _format_review_comment(findings: str, cycle: int) -> str:
    """Returns a markdown PR-comment body.

    ## ⚠️ Changes requested — automated review (cycle {cycle})

    {findings}
    """
    return f"## ⚠️ Changes requested — automated review (cycle {cycle})\n\n{findings}"


def _post_pr_comment(worktree: str, body: str) -> None:
    """Posts `body` as a comment on the current branch's PR via the gh CLI.

    External boundary: spawns `gh`. Tests mock this function rather than hitting a real remote.
    """
    subprocess.run(
        ["gh", "pr", "comment", "--body", body],
        cwd=worktree, check=True, capture_output=True, text=True,
    )


__all__ = [
    "_format_review_comment",
    "_merge_pr",
    "_open_pr",
    "_post_pr_comment",
]
