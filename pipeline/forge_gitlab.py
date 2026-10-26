"""GitLab adapter for the :class:`pipeline.forge.Forge` protocol.

Every ``glab`` invocation goes through one runner seam,
:meth:`GitLabForge._run`, which calls ``subprocess.run``. Tests patch
``pipeline.forge_gitlab.subprocess.run``, so they observe exactly the argv
this adapter issues.

**The interface keeps PR vocabulary on purpose.** GitLab calls them *merge
requests*, but the protocol's verbs are ``open_pr``/``merge_pr``/``pr_title``
and the rest of the pipeline is written against those names. Renaming them
here would fork the seam for one host; do not "fix" the names.

**CI status comes from the pipelines API, not from commit statuses.**
``glab api projects/:id/pipelines?sha=<sha>`` is the source of truth because a
*pipeline* owns the per-job records, and those records are what
:meth:`failure_excerpt` needs: the failed job's id is the only way to fetch
the job trace that carries the failing test output. GitLab's commit-status
API reports a flat pass/fail per context and exposes no trace, so it could
classify a red build but never explain it.

``UNREADABLE`` and ``NONE`` are kept strictly apart: a repo that declares
``.gitlab-ci.yml`` but whose status cannot be read is ``UNREADABLE`` (the
merge gate refuses), while a repo with no ``.gitlab-ci.yml`` is ``NONE`` (the
gate may merge without checks). The repo root is resolved from
``server.REPO_ROOT`` (the scoped plan repo), never from ``os.getcwd()``, so a
process running outside the repo cannot misread a CI-declaring repo as NONE.

``prepare_merge`` is the one merge verb this adapter owns: it performs the
local rebase and the force-push that publish the rewritten branch, so
``merge_pr`` stays a pure "land it" verb.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .forge import (
    CANCELLED,
    FAIL,
    NONE,
    PASS,
    PENDING,
    UNREADABLE,
    CISnapshot,
    CIStatusUnreadable,
    Forge,
)

# GitLab's merge-request states, mapped to the protocol's vocabulary.
MERGED = "MERGED"
OPEN = "OPEN"
CLOSED = "CLOSED"
UNKNOWN = "UNKNOWN"

_MR_STATES = {"merged": MERGED, "opened": OPEN, "closed": CLOSED}

# The CI configuration file whose presence means "this repo declares CI".
_CI_CONFIG = ".gitlab-ci.yml"

_URL_RE = re.compile(r"https?://\S+")

# GitLab pipeline status -> canonical state. ``manual`` is a PASS: a job that
# waits for a human is deliberately gated, not failing, so it must not block a
# merge. Every other status is deliberately absent: an unrecognised value maps
# to UNREADABLE, never to NONE (which would permit merging without checks).
_PIPELINE_STATES = {
    "success": PASS,
    "failed": FAIL,
    "running": PENDING,
    "pending": PENDING,
    "created": PENDING,
    "preparing": PENDING,
    "scheduled": PENDING,
    "waiting_for_resource": PENDING,
    "canceled": CANCELLED,
    "cancelling": CANCELLED,
    "manual": PASS,
    "skipped": PASS,
}

# How much of a job trace to keep: enough for the failing assertion, bounded so
# the rework feedback stays readable.
_EXCERPT_LIMIT = 4000


def _mr_url(text: str) -> str:
    """The merge-request URL in ``text``, or "".

    ``glab mr create`` prints a human-readable line containing the URL, while
    ``--output json`` callers get ``{"web_url": ...}``; accept both.
    """
    text = (text or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        url = data.get("web_url") or data.get("url")
        if url:
            return str(url)
    match = _URL_RE.search(text)
    return match.group(0) if match else ""


def _repo_cwd() -> str:
    """Directory ``glab`` must run in: the scoped plan repo, read at call time.

    ``pipeline.ci._scoped_repo_root`` reassigns ``server.REPO_ROOT`` without
    chdir, so a bare ``glab`` call would query whichever repo the process was
    launched in. Reading the root here - never ``os.getcwd()`` - is what keeps
    a repo that declares CI from being misread as NONE (the merge gate's
    permission to merge without checks) just because the process happens to
    run somewhere else.
    """
    from .server import REPO_ROOT

    return str(REPO_ROOT)


def _declares_ci(repo_root: str | os.PathLike[str]) -> bool:
    """True when ``repo_root`` holds a ``.gitlab-ci.yml``."""
    return (Path(repo_root) / _CI_CONFIG).is_file()


class GitLabForge(Forge):
    """The GitLab code host, driven by the ``glab`` CLI."""

    validates_pre_merge = True
    supports_comments = True

    # -- runner seam ------------------------------------------------------
    def _run(self, argv: list[str], cwd: str | None = None, **kw: Any):
        """The single place this adapter spawns a process."""
        check = kw.pop("check", False)
        return subprocess.run(
            argv, check=check, cwd=cwd, capture_output=True, text=True, **kw
        )

    # -- merge-request verbs ----------------------------------------------
    def open_pr(self, worktree: str, branch: str, title: str, body: str) -> str:
        """Open a merge request for ``branch``, or return the existing one.

        GitLab has no "already exists" error to recover from, so the lookup
        comes *first*: an existing MR for the source branch is returned as-is
        and ``glab mr create`` is never reached. That keeps the verb
        idempotent across resumed dispatches, which re-run it for the same
        branch.
        """
        existing = self._find_mr(worktree, branch)
        if existing:
            return existing

        proc = self._run(
            [
                "glab", "mr", "create",
                "--title", title,
                "--description", body,
                "--source-branch", branch,
                "--yes",
            ],
            cwd=worktree,
        )
        return _mr_url(proc.stdout)

    def _find_mr(self, worktree: str, branch: str) -> str:
        """The ``web_url`` of the open MR whose source is ``branch``, or ""."""
        proc = self._run(
            [
                "glab", "mr", "list",
                "--source-branch", branch,
                "--output", "json",
            ],
            cwd=worktree,
        )
        if proc.returncode != 0:
            return ""
        try:
            mrs = json.loads(proc.stdout or "[]")
        except ValueError:
            return ""
        if isinstance(mrs, list) and mrs and isinstance(mrs[0], dict):
            return str(mrs[0].get("web_url") or "")
        return ""

    def update_pr_title(self, worktree: str, branch: str, title: str) -> None:
        """Set the MR title; best-effort, never raises."""
        try:
            self._run(
                ["glab", "mr", "update", branch, "--title", title],
                cwd=worktree,
            )
        except OSError:
            return

    def comment_pr(self, worktree: str, body: str) -> None:
        """Post ``body`` as a note on the branch's MR; never raises."""
        try:
            self._run(
                ["glab", "mr", "note", "--message", body],
                cwd=worktree,
            )
        except OSError:
            return

    def merge_pr(self, worktree: str, branch: str, subject: str) -> str:
        """Squash-merge the MR, passing ``subject`` as the commit subject.

        ``--message`` is passed explicitly: without it the squash commit takes
        the branch's own last commit message, which for a resumed dispatch can
        be a step-cap checkpoint's "WIP" text rather than the story title.
        """
        argv = ["glab", "mr", "merge", branch, "--squash", "--yes"]
        if subject:
            argv += ["--message", subject]
        proc = self._run(argv, cwd=worktree)
        return (proc.stdout or "").strip()

    def pr_title(self, worktree: str, branch: str) -> str:
        """The MR's current title, or "" when it cannot be read."""
        try:
            proc = self._run(
                ["glab", "mr", "view", branch, "--output", "json"],
                cwd=worktree,
            )
        except OSError:
            return ""
        if proc.returncode != 0:
            return ""
        try:
            data = json.loads(proc.stdout or "{}")
        except ValueError:
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("title") or "")

    def pr_state(self, handle: str) -> str:
        """One of MERGED | OPEN | CLOSED | UNKNOWN for the MR ``handle``."""
        try:
            proc = self._run(["glab", "mr", "view", handle, "--output", "json"])
        except OSError:
            return UNKNOWN
        if proc.returncode != 0:
            return UNKNOWN
        try:
            data = json.loads(proc.stdout or "{}")
        except ValueError:
            return UNKNOWN
        if not isinstance(data, dict):
            return UNKNOWN
        return _MR_STATES.get(str(data.get("state") or "").lower(), UNKNOWN)


    # -- merge preparation ------------------------------------------------
    def prepare_merge(self, worktree: str, branch: str) -> dict[str, Any]:
        """Rebase ``branch`` onto the default branch, then force-push it.

        This is the whole "make it landable" half of a merge - the local
        rebase plus the force-push that publishes the rewritten history - so
        :meth:`merge_pr` can stay a pure "land it" verb. GitLab's merge
        endpoint does not rebase for us, so the adapter must own it exactly as
        :class:`pipeline.forge_github.GitHubForge` does. Returns::

            {"ok": bool, "error": str, "pushed_sha": str, "auto_resolved": bool}

        ``error`` is the bare ``rebase: <git stderr>`` / ``push: <stderr>``
        text, never wrapped: the merge gate's conflict-rework route matches
        ``gate_error.startswith("rebase:")`` and ``"conflict" in gate_error``,
        so a prefix like ``prepare_merge failed: rebase: ...`` would silently
        disable that route.

        A conflicted rebase short-circuits before the push, so a rework
        round's follow-up call re-runs the rebase first instead of
        force-pushing stale history. No "already prepared" flag is kept for
        the same reason: state across calls would let call 2 skip the rebase.
        """
        from .server import REPO_ROOT, _rebase_onto_master

        rb = _rebase_onto_master(worktree, branch)
        auto_resolved = bool(rb.get("auto_resolved"))
        if not rb["ok"]:
            return {
                "ok": False,
                "error": f"rebase: {rb['error']}",
                "pushed_sha": "",
                "auto_resolved": auto_resolved,
            }
        pushed_sha = ""
        if Path(worktree).is_dir():
            push = self._run(
                ["git", "push", "--force-with-lease", "origin", branch],
                cwd=REPO_ROOT, check=False,
            )
            if push.returncode != 0:
                return {
                    "ok": False,
                    "error": f"push: {(push.stderr or push.stdout).strip()[:200]}",
                    "pushed_sha": "",
                    "auto_resolved": auto_resolved,
                }
            rev = self._run(
                ["git", "rev-parse", "HEAD"], cwd=worktree, check=False
            )
            pushed_sha = rev.stdout.strip()
        return {
            "ok": True,
            "error": "",
            "pushed_sha": pushed_sha,
            "auto_resolved": auto_resolved,
        }

    # -- CI verbs ---------------------------------------------------------
    # The poll loop and the state machine stay above the adapter, in
    # ``pipeline/ci.py``; each verb here answers exactly one question.
    def has_ci_config(self, repo_root: str) -> bool:
        """True when ``repo_root`` declares GitLab CI (``.gitlab-ci.yml``).

        ``repo_root`` is the primary source, so a worktree or a scoped plan
        repo is answered from its own ``.gitlab-ci.yml``. A root that declares
        nothing defers to the repo ``glab`` will actually query
        (:func:`_repo_cwd`) and to the process repo: worktrees and the process
        cwd are views of the same repo, so the repo as a whole is
        authoritative. The union is fail-safe - "CI is configured" is the
        strict direction, yielding UNREADABLE/PENDING and never NONE, which
        would permit merging without checks.

        Read from disk on every call, never cached: a repo can gain or lose
        its CI config between two polls, and a stale ``False`` would read as
        NONE and permit a merge without checks.
        """
        if _declares_ci(repo_root):
            return True
        for other in (_repo_cwd(), os.getcwd()):
            if str(other) != str(repo_root) and _declares_ci(other):
                return True
        return False

    def checks_status(self, branch: str, sha: str) -> dict[str, str]:
        """ONE pipeline snapshot for ``branch``/``sha``; never polls.

        The pipelines API is the source of truth (see the module docstring).
        When the repo declares no ``.gitlab-ci.yml`` the answer is NONE and
        the API is never called; when it does declare CI, any failure to read
        the status - missing ``glab``, non-zero exit, unparseable JSON - is
        UNREADABLE, never NONE.
        """
        cwd = _repo_cwd()
        if not self.has_ci_config(cwd):
            return {"pipeline": NONE}

        query = f"sha={sha}" if sha else f"ref={branch}"
        try:
            proc = self._api(f"projects/:id/pipelines?{query}", cwd=cwd)
        except OSError:
            return {"pipeline": UNREADABLE}
        if proc.returncode != 0:
            return {"pipeline": UNREADABLE}
        try:
            pipelines = json.loads(proc.stdout or "[]")
        except ValueError:
            return {"pipeline": UNREADABLE}
        if not isinstance(pipelines, list):
            return {"pipeline": UNREADABLE}
        if not pipelines:
            # CI is configured but no pipeline has reported for this commit
            # yet: that is "not finished", not "no CI".
            return {"pipeline": PENDING}

        first = pipelines[0]
        status = str(first.get("status") or "").lower() if isinstance(first, dict) else ""
        return {"pipeline": _PIPELINE_STATES.get(status, UNREADABLE)}

    def checks_snapshot(self, branch: str, sha: str) -> CISnapshot:
        """ONE CI snapshot for ``branch``/``sha``; never polls.

        Raises :class:`CIStatusUnreadable` when CI is declared but its status
        cannot be read, so the caller maps it to UNREADABLE rather than NONE.
        """
        snapshot = self.checks_status(branch, sha)
        state = next(iter(snapshot.values()), NONE)
        if state == UNREADABLE:
            raise CIStatusUnreadable("GitLab pipeline status unreadable")
        if state == NONE:
            return CISnapshot()
        return CISnapshot(snapshot, detail=f"pipeline: {state}")

    def failure_excerpt(self, branch: str) -> str | None:
        """A bounded trace of the failed job in ``branch``'s latest pipeline.

        Pipelines, not commit statuses, are what make this possible: the
        pipeline's job list names the failed job, and that job's trace carries
        the failing test output. Returns None on any failure - the caller
        falls back to the classification-only detail.
        """
        cwd = _repo_cwd()
        try:
            pipeline_id = self._latest_pipeline_id(cwd, ref=branch)
            if pipeline_id is None:
                return None
            jobs = self._api_json(
                f"projects/:id/pipelines/{pipeline_id}/jobs", cwd=cwd
            )
            if not isinstance(jobs, list):
                return None
            failed = next(
                (
                    job
                    for job in jobs
                    if isinstance(job, dict) and str(job.get("status")) == "failed"
                ),
                None,
            )
            if failed is None or failed.get("id") is None:
                return None
            proc = self._api(f"projects/:id/jobs/{failed['id']}/trace", cwd=cwd)
        except (OSError, ValueError):
            return None
        if proc.returncode != 0:
            return None
        return _tail(proc.stdout)

    def rerun_failed(self, sha: str) -> bool:
        """Retry the failed jobs of ``sha``'s pipeline; False on any failure.

        A pipeline retry, not a single-job retry: the pipeline re-runs exactly
        the jobs that failed and keeps the rest of the graph intact.
        """
        cwd = _repo_cwd()
        try:
            pipeline_id = self._latest_pipeline_id(cwd, sha=sha)
            if pipeline_id is None:
                return False
            proc = self._api(
                f"projects/:id/pipelines/{pipeline_id}/retry",
                cwd=cwd,
                method="POST",
            )
        except (OSError, ValueError):
            return False
        return proc.returncode == 0

    # -- CI helpers -------------------------------------------------------
    def _api(self, path: str, cwd: str | None = None, *, method: str | None = None):
        """One ``glab api`` call; ``:id`` is glab's current-project placeholder."""
        argv = ["glab", "api"]
        if method:
            argv += ["--method", method]
        argv.append(path)
        return self._run(argv, cwd=cwd)

    def _api_json(self, path: str, cwd: str | None = None):
        """``glab api`` decoded as JSON; raises ValueError when unparseable."""
        proc = self._api(path, cwd=cwd)
        if proc.returncode != 0:
            raise ValueError((proc.stderr or "").strip()[:200])
        return json.loads(proc.stdout or "[]")

    def _latest_pipeline_id(
        self, cwd: str, *, sha: str | None = None, ref: str | None = None
    ):
        """The most recent pipeline's id for ``sha`` or ``ref``, or None."""
        query = f"sha={sha}" if sha else f"ref={ref}"
        pipelines = self._api_json(f"projects/:id/pipelines?{query}", cwd=cwd)
        if not isinstance(pipelines, list) or not pipelines:
            return None
        first = pipelines[0]
        return first.get("id") if isinstance(first, dict) else None


def _tail(text: str | None, limit: int = _EXCERPT_LIMIT) -> str | None:
    """The last ``limit`` characters of ``text``, stripped; None when empty."""
    text = (text or "").strip()
    if not text:
        return None
    return text[-limit:]


__all__ = ["CLOSED", "MERGED", "OPEN", "UNKNOWN", "GitLabForge"]
