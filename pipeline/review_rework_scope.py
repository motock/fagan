"""Scope a rework review's diff to the story's own new commits (D-A).

`git diff {since_sha}..HEAD` is only the story's own work while the branch
still sits on an un-moved since_sha. Once master advances and the branch is
rebased (or has master merged in), that range also contains the trunk's own
changes, so the reviewer flags work this story never touched.

scoped_rework_diff returns the diff of the files THIS story changed since the
last review, taken against the merge-base with the trunk so the trunk's
changes to other files are excluded. It returns None whenever there is no
drift (today's prompt stays byte-for-byte identical) or whenever anything
goes wrong - the review must still run.
"""
from __future__ import annotations

import logging
import subprocess

from .config import REVIEWER_INLINE_DIFF_MAX_CHARS

logger = logging.getLogger(__name__)

# Same candidate order as review._first_review_base: repos differ on
# default-branch naming and on whether a remote exists at all.
_TRUNK_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master", "main", "master")
_FETCH_TIMEOUT_SECONDS = 30


def _run_git(worktree: str, args: list[str], *, check: bool = False,
             timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a git command in `worktree`. Never uses a shell; stderr is
    captured, never logged (it can carry local paths)."""
    return subprocess.run(
        ["git", *args], cwd=worktree, capture_output=True, text=True,
        check=check, timeout=timeout,
    )


def _rev_parse(worktree: str, ref: str) -> str | None:
    """The commit `ref` resolves to, or None when it does not resolve."""
    result = _run_git(
        worktree, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _rev_list_count(worktree: str, *revisions: str) -> int:
    """`git rev-list --count` for the given revision arguments; raises on a
    git error so the caller's outer handler falls back to today's diff."""
    result = _run_git(worktree, ["rev-list", "--count", *revisions])
    if result.returncode != 0:
        raise RuntimeError("git rev-list --count failed")
    return int(result.stdout.strip() or 0)


def _git_stdout(worktree: str, args: list[str]) -> str:
    result = _run_git(worktree, args)
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed")
    return result.stdout


def _resolve_trunk_ref(worktree: str, trunk_ref: str | None = None) -> str | None:
    """The local ref this branch's trunk tracks.

    Best-effort `git fetch origin` first (30 s timeout, any failure ignored
    and only its exception TYPE logged), then the first candidate that
    resolves with the SMALLEST `rev-list --count <ref>..HEAD` - the branch is
    closest to its own trunk. `trunk_ref` is reserved for the trunk-selection
    story (S2); it is accepted but must not bypass the smallest-count choice,
    which is why review.py passes None.
    """
    try:
        _run_git(
            worktree, ["fetch", "--quiet", "origin"],
            check=True, timeout=_FETCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - best effort: no remote, offline, auth, timeout
        logger.info(
            "review rework scope: origin fetch failed (%s), using local refs",
            type(exc).__name__,
        )

    best: str | None = None
    best_count: int | None = None
    for candidate in _TRUNK_CANDIDATES:
        if _rev_parse(worktree, candidate) is None:
            continue
        result = _run_git(
            worktree, ["rev-list", "--count", f"{candidate}..HEAD"],
        )
        if result.returncode != 0:
            continue
        count = int(result.stdout.strip() or 0)
        if best_count is None or count < best_count:
            best, best_count = candidate, count
    return best


def _has_drift(worktree: str, since_sha: str, trunk_ref: str) -> bool:
    """True when since_sha..HEAD has been polluted by the trunk.

    Drift is either an orphaned since_sha (a rebase rewrote its commits, so
    it is no longer an ancestor of HEAD) or trunk commits sitting inside the
    range: `rev-list --count since_sha..HEAD` then exceeds the count that
    excludes the trunk. A non-0/1 exit from --is-ancestor is a git error and
    propagates to the caller's outer handler.
    """
    result = _run_git(
        worktree, ["merge-base", "--is-ancestor", since_sha, "HEAD"],
    )
    if result.returncode == 1:
        return True
    if result.returncode != 0:
        raise RuntimeError("git merge-base --is-ancestor failed")
    full_count = _rev_list_count(worktree, f"{since_sha}..HEAD")
    own_count = _rev_list_count(worktree, f"{since_sha}..HEAD", f"^{trunk_ref}")
    return full_count != own_count


def _own_commits_and_files(
    worktree: str, since_sha: str, trunk_ref: str,
) -> tuple[list[str], list[str]]:
    """This story's own non-merge commits since since_sha, and the files they
    touched (deduped, order preserved).

    Uses the non-symmetric range `since_sha..HEAD ^trunk_ref`: `--cherry-pick`
    then drops commits the trunk already has, while keeping the branch's own
    commits - including ones a rebase rewrote (their pre-rebase originals sit
    in HEAD's history too, which is why a symmetric `since_sha...HEAD` range
    with --right-only would cancel the branch's commits and yield nothing).
    """
    revisions = [f"{since_sha}..HEAD", f"^{trunk_ref}"]
    hashes = _git_stdout(
        worktree,
        ["log", "--cherry-pick", "--no-merges", "--format=%H", *revisions],
    )
    commits = [line.strip() for line in hashes.splitlines() if line.strip()]
    if not commits:
        return [], []
    names = _git_stdout(
        worktree,
        ["log", "--cherry-pick", "--no-merges", "--name-only", "--format=",
         *revisions],
    )
    files: list[str] = []
    seen: set[str] = set()
    for line in names.splitlines():
        name = line.strip()
        if name and name not in seen:
            seen.add(name)
            files.append(name)
    return commits, files


def scoped_rework_diff(
    worktree: str, since_sha: str | None, trunk_ref: str | None = None,
) -> tuple[str, str] | None:
    """`(base_for_label, diff_text)` scoped to the story's own changes, or
    None meaning "no drift - use today's diff".

    `("", "")` means drift was detected but the story has no new files of its
    own; the caller falls back to today's prompt and merely notes the drift.
    Never raises: every git failure fails open to today's behaviour.
    """
    try:
        if not since_sha:
            return None
        trunk = _resolve_trunk_ref(worktree, trunk_ref)
        if trunk is None:
            return None
        if not _has_drift(worktree, since_sha, trunk):
            return None
        commits, files = _own_commits_and_files(worktree, since_sha, trunk)
        if not commits or not files:
            return ("", "")
        merge_base = _git_stdout(worktree, ["merge-base", "HEAD", trunk]).strip()
        if not merge_base:
            return None
        diff = _git_stdout(
            worktree, ["diff", merge_base, "HEAD", "--", *files],
        )
        if not diff or not diff.strip():
            return ("", "")
        # Never truncate: an over-budget diff falls back to today's behaviour.
        if len(diff) > REVIEWER_INLINE_DIFF_MAX_CHARS:
            return None
        return (merge_base, diff)
    except Exception as exc:  # noqa: BLE001 - the review must still run
        logger.info(
            "review rework scope: git failure (%s), using today's diff",
            type(exc).__name__,
        )
        return None
