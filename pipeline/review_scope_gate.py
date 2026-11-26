"""First review phase of `review_story`: the scope gate or the reviewer call.

Moved verbatim out of `pipeline.review_orchestrator.review_story` (issue
ohc-07). Exactly one of three things produces the first `reviewer_output`:

1. the scope gate (a story that declares `files` and changed production paths
   outside them is sent back without an LLM review),
2. the synthesized test-failure feedback (`skip_llm_reviewer`), or
3. the reviewer itself, whose rate-limit and transport failures defer the
   review (a finished result dictionary) instead of producing an output.

Every orchestrator-owned name is read through `pipeline.review_orchestrator`
at call time (`_home.<name>`), because tests patch those names there.
"""

from __future__ import annotations

from typing import Any, NamedTuple


class FirstReview(NamedTuple):
    """Outcome of the first review phase.

    `result` is a finished `review_story` result dictionary when the review
    was deferred or parked (the caller returns it unchanged); otherwise None
    and `reviewer_output` is the text the verdict is parsed from.
    `prior_kw` and `escalation_backend` are the reviewer-call inputs the later
    inline retries re-use.
    """

    result: dict[str, Any] | None
    reviewer_output: str
    prior_kw: dict[str, Any]
    escalation_backend: Any


def scope_gate_output(scope_violations, *, plan_name, story_key, cid_kwargs) -> str:
    """Synthesize the REQUEST_CHANGES text for an out-of-scope branch."""
    from . import review_orchestrator as _home

    reviewer_output = (
        _home.SCOPE_GATE_HEADER + "\n"
        + "\n".join(f"- Blocking: {v}" for v in scope_violations)
        + "\n" + _home.SCOPE_GATE_REMEDY + "\n"
        + "VERDICT: REQUEST_CHANGES"
    )
    _home._notify_user(
        plan_name,
        f"{story_key} scope gate: {len(scope_violations)} out-of-scope path(s); sent back without an LLM review.",
        event="scope_gate_failed",
        story_key=story_key,
        **cid_kwargs,
    )
    return reviewer_output


def run_reviewer_phase(
    *, plan_name, story_key, story, manifest, worktree, branch,
    plan_role_config, routing_kw, cid_kwargs,
) -> FirstReview:
    """Call the reviewer; a rate limit or transport failure defers the review."""
    from . import review_orchestrator as _home

    # Mode 47: carry the prior cycle's findings into a RE-review so the
    # reviewer must discharge each one individually. Passed as a kwarg
    # only when there is actually prior feedback (a first review has
    # none), so the common path's call signature is unchanged. The
    # story's brief/sibling context rides in the same kwargs dict, so
    # the call signature is likewise unchanged when a story has no
    # brief.
    _prior_fb = story.get("review_feedback")
    prior_kw = {"prior_feedback": _prior_fb} if _prior_fb else {}
    _story_context = _home.build_review_story_context(story_key, manifest)
    if _story_context:
        # In-place key add, not a dict rebuild: every _run_reviewer call
        # site below spreads this same dict, so it stays the single
        # carrier of both kwargs.
        prior_kw["story_context"] = _story_context
    _escalation_backend = None
    try:
        # Once a story is escalated (see _escalate_review_to_claude below),
        # every subsequent review must go to the escalation target
        # regardless of the global PIPELINE_BACKEND_REVIEW setting - review
        # backend is otherwise resolved purely from that env var with no
        # per-story override, so this is the one seam that needs an explicit
        # check. The target is Claude by default; PIPELINE_ESCALATION_BACKEND
        # retargets it (e.g. to a non-Claude provider while Claude is capped).
        _escalation_backend = _home._escalation_target()[0]
        reviewer_output = (
            _home._run_reviewer(
                worktree,
                branch,
                backend_name=_escalation_backend,
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
    except _home.backend.RateLimitedError:
        # FM-B: an Ollama-cloud (or any Ollama-proxied) 429 on the review path
        # is an infrastructure event, not a real review cycle. Treat it the
        # same as Claude's weekly-usage pause: defer and retry on the next
        # tick, do NOT burn REVIEW_INCONCLUSIVE_MAX. Without this, a
        # misclassified rate-limit would eventually park a correct impl.
        story["review_deferred_count"] = story.get("review_deferred_count", 0) + 1
        _defer_limit = _home.review_defer_park_after()
        if 0 < _defer_limit <= story["review_deferred_count"]:
            return FirstReview(
                _home.park_rate_limited_review(
                    plan_name, story_key, story, manifest, **cid_kwargs
                ),
                "", prior_kw, _escalation_backend,
            )
        _home._notify_user(
            plan_name,
            f"{story_key} review deferred: local reviewer rate-limited; will retry next tick.",
            **cid_kwargs,
        )
        _home.commit_story(plan_name, story_key, story)
        return FirstReview(
            {"ok": True, "status": story["status"], "deferred": "rate_limited"},
            "", prior_kw, _escalation_backend,
        )
    except Exception as e:  # noqa: BLE001 (defense in depth, per the comment below)
        if _home._is_transient_backend_exception(e):
            # A reviewer transport failure (timeout, refused or reset
            # connection) is infrastructure, not a review outcome: defer
            # like the rate-limit path and never charge
            # review_inconclusive_count. Only the exception TYPE reaches
            # logs and the notification; its text may carry sensitive detail.
            _home.logging.getLogger("pipeline").warning(
                "%s/%s review deferred: reviewer transport failure %s",
                plan_name, story_key, type(e).__name__,
            )
            story["review_deferred_count"] = story.get("review_deferred_count", 0) + 1
            _home._notify_user(
                plan_name,
                f"{story_key} review deferred: reviewer backend transport "
                f"failure ({type(e).__name__}); will retry next tick.",
                **cid_kwargs,
            )
            _home.commit_story(plan_name, story_key, story)
            return FirstReview(
                {"ok": True, "status": story["status"], "deferred": "transient_backend"},
                "", prior_kw, _escalation_backend,
            )
        # Defense in depth: a reviewer backend's own internal error (a bad
        # tool-call shape, a malformed backend response, ...) must not crash
        # the pipeline process. Fail safe into the same UNKNOWN-verdict path
        # a genuinely inconclusive review already takes below - never treat
        # this as an APPROVE (fail-closed). The user-facing notification
        # names only the exception TYPE, not its text, which could carry
        # sensitive detail - but that also made the failure permanently
        # undiagnosable (observed live 2026-07-30: a RuntimeError here
        # could never be root-caused). Log a full traceback server-side
        # instead, at ERROR (never INFO/below - see Observability &
        # Logging), so it's available for investigation without exposing
        # exception text to the operator-facing notification.
        _home.logging.getLogger("pipeline").error(
            f"{plan_name}/{story_key} review raised {type(e).__name__}:\n"
            f"{_home.traceback.format_exc()}"
        )
        _home._notify_user(
            plan_name,
            f"{story_key} review failed with an unexpected "
            f"{type(e).__name__}; treating as inconclusive.",
            **cid_kwargs,
        )
        reviewer_output = ""
    return FirstReview(None, reviewer_output, prior_kw, _escalation_backend)


def first_review(
    *, plan_name, story_key, story, manifest, worktree, branch,
    plan_role_config, routing_kw, cid_kwargs, before_sha,
) -> FirstReview:
    """Scope gate, else synthesized test-failure feedback, else the reviewer."""
    from . import review_orchestrator as _home

    # Mode 40: a story routed to review via acceptance_failed_review
    # (PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1) whose last recorded test run
    # actually failed can't be meaningfully correctness-reviewed by the LLM
    # reviewer - a live incident showed the reviewer's own principal finding
    # was just restating the failing-test list check_story_status had
    # already recorded. Skip the reviewer call entirely and synthesize
    # REQUEST_CHANGES directly from that test output. Only fires when both
    # the flag AND a genuinely failing last_test_check are present - a
    # missing last_test_check, or one that passed (acceptance oracle failed
    # while the detected test command itself passed), falls through to the
    # normal reviewer call below.
    last_test_check = story.get("last_test_check") or {}
    # Staleness gate (2026-08-15): last_test_check records the worktree HEAD
    # sha at the moment the check ran. If the worktree has since moved to a
    # new commit (before_sha != recorded sha), the recorded failure may no
    # longer exist - do NOT trust it. Only take the skip fast path when the
    # recorded sha matches the current HEAD. A missing sha (stories written
    # before this fix) also falls through to the real reviewer.
    skip_llm_reviewer = (
        story.get("acceptance_failed_review")
        and last_test_check.get("returncode") not in (0, None)
        and last_test_check.get("sha") == before_sha
    )
    # Invariant: a story that declares `files` is never LLM-reviewed while its
    # branch changes production paths outside them.
    scope_violations = (
        _home.check_branch_scope(worktree, _home._first_review_base(worktree), story["files"])
        if isinstance(story.get("files"), list) and worktree and _home.os.path.isdir(worktree)
        else []
    )
    if scope_violations:
        return FirstReview(
            None,
            scope_gate_output(
                scope_violations, plan_name=plan_name, story_key=story_key,
                cid_kwargs=cid_kwargs,
            ),
            {}, None,
        )
    if skip_llm_reviewer:
        return FirstReview(
            None, _home._synthesize_test_failure_feedback(last_test_check), {}, None,
        )
    return run_reviewer_phase(
        plan_name=plan_name, story_key=story_key, story=story, manifest=manifest,
        worktree=worktree, branch=branch, plan_role_config=plan_role_config,
        routing_kw=routing_kw, cid_kwargs=cid_kwargs,
    )
