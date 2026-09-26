"""The decision, review, merge and plan-control MCP tools, moved verbatim
from pipeline/server.py (line-count target).

They are plain functions here: pipeline.server registers each one on its own
FastMCP instance (``mcp.tool()(fn)``, which returns ``fn`` itself) and
re-exports every tool name, so p.request_decision is still the registered
object. Keeping the registration in pipeline.server means this module never
imports the server at import time and can be imported cold. _service resolves
through pipeline.server at call time, so tests patching pipeline.server._service
keep landing.
"""

import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

from . import paths, security_audit
from .module_ref import _ModuleRef

_service = _ModuleRef("pipeline.server", "_service")


def request_decision(
    plan_name: str,
    story_key: str,
    question: str,
    options: list[str],
    context: str = "",
) -> dict[str, Any] | str:
    """
    Escalate a blocking decision to the overlord, which rules on the user's
    behalf per the decision policy. The ruling is appended to the plan's
    decisions log (audit trail, readable via list_decisions) and returned.

    plan_name: the plan's name this story belongs to.
    story_key: the story's key within that plan's manifest — the story
        that's actually blocked.
    question: the specific question you need answered, stated so a ruling
        of "pick one of these options" fully resolves it.
    options: the mutually exclusive choices the overlord may rule between,
        as plain strings (e.g. ["hand-roll a parser", "add a dependency"]).
        Not free text — the ruling should select one of these verbatim.
    context: optional — anything the overlord needs to rule correctly that
        isn't in `question` itself (constraints, tradeoffs you've already
        found, why the choice matters). Defaults to empty; provide it
        whenever the bare question is ambiguous without it.

    On success returns a dict with at least "ruling" (the chosen option's
    text), "rationale", "risk", "tier", "action", and "notify_user" — act on
    "ruling", not on your own preference. Call this from a story agent when
    you are blocked on a choice the user would normally make; do not guess.

    Fails open: if the overlord backend errors, the story is parked for a
    human and a single-line escalation message (a plain string, not the
    dict above) is returned instead of raising — check whether the return
    value is a str before reading dict keys off it.
    """
    return _service.request_decision(
        plan_name,
        story_key,
        question,
        options,
        context,
    )


def list_decisions(plan_name: str) -> list[dict]:
    """
    Return the overlord's decision log for a plan: every ruling ever made
    by request_decision on this plan, oldest first, as an audit trail.
    Read-only — makes no changes to the plan or any story.

    plan_name: the plan's name, as returned by list_plans or passed to
        save_plan/ingest_plan.

    Each entry corresponds one-to-one with a prior request_decision call
    and carries at least the story_key, question, the ruling made, and a
    timestamp. Returns an empty list if the plan has no decisions logged
    yet — this is normal for a plan with no blocked stories, not an error.

    Call this to check for precedent before escalating a similar decision
    with request_decision, or when a human wants to review what the
    overlord has ruled on so far for a plan.
    """
    return _service.list_decisions(plan_name)


def review_story(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Run the code-reviewer persona over a dispatched story's branch. On APPROVE,
    open a PR via gh and set status to pr_open; otherwise set status to
    changes_requested. Does not merge — merge is the overlord's decision.

    Only reviewable when story["status"] == "tests_passed" - any other status
    (a stale/duplicate call, e.g. a second tick racing an already-merged
    story) is a no-op skip; see README.md's "Review & merge" section.
    """
    return _service.review_story(plan_name, story_key)


def advance_pipeline(plan_name: str) -> dict[str, Any]:
    """
    Run one orchestration tick: dispatch every ready story (deps satisfied),
    advance finished stories through test -> review -> PR, and adjudicate merges
    against the risk threshold. Idempotent; designed to be called repeatedly by
    a scheduler (/loop or cron). In PIPELINE_AUTONOMY=dry-run it plans and logs
    only, taking no actions.

    Honors a per-backend resource gate: dispatch and review are gated
    independently by their own backend's resource_status() (see
    _role_resource_ok). If the dispatch backend is gated, in-progress stories
    are interrupted (checkpointed, resumable) and no new dispatch starts; if
    the review backend is gated, review is deferred. Each is independent, so a
    Claude usage pause no longer freezes local-backed dispatch. Merge
    adjudication always runs (no model usage). "interrupted" stories are
    dispatch-eligible like "todo" ones, so they resume automatically once the
    dispatch backend frees up.

    Also honors MAX_CONCURRENT_AGENTS: dispatch is capped to the number of
    free slots remaining (limit minus agents already in_progress across all
    plans), so a tick never starts more agents than the configured ceiling.
    Stories left undispatched this tick stay "todo"/"interrupted" and are
    picked up on a later tick as slots free up.

    Skips entirely (returns {"ok": True, "skipped": "locked"}) if another
    tick for this same plan is already running - see _plan_lock.
    """
    return _service.advance_pipeline(plan_name)


def approve_merge(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Merge a reviewed story's PR into the default branch, right now, on the
    caller's explicit approval — this is the human/overlord merge decision
    itself, not a status check.

    plan_name: the plan's name, as returned by list_plans or passed to
        save_plan/ingest_plan.
    story_key: the story's key within that plan's manifest. Must currently
        be "parked" or "pr_open" with review_verdict == "APPROVE" — any
        other state (not yet reviewed, still in progress, already merged)
        returns {"ok": False, "error": ...} without changing anything.

    On success this: rebases the story's branch onto the current default
    branch, force-pushes it (--force-with-lease) to origin, polls real CI
    (gh pr checks) — auto-retrying once on a cancelled run, and failing
    closed on a fail/cancelled/still-pending result — re-runs the story's
    acceptance fixtures and a build check against the rebased code, then
    merges the PR, deletes the branch/worktree, marks the story "done" in
    the manifest and its ticket, and notifies the user to restart the MCP
    server if the story touched the pipeline's own source. Any failure at
    any of those steps aborts the merge and returns the specific reason
    instead of partially completing it.

    This does more than a plain `gh pr merge` (which skips the rebase,
    force-push, and re-verification) — prefer this tool over a manual
    merge for exactly that reason. It force-pushes and merges regardless
    of any local test run you've done yourself, so only call it once you
    actually want this specific story merged now; there is no separate
    confirmation step after this call.
    """
    return _service.approve_merge(plan_name, story_key)




def pause_plan(plan_name: str) -> dict[str, Any]:
    """
    Stop advance_pipeline/advance_all_plans from touching this one plan -
    no new dispatch, review, or merge - while leaving every other ingested
    plan's scheduler ticks unaffected. Any story currently in_progress is
    interrupted (checkpointed and left resumable) so a paused plan isn't
    quietly burning usage in the background. Resume with resume_plan.
    """
    return _service.pause_plan(plan_name)


def resume_plan(plan_name: str) -> dict[str, Any]:
    """Clear a pause set by pause_plan so this plan's stories are eligible
    for dispatch/review/merge on the next advance_pipeline tick again."""
    return _service.resume_plan(plan_name)


def advance_all_plans() -> dict[str, Any]:
    """
    Run advance_pipeline on every plan that has been ingested (has a
    manifest), keyed by plan name. Plans saved but not yet ingested (no
    manifest) are skipped. Intended for a recurring scheduler (cron/launchd
    or /loop) so newly ingested plans are picked up automatically with no
    hardcoded plan name to maintain.

    NOTE on zombie reaping: the per-plan advance_pipeline polling phase
    already handles dead-pid in_progress stories via check_story_status
    (which falls through to test-running on dead pids). Running an external
    reap pass BEFORE the polling would clobber that and silently leave
    stories re-dispatching forever without ever running the test
    (manifest observation 2026-06-28: 3 e2e stories hit dispatch_attempts=
    MISSING because the reap ate the polling opportunity). The reap helper
    _reap_zombie_in_progress_stories is kept for callers that need a
    one-shot cleanup (e.g. tests, ops CLI) but is NOT wired in here.
    """
    return _service.advance_all_plans()


def record_security_audit(repo_root: str, sha: str | None = None) -> dict[str, Any]:
    """Record that a repository was security-audited at a given commit.

    Parameters:
        repo_root: Absolute path to an existing directory containing the git repo.
        sha: Optional 4-64 hex-character commit id. Defaults to HEAD.

    Returns:
        On success, ``{"ok": True, "repo_root": ..., "last_audited_sha": ...,
        "last_audited_at": ...}``. On failure, ``{"ok": False, "error": ...}``.
    """
    if not isinstance(repo_root, str) or not os.path.isabs(repo_root) or not os.path.isdir(repo_root):
        return {"ok": False, "error": f"repo_root must be an absolute path to an existing directory: {repo_root!r}"}
    if sha is not None and (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{4,64}", sha)):
        return {"ok": False, "error": f"sha must be 4-64 hex characters: {sha!r}"}
    target = sha if sha is not None else "HEAD"
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"could not resolve sha {target!r}: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "error": f"could not resolve sha {target!r}"}
    full_sha = proc.stdout.strip()
    state = security_audit.record_audit(paths.PLAN_DIR, repo_root, full_sha, datetime.now(timezone.utc))
    return {"ok": True, **state}
