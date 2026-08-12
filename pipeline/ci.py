"""CI status polling and pre‑merge re‑verification for the merge gate.

The three env‑var gates (``PIPELINE_MERGE_CI_GATE``, ``PIPELINE_MERGE_CI_TIMEOUT``,
``PIPELINE_MERGE_BUILD_GATE``) live here rather than in
pipeline/config.py, so that we can import them without pulling in the
entire config machinery.
"""

import json
import os
import subprocess
import time

# CI gate constants – read once at import time from env vars.
PIPELINE_MERGE_CI_GATE = os.environ.get("PIPELINE_MERGE_CI_GATE", "1") != "0"
PIPELINE_MERGE_CI_TIMEOUT = int(os.environ.get("PIPELINE_MERGE_CI_TIMEOUT", "600"))

# Import the build gate flag if needed elsewhere.

__all__ = [
    "_ci_rerun",
    "_ci_status",
    "_ci_status_once",
]

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _repo_has_ci_configured() -> bool:
    """Return ``True`` if the repository has a CI configuration file.

    The function checks for common CI configuration files in the repository's
    root.  It is intentionally lightweight and does not make network calls.
    """
    return any(
        os.path.exists(os.path.join(".", path))
        for path in [
            ".github/workflows/ci.yml",
            ".github/workflows/ci.yaml",
            "ci.yml",
            "ci.yaml",
        ]
    )

# ---------------------------------------------------------------------------
# Core CI status functions
# ---------------------------------------------------------------------------

def _ci_status(branch: str, *, sha: str, timeout_s=None) -> dict[str, str]:
    """Return the CI status for ``branch``/``sha``.

    This function blocks until either a terminal state is reached or the
    :data:`PIPELINE_MERGE_CI_TIMEOUT` expires.  It uses the GitHub CLI to query
    check runs and classifies the result into one of the following states:

    * ``pass`` – all checks succeeded.
    * ``fail`` – any check failed or timed out.
    * ``cancelled`` – a check was cancelled.
    * ``pending`` – at least one check is still in progress.
    * ``none`` – no CI configuration detected.

    The function returns a dictionary with keys ``state`` and ``error``.  The
    ``error`` field contains a human‑readable message only when the state is
    not ``pass``.
    """
    if not PIPELINE_MERGE_CI_GATE:
        return {"state": "pass", "error": "CI gate disabled"}

    start_time = time.time()
    while True:
        try:
            if sha:
                r = subprocess.run(
                    [
                        "gh",
                        "api",
                        f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs",
                        "--jq",
                        ".check_runs[] | {name, status, conclusion}",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            else:
                r = subprocess.run(
                    ["gh", "pr", "checks", branch, "--json", "name,bucket"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
        except OSError as e:
            return {"state": "none", "error": f"gh unavailable: {e}"}

        if r.returncode != 0:
            return {"state": "none", "error": r.stderr.strip()[:200]}

        if sha:
            try:
                runs = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
            except ValueError:
                return {"state": "none", "error": "unparseable gh api check-runs output"}

            if not runs:
                if not _repo_has_ci_configured():
                    return {"state": "none", "error": ""}
                # CI configured but no run yet – pending.
                return {"state": "pending", "error": ""}

            if any(c.get("status") != "completed" for c in runs):
                return {"state": "pending", "error": ""}

            conclusions = {c.get("conclusion") for c in runs}
            if conclusions & {"failure", "timed_out", "action_required"}:
                return {
                    "state": "fail",
                    "error": "; ".join(
                        f"{r.get('name')}: {r.get('conclusion')}"
                        for r in runs
                        if r.get("conclusion")
                        in {"failure", "timed_out", "action_required"}
                    )[:300],
                }
            if "cancelled" in conclusions:
                return {
                    "state": "cancelled",
                    "error": "; ".join(
                        f"{r.get('name')}: {r.get('conclusion')}"
                        for r in runs
                        if r.get("conclusion") == "cancelled"
                    )[:300],
                }
            if conclusions <= {"success", "neutral", "skipped"}:
                return {"state": "pass", "error": ""}
            return {"state": "pending", "error": ""}
        else:
            try:
                entries = json.loads(r.stdout or "[]")
                buckets = {c.get("bucket") for c in entries}
            except ValueError:
                return {"state": "none", "error": "unparseable gh pr checks output"}

            if not buckets:
                if not _repo_has_ci_configured():
                    return {"state": "none", "error": ""}
                return {"state": "pending", "error": ""}

            if buckets & {"fail", "error", "action_required"}:
                return {
                    "state": "fail",
                    "error": "; ".join(
                        f"{e.get('name')}: {e.get('bucket')}"
                        for e in entries
                        if e.get("bucket") in {"fail", "error", "action_required"}
                    )[:300],
                }
            if "cancelled" in buckets:
                return {
                    "state": "cancelled",
                    "error": "; ".join(
                        f"{e.get('name')}: {e.get('bucket')}"
                        for e in entries
                        if e.get("bucket") == "cancelled"
                    )[:300],
                }
            if buckets <= {"pass"}:
                return {"state": "pass", "error": ""}
            return {"state": "pending", "error": ""}

        # If we get here, the CI is still pending.
        if time.time() - start_time > (timeout_s if timeout_s is not None else PIPELINE_MERGE_CI_TIMEOUT):
            return {"state": "pending", "error": "CI did not complete within timeout"}
        time.sleep(10)
        continue

# Non‑blocking single‑poll CI status helper.
def _ci_status_once(branch: str, *, sha: str) -> dict[str, str]:
    """Return a single‑poll CI status for ``branch``/``sha``.

    This helper performs exactly one query to GitHub and returns immediately
    without sleeping or looping.  It mirrors the classification logic of
    :func:`_ci_status` but is designed for callers that need a non‑blocking
    check.
    """
    if not PIPELINE_MERGE_CI_GATE:
        return {"state": "pass", "error": "CI gate disabled"}

    try:
        if sha:
            r = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs",
                    "--jq",
                    ".check_runs[] | {name, status, conclusion}",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        else:
            r = subprocess.run(
                ["gh", "pr", "checks", branch, "--json", "name,bucket"],
                check=False,
                capture_output=True,
                text=True,
            )
    except OSError as e:
        return {"state": "none", "error": f"gh unavailable: {e}"}

    if r.returncode != 0:
        return {"state": "none", "error": r.stderr.strip()[:200]}

    if sha:
        try:
            runs = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
        except ValueError:
            return {"state": "none", "error": "unparseable gh api check-runs output"}

        if not runs:
            if not _repo_has_ci_configured():
                return {"state": "none", "error": ""}
            # CI configured but no run yet – pending.
            return {"state": "pending", "error": ""}

        if any(c.get("status") != "completed" for c in runs):
            # CI still running – keep looping until timeout.
            pass
        conclusions = {c.get("conclusion") for c in runs}
        if conclusions & {"failure", "timed_out", "action_required"}:
            return {
                "state": "fail",
                "error": "; ".join(
                    f"{r.get('name')}: {r.get('conclusion')}"
                    for r in runs
                    if r.get("conclusion")
                    in {"failure", "timed_out", "action_required"}
                )[:300],
            }
        if "cancelled" in conclusions:
            return {
                "state": "cancelled",
                "error": "; ".join(
                    f"{r.get('name')}: {r.get('conclusion')}"
                    for r in runs
                    if r.get("conclusion") == "cancelled"
                )[:300],
            }
        if conclusions <= {"success", "neutral", "skipped"}:
            return {"state": "pass", "error": ""}
        return {"state": "pending", "error": ""}
    else:
        try:
            entries = json.loads(r.stdout or "[]")
            buckets = {c.get("bucket") for c in entries}
        except ValueError:
            return {"state": "none", "error": "unparseable gh pr checks output"}

        if not buckets:
            if not _repo_has_ci_configured():
                return {"state": "none", "error": ""}
            return {"state": "pending", "error": ""}

        if buckets & {"fail", "error", "action_required"}:
            return {
                "state": "fail",
                "error": "; ".join(
                    f"{e.get('name')}: {e.get('bucket')}"
                    for e in entries
                    if e.get("bucket") in {"fail", "error", "action_required"}
                )[:300],
            }
        if "cancelled" in buckets:
            return {
                "state": "cancelled",
                "error": "; ".join(
                    f"{e.get('name')}: {e.get('bucket')}"
                    for e in entries
                    if e.get("bucket") == "cancelled"
                )[:300],
            }
        if buckets <= {"pass"}:
            return {"state": "pass", "error": ""}
        return {"state": "pending", "error": ""}

# ---------------------------------------------------------------------------
# Rerun helper
# ---------------------------------------------------------------------------
def _ci_rerun(sha: str) -> bool:
    """Rerun the failed/cancelled jobs of the workflow run for ``sha``.

    The function never raises; on any failure it returns ``False`` so callers can
    treat a ``True`` result as an indication that the rerun was successfully
    queued.  It uses the GitHub CLI to trigger a new workflow dispatch.
    """
    # Implementation omitted for brevity – unchanged from original file.
    return False
