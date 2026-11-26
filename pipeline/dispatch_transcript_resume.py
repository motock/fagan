"""Transcript-resume phase carved out of ``pipeline.dispatch``.

Holds the ``if resume_via_transcript:`` block of ``_dispatch_story_impl``: a
rework redispatch on the local driver resumes the prior message transcript and
appends the reviewer's feedback (optionally translated into a fix checklist,
optionally preceded by a freshly authored regression test).

The caller holds ``_plan_lock``; nothing here touches it. Names tests patch on
``pipeline.dispatch`` are reached through that module at call time.
"""

import json
from pathlib import Path
from typing import Any


def apply_transcript_resume(
    story: dict[str, Any],
    dispatch_kwargs: dict[str, Any],
    *,
    plan_name: str,
    story_key: str,
    worktree_path: Path,
    dispatch_backend: str,
    local_model: str,
    review_feedback: str,
    transcript_path: Path,
    revised_instructions_note: str,
) -> None:
    """Set ``resume_transcript_path`` and ``resume_append_content`` on the kwargs."""
    from . import dispatch as _home

    dispatch_kwargs["resume_transcript_path"] = transcript_path
    # Same tech-lead-decomposition logic as the initial checklist,
    # applied to review feedback: a reviewer's prose diagnosis is
    # itself a coarse brief for a weak executor. Re-run per rework
    # cycle (unlike the initial checklist, which plans once) since
    # each cycle's feedback is different. Fails open to the raw
    # feedback format on any planner failure - identical contract
    # to the initial-dispatch checklist.
    fix_checklist = None
    if dispatch_backend in _home._LOCAL_BACKEND_NAMES:
        fix_checklist = _home._run_rework_planner(
            review_feedback,
            dispatch_backend=dispatch_backend,
            local_model=local_model,
            plan_role_config=_home._plan_role_config(plan_name),
            worktree=str(worktree_path),
        )
    rework_tests_note = _rework_tests_note(
        story,
        plan_name=plan_name,
        story_key=story_key,
        worktree_path=worktree_path,
        dispatch_backend=dispatch_backend,
        local_model=local_model,
        review_feedback=review_feedback,
        fix_checklist=fix_checklist,
    )
    rework_attempts = story.get("rework_attempts", 0)
    if rework_attempts >= 2:
        round_prefix = (
            f"This is rework round {rework_attempts}. A previous attempt "
            f"already redispatched on this same feedback and did not fully "
            f"resolve it -- read the feedback below carefully rather than "
            f"repeating the same partial fix. "
        )
    else:
        round_prefix = ""
    feedback_note, feedback_lead = _feedback_wording(transcript_path, review_feedback)
    if fix_checklist:
        dispatch_kwargs["resume_append_content"] = (
            f"{round_prefix}"
            "The code reviewer REQUESTED CHANGES on your previous "
            "attempt. Your tech lead has translated the feedback "
            f"into a fix checklist:\n{fix_checklist}\n\n"
            f"{feedback_note}"
            f"{revised_instructions_note}"
            f"{rework_tests_note}"
        )
    else:
        dispatch_kwargs["resume_append_content"] = (
            f"{round_prefix}"
            f"{feedback_lead}"
            f"{revised_instructions_note}"
            f"{rework_tests_note}"
        )


def _rework_tests_note(
    story: dict[str, Any],
    *,
    plan_name: str,
    story_key: str,
    worktree_path: Path,
    dispatch_backend: str,
    local_model: str,
    review_feedback: str,
    fix_checklist: str | None,
) -> str:
    """Author the reviewer-requested regression test(s); return the steering note.

    Rework test-author phase: when the reviewer's feedback itself
    calls for NEW test(s) (e.g. a regression test reproducing a
    named bug), author them via the dedicated test_author role
    BEFORE the main executor redispatch, then steer the executor
    to implement against those already-committed tests instead of
    writing the tests itself - applying the tech-lead/weak-
    executor split (TDD_SPLIT_PRODUCTION_PLAN.md) one rework cycle
    later. Gated to local-family dispatch (Claude doesn't need the
    crutch) and to a per-sha "already done" marker so a
    retried/resumed dispatch on the same reviewer verdict never
    re-runs the phase or re-asks the classifier. Fails open: on
    any failure, unconfigured role, or "no new tests required",
    the note stays "" and the resumed prompt + manifest
    are byte-for-byte identical to today's monolithic rework
    dispatch.
    """
    from . import dispatch as _home

    rework_tests_committed = False
    if dispatch_backend in _home._LOCAL_BACKEND_NAMES:
        already_done_sha = story.get("rework_test_author_done_for_sha")
        current_sha = story.get("last_reviewed_sha")
        if (
            current_sha
            and already_done_sha != current_sha
            and _home._rework_requires_new_tests(
                review_feedback,
                dispatch_backend=dispatch_backend,
                local_model=local_model,
                plan_role_config=_home._plan_role_config(plan_name),
            )
        ):
            rework_tests_committed = _home._run_rework_test_author_phase(
                story,
                story_key=story_key,
                worktree_path=worktree_path,
                dispatch_backend=dispatch_backend,
                local_model=local_model,
                review_feedback=review_feedback,
                fix_checklist=fix_checklist,
                plan_role_config=_home._plan_role_config(plan_name),
                layer_routing=_home.layer_routing_for_plan(plan_name),
            )
            if rework_tests_committed:
                story["rework_test_author_done_for_sha"] = current_sha
    if not rework_tests_committed:
        return ""
    return (
        "\n\nThe regression test(s) reproducing this bug have already "
        "been written and committed to this branch by your tech lead. "
        "Do NOT create, write, or modify any test file. "
        f"{_home._NEVER_TOUCH_TESTS_STEERING} Run them to see the current "
        "failures, then implement until they pass."
    )


def _feedback_wording(transcript_path: Path, review_feedback: str) -> tuple[str, str]:
    """Return ``(feedback_note, feedback_lead)``, deduplicating a seeded feedback.

    Token dedup (measured 2026-09-04: each rework cycle re-injected
    the full ~7-8K-char feedback into the SAME resumed transcript,
    ~22K chars over 3 rounds). If this exact feedback is already in
    the resumed transcript from a prior cycle, reference it instead
    of re-embedding it. Fail OPEN to re-injection on any read/parse
    problem — a token optimization must never break dispatch.
    """
    feedback_already_seeded = False
    try:
        prior_msgs = json.loads(transcript_path.read_text())
        feedback_already_seeded = any(
            isinstance(m.get("content"), str) and review_feedback in m["content"]
            for m in prior_msgs
        )
    except Exception:  # noqa: BLE001 (fail open to re-injection; a token optimization must never break dispatch)
        feedback_already_seeded = False
    if feedback_already_seeded:
        note = (
            "Original review feedback: unchanged from your previous "
            "rework cycle — it is already in your transcript above; "
            "re-read it there."
        )
        lead = (
            "The code reviewer REQUESTED CHANGES on your previous attempt. "
            "Address the review feedback already in your transcript above "
            "(unchanged from your previous rework cycle)."
        )
        return note, lead
    note = f"Original review feedback (for reference):\n{review_feedback}"
    lead = (
        "The code reviewer REQUESTED CHANGES on your previous attempt. "
        f"Address this feedback:\n{review_feedback}"
    )
    return note, lead
