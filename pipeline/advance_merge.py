"""Advance-owned merge-hold re-adjudication.

The names this module reads that are bound at module level in
``pipeline.advance`` (``PIPELINE_AUTONOMY``, ``_merge_decision``,
``_merge_mod``, ``merge_adjudication_plan``) are resolved through
``pipeline.advance`` at call time via ``_ModuleRef``, so
``monkeypatch.setattr(pipeline.advance, NAME, ...)`` keeps landing.
"""

import logging
import os  # noqa: F401 - names stay importable from here
import subprocess  # noqa: F401
import time
from datetime import UTC, datetime  # noqa: F401
from pathlib import Path
from typing import Any

from . import (
    access_hooks,  # noqa: F401 - names stay importable from here
    bound_records,
    external_merge,  # noqa: F401
    merge_local_checks,
    merge_reverify,
)
from .ci import (
    SettingsContext,
    gate_record,  # noqa: F401 - stays importable from here
    settings_for_plan,
)
from .concurrency import _released_plan_lock  # noqa: F401 - the readiness phase reads it here
from .host_state import build_host_state
from .merge_local_checks import (  # re-exported for pipeline.advance_merge callers
    MERGE_REVERIFY_TIMEOUT_DEFAULT,  # noqa: F401
)
from .merge_readjudication import (  # re-exported for pipeline.advance
    _MERGE_HOLD_REASON,  # noqa: F401
    _readjudicate_parked_merge_hold,  # noqa: F401 - patched here; the hold phase calls it through this module
)
from .merge_reverify import (
    cancel_merge_reverify,
    collect_merge_reverify,
    start_merge_reverify,
)
from .module_ref import _ModuleRef
from .rebase_conflict_feedback import (  # re-exported: _conflict_rework_max etc. are used by callers and tests
    _CI_FEEDBACK_MARKER,  # noqa: F401
    _PRIOR_FEEDBACK_HEADER,  # noqa: F401
    CONFLICT_REWORK_MAX_DEFAULT,  # noqa: F401
    _conflict_rework_max,  # noqa: F401 - the gate-failure phase reads these through this module
    _merge_ci_rework_feedback,  # noqa: F401
    conflict_rework_feedback,  # noqa: F401
    parse_conflict_files,  # noqa: F401
)
from .telemetry import record_merge_result  # noqa: F401

MERGE_MAX_ATTEMPTS = _ModuleRef("pipeline.advance", "MERGE_MAX_ATTEMPTS")

# The verdicts the detached re-verify job can report (see
# ``pipeline.merge_reverify``); anything else fails closed.
_REVERIFY_PASS = "pass"
_REVERIFY_FAIL = "fail"


def _merge_readiness_timeout(settings: SettingsContext) -> float:
    """Wall-clock budget for one merge-readiness poll, in seconds.

    The plan's resolved ``merge_readiness_timeout_s`` (default 180), read
    for the plan's (namespace, team, repo) on every call so a settings change
    applies to the next tick without a restart.
    """
    return float(settings.value("merge_readiness_timeout_s"))


def _merge_readiness_forge():
    """The active forge, or None when it cannot be resolved at all.

    Resolved lazily (the repo root is read at call time) so a missing forge
    degrades to "no readiness verb available" instead of raising inside the
    merge gate. ``None`` means the host cannot be interrogated here at all -
    that is NOT the fail-closed case (a resolved forge whose answer cannot be
    READ is, and that one is ``pending``); it is simply "this deployment has no
    forge adapter", where the pre-existing exception path remains the only
    signal there is.
    """
    try:
        from .forge import get_forge
        from .pr import _repo_root

        return get_forge(_repo_root())
    except Exception:
        logging.getLogger("pipeline").debug(
            "forge unavailable for merge readiness", exc_info=True
        )
        return None


def _await_merge_readiness(
    forge, worktree: str, branch: str, settings: SettingsContext
) -> str:
    """Poll the host until it has FINISHED computing this PR's mergeability.

    Returns ``"ready"``, ``"blocked"`` or ``"pending"`` (the budget expired
    while the host was still computing). Bounded backoff: a first 5-second
    delay doubling to a 30-second cap, every sleep clamped to what is left of
    the budget, so a 180-second budget reads at t=0,5,15,35,65,95,125,155,180
    and sleeps exactly 180 seconds in total.

    An unreadable answer (any exception, including ``OSError`` and a timeout)
    is ``pending``: the host has not finished computing, which is emphatically
    not the same as a refusal. A forge that predates the verb is ``"ready"`` -
    that keeps every caller that cannot answer on today's behaviour.
    """
    read = getattr(forge, "merge_readiness", None)
    if read is None:
        return "ready"
    if not worktree or not Path(worktree, ".git").exists():
        # No real checkout to interrogate (the worktree is gone or was never a
        # git worktree): there is no PR to read a mergeability for, so behave
        # exactly as before this gate existed and let the merge's own failure
        # be the signal.
        return "ready"
    deadline = time.monotonic() + _merge_readiness_timeout(settings)
    ci_gate_on = bool(settings.value("merge_ci_gate"))
    delay = 5.0
    while True:
        try:
            state = read(worktree, branch, ci_gate_on=ci_gate_on)
        except Exception:  # noqa: BLE001 - unreadable is pending, never ready
            state = "pending"
        if state == "ready":
            return "ready"
        if state == "blocked":
            return "blocked"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "pending"
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 30.0)

_atomic_write_json = _ModuleRef("pipeline.advance", "_atomic_write_json")
_ci_pending_expired = _ModuleRef("pipeline.advance", "_ci_pending_expired")
_ci_rerun = _ModuleRef("pipeline.advance", "_ci_rerun")
_ci_rework_feedback = _ModuleRef("pipeline.advance", "_ci_rework_feedback")
_default_branch = _ModuleRef("pipeline.advance", "_default_branch")
_degraded_ci_branch = _ModuleRef("pipeline.advance", "_degraded_ci_branch")
_mark_plane_done = _ModuleRef("pipeline.advance", "_mark_plane_done")
_maybe_record_retro = _ModuleRef("pipeline.advance", "_maybe_record_retro")
_mcp_restart_notice = _ModuleRef("pipeline.advance", "_mcp_restart_notice")
_mcp_self_source_touched = _ModuleRef("pipeline.advance", "_mcp_self_source_touched")
_merge_gate_ci_status = _ModuleRef("pipeline.advance", "_merge_gate_ci_status")
_merge_pr = _ModuleRef("pipeline.advance", "_merge_pr")
_notify_user = _ModuleRef("pipeline.advance", "_notify_user")
_rebase_and_push_for_merge = _ModuleRef("pipeline.advance", "_rebase_and_push_for_merge")
_reverify_acceptance = _ModuleRef("pipeline.advance", "_reverify_acceptance")
_reverify_build = _ModuleRef("pipeline.advance", "_reverify_build")
_store = _ModuleRef("pipeline.advance", "_store")

PIPELINE_AUTONOMY = _ModuleRef("pipeline.advance", "PIPELINE_AUTONOMY")
_merge_decision = _ModuleRef("pipeline.advance", "_merge_decision")
_merge_mod = _ModuleRef("pipeline.advance", "_merge_mod")
merge_adjudication_plan = _ModuleRef("pipeline.advance", "merge_adjudication_plan")


def _merge_reverify_gate(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    worktree: str,
    pushed_sha: str,
    summary: dict[str, Any],
) -> tuple[str, bool]:
    """Gate one story's merge on a re-verify of ``pushed_sha``.

    Returns ``(gate_error, wait)``. ``wait`` means "no verdict this tick": the
    story stays ``pr_open`` and nothing is merged, and the caller's manifest
    write still happens - exactly the ``ci_wait`` contract above it.

    The guarantee is unchanged: no merge without a passing re-verify of the
    exact SHA being merged. Only the *placement* varies. Under pytest, and for
    an operator who sets ``PIPELINE_MERGE_REVERIFY_DETACHED=0``, the re-verify
    runs inline here; otherwise it runs as a detached job started on one tick
    and collected on a later one, so a full suite plus a build no longer
    blocks the advance tick.
    """
    local = story.get("local_checks")
    if (
        isinstance(local, dict)
        and local.get("state") == "pass"
        and local.get("sha") == pushed_sha
    ):
        # The pre-ruling local checks already ran tests, build and lint on this
        # exact commit and passed: re-running them would grade nothing new.
        return "", False

    if not merge_reverify.detached_enabled():
        # Independent of review: re-run the acceptance oracle against the
        # just-rebased branch right before merging. Closes the gap CI alone
        # can't (a repo without CI, or a CI-independent slip between
        # tests_passed and review).
        gate_error = ""
        settings = settings_for_plan(plan_name)
        acc = _reverify_acceptance(story, worktree, key, settings=settings)
        if acc["state"] == "fail":
            gate_error = f"acceptance reverify fail: {acc['error']}"
        if not gate_error:
            # Independent of tests: a green suite doesn't mean the
            # project actually builds (PR #48 merged with `npm run
            # build` broken - retro §3.1).
            build = _reverify_build(worktree, settings=settings)
            if build["state"] == "fail":
                gate_error = f"build reverify fail: {build['error']}"
        return gate_error, False

    job = story.get("merge_reverify")
    if job and job.get("sha") != pushed_sha:
        # The branch head moved since this job started: its verdict grades a
        # commit that is no longer the one being merged, so it can never
        # authorise this merge.
        cancel_merge_reverify(job)
        story.pop("merge_reverify", None)
        job = None
    if not job:
        state_root = build_host_state().job_state_root()
        try:
            job = start_merge_reverify(plan_name, key, worktree, state_root)
        except (ValueError, OSError) as exc:
            # The exception's text can carry a path, so only its class name
            # reaches the gate message.
            return (
                "merge re-verify could not start: " + type(exc).__name__,
                False,
            )
        story["merge_reverify"] = {**job, "sha": pushed_sha}
        summary.setdefault("reverify_pending", []).append(key)
        return "", True

    res = collect_merge_reverify(job, merge_local_checks.reverify_timeout())
    if res is None:
        summary.setdefault("reverify_pending", []).append(key)
        return "", True
    story.pop("merge_reverify", None)
    if isinstance(res.get("last_test_check"), dict):
        story["last_test_check"] = res["last_test_check"]
    # The state is read before the SHA. An error - or any state this module
    # does not recognise - is never a pass: fail closed, and report the job's
    # own error. A job that died before it graded a commit reports sha None;
    # checking the SHA first would misreport that as a commit mismatch and hide
    # the real error. Keep the tail of the job log for the operator.
    if res.get("state") not in (_REVERIFY_PASS, _REVERIFY_FAIL):
        story["merge_reverify_log_tail"] = res.get("log_tail", "")
        return (
            "merge re-verify harness error: "
            + res.get("error", "unrecognised re-verify result"),
            False,
        )
    # Only a pass or a fail reaches the SHA check. Equality, not presence: a
    # result that does not name the SHA it graded (missing key, null, or a
    # document written by another version or a sibling writer) cannot
    # authorise a merge of this commit. Treating a missing ``sha`` as "no
    # mismatch" would fail open - ``collect`` only checks ``state`` - and would
    # let any ``{"state": "pass"}`` document in the job dir merge an unverified
    # branch.
    if res.get("sha") != pushed_sha:
        return "merge re-verify graded a different commit", False
    if res.get("state") == _REVERIFY_PASS:
        return "", False
    # A red gate is an ordinary gate failure: it counts against
    # MERGE_MAX_ATTEMPTS and retries like a transient merge failure.
    return res.get("error", "merge re-verify failed"), False


def _adjudicate_merges(plan_name: str, summary: dict[str, Any]) -> None:
    # Phase modules read names back from this module, so import them lazily.
    from .advance_merge_ci_gate import run_ci_gate
    from .advance_merge_gate_failure import handle_gate_failure
    from .advance_merge_hold import check_review_binding, evaluate_merge_hold
    from .advance_merge_land import await_merge_readiness, land_story

    # The merge gates this tick reads, resolved once from the plan manifest so
    # every gate call and the merge record name the same (namespace, team, repo).
    settings = settings_for_plan(plan_name)

    # 3. Adjudicate merges for reviewed PRs (no model usage; runs even paused).
    manifest = bound_records.get_manifest(_store, plan_name)
    if manifest is None:
        raise FileNotFoundError("manifest not found")
    stories = manifest["stories"]
    for key in list(stories):
        # Re-read per key: a readiness poll below reloads the manifest.
        story = stories.get(key)
        if story is None:
            continue
        if evaluate_merge_hold(plan_name, key, story, manifest, summary):
            continue

        # Mode 9: rebase onto current origin/master + CI gate before merge,
        # so a stale-base branch can't land cross-story breakage or a
        # ruff-red PR onto main. Failures count against merge_attempts just
        # like a transient `gh pr merge` failure (see MERGE_MAX_ATTEMPTS).
        worktree = story.get("worktree", "")
        # Resolve the worktree's ACTUAL HEAD branch (a rework round can
        # leave it on an alias agent/<key>-<suffix>) so the gate's rebase,
        # push, CI poll and _merge_pr all operate on the one branch
        # _merge_pr merges. The hardcoded convention name previously
        # named a branch a prior _merge_pr had already deleted ("src
        # refspec ... does not match any") or a stale twin, and the CI
        # poll queried a SHA that was never pushed to it. The resolver
        # itself fails open to the convention name when the worktree
        # cannot be probed, so no local fallback is needed here - and
        # none may be added: a locally computed convention branch is the
        # exact mistake the round-2 review finding names.
        from .pr import _resolve_story_branch

        if worktree and Path(worktree).is_dir():
            branch = _resolve_story_branch(worktree, key)
        else:
            # No worktree to probe (missing/anomalous): nothing was
            # dispatched, so no alias can exist. Hand the gate NO
            # branch at all - a locally computed convention branch is
            # the exact mistake the round-2 review finding names, and
            # the gate's own is_dir guard skips rebase/push for a
            # missing worktree without spawning a subprocess (the
            # CI-gate-disabled path must run zero subprocesses, see
            # test_advance_pipeline_ci_gate_disabled_skips_ci).
            branch = ""
        if check_review_binding(plan_name, key, story, branch, summary):
            continue
        gate = run_ci_gate(plan_name, key, story, branch, worktree, settings)
        gate_error = gate.gate_error
        pushed_sha = gate.pushed_sha
        ci_definitive_fail = gate.definitive_fail
        ci_wait = gate.wait
        if ci_wait:
            summary["ci_pending"].append(key)
            continue
        if not gate_error:
            gate_error, reverify_wait = _merge_reverify_gate(
                plan_name, key, story, worktree, pushed_sha, summary
            )
            if reverify_wait:
                # The detached re-verify is still running: nothing is merged
                # and the manifest write at the end of this function still
                # persists the job handle. Same contract as ``ci_wait``.
                continue

        if gate_error:
            handle_gate_failure(
                plan_name, key, story, gate_error, ci_definitive_fail, settings, summary
            )
            continue

        ready = await_merge_readiness(
            plan_name, key, story, manifest, worktree, branch, settings, summary
        )
        manifest = ready.manifest
        stories = manifest["stories"]
        story = ready.story
        if ready.finished:
            continue
        land_story(plan_name, key, story, manifest, worktree, settings, summary)
    bound_records.put_manifest(_store, plan_name, manifest)
