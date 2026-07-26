"""PR open / merge helpers for the pipeline MCP server.

_open_pr pushes the story's branch and opens a PR via the gh CLI. _merge_pr
squash-merges the PR and cleans up the worktree/branches. Both are patched
via p.<name> by tests; server call sites use bare names -> re-export ->
patch lands.

_merge_decision stays in pipeline_mcp_server.py because it reads
PIPELINE_AUTONOMY / PIPELINE_RISK_THRESHOLD / _RISK_ORDER as free variables
that tests patch via p.<name> across many functions (advance_pipeline_locked
included); moving it would require migrating those patches too.
"""

import subprocess
from typing import Any


def _open_pr(worktree: str, story_key: str, story: dict[str, Any]) -> str:
    """Push the story's branch and open a PR for it via the gh CLI.

    External boundary: spawns `git`/`gh`. Tests mock this function (or
    subprocess.run) rather than hitting a real remote.
    """
    branch = f"agent/{story_key.lower()}"
    title = f"{story_key}: {story['summary']}"
    body = story.get("pr_body") or (
        f"Automated PR for {story_key} produced by the agent pipeline."
    )

    subprocess.run(
        ["git", "push", "-u", "origin", branch],
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
    branch = f"agent/{story_key.lower()}"
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


__all__ = [
    "_merge_pr",
    "_open_pr",
]