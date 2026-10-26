"""GitHub adapter for the :class:`pipeline.forge.Forge` protocol.

Every ``gh``/``git`` invocation for the PR verbs goes through one runner seam,
:meth:`GitHubForge._run`, which calls ``subprocess.run``. Tests patch
``subprocess.run`` on the shared subprocess module, so they observe exactly
the argv this adapter issues.

The CI verbs and the merge-preparation helper are *delegations*, not
reimplementations: S1 only declares the seam, a later story moves the bodies.
"""

from __future__ import annotations

import subprocess
from typing import Any

from .forge import Forge


class GitHubForge(Forge):
    """The GitHub code host, driven by the ``gh`` CLI."""

    validates_pre_merge = True
    supports_comments = True

    # -- runner seam ------------------------------------------------------
    def _run(self, argv: list[str], cwd: str | None = None, **kw: Any):
        """The single place this adapter spawns a process."""
        check = kw.pop("check", False)
        return subprocess.run(argv, check=check, cwd=cwd, **kw)

    # -- PR verbs ---------------------------------------------------------
    def open_pr(self, worktree: str, branch: str, title: str, body: str) -> str:
        """Push ``branch`` and open a PR for it via the gh CLI.

        ``--force-with-lease``: this branch is rebased onto the default branch
        before every resumed dispatch, which rewrites its commit SHAs. If a
        prior round already pushed, a plain push is rejected as
        non-fast-forward on every retry - the branch is exclusively owned by
        this pipeline's own dispatched agent, so there is no external pusher
        to race and a force-with-lease push is safe.
        """
        self._run(
            ["git", "push", "--force-with-lease", "-u", "origin", branch],
            cwd=worktree, check=True, capture_output=True, text=True,
        )
        try:
            proc = self._run(
                ["gh", "pr", "create", "--title", title, "--body", body,
                 "--head", branch],
                cwd=worktree, check=True, capture_output=True, text=True,
            )
            return proc.stdout.strip()
        except subprocess.CalledProcessError as e:
            # A dispatched agent's own Bash access can include `gh pr create`,
            # so a PR may already exist by the time review_story gets here.
            # Recover its URL instead of failing the whole pipeline tick.
            if "already exists" not in (e.stderr or ""):
                raise
            proc = self._run(
                ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
                cwd=worktree, check=True, capture_output=True, text=True,
            )
            self.update_pr_title(worktree, branch, title)
            return proc.stdout.strip()

    def update_pr_title(self, worktree: str, branch: str, title: str) -> None:
        """Refresh the PR title; a failure must never fail the tick."""
        try:
            self._run(
                ["gh", "pr", "edit", branch, "--title", title],
                cwd=worktree, check=True, capture_output=True, text=True,
            )
        except (subprocess.CalledProcessError, OSError):
            pass

    def comment_pr(self, worktree: str, body: str) -> None:
        """Post ``body`` as a comment on the current branch's PR."""
        self._run(
            ["gh", "pr", "comment", "--body", body],
            cwd=worktree, check=True, capture_output=True, text=True,
        )

    def merge_pr(self, worktree: str, branch: str, subject: str) -> str:
        """Squash-merge the PR, passing ``subject`` as the commit subject.

        Without ``--subject``, ``gh pr merge --squash`` can fall back to the
        branch's own last commit message instead of the PR title - observed
        live 2026-09-17 (PR #821), where a step-cap checkpoint's own
        "WIP (step cap reached)" message became the permanent subject on the
        default branch.

        Deliberately does not pass ``--delete-branch``: that asks gh to switch
        the local checkout away from the branch being deleted, which fails
        here because the branch is checked out in its own worktree.
        """
        merge_cmd = ["gh", "pr", "merge", branch, "--squash"]
        if subject:
            merge_cmd += ["--subject", subject]
        proc = self._run(
            merge_cmd, cwd=worktree, check=True, capture_output=True, text=True,
        )
        return proc.stdout.strip()

    def pr_title(self, worktree: str, branch: str) -> str:
        """The PR's current title, or "" when it cannot be read.

        The title *formatter* (``pipeline.pr._pr_title``) stays in
        ``pipeline/pr.py``: it is a pure string helper, not a forge verb.
        """
        try:
            proc = self._run(
                ["gh", "pr", "view", branch, "--json", "title", "-q", ".title"],
                cwd=worktree, check=False, capture_output=True, text=True,
            )
        except OSError:
            return ""
        if proc is None or proc.returncode != 0:
            return ""
        return (proc.stdout or "").strip()

    def pr_state(self, handle: str) -> str:
        """One of MERGED | OPEN | CLOSED | UNKNOWN for the PR ``handle``."""
        try:
            proc = self._run(
                ["gh", "pr", "view", handle, "--json", "state", "-q", ".state"],
                check=False, capture_output=True, text=True,
            )
        except OSError:
            return "UNKNOWN"
        if proc is None or proc.returncode != 0:
            return "UNKNOWN"
        state = (proc.stdout or "").strip().upper()
        return state if state in {"MERGED", "OPEN", "CLOSED"} else "UNKNOWN"

    # -- CI verbs: delegations, not reimplementations ---------------------
    def checks_status(self, branch: str, sha: str) -> dict[str, str]:
        """ONE CI snapshot for ``branch``/``sha``; never polls."""
        from .ci import _ci_status_once

        return _ci_status_once(branch, sha=sha)

    def failure_excerpt(self, branch: str) -> str | None:
        """A bounded failing-test excerpt, or None."""
        from .ci import _fetch_ci_failure_excerpt

        return _fetch_ci_failure_excerpt(branch)

    def rerun_failed(self, sha: str) -> bool:
        """Rerun the failed jobs for ``sha``; False on any failure."""
        from .ci import _ci_rerun

        return _ci_rerun(sha)

    def has_ci_config(self, repo_root: str) -> bool:
        """True when the repo declares CI workflows."""
        from .ci import _repo_has_ci_configured

        return _repo_has_ci_configured()

    # -- merge preparation ------------------------------------------------
    def prepare_merge(self, worktree: str, branch: str) -> None:
        """Rebase ``branch`` onto the default branch and push it."""
        from .rebase import _rebase_onto_master, _sync_branch_remote

        result = _rebase_onto_master(worktree, branch)
        if result.get("ok"):
            _sync_branch_remote(worktree, branch)


__all__ = ["GitHubForge"]
