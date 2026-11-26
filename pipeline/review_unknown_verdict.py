"""UNKNOWN-verdict handling for `review_story`.

Moved verbatim out of `pipeline.review_orchestrator.review_story` (issue
ohc-07). An UNKNOWN verdict is never an approval: each phase here either
returns a finished result dictionary (the review was deferred, or recorded as
inconclusive) or hands back a new `(verdict, reviewer_output)` pair for the
checks that follow.

Every orchestrator-owned name is read through `pipeline.review_orchestrator`
at call time (`_home.<name>`), because tests patch those names there.
"""

from __future__ import annotations

from typing import Any


def defer_rate_limited(
    verdict, reviewer_output, *, plan_name, story_key, story, manifest, worktree,
    branch, plan_role_config, routing_kw, cid_kwargs, prior_kw,
) -> dict[str, Any] | tuple[str, str]:
    """Defer (or park) a rate-limited review; after enough deferrals, fall back."""
    from . import review_orchestrator as _home

    # FM-B: a rate-limit response from the reviewer is an infrastructure event,
    # not a genuine review cycle. Leave the story at tests_passed so the next
    # advance_pipeline tick retries review once the backend recovers. Do NOT
    # touch rework_attempts — burning the rework budget on rate-limits parks
    # correct implementations silently.
    if verdict == "UNKNOWN" and _home._is_rate_limited(reviewer_output):
        story["review_deferred_count"] = story.get("review_deferred_count", 0) + 1
        fallback_mode = (
            _home.os.environ.get("PIPELINE_REVIEW_FALLBACK", "off").strip().lower()
        )
        fallback_after = int(_home.os.environ.get("PIPELINE_REVIEW_FALLBACK_AFTER", "3"))
        if (
            fallback_mode in _home._LOCAL_BACKEND_NAMES
            and story["review_deferred_count"] >= fallback_after
        ):
            _home._notify_user(
                plan_name,
                f"{story_key} review falling back to {fallback_mode} backend "
                f"after {story['review_deferred_count']} rate-limited attempts.",
                **cid_kwargs,
            )
            reviewer_output = _home._run_reviewer(
                worktree,
                branch,
                backend_name=fallback_mode,
                plan_role_config=plan_role_config, **routing_kw,
                since_sha=story.get("last_reviewed_sha"),
                risk=story.get("risk", "low"),
                **prior_kw,
            )
            verdict = _home._parse_verdict(reviewer_output)
            # Fall through into the normal verdict-handling code below —
            # this is a genuine review attempt now, not a deferral.
        else:
            _defer_limit = _home.review_defer_park_after()
            if 0 < _defer_limit <= story["review_deferred_count"]:
                return _home.park_rate_limited_review(
                    plan_name, story_key, story, manifest, **cid_kwargs
                )
            _home._notify_user(
                plan_name,
                f"{story_key} review deferred: reviewer rate-limited; will retry next tick.",
                **cid_kwargs,
            )
            _home.commit_story(plan_name, story_key, story)
            return {"ok": True, "status": story["status"], "deferred": "rate_limited"}
    return verdict, reviewer_output


def retry_transient_backend(
    verdict, reviewer_output, *, plan_name, story_key, story, worktree, branch,
    plan_role_config, routing_kw, cid_kwargs, prior_kw, escalation_backend,
) -> dict[str, Any] | tuple[str, str]:
    """Re-invoke the reviewer once after a transient backend error, else defer."""
    from . import review_orchestrator as _home

    # Transient backend error (HTTP 5xx / connection reset or refused / timed
    # out): re-invoke the reviewer once inline. This is an infrastructure
    # hiccup, not a genuine review cycle, so it never increments
    # review_inconclusive_count; a retry that is still a transient failure is
    # deferred to the next tick just below.
    _transient_retried = False
    if verdict == "UNKNOWN" and _home._is_transient_backend_error(reviewer_output):
        _home._notify_user(
            plan_name, f"{story_key} review hit transient backend error; retrying once.",
            **cid_kwargs,
        )
        reviewer_output = (
            _home._run_reviewer(
                worktree,
                branch,
                backend_name=escalation_backend,
                plan_role_config=plan_role_config, **routing_kw,
                since_sha=story.get("last_reviewed_sha"),
                risk=story.get("risk", "low"),
                **prior_kw,
            )
            if story.get("escalated")
            else _home._run_reviewer(
                worktree,
                branch,
                plan_role_config=plan_role_config, **routing_kw,
                since_sha=story.get("last_reviewed_sha"),
                risk=story.get("risk", "low"),
                **prior_kw,
            )
        )
        verdict = _home._parse_verdict(reviewer_output)
        _transient_retried = True
    # A transport failure that survives the single inline retry is an
    # infrastructure event, not a review outcome: defer to the next tick like
    # the rate-limit path and never charge review_inconclusive_count.
    if (
        _transient_retried
        and verdict == "UNKNOWN"
        and _home._is_transient_backend_error(reviewer_output)
    ):
        story["review_deferred_count"] = story.get("review_deferred_count", 0) + 1
        _home._notify_user(
            plan_name,
            f"{story_key} review deferred: reviewer backend still failing after "
            f"one retry (transient transport error); will retry next tick.",
            **cid_kwargs,
        )
        _home.commit_story(plan_name, story_key, story)
        return {"ok": True, "status": story["status"], "deferred": "transient_backend"}
    return verdict, reviewer_output


def record_inconclusive(
    verdict, reviewer_output, *, plan_name, story_key, story, cid_kwargs,
) -> dict[str, Any]:
    """Count an UNKNOWN verdict as inconclusive; park (or escalate) at the cap."""
    from . import review_orchestrator as _home

    # A non-rate-limited UNKNOWN is inconclusive, not a rejection: don't touch
    # review_feedback or rework_attempts, and leave status at its pre-review
    # value so the next advance_pipeline tick retries review. Fail closed -
    # this must never fall through to the APPROVE branch. Only after repeated
    # inconclusive attempts does it park for a human.
    inconclusive = story.get("review_inconclusive_count", 0) + 1
    story["review_inconclusive_count"] = inconclusive
    if inconclusive >= _home.REVIEW_INCONCLUSIVE_MAX:
        if (
            _home._auto_escalation_enabled()
            and not story.get("escalated")
            and _home._escalation_target() != (story.get("backend"), story.get("model"))
        ):
            _home._escalate_review_to_claude(
                story,
                story_key,
                plan_name,
                f"review inconclusive after {inconclusive} attempts",
            )
            # status stays at its pre-review value (e.g. tests_passed) -
            # the next tick retries review, now resolved via Claude.
        else:
            story["status"] = "parked"
            story["parked_reason"] = (
                f"review inconclusive after {inconclusive} attempts - needs human review"
            )
            # Diagnostic-only: the raw reason a VERDICT line never
            # appeared (step-cap, truncation, a swallowed backend
            # exception, or a genuinely empty response) is otherwise
            # lost the moment this story parks - nothing else persists
            # reviewer_output on the inconclusive path, so a human
            # investigating later has no way to tell which of those it
            # was without re-running the review. Bounded to keep the
            # manifest small; not used by any control-flow logic.
            story["last_inconclusive_output_excerpt"] = (
                reviewer_output[:500] if reviewer_output else "(empty response)"
            )
            _home._notify_user(
                plan_name,
                f"{story_key} parked: review inconclusive after "
                f"{inconclusive} attempts - needs human review.",
                event="story_parked",
                story_key=story_key,
                **cid_kwargs,
            )
    else:
        _home._notify_user(plan_name, f"{story_key} review inconclusive; will retry.", **cid_kwargs)
    _home.commit_story(plan_name, story_key, story)
    return {"ok": True, "verdict": verdict, "status": story["status"]}


def record_empty_request_changes(
    verdict, reviewer_output, *, plan_name, story_key, story, cid_kwargs,
) -> dict[str, Any]:
    """Count a content-free REQUEST_CHANGES as inconclusive; park (or escalate) at the cap.

    T11: a REQUEST_CHANGES with no substantive findings text is not a
    genuine rejection - it gives the redispatched agent nothing to fix, and
    treating it as one silently burns the rework budget on nothing (the
    2026-07-02 gpt-oss run parked a story this way). Route it through the
    same inconclusive-handling shape as UNKNOWN above - before the
    review_inconclusive_count reset that follows, so repeated empty responses
    still accumulate toward REVIEW_INCONCLUSIVE_MAX - but leave the verdict
    itself visible and never touch rework_attempts/review_feedback. Checked
    separately from the UNKNOWN handling because it applies equally to a
    content-free REQUEST_CHANGES from either the ordinary reviewer or a
    security-reviewer override.
    """
    from . import review_orchestrator as _home

    inconclusive = story.get("review_inconclusive_count", 0) + 1
    story["review_inconclusive_count"] = inconclusive
    if inconclusive >= _home.REVIEW_INCONCLUSIVE_MAX:
        if (
            _home._auto_escalation_enabled()
            and not story.get("escalated")
            and _home._escalation_target() != (story.get("backend"), story.get("model"))
        ):
            _home._escalate_review_to_claude(
                story,
                story_key,
                plan_name,
                f"review inconclusive after {inconclusive} attempts (empty REQUEST_CHANGES)",
            )
        else:
            story["status"] = "parked"
            story["parked_reason"] = (
                f"review inconclusive after {inconclusive} attempts - needs human review"
            )
            story["last_inconclusive_output_excerpt"] = (
                reviewer_output[:500] if reviewer_output else "(empty response)"
            )
            _home._notify_user(
                plan_name,
                f"{story_key} parked: review inconclusive after "
                f"{inconclusive} attempts - needs human review.",
                event="story_parked",
                story_key=story_key,
                **cid_kwargs,
            )
    else:
        _home._notify_user(
            plan_name,
            f"{story_key} review approved-changes-requested-empty: "
            f"REQUEST_CHANGES with no findings text; will retry.",
            **cid_kwargs,
        )
    _home.commit_story(plan_name, story_key, story)
    return {"ok": True, "verdict": verdict, "status": story["status"]}
