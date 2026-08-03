"""CI status polling and pre-merge re-verification for the merge gate.

The three env-var gates (``PIPELINE_MERGE_CI_GATE``, ``PIPELINE_MERGE_CI_TIMEOUT``,
``PIPELINE_MERGE_BUILD_GATE``) live here rather than in
:mod:`pipeline_config` because they are CI‑specific; tests patch the module
directly.
"""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

# Import helpers for test detection and scoping.  These imports are used in
# :func:`_reverify_acceptance` below; the ``noqa`` comments were removed as
# they were incorrect.
from .build_detect import (
    _acceptance_rel_paths,
    _added_pytest_test_paths,
    _is_pytest_cmd,
    _scope_test_cmd_to_acceptance,
    detect_build_command,
    detect_test_command,
)
from .concurrency import _heavy_lock, _is_heavy

# ---------- CI env-var gates ----------
PIPELINE_MERGE_CI_GATE = os.environ.get("PIPELINE_MERGE_CI_GATE", "1") != "0"
PIPELINE_MERGE_CI_TIMEOUT = int(os.environ.get("PIPELINE_MERGE_CI_TIMEOUT", "300"))
# Mirrors ``PIPELINE_MERGE_CI_GATE``'s opt‑out pattern for operators with
# slow builds who don't want a build re‑run at the merge gate.
PIPELINE_MERGE_BUILD_GATE = os.environ.get("PIPELINE_MERGE_BUILD_GATE", "1") != "0"


def _repo_has_ci_configured() -> bool:
    """Return ``True`` if the repo declares any GitHub Actions workflows.

    Distinguishes *genuinely no CI* from *CI exists but hasn't registered
    checks for this branch yet* in :func:`_ci_status` – PR #48 merged with a red
    Linux CI job because an empty ``gh pr checks`` result was treated
    identically to "no CI configured".
    """
    from .server import REPO_ROOT

    return (Path(REPO_ROOT) / ".github" / "workflows").is_dir()


def _ci_status(
    branch: str, *, sha: str, timeout_s: int | None = None
) -> dict[str, str]:
    """Poll GitHub for the status of checks on ``sha`` or ``branch``.

    Parameters
    ----------
    branch:
        The PR branch name to query when ``sha`` is empty.
    sha:
        Commit SHA to query; if falsy, falls back to a branch‑scoped query.
    timeout_s:
        Optional override of :data:`PIPELINE_MERGE_CI_TIMEOUT`.

    Returns
    -------
    dict[str, str]
        ``{"state": <state>, "error": <message>}``
    """
    if not PIPELINE_MERGE_CI_GATE:
        return {"state": "pass", "error": "CI gate disabled"}

    deadline = time.monotonic() + (
        timeout_s if timeout_s is not None else PIPELINE_MERGE_CI_TIMEOUT
    )
    while time.monotonic() < deadline:
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
                    check=False, capture_output=True,
                    text=True,
                )
            else:
                r = subprocess.run(
                    ["gh", "pr", "checks", branch, "--json", "name,bucket"],
                    check=False, capture_output=True,
                    text=True,
                )
        except OSError as e:
            return {"state": "none", "error": f"gh unavailable: {e}"}

        if r.returncode != 0:
            return {"state": "none", "error": r.stderr.strip()[:200]}

        if sha:
            try:
                runs = [
                    json.loads(line) for line in r.stdout.splitlines() if line.strip()
                ]
            except ValueError:
                return {
                    "state": "none",
                    "error": "unparseable gh api check-runs output",
                }

            if not runs:
                if not _repo_has_ci_configured():
                    return {"state": "none", "error": ""}
                # Checks are configured but haven't registered for this SHA yet – keep polling.
                time.sleep(10)
                continue

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
            if any(c.get("status") != "completed" for c in runs):
                time.sleep(10)  # still pending — keep polling
                continue
            if conclusions <= {"success", "neutral", "skipped"}:
                return {"state": "pass", "error": ""}
            time.sleep(10)  # still pending — keep polling
        else:
            try:
                entries = json.loads(r.stdout or "[]")
                buckets = {c.get("bucket") for c in entries}
            except ValueError:
                return {"state": "none", "error": "unparseable gh pr checks output"}

            if not buckets:
                if not _repo_has_ci_configured():
                    return {"state": "none", "error": ""}
                time.sleep(10)
                continue

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
            time.sleep(10)  # still pending — keep polling
    # Timeout reached – no terminal state.
    return {"state": "pending", "error": "CI did not complete within timeout"}


def _ci_rerun(sha: str) -> bool:
    """Rerun the failed/cancelled jobs of the workflow run for ``sha``.

    The function never raises; on any failure it returns ``False`` so callers can
    fall back to the ordinary retry logic.
    """
    try:
        r = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={sha}",
                "--jq",
                ".workflow_runs[0].id",
            ],
            check=False, capture_output=True,
            text=True,
        )
    except OSError:
        return False
    if r.returncode != 0:
        return False
    run_id = r.stdout.strip()
    if not run_id:
        return False
    try:
        rerun = subprocess.run(
            ["gh", "run", "rerun", run_id, "--failed"],
            check=False, capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return rerun.returncode == 0


def _acceptance_tampered(story: dict[str, Any], worktree: str) -> list[str]:
    """Return the sorted list of acceptance fixture paths whose worktree
    content no longer matches the sha256 digest recorded at dispatch (see
    pipeline.oracle_gate.acceptance_digests). A path missing from the
    worktree counts as tampered — deleting the oracle must not pass the
    gate. Returns [] when the story carries no recorded digests (a story
    dispatched before this existed, or one without acceptance fixtures).

    In a non-TDD-split story the implementer is instructed "do not modify any
    existing test; add new tests" and legitimately APPENDS tests to the oracle
    fixture. A pure append — the original source survives as a byte-exact
    prefix of the worktree file, with new tests added after — is NOT tampering:
    the original grader's assertions are untouched, so its authority over the
    original behavior is preserved; only new (non-authoritative) tests were
    appended (hit live on edit-guard-enforcement s4, PR #222: a 105a106,241
    append was refused). TDD-split stories keep the oracle strictly read-only,
    so any divergence there is tampering.
    """
    digests = story.get("acceptance_digests") or {}
    if not digests:
        return []
    tdd_split = bool(story.get("tdd_split", False))
    sources = {
        entry["path"]: (entry.get("source") or "")
        for entry in (story.get("acceptance") or [])
    }
    tampered = []
    for path, expected in digests.items():
        target = Path(worktree) / path
        if not target.is_file():
            tampered.append(path)
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual == expected:
            continue
        if not tdd_split:
            original = sources.get(path, "")
            if original and target.read_bytes().startswith(
                original.encode("utf-8")
            ):
                continue
        tampered.append(path)
    return sorted(tampered)


def _reverify_acceptance(
    story: dict[str, Any], worktree: str, story_key: str = ""
) -> dict[str, str]:
    """Re‑run a story's acceptance oracle against its rebased worktree.

    The function first attempts to run only the acceptance paths when the
    story declares an ``acceptance`` block *and* the runner can be safely
    scoped (pytest path arguments, cargo ``--test``, npm/yarn node ``--test`` –
    see :func:`_scope_test_cmd_to_acceptance`).  If no acceptance block is
    present, or scoping is not possible, the full test suite is executed.

    The MBW safety net ensures that a story without an explicit acceptance
    block still gets the rebased branch's full test suite re‑run before merge –
    this protects against accidental regressions in the mainline tests.  An
    operator may opt out of the full‑suite run by setting the environment
    variable ``PIPELINE_REVERIFY_FULL_SUITE=0``.

    ``story_key`` (when given) lets a no-acceptance story additionally pull
    in its own new/modified tests/test_*.py files that the full-suite
    command's --ignore=tests would otherwise hide from this last gate before
    merge — the same Mode 42 done-bar blindspot check_story_status's test
    gate closes; see :func:`_added_pytest_test_paths`.
    """
    acceptance = story.get("acceptance") or []
    if not worktree or not Path(worktree).is_dir():
        return {"state": "none", "error": ""}

    # The read-only oracle is described as read-only in prompt text only;
    # this is the mechanism behind it. A worktree whose acceptance fixture no
    # longer matches its dispatch-time digest must be refused WITHOUT running
    # it — a rewritten grader is not a trustworthy gate (observed live
    # 2026-07-30, LAUNCHD-PLIST-PORTABILITY: two hand commits rewrote the
    # oracle and the merge gate re-verified against the rewrite).
    tampered = _acceptance_tampered(story, worktree)
    if tampered:
        return {
            "state": "fail",
            "error": "acceptance fixture modified since dispatch: " + ", ".join(tampered),
        }

    test_dir, test_cmd = detect_test_command(Path(worktree))
    scoped = None
    if acceptance:
        acceptance_paths = [
            str(Path(worktree) / p) for p in _acceptance_rel_paths(story)
        ]
        scoped = _scope_test_cmd_to_acceptance(test_cmd, acceptance_paths, test_dir)
    if scoped is not None:
        test_cmd = scoped
    elif not acceptance:
        # No acceptance block: run the full suite unless the operator opted out.
        if os.environ.get("PIPELINE_REVERIFY_FULL_SUITE", "1") == "0":
            return {"state": "none", "error": ""}
        if story_key and _is_pytest_cmd(test_cmd):
            from .server import _default_branch
            own_test_paths = _added_pytest_test_paths(
                Path(worktree), story_key, _default_branch())
            if own_test_paths:
                test_cmd = [*test_cmd,
                            *(str(Path(worktree) / p) for p in own_test_paths)]

    test_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }
    if _is_heavy(test_cmd):
        with _heavy_lock():
            r = subprocess.run(
                test_cmd, check=False, cwd=test_dir, capture_output=True, text=True, env=test_env
            )
    else:
        r = subprocess.run(
            test_cmd, check=False, cwd=test_dir, capture_output=True, text=True, env=test_env
        )

    if r.returncode == 0:
        return {"state": "pass", "error": ""}
    return {"state": "fail", "error": (r.stdout + r.stderr).strip()[-500:]}


def _reverify_build(worktree: str) -> dict[str, str]:
    """Run the rebased worktree's build command before merge.

    The function returns ``{"state": "pass"}`` on success, ``{"state": "fail"}``
    when the build fails, and ``{"state": "none"}`` when the gate is disabled,
    there is no worktree to build against, or no build command is detectable.
    """
    if not PIPELINE_MERGE_BUILD_GATE:
        return {"state": "none", "error": "build gate disabled"}
    if not worktree or not Path(worktree).is_dir():
        return {"state": "none", "error": ""}

    detected = detect_build_command(Path(worktree))
    if detected is None:
        return {"state": "none", "error": ""}
    build_dir, build_cmd = detected

    build_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }
    if _is_heavy(build_cmd):
        with _heavy_lock():
            r = subprocess.run(
                build_cmd, check=False, cwd=build_dir, capture_output=True, text=True, env=build_env
            )
    else:
        r = subprocess.run(
            build_cmd, check=False, cwd=build_dir, capture_output=True, text=True, env=build_env
        )

    if r.returncode == 0:
        return {"state": "pass", "error": ""}
    return {"state": "fail", "error": (r.stdout + r.stderr).strip()[-500:]}


__all__ = [
    "PIPELINE_MERGE_BUILD_GATE",
    "PIPELINE_MERGE_CI_GATE",
    "PIPELINE_MERGE_CI_TIMEOUT",
    "_acceptance_tampered",
    "_ci_rerun",
    "_ci_status",
    "_repo_has_ci_configured",
    "_reverify_acceptance",
    "_reverify_build",
]
