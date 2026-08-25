"""
check_story_status: poll a dispatched agent's story, run its tests in the
worktree, and report pass/fail without auto-merging.

Extracted verbatim from pipeline/server.py (behavior-preserving file move).
"""

import os
import subprocess
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import server as _server
from pipeline.dispatch import _find_dead_new_functions

from .build_detect import (
    _acceptance_rel_paths,
    _added_pytest_test_paths,
    _is_pytest_cmd,
    _scope_test_cmd_to_acceptance,
)
from .checkpoint import _terminate_and_checkpoint
from .ci import _acceptance_tampered
from .concurrency import _heavy_lock
from .config import (
    DISPATCH_MAX_ATTEMPTS,
    DISPATCH_STARTUP_GRACE_SECONDS,
    DISPATCH_WATCHDOG_SECONDS,
    INFRA_FAILURE_FALLBACK_THRESHOLD,
    INFRA_FAILURE_LOG_SUBSTRING,
    REWORK_MAX_ATTEMPTS_NO_COMMIT,
    STEP_CAP_FALLBACK_THRESHOLD,
    STEP_CAP_MARKERS,
)
from .escalation import (
    _escalate_review_to_claude,
    _escalate_to_claude,
    _escalation_label,
)
from .git_ops import _commit_wip
from .parsers import (
    _atomic_write_json,
    _is_give_up_summary,
    _validate_key,
)
from .rebrief import append_cleanup_guidance


def check_story_status(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Check whether a dispatched agent has finished. If complete, runs tests
    in the worktree and reports pass/fail without auto-merging.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    manifest_path = _store.manifest_path(plan_name)  # noqa: F821
    manifest = _store.get_manifest(plan_name)  # noqa: F821
    story = manifest["stories"].get(story_key)
    if not story or "pid" not in story:
        return {"ok": False, "error": "Story not dispatched"}
    if story["status"] == "interrupted":
        # Incomplete by definition — running tests here would just record a
        # spurious failure instead of leaving it resumable.
        return {"status": "interrupted", "pid": story["pid"]}

    pid = story["pid"]
    try:
        os.kill(pid, 0)
        # os.kill succeeds for zombie (defunct) processes too — check ps stat
        ps = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            check=False,
            capture_output=True,
            text=True,
        )
        stat = ps.stdout.strip()
        if stat and not stat.startswith("Z"):
            dispatched_at = story.get("dispatched_at")
            if dispatched_at is not None:
                elapsed = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(dispatched_at)
                ).total_seconds()
                if elapsed > DISPATCH_WATCHDOG_SECONDS:
                    _terminate_and_checkpoint(
                        manifest,
                        manifest_path,
                        plan_name,
                        story_key,
                        story,
                        pid=pid,
                        step="dispatch_watchdog_timeout",
                        summary=(
                            f"Dispatch watchdog: no completion after "
                            f"{elapsed:.0f}s; process terminated."
                        ),
                    )
                    # CLAUDE.md Step 9: diagnose where this implementer hung and
                    # fold the root cause into agent_instructions so the resume
                    # isn't a blind retry. Mirrors the step-cap branch's call
                    # byte-for-byte (same helper, same arguments). Fail-open: a
                    # None/errored diagnosis leaves agent_instructions untouched.
                    _rebrief_step_cap_struggle(  # noqa: F821
                        story, str(Path(story["worktree"])),
                        plan_role_config=manifest.get("role_config"),
                        plan_name=plan_name,
                        story_key=story_key)
                    story["dispatch_error"] = (
                        f"watchdog killed after {elapsed:.0f}s with no completion"
                    )
                    _atomic_write_json(manifest_path, manifest)
                    return {
                        "status": "interrupted",
                        "pid": pid,
                        "watchdog_killed": True,
                    }
            return {"status": "running", "pid": pid}
        # process is zombie or gone — fall through to test detection
    except ProcessLookupError:
        pass

    worktree = Path(story["worktree"])
    agent_log = worktree / "agent.log"
    if agent_log.exists() and agent_log.stat().st_size == 0:
        # Empty log within the startup grace window means the agent is alive
        # and bootstrapping - its first print() hasn't flushed yet, especially
        # when queued on Ollama's -np 1 worker behind another request. The
        # PID-alive check above already passed, so trust that and don't burn
        # dispatch_attempts on a process that's just slow to print. After the
        # grace window elapses with the log still empty, the agent is
        # presumed genuinely dead (failed launch) and we count it.
        log_age = time.time() - agent_log.stat().st_mtime
        if log_age < DISPATCH_STARTUP_GRACE_SECONDS:
            return {"status": "running", "pid": pid}
        # The agent process exited without ever writing a byte of output -
        # a failed launch, not a real attempt. Running tests against the
        # untouched worktree would just record a misleading "failed" for
        # work that was never tried. Within the dispatch error budget we keep
        # it "interrupted" (dispatch-eligible like "todo", so the next tick
        # retries it); once the budget is spent, a launch that never works
        # becomes a terminal "failed" so it stops looping forever.
        attempts = story.get("dispatch_attempts", 0) + 1
        story["dispatch_attempts"] = attempts
        if attempts >= DISPATCH_MAX_ATTEMPTS:
            story["status"] = "failed"
            story["dispatch_error"] = (
                f"agent produced no output in {attempts} launch attempts"
            )
            _notify_user(  # noqa: F821
                plan_name,
                f"{story_key} failed to launch {attempts}x; "
                f"giving up - needs human intervention.",
            )
            _atomic_write_json(manifest_path, manifest)
            return {"status": "failed", "pid": pid}
        story["status"] = "interrupted"
        _atomic_write_json(manifest_path, manifest)
        return {"status": "interrupted", "pid": pid}

    # Step-cap exit routing (regression guard for PR #49 / commit 90a3cf1):
    # when the headless agent hits its step cap it prints a terminal marker
    # on the LAST line of agent.log, exits with code 2, and has already
    # WIP-committed. Classifying the run by its tail line (NOT a substring
    # search of the whole file — a resumed agent appends to agent.log, so an
    # old marker from a prior tick may appear earlier) lets us short-circuit
    # before the test suite runs. If we ran tests against the WIP commit and
    # it passed, we'd land the story on `tests_passed`, which is merge-
    # eligible — and that is exactly how incomplete step-capped work landed
    # on master. `interrupted` is dispatch-eligible, so the next
    # advance_pipeline tick resumes the agent in its existing worktree from
    # its WIP commit, seeded by the journal entry we write below.
    last_log_line = _last_nonempty_line(agent_log) if agent_log.exists() else ""  # noqa: F821

    # Infra-failure exit routing: a dispatch that died on an LLM/Ollama
    # transport error (after chat()'s own retries and the 5xx trim-retry are
    # exhausted) is not a review/test-quality outcome and must not be graded
    # or counted against rework_attempts - see INFRA_FAILURE_LOG_SUBSTRING's
    # docstring for the live incident this fixes.
    if INFRA_FAILURE_LOG_SUBSTRING in last_log_line:
        sha = _commit_wip(str(worktree), story_key, "infra_failure")
        interrupted_at = datetime.now(timezone.utc).isoformat()
        _store.append_journal(  # noqa: F821
            plan_name,
            story_key,
            {
                "step": "infra_failure",
                "summary": "Dispatch died on an infrastructure failure (LLM/Ollama "
                "transport error); checkpointed for resume.",
                "next_hint": "",
                "commit": sha,
                "ts": interrupted_at,
            },
        )
        story["status"] = "interrupted"
        story["last_commit"] = sha
        story["interrupted_at"] = interrupted_at

        # See INFRA_FAILURE_FALLBACK_THRESHOLD: track consecutive infra
        # failures on the current model with SEPARATE streak fields from
        # STEP_CAP_MARKERS below - an infra death is not evidence the MODEL
        # is struggling, so it must never feed that branch's model-switch
        # logic (test_check_story_status_infra_failure_does_not_trigger_
        # model_fallback). Notify on the very first occurrence (unlike a
        # step-cap hit, which is routine for a local model, an infra death
        # this late - after chat()'s own retries AND the 5xx trim-retry are
        # exhausted - is unusual enough to be worth surfacing immediately),
        # then apply the same two remedies step-cap uses once the streak
        # crosses the threshold: switch to the plan's opted-in local fallback
        # model, or escalate to Claude.
        current_model = story.get("dispatched_model") or story.get("model")
        if story.get("infra_failure_streak_model") == current_model:
            story["infra_failure_streak"] = story.get("infra_failure_streak", 0) + 1
        else:
            story["infra_failure_streak"] = 1
            story["infra_failure_streak_model"] = current_model
        if story["infra_failure_streak"] == 1:
            _notify_user(  # noqa: F821
                plan_name,
                f"{story_key} dispatch died on an infrastructure failure "
                f"(LLM/Ollama transport error) on {current_model}; resuming "
                f"from the last checkpoint.",
            )

        fallback_model = manifest.get("local_model_fallback")
        if (
            fallback_model
            and current_model != fallback_model
            and story.get("backend", "local") == "local"
            and story["infra_failure_streak"] >= INFRA_FAILURE_FALLBACK_THRESHOLD
        ):
            story["model"] = fallback_model
            story.pop("infra_failure_streak", None)
            story.pop("infra_failure_streak_model", None)
            _notify_user(  # noqa: F821
                plan_name,
                f"{story_key} hit {INFRA_FAILURE_FALLBACK_THRESHOLD} consecutive "
                f"infrastructure failures on {current_model}; switching to "
                f"fallback model {fallback_model} for the next resume.",
            )
        elif (
            not fallback_model
            and _auto_escalation_enabled()  # noqa: F821
            and story.get("backend", "local") == "local"
            and not story.get("escalated")
            and story["infra_failure_streak"] >= INFRA_FAILURE_FALLBACK_THRESHOLD
        ):
            _escalate_to_claude(manifest, plan_name, story_key, manifest_path)
            _notify_user(  # noqa: F821
                plan_name,
                f"{story_key} hit {INFRA_FAILURE_FALLBACK_THRESHOLD} consecutive "
                f"infrastructure failures on {current_model}; escalating to "
                f"{_escalation_label()} (no local_model_fallback configured).",
            )
            return {
                "status": "todo",
                "reason": "infra_failure_escalated_to_claude",
                "pid": pid,
            }
        _atomic_write_json(manifest_path, manifest)
        return {"status": "interrupted", "pid": pid, "reason": "infra_failure"}

    if last_log_line in STEP_CAP_MARKERS:
        sha = _commit_wip(str(worktree), story_key, "step_cap_reached")
        interrupted_at = datetime.now(timezone.utc).isoformat()
        _store.append_journal(  # noqa: F821
            plan_name,
            story_key,
            {
                "step": "step_cap_reached",
                "summary": "Agent hit the step cap; checkpointed for resume.",
                "next_hint": "",
                "commit": sha,
                "ts": interrupted_at,
            },
        )
        story["status"] = "interrupted"
        story["last_commit"] = sha
        story["interrupted_at"] = interrupted_at

        # CLAUDE.md Step 9: diagnose where this implementer struggled and fold
        # the root cause into agent_instructions so the resume isn't a blind
        # retry. Runs before any model-switch/escalation below so the diagnosis
        # uses the model that just ran (the struggling one) and the worktree's
        # agent.log is still present for evidence. Fail-open: a None/errored
        # diagnosis leaves agent_instructions untouched (no-op).
        _rebrief_step_cap_struggle(  # noqa: F821
            story, str(worktree), plan_role_config=manifest.get("role_config"),
            plan_name=plan_name,
            story_key=story_key)
        # Worktree hygiene: append the cleanup-guidance section so the resumed
        # agent tidies the worktree before continuing. Unconditional and
        # idempotent (append_cleanup_guidance is a no-op when its header is
        # already present), so it composes safely with the diagnosis above.
        story["agent_instructions"] = append_cleanup_guidance(
            story.get("agent_instructions", ""))

        # See STEP_CAP_FALLBACK_THRESHOLD: track consecutive step-cap
        # interrupts on the current model and, past the threshold, switch to
        # the plan's opted-in fallback model for the next resume. Worktree
        # and journal are left untouched so the resumed run still benefits
        # from whatever real progress is already committed.
        fallback_model = manifest.get("local_model_fallback")
        current_model = story.get("dispatched_model") or story.get("model")
        # STEP_CAP_MARKERS are only ever printed by the local agent scripts, so
        # a Claude-backend story should never reach here in practice - guard
        # explicitly anyway (defense in depth) so a plan-scoped local model
        # name can never land in a Claude story's model field. Missing
        # "backend" defaults to local: dispatch_story always sets it
        # explicitly, so an absent key only occurs in tests exercising this
        # branch in isolation.
        if (
            fallback_model
            and current_model != fallback_model
            and story.get("backend", "local") == "local"
        ):
            if story.get("step_cap_streak_model") == current_model:
                story["step_cap_streak"] = story.get("step_cap_streak", 0) + 1
            else:
                story["step_cap_streak"] = 1
                story["step_cap_streak_model"] = current_model
            if story["step_cap_streak"] >= STEP_CAP_FALLBACK_THRESHOLD:
                story["model"] = fallback_model
                story.pop("step_cap_streak", None)
                story.pop("step_cap_streak_model", None)
                _notify_user(  # noqa: F821
                    plan_name,
                    f"{story_key} hit the step cap {STEP_CAP_FALLBACK_THRESHOLD}x "
                    f"on {current_model}; switching to fallback model "
                    f"{fallback_model} for the next resume.",
                )
        elif (
            not fallback_model
            and _auto_escalation_enabled()  # noqa: F821
            and story.get("backend", "local") == "local"
            and not story.get("escalated")
        ):
            # No local_model_fallback opt-in for this plan: under auto
            # dispatch, escalate to Claude instead of cycling on the same
            # struggling local model forever. Mutually exclusive with the
            # local-fallback branch above (gated on `not fallback_model`) -
            # no chaining from local fallback to Claude.
            if story.get("step_cap_streak_model") == current_model:
                story["step_cap_streak"] = story.get("step_cap_streak", 0) + 1
            else:
                story["step_cap_streak"] = 1
                story["step_cap_streak_model"] = current_model
            if story["step_cap_streak"] >= STEP_CAP_FALLBACK_THRESHOLD:
                _escalate_to_claude(manifest, plan_name, story_key, manifest_path)
                _notify_user(  # noqa: F821
                    plan_name,
                    f"{story_key} hit the step cap {STEP_CAP_FALLBACK_THRESHOLD}x "
                    f"on {current_model}; escalating to {_escalation_label()} (no "
                    f"local_model_fallback configured).",
                )
                return {
                    "status": "todo",
                    "reason": "step_cap_escalated_to_claude",
                    "pid": pid,
                }
        _atomic_write_json(manifest_path, manifest)
        return {"status": "interrupted", "pid": pid, "reason": "step_cap_reached"}

    # The read-only oracle is described as read-only in prompt text only;
    # this is the mechanism behind it. Refuse tests_passed on a worktree whose
    # acceptance fixture no longer matches its dispatch-time digest, so a
    # rewritten grader is caught at the done-bar rather than only much later
    # at the merge gate (see pipeline.ci._acceptance_tampered).
    tampered = _acceptance_tampered(story, str(worktree))
    if tampered:
        story["status"] = "changes_requested"
        _notify_user(  # noqa: F821
            plan_name,
            f"{story_key} acceptance fixture modified since dispatch: "
            f"{', '.join(tampered)} - refusing tests_passed",
        )
        _atomic_write_json(manifest_path, manifest)
        return {
            "status": "changes_requested",
            "reason": "acceptance_tampered",
            "tampered": tampered,
        }

    test_dir, test_cmd = detect_test_command(worktree)  # noqa: F821

    # FM-A: when the story carries an acceptance block, gate on only those
    # oracle test files rather than the full worktree suite. The model's own
    # tests can contain wrong assertions (the "graded on own buggy tests"
    # failure mode); the harness-owned oracle is the authoritative bar.
    # _scope_test_cmd_to_acceptance scopes pytest (path args), cargo
    # (--test <stem>), and npm/yarn-with-node --test; other runners fall back
    # to the whole suite (the MBW safety net — a story without an acceptance
    # block, or a runner we can't safely scope, still gets the full re-run).
    #
    # Paths are materialized relative to the worktree root (dispatch_story),
    # but test_dir can be a child subdirectory when the buildable project
    # doesn't live at the worktree root (detect_test_command's fallback).
    # Use absolute paths so the scoped run works regardless of test_dir.
    acceptance = story.get("acceptance") or []
    if acceptance:
        acceptance_paths = [str(worktree / p) for p in _acceptance_rel_paths(story)]
        scoped = _scope_test_cmd_to_acceptance(test_cmd, acceptance_paths, test_dir)
        if scoped is not None:
            test_cmd = scoped
    elif _is_pytest_cmd(test_cmd):
        # Mode 42 done-bar blindspot: a story without an acceptance block
        # whose deliverable lives under tests/ can add its own test_*.py
        # there, which --ignore=tests then hides from this same gate run
        # (see _added_pytest_test_paths). Pass those paths explicitly so the
        # model's own tests for its own tests/-scoped code actually execute.
        own_test_paths = _added_pytest_test_paths(
            worktree, story_key, _default_branch()  # noqa: F821
        )
        if own_test_paths:
            test_cmd = [*test_cmd, *(str(worktree / p) for p in own_test_paths)]

    # Grade in a clean dev env, not the MCP server's operational one. The
    # server carries PIPELINE_* (pause/resume thresholds, backend dispatch,
    # model defaults) so advance_pipeline/check_usage see the real config —
    # but those same vars override the defaults the test suite asserts
    # against (e.g. usage_gate thresholds, dispatch backend routing) and
    # false-fail the gate for every Python story. Strip them so the suite
    # sees the same defaults a developer runs it under.
    #
    # Also strip LOCAL_AGENT_* and REPO_ROOT: LOCAL_AGENT_* (read-heavy
    # windows, chat retry, etc.) are harness-config the scheduler's plist may
    # set for a run (e.g. LOCAL_AGENT_READ_HEAVY_DISTINCT_WINDOWS raised to
    # let a model explore longer), and test_local_agent.py asserts the
    # DEFAULTS — an override that survives into the graded run false-fails
    # the suite for every story in a repo that vendors the pipeline's own
    # tests (the dashboard worktree is the pipeline repo, so its full suite
    # includes test_local_agent.py). REPO_ROOT is a per-plan sentinel
    # (/nonexistent-...) that likewise isn't a developer default.
    test_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }
    # Heavy build/test commands (cargo, npm, mvn, gradle, etc.) can run GB-
    # seconds of memory each. Serialize against other in-flight agents so
    # we never have N concurrent builds saturating the host. Cheap commands
    # (pytest, mvn, gradle, make, npm — depending on the project) skip the
    # lock entirely.
    if _is_heavy(test_cmd):  # noqa: F821
        with _heavy_lock():
            test_result = subprocess.run(
                test_cmd,
                check=False,
                cwd=test_dir,
                capture_output=True,
                text=True,
                env=test_env,
            )
    else:
        test_result = subprocess.run(
            test_cmd,
            check=False,
            cwd=test_dir,
            capture_output=True,
            text=True,
            env=test_env,
        )
    passed = test_result.returncode == 0

    # Diagnostic gap found live 2026-07-22 (MODE-29-REVIEW-STORY-LOCK-GUARD):
    # this test-run result was only ever returned transiently from the tool
    # call - nothing persisted it, so a status that later turned out to be
    # wrong (tests_passed recorded when the same command deterministically
    # fails on manual re-run) was impossible to diagnose after the fact.
    # Persist it on the story every time, regardless of pass/fail, so a
    # future occurrence leaves a paper trail. getattr() on stderr: some
    # test doubles for subprocess.run's return value don't define it.
    # The worktree's current HEAD sha is recorded alongside so later
    # dispatch/review/rebrief logic can detect when this cache is stale
    # (recorded at a past commit) and refuse to reuse it.
    check_sha = None
    if worktree and os.path.isdir(worktree):
        try:
            check_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, OSError):
            check_sha = None
    story["last_test_check"] = {
        "cmd": test_cmd,
        "cwd": str(test_dir),
        "returncode": test_result.returncode,
        "stdout_tail": (test_result.stdout or "")[-2000:],
        "stderr_tail": (getattr(test_result, "stderr", "") or "")[-2000:],
        "ts": datetime.now(timezone.utc).isoformat(),
        "sha": check_sha,
    }
    if passed:
        lint = _run_lint_gate(worktree, test_env)  # noqa: F821
        if lint is not None:
            lint["sha"] = check_sha
            story["last_lint_check"] = lint
            if lint["returncode"] != 0:
                passed = False

    # Only worth checking once the baseline (tests, lint) actually passed -
    # a story already failing on those has enough signal without this too.
    if passed:
        dead_functions = _find_dead_new_functions(worktree, _default_branch())  # noqa: F821
        story["last_dead_code_check"] = dead_functions
        if dead_functions:
            passed = False

    # The agent produced real output and the tests ran: the launch worked, so
    # clear any failed-launch attempts accumulated by earlier infra blips.
    # The step-cap and infra-failure streaks go too: both gate escalation on
    # CONSECUTIVE failures, and a dispatch that got this far breaks any
    # streak. Without this, non-consecutive failures accumulated across a
    # story's whole life (two infra deaths early, one much later, real
    # progress in between) would escalate as though they were consecutive.
    for _streak_key in (
        "dispatch_attempts",
        "step_cap_streak",
        "step_cap_streak_model",
        "infra_failure_streak",
        "infra_failure_streak_model",
    ):
        story.pop(_streak_key, None)

    # False-positive guard: tests passing against an untouched worktree
    # (e.g. main's suite against an empty branch because the agent parked
    # in a repetition loop without writing code) is not "the task is done."
    # require at least one commit on the agent branch beyond the base
    # branch before we count it as `tests_passed`. Mark `failed` (not
    # `interrupted`) because re-dispatching the same prompt to the same
    # model on the same empty worktree is unlikely to produce a different
    # outcome next tick; better to surface it for the dashboard.
    if passed and not _worktree_has_new_commits(  # noqa: F821
        worktree,
        story_key,
        base_branch=_default_branch(),  # noqa: F821
    ):
        base = _default_branch()  # noqa: F821
        story["status"] = "failed"
        story["failure_reason"] = (
            f"tests passed but agent branch has no new commits vs {base}; "
            "agent likely parked without writing code."
        )
        _atomic_write_json(manifest_path, manifest)
        return {"status": "failed", "reason": "empty_agent_branch"}

    story["status"] = "tests_passed" if passed else "failed"

    # Opt-in review-on-acceptance-fail (PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1):
    # route a dispatch whose acceptance oracle FAILED — but which produced real
    # work (new commits on the agent branch) — to review instead of straight to
    # "failed", so the reviewer evaluates the failing submission and the rework
    # loop re-dispatches the model up to REWORK_MAX_ATTEMPTS with the reviewer's
    # feedback. This engages the reviewer (previously unreachable for any
    # acceptance-failing cell: every such cell parked at "failed" with
    # rework_attempts=0, review_verdict=None, so the configured rework budget
    # and reviewer never ran — observed live, 2026-07-17, 0/9 mlx cells reached
    # review, zero GLM reviewer usage). Production-aligned: a reviewer sees
    # failing CI and REQUEST_CHANGES; the merge gate (_reverify_acceptance)
    # still blocks any APPROVEd-but-failing merge, so this never lands wrong
    # code. An empty-branch park (no real work) stays "failed" — re-dispatching
    # the same stuck prompt to the same model won't help. Opt-in so default
    # production behavior is unchanged; review_story's existing rework cap
    # (park/escalate after REWORK_MAX_ATTEMPTS) bounds the cycles.
    if (
        not passed
        and story["status"] == "failed"
        and os.environ.get("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "0") == "1"
        and _worktree_has_new_commits(  # noqa: F821
            worktree, story_key, base_branch=_default_branch()  # noqa: F821
        )
    ):
        story["status"] = "tests_passed"  # reviewable; reviewer sees the failure
        story["acceptance_failed_review"] = True

    # Mode 27 guard: the story is about to land on tests_passed (via either
    # path above — a genuine pass, or the PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL
    # opt-in surfacing a failing-but-reviewable submission) but HEAD is
    # unchanged since the last REQUEST_CHANGES recorded last_reviewed_sha —
    # the rework redispatch produced no new commit (e.g. it parked in a read
    # loop or crashed without committing, including an LLM transport error
    # mid-turn). Routing to tests_passed would hand review_story the same
    # SHA, where Mode 24's same-SHA skip guard loops forever (tests_passed is
    # not dispatch-eligible, so the story stalls invisibly — observed live
    # 2026-07-20 on the acceptance-fail-review path: 14+ consecutive silent
    # skip-notifications, the guard below originally covered only the
    # `passed=True` branch and missed this one). Route to changes_requested
    # so the scheduler redispatches, and count this no-progress retry against
    # the rework cap so a stuck agent parks rather than looping forever.
    if story["status"] == "tests_passed" and story.get("last_reviewed_sha"):
        head_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            cwd=worktree,
            capture_output=True,
            text=True,
        )
        if head_res.stdout.strip() == story["last_reviewed_sha"]:
            attempts = story.get("rework_attempts", 0) + 1
            story["rework_attempts"] = attempts
            # Zero commits since the last review is a stronger, distinct
            # signal from "made changes but the reviewer wasn't satisfied" -
            # see REWORK_MAX_ATTEMPTS_NO_COMMIT's docstring in config.py.
            # Applies uniformly regardless of escalation/oracle status.
            rework_cap = REWORK_MAX_ATTEMPTS_NO_COMMIT
            if attempts >= rework_cap:
                # Mirror review_story's own rework-cap escalation (Mode 24/28):
                # a local agent that keeps parking/crashing without writing
                # code is exactly the same "local tier couldn't finish this"
                # signal as a reviewer's rework budget running out - give it
                # to Claude before parking for a human, under the same
                # PIPELINE_BACKEND_DISPATCH=auto opt-in. Root-caused live
                # 2026-07-24: this guard was the one park path in the file
                # missing the hook every other rework-exhaustion park path
                # already had (review_story's three call sites), so a story
                # that hit exactly this path never got a chance at Claude.
                if _auto_escalation_enabled() and not story.get("escalated"):  # noqa: F821
                    _escalate_review_to_claude(
                        story,
                        story_key,
                        plan_name,
                        f"no new commit after {attempts} rework redispatches",
                    )
                    story["status"] = "changes_requested"
                    _atomic_write_json(manifest_path, manifest)
                    return {
                        "status": "changes_requested",
                        "reason": "no_new_commit_escalated_to_claude",
                    }
                story["status"] = "parked"
                story["parked_reason"] = (
                    f"no new commit after {attempts} rework redispatches - "
                    "agent keeps parking/crashing without writing code."
                )
                _atomic_write_json(manifest_path, manifest)
                _notify_user(  # noqa: F821
                    plan_name,
                    f"{story_key} parked: no new commit after {attempts} rework "
                    f"redispatches - needs human review.",
                )
                return {
                    "status": "parked",
                    "reason": "no_new_commit_rework_budget_exhausted",
                }
            story["status"] = "changes_requested"
            _atomic_write_json(manifest_path, manifest)
            return {
                "status": "changes_requested",
                "reason": "no_new_commit_since_last_review",
            }

    # T6: distinguish an explicit agent surrender from an ordinary red test
    # run. A missing/wrong API is a story-scoping bug, not a model-capability
    # gap - the terminal notify in advance_pipeline uses this to point a
    # human at "clarify the story" instead of the generic "tests failed".
    give_up_summary = _last_done_summary(agent_log) if not passed else ""  # noqa: F821
    if give_up_summary and _is_give_up_summary(give_up_summary):
        story["failure_kind"] = "give_up"
    else:
        story.pop("failure_kind", None)

    _atomic_write_json(manifest_path, manifest)

    result = {
        "status": story["status"],
        "tests_passed": passed,
        "test_command": test_cmd,
        "output_tail": test_result.stdout[-500:],
    }
    if story.get("failure_kind"):
        result["failure_kind"] = story["failure_kind"]
    return result


def _mark_story_done_impl(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Transition the ticket to Done and update the local manifest.
    Use after you've reviewed and merged the agent's PR.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    get_ticket_provider().set_state(story_key, LogicalState.DONE, plan_name)

    manifest = _store.get_manifest(plan_name)
    manifest["stories"][story_key]["status"] = "done"
    manifest["stories"][story_key].pop("parked_reason", None)
    _store.save_manifest(plan_name, manifest)

    # Check if all stories are now done
    all_done = all(s.get("status") == "done" for s in manifest["stories"].values())
    if all_done:
        if manifest.get("repo_root") == str(PIPELINE_SELF_REPO_ROOT):
            _record_retro_pending(plan_name, len(manifest["stories"]))
        return {
            "ok": True,
            "plan_completed": True,
            "stories": list(manifest["stories"].keys()),
        }
    return {"ok": True}


# Story fields patch_story may edit. Deliberately excludes "status" (use
# set_story_status), "worktree", "pid", "review_verdict" and other
# pipeline-owned runtime state - this tool is for correcting what the plan
# authored, not for mechanically bypassing the review/merge gates.
_PATCHABLE_STORY_FIELDS = frozenset(
    (
        "agent_instructions",
        "model",
        "persona",
        "risk",
        "dependencies",
        "acceptance",
        "pr_url",
        "summary",
        "tdd_split",
        "backend",
    )
)

# Every status value the pipeline itself assigns to a story (see the
# "status"] = / "status": literal assignments throughout this file). Kept as
# an explicit allowlist so set_story_status can't be used to invent a status
# the rest of the code doesn't know how to handle.
_VALID_STORY_STATUSES = frozenset(
    (
        "todo",
        "in_progress",
        "running",
        "interrupted",
        "failed",
        "tests_passed",
        "pr_open",
        "changes_requested",
        "parked",
        "done",
        "done",
    )
)


# Rebind the functions' globals to pipeline.server's namespace so that
# bare-name reads inside the bodies (e.g. `detect_test_command`,
# `_worktree_has_new_commits`, `_store`, `_validate_key`,
# `get_ticket_provider`, `_record_retro_pending`) resolve against
# pipeline.server at call time. This preserves the original behavior where the
# functions lived in pipeline.server and saw monkeypatched module globals
# (LOAD_GLOBAL does not consult a module-level __getattr__, so a plain
# re-export would not).
check_story_status = types.FunctionType(
    check_story_status.__code__,
    _server.__dict__,
    check_story_status.__name__,
    check_story_status.__defaults__,
    check_story_status.__closure__,
)
_mark_story_done_impl = types.FunctionType(
    _mark_story_done_impl.__code__,
    _server.__dict__,
    _mark_story_done_impl.__name__,
    _mark_story_done_impl.__defaults__,
    _mark_story_done_impl.__closure__,
)
