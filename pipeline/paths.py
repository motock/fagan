"""Path constants for the pipeline MCP server.

These are read once at import time from env vars and re-imported into
pipeline_mcp_server.py, which is the surface tests patch (p.PLAN_DIR = ...).
Server functions read them as free variables, which resolve to the
pipeline_mcp_server module globals (the re-exported, patchable bindings) -
so moving only the *definitions* here is safe; no function that reads them
as free variables moves with them.

Functions that read REPO_ROOT as a free variable (_default_branch,
_repo_root_for, _scoped_repo_root) stay in pipeline_mcp_server.py for the
same reason: tests monkeypatch p.REPO_ROOT and expect those functions to
see the patched value, which only works while they live in the same module
as the patched binding.

_WORKTREE_LOG_EXCLUDES + _exclude_worktree_logs_from_tracking move here
because tests don't patch them and they're a pure leaf helper.
"""

import os
from pathlib import Path

PLAN_DIR = Path(os.environ.get("PLAN_DIR", "~/.claude/plans")).expanduser()
WORKTREE_ROOT = Path(os.environ.get("WORKTREE_ROOT", "~/.claude/worktrees")).expanduser()
AGENTS_DIR = Path(os.environ.get("AGENTS_DIR", "~/.claude/agents")).expanduser()
POLICY_PATH = Path(os.environ.get("OVERLORD_POLICY", "~/.claude/overlord-policy.md")).expanduser()
USAGE_STATE_PATH = Path(os.environ.get("USAGE_STATE_PATH", "~/.claude/usage_state.json")).expanduser()


# Observability artifacts a dispatched/reviewed agent writes into its own
# worktree (agent.log, review.log) but must NEVER be trackable by git. Mode
# 17: review.log starts untracked (harmless), but a rework cycle's auto
# WIP-commit (`git add -A`) tracks it if the story gets REQUEST_CHANGES;
# the next review cycle's append then makes it a modified tracked file, and
# the pre-merge rebase (Mode 9's gate) refuses on "unstaged changes" -
# failing an already-APPROVED, ground-truth-correct story 3 retries running.
# .git/info/exclude is shared across every worktree of a repo (verified:
# `git rev-parse --git-path info/exclude` from inside a worktree resolves to
# the MAIN repo's .git/info/exclude, not a per-worktree file), so writing it
# once per repo, idempotently, covers every past and future worktree.
#
# .agent_plan.md/.agent_scratchpad.md (GUIDED_DECOMPOSITION_PLAN.md) are the
# same kind of untracked runtime artifact as agent.log/review.log - written
# into the worktree outside of any commit, and vulnerable to the identical
# Mode 17 failure (a rework's `git add -A` WIP-commit would track them,
# dirtying the tree ahead of the pre-merge rebase) if not excluded up front.
#
# .agent_plan_src_hash (the checklist-reuse guard's companion hash, see
# commit 9a45803) is the same class of artifact and hit the same failure live
# (2026-08-13): it was never excluded, so a WIP commit's `git add -A` tracked
# it, and the pre-merge rebase then failed on an add/add conflict against the
# copy that had leaked onto the default branch - terminal-failing an
# otherwise-green story's merge gate (P3-6, 3/3 attempts exhausted).
_WORKTREE_LOG_EXCLUDES = (
    # "*.log.ts" (not just "agent.log.ts"): spawn_local writes a timestamp
    # sidecar for EVERY log it opens - agent.log, review.log, test_author.log,
    # rework_test_author.log and grading.log - and the repo .gitignore's
    # "*.log" does NOT match "foo.log.ts". Naming only agent.log.ts left the
    # other four untracked-and-unignored, so _commit_wip's `git add -A` swept
    # them into story commits (a stray test_author.log.ts reached
    # agent/chatreload-1 on 2026-09-11).
    "agent.log", "*.log.ts", "*.log.raw", "review.log", ".agent_plan.md",
    ".agent_scratchpad.md", ".agent_scratchpad*.md", "*agent_scratchpad*.md", ".agent_plan_src_hash",
)


def _exclude_worktree_logs_from_tracking(repo_root: Path) -> None:
    """Best-effort: append _WORKTREE_LOG_EXCLUDES to repo_root/.git/info/exclude
    if not already present. Never raises - this is a hygiene fix, not a
    correctness requirement, and must not break dispatch if the repo's .git
    layout is unexpected (e.g. a submodule, or repo_root not actually a git
    repo yet in some caller)."""
    try:
        info_dir = repo_root / ".git" / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude_path = info_dir / "exclude"
        existing = exclude_path.read_text() if exclude_path.exists() else ""
        missing = [name for name in _WORKTREE_LOG_EXCLUDES if name not in existing]
        if missing:
            with exclude_path.open("a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                for name in missing:
                    f.write(f"{name}\n")
    except OSError:
        pass


__all__ = [
    "AGENTS_DIR",
    "PLAN_DIR",
    "POLICY_PATH",
    "USAGE_STATE_PATH",
    "WORKTREE_ROOT",
    "_WORKTREE_LOG_EXCLUDES",
    "_exclude_worktree_logs_from_tracking",
]