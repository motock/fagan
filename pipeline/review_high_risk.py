"""The security-engineer pass `review_story` requires for a high-risk APPROVE.

Moved verbatim out of `pipeline.review_orchestrator.review_story` (issue
ohc-07). Returns either a finished result dictionary (the pass was deferred:
rate limit, transport or backend error, or no verdict) or the
`(verdict, reviewer_output, security_output)` the approve path continues with.
Nothing here can turn a non-APPROVE into an APPROVE.

Every orchestrator-owned name is read through `pipeline.review_orchestrator`
at call time (`_home.<name>`), because tests patch those names there.
"""

from __future__ import annotations

from typing import Any


def run_security_review(
    verdict, reviewer_output, *, plan_name, story_key, story, worktree, branch,
    plan_role_config, routing_kw, cid_kwargs,
) -> dict[str, Any] | tuple[str, str, str]:
    """Run the security pass; both reviewers must APPROVE to keep the verdict."""
    from . import review_orchestrator as _home

    try:
        security_output = _home._run_security_reviewer(
            worktree, branch, since_sha=story.get("last_reviewed_sha"),
            plan_role_config=plan_role_config, **routing_kw,
        )
    except _home.backend.RateLimitedError:
        # OA2-04: mirror the main reviewer's rate-limit deferral. A 429 on
        # the security pass is an infrastructure event, not a review cycle:
        # defer and retry on the next tick, never burn the rework or
        # inconclusive budget.
        _home._notify_user(
            plan_name,
            f"{story_key} security review deferred: reviewer rate-limited; will retry next tick.",
            **cid_kwargs,
        )
        _home.commit_story(plan_name, story_key, story)
        return {"ok": True, "status": story["status"], "deferred": "rate_limited"}
    except Exception as e:  # noqa: BLE001 (defense in depth, per the comment below)
        if _home._is_transient_backend_exception(e):
            # A security-reviewer transport failure (timeout, refused or
            # reset connection) is infrastructure, not a review outcome:
            # defer like the rate-limit path and never charge
            # review_inconclusive_count. Only the exception TYPE reaches
            # logs and the notification; its text may carry sensitive detail.
            _home.logging.getLogger("pipeline").warning(
                "%s/%s security review deferred: reviewer transport failure %s",
                plan_name, story_key, type(e).__name__,
            )
            _home._notify_user(
                plan_name,
                f"{story_key} security review deferred: reviewer backend "
                f"transport failure ({type(e).__name__}); will retry next tick.",
                **cid_kwargs,
            )
            _home.commit_story(plan_name, story_key, story)
            return {"ok": True, "status": story["status"], "deferred": "transient_backend"}
        # Defense in depth: the security backend's own internal error (a bad
        # tool-call shape, a malformed backend response, ...) must not crash
        # the pipeline process or abort the plan's review pass for the tick.
        # Defer like the rate-limit path (never treat this as an APPROVE -
        # fail closed) and never charge review_inconclusive_count. Log a
        # full traceback server-side at ERROR (never INFO/below - see
        # Observability & Logging) so it stays diagnosable without exposing
        # exception text to the operator-facing notification.
        _home.logging.getLogger("pipeline").error(
            f"{plan_name}/{story_key} security review raised {type(e).__name__}:\n"
            f"{_home.traceback.format_exc()}"
        )
        _home._notify_user(
            plan_name,
            f"{story_key} security review deferred: unexpected "
            f"{type(e).__name__}; will retry next tick.",
            **cid_kwargs,
        )
        _home.commit_story(plan_name, story_key, story)
        return {"ok": True, "status": story["status"], "deferred": "backend_error"}
    security_verdict = _home._parse_verdict(security_output)

    # FM-B/OA2-04: a non-verdict from the security reviewer is an
    # infrastructure event, not a rejection. Defer like a rate limit - never
    # fall through to the rework routing below (which would burn the rework
    # budget) and never charge review_inconclusive_count. A rate-limit text
    # response keeps its own sentinel; any other non-verdict defers too.
    if security_verdict == "UNKNOWN":
        deferred_reason = (
            "rate_limited" if _home._is_rate_limited(security_output) else "unknown_verdict"
        )
        _home._notify_user(
            plan_name,
            f"{story_key} security review deferred: reviewer returned no "
            f"verdict; will retry next tick.",
            **cid_kwargs,
        )
        _home.commit_story(plan_name, story_key, story)
        return {"ok": True, "status": story["status"], "deferred": deferred_reason}

    story["security_review_verdict"] = security_verdict
    if security_verdict != "APPROVE":
        verdict = security_verdict
        reviewer_output = security_output  # use security feedback for rework
    return verdict, reviewer_output, security_output
