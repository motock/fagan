"""Rebase-before-merge and conflict auto-resolution.

_rebase_onto_master rebases a story branch onto current origin/master inside
its worktree so the merge gate sees the branch against current master.
_try_auto_resolve_conflict attempts a narrow, fail-closed additive-import
auto-resolution for pure add/add import conflicts.

Both are patched via p.<name> by tests; server call sites use bare names ->
re-export -> patch lands. _rebase_onto_master reads REPO_ROOT and
_default_branch via lazy imports from the server (circular-avoidance).
"""

import os
import subprocess
from pathlib import Path
from typing import Any

from pipeline_parsers import (
    _parse_conflict_blocks,
    _resolve_conflict_blocks,
    _git_show_stage,
    _is_pure_additive_import_diff,
)


def _try_auto_resolve_conflict(worktree: str) -> list[str]:
    """Attempt the narrow, fail-closed additive-import auto-resolution.

    Eligible only if EVERY conflicted file's whole-file diff from its merge
    base, on BOTH the "ours" (rebase target) and "theirs" (incoming commit)
    side, is pure-insertion-only and every inserted line is a conservative
    import/use statement - i.e. a genuine add/add conflict, never a case
    where either side deleted or modified a pre-existing line. One
    disqualifying file anywhere disqualifies the whole rebase step (no
    partial per-file resolution).

    On success, every eligible file's working-tree content is rewritten with
    its conflict markers replaced by the union of both sides' added lines,
    and the list of resolved filenames is returned (still needs `git add`).
    Returns an empty list if not eligible - the working tree is left
    untouched so the caller's abort path is unaffected."""
    try:
        diff = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"],
                              cwd=worktree, capture_output=True, text=True)
    except OSError:
        return []
    if diff.returncode != 0:
        return []
    conflicted = [f for f in diff.stdout.splitlines() if f.strip()]
    if not conflicted:
        return []

    resolutions: dict[str, str] = {}
    for fname in conflicted:
        try:
            text = (Path(worktree) / fname).read_text()
        except (OSError, UnicodeDecodeError):
            return []  # unreadable/binary - disqualify the whole step

        blocks = _parse_conflict_blocks(text)
        if blocks is None:
            return []  # no/malformed markers - can't verify, disqualify

        base = _git_show_stage(worktree, 1, fname)
        ours = _git_show_stage(worktree, 2, fname)
        theirs = _git_show_stage(worktree, 3, fname)
        if base is None or ours is None or theirs is None:
            return []  # rename/delete conflict (missing a stage) - disqualify

        if not _is_pure_additive_import_diff(base, ours):
            return []
        if not _is_pure_additive_import_diff(base, theirs):
            return []

        resolutions[fname] = _resolve_conflict_blocks(text, blocks)

    # Wrap the write loop in try/except so a write failure (ENOSPC, EROFS,
    # quota, etc.) disqualifies the whole step instead of propagating and
    # leaving the worktree mid-rebase. Matches the read-side handling above
    # and honors _rebase_onto_master's never-raises contract.
    try:
        for fname, resolved_text in resolutions.items():
            (Path(worktree) / fname).write_text(resolved_text)
    except (OSError, UnicodeDecodeError):
        return []
    return list(resolutions.keys())


def _rebase_onto_master(worktree: str, branch: str) -> dict[str, Any]:
    """Rebase `branch` onto current origin/master inside its worktree so the
    merge gate sees the branch against current master, not the stale base the
    agent branched from. Fetches origin/master first (from REPO_ROOT, the shared
    repo) so the rebase target is current.

    Returns ``{"ok": bool, "conflict": bool, "error": str}``, plus
    ``"auto_resolved": True`` when a conflict was narrowly auto-resolved (see
    below) instead of aborted:
      - ok=True            rebase succeeded; the branch is on top of origin/master.
      - ok=True, auto_resolved=True  the rebase hit a conflict, but every
        conflicted file was a pure add/add of import/use statements (never a
        deletion or modification of an existing line) - both sides' added
        lines were unioned and the rebase continued. Fail-closed: any doubt
        anywhere (a modified/deleted line, a non-import addition, a
        rename/delete conflict, one disqualifying file among several) falls
        straight through to the ordinary abort path below - there is no
        partial per-file resolution.
      - ok=False, conflict=True  rebase hit a merge conflict that either
        wasn't a pure additive-import case or couldn't be safely verified as
        one; the rebase was aborted so the worktree is back to its pre-rebase
        state and the caller can park/re-dispatch for human resolution.
      - ok=False, conflict=False some other git failure (dirty tree, missing
        ref); rebase aborted if one was in progress.
    """
    def _run(argv: list[str], cwd, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        # `git` may be absent or non-executable (e.g. a minimal container).
        # Catch OSError so this helper honors its never-raises contract and
        # reports a non-conflict failure instead of crashing the tick.
        try:
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env)
        except OSError as e:
            return subprocess.CompletedProcess(argv, 127, "", str(e))

    if not Path(worktree).is_dir():
        # No worktree to rebase in (missing/anomalous). The merge gate falls
        # back to the CI gate + the original conflict-at-`gh pr merge` check;
        # rebasing is impossible without the worktree the branch lives in.
        return {"ok": True, "conflict": False, "error": "worktree missing - rebase skipped"}
    # Lazy imports: REPO_ROOT and _default_branch are server module-level
    # globals patched by tests via p.<name>; reading them here at call time
    # sees the patched value. The server imports this module at top level, so
    # a module-load import would cycle.
    from pipeline_mcp_server import REPO_ROOT, _default_branch
    # Full `git fetch origin` (not `fetch origin <branch>`) so every
    # remote-tracking ref is updated on configs with a narrow/custom refspec,
    # keeping the rebase target current. The rebase target itself must follow
    # the repo's default branch (`main` on fresh `gh repo create`, `master` on
    # legacy / local bench clones); hardcoding `origin/master` would break
    # every merge-gate attempt on a main-default repo (live-`gh` probe,
    # PROOF.md note #1).
    _run(["git", "fetch", "origin"], REPO_ROOT)
    r = _run(["git", "rebase", f"origin/{_default_branch()}"], worktree)
    if r.returncode == 0:
        return {"ok": True, "conflict": False, "error": ""}
    blob = (r.stdout + "\n" + r.stderr).lower()
    conflict = "fix conflicts" in blob or "could not apply" in blob or "conflict" in blob

    if conflict:
        resolved_files = _try_auto_resolve_conflict(worktree)
        if resolved_files:
            add_ok = True
            for fname in resolved_files:
                if _run(["git", "add", fname], worktree).returncode != 0:
                    add_ok = False
                    break
            if add_ok:
                # GIT_EDITOR=true: --continue reuses the original commit
                # message by default, but pin a no-op editor defensively so
                # this can never block on an interactive prompt.
                env = dict(os.environ, GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true")
                cont = _run(["git", "rebase", "--continue"], worktree, env=env)
                if cont.returncode == 0:
                    return {"ok": True, "conflict": False, "auto_resolved": True, "error": ""}
        # Resolution or --continue failed (e.g. a second conflicting
        # commit further down the rebase) - never attempt recursively;
        # fall through to the ordinary abort below.

    # Abort so we never leave the worktree mid-rebase (a half-rebased tree would
    # break the next dispatch into it). Best-effort: --abort is a no-op if no
    # rebase is in progress.
    _run(["git", "rebase", "--abort"], worktree)
    return {"ok": False, "conflict": conflict,
            "error": (r.stdout + r.stderr).strip()[:500]}


__all__ = [
    "_try_auto_resolve_conflict",
    "_rebase_onto_master",
]