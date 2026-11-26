"""Test-grading phases of check_story_status: scope the test command to the
story's acceptance fixtures, run it when no detached grade supplied a result,
and record the outcome on the story.

Extracted verbatim from pipeline/story_status.py (behavior-preserving move).
"""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import server_bindings, tenant_exec
from .build_detect import _acceptance_rel_paths, _is_pytest_cmd
from .detached_grade import _baseline_exempted_failures
from .story_status_records import _record_test_check
from .test_scope_run import grade_test_cmd


def scope_test_command(
    story: dict[str, Any],
    story_key: str,
    worktree: Path,
    test_dir: Path,
    test_cmd: list[str],
    original_cmd: list[str],
) -> list[str]:
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
    # A rework redispatch is held to a full-suite-green done-bar (dispatch.py
    # raises it on the agent side under exactly this condition). Re-applying the
    # FM-A acceptance scoping here would grade the same round on a narrower bar
    # than the one the agent was held to, so a round the agent could not green
    # would land on tests_passed anyway - fail-open (observed live 2026-09-21:
    # the oracle parked at the suite-reject cap with the branch tip unchanged
    # and the repo-wide suite red, and the tick still promoted the story off the
    # scoped fixture run).
    rework_full_suite = (
        story.get("backend", "local") in server_bindings.server()._LOCAL_BACKEND_NAMES
        and bool(story.get("ci_rework") or story.get("review_feedback"))
    )
    if acceptance and not rework_full_suite:
        acceptance_paths = [str(worktree / p) for p in _acceptance_rel_paths(story)]
        scoped = server_bindings.server()._scope_test_cmd_to_acceptance(test_cmd, acceptance_paths, test_dir)
        if scoped is not None:
            test_cmd = scoped
    elif _is_pytest_cmd(test_cmd):
        # Mode 42 done-bar blindspot: a story without an acceptance block
        # whose deliverable lives under tests/ can add its own test_*.py
        # there, which an --ignore then hides from this same gate run
        # (see _added_pytest_test_paths). Pass those paths explicitly so the
        # model's own tests for its own tests/-scoped code actually execute.
        #
        # But ONLY the paths that are genuinely hidden. A positional path
        # argument REPLACES pytest's default collection rather than adding
        # to it, so appending an already-collected path silently converts
        # this full-suite done-bar into a single-file done-bar:
        #
        #   pytest --override-ini=testpaths=. --ignore=tests/benchmark
        #       -> collects the whole suite
        #   pytest --override-ini=testpaths=. --ignore=tests/benchmark A.py
        #       -> collects ONLY A.py
        #
        # The unconditional append was safe only while
        # _apply_pytest_collection_overrides emitted a blanket
        # `--ignore=tests` (every tests/ path was hidden, so every path
        # needed passing). That blanket ignore was removed - it now emits
        # only --ignore=tests/benchmark --ignore=tests/experiments - which
        # left the append narrowing the gate for the common case.
        #
        # Live consequence (2026-09-02, PR #552): story f68b8350's gate ran
        # only its own new tests/unit/test_guard_liveness_check.py (23
        # tests), never ran the pre-existing sibling test file that was red,
        # returned 0, and merged onto a broken master.
        own_test_paths = server_bindings.server()._added_pytest_test_paths(
            worktree, story_key, server_bindings.server()._default_branch()
        )
        ignored = server_bindings.server()._pytest_ignored_paths(test_cmd)
        hidden_paths = [
            rel for rel in own_test_paths
            if server_bindings.server()._is_hidden_by_pytest_ignores(rel, ignored)
        ]
        if hidden_paths:
            test_cmd = [*test_cmd, *(str(worktree / p) for p in hidden_paths)]

    return grade_test_cmd(story, story_key, worktree, test_dir, original_cmd, test_cmd)


def story_test_env() -> dict[str, str]:
    """The environment the graded test run sees."""
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
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }


def resolve_test_result(
    test_result: subprocess.CompletedProcess[Any] | None,
    test_cmd: list[str],
    test_dir: Path,
    test_env: dict[str, str],
) -> subprocess.CompletedProcess[Any]:
    """Return the detached grade's result, or run the test command now when no
    detached grade supplied one."""
    if test_result is not None:
        return test_result
    # Heavy build/test commands (cargo, npm, mvn, gradle, etc.) can run GB-
    # seconds of memory each. Serialize against other in-flight agents so
    # we never have N concurrent builds saturating the host. Cheap commands
    # (pytest, mvn, gradle, make, npm — depending on the project) skip the
    # lock entirely.
    if server_bindings.server()._is_heavy(test_cmd):
        with server_bindings.server()._heavy_lock():
            return _run_test_command(test_cmd, test_dir, test_env)
    return _run_test_command(test_cmd, test_dir, test_env)


def _run_test_command(
    test_cmd: list[str], test_dir: Path, test_env: dict[str, str]
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        tenant_exec.wrap(test_cmd, cwd=str(test_dir), kind="test"),
        check=False,
        cwd=test_dir,
        capture_output=True,
        text=True,
        env=test_env,
    )


def record_test_result(
    story: dict[str, Any],
    story_key: str,
    test_cmd: list[str],
    test_dir: Path,
    test_result: subprocess.CompletedProcess[Any],
    worktree: Path,
) -> tuple[bool, str | None]:
    """Decide pass/fail for the test run (honouring the baseline exemption) and
    persist the run on the story. Returns (passed, check_sha)."""
    passed = test_result.returncode == 0
    if not passed:
        exempted = _baseline_exempted_failures(story, test_result)
        if exempted:
            # This red run failed only on node ids the pre-dispatch baseline
            # was already failing before this story touched the worktree, so
            # the grade must not reject the story for them. Recorded, never
            # silent: the exempted ids stay on the manifest for audit.
            passed = True
            story["grade_baseline_exemption"] = {
                "failed_node_ids": exempted,
                "baseline_returncode": story["baseline_test_check"]["returncode"],
                "ts": datetime.now(UTC).isoformat(),
            }

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
    check_sha = _record_test_check(story, test_cmd, test_dir, test_result, worktree)
    return passed, check_sha
