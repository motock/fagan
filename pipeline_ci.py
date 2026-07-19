"""CI status polling for the merge gate.

_repo_has_ci_configured distinguishes "genuinely no CI" from "CI exists but
hasn't registered checks for this branch yet" (PR #48 retro). _ci_status
polls ``gh pr checks <branch>`` until all checks reach a terminal bucket or
the timeout elapses. _ci_rerun retries a cancelled run once.

The three env-var gates (PIPELINE_MERGE_CI_GATE, PIPELINE_MERGE_CI_TIMEOUT,
PIPELINE_MERGE_BUILD_GATE) live here rather than in pipeline_config because
they're CI-specific; tests patch pipeline_ci.<name> directly (Option B -
see PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).

_reverify_acceptance and _reverify_build stay in pipeline_mcp_server.py for
now: they call _is_heavy / _heavy_lock (concurrency helpers not yet
extracted) and detect_test_command / detect_build_command (already in
pipeline_build_detect). They'll move once pipeline_concurrency lands.
"""

import json
import os
import subprocess
import time
from pathlib import Path


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


__all__ = [
    "PIPELINE_MERGE_CI_GATE",
    "PIPELINE_MERGE_CI_TIMEOUT",
    "PIPELINE_MERGE_BUILD_GATE",
    "_repo_has_ci_configured",
    "_ci_status",
    "_ci_rerun",
]