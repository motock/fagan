"""Fresh-worktree setup carved out of ``pipeline.dispatch``.

Holds the git setup (fetch + ``git worktree add``) and venv provisioning for a
story's first dispatch. Every free name it reads that is a module-level binding
of ``pipeline.dispatch`` is resolved lazily through ``_ModuleRef``, so
``monkeypatch.setattr(pipeline.dispatch, "NAME", ...)`` still lands.
"""

import logging
import subprocess
from pathlib import Path
from typing import Any

from .audit import record_audit
from .branch_guard import BranchGuardError, refusal
from .module_ref import _ModuleRef

_scoped_repo_root = _ModuleRef("pipeline.dispatch", "_scoped_repo_root")
_try_acquire_git_lock = _ModuleRef("pipeline.dispatch", "_try_acquire_git_lock")
_atomic_write_json = _ModuleRef("pipeline.dispatch", "_atomic_write_json")
_notify_user = _ModuleRef("pipeline.dispatch", "_notify_user")
_remote_branch_exists = _ModuleRef("pipeline.dispatch", "remote_branch_exists")
_default_branch = _ModuleRef("pipeline.dispatch", "_default_branch")
_exclude_worktree_logs_from_tracking = _ModuleRef(
    "pipeline.dispatch", "_exclude_worktree_logs_from_tracking"
)
_provision_worktree_venv = _ModuleRef(
    "pipeline.dispatch", "_provision_worktree_venv"
)

#: Bound on each freshness probe. Both probes are read-only and local or
#: single-round-trip, so a reply slower than this is a network condition, not
#: a slow git - treat it as "unknown" rather than hold dispatch open.
_PROBE_TIMEOUT_S = 30


def _remote_tip(repo_root: Path, default_branch: str) -> str | None:
    """Return the remote's current tip for ``default_branch``, or ``None``.

    Reads the remote with ``git ls-remote`` instead of fetching: it consults
    the remote without writing any local ref, so it needs neither the advisory
    git lock nor a working tree. ``None`` means the question could not be
    answered (network failure, no origin, no such remote branch) and must be
    read as "unknown", never as "fresh".
    """
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "origin", f"refs/heads/{default_branch}"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    fields = completed.stdout.split()
    return fields[0] if fields else None


def _cached_remote_tip(repo_root: Path, default_branch: str) -> str | None:
    """Return this checkout's cached ``origin/<default>`` tip, or ``None``."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/remotes/origin/{default_branch}"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return completed.stdout.strip() or None


def _stale_base_error(
    repo_root: Path, default_branch: str, branch: str
) -> str | None:
    """Return a refusal message when the base is *provably* stale.

    Only a provable mismatch refuses: the remote tip was read successfully and
    differs from the cached ``origin/<default>`` this worktree would be cut
    from. An unanswerable probe (outage, no origin, missing branch) returns
    ``None`` so dispatch keeps the degrade-and-warn behaviour - refusing on an
    outage would block a dispatch that has done nothing wrong.
    """
    remote = _remote_tip(repo_root, default_branch)
    cached = _cached_remote_tip(repo_root, default_branch)
    if remote is None or cached is None or remote == cached:
        return None
    return (
        f"{branch}: refusing to dispatch from a provably stale base - the "
        f"cached origin/{default_branch} is {cached[:12]} but the remote is "
        f"{remote[:12]}. Another process held the git lock, so the fetch was "
        "skipped; the next scheduler tick retries with the lock free."
    )


def _worktree_base_sha(worktree_path: Path) -> str | None:
    """Return the commit the new worktree was cut from, or ``None``.

    Read after ``git worktree add`` succeeds: the branch was created at
    ``origin/<default>``, so the worktree's first commit IS the base the agent
    will build on. An unreadable value is recorded as ``None`` rather than
    failing a dispatch whose worktree already exists.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree_path,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    # The dispatch tests substitute subprocess.run with a recorder that returns
    # None, and this value is advisory by contract: an unreadable base is
    # recorded as unknown, never turned into a failure of a dispatch whose
    # worktree already exists.
    output = getattr(completed, "stdout", "") or ""
    return output.strip() or None


def _create_fresh_worktree(
    plan_name: str, branch: str, worktree_path: Path, *, story_key: str | None = None
) -> dict[str, Any] | None:
    """Create the story branch's worktree from ``origin/<default>``.

    Runs the fresh-dispatch git setup (fetch the default branch, then
    ``git worktree add -b <branch>``) and provisions the new worktree's own
    virtualenv. Returns the structured ``{"ok": False, "error": ...}`` dict when
    git setup fails (never raises ``CalledProcessError``), otherwise ``None``.
    """
    try:
        with _scoped_repo_root(plan_name) as repo_root:
            if not Path(repo_root).is_dir():
                # repo_root is recorded at ingest and then trusted forever, so
                # it can be gone by dispatch time (a plan ingested against a
                # temp directory, a moved checkout). Every git command below
                # runs with it as its cwd, where subprocess raises
                # FileNotFoundError - not the CalledProcessError this function
                # handles - so fail closed here, before running any git at all.
                return {
                    "ok": False,
                    "error": (
                        f"repo_root {str(repo_root)!r} does not exist - refusing "
                        "to set up a worktree for a missing repository"
                    ),
                }
            default_branch = _default_branch()
            fetch_outcome = "stale_skipped"
            with _try_acquire_git_lock(repo_root) as acquired:
                if acquired:
                    fetch_outcome = "fetched"
                    subprocess.run(
                        ["git", "fetch", "origin", default_branch],
                        cwd=repo_root,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                else:
                    # The fetch is deliberately best-effort (see
                    # tests/unit/test_dispatch_git_fetch_lock.py), but a
                    # silent skip is how a story ends up branched from an
                    # origin tip that predates its own dependency: AUTH-03
                    # (2026-10-27) branched 32 minutes stale and re-added a
                    # module that had already merged. Record it instead.
                    _notify_user(
                        plan_name,
                        f"{branch}: dispatched from a possibly stale "
                        f"origin/{default_branch} - the git lock was held, so "
                        "the fetch was skipped",
                        story_key=None,
                        severity="warning",
                        event="stale_base_dispatch",
                    )
                    stale = _stale_base_error(repo_root, default_branch, branch)
                    if stale is not None:
                        _notify_user(
                            plan_name,
                            stale,
                            story_key=None,
                            severity="warning",
                            event="stale_base_refused",
                        )
                        record_audit(
                            plan_name,
                            action="story.dispatch",
                            outcome="refused",
                            target={"plan": plan_name, "story": story_key or branch},
                            detail={"branch": branch, "reason": "stale_base"},
                        )
                        return {"ok": False, "error": stale}
            subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree_path),
                    f"origin/{default_branch}",
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
    except subprocess.CalledProcessError as e:
        # A fresh dispatch's git setup (fetch + worktree add) can fail
        # for reasons outside the story itself - e.g. repo_root has no
        # usable 'origin' remote, or git can't authenticate. Left
        # uncaught, this used to escape all the way to an unhandled
        # 500 with an EMPTY body (no JSON), which broke every caller's
        # response.json() with "Expecting value: line 1 column 1 (char
        # 0)" - reproduced live twice via chat's dispatch_story tool
        # call. Return a structured, actionable failure instead.
        stderr = (e.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        return {
            "ok": False,
            "error": (
                f"git setup failed for repo_root {str(repo_root)!r} "
                f"(command {' '.join(e.cmd)!r}, exit {e.returncode})"
                f"{detail}"
            ),
        }
    record_audit(
        plan_name,
        action="story.dispatch",
        outcome="allowed",
        target={"plan": plan_name, "story": story_key or branch},
        detail={
            "branch": branch,
            "base_sha": _worktree_base_sha(worktree_path),
            "fetch_outcome": fetch_outcome,
        },
    )
    _exclude_worktree_logs_from_tracking(Path(repo_root))
    # A fresh worktree has no .venv (gitignored) - give it its own
    # complete one now rather than let it fall back to (and
    # potentially mutate) the shared main-repo venv other
    # concurrently-dispatched stories may be using. See
    # _provision_worktree_venv's docstring for the failure mode
    # this closes (root-caused live on RUFF-016-ADOPTION).
    # No-ops for non-Python projects or ones without a
    # requirements file.
    _provision_worktree_venv(worktree_path)
    return None


# Kept here, not in dispatch.py, to hold dispatch.py under its 1000-line limit.
def _refuse_if_remote_branch_taken(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    branch: str,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """Fail-closed guard for a FRESH dispatch: block when origin already holds
    ``branch`` or cannot be queried (a second push would overwrite another
    machine's work). Returns the refusal result, or None to proceed. The story
    stays ``todo`` with the reason recorded on it."""
    try:
        with _scoped_repo_root(plan_name) as repo_root:
            if not _remote_branch_exists(repo_root, branch):
                return None
        outcome, reason = refusal(story_key, branch)
    except BranchGuardError as exc:
        outcome, reason = refusal(story_key, branch, exc)
    story["dispatch_block_reason"] = reason
    _atomic_write_json(manifest_path, manifest)
    logging.getLogger("pipeline").warning(
        "fresh dispatch blocked by remote branch guard",
        extra={"plan": plan_name, "story_key": story_key, "branch": branch,
               "outcome": outcome},
    )
    _notify_user(
        plan_name,
        reason,
        story_key=story_key,
        severity="warning",
        event=outcome,
        dedup_key=f"{outcome}:{story_key}",
    )
    return {"ok": False, "error": reason, "story_key": story_key,
            "branch": branch, "outcome": outcome}
