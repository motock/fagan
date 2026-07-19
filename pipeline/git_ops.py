"""Git worktree helpers for the pipeline MCP server.

Three pure leaf functions used by check_story_status / dispatch_story /
interrupt_story / checkpoint: classify an agent.log by its last non-empty
line, WIP-commit uncommitted work in a worktree (excluding agent.log), and
detect whether the agent branch has commits not on the base branch.

No module-level state, no free-variable reads of server globals. Tests patch
p._worktree_has_new_commits / p._commit_wip directly; server call sites use
the bare names, which resolve through the re-export in
pipeline_mcp_server.py to the patched attribute (Option A in
PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).
"""

import subprocess
from pathlib import Path


def _last_nonempty_line(path: Path) -> str:
    """Return the last stripped-non-empty line of `path`, or "" if the file
    has no non-empty lines (or doesn't exist — caller should check).

    Used by check_story_status to classify the agent's terminal exit by the
    tail of agent.log. We must NOT substring-match the whole file: a resumed
    agent appends to the log, so an earlier step-cap marker from a prior
    tick may still be present when the resumed run completes successfully.
    Only the final terminal line classifies the current run.

    Iterates line by line so we don't materialize a multi-MB log into memory
    just to grab the last line; the file is read in binary mode and decoded
    per-line so a partial trailing line (no newline) is still considered."""
    last = ""
    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                last = line
    return last


def _commit_wip(worktree: str, story_key: str, step: str) -> str:
    """Commit any uncommitted work in the worktree as a WIP checkpoint.

    External boundary: spawns `git`. Tests mock subprocess.run. If there is
    nothing to commit (the agent already committed its own work), this is
    not an error — the existing HEAD sha is returned so the journal still
    records a checkpoint marker.

    Excludes agent.log: it's the dispatcher's own session-narration file
    written into the worktree root, not project code, and must never be
    swept into a commit. We stage everything, then unstage agent.log, rather
    than naming it in an exclude pathspec (`:!agent.log`): if the worktree has
    agent.log locally git-ignored (.git/info/exclude or .gitignore, e.g. a
    reviewer keeping it out of diffs), naming it in the pathspec makes `git
    add` reject the whole add ("paths are ignored... use -f", exit 1), which
    would lose the checkpoint. `git add -A` with no pathspec silently skips
    ignored files, and the unstage is a no-op when agent.log is absent or
    ignored.
    """
    subprocess.run(["git", "add", "-A"], cwd=worktree,
                    check=True, capture_output=True, text=True)
    subprocess.run(["git", "reset", "-q", "--", "agent.log"], cwd=worktree,
                    check=False, capture_output=True, text=True)
    commit = subprocess.run(
        ["git", "commit", "-m", f"wip({story_key}): {step}"],
        cwd=worktree, capture_output=True, text=True,
    )
    output = commit.stdout + commit.stderr
    nothing_to_commit = "nothing to commit" in output or "nothing added to commit" in output
    if commit.returncode != 0 and not nothing_to_commit:
        raise RuntimeError(f"git commit failed: {commit.stderr}")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _worktree_has_new_commits(worktree: Path, story_key: str, base_branch: str) -> bool:
    """True iff the agent branch has any commits not on base_branch.

    `git log <base>..HEAD --oneline` lists commits reachable from HEAD
    that aren't reachable from <base>. For an empty branch (agent
    parked without writing code), this list is empty even though the
    test command would pass against main's untouched suite. That's the
    false-positive trap this guards against in check_story_status.

    Returns False on any git error — a broken worktree is the
    orchestrator's problem to surface elsewhere; we'd rather mark a
    real attempt failed than let a transient git hiccup silently
    re-dispatch. The branch name follows the same convention as the
    rest of the orchestrator (line 521 et seq.).
    """
    branch = f"agent/{story_key.lower()}"
    r = subprocess.run(
        ["git", "log", f"{base_branch}..{branch}", "--oneline"],
        cwd=str(worktree), capture_output=True, text=True,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


__all__ = [
    "_last_nonempty_line",
    "_commit_wip",
    "_worktree_has_new_commits",
]