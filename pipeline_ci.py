"""CI status polling and pre-merge re-verification for the merge gate.

_repo_has_ci_configured distinguishes "genuinely no CI" from "CI exists but
hasn't registered checks for this branch yet" (PR #48 retro). _ci_status
polls ``gh pr checks <branch>`` until all checks reach a terminal bucket or
the timeout elapses. _ci_rerun retries a cancelled run once.

_reverify_acceptance re-runs a story's acceptance oracle against its
rebased worktree right before merge; _reverify_build runs the build command.
Both use _is_heavy / _heavy_lock from pipeline_concurrency.

The three env-var gates (PIPELINE_MERGE_CI_GATE, PIPELINE_MERGE_CI_TIMEOUT,
PIPELINE_MERGE_BUILD_GATE) live here rather than in pipeline_config because
they're CI-specific; tests patch pipeline_ci.<name> directly (Option B -
see PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).
"""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from pipeline_build_detect import (
    detect_test_command,
    detect_build_command,
    _acceptance_rel_paths,
    _scope_test_cmd_to_acceptance,
)
from pipeline_concurrency import _is_heavy, _heavy_lock


# ---------- CI env-var gates ----------
PIPELINE_MERGE_CI_GATE = os.environ.get("PIPELINE_MERGE_CI_GATE", "1") != "0"
PIPELINE_MERGE_CI_TIMEOUT = int(os.environ.get("PIPELINE_MERGE_CI_TIMEOUT", "300"))
# Mirrors PIPELINE_MERGE_CI_GATE's opt-out pattern for operators with slow
# builds who don't want a build re-run at the merge gate.
PIPELINE_MERGE_BUILD_GATE = os.environ.get("PIPELINE_MERGE_BUILD_GATE", "1") != "0"


def _repo_has_ci_configured() -> bool:
    """Whether the plan's repo (module-level REPO_ROOT, set by
    _scoped_repo_root for the duration of the merge gate) declares any GitHub
    Actions workflows at all. Distinguishes "genuinely no CI" from "CI exists
    but hasn't registered checks for this branch yet" in _ci_status - PR #48
    merged with a red Linux CI job because an empty `gh pr checks` result was
    treated identically to "no CI configured" (2026-07-07 web-client-epic
    retro §4)."""
    # Lazy import: pipeline_ci is imported by the server at top level, so
    # importing the server at module load here would cycle. REPO_ROOT is the
    # server's module-level global, patched by tests via p.REPO_ROOT and
    # scoped by _scoped_repo_root for each plan's merge gate.
    from pipeline_mcp_server import REPO_ROOT
    return (Path(REPO_ROOT) / ".github" / "workflows").is_dir()


def _ci_status(branch: str, *, timeout_s: int | None = None) -> dict[str, str]:
    """Poll ``gh pr checks <branch>`` until all checks reach a terminal bucket
    or the timeout elapses. Returns ``{"state": "pass"|"fail"|"cancelled"|
    "pending"|"none", "error": str}``.

      - ``pass``   every check passed -> safe to merge.
      - ``fail``   at least one check failed/errored/needs-action -> do not
        merge.
      - ``cancelled`` at least one check was cancelled (e.g. an abnormal
        queue delay) and no check failed/errored - worth exactly one
        automatic rerun before being treated as a failure; callers retry via
        `_ci_rerun` once, then re-poll.
      - ``pending`` checks still running (or a configured repo's checks
        haven't registered yet) at timeout -> do not merge (retry/park).
      - ``none``   no PR / unparseable output / a repo with no
        .github/workflows at all -> treat as pass (a repo without CI must
        not be blocked by this gate).
    Never raises; the merge adjudication loop decides what to do with the result.
    """
    if not PIPELINE_MERGE_CI_GATE:
        return {"state": "pass", "error": "CI gate disabled"}
    deadline = time.monotonic() + (timeout_s if timeout_s is not None else PIPELINE_MERGE_CI_TIMEOUT)
    while time.monotonic() < deadline:
        # `gh` may be absent or non-executable; treat that as "no CI" (none)
        # rather than letting OSError escape and crash the scheduler tick.
        try:
            r = subprocess.run(["gh", "pr", "checks", branch, "--json", "bucket"],
                               capture_output=True, text=True)
        except OSError as e:
            return {"state": "none", "error": f"gh unavailable: {e}"}
        if r.returncode != 0:
            return {"state": "none", "error": r.stderr.strip()[:200]}
        try:
            buckets = {c.get("bucket") for c in json.loads(r.stdout or "[]")}
        except ValueError:
            return {"state": "none", "error": "unparseable gh pr checks output"}
        if not buckets:
            if not _repo_has_ci_configured():
                return {"state": "none", "error": ""}
            # Checks are configured but haven't registered for this branch
            # yet - keep polling within the deadline rather than fast-pathing
            # to pass; falls through to "pending" below if they never do.
            time.sleep(10)
            continue
        if buckets & {"fail", "error", "action_required"}:
            return {"state": "fail", "error": ""}
        if "cancelled" in buckets:
            return {"state": "cancelled", "error": ""}
        if buckets <= {"pass"}:
            return {"state": "pass", "error": ""}
        time.sleep(10)  # still pending — keep polling
    return {"state": "pending", "error": "CI did not complete within timeout"}


def _ci_rerun(branch: str) -> bool:
    """Rerun the most recent CI run's failed/cancelled jobs for `branch` via
    `gh run rerun --failed`, for the one-shot auto-retry on a `cancelled`
    `_ci_status` result. Never raises - `gh`/network failures return False so
    the caller falls through to the ordinary fail/retry path rather than
    crashing the scheduler tick."""
    try:
        r = subprocess.run(
            ["gh", "run", "list", "--branch", branch, "--limit", "1", "--json", "databaseId"],
            capture_output=True, text=True,
        )
    except OSError:
        return False
    if r.returncode != 0:
        return False
    try:
        runs = json.loads(r.stdout or "[]")
    except ValueError:
        return False
    if not runs:
        return False
    run_id = runs[0].get("databaseId")
    if not run_id:
        return False
    try:
        rerun = subprocess.run(
            ["gh", "run", "rerun", str(run_id), "--failed"],
            capture_output=True, text=True,
        )
    except OSError:
        return False
    return rerun.returncode == 0


def _reverify_acceptance(story: dict[str, Any], worktree: str) -> dict[str, str]:
    """Re-run a story's acceptance oracle against its (rebased) worktree right
    before merge, as a second check independent of review and of whatever
    `check_story_status` decided when it set `tests_passed`.

    A repo's own CI (`_ci_status`) only exists if the repo has one configured;
    a story graded against a harness-owned `acceptance` block deserves the
    same re-verification regardless. Returns ``{"state": "pass"|"fail"|"none",
    "error": str}`` — ``"none"`` only when there's no worktree to test
    against. Stories with an `acceptance` block get the scoped oracle re-run;
    stories WITHOUT one (ordinary TDD stories) and non-pytest runners fall
    back to re-running the full suite, so a post-rebase break can't slip
    through (this is the MBW safety net — see commit history). Operators
    with slow suites can opt out via ``PIPELINE_REVERIFY_FULL_SUITE=0`` to
    restore the old silent-pass behavior.
    """
    acceptance = story.get("acceptance") or []
    if not worktree or not Path(worktree).is_dir():
        return {"state": "none", "error": ""}
    test_dir, test_cmd = detect_test_command(Path(worktree))
    # Decide what to run: scoped to acceptance paths when the story carries
    # an acceptance block AND the runner can be safely scoped (pytest path
    # args, cargo --test, npm/yarn node --test — see _scope_test_cmd_to_acceptance);
    # otherwise the full suite. The full-suite path is the MBW safety net — a
    # story without an acceptance block (the common case for real-project
    # stories) still gets the rebased branch's full test suite re-run before
    # merge.
    scoped = None
    if acceptance:
        acceptance_paths = [str(Path(worktree) / p) for p in _acceptance_rel_paths(story)]
        scoped = _scope_test_cmd_to_acceptance(test_cmd, acceptance_paths, test_dir)
    if scoped is not None:
        test_cmd = scoped
    elif not acceptance:
        # No acceptance block: run the full suite unless the operator opted out.
        if os.environ.get("PIPELINE_REVERIFY_FULL_SUITE", "1") == "0":
            return {"state": "none", "error": ""}
    # Same operational-env stripping as check_story_status: PIPELINE_*/
    # LOCAL_AGENT_*/REPO_ROOT are harness config, not developer defaults the
    # suite asserts against.
    test_env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }
    if _is_heavy(test_cmd):
        with _heavy_lock():
            r = subprocess.run(test_cmd, cwd=test_dir, capture_output=True, text=True, env=test_env)
    else:
        r = subprocess.run(test_cmd, cwd=test_dir, capture_output=True, text=True, env=test_env)
    if r.returncode == 0:
        return {"state": "pass", "error": ""}
    return {"state": "fail", "error": (r.stdout + r.stderr).strip()[-500:]}


def _reverify_build(worktree: str) -> dict[str, str]:
    """Run the rebased worktree's build command (if one is detectable)
    right before merge, alongside _reverify_acceptance's test re-run.

    Neither the reviewer nor the dispatched agent's own "tests pass" report
    is proof the project actually builds - PR #48 merged with `npm run
    build` broken (Node's `crypto` module can't bundle for a browser
    target, a real pre-existing bug) because nobody ran it before merge
    (2026-07-07 web-client-epic retro §3.1). Returns {"state":
    "pass"|"fail"|"none", "error": str} - "none" when the gate is disabled,
    there's no worktree to build against, or no build command is
    detectable (a repo without a build step must merge freely).
    """
    if not PIPELINE_MERGE_BUILD_GATE:
        return {"state": "none", "error": "build gate disabled"}
    if not worktree or not Path(worktree).is_dir():
        return {"state": "none", "error": ""}
    detected = detect_build_command(Path(worktree))
    if detected is None:
        return {"state": "none", "error": ""}
    build_dir, build_cmd = detected
    # Same operational-env stripping as _reverify_acceptance: PIPELINE_*/
    # LOCAL_AGENT_*/REPO_ROOT are harness config, not developer defaults the
    # build asserts against.
    build_env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }
    if _is_heavy(build_cmd):
        with _heavy_lock():
            r = subprocess.run(build_cmd, cwd=build_dir, capture_output=True, text=True, env=build_env)
    else:
        r = subprocess.run(build_cmd, cwd=build_dir, capture_output=True, text=True, env=build_env)
    if r.returncode == 0:
        return {"state": "pass", "error": ""}
    return {"state": "fail", "error": (r.stdout + r.stderr).strip()[-500:]}


__all__ = [
    "PIPELINE_MERGE_CI_GATE",
    "PIPELINE_MERGE_CI_TIMEOUT",
    "PIPELINE_MERGE_BUILD_GATE",
    "_repo_has_ci_configured",
    "_ci_status",
    "_ci_rerun",
    "_reverify_acceptance",
    "_reverify_build",
]