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

from typing import Any

import pipeline.server as _server

from .parsers import _is_transient_backend_exception


class _ServerRef:
    """Delegates to the *current* ``pipeline.server`` binding for a name.

    The moved body references server-sourced names as bare module globals.
    The test suite patches ``pipeline.server`` for those names, so these
    bindings must read the live ``pipeline.server`` value at call time rather
    than hold a copy imported at module load.
    """

    def __init__(self, name: str):
        self._name = name

    def _value(self):
        return getattr(_server, self._name)

    def __getattr__(self, attr: str):
        return getattr(self._value(), attr)

    def __call__(self, *args, **kwargs):
        return self._value()(*args, **kwargs)

    def __truediv__(self, other):
        return self._value() / other

    def __contains__(self, item):
        return item in self._value()

    def __iter__(self):
        return iter(self._value())

    def __sub__(self, other):
        return self._value() - other

    def __rsub__(self, other):
        return other - self._value()

    def __len__(self):
        return len(self._value())

    def __eq__(self, other):
        if isinstance(other, _ServerRef):
            return self._value() == other._value()
        return self._value() == other

    def __lt__(self, other):
        return self._value() < other

    def __le__(self, other):
        return self._value() <= other

    def __gt__(self, other):
        return self._value() > other

    def __ge__(self, other):
        return self._value() >= other

    def __hash__(self):
        return hash(self._value())

    def __str__(self):
        return str(self._value())

    def __repr__(self):
        return repr(self._value())


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
_reverify_acceptance = _ServerRef("_reverify_acceptance")
_extract_blocking_finding_files = _ServerRef("_extract_blocking_finding_files")
_extract_suggested_commit_message = _ServerRef("_extract_suggested_commit_message")
_post_pr_comment = _ServerRef("_post_pr_comment")
_format_review_comment = _ServerRef("_format_review_comment")
_atomic_write_json = _ServerRef("_atomic_write_json")
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


def _verify_reviewer_auto_fix(
    worktree: str,
    story: dict[str, Any],
    reviewer_output: str,
    before_sha: str | None,
) -> tuple[str, str]:
    """Mechanically re-verify a reviewer's self-reported APPROVE_WITH_FIX
    before review_story ever honors it like a real APPROVE (2026-07-29).

    Defense in depth: the reviewer's own "this is trivial and I'm
    confident" claim is never trusted alone. Independently re-checks (in
    order, cheapest first): the story's risk tier, that a new commit
    actually landed, that its diff stays within the configured file/line
    caps, and that the full test suite still passes. Any failure downgrades
    to REQUEST_CHANGES (fail closed) with an explanation - this folds back
    into review_story's ordinary rejection path, so an unverified self-fix
    still counts against the rework budget rather than looping forever or
    silently landing unverified code.

    Returns (verdict, feedback) where verdict is "APPROVE" or
    "REQUEST_CHANGES" - never the raw "APPROVE_WITH_FIX", so callers can
    treat the result exactly like any other reviewer verdict.
    """
    if story.get("risk", "low") != "low":
        return "REQUEST_CHANGES", (
            f"Reviewer self-fix (APPROVE_WITH_FIX) is only allowed for "
            f"risk: low stories; this story is risk: "
            f"{story.get('risk', 'low')!r}. Downgraded to REQUEST_CHANGES - "
            f"a human must review this change.\n\n{reviewer_output}"
        )
    if not before_sha:
        return "REQUEST_CHANGES", (
            "Reviewer self-fix could not be verified (no baseline commit "
            "was recorded before the review ran). Downgraded to "
            f"REQUEST_CHANGES.\n\n{reviewer_output}"
        )
    try:
        after_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as e:
        return "REQUEST_CHANGES", (
            f"Reviewer self-fix could not be verified (git rev-parse failed: "
            f"{type(e).__name__}). Downgraded to REQUEST_CHANGES.\n\n{reviewer_output}"
        )
    if after_sha == before_sha:
        return "REQUEST_CHANGES", (
            "Reviewer reported APPROVE_WITH_FIX but no new commit was found "
            "on the branch - the claimed fix was never actually committed. "
            f"Downgraded to REQUEST_CHANGES.\n\n{reviewer_output}"
        )
    try:
        numstat = subprocess.run(
            ["git", "diff", "--numstat", before_sha, after_sha],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError) as e:
        return "REQUEST_CHANGES", (
            f"Reviewer self-fix could not be verified (git diff failed: "
            f"{type(e).__name__}). Downgraded to REQUEST_CHANGES.\n\n{reviewer_output}"
        )
    changed_lines = [ln for ln in numstat.splitlines() if ln.strip()]
    files_changed = len(changed_lines)
    total_lines = sum(
        int(part)
        for ln in changed_lines
        for part in ln.split("\t")[:2]
        if part.isdigit()
    )
    if (
        files_changed > REVIEWER_AUTO_FIX_MAX_FILES
        or total_lines > REVIEWER_AUTO_FIX_MAX_LINES
    ):
        return "REQUEST_CHANGES", (
            f"Reviewer self-fix touched {files_changed} file(s) and "
            f"{total_lines} changed line(s), exceeding the auto-fix cap "
            f"({REVIEWER_AUTO_FIX_MAX_FILES} file(s), "
            f"{REVIEWER_AUTO_FIX_MAX_LINES} line(s)). Downgraded to "
            f"REQUEST_CHANGES - too large to trust as a mechanical, "
            f"low-risk fix; a full rework/re-review cycle is required.\n\n"
            f"{reviewer_output}"
        )
    test_dir, test_cmd = detect_test_command(Path(worktree))
    test_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }
    test_result = subprocess.run(
        test_cmd,
        check=False,
        cwd=test_dir,
        capture_output=True,
        text=True,
        env=test_env,
    )
    if test_result.returncode != 0:
        return "REQUEST_CHANGES", (
            "Reviewer self-fix failed the full test suite after being "
            "applied. Downgraded to REQUEST_CHANGES.\n\n"
            f"Failing command: {' '.join(str(c) for c in test_cmd)}\n\n"
            f"```\n{(test_result.stdout or '')[-2000:]}\n```\n\n{reviewer_output}"
        )
    return "APPROVE", (
        f"{reviewer_output}\n\n[harness-verified self-fix: {files_changed} "
        f"file(s), {total_lines} line(s) changed, full test suite passed]"
    )


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
    manifest_path = _store.manifest_path(plan_name)
    manifest = _store.get_manifest(plan_name)
    story = manifest["stories"].get(story_key)
    if not story:
        return {"ok": False, "error": f"No such story {story_key}"}

    branch = f"agent/{story_key.lower()}"
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
                if current_sha == story["last_reviewed_sha"]:
                    _notify_user(
                        plan_name,
                        f"{story_key} review skipped: HEAD unchanged since the last REQUEST_CHANGES ({current_sha[:9]}) - a redispatch/rework must land a new commit before re-review.",
                        **_cid_kwargs,
                    )
                    _atomic_write_json(manifest_path, manifest)
                    return {
                        "ok": True,
                        "status": story["status"],
                        "skipped": "unchanged_since_last_review",
                    }
            except (subprocess.CalledProcessError, OSError):
                pass

    plan_role_config = _plan_role_config(plan_name)
    # Reviewer self-fix (2026-07-29): captured before invoking the reviewer
    # so a later APPROVE_WITH_FIX can be mechanically verified against what
    # actually changed. Guarded like the last_reviewed_sha capture above -
    # a missing/fake worktree (or any git error) must not crash review;
    # _verify_reviewer_auto_fix treats a None before_sha as unverifiable and
    # downgrades to REQUEST_CHANGES rather than trusting an unbounded diff.
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
    if skip_llm_reviewer:
        reviewer_output = _synthesize_test_failure_feedback(last_test_check)
    else:
        # Mode 47: carry the prior cycle's findings into a RE-review so the
        # reviewer must discharge each one individually. Passed as a kwarg
        # only when there is actually prior feedback (a first review has
        # none), so the common path's call signature is unchanged.
        _prior_fb = story.get("review_feedback")
        prior_kw = {"prior_feedback": _prior_fb} if _prior_fb else {}
        try:
            # Once a story is escalated (see _escalate_review_to_claude below),
            # every subsequent review must go to the escalation target
            # regardless of the global PIPELINE_BACKEND_REVIEW setting - review
            # backend is otherwise resolved purely from that env var with no
            # per-story override, so this is the one seam that needs an explicit
            # check. The target is Claude by default; PIPELINE_ESCALATION_BACKEND
            # retargets it (e.g. to a non-Claude provider while Claude is capped).
            _escalation_backend = _escalation_target()[0]
            reviewer_output = (
                _run_reviewer(
                    worktree,
                    branch,
                    backend_name=_escalation_backend,
                    plan_role_config=plan_role_config,
                    since_sha=story.get("last_reviewed_sha"),
                    risk=story.get("risk", "low"),
                    **prior_kw,
                )
                if story.get("escalated")
                else _run_reviewer(
                    worktree,
                    branch,
                    plan_role_config=plan_role_config,
                    since_sha=story.get("last_reviewed_sha"),
                    risk=story.get("risk", "low"),
                    **prior_kw,
                )
            )
        except backend.RateLimitedError:
            # FM-B: an Ollama-cloud (or any Ollama-proxied) 429 on the review path
            # is an infrastructure event, not a real review cycle. Treat it the
            # same as Claude's weekly-usage pause: defer and retry on the next
            # tick, do NOT burn REVIEW_INCONCLUSIVE_MAX. Without this, a
            # misclassified rate-limit would eventually park a correct impl.
            story["review_deferred_count"] = story.get("review_deferred_count", 0) + 1
            _notify_user(
                plan_name,
                f"{story_key} review deferred: local reviewer rate-limited; will retry next tick.",
                **_cid_kwargs,
            )
            _atomic_write_json(manifest_path, manifest)
            return {"ok": True, "status": story["status"], "deferred": "rate_limited"}
        except Exception as e:  # noqa: BLE001 (defense in depth, per the comment below)
            if _is_transient_backend_exception(e):
                # A reviewer transport failure (timeout, refused or reset
                # connection) is infrastructure, not a review outcome: defer
                # like the rate-limit path and never charge
                # review_inconclusive_count. Only the exception TYPE reaches
                # logs and the notification; its text may carry sensitive detail.
                logging.getLogger("pipeline").warning(
                    "%s/%s review deferred: reviewer transport failure %s",
                    plan_name, story_key, type(e).__name__,
                )
                story["review_deferred_count"] = story.get("review_deferred_count", 0) + 1
                _notify_user(
                    plan_name,
                    f"{story_key} review deferred: reviewer backend transport "
                    f"failure ({type(e).__name__}); will retry next tick.",
                    **_cid_kwargs,
                )
                _atomic_write_json(manifest_path, manifest)
                return {"ok": True, "status": story["status"], "deferred": "transient_backend"}
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
            logging.getLogger("pipeline").error(
                f"{plan_name}/{story_key} review raised {type(e).__name__}:\n"
                f"{traceback.format_exc()}"
            )
            _notify_user(
                plan_name,
                f"{story_key} review failed with an unexpected "
                f"{type(e).__name__}; treating as inconclusive.",
                **_cid_kwargs,
            )
            reviewer_output = ""
    verdict = _parse_verdict(reviewer_output)

    # FM-B: a rate-limit response from the reviewer is an infrastructure event,
    # not a genuine review cycle. Leave the story at tests_passed so the next
    # advance_pipeline tick retries review once the backend recovers. Do NOT
    # touch rework_attempts — burning the rework budget on rate-limits parks
    # correct implementations silently.
    if verdict == "UNKNOWN" and _is_rate_limited(reviewer_output):
        story["review_deferred_count"] = story.get("review_deferred_count", 0) + 1
        fallback_mode = (
            os.environ.get("PIPELINE_REVIEW_FALLBACK", "off").strip().lower()
        )
        fallback_after = int(os.environ.get("PIPELINE_REVIEW_FALLBACK_AFTER", "3"))
        if (
            fallback_mode in _LOCAL_BACKEND_NAMES
            and story["review_deferred_count"] >= fallback_after
        ):
            _notify_user(
                plan_name,
                f"{story_key} review falling back to {fallback_mode} backend "
                f"after {story['review_deferred_count']} rate-limited attempts.",
                **_cid_kwargs,
            )
            reviewer_output = _run_reviewer(
                worktree,
                branch,
                backend_name=fallback_mode,
                plan_role_config=plan_role_config,
                since_sha=story.get("last_reviewed_sha"),
                risk=story.get("risk", "low"),
                **prior_kw,
            )
            verdict = _parse_verdict(reviewer_output)
            # Fall through into the normal verdict-handling code below —
            # this is a genuine review attempt now, not a deferral.
        else:
            _notify_user(
                plan_name,
                f"{story_key} review deferred: reviewer rate-limited; will retry next tick.",
                **_cid_kwargs,
            )
            _atomic_write_json(manifest_path, manifest)
            return {"ok": True, "status": story["status"], "deferred": "rate_limited"}

    # Transient backend error (HTTP 5xx / connection reset or refused / timed
    # out): re-invoke the reviewer once inline. This is an infrastructure
    # hiccup, not a genuine review cycle, so it never increments
    # review_inconclusive_count; a retry that is still a transient failure is
    # deferred to the next tick just below.
    _transient_retried = False
    if verdict == "UNKNOWN" and _is_transient_backend_error(reviewer_output):
        _notify_user(
            plan_name, f"{story_key} review hit transient backend error; retrying once.",
            **_cid_kwargs,
        )
        reviewer_output = (
            _run_reviewer(
                worktree,
                branch,
                backend_name=_escalation_backend,
                plan_role_config=plan_role_config,
                since_sha=story.get("last_reviewed_sha"),
                risk=story.get("risk", "low"),
                **prior_kw,
            )
            if story.get("escalated")
            else _run_reviewer(
                worktree,
                branch,
                plan_role_config=plan_role_config,
                since_sha=story.get("last_reviewed_sha"),
                risk=story.get("risk", "low"),
                **prior_kw,
            )
        )
        verdict = _parse_verdict(reviewer_output)
        _transient_retried = True
    # A transport failure that survives the single inline retry is an
    # infrastructure event, not a review outcome: defer to the next tick like
    # the rate-limit path and never charge review_inconclusive_count.
    if (
        _transient_retried
        and verdict == "UNKNOWN"
        and _is_transient_backend_error(reviewer_output)
    ):
        story["review_deferred_count"] = story.get("review_deferred_count", 0) + 1
        _notify_user(
            plan_name,
            f"{story_key} review deferred: reviewer backend still failing after "
            f"one retry (transient transport error); will retry next tick.",
            **_cid_kwargs,
        )
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "status": story["status"], "deferred": "transient_backend"}

    # Reviewer self-fix (2026-07-29): the reviewer's own "trivial and
    # confident" self-assessment is never trusted alone - mechanically
    # re-verify risk, diff size, and the full test suite before honoring it.
    # Folds into the existing APPROVE/REQUEST_CHANGES branches below
    # unchanged: verified -> APPROVE (opens a PR like any other approval);
    # unverified -> REQUEST_CHANGES (counts against the rework budget like
    # any other rejection, so an unverifiable self-fix can't loop forever).
    if verdict == "APPROVE_WITH_FIX":
        verdict, reviewer_output = _server._verify_reviewer_auto_fix(
            worktree,
            story,
            reviewer_output,
            before_sha,
        )

    story["review_verdict"] = verdict
    story["review_deferred_count"] = 0

    # High-risk stories require an additional security-engineer pass; both
    # must APPROVE before the story proceeds to pr_open.
    if verdict == "APPROVE" and story.get("risk") == "high":
        security_output = _run_security_reviewer(
            worktree, branch, since_sha=story.get("last_reviewed_sha"),
            plan_role_config=plan_role_config,
        )
        security_verdict = _parse_verdict(security_output)

        # FM-B: same rate-limit deferral for the security-reviewer pass.
        if security_verdict == "UNKNOWN" and _is_rate_limited(security_output):
            _notify_user(
                plan_name,
                f"{story_key} security review deferred: reviewer rate-limited; will retry next tick.",
                **_cid_kwargs,
            )
            _atomic_write_json(manifest_path, manifest)
            return {"ok": True, "status": story["status"], "deferred": "rate_limited"}

        story["security_review_verdict"] = security_verdict
        if security_verdict != "APPROVE":
            verdict = security_verdict
            reviewer_output = security_output  # use security feedback for rework

    # A non-rate-limited UNKNOWN is inconclusive, not a rejection: don't touch
    # review_feedback or rework_attempts, and leave status at its pre-review
    # value so the next advance_pipeline tick retries review. Fail closed -
    # this must never fall through to the APPROVE branch. Only after repeated
    # inconclusive attempts does it park for a human.
    if verdict == "UNKNOWN":
        inconclusive = story.get("review_inconclusive_count", 0) + 1
        story["review_inconclusive_count"] = inconclusive
        if inconclusive >= REVIEW_INCONCLUSIVE_MAX:
            if _auto_escalation_enabled() and not story.get("escalated"):
                _escalate_review_to_claude(
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
                _notify_user(
                    plan_name,
                    f"{story_key} parked: review inconclusive after "
                    f"{inconclusive} attempts - needs human review.",
                    event="story_parked",
                    story_key=story_key,
                    **_cid_kwargs,
                )
        else:
            _notify_user(plan_name, f"{story_key} review inconclusive; will retry.", **_cid_kwargs)
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "verdict": verdict, "status": story["status"]}

    # T11: a REQUEST_CHANGES with no substantive findings text is not a
    # genuine rejection - it gives the redispatched agent nothing to fix, and
    # treating it as one silently burns the rework budget on nothing (the
    # 2026-07-02 gpt-oss run parked a story this way). Route it through the
    # same inconclusive-handling shape as UNKNOWN above - before the
    # review_inconclusive_count reset below, so repeated empty responses
    # still accumulate toward REVIEW_INCONCLUSIVE_MAX - but leave the verdict
    # itself visible and never touch rework_attempts/review_feedback. Checked
    # here (not merged into the UNKNOWN branch above) because it applies
    # equally to a content-free REQUEST_CHANGES from either the ordinary
    # reviewer or a security-reviewer override.
    if verdict == "REQUEST_CHANGES" and not _has_review_findings(reviewer_output):
        inconclusive = story.get("review_inconclusive_count", 0) + 1
        story["review_inconclusive_count"] = inconclusive
        if inconclusive >= REVIEW_INCONCLUSIVE_MAX:
            if _auto_escalation_enabled() and not story.get("escalated"):
                _escalate_review_to_claude(
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
                _notify_user(
                    plan_name,
                    f"{story_key} parked: review inconclusive after "
                    f"{inconclusive} attempts - needs human review.",
                    event="story_parked",
                    story_key=story_key,
                    **_cid_kwargs,
                )
        else:
            _notify_user(
                plan_name,
                f"{story_key} review approved-changes-requested-empty: "
                f"REQUEST_CHANGES with no findings text; will retry.",
                **_cid_kwargs,
            )
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "verdict": verdict, "status": story["status"]}

    story["review_inconclusive_count"] = 0

    # Mode 24/28 finding-target guard: the Mode 27 same-SHA guard only
    # catches a review call where HEAD is byte-identical to the last
    # reviewed commit. A dispatch watchdog checkpoint commit changes HEAD's
    # SHA trivially (a WIP commit) without addressing the reviewer's own
    # prior Blocking findings, slipping past that guard and letting the
    # reviewer silently APPROVE a diff that never touched the flagged
    # file(s) - "merged-but-incomplete". If every file recorded from the
    # prior REQUEST_CHANGES cycle's Blocking findings wasn't touched by the
    # diff since then, downgrade this APPROVE back to REQUEST_CHANGES
    # instead of opening a PR.
    #
    # Exception: a flagged TEST file is exempt from the "was it touched"
    # check, because review_story only reaches this APPROVE branch when
    # story["status"] == "tests_passed" - the full suite, including that
    # test file, is provably green right now. That is strictly stronger,
    # directly-verified proof the finding (a failing test) is resolved than
    # "were the test file's own bytes touched" - a correct fix legitimately
    # lands in the implementation the test exercises, not the test itself.
    # Root-caused live 2026-07-24 (MODE40-CI-REWORK-FEEDBACK-V2): a
    # gate-synthesized review flagged the failing test file, the agent fixed
    # the bug in the implementation module, the suite went green and a real
    # reviewer said APPROVE, but this guard downgraded it anyway - burning
    # the story's entire rework budget on an already-resolved finding.
    # Non-test findings (README prose, server.py logic) still require the
    # flagged file to be touched; there's no equivalent objective proof.
    if (
        verdict == "APPROVE"
        and story.get("last_reviewed_sha")
        and story.get("last_review_findings")
    ):
        try:
            diff_res = subprocess.run(
                ["git", "diff", "--name-only", story["last_reviewed_sha"], "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            )
            changed_files = set(diff_res.stdout.splitlines())
            untouched = [
                p
                for p in story["last_review_findings"]
                if p not in changed_files and not _is_test_file_path(p)
            ]
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
        except (subprocess.CalledProcessError, OSError):
            # Fail open - this is a workflow-correctness gate, not a
            # security boundary, so an infra error must not block a
            # genuine APPROVE.
            pass

    if verdict == "APPROVE":
        # A git-push failure (transient network/auth hiccup, or a race with
        # a concurrent rebase) must not crash the whole advance_pipeline
        # tick: this call sits inside the tests_passed review loop in
        # _advance_pipeline_locked, so an uncaught exception here aborts
        # review for every OTHER tests_passed story and skips merge
        # adjudication entirely for the rest of that tick (confirmed live in
        # advance-scheduler.err.log, story 30e5f9fc, 2026-08-19: an
        # unguarded CalledProcessError from this exact call propagated
        # through wake_handler and silently dropped the tick). The sibling
        # REQUEST_CHANGES-path call (below, in the PR-comment block) already
        # swallows this error the same way - mirror that here. Fail open:
        # leave status/verdict untouched so the next tick's review_story
        # call retries from scratch rather than getting stuck.
        try:
            pr_url = _open_pr(worktree, story_key, story)
        except (subprocess.CalledProcessError, OSError) as exc:
            _notify_user(
                plan_name,
                f"{story_key}: review APPROVEd but could not open PR "
                f"({exc.__class__.__name__}); will retry next tick.",
                **_cid_kwargs,
            )
            _atomic_write_json(manifest_path, manifest)
            return {"ok": True, "verdict": verdict, "status": story["status"]}
        story["pr_url"] = pr_url
        story["status"] = "pr_open"
        # The work passed: drop any stale rework state from earlier cycles.
        story.pop("review_feedback", None)
        story.pop("rework_attempts", None)
        # Clear any stored SHA when review is approved
        story.pop("last_reviewed_sha", None)
        story.pop("last_review_findings", None)
    else:
        # Persist the reviewer's reasoning (not just the verdict) so the
        # redispatched agent knows what to fix, and count the cycle against
        # the rework budget so a perpetually-rejected story eventually parks
        # for a human instead of looping review -> rework forever.
        #
        # Mode 20 (2026-07-17, verified by replay): a REQUEST_CHANGES verdict
        # on an acceptance-bearing story can be correct about SOMETHING
        # outside the oracle's scope while the oracle itself is currently
        # green - and a whole-file rework, given only the reviewer's raw
        # feedback, has no signal that it must not regress that already-
        # correct behavior (observed: this exact gap let a rework destroy a
        # passing backward-jump fix). Re-verify the oracle against the
        # CURRENT worktree state before dispatching rework and, if it still
        # passes, prepend an explicit warning. This does not change the
        # verdict or control flow - the story still goes to rework - it only
        # gives the next dispatch a fact the reviewer's own text can't convey.
        feedback = reviewer_output
        if story.get("acceptance"):
            oracle_now = _reverify_acceptance(story, worktree, story_key)
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
                # incremented below, after this block, so here it still holds
                # the count from PRIOR cycles.
                #
                # Best-effort: a missing/unwritable transcript must not block
                # the review outcome itself.
                transcript_path = Path(worktree) / ".agent_transcript.json" if worktree else None
                if transcript_path and transcript_path.exists():
                    try:
                        transcript_path.unlink()
                        _notify_user(
                            plan_name,
                            f"{story_key} rework regressed the acceptance "
                            f"oracle (was passing before this rework, now "
                            f"failing); deleted the dispatch transcript so the "
                            f"next attempt starts fresh instead of resuming "
                            f"the churn that caused it.",
                            **_rework_kwargs,
                        )
                    except OSError:
                        pass
        story["review_feedback"] = feedback
        # Mode 24/28: track which files this cycle's Blocking findings
        # target, so a later APPROVE can verify they were actually
        # addressed. Store even when empty (a real "nothing to track"
        # state, distinct from the key being absent entirely).
        story["last_review_findings"] = _extract_blocking_finding_files(reviewer_output)
        # Record the HEAD SHA for this REQUEST_CHANGES review
        if worktree and os.path.isdir(worktree):
            try:
                story["last_reviewed_sha"] = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=worktree,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            except (subprocess.CalledProcessError, OSError):
                pass
        if (
            verdict == "REQUEST_CHANGES"
            and not story["last_review_findings"]
            and story.get("commit_hygiene_autofix_attempts", 0) < 2
        ):
            _suggested = _extract_suggested_commit_message(reviewer_output)
            if _suggested is not None and worktree and os.path.isdir(worktree):
                try:
                    _status_r = subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=worktree,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    if _status_r.stdout.strip() == "":
                        try:
                            subprocess.run(
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
                            _notify_user(
                                plan_name,
                                f"{story_key}: reviewer's only blocking finding was "
                                f"commit-message format; auto-amended HEAD commit "
                                f"without spending a rework attempt.",
                                **_rework_kwargs,
                            )
                            _atomic_write_json(manifest_path, manifest)
                            return {
                                "ok": True,
                                "verdict": verdict,
                                "status": story["status"],
                                "auto_fixed_commit_message": True,
                            }
                        except (subprocess.CalledProcessError, OSError):
                            pass
                except (subprocess.CalledProcessError, OSError):
                    pass
        attempts = story.get("rework_attempts", 0) + 1
        story["rework_attempts"] = attempts
        if story.get("escalated"):
            rework_cap = REWORK_MAX_ATTEMPTS_ESCALATED
        elif story.get("acceptance"):
            rework_cap = REWORK_MAX_ATTEMPTS_ORACLE
        else:
            rework_cap = REWORK_MAX_ATTEMPTS
        if attempts >= rework_cap:
            if _auto_escalation_enabled() and not story.get("escalated"):
                _escalate_review_to_claude(
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
                _notify_user(
                    plan_name,
                    f"{story_key} parked: reviewer still requesting changes "
                    f"after {attempts} cycles - needs human review.",
                    event="story_parked",
                    story_key=story_key,
                    **_rework_kwargs,
                )
        else:
            fre = manifest.get("final_rework_escalation") or {}
            if attempts == rework_cap - 1 and fre.get("enabled"):
                provider = fre.get("provider")
                if provider in {"claude", "local", "ollama", "lmstudio", "mlx"}:
                    model = fre.get("model")
                    story["backend"] = provider
                    story["model"] = model
                    _notify_user(
                        plan_name,
                        f"{story_key} final rework attempt ({attempts}/{rework_cap}) escalating to {provider}/{model}.",
                        story_key=story_key,
                        event="escalated",
                        **_rework_kwargs,
                    )
            story["status"] = "changes_requested"
            if worktree and os.path.isdir(worktree):
                try:
                    story["pr_url"] = _open_pr(worktree, story_key, story)
                    _post_pr_comment(worktree, _format_review_comment(reviewer_output, attempts))
                except (subprocess.CalledProcessError, OSError) as exc:
                    _notify_user(plan_name, f"{story_key}: could not open PR / post review comment ({exc.__class__.__name__}); findings remain in review_feedback for the rework agent.", **_rework_kwargs)
    _atomic_write_json(manifest_path, manifest)
    return {
        "ok": True,
        "verdict": verdict,
        "status": story["status"],
        "pr_url": story.get("pr_url"),
    }


# Preserve original review_story implementation
_original_review_story = review_story
