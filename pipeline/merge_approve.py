"""Phase functions behind ``pipeline.merge._approve_merge_impl``.

Moved out of ``pipeline/merge.py`` to keep that file under its line cap. Tests
patch names on ``pipeline.merge`` and on ``pipeline.server``; every such name
is looked up at call time through ``_home`` (``pipeline.merge``) or a
function-level ``from .server import`` so those patches keep taking effect.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import merge as _home


@dataclass(frozen=True)
class MergeContext:
    """The manifest state one approval works against, re-read inside the lock."""

    plan_name: str
    story_key: str
    manifest: dict[str, Any]
    story: dict[str, Any]


@dataclass(frozen=True)
class GateOutcome:
    """What the merge gate hands to the post-merge steps."""

    settings: Any
    mcp_touched: Any


def preflight_refusal(plan_name: str, story_key: str) -> dict[str, Any] | None:
    """Refuse a bound namespace, bad keys or an unauthorized caller."""
    from .authorization import authorize
    from .server import _store, _validate_key

    # The file layout holds only the default partition end to end, so a bound
    # request is refused up front; a record store proceeds in the bound
    # namespace through bound_records.
    if _home.bound_records.refuses_bound_namespace(_store):
        return {"ok": False, "error": "unknown namespace"}

    # Validate the caller-supplied keys BEFORE anything reads or writes a path
    # built from them. ``authorize`` is not inert on bad input: it reads the
    # manifest and, on denial, appends to the plan's notification sidecar, so a
    # traversal in ``plan_name`` must be rejected before it gets that far.
    _validate_key(plan_name)
    _validate_key(story_key)

    return authorize("story:merge", plan_name=plan_name, story_key=story_key)


def plan_busy_refusal() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "plan busy (scheduler tick in progress); retry",
        "retriable": True,
    }


def load_context(
    plan_name: str, story_key: str
) -> tuple[MergeContext | None, dict[str, Any] | None]:
    """Re-read the manifest inside the lock and check the story may merge."""
    from . import access_hooks
    from .server import _store

    # Re-read the manifest from disk INSIDE the lock so we merge against
    # the freshest on-disk state, not a pre-lock stale copy. A scheduler
    # tick may have changed the story's status or verdict while we waited
    # to acquire the lock.
    manifest = _store.get_manifest(plan_name)
    story = manifest["stories"].get(story_key)
    if not story:
        return None, {"ok": False, "error": f"No such story {story_key}"}
    if story["status"] not in ("parked", "pr_open"):
        return None, {
            "ok": False,
            "error": f"Story is {story['status']}, not parked/pr_open",
        }
    if story.get("review_verdict") != "APPROVE":
        return None, {"ok": False, "error": "Story was never reviewer-approved"}

    # The repo access hook is consulted before any rebase, push or merge.
    refusal = access_hooks.merge_refusal(plan_name, story)
    if refusal is not None:
        return None, refusal
    return MergeContext(plan_name, story_key, manifest, story), None


def _gate_refusal(ctx: MergeContext, error: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "story_key": ctx.story_key}


def _bind_branch(ctx: MergeContext) -> tuple[str, dict[str, Any] | None]:
    """Resolve the branch to merge and check the approval covers its head."""
    from .pr import _resolve_story_branch
    from .review_merge_binding import branch_head_sha, check_with_worktree, refuse

    # Mode 9 gate applies here too: even an explicit human merge must
    # not land a conflicting or CI-red PR. The merge_ci_gate setting
    # (resolved per namespace, team and repo) turns it off, and only
    # if you intentionally accept that.
    worktree = ctx.story.get("worktree", "")
    # Resolve the worktree's ACTUAL HEAD branch (a rework round can
    # leave it on an alias agent/<key>-<suffix>) so rebase -> push
    # -> CI -> _merge_pr all operate on the one branch _merge_pr
    # merges. The hardcoded convention name previously pushed a
    # branch a prior _merge_pr had already deleted ("src refspec
    # does not match any") or a stale twin, and CI-polled a SHA
    # that was never pushed to it. Resolve only when there is a
    # worktree to probe; a missing/anomalous worktree degrades to
    # the convention branch without paying a subprocess.
    branch = _home.story_branch(ctx.story_key)
    if worktree and Path(worktree).is_dir():
        branch = _resolve_story_branch(worktree, ctx.story_key)
    # The approval must cover the commit being merged: probe the
    # remote tip AFTER fetch and BEFORE the merge-time rebase, and
    # refuse here so nothing is rebased or pushed first.
    head = branch_head_sha(branch)
    binding = check_with_worktree(ctx.story, head, worktree)
    if not binding["ok"]:
        return branch, refuse(ctx.plan_name, ctx.story_key, ctx.story, head, binding)
    return branch, None


def _rebase(ctx: MergeContext, branch: str) -> dict[str, Any] | None:
    from .server import _default_branch, _notify_user, _rebase_onto_master

    rb = _rebase_onto_master(ctx.story.get("worktree", ""), branch)
    if rb.get("auto_resolved"):
        # W4L-04: stamp the story's persisted correlation_id
        # (omitted entirely for older manifests - absent, not
        # null).
        _cid = ctx.story.get("correlation_id")
        _notify_user(
            ctx.plan_name,
            f"{ctx.story_key} rebase auto-resolved an "
            f"additive-import conflict against "
            f"origin/{_default_branch()}.",
            **({"correlation_id": _cid} if _cid else {}),
            event="rebase_auto_resolved",
        )
    if not rb["ok"]:
        return _gate_refusal(ctx, f"rebase failed: {rb['error']}")
    return None


def _force_push(ctx: MergeContext, branch: str) -> tuple[str, dict[str, Any] | None]:
    """Force-push the rebased branch; return the pushed sha or a refusal."""
    from .review_merge_binding import rebind_after_pipeline_push

    worktree = ctx.story.get("worktree", "")
    if not Path(worktree).is_dir():
        return "", None
    push = _home.subprocess.run(
        ["git", "push", "--force-with-lease", "origin", branch],
        check=False,
        cwd=worktree,
        capture_output=True,
        text=True,
        **_home.forge_auth.env_kw(),
    )
    if push.returncode != 0:
        return "", _gate_refusal(
            ctx, f"push failed: {(push.stderr or push.stdout).strip()[:200]}"
        )
    # Mode 26: pin to the exact commit that was just pushed,
    # not the branch name - see the matching comment in
    # advance_pipeline's own merge adjudication above.
    rev = _home.subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    pushed_sha = rev.stdout.strip()
    # The checkpoint/rebase/force-push above move the head after
    # the review by design: re-bind at the pipeline's own commit
    # point so a later retry does not deny known-good work.
    rebind_after_pipeline_push(ctx.story, pushed_sha)
    return pushed_sha, None


def _ci_gate(
    ctx: MergeContext, branch: str, pushed_sha: str
) -> tuple[Any, dict[str, Any] | None]:
    """Poll CI (with the one-shot cancelled rerun) and persist the result."""
    from .server import _ci_rerun, _ci_status, _store

    story = ctx.story
    settings = _home.context_from_manifest(ctx.manifest)
    ci = _ci_status(branch, sha=pushed_sha, settings=settings)
    if ci["state"] == "cancelled" and not story.get("ci_rerun_attempted"):
        # Same one-shot auto-rerun as the scheduler's merge gate:
        # a queue-delay cancellation carries no code-quality
        # signal, so give it one automatic retry before failing.
        story["ci_rerun_attempted"] = True
        _ci_rerun(pushed_sha)
        ci = _ci_status(branch, sha=pushed_sha, settings=settings)
    # Record the gate's final result for every risk level; persisted
    # now because the blocking returns below skip the final write.
    story["pr_checks"] = ci
    story.pop("pr_checks_error", None)
    _home.bound_records.put_manifest(_store, ctx.plan_name, ctx.manifest)
    if ci["state"] in ("fail", "cancelled"):
        return settings, _gate_refusal(ctx, f"CI failing: {ci['error']}")
    if ci["state"] == "pending":
        return settings, _gate_refusal(ctx, f"CI still pending: {ci['error']}")
    if ci["state"] == "unreadable":
        return settings, _gate_refusal(ctx, f"ci unreadable: {ci['error']}")
    return settings, None


def _reverify(ctx: MergeContext, settings: Any) -> dict[str, Any] | None:
    from .server import _reverify_acceptance, _reverify_build

    worktree = ctx.story.get("worktree", "")
    acc = _reverify_acceptance(ctx.story, worktree, ctx.story_key, settings=settings)
    if acc["state"] == "fail":
        return _gate_refusal(ctx, f"acceptance reverify fail: {acc['error']}")
    build = _reverify_build(worktree, settings=settings)
    if build["state"] == "fail":
        return _gate_refusal(ctx, f"build reverify fail: {build['error']}")
    return None


def _merge_pr_step(ctx: MergeContext) -> Any:
    from .server import _default_branch, _mcp_self_source_touched, _merge_pr

    worktree = ctx.story.get("worktree", "")
    # _merge_pr removes the worktree and deletes the branch, so the
    # self-source diff must be taken BEFORE the merge, not after.
    mcp_touched = _mcp_self_source_touched(worktree, f"origin/{_default_branch()}")
    _merge_pr(ctx.story.get("worktree", ""), ctx.story_key)
    return mcp_touched


def _gate_steps(ctx: MergeContext) -> tuple[GateOutcome | None, dict[str, Any] | None]:
    branch, refusal = _bind_branch(ctx)
    if refusal is not None:
        return None, refusal
    refusal = _rebase(ctx, branch)
    if refusal is not None:
        return None, refusal
    pushed_sha, refusal = _force_push(ctx, branch)
    if refusal is not None:
        return None, refusal
    settings, refusal = _ci_gate(ctx, branch, pushed_sha)
    if refusal is not None:
        return None, refusal
    refusal = _reverify(ctx, settings)
    if refusal is not None:
        return None, refusal
    return GateOutcome(settings, _merge_pr_step(ctx)), None


def run_merge_gate(
    ctx: MergeContext,
) -> tuple[GateOutcome | None, dict[str, Any] | None]:
    """Rebase, push, CI, reverify and merge; any failure becomes a refusal."""
    from .server import _scoped_repo_root

    try:
        with _scoped_repo_root(ctx.plan_name):
            return _gate_steps(ctx)
    except Exception as e:  # noqa: BLE001 (surface the gh/git failure to the human, don't raise)
        return None, {"ok": False, "error": str(e), "story_key": ctx.story_key}


def record_merged(ctx: MergeContext, outcome: GateOutcome) -> None:
    """Final write and notifications, run INSIDE the plan lock."""
    from .plan_completion import notify_if_plan_completed
    from .server import _notify_user, _store

    plan_name, story_key, story = ctx.plan_name, ctx.story_key, ctx.story
    # Final write INSIDE the lock, using the manifest re-read inside the
    # lock (not a pre-lock copy). Clear parked_reason on leaving 'parked'.
    story["status"] = "done"
    story.pop("parked_reason", None)
    story.pop("ci_rerun_attempted", None)
    _home.bound_records.put_manifest(_store, plan_name, ctx.manifest)
    _home.record_audit(
        plan_name,
        action="story.merge",
        outcome="allowed",
        target={"plan": plan_name, "story": story_key},
        detail={
            "branch": _home.story_branch(story_key),
            "gates": _home.gate_record(outcome.settings),
        },
    )

    _notify_user(
        plan_name,
        f"{story_key} merged",
        story_key=story_key,
        event="story_merged",
        **(
            {"correlation_id": story["correlation_id"]}
            if story.get("correlation_id")
            else {}
        ),
    )

    try:
        notify_if_plan_completed(plan_name, ctx.manifest, just_merged=story_key)
    except Exception:
        logging.getLogger("pipeline.merge").exception(
            "notify_if_plan_completed failed for %s", plan_name
        )
    _home._maybe_record_retro(plan_name, ctx.manifest)


def finish_merge(ctx: MergeContext, outcome: GateOutcome) -> dict[str, Any]:
    """Post-lock steps: triage resolution, Plane close, MCP restart notice."""
    from .server import _mark_plane_done, _mcp_restart_notice, _notify_user

    _home.record_resolution_if_ruled(
        ctx.plan_name,
        ctx.story_key,
        human_action="merge_held",
        new_status="done",
        principal=_home.actor_id(),
    )
    _mark_plane_done(ctx.story_key, ctx.plan_name)
    if outcome.mcp_touched:
        _notify_user(ctx.plan_name, _mcp_restart_notice(outcome.mcp_touched))
    return {"ok": True, "story_key": ctx.story_key, "status": "done"}
