"""
check_story_status: poll a dispatched agent's story, run its tests in the
worktree, and report pass/fail without auto-merging.

Extracted verbatim from pipeline/server.py (behavior-preserving file move).
"""

import json  # noqa: F401
import os  # noqa: F401
import subprocess  # noqa: F401
import time  # noqa: F401
from datetime import UTC, datetime  # noqa: F401
from pathlib import Path
from typing import Any

from . import bound_records, server_bindings, tenant_exec  # noqa: F401
from .build_detect import (
    _acceptance_rel_paths,  # noqa: F401
    _is_pytest_cmd,  # noqa: F401
    failed_node_ids,  # noqa: F401
)
from .config import (
    DISPATCH_STALE_ACTIVITY_SECONDS,  # noqa: F401
    DISPATCH_WATCHDOG_SECONDS,
    INFRA_FAILURE_FALLBACK_THRESHOLD,  # noqa: F401
    INFRA_FAILURE_LOG_SUBSTRING,  # noqa: F401
    STEP_CAP_FALLBACK_THRESHOLD,  # noqa: F401
    STEP_CAP_MARKERS,  # noqa: F401
)
from .detached_grade import (
    GRADE_WRAPPER,  # noqa: F401
    _baseline_exempted_failures,  # noqa: F401
    collect_detached_grade,  # noqa: F401
    start_detached_grade,  # noqa: F401
)
from .failure_reasons import auth_failure_hint, tests_failed_reason
from .git_ops import _commit_wip, _untrack_scratchpad  # noqa: F401
from .no_progress import classify_no_progress  # noqa: F401
from .parsers import (
    _is_give_up_summary,
)
from .rebrief import append_cleanup_guidance  # noqa: F401
from .story_fields import merge_into_fresh_manifest
from .story_status_failures import check_infra_failure, check_step_cap
from .story_status_liveness import check_empty_launch_log, check_process_liveness
from .story_status_records import (
    _clear_failure_streaks,
    _record_test_check,  # noqa: F401
)
from .story_status_review_reuse import (
    check_no_new_commit_since_review,
    route_acceptance_failure_to_review,
)
from .story_status_tests import (
    apply_static_gates,
    check_acceptance_tampered,
    record_test_result,
    resolve_test_result,
    scope_test_command,
    story_test_env,
)
from .test_scope_run import grade_test_cmd  # noqa: F401
from .wedge_io import collect_story_wedge_signals  # noqa: F401

# The detached-grading watchdog reuses the dispatch watchdog's threshold so a
# single policy governs both "how long may a grade/dispatch stay outstanding"
# decisions (no new env var — see DETACHED_GRADE_WATCHDOG_SECONDS's use in
# check_story_status's dead-pid recovery path).
DETACHED_GRADE_WATCHDOG_SECONDS = DISPATCH_WATCHDOG_SECONDS


def check_story_status(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Check whether a dispatched agent has finished. If complete, runs tests
    in the worktree and reports pass/fail without auto-merging.
    """
    server_bindings.server()._validate_key(plan_name)
    server_bindings.server()._validate_key(story_key)
    manifest = bound_records.get_manifest(server_bindings.server()._store, plan_name)
    if manifest is None:
        return {"ok": False, "error": "Plan not found"}
    story = manifest["stories"].get(story_key)
    _story_keys = tuple(story) if isinstance(story, dict) else ()
    if not story or "pid" not in story:
        return {"ok": False, "error": "Story not dispatched"}
    if story["status"] == "interrupted":
        # Incomplete by definition — running tests here would just record a
        # spurious failure instead of leaving it resumable.
        return {"status": "interrupted", "pid": story["pid"]}
    if story["status"] in ("done", "merged"):
        # Finished stories keep their pid but their worktree is gone, so
        # there is nothing to test; report the terminal state as-is.
        return {"status": story["status"], "pid": story["pid"]}

    pid = story["pid"]
    result = check_process_liveness(plan_name, story_key, story, manifest, pid)
    if result is not None:
        return result

    worktree = Path(story["worktree"])
    agent_log = worktree / "agent.log"
    result = check_empty_launch_log(plan_name, story_key, story, manifest, pid, agent_log)
    if result is not None:
        return result

    last_log_line = server_bindings.server()._last_nonempty_line(agent_log) if agent_log.exists() else ""
    result = check_infra_failure(
        plan_name, story_key, story, manifest, pid, worktree, last_log_line)
    if result is not None:
        return result
    result = check_step_cap(
        plan_name, story_key, story, manifest, pid, worktree, last_log_line)
    if result is not None:
        return result

    result = check_acceptance_tampered(plan_name, story_key, story, manifest, worktree)
    if result is not None:
        return result

    test_dir, test_cmd = detected = server_bindings.server().detect_test_command(worktree)
    test_cmd = scope_test_command(story, story_key, worktree, test_dir, test_cmd, detected[1])
    test_env = story_test_env()
    # Dead-pid recovery (2026-09-02 live incident): a dispatch whose process
    # is gone used to be re-graded SYNCHRONOUSLY here, inside the scheduler
    # tick - advance.py calls check_story_status for every in_progress story,
    # so one dead story blocked dispatch/review/merge for ALL plans for the
    # duration of a full suite run (~19 min of scheduler silence, observed
    # live). The grade is now handed to a detached wrapper process (adp-01's
    # start_detached_grade) and picked up by a LATER tick, keyed on per-story
    # bookkeeping persisted in the manifest:
    #
    #   no grading_pid    -> spawn the detached grade, record grading_pid /
    #                        grading_started_at / grading_result_path, and free
    #                        the tick immediately.
    #   grading_pid alive -> pure read: still grading, manifest untouched.
    #   grading_pid dead  -> collect the result (fail-closed when the wrapper
    #                        left none) and resume the post-grade logic below
    #                        through a subprocess.run-shaped shim; the
    #                        bookkeeping is consumed so a future re-grade
    #                        starts clean.
    _early, test_result = server_bindings.server()._detached_grade_lifecycle(
        story, story_key, pid, worktree, manifest, plan_name,
        test_cmd, test_dir, test_env,
        _exports=server_bindings.server(),
    )
    if _early is not None:
        return _early
    test_result = resolve_test_result(test_result, test_cmd, test_dir, test_env)
    passed, check_sha = record_test_result(
        story, story_key, test_cmd, test_dir, test_result, worktree)
    passed = apply_static_gates(story, story_key, worktree, check_sha, test_env, passed)

    # The agent produced real output and the tests ran: the launch worked, so
    # clear any failed-launch attempts accumulated by earlier infra blips.
    # The step-cap, infra-failure and watchdog streaks go too: all gate
    # escalation on CONSECUTIVE failures, and a dispatch that got this far
    # breaks any streak. Without this, non-consecutive failures accumulated
    # across a story's whole life (two infra deaths early, one much later,
    # real progress in between) would escalate as though they were
    # consecutive.
    _clear_failure_streaks(story)

    # A red grade confined to pre-existing test files the branch never touched
    # is a PLAN CONFLICT, not a failed attempt: the brief and those tests
    # contradict each other, so the overlord role rules on it. Returning here,
    # before the status/rework assignment below, is what keeps a conflict from
    # being charged as a failed attempt. The helper resolves through globals()
    # for the same reason as _untrack_scratchpad: this body's globals ARE
    # pipeline.server's namespace, so the probe sees the pytest-conditional
    # export (absent under pytest, where every pre-existing grade test pins
    # subprocess.run's call sequence). The test result and the streaks above
    # are recorded first: a security-relevant ruling must still leave a
    # test-run record on the manifest.
    if not passed:
        _intercept = getattr(server_bindings.server(), "_plan_conflict_intercept", None)
        if _intercept is not None:
            _conflict = _intercept(
                plan_name, story_key, story, test_result, worktree,
                manifest, pid,
            )
            if _conflict is not None:
                return _conflict

    # False-positive guard: tests passing against an untouched worktree
    # (e.g. main's suite against an empty branch because the agent parked
    # in a repetition loop without writing code) is not "the task is done."
    # require at least one commit on the agent branch beyond the base
    # branch before we count it as `tests_passed`. Mark `failed` (not
    # `interrupted`) because re-dispatching the same prompt to the same
    # model on the same empty worktree is unlikely to produce a different
    # outcome next tick; better to surface it for the dashboard.
    if passed and not server_bindings.server()._worktree_has_new_commits(
        worktree,
        story_key,
        base_branch=server_bindings.server()._default_branch(),
    ):
        base = server_bindings.server()._default_branch()
        story["status"] = "failed"
        story["failure_reason"] = (
            f"tests passed but agent branch has no new commits vs {base}; "
            "agent likely parked without writing code."
            + auth_failure_hint(story.get("log"))
        )
        bound_records.put_manifest(server_bindings.server()._store, plan_name, manifest)
        return {"status": "failed", "reason": "empty_agent_branch"}

    story["status"] = "tests_passed" if passed else "failed"

    route_acceptance_failure_to_review(story, story_key, worktree, passed)

    # The reason names this attempt only: a pass drops any reason an earlier
    # attempt left, and a red test run gets its exit code. Output is never
    # copied into the reason. A red grade whose test command passed (lint or
    # dead-code) has no new reason to give, so the stale one is dropped.
    if story["status"] == "tests_passed":
        story.pop("failure_reason", None)
    elif story["status"] == "failed":
        story.pop("failure_reason", None)
        returncode = (story.get("last_test_check") or {}).get("returncode")
        if returncode not in (None, 0):
            story["failure_reason"] = tests_failed_reason(returncode)

    result = check_no_new_commit_since_review(
        plan_name, story_key, story, manifest, worktree, _story_keys)
    if result is not None:
        return result

    # T6: distinguish an explicit agent surrender from an ordinary red test
    # run. A missing/wrong API is a story-scoping bug, not a model-capability
    # gap - the terminal notify in advance_pipeline uses this to point a
    # human at "clarify the story" instead of the generic "tests failed".
    give_up_summary = server_bindings.server()._last_done_summary(agent_log) if not passed else ""
    if give_up_summary and _is_give_up_summary(give_up_summary):
        story["failure_kind"] = "give_up"
    else:
        story.pop("failure_kind", None)

    bound_records.put_manifest(server_bindings.server()._store, plan_name,
        merge_into_fresh_manifest(server_bindings.server()._store, plan_name, story_key, story,
            manifest, drop=("failure_kind",), keys_at_entry=_story_keys))  # type: ignore[arg-type]

    result = {
        "status": story["status"],
        "tests_passed": passed,
        "test_command": test_cmd,
        "output_tail": test_result.stdout[-500:],
    }
    if story.get("failure_kind"):
        result["failure_kind"] = story["failure_kind"]
    return result
