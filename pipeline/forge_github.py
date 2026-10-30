"""GitHub adapter for the :class:`pipeline.forge.Forge` protocol.

Every ``gh``/``git`` invocation for the PR verbs goes through one runner seam,
:meth:`GitHubForge._run`, which calls ``subprocess.run``. Tests patch
``subprocess.run`` on the shared subprocess module, so they observe exactly
the argv this adapter issues.

The CI verbs are implemented here: ``checks_snapshot`` classifies one
snapshot of GitHub's two CI vocabularies into the canonical states, and
``has_ci_config`` / ``failure_log_excerpt`` / ``rerun_workflow`` own the
``gh`` argv. The poll loop and the state machine stay above the adapter in
``pipeline/ci.py``, which is host-neutral.

The CI verbs spawn ``gh`` directly rather than through :meth:`_run`: ``_run``
is the PR-verb seam, and the CI verbs need ``capture_output``/``text`` and
their own error handling, so they call ``subprocess.run`` themselves.

``prepare_merge`` is the one merge verb this adapter owns: it performs the
local rebase and the force-push that publish the rewritten branch, so
``merge_pr`` stays a pure "land it" verb.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .forge import (
    CANCELLED,
    FAIL,
    PASS,
    PENDING,
    CISnapshot,
    CIStatusUnreadable,
    Forge,
)

# GitHub's legacy commit-status ``bucket`` vocabulary, grouped by the canonical
# state it maps to. ``skipping`` is a PASS: a skipped check is not a failure
# and must not block a merge. ``cancel`` is CANCELLED, not FAIL - the merge
# gate gives a cancellation exactly one automatic rerun.
_BUCKET_FAIL = frozenset({"fail", "error", "action_required"})
_BUCKET_CANCELLED = frozenset({"cancel", "cancelled"})
_BUCKET_PASS = frozenset({"pass", "skipping"})
_BUCKET_PENDING = frozenset({"pending"})

# The check-runs vocabulary. ``completed`` with no conclusion is deliberately
# absent from both sets: it is unreadable, never a vacuous PASS.
_CHECK_RUN_FAIL_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required"}
)
_CHECK_RUN_PASS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


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
                ["gh", "pr", "view", handle, "--json", "state", "--jq", ".state"],
                check=False, capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "UNKNOWN"
        if proc is None or proc.returncode != 0:
            return "UNKNOWN"
        state = (proc.stdout or "").strip().upper()
        return state if state in {"MERGED", "OPEN", "CLOSED"} else "UNKNOWN"

    # -- CI verbs ---------------------------------------------------------
    # The real implementations live here. The S1-era names
    # (``checks_status``/``failure_excerpt``/``rerun_failed``/``has_ci_config``)
    # stay thin delegations to ``pipeline.ci``'s public shims, which in turn
    # delegate back to the ``*_impl`` methods below - so the seam is inverted
    # without a delegation cycle and the S1 contract tests keep passing.
    def checks_status(self, branch: str, sha: str) -> dict[str, str]:
        """ONE CI status for ``branch``/``sha``; never polls (S1-era name)."""
        from .ci import _ci_status_once

        return _ci_status_once(branch, sha=sha)

    def failure_excerpt(self, branch: str) -> str | None:
        """A bounded failing-test excerpt, or None (S1-era name)."""
        from .ci import _fetch_ci_failure_excerpt

        return _fetch_ci_failure_excerpt(branch)

    def rerun_failed(self, sha: str) -> bool:
        """Rerun the failed jobs for ``sha``; False on failure (S1-era name)."""
        from .ci import _ci_rerun

        return _ci_rerun(sha)

    # -- CI verbs: the implementations ------------------------------------
    @staticmethod
    def _declares_workflows(repo_root: str) -> bool:
        """True when ``repo_root`` has a ``.github/workflows/`` directory.

        ``is_dir()`` rather than "contains a workflow file": an empty
        ``.github/workflows/`` still means CI is configured, so an unreadable
        status must refuse the merge instead of reading as "no CI".
        """
        from pathlib import Path

        return (Path(repo_root) / ".github" / "workflows").is_dir()

    def has_ci_config(self, repo_root: str) -> bool:
        """True when ``repo_root`` declares GitHub Actions workflows.

        ``repo_root`` is the primary source, so a worktree or a scoped plan
        repo is answered from its own ``.github/workflows/`` rather than from
        the process-global ``server.REPO_ROOT``. When that root declares no
        workflows we defer to ``pipeline.ci``'s process-repo shim: worktrees
        are views of the same repo, so the repo as a whole is authoritative
        for a root that declares nothing. The union is fail-safe - "CI is
        configured" is the strict direction, yielding UNREADABLE/PENDING and
        never NONE, which would permit merging without checks.

        Asking about the process repo itself short-circuits: the shim would
        only re-enter this method with the same root.
        """
        if self._declares_workflows(repo_root):
            return True
        from .ci import _repo_has_ci_configured
        from .server import REPO_ROOT

        if str(repo_root) == str(REPO_ROOT):
            return False
        return _repo_has_ci_configured()

    def checks_snapshot(self, branch: str, sha: str) -> CISnapshot:
        """ONE CI snapshot for ``branch``/``sha``; never polls.

        Classifies both GitHub vocabularies - the legacy commit-status
        ``bucket`` values and the check-runs ``status``/``conclusion`` pair -
        into the canonical states. Raises :class:`CIStatusUnreadable` when the
        status cannot be read or a value is outside the known vocabulary.

        With a non-empty ``sha`` and a positive ``PIPELINE_CI_ROLLUP_MAX_AGE_SECONDS``
        (default 90, ``0`` disables) a fresh entry of the per-repository rollup
        cache answers instead of the REST call. The cache only ever holds what
        GitHub reported for that exact sha and an aged entry is ignored, so a
        cache hit can never report a failing sha as passing; a miss simply
        runs the REST call unchanged.
        """
        from .ci import _scoped_repo_cwd

        cwd = _scoped_repo_cwd()
        if sha:
            from . import ci_rollup

            max_age = ci_rollup.max_age_seconds()
            if max_age > 0:
                cached = ci_rollup.cached_snapshot(
                    cwd, sha, ci_rollup.state_root(), max_age
                )
                if cached is not None:
                    return cached
        try:
            if sha:
                proc = subprocess.run(
                    [
                        "gh",
                        "api",
                        f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs",
                        "--jq",
                        ".check_runs[] | {name, status, conclusion}",
                    ],
                    check=False, capture_output=True, cwd=cwd, text=True,
                )
            else:
                proc = subprocess.run(
                    ["gh", "pr", "checks", branch, "--json", "name,bucket"],
                    check=False, capture_output=True, cwd=cwd, text=True,
                )
        except OSError as e:
            raise CIStatusUnreadable(f"gh unavailable: {e}") from e

        if proc.returncode != 0:
            raise CIStatusUnreadable((proc.stderr or "").strip()[:200])

        if sha:
            return self._classify_check_runs(proc.stdout)
        return self._classify_buckets(proc.stdout)

    @staticmethod
    def _classify_check_runs(stdout: str) -> CISnapshot:
        """Map ``gh api .../check-runs`` output to canonical states."""
        import json

        try:
            runs = [
                json.loads(line) for line in (stdout or "").splitlines() if line.strip()
            ]
        except ValueError as e:
            raise CIStatusUnreadable("unparseable gh api check-runs output") from e

        snapshot = CISnapshot()
        failing: list[str] = []
        cancelled: list[str] = []
        for index, run in enumerate(runs):
            # A check with no name still counts: key it positionally so two
            # unnamed checks cannot collapse into one and hide a state.
            name = run.get("name") or f"check-{index}"
            status = run.get("status")
            conclusion = run.get("conclusion")
            if conclusion in _CHECK_RUN_FAIL_CONCLUSIONS:
                state = FAIL
                failing.append(f"{name}: {conclusion}")
            elif conclusion == "cancelled":
                state = CANCELLED
                cancelled.append(f"{name}: {conclusion}")
            elif status != "completed":
                state = PENDING
            elif conclusion in _CHECK_RUN_PASS_CONCLUSIONS:
                state = PASS
            else:
                # ``completed`` with no (or an unknown) conclusion: the status
                # was read but cannot be classified. Never guess PASS.
                raise CIStatusUnreadable(
                    f"unrecognised check-run conclusion {conclusion!r} for {name!r}"
                )
            snapshot[name] = state

        if failing:
            snapshot.detail = "; ".join(failing)
        elif cancelled:
            snapshot.detail = "; ".join(cancelled)
        return snapshot

    @staticmethod
    def _classify_buckets(stdout: str) -> CISnapshot:
        """Map ``gh pr checks --json name,bucket`` output to canonical states."""
        import json

        try:
            entries = json.loads(stdout or "[]")
        except ValueError as e:
            raise CIStatusUnreadable("unparseable gh pr checks output") from e

        snapshot = CISnapshot()
        failing: list[str] = []
        cancelled: list[str] = []
        for index, entry in enumerate(entries):
            # A check with no name still counts: key it positionally so two
            # unnamed checks cannot collapse into one and hide a state.
            name = entry.get("name") or f"check-{index}"
            bucket = entry.get("bucket")
            if bucket in _BUCKET_FAIL:
                state = FAIL
                failing.append(f"{name}: {bucket}")
            elif bucket in _BUCKET_CANCELLED:
                state = CANCELLED
                cancelled.append(f"{name}: {bucket}")
            elif bucket in _BUCKET_PASS:
                state = PASS
            elif bucket in _BUCKET_PENDING:
                state = PENDING
            else:
                raise CIStatusUnreadable(
                    f"unrecognised check bucket {bucket!r} for {name!r}"
                )
            snapshot[name] = state

        if failing:
            snapshot.detail = "; ".join(failing)
        elif cancelled:
            snapshot.detail = "; ".join(cancelled)
        return snapshot

    def failure_log_excerpt(self, branch: str) -> str | None:
        """Capture a bounded failing-test excerpt from the failed workflow run.

        Resolves the most recent workflow run id for ``branch`` via
        ``gh run list``, then pulls the failing job's log with
        ``gh run view --log-failed`` (falling back to ``--log`` if that errors
        or returns empty). Returns a bounded tail (~800 chars) of the log, or
        ``None`` on any failure so callers can fall back to the
        classification-only ``error``. Never raises.
        """
        import json

        try:
            run_list = subprocess.run(
                [
                    "gh", "run", "list", "--branch", branch, "--limit", "1",
                    "--json", "databaseId", "--jq", ".[0].databaseId",
                ],
                check=False, capture_output=True, text=True, timeout=20,
            )
            if run_list.returncode != 0:
                return None
            try:
                data = json.loads(run_list.stdout or "[]")
            except ValueError:
                return None
            if isinstance(data, list):
                if not data:
                    return None
                run_id = data[0].get("databaseId")
            else:
                run_id = data
            if not run_id:
                return None
            run_id = str(run_id)
            for flag in ("--log-failed", "--log"):
                view = subprocess.run(
                    ["gh", "run", "view", run_id, flag],
                    check=False, capture_output=True, text=True, timeout=20,
                )
                if view.returncode == 0 and view.stdout.strip():
                    return view.stdout[-800:]
            return None
        except (subprocess.SubprocessError, OSError, ValueError):
            return None

    def rerun_workflow(self, sha: str) -> bool:
        """Rerun the failed/cancelled jobs of the workflow run for ``sha``.

        Never raises; on any failure it returns ``False`` so callers can fall
        back to the ordinary retry logic.
        """
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={sha}",
                    "--jq",
                    ".workflow_runs[0].id",
                ],
                check=False, capture_output=True,
                text=True,
            )
        except OSError:
            return False
        if proc.returncode != 0:
            return False
        run_id = proc.stdout.strip()
        if not run_id:
            return False
        try:
            rerun = subprocess.run(
                ["gh", "run", "rerun", run_id, "--failed"],
                check=False, capture_output=True,
                text=True,
            )
        except OSError:
            return False
        return rerun.returncode == 0


    # -- merge preparation ------------------------------------------------
    def prepare_merge(self, worktree: str, branch: str) -> dict[str, Any]:
        """Rebase ``branch`` onto the default branch, then force-push it.

        This is the whole "make it landable" half of a merge - the local
        rebase plus the force-push that publishes the rewritten history - so
        :meth:`merge_pr` can stay a pure "land it" verb. Returns::

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
                cwd=REPO_ROOT, check=False, capture_output=True, text=True,
            )
            if push.returncode != 0:
                return {
                    "ok": False,
                    "error": f"push: {(push.stderr or push.stdout).strip()[:200]}",
                    "pushed_sha": "",
                    "auto_resolved": auto_resolved,
                }
            rev = self._run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree, check=False, capture_output=True, text=True,
            )
            pushed_sha = rev.stdout.strip()
        return {
            "ok": True,
            "error": "",
            "pushed_sha": pushed_sha,
            "auto_resolved": auto_resolved,
        }


__all__ = ["GitHubForge"]
