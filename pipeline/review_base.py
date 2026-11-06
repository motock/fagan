"""The base commit a first (non-rework) review diffs against.

`_first_review_base` in review.py used to return the *first* candidate trunk
ref that had a merge-base with HEAD. In a long-lived clone the remote-tracking
refs go stale (nothing re-fetches between stories), so `origin/main` can sit
several stories behind the branch's real fork point: the first-review diff
then carries unrelated history and the reviewer grades work that isn't in the
story.

The fix is two-part: refresh `origin` best-effort, then pick the candidate with
the fewest commits between it and HEAD - the closest trunk, i.e. the truest
fork point - instead of trusting candidate order.

Anti-patterns this module deliberately avoids:

* returning the first candidate with *a* merge-base instead of the minimum
  `rev-list --count <base>..HEAD` (that is the stale-ref bug itself);
* fetching inside the candidate loop (one slow/blocked remote would be retried
  per candidate, and later candidates would be scored against different refs
  than earlier ones);
* running `rev-list --count` on a ref *name* that may not exist (rev-list fails
  on an unknown ref, so each candidate is first pinned to a commit SHA).

No module-level "already fetched" flag or cache: the pipeline server is
long-lived, so a process-wide flag would let one unreachable-remote call pin
every later base to whatever stale ref happened to be on disk at the time.
Every call fetches again.
"""

import logging
import subprocess

log = logging.getLogger(__name__)

# Candidate default-branch refs, most specific first. Order only breaks ties
# between candidates that are equally close to HEAD; the minimum count wins.
_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master", "main", "master")


def _fetch_origin(worktree: str, timeout: int) -> None:
    """Best-effort `git fetch --quiet origin`; failure is logged, never raised.

    The warning carries only the exit code or the timeout - never git's stderr
    and never the command line. Both can contain the remote URL, and a CI
    clone's URL embeds an access token.
    """
    try:
        result = subprocess.run(
            ["git", "fetch", "--quiet", "origin"],
            cwd=worktree, check=False,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning(
            "Review base: git fetch origin timed out after %ss; "
            "falling back to local refs", timeout,
        )
        return
    except OSError as exc:
        log.warning(
            "Review base: git fetch origin could not run (%s); "
            "falling back to local refs", type(exc).__name__,
        )
        return
    if result.returncode != 0:
        log.warning(
            "Review base: git fetch origin exited %s; "
            "falling back to local refs", result.returncode,
        )


def _git(worktree: str, args: list[str], timeout: int = 15) -> str | None:
    """Stdout of a read-only git command, or None if it failed for any reason."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=worktree, check=False,
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _candidate_sha(worktree: str, candidate: str) -> str | None:
    """Full commit SHA `candidate` points at, or None if it doesn't resolve."""
    out = _git(worktree, ["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"])
    if not out or not out.strip():
        return None
    return out.strip()


def _commits_ahead(worktree: str, sha: str) -> int | None:
    """`git rev-list --count <sha>..HEAD`, or None if git can't count it."""
    out = _git(worktree, ["rev-list", "--count", f"{sha}..HEAD"])
    if not out:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def resolve_review_base(
    worktree: str, fetch: bool = True, timeout: int = 30,
) -> str | None:
    """The commit a first review's `<base>..HEAD` diff should start from.

    Fetches `origin` once (best effort, see `_fetch_origin`), then returns the
    candidate trunk commit with the fewest commits between it and HEAD. Ties
    keep `_CANDIDATES` order. Returns None when no candidate resolves, so
    callers fall back to letting the reviewer discover the diff itself.
    """
    if fetch:
        _fetch_origin(worktree, timeout)

    best_sha = None
    best_count = None
    for candidate in _CANDIDATES:
        sha = _candidate_sha(worktree, candidate)
        if sha is None:
            continue
        count = _commits_ahead(worktree, sha)
        if count is None:
            continue
        # Strict `<`: an equally close later candidate never displaces an
        # earlier one, so the most specific trunk wins the tie.
        if best_count is None or count < best_count:
            best_sha, best_count = sha, count
    return best_sha
