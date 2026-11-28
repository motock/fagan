"""Review orchestration: `_verify_reviewer_auto_fix` and the `review_story`
implementation, moved VERBATIM from `pipeline/server.py` (issue 35c9ee87).

The `@mcp.tool() def review_story` shim stays in `pipeline/server.py` (it is
the MCP-registered entrypoint). This module holds the real implementation,
aliased as `_original_review_story`, which `PipelineService.review_story`
calls via a live `pipeline.server` reference.

Monkeypatch compatibility: the test suite patches module globals on
`pipeline.server` (e.g. `_run_reviewer`, `_open_pr`, `_atomic_write_json`,
`REWORK_MAX_ATTEMPTS`, `subprocess`, ...). The moved body reads those names as
bare module globals, so each is bound here to a `_ServerRef` that resolves the
LIVE `pipeline.server` value at call time - a `monkeypatch.setattr(pipeline.server,
"NAME", ...)` therefore still lands. `_verify_reviewer_auto_fix` is defined in
this module (a real function, re-exported through `pipeline.server`); the one
call site inside `review_story` goes through `pipeline.server` so tests that
patch it land too.
"""

import subprocess as _subprocess  # the real module; `subprocess` below is a _ServerRef
from datetime import UTC, datetime
from pathlib import Path as _Path  # likewise; `Path` below is a _ServerRef
from typing import Any

from . import (
    git_ops,
    parsers,
    review_approve_path,
    review_high_risk,
    review_scope_gate,
    review_unknown_verdict,
)
from .branch_names import story_branch

# The names below are not called here any more: the phase modules
# (review_scope_gate, review_unknown_verdict, review_high_risk,
# review_approve_path) read them back through this module at call time, so a
# test that patches one of them on `pipeline.review_orchestrator` still lands.
from .ci import settings_for_plan  # noqa: F401
from .module_ref import _ModuleRef
from .parsers import _is_transient_backend_exception  # noqa: F401
from .pr import _pr_title_mismatch_message, _resolve_story_branch
from .review import _first_review_base, build_review_story_context  # noqa: F401
from .review_autofix import _verify_reviewer_auto_fix  # noqa: F401
from .review_deferral import park_rate_limited_review, review_defer_park_after  # noqa: F401
from .review_logic import (  # noqa: F401
    REVIEW_LOGIC_FINGERPRINT_KEY,
    is_unchanged_since_review,
    review_logic_fingerprint,
)
from .review_open_pr import handle_open_pr_failure  # noqa: F401
from .review_orchestrator_comments import _format_security_comment  # noqa: F401
from .review_refs import _ServerRef
from .review_verdict_binding import bind_verdict
from .role_routing_scope import layer_routing_for_plan, layer_routing_kwargs
from .scope_gate import SCOPE_GATE_HEADER, SCOPE_GATE_REMEDY, check_branch_scope  # noqa: F401
from .story_commit import commit_story

# Server-sourced names the moved body references as free variables. Each
# resolves to the live ``pipeline.server`` binding at call time so
# ``monkeypatch.setattr(pipeline.server, "NAME", ...)`` still lands.
_validate_key = _ServerRef("_validate_key")
_store = _ServerRef("_store")
_notify_user = _ServerRef("_notify_user")
_plan_role_config = _ServerRef("_plan_role_config")
_synthesize_test_failure_feedback = _ServerRef("_synthesize_test_failure_feedback")
_run_reviewer = _ServerRef("_run_reviewer")
_escalation_target = _ServerRef("_escalation_target")
_parse_verdict = _ServerRef("_parse_verdict")
_is_rate_limited = _ServerRef("_is_rate_limited")
_is_transient_backend_error = _ServerRef("_is_transient_backend_error")
_run_security_reviewer = _ServerRef("_run_security_reviewer")
_auto_escalation_enabled = _ServerRef("_auto_escalation_enabled")
_escalate_review_to_claude = _ServerRef("_escalate_review_to_claude")
_has_review_findings = _ServerRef("_has_review_findings")
_is_test_file_path = _ServerRef("_is_test_file_path")
_open_pr = _ServerRef("_open_pr")


def _unaddressed_findings(findings, changed_files, worktree):
    """Prior Blocking findings not addressed: untouched, non-test, still differing from base. Fails open on doubt (P0-MOOT-01)."""
    base = git_ops._resolve_base_branch(_Path(worktree), "")
    out = []
    for f in findings:
        if _is_test_file_path(f) or f in changed_files:
            continue
        if base and _Path(worktree, f).exists():
            try:
                r = _subprocess.run(["git", "diff", "--quiet", f"origin/{base}", "--", f], cwd=worktree, check=False, capture_output=True)
                if r.returncode == 0:
                    continue
            except OSError:
                pass
        out.append(f)
    return out


def _warn_on_pr_title_mismatch(plan_name, story_key, story, worktree, **kwargs):
    """Warn-only: never alters the PR, the verdict, or the returned pr_url."""
    try:
        message = _pr_title_mismatch_message(
            worktree, _resolve_story_branch(worktree, story_key), story.get("summary", ""))
        if message:
            _notify_user(plan_name, f"{story_key}: {message}", story_key=story_key,
                         severity="warning", event="pr_title_mismatch", **kwargs)
    except Exception:  # noqa: BLE001 - advisory check must not affect the gate
        logging.getLogger("pipeline").debug("pr_title_mismatch warning failed", exc_info=True)

_reverify_acceptance = _ModuleRef("pipeline.ci", "_reverify_acceptance")
_extract_blocking_finding_files = _ServerRef("_extract_blocking_finding_files")
_extract_suggested_commit_message = _ServerRef("_extract_suggested_commit_message")
_post_pr_comment = _ServerRef("_post_pr_comment")
_format_review_comment = _ServerRef("_format_review_comment")
detect_test_command = _ServerRef("detect_test_command")
backend = _ServerRef("backend")
subprocess = _ServerRef("subprocess")
os = _ServerRef("os")
logging = _ServerRef("logging")
traceback = _ServerRef("traceback")
Path = _ServerRef("Path")
REVIEWER_AUTO_FIX_MAX_FILES = _ServerRef("REVIEWER_AUTO_FIX_MAX_FILES")
REVIEWER_AUTO_FIX_MAX_LINES = _ServerRef("REVIEWER_AUTO_FIX_MAX_LINES")
REVIEW_INCONCLUSIVE_MAX = _ServerRef("REVIEW_INCONCLUSIVE_MAX")
REWORK_MAX_ATTEMPTS = _ServerRef("REWORK_MAX_ATTEMPTS")
REWORK_MAX_ATTEMPTS_ESCALATED = _ServerRef("REWORK_MAX_ATTEMPTS_ESCALATED")
REWORK_MAX_ATTEMPTS_ORACLE = _ServerRef("REWORK_MAX_ATTEMPTS_ORACLE")
_LOCAL_BACKEND_NAMES = _ServerRef("_LOCAL_BACKEND_NAMES")


def skip_info(story: dict, current_sha: str) -> dict:
    """Explain the skip; `to_force` names `pipeline.unpark`, the only route that can clear `last_reviewed_sha`."""
    return {"ok": True, "status": story["status"], "skipped": "unchanged_since_last_review", "reason": "head_and_review_logic_unchanged", "head": current_sha[:12], "last_reviewed": story.get("last_reviewed_sha", "")[:12], "stored_verdict": story.get("review_verdict") or "", "to_force": "push a new commit, or clear last_reviewed_sha with `python -m pipeline.unpark <plan> <story-key> --clear last_reviewed_sha`"}


def review_story(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Run the code-reviewer persona over a dispatched story's branch. On APPROVE,
    open a PR via gh and set status to pr_open; otherwise set status to
    changes_requested. Does not merge — merge is the overlord's decision.

    Only reviewable when story["status"] == "tests_passed" - any other status
    (a stale/duplicate call, e.g. a second tick racing an already-merged
    story) is a no-op skip; see README.md's "Review & merge" section.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    manifest = _store.get_manifest(plan_name)
    story = manifest["stories"].get(story_key)
    if not story:
        return {"ok": False, "error": f"No such story {story_key}"}

    branch = story_branch(story_key)
    worktree = story.get("worktree", "")
    # W4L-04: stamp every story-scoped notification with the correlation_id
    # dispatch minted for this story. Older manifests predate the field - the
    # kwargs are then omitted entirely so legacy records keep their exact
    # shape (absent key, never null). _rework_kwargs additionally carries the
    # dispatch attempt counter on the rework path's emit points.
    _cid = story.get("correlation_id")
    _cid_kwargs = {"correlation_id": _cid} if _cid else {}
    _rework_kwargs = (
        {**_cid_kwargs, "attempt": story.get("dispatch_attempts", 0)}
        if _cid
        else {}
    )
    # Guard: skip if story is not in tests_passed state
    if story.get("status") != "tests_passed":
        _notify_user(
            plan_name,
            f"{story_key} review skipped: status {story.get('status')!r} - only stories with status 'tests_passed' are reviewable.",
            **_cid_kwargs,
        )
        return {
            "ok": True,
            "status": story.get("status"),
            "skipped": "not_reviewable_state",
        }
    if story.get("last_reviewed_sha"):
        if not worktree or not os.path.isdir(worktree):
            pass
        else:
            try:
                current_sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=worktree,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if is_unchanged_since_review(story, current_sha):
                    # Once per SHA: the scheduler re-enters this path every tick
                    # and the condition does not resolve on its own.
                    if story.get("skip_notified_sha") != current_sha:
                        _notify_user(
                            plan_name,
                            f"{story_key} review skipped: HEAD unchanged since the last REQUEST_CHANGES ({current_sha[:9]}) - a redispatch/rework must land a new commit before re-review.",
                            **_cid_kwargs,
                        )
                        story["skip_notified_sha"] = current_sha
                    commit_story(plan_name, story_key, story)
                    return skip_info(story, current_sha)
            except (subprocess.CalledProcessError, OSError):
                pass

    plan_role_config = _plan_role_config(plan_name)
    # Resolved ONCE, in the PLAN's own scope, for every reviewer call below.
    # layer_routing_kwargs adds no keyword for an unset ({}) scope, so a plan
    # with no team routing keeps its call shape; a configured one changes it.
    layer_routing = layer_routing_for_plan(plan_name)
    _routing_kw = layer_routing_kwargs(layer_routing)
    # Captured before invoking the reviewer: a later APPROVE_WITH_FIX is
    # verified against it, and the verdict is bound to the sha the reviewer
    # actually saw (issue 9012b28e). None -> unverifiable; never crash review.
    before_sha = None
    if worktree and os.path.isdir(worktree):
        try:
            before_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, OSError):
            before_sha = None
    first = review_scope_gate.first_review(
        plan_name=plan_name, story_key=story_key, story=story, manifest=manifest,
        worktree=worktree, branch=branch, plan_role_config=plan_role_config,
        routing_kw=_routing_kw, cid_kwargs=_cid_kwargs,
        before_sha=before_sha,
    )
    if first.result is not None:
        return first.result
    reviewer_output, prior_kw = first.reviewer_output, first.prior_kw
    verdict = _parse_verdict(reviewer_output)

    outcome = review_unknown_verdict.defer_rate_limited(
        verdict, reviewer_output, plan_name=plan_name, story_key=story_key,
        story=story, manifest=manifest, worktree=worktree, branch=branch,
        plan_role_config=plan_role_config, routing_kw=_routing_kw,
        cid_kwargs=_cid_kwargs, prior_kw=prior_kw,
    )
    if isinstance(outcome, dict):
        return outcome
    verdict, reviewer_output = outcome
    outcome = review_unknown_verdict.retry_transient_backend(
        verdict, reviewer_output, plan_name=plan_name, story_key=story_key,
        story=story, worktree=worktree, branch=branch,
        plan_role_config=plan_role_config, routing_kw=_routing_kw,
        cid_kwargs=_cid_kwargs, prior_kw=prior_kw,
        escalation_backend=first.escalation_backend,
    )
    if isinstance(outcome, dict):
        return outcome
    verdict, reviewer_output = outcome

    # Reviewer self-fix (2026-07-29): the reviewer's own "trivial and
    # confident" self-assessment is never trusted alone - re-verify risk,
    # diff size, and the full test suite before honoring it. Unverified ->
    # REQUEST_CHANGES, so an unverifiable self-fix can't loop forever.
    if verdict == "APPROVE_WITH_FIX":
        import pipeline.server as _server

        verdict, reviewer_output = _server._verify_reviewer_auto_fix(
            worktree,
            story,
            reviewer_output,
            before_sha,
        )

    bind_verdict(story, verdict, before_sha, datetime.now(UTC))
    story.update(review_deferred_count=0, **({"review_rebind_attempts": 0} if verdict == "APPROVE" else {}))  # a fresh APPROVE restarts the merge gate's stale-approval re-review ladder (b4)

    # High-risk stories require an additional security-engineer pass; both
    # must APPROVE before the story proceeds to pr_open.
    security_output = None
    if verdict == "APPROVE" and story.get("risk") == "high":
        secured = review_high_risk.run_security_review(
            verdict, reviewer_output, plan_name=plan_name, story_key=story_key,
            story=story, worktree=worktree, branch=branch,
            plan_role_config=plan_role_config, routing_kw=_routing_kw,
            cid_kwargs=_cid_kwargs,
        )
        if isinstance(secured, dict):
            return secured
        verdict, reviewer_output, security_output = secured

    # A non-rate-limited UNKNOWN is inconclusive, not a rejection: it never
    # falls through to the APPROVE branch below.
    if verdict == "UNKNOWN":
        return review_unknown_verdict.record_inconclusive(
            verdict, reviewer_output, plan_name=plan_name, story_key=story_key,
            story=story, cid_kwargs=_cid_kwargs,
        )

    # T11: a content-free REQUEST_CHANGES is inconclusive, not a rejection (see
    # review_unknown_verdict.record_empty_request_changes); it never burns rework.
    if verdict == "REQUEST_CHANGES" and (not _has_review_findings(reviewer_output) or (len(reviewer_output) > parsers.UNFOCUSED_REQUEST_CHANGES_MIN_CHARS and not parsers.findings_are_parseable(reviewer_output))):
        return review_unknown_verdict.record_empty_request_changes(
            verdict, reviewer_output, plan_name=plan_name, story_key=story_key,
            story=story, cid_kwargs=_cid_kwargs,
        )

    story["review_inconclusive_count"] = 0

    # Mode 24/28 finding-target guard: an APPROVE that never touched the files
    # flagged by the prior REQUEST_CHANGES is downgraded (see review_approve_path).
    if verdict == "APPROVE":
        verdict, reviewer_output = review_approve_path.downgrade_unaddressed_approve(
            verdict, reviewer_output, story_key=story_key, story=story, worktree=worktree,
        )

    if verdict == "APPROVE":
        early = review_approve_path.finish_approved(
            plan_name=plan_name, story_key=story_key, story=story, worktree=worktree,
            security_output=security_output, cid_kwargs=_cid_kwargs,
        )
    else:
        early = review_approve_path.finish_rework(
            verdict, reviewer_output, plan_name=plan_name, story_key=story_key,
            story=story, manifest=manifest, worktree=worktree, before_sha=before_sha,
            rework_kwargs=_rework_kwargs,
        )
    if early is not None:
        return early
    commit_story(plan_name, story_key, story)
    return {
        "ok": True,
        "verdict": verdict,
        "status": story["status"],
        "pr_url": story.get("pr_url"),
    }


# Preserve original review_story implementation
_original_review_story = review_story
