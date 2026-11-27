"""Gate selection and CI poll phase of the advance merge adjudication loop.

Split out of ``pipeline.advance_merge._adjudicate_merges``. Names that tests
patch on ``pipeline.advance_merge`` are reached through that module at call
time (``_home``); the names bound in ``pipeline.advance`` go through
``_ModuleRef`` exactly as they do there.
"""

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import advance_merge as _home
from .ci import SettingsContext
from .module_ref import _ModuleRef

_ci_pending_expired = _ModuleRef("pipeline.advance", "_ci_pending_expired")
_ci_rerun = _ModuleRef("pipeline.advance", "_ci_rerun")
_degraded_ci_branch = _ModuleRef("pipeline.advance", "_degraded_ci_branch")
_merge_gate_ci_status = _ModuleRef("pipeline.advance", "_merge_gate_ci_status")
_notify_user = _ModuleRef("pipeline.advance", "_notify_user")
_rebase_and_push_for_merge = _ModuleRef("pipeline.advance", "_rebase_and_push_for_merge")


@dataclass(frozen=True)
class CiGate:
    """What the CI gate phase hands the loop.

    ``wait`` means no verdict this tick (the old ``ci_wait``); otherwise
    ``gate_error`` is empty on a pass, and ``definitive_fail`` marks a genuine
    CI test-failure verdict. ``pushed_sha`` is the commit the gate graded.
    """

    gate_error: str
    pushed_sha: str
    definitive_fail: bool
    wait: bool


def _select_gate_sha(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    branch: str,
    worktree: str,
) -> tuple[str, str]:
    """Pick the commit to gate: the pending one, the re-verify one, or a fresh rebase."""
    from .review_merge_binding import rebind_after_pipeline_push

    gate_error = ""
    # S5: a story already polling a pending CI run must not
    # re-rebase/force-push on every tick - with the non-blocking CI
    # poll that mints a new SHA whenever origin/master moved,
    # restarting CI and burning Actions minutes. Skip straight to
    # polling the exact SHA recorded on the first pending observation.
    if story.get("ci_pending_sha"):
        pushed_sha = story["ci_pending_sha"]
    elif story.get("merge_reverify"):
        # A pending re-verify must not re-rebase and restart CI every
        # tick: poll the exact commit the job was started for as long as
        # the branch head is still that commit. A moved head makes the
        # job's verdict worthless, so cancel it and rebase as usual.
        head = None
        if worktree:
            try:
                proc = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=worktree,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                head = (proc.stdout or "").strip() or None
            except (OSError, subprocess.SubprocessError):
                head = None
        if head is not None and head == story["merge_reverify"].get("sha"):
            pushed_sha = head
        else:
            _rv_job = story.pop("merge_reverify", None)
            if _rv_job:
                _home.cancel_merge_reverify(_rv_job)
            gate_error, pushed_sha = _rebase_and_push_for_merge(
                plan_name, key, branch, worktree
            )
    else:
        gate_error, pushed_sha = _rebase_and_push_for_merge(plan_name, key, branch, worktree)
    if not gate_error and pushed_sha:
        # The merge gate's own rebase and force-push move the branch head
        # AFTER the review: that is expected, so the verdict is re-bound to
        # the head the pipeline just pushed HERE (at its commit point) -
        # never by loosening the check, which already ran on the tip above.
        rebind_after_pipeline_push(story, pushed_sha)
    return gate_error, pushed_sha


def _poll_ci(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    branch: str,
    pushed_sha: str,
    settings: SettingsContext,
) -> tuple[str, bool, bool]:
    """Poll CI for ``pushed_sha``; returns ``(gate_error, definitive_fail, wait)``."""
    gate_error = ""
    ci_definitive_fail = False
    ci_wait = False
    poll_branch = branch or _degraded_ci_branch(key)
    ci = _merge_gate_ci_status(poll_branch, sha=pushed_sha, settings=settings)
    if ci["state"] == "cancelled" and not story.get("ci_rerun_attempted"):
        # Worth exactly one automatic rerun before treating it
        # as a failure - an abnormal queue delay can cancel
        # jobs with no code-quality signal at all.
        story["ci_rerun_attempted"] = True
        _ci_rerun(pushed_sha)
        ci = _merge_gate_ci_status(poll_branch, sha=pushed_sha, settings=settings)
    # Record the gate's own final result for every risk level, so a
    # merged story always carries CI evidence and a blocked one shows
    # why (fail/pending/unreadable included).
    story["pr_checks"] = ci
    story.pop("pr_checks_error", None)
    if ci["state"] == "fail":
        gate_error = f"ci fail: {ci['error']}"
        # Only a genuine test-failure verdict is "definitive" -
        # cancelled (queue/infra flake, already given one
        # auto-rerun above) and pending are NOT, and must keep
        # retrying via the ordinary merge_attempts path below,
        # not consume rework budget.
        ci_definitive_fail = True
    elif ci["state"] == "cancelled":
        gate_error = f"ci fail: {ci['error']}"
    elif ci["state"] == "unreadable":
        # Status could not be read although workflows exist: park via
        # the ordinary gate_error/merge_attempts path, never merge blind.
        gate_error = f"ci unreadable: {ci['error']}"
    elif ci["state"] == "pending":
        story.setdefault("ci_pending_since", datetime.now(UTC).isoformat())
        story["ci_pending_sha"] = pushed_sha
        if _ci_pending_expired(story["ci_pending_since"], settings=settings):
            _notify_user(
                plan_name,
                f"{key} CI has been pending since "
                f"{story['ci_pending_since']} and exceeded the "
                f"merge-gate pending bound; giving up on the wait.",
                story_key=key,
                severity="warning",
                event="ci_pending_stalled",
                dedup_key=f"ci_pending_stalled:{key}",
                **(
                    {"correlation_id": story["correlation_id"]}
                    if story.get("correlation_id")
                    else {}
                ),
            )
            story.pop("ci_pending_since", None)
            story.pop("ci_pending_sha", None)
            gate_error = f"ci pending: {ci['error']}"
        else:
            ci_wait = True
    if ci["state"] != "pending":
        story.pop("ci_pending_since", None)
        story.pop("ci_pending_sha", None)
    return gate_error, ci_definitive_fail, ci_wait


def run_ci_gate(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    branch: str,
    worktree: str,
    settings: SettingsContext,
) -> CiGate:
    """Rebase/push (or reuse the pending commit) and poll CI for it."""
    gate_error, pushed_sha = _select_gate_sha(plan_name, key, story, branch, worktree)
    if gate_error:
        return CiGate(gate_error, pushed_sha, False, False)
    gate_error, definitive_fail, wait = _poll_ci(
        plan_name, key, story, branch, pushed_sha, settings
    )
    return CiGate(gate_error, pushed_sha, definitive_fail, wait)
