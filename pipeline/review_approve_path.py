"""The verdict-routing tail of `review_story`: open the PR, or send back.

Moved verbatim out of `pipeline.review_orchestrator.review_story` (issue
ohc-07). `downgrade_unaddressed_approve` first downgrades an APPROVE that skipped
its prior findings; `finish_approved` handles an APPROVE verdict (open the PR, clear the
stale rework state); `finish_rework` handles every other verdict (record the
feedback, count the rework cycle). Each returns a finished result dictionary
when it ends the review early, or None when `review_story` should persist the
story and return its ordinary result. `reverify_acceptance_feedback` (the
acceptance branch) and `route_by_rework_cap` (the rework-cap branch) are the
two larger steps of `finish_rework`.

Every orchestrator-owned name is read through `pipeline.review_orchestrator`
at call time (`_home.<name>`), because tests patch those names there.
"""

from __future__ import annotations

from typing import Any


def downgrade_unaddressed_approve(
    verdict, reviewer_output, *, story_key, story, worktree,
) -> tuple[str, str]:
    """Downgrade an APPROVE that never touched the files its predecessor flagged.

    Mode 24/28 finding-target guard: the Mode 27 same-SHA guard only
    catches a review call where HEAD is byte-identical to the last
    reviewed commit. A dispatch watchdog checkpoint commit changes HEAD's
    SHA trivially (a WIP commit) without addressing the reviewer's own
    prior Blocking findings, slipping past that guard and letting the
    reviewer silently APPROVE a diff that never touched the flagged
    file(s) - "merged-but-incomplete". If every file recorded from the
    prior REQUEST_CHANGES cycle's Blocking findings wasn't touched by the
    diff since then, downgrade this APPROVE back to REQUEST_CHANGES
    instead of opening a PR.

    Exception: a flagged TEST file is exempt from the "was it touched"
    check, because review_story only reaches this APPROVE branch when
    story["status"] == "tests_passed" - the full suite, including that
    test file, is provably green right now. That is strictly stronger,
    directly-verified proof the finding (a failing test) is resolved than
    "were the test file's own bytes touched" - a correct fix legitimately
    lands in the implementation the test exercises, not the test itself.
    Root-caused live 2026-07-24 (MODE40-CI-REWORK-FEEDBACK-V2): a
    gate-synthesized review flagged the failing test file, the agent fixed
    the bug in the implementation module, the suite went green and a real
    reviewer said APPROVE, but this guard downgraded it anyway - burning
    the story's entire rework budget on an already-resolved finding.
    Non-test findings (README prose, server.py logic) still require the flagged
    file to be touched OR to no longer differ from the base branch - the same
    proof as the test-file exemption. Fails open on base, OSError, returncode.
    """
    from . import review_orchestrator as _home

    if (
        verdict == "APPROVE"
        and story.get("last_reviewed_sha")
        and story.get("last_review_findings")
    ):
        try:
            diff_res = _home.subprocess.run(
                ["git", "diff", "--name-only", story["last_reviewed_sha"], "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            )
            changed_files = set(diff_res.stdout.splitlines())
            findings = story["last_review_findings"]
            untouched = _home._unaddressed_findings(findings, changed_files, worktree)
            for f in findings:
                if not _home._is_test_file_path(f) and f not in changed_files and f not in untouched:
                    _home.logging.getLogger("pipeline").info(
                        "Story %s: %s no longer differs from base branch", story_key, f)
            if untouched:
                verdict = "REQUEST_CHANGES"
                reviewer_output = (
                    "Prior Blocking finding(s) were never addressed - the "
                    "following file(s) flagged in an earlier review have not "
                    "been touched since:\n"
                    + "\n".join(
                        f"- Blocking: {p}: not addressed since the last review"
                        for p in untouched
                    )
                )
        except (_home.subprocess.CalledProcessError, OSError):
            # Fail open - this is a workflow-correctness gate, not a
            # security boundary, so an infra error must not block a
            # genuine APPROVE.
            pass
    return verdict, reviewer_output


def finish_approved(
    *, plan_name, story_key, story, worktree, security_output, cid_kwargs,
) -> dict[str, Any] | None:
    """Open the PR for an APPROVEd story; a failed open is bounded and parked."""
    from . import review_orchestrator as _home

    # A git-push failure (transient network/auth hiccup, or a race with
    # a concurrent rebase) must not crash the whole advance_pipeline
    # tick: this call sits inside the tests_passed review loop in
    # _advance_pipeline_locked, so an uncaught exception here aborts
    # review for every OTHER tests_passed story and skips merge
    # adjudication entirely for the rest of that tick (confirmed live in
    # advance-scheduler.err.log, story 30e5f9fc, 2026-08-19: an
    # unguarded CalledProcessError from this exact call propagated
    # through wake_handler and silently dropped the tick). A failed open
    # is counted and parks after a bounded number of tries, so a missing
    # token cannot re-run a paid review every tick (review_open_pr).
    try:
        pr_url = _home._open_pr(worktree, story_key, story)
        _home._warn_on_pr_title_mismatch(plan_name, story_key, story, worktree, **cid_kwargs)
    except (_home.subprocess.CalledProcessError, OSError) as exc:
        return _home.handle_open_pr_failure(plan_name, story_key, story, exc, **cid_kwargs)
    story["pr_url"] = pr_url
    story.pop("pr_open_failures", None)
    story["status"] = "pr_open"
    # The security-engineer pass's verdict otherwise lives only in the
    # manifest, so a reader of the PR sees no evidence it ran. Post it now
    # that the PR exists. A failed post must never change the story's
    # status: the PR is already open and the verdict is already recorded.
    if security_output is not None and worktree and _home.os.path.isdir(worktree):
        try:
            _home._post_pr_comment(worktree, _home._format_security_comment(security_output))
        except (_home.subprocess.CalledProcessError, OSError):
            _home._notify_user(
                plan_name,
                "security review comment not posted: the PR is already open and "
                "the security verdict is recorded in the manifest.",
                **cid_kwargs,
            )
    # The work passed: drop any stale rework state from earlier cycles.
    story.pop("review_feedback", None)
    story.pop("rework_attempts", None)
    # Clear any stored SHA when review is approved
    story.pop("last_reviewed_sha", None)
    story.pop(_home.REVIEW_LOGIC_FINGERPRINT_KEY, None)
    story.pop("skip_notified_sha", None)
    story.pop("last_review_findings", None)
    return None


def reverify_acceptance_feedback(
    reviewer_output, *, plan_name, story_key, story, worktree, rework_kwargs,
) -> str:
    """The rework feedback, prefixed with a warning when the oracle is green.

    Mode 20 (2026-07-17, verified by replay): a REQUEST_CHANGES verdict
    on an acceptance-bearing story can be correct about SOMETHING
    outside the oracle's scope while the oracle itself is currently
    green - and a whole-file rework, given only the reviewer's raw
    feedback, has no signal that it must not regress that already-
    correct behavior (observed: this exact gap let a rework destroy a
    passing backward-jump fix). Re-verify the oracle against the
    CURRENT worktree state before dispatching rework and, if it still
    passes, prepend an explicit warning. This does not change the
    verdict or control flow - the story still goes to rework - it only
    gives the next dispatch a fact the reviewer's own text can't convey.
    """
    from . import review_orchestrator as _home

    feedback = reviewer_output
    if story.get("acceptance"):
        oracle_now = _home._reverify_acceptance(
            story, worktree, story_key, settings=_home.settings_for_plan(plan_name)
        )
        if oracle_now.get("state") == "pass":
            feedback = (
                "NOTE: the acceptance oracle is currently PASSING against "
                "this worktree. The reviewer's feedback below may be about "
                "something outside the oracle's required behavior - do "
                "NOT regress the acceptance-oracle-passing behavior while "
                "addressing it, and re-run the acceptance tests after your "
                "change to confirm they are still green.\n\n" + reviewer_output
            )
        elif oracle_now.get("state") == "fail" and story.get("rework_attempts", 0) > 0:
            # Fresh-rework-on-regression (2026-07-29 gpt-oss E2E finding,
            # bcca562e/token_report): a rework redispatch that RESUMES
            # the prior dispatch's transcript (see resume_via_transcript
            # in dispatch_story) replays whatever churn led to this
            # state, which measurably compounds it - the same story
            # 500-died and regressed further (11/11 -> 9/11 acceptance)
            # resuming a poisoned transcript, then converged cleanly on
            # a FRESH rework once the transcript was deleted. Delete the
            # transcript so the next redispatch is forced onto
            # dispatch_story's from-scratch rework prompt.
            #
            # Gated on rework_attempts > 0, i.e. a rework has ALREADY
            # happened: a failing oracle alone is not a regression. With
            # PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1 the common path to
            # review-with-red-oracle is a FIRST dispatch that was simply
            # incomplete, and there the transcript is the richest context
            # a rework could resume from - discarding it would degrade
            # the common case to fix the rarer one. rework_attempts is
            # incremented by the caller after this step, so here it still
            # holds the count from PRIOR cycles.
            #
            # Best-effort: a missing/unwritable transcript must not block
            # the review outcome itself.
            transcript_path = _home.Path(worktree) / ".agent_transcript.json" if worktree else None
            if transcript_path and transcript_path.exists():
                try:
                    transcript_path.unlink()
                    _home._notify_user(
                        plan_name,
                        f"{story_key} rework regressed the acceptance "
                        f"oracle (was passing before this rework, now "
                        f"failing); deleted the dispatch transcript so the "
                        f"next attempt starts fresh instead of resuming "
                        f"the churn that caused it.",
                        **rework_kwargs,
                    )
                except OSError:
                    pass
    return feedback


def _auto_amend_commit_message(
    verdict, reviewer_output, *, plan_name, story_key, story, worktree, rework_kwargs,
) -> dict[str, Any] | None:
    """Amend HEAD when the reviewer's only finding is the commit message format."""
    from . import review_orchestrator as _home

    if (
        verdict == "REQUEST_CHANGES"
        and not story["last_review_findings"]
        and story.get("commit_hygiene_autofix_attempts", 0) < 2
    ):
        _suggested = _home._extract_suggested_commit_message(reviewer_output)
        if _suggested is not None and worktree and _home.os.path.isdir(worktree):
            try:
                _status_r = _home.subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                if _status_r.stdout.strip() == "":
                    try:
                        _home.subprocess.run(
                            ["git", "commit", "--amend", "-m", _suggested],
                            cwd=worktree,
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                        story["commit_hygiene_autofix_attempts"] = (
                            story.get("commit_hygiene_autofix_attempts", 0) + 1
                        )
                        story["status"] = "tests_passed"
                        _home._notify_user(
                            plan_name,
                            f"{story_key}: reviewer's only blocking finding was "
                            f"commit-message format; auto-amended HEAD commit "
                            f"without spending a rework attempt.",
                            **rework_kwargs,
                        )
                        _home.commit_story(plan_name, story_key, story)
                        return {
                            "ok": True,
                            "verdict": verdict,
                            "status": story["status"],
                            "auto_fixed_commit_message": True,
                        }
                    except (_home.subprocess.CalledProcessError, OSError):
                        pass
            except (_home.subprocess.CalledProcessError, OSError):
                pass
    return None


def route_by_rework_cap(
    reviewer_output, attempts, *, plan_name, story_key, story, manifest, worktree,
    rework_kwargs,
) -> None:
    """Escalate, park, or send back for rework depending on the rework budget."""
    from . import review_orchestrator as _home

    if story.get("escalated"):
        rework_cap = _home.REWORK_MAX_ATTEMPTS_ESCALATED
    elif story.get("acceptance"):
        rework_cap = _home.REWORK_MAX_ATTEMPTS_ORACLE
    else:
        rework_cap = _home.REWORK_MAX_ATTEMPTS
    if attempts >= rework_cap:
        if (
            _home._auto_escalation_enabled()
            and not story.get("escalated")
            and _home._escalation_target() != (story.get("backend"), story.get("model"))
        ):
            _home._escalate_review_to_claude(
                story,
                story_key,
                plan_name,
                f"rework budget exhausted after {attempts} review cycles",
            )
            # A redispatch will pick up the real review_feedback already
            # set above, now on Claude (story["backend"] was just set).
            story["status"] = "changes_requested"
        else:
            story["status"] = "parked"
            story["parked_reason"] = (
                f"rework budget exhausted after {attempts} review cycles"
            )
            _home._notify_user(
                plan_name,
                f"{story_key} parked: reviewer still requesting changes "
                f"after {attempts} cycles - needs human review.",
                event="story_parked",
                story_key=story_key,
                **rework_kwargs,
            )
    else:
        fre = manifest.get("final_rework_escalation") or {}
        if attempts == rework_cap - 1 and fre.get("enabled"):
            provider = fre.get("provider")
            if provider in {"claude", "local", "ollama", "lmstudio", "mlx"}:
                model = fre.get("model")
                story["backend"] = provider
                story["model"] = model
                _home._notify_user(
                    plan_name,
                    f"{story_key} final rework attempt ({attempts}/{rework_cap}) escalating to {provider}/{model}.",
                    story_key=story_key,
                    event="escalated",
                    **rework_kwargs,
                )
        story["status"] = "changes_requested"
        if worktree and _home.os.path.isdir(worktree):
            try:
                story["pr_url"] = _home._open_pr(worktree, story_key, story)
                _home._warn_on_pr_title_mismatch(plan_name, story_key, story, worktree, **rework_kwargs)
                _home._post_pr_comment(worktree, _home._format_review_comment(reviewer_output, attempts))
            except (_home.subprocess.CalledProcessError, OSError) as exc:
                _home._notify_user(plan_name, f"{story_key}: could not open PR / post review comment ({exc.__class__.__name__}); findings remain in review_feedback for the rework agent.", **rework_kwargs)


def finish_rework(
    verdict, reviewer_output, *, plan_name, story_key, story, manifest, worktree,
    before_sha, rework_kwargs,
) -> dict[str, Any] | None:
    """Record the feedback and rework cycle for a verdict that is not APPROVE."""
    from . import review_orchestrator as _home

    # Persist the reviewer's reasoning (not just the verdict) so the
    # redispatched agent knows what to fix, and count the cycle against
    # the rework budget so a perpetually-rejected story eventually parks
    # for a human instead of looping review -> rework forever.
    story["review_feedback"] = reverify_acceptance_feedback(
        reviewer_output, plan_name=plan_name, story_key=story_key, story=story,
        worktree=worktree, rework_kwargs=rework_kwargs,
    )
    # Mode 24/28: track which files this cycle's Blocking findings
    # target, so a later APPROVE can verify they were actually
    # addressed. Store even when empty (a real "nothing to track"
    # state, distinct from the key being absent entirely).
    story["last_review_findings"] = _home._extract_blocking_finding_files(reviewer_output)
    # Record the HEAD SHA for this REQUEST_CHANGES review
    if worktree and _home.os.path.isdir(worktree):
        try:
            story["last_reviewed_sha"] = _home.subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if story["last_reviewed_sha"] != before_sha:
                _home.logging.getLogger("pipeline").info(
                    "review head moved during review",
                    extra={"plan": plan_name, "story": story_key})
            story[_home.REVIEW_LOGIC_FINGERPRINT_KEY] = _home.review_logic_fingerprint()
        except (_home.subprocess.CalledProcessError, OSError):
            pass
    amended = _auto_amend_commit_message(
        verdict, reviewer_output, plan_name=plan_name, story_key=story_key,
        story=story, worktree=worktree, rework_kwargs=rework_kwargs,
    )
    if amended is not None:
        return amended
    attempts = story.get("rework_attempts", 0) + 1
    story["rework_attempts"] = attempts
    _home._notify_user(
        plan_name,
        f"{story_key} sent back by review: changes requested (rework cycle {attempts}).",
        event="review_changes_requested",
        story_key=story_key,
        **rework_kwargs,
    )
    route_by_rework_cap(
        reviewer_output, attempts, plan_name=plan_name, story_key=story_key,
        story=story, manifest=manifest, worktree=worktree, rework_kwargs=rework_kwargs,
    )
    return None
