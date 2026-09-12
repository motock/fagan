"""CI status polling and pre-merge re-verification for the merge gate.

The three env-var gates (``PIPELINE_MERGE_CI_GATE``, ``PIPELINE_MERGE_CI_TIMEOUT``,
``PIPELINE_MERGE_BUILD_GATE``) live here rather than in
:mod:`pipeline_config` because they are CI‑specific; tests patch the module
directly.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import role_registry

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
from .config import DEFAULT_MODEL, MERGE_MAX_ATTEMPTS
from .config_provenance import (
    _claude_json_path,
    _scheduler_plist_path,
    effective_env_config,
    effective_role_config,
    ignored_env_vars_present,
)
from .persistence import _plan_role_config
from .persona import _persona_default_model

logger = logging.getLogger(__name__)

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


def _fetch_ci_failure_excerpt(branch: str) -> str | None:
    """Capture a bounded failing-test excerpt from the failed workflow run.

    Resolves the most recent workflow run id for ``branch`` via ``gh run list``,
    then pulls the failing job's log with ``gh run view --log-failed`` (falling
    back to ``--log`` if that errors or returns empty). Returns a bounded tail
    (~800 chars) of the log, or ``None`` on any failure so callers can fall back
    to the classification-only ``error``. Never raises.
    """
    try:
        run_list = subprocess.run(
            [
                "gh", "run", "list", "--branch", branch, "--limit", "1",
                "--json", "databaseId", "--jq", ".[0].databaseId",
            ],
            check=False, capture_output=True, text=True, timeout=20,
        )
        if run_list.returncode != 0:
            return None
        try:
            data = json.loads(run_list.stdout or "[]")
        except ValueError:
            return None
        if isinstance(data, list):
            if not data:
                return None
            run_id = data[0].get("databaseId")
        else:
            run_id = data
        if not run_id:
            return None
        run_id = str(run_id)
        for flag in ("--log-failed", "--log"):
            view = subprocess.run(
                ["gh", "run", "view", run_id, flag],
                check=False, capture_output=True, text=True, timeout=20,
            )
            if view.returncode == 0 and view.stdout.strip():
                return view.stdout[-800:]
        return None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


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
        logger.warning(
            "CI merge gate is DISABLED (PIPELINE_MERGE_CI_GATE); merge is "
            "proceeding without checking CI"
        )
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
                error = "; ".join(
                    f"{r.get('name')}: {r.get('conclusion')}"
                    for r in runs
                    if r.get("conclusion")
                    in {"failure", "timed_out", "action_required"}
                )[:300]
                excerpt = _fetch_ci_failure_excerpt(branch)
                if excerpt:
                    error = f"{error}\n\n{excerpt}"
                return {"state": "fail", "error": error}
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
                error = "; ".join(
                    f"{e.get('name')}: {e.get('bucket')}"
                    for e in entries
                    if e.get("bucket") in {"fail", "error", "action_required"}
                )[:300]
                excerpt = _fetch_ci_failure_excerpt(branch)
                if excerpt:
                    error = f"{error}\n\n{excerpt}"
                return {"state": "fail", "error": error}
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


def _ci_status_once(branch: str, *, sha: str) -> dict[str, str]:
    """Return a single-poll CI status for ``branch``/``sha``.

    Mirrors :func:`_ci_status`'s classification logic exactly, but performs
    exactly one query and returns immediately instead of sleeping and
    looping until a terminal state or timeout is reached. Every point where
    :func:`_ci_status` would ``time.sleep(10); continue`` instead returns
    ``{"state": "pending"}`` here.
    """
    if not PIPELINE_MERGE_CI_GATE:
        logger.warning(
            "CI merge gate is DISABLED (PIPELINE_MERGE_CI_GATE); merge is "
            "proceeding without checking CI"
        )
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
            return {"state": "pending", "error": ""}

        conclusions = {c.get("conclusion") for c in runs}
        if conclusions & {"failure", "timed_out", "action_required"}:
            error = "; ".join(
                f"{r.get('name')}: {r.get('conclusion')}"
                for r in runs
                if r.get("conclusion")
                in {"failure", "timed_out", "action_required"}
            )[:300]
            excerpt = _fetch_ci_failure_excerpt(branch)
            if excerpt:
                error = f"{error}\n\n{excerpt}"
            return {"state": "fail", "error": error}
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
            return {"state": "pending", "error": ""}
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
            error = "; ".join(
                f"{e.get('name')}: {e.get('bucket')}"
                for e in entries
                if e.get("bucket") in {"fail", "error", "action_required"}
            )[:300]
            excerpt = _fetch_ci_failure_excerpt(branch)
            if excerpt:
                error = f"{error}\n\n{excerpt}"
            return {"state": "fail", "error": error}
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


def _ci_pending_expired(since_iso: str) -> bool:
    """True once a story has waited on pending CI longer than the total
    patience the blocking gate used to provide (MERGE_MAX_ATTEMPTS
    attempts x PIPELINE_MERGE_CI_TIMEOUT each)."""
    try:
        since = datetime.fromisoformat(since_iso)
    except (TypeError, ValueError):
        return False
    now = datetime.now(timezone.utc)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    elapsed = (now - since).total_seconds()
    return elapsed >= MERGE_MAX_ATTEMPTS * PIPELINE_MERGE_CI_TIMEOUT


def _parse_pytest_excerpt(gate_error: str) -> str | None:
    """Best-effort extract of a pytest failure excerpt from ``gate_error``.

    Looks for a literal ``<file>.py:<line>: <Exception>: <assertion>`` pattern
    and, only when the whole pattern is found, returns a short
    ``On {file}:{line}, {assertion} fails`` string built from the exact file,
    line, and assertion substrings captured from ``gate_error``. Returns
    ``None`` when nothing recognizable parses -- it never fabricates a
    file/line/assertion that is not literally present in ``gate_error``.
    """
    if not gate_error:
        return None
    match = re.search(
        r"(?P<file>[\w./-]+\.py):(?P<line>\d+):\s+"
        r"(?P<exc>[A-Za-z_]+Error):\s*(?P<assertion>.+)",
        gate_error,
    )
    if not match:
        return None
    return (
        f"On {match.group('file')}:{match.group('line')}, "
        f"{match.group('assertion')} fails"
    )


def _ci_rework_feedback(gate_error: str, attempts: int) -> str:
    """Generate review feedback for merge-gate CI failures.

    ``attempts`` is the current rework round number (1 for the first rework).
    Round 1 is byte-identical to the pre-round-escalation wording. From round 2
    onward a ``PREVIOUS REWORK ATTEMPT {attempts-1} DID NOT FIX THIS.`` prefix
    is prepended, and when a pytest excerpt is parseable from ``gate_error`` it
    is appended (in addition to the verbatim ``Gate error:`` line) along with
    the full-suite done-bar instruction.
    """
    lint_keywords = ("lint", "ruff", "eslint", "clippy", "golangci")
    lower = gate_error.lower()
    if any(k in lower for k in lint_keywords):
        base = (
            f"The merge-gate CI check failed on your submitted branch "
            f"Gate error: {gate_error}\n\n"
            "This is a LINT failure, not a test failure - the test suite may already pass, so re-running tests alone proves nothing. Run the project's lint command (e.g. `ruff check .` for Python) from the repo root, fix every finding, and commit.\n\n"
            "A NEW COMMIT on your branch is REQUIRED - CI runs on your pushed commits, and exiting without committing a change cannot alter the CI result."
        )
    else:
        base = (
            f"The merge-gate CI check failed on your submitted branch "
            f"Gate error: {gate_error}\n\n"
            "The bug could be in the implementation OR in a test file you wrote; re-examine both against the spec and make a targeted fix.\n\n"
            "A NEW COMMIT on your branch is REQUIRED - CI runs on your pushed commits, and exiting without committing a change cannot alter the CI result."
        )
    if attempts < 2:
        return base
    msg = f"PREVIOUS REWORK ATTEMPT {attempts - 1} DID NOT FIX THIS. " + base
    excerpt = _parse_pytest_excerpt(gate_error)
    if excerpt is not None:
        msg = (
            f"{msg}\n\n{excerpt}\n"
            "Do not call done until the full suite passes."
        )
    return msg


def _get_effective_config_impl(
    plan_name: str | None = None,
) -> dict[str, Any]:
    plan_role_config = _plan_role_config(plan_name) if plan_name else None
    model_fallbacks = {
        "overlord": lambda: _persona_default_model("overlord") or "opus",
        "planner": lambda: DEFAULT_MODEL,
        "dispatch": lambda: DEFAULT_MODEL,
        "review": lambda: _persona_default_model("code-reviewer") or DEFAULT_MODEL,
        "decompose": lambda: _persona_default_model("product-analyst") or "opus",
        "security": lambda: _persona_default_model("security-engineer") or DEFAULT_MODEL,
    }
    try:
        registry = role_registry.load_registry()
    except role_registry.RoleRegistryError:
        registry = {}

    roles = effective_role_config(
        plan_role_config=plan_role_config,
        registry=registry,
        model_fallbacks=model_fallbacks,
    )
    env = effective_env_config()
    ignored_env_vars = ignored_env_vars_present()

    plist_path = _scheduler_plist_path()
    mcp_env_path = _claude_json_path()
    registry_path = role_registry._registry_path()

    sources = {
        "launchd_plist": {"path": str(plist_path), "exists": plist_path.exists()},
        "mcp_server_env": {"path": str(mcp_env_path), "exists": mcp_env_path.exists()},
        "model_registry": {"path": str(registry_path), "exists": registry_path.exists()},
    }

    return {
        "ok": True,
        "roles": roles,
        "env": env,
        "ignored_env_vars": ignored_env_vars,
        "sources": sources,
    }


# ---------- Story-done + story-field allowlists (folded from server.py) ----------
# ``_mark_story_done_impl`` and the story-field allowlists were moved here from
# ``pipeline/server.py`` (behavior-preserving refactor). The function body reads
# server-sourced module globals (``_store``, ``_validate_key``,
# ``get_ticket_provider``, ``LogicalState``, ``PIPELINE_SELF_REPO_ROOT``,
# ``_record_retro_pending``) as free variables. The test suite monkeypatches
# those names on ``pipeline.server``, so the function is rebound below to
# ``pipeline.server``'s namespace at call time (LOAD_GLOBAL does not consult a
# module-level __getattr__, so a plain re-export would not).


def _record_retro_pending(plan_name: str, story_count: int) -> None:
    RETRO_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)  # noqa: F821
    existing_lines = (
        RETRO_PENDING_PATH.read_text().splitlines()  # noqa: F821
        if RETRO_PENDING_PATH.exists()  # noqa: F821
        else []
    )
    marker = f"- {plan_name} "
    if any(line.startswith(marker) for line in existing_lines):
        return
    date = datetime.now(timezone.utc).date().isoformat()
    with RETRO_PENDING_PATH.open("a") as f:  # noqa: F821
        f.write(f"- {plan_name} \u2014 completed {date}, {story_count} stories\n")


def _mark_story_done_impl(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Transition the ticket to Done and update the local manifest.
    Use after you've reviewed and merged the agent's PR.
    """
    _validate_key(plan_name)  # noqa: F821
    _validate_key(story_key)  # noqa: F821
    get_ticket_provider().set_state(  # noqa: F821
        story_key, LogicalState.DONE, plan_name  # noqa: F821
    )

    manifest = _store.get_manifest(plan_name)  # noqa: F821
    manifest["stories"][story_key]["status"] = "done"
    manifest["stories"][story_key].pop("parked_reason", None)
    _store.save_manifest(plan_name, manifest)  # noqa: F821

    from .plan_completion import notify_if_plan_completed

    try:
        notify_if_plan_completed(plan_name, manifest)
    except Exception:
        logging.getLogger(__name__).exception(
            "notify_if_plan_completed failed for %s", plan_name
        )

    # Check if all stories are now done
    all_done = all(s.get("status") == "done" for s in manifest["stories"].values())
    if all_done:
        if manifest.get("repo_root") == str(PIPELINE_SELF_REPO_ROOT):  # noqa: F821
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
# bare-name reads inside the bodies (e.g. ``_store``, ``_validate_key``,
# ``get_ticket_provider``, ``_record_retro_pending``) resolve against
# pipeline.server at call time. This preserves the original behavior where the
# functions lived in pipeline.server and saw monkeypatched module globals.
from . import server as _server

_mark_story_done_impl = types.FunctionType(
    _mark_story_done_impl.__code__,
    _server.__dict__,
    _mark_story_done_impl.__name__,
    _mark_story_done_impl.__defaults__,
    _mark_story_done_impl.__closure__,
)
_record_retro_pending = types.FunctionType(
    _record_retro_pending.__code__,
    _server.__dict__,
    _record_retro_pending.__name__,
    _record_retro_pending.__defaults__,
    _record_retro_pending.__closure__,
)


__all__ = [
    "PIPELINE_MERGE_BUILD_GATE",
    "PIPELINE_MERGE_CI_GATE",
    "PIPELINE_MERGE_CI_TIMEOUT",
    "_PATCHABLE_STORY_FIELDS",
    "_VALID_STORY_STATUSES",
    "_acceptance_tampered",
    "_ci_pending_expired",
    "_ci_rerun",
    "_ci_rework_feedback",
    "_ci_status",
    "_ci_status_once",
    "_fetch_ci_failure_excerpt",
    "_get_effective_config_impl",
    "_mark_story_done_impl",
    "_parse_pytest_excerpt",
    "_record_retro_pending",
    "_repo_has_ci_configured",
    "_reverify_acceptance",
    "_reverify_build",
]
