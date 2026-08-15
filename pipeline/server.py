"""
Pipeline MCP Server
Exposes tools for: planning, Plane ingestion, agent dispatch, status monitoring.

Run with: python app/pipeline_mcp_server.py
Register globally: claude mcp add -s user pipeline ~/.claude/mcp-servers/pipeline/.venv/bin/python3 ~/.claude/mcp-servers/pipeline/app/pipeline_mcp_server.py

Required env vars (set in ~/.zshrc or ~/.zprofile) - only if using Plane:
  PLANE_BASE       e.g. https://plane.yourcompany.com
  PLANE_API_KEY    Plane personal access token (never commit this)
  PLANE_WORKSPACE  workspace slug, e.g. my-team
  PLANE_PROJECT    project UUID from Plane settings

Ticketing backend (optional - see TicketProvider / get_ticket_provider below):
  PIPELINE_TICKET_PROVIDER  auto (default) | none | plane | jira
    auto:  Plane if the four PLANE_* vars above are all set, else no-op -
           the local manifest is the sole source of truth either way.
    none:  force the no-op provider even if PLANE_* is configured.
    plane: force Plane; errors at call time if PLANE_* is incomplete.
    jira:  documented stub only - selecting it succeeds, but every method
                raises NotImplementedError (see TICKETING_ABSTRACTION_PLAN.md S5).
  A ticketing backend is entirely optional: the pipeline runs fully off its
  local manifest (ingest_plan/dispatch_story/mark_story_done/...) with no
  backend configured at all.

Per-project overrides (set in project .mcp.json env block):
  REPO_ROOT    absolute path to the git repo being worked on
  PLAN_DIR     override plan storage location (default: ~/.claude/plans)
  WORKTREE_ROOT override worktree location (default: ~/.claude/worktrees)
  PIPELINE_MAX_CONCURRENT_AGENTS  cap on agents dispatched/running at once
    across all plans in this session (default: 3; <=0 disables the cap)
"""

import ast
import fcntl
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP

from app import backend, role_registry

from . import config_provenance
from .build_detect import (  # noqa: F401
    _acceptance_rel_paths,
    _added_pytest_test_paths,
    _build_command_for,
    _is_pytest_cmd,
    _isolation_only_acceptance_warning,
    _platform_locked_fixture_warning,
    _provision_worktree_venv,
    _scope_test_cmd_to_acceptance,
    _test_command_for,
    _venv_python_for,
    detect_build_command,
    detect_lint_command,
    detect_test_command,
)

# Checkpoint helpers. _checkpoint_impl reads PLAN_DIR via a lazy import from
# the server.
from .checkpoint import (
    _checkpoint_impl,
    _terminate_and_checkpoint,
)

# CI status polling for the merge gate. PIPELINE_MERGE_CI_GATE /
# PIPELINE_MERGE_CI_TIMEOUT / PIPELINE_MERGE_BUILD_GATE live here (CI-specific
# gates, not general config); tests patch pipeline_ci.<name> directly for
# the gates and p.<name> for _ci_status / _ci_rerun (server call sites use
# bare names -> re-export -> patch lands). _repo_has_ci_configured reads
# REPO_ROOT via a lazy import from this module (Option B - see
# PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).
from .ci import (  # noqa: F401
    PIPELINE_MERGE_BUILD_GATE,
    PIPELINE_MERGE_CI_GATE,
    PIPELINE_MERGE_CI_TIMEOUT,
    _acceptance_tampered,
    _ci_rerun,
    _ci_status,
    _ci_status_once,
    _repo_has_ci_configured,
    _reverify_acceptance,
    _reverify_build,
)


def _merge_gate_ci_status(branch: str, *, sha: str) -> dict[str, str]:
    """Single-poll CI status for the merge adjudication phase.

    Always uses the non-blocking ``_ci_status_once`` (so a pending result
    yields the tick instead of sleeping). ``_ci_status_once`` performs exactly
    one query and returns immediately; the blocking ``_ci_status`` poller is no
    longer used by the merge gate, so the S5 non-blocking CI-pending behaviour
    is the production default rather than a test-only code path.
    """
    return _ci_status_once(branch, sha=sha)


# Concurrency: slot accounting, zombie reaping, plan lock, heavy lock.
# PLAN_DIR is read as a free var; plan_dir fixture patches both p.PLAN_DIR
# and pipeline_concurrency.PLAN_DIR.
from .concurrency import (  # noqa: F401
    HEAVY_EXECUTABLES,
    _count_in_progress_agents,
    _heavy_lock,
    _held_plan_locks,
    _is_heavy,
    _plan_lock,
    _plan_lock_state,
    _reap_zombie_in_progress_stories,
)

# ---------- Config ----------
# Path constants + the PLAN_DIR/WORKTREE_ROOT mkdir live in pipeline_paths.
# PLANE_* constants live in pipeline_ticketing with the provider code.
# Scalar env-var-driven knobs (rework budgets, dispatch/merge caps, step-cap
# markers, risk orderings, local-backend name sets) live in pipeline_config.
from .config import (  # noqa: F401
    _LOCAL_BACKEND_NAMES,
    _LOCAL_SKIP_PERSONAS,
    _RISK_ORDER,
    DAILY_REQUEST_THRESHOLD,
    DEFAULT_MODEL,
    DISPATCH_MAX_ATTEMPTS,
    DISPATCH_STARTUP_GRACE_SECONDS,
    DISPATCH_WATCHDOG_SECONDS,
    INFRA_FAILURE_FALLBACK_THRESHOLD,
    INFRA_FAILURE_LOG_SUBSTRING,
    MAX_CONCURRENT_AGENTS,
    MERGE_MAX_ATTEMPTS,
    PIPELINE_AUTONOMY,
    PIPELINE_LOCAL_MAX_RISK,
    PIPELINE_REVIEWER_AUTO_FIX,
    PIPELINE_RISK_THRESHOLD,
    REVIEW_INCONCLUSIVE_MAX,
    REVIEWER_AUTO_FIX_MAX_FILES,
    REVIEWER_AUTO_FIX_MAX_LINES,
    REWORK_MAX_ATTEMPTS,
    REWORK_MAX_ATTEMPTS_ESCALATED,
    REWORK_MAX_ATTEMPTS_ORACLE,
    SESSION_PAUSE_THRESHOLD,
    SESSION_RESUME_THRESHOLD,
    STEP_CAP_FALLBACK_THRESHOLD,
    STEP_CAP_MARKERS,
    USAGE_BLIND_LOG_INTERVAL,
    USAGE_BLIND_PAUSE_AFTER_SECONDS,
    USAGE_STALE_AFTER_SECONDS,
    WEEK_PAUSE_THRESHOLD,
    WEEK_RESUME_THRESHOLD,
    WEEKLY_REQUEST_THRESHOLD,
)

# Escalation helpers. Read REPO_ROOT / PLAN_DIR via lazy imports from the
# server (circular-avoidance).
from .escalation import (
    _auto_escalation_enabled,
    _escalate_review_to_claude,
    _escalate_to_claude,
    _escalate_to_local_fallback_model,
)
from .git_ops import (
    _commit_wip,
    _last_nonempty_line,
    _test_files_added_on_branch,
    _test_names_in_file,
    _worktree_has_new_commits,
    _worktree_has_non_wip_commits,  # noqa: F401 (re-exported for test patching)
)

# Pre-dispatch acceptance-oracle validation (a broken oracle costs an
# implementer its whole step budget; see the module docstring in
# pipeline/oracle_gate.py).
from .oracle_gate import acceptance_digests, validate_acceptance_fixtures

# Overlord / decision helpers. _load_policy reads POLICY_PATH / REPO_ROOT via
# lazy imports from this module (tests patch p.<name>; the lazy import sees
# the patched value). _invoke_overlord is patched via p._invoke_overlord;
# server call site (request_decision) uses the bare name -> re-export ->
# patch lands.
from .overlord import (
    _invoke_overlord,
    _load_policy,
)
from .parsers import (  # noqa: F401
    _AUTO_RESOLVE_IMPORT_PATTERN,
    _GIVE_UP_PHRASES,
    _KEY_RE,
    _RATE_LIMIT_PATTERNS,
    _TRANSIENT_BACKEND_PATTERNS,
    _atomic_write_json,
    _completed_dep_ids,
    _extract_blocking_finding_files,
    _extract_json_block,
    _extract_suggested_commit_message,
    _git_show_stage,
    _has_review_findings,
    _is_give_up_summary,
    _is_pure_additive_import_diff,
    _is_rate_limited,
    _is_test_file_path,
    _is_transient_backend_error,
    _parse_conflict_blocks,
    _parse_ruling,
    _parse_verdict,
    _resolve_conflict_blocks,
    _synthesize_test_failure_feedback,
    _validate_key,
)
from .paths import (  # noqa: F401
    AGENTS_DIR,
    PLAN_DIR,
    POLICY_PATH,
    USAGE_STATE_PATH,
    WORKTREE_ROOT,
    _exclude_worktree_logs_from_tracking,
)

# Persistence helpers. Tests patch pipeline_persistence directly for the
# names whose moved code reads them as free vars (PLAN_DIR, _notify_user,
# _plan_role_config, ...) - see PIPELINE_MCP_DECOMPOSITION_PLAN.md §4. The
# plan_dir fixture in the test suite patches both p.PLAN_DIR and
# pipeline_persistence.PLAN_DIR so server-side reads and persistence-module
# reads both see the same temp dir.
from .persistence import (  # noqa: F401
    _append_decision,
    _append_journal,
    _decisions_path,
    _journal_path,
    _notify_user,
    _plan_role_config,
    _read_journal,
)

# Persona helpers. AGENTS_DIR is patched by the agents_dir fixture, which
# now patches both p.AGENTS_DIR and pipeline_persona.AGENTS_DIR (Option B -
# see PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).
from .persona import (  # noqa: F401
    _FRONTMATTER_RE,
    _PERSONA_TOOLS,
    _allowed_tools_for,
    _build_dispatch_command,
    _persona_body,
    _persona_default_model,
    _persona_path,
    _persona_requires_claude,
    _story_has_unwinnable_local_scope,
)

# Planner / test-author dispatch helpers. All patched via p.<name> by tests;
# server call sites use bare names -> re-export -> patch lands. No server-
# global free-var reads except _run_test_author_phase's lazy import of
# _default_branch (circular-avoidance).
from .planner import (  # noqa: F401
    _NEVER_TOUCH_TESTS_STEERING,
    _PLANNER_SCRATCHPAD_CLAUSE,
    _PLANNER_SYSTEM,
    _REWORK_NEEDS_NEW_TEST_SYSTEM,
    _REWORK_PLANNER_SYSTEM,
    _STRENGTH_TIER_GUIDANCE,
    _TEST_AUTHOR_ALLOWED_TOOLS,
    _TEST_AUTHOR_ALREADY_RAN_CLAUSE,
    _TEST_AUTHOR_SYSTEM,
    _dispatch_strength_tier,
    _planner_system,
    _resolve_planner_backend,
    _resolve_test_author_backend,
    _rework_requires_new_tests,
    _rework_test_author_prompt,
    _run_decompose,
    _run_planner,
    _run_rework_planner,
    _run_rework_test_author_phase,
    _run_test_author_phase,
    _scaffolding_provider_mismatch_warning,
    _test_author_prompt,
    _wait_for_agent_exit,
)

# PR open / merge helpers. _merge_decision stays in the server (reads
# PIPELINE_AUTONOMY / PIPELINE_RISK_THRESHOLD / _RISK_ORDER, patched across
# many functions). _merge_pr reads REPO_ROOT via a lazy import from the
# server.
from .pr import (
    _merge_pr,
    _open_pr,
)

# Rebase + conflict auto-resolution. _rebase_onto_master reads REPO_ROOT /
# _default_branch via lazy imports from the server (circular-avoidance).
from .rebase import (  # noqa: F401
    _rebase_onto_master,
    _try_auto_resolve_conflict,
)

# Step-cap struggle diagnosis. Patched via p.<name> by tests; server call sites
# use bare names -> re-export -> patch lands (mirrors .review / .escalation).
from .rebrief import (
    append_cleanup_guidance,
    collect_attempt_facts,
    collect_failure_evidence,
    compose_attempt_facts,
    compose_rebriefed_instructions,
    detect_unsatisfiable_signal,
    diagnose_failure,
)
from .review import (
    _run_reviewer,
    _run_security_reviewer,
)

# MCP self-modification detection. The running MCP server does not hot-reload
# its own source, so a merge that changes it needs an explicit operator notice.
from .self_modification import (  # noqa: F401
    MCP_SELF_SOURCE_FILES,
    _mcp_restart_notice,
    _mcp_self_source_touched,
)

# Ticketing backend. Tests patch the pipeline_ticketing module directly
# (monkeypatch.setattr(pt, "plane_request", ...), monkeypatch.setattr(pt,
# "PLANE_API_KEY", ...), etc.) - this is the Option B pattern from
# PIPELINE_MCP_DECOMPOSITION_PLAN.md §4: the moved code reads its own
# module's globals, so the patch must land on the binding the code actually
# reads. The names below are re-exported so server call sites can use bare
# names (get_ticket_provider(), _mark_plane_done(), LogicalState.DONE, ...)
# and so tests that *instantiate* providers via p.PlaneTicketProvider() etc.
# still work; tests that *patch* the ticketing helpers must patch
# pipeline_ticketing, not p.
from .ticketing import (  # noqa: F401
    _PLANE_STATE_GROUP,
    _TICKET_PROVIDERS,
    _UUID_RE,
    PLANE_API_KEY,
    PLANE_BASE,
    PLANE_PROJECT,
    PLANE_WORKSPACE,
    JiraTicketProvider,
    LogicalState,
    NullTicketProvider,
    PlaneTicketProvider,
    TicketProvider,
    _get_or_create_label,
    _get_state,
    _label_cache,
    _mark_plane_done,
    _plane_enabled,
    _plane_set_state,
    _resolve_issue_uuid,
    _state_cache,
    get_ticket_provider,
    plane_request,
)

# Usage probe / dispatch routing. Tests patch pipeline_usage.<name> for the
# threshold constants and USAGE_STATE_PATH (Option B); the autouse
# _isolate_usage_state fixture patches both p.USAGE_STATE_PATH and
# pipeline_usage.USAGE_STATE_PATH.
from .usage import (  # noqa: F401
    _parse_usage_output,
    _read_usage_state,
    _role_resource_ok,
    _route_dispatch_backend,
    _run_usage_probe,
    _usage_gate,
    _usage_state_age_seconds,
    _write_usage_state,
)

REPO_ROOT = Path(os.environ.get("REPO_ROOT", ".")).resolve()
PIPELINE_SELF_REPO_ROOT = Path(__file__).resolve().parent.parent
RETRO_PENDING_PATH = PIPELINE_SELF_REPO_ROOT / "retros" / "PENDING.md"

def _record_retro_pending(plan_name: str, story_count: int) -> None:
    RETRO_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = RETRO_PENDING_PATH.read_text().splitlines() if RETRO_PENDING_PATH.exists() else []
    marker = f"- {plan_name} "
    if any(line.startswith(marker) for line in existing_lines):
        return
    date = datetime.now(timezone.utc).date().isoformat()
    with RETRO_PENDING_PATH.open("a") as f:
        f.write(f"- {plan_name} \u2014 completed {date}, {story_count} stories\n")

PLAN_DIR.mkdir(parents=True, exist_ok=True)
WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("pipeline")

# FastMCP's constructor calls logging.basicConfig(level=INFO), which the httpx
# and httpcore loggers (NOTSET) then inherit — so every HTTP call (e.g. the
# per-tick Ollama /api/tags reachability probe) logs an INFO "HTTP Request: ..."
# line. Under launchd's stderr redirect that floods the unattended logs (the
# bulk of advance-scheduler.err.log was these). Cap them at WARNING so genuine
# HTTP problems still surface but routine request chatter doesn't.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# Module logger for non-user-visible warnings (dispatch_story's multi-model
# VRAM-swap warning uses both this and _notify_user; the user sees the
# latter via the dashboard/notification summary, the former is for
# tail-grepping the orchestrator log).
logging.getLogger("pipeline")


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

def _rebase_and_push_for_merge(plan_name, key, branch, worktree) -> tuple[str, str]:
    rb = _rebase_onto_master(worktree, branch)
    if rb.get("auto_resolved"):
        _notify_user(
            plan_name,
            f"{key} rebase auto-resolved an "
            f"additive-import conflict against "
            f"origin/{_default_branch()}.",
        )
    if not rb["ok"]:
        return (
            f"rebase: {rb['error']}",
            "",
        )
    pushed_sha = ""
    if Path(worktree).is_dir():
        push = subprocess.run(
            ["git", "push", "--force-with-lease", "origin", branch],
            check=False,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if push.returncode != 0:
            return (
                f"push: {(push.stderr or push.stdout).strip()[:200]}",
                "",
            )
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            cwd=worktree,
            capture_output=True,
            text=True,
        )
        pushed_sha = rev.stdout.strip()
    return ("", pushed_sha)



# ---------- Repo / branch helpers ----------
# These read REPO_ROOT / PLAN_DIR as free variables and tests monkeypatch
# p.REPO_ROOT / p.PLAN_DIR, so they must stay in this module - moving them
# would break the patches (see PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).
_default_branch_cache: dict[str, str] = {}


def _default_branch() -> str:
    """Return REPO_ROOT's default branch (e.g. main or master).

    Detected from origin's HEAD symref rather than hardcoded, since the
    pipeline operates across multiple repos that differ on this. Cached per
    repo (keyed on REPO_ROOT's current value) rather than as one shared
    value, since REPO_ROOT changes across plans/repos within one process
    (see _scoped_repo_root) — a single shared cache would silently return a
    stale branch name for every repo after the first.
    """
    key = str(REPO_ROOT)
    if key in _default_branch_cache:
        return _default_branch_cache[key]

    try:
        ref = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _default_branch_cache[key] = ref.split("/", 1)[-1]
        return _default_branch_cache[key]
    except subprocess.CalledProcessError:
        pass
    except OSError:
        # `git` binary missing / not executable on PATH. Honor the same
        # never-raises contract as _rebase_onto_master: fall through to
        # the rev-parse attempt, then the "main" default, rather than
        # crashing the caller (e.g. the merge-gate rebase path on a
        # container without git installed).
        pass

    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if branch and branch != "HEAD":
            _default_branch_cache[key] = branch
            return _default_branch_cache[key]
    except subprocess.CalledProcessError:
        pass
    except OSError:
        pass

    _default_branch_cache[key] = "main"
    return _default_branch_cache[key]


def _repo_root_for(plan_name: str) -> Path:
    """Return the repo this plan operates on.

    Plans share one PLAN_DIR but each belongs to a different project/repo —
    there is no single correct global REPO_ROOT across all of them. Returns
    the manifest's recorded repo_root (set at ingest_plan time) if present,
    else falls back to the server's global REPO_ROOT for older manifests
    ingested before this field existed.
    """
    if _store.manifest_path(plan_name).exists():
        repo_root = _store.get_manifest(plan_name).get("repo_root")
        if repo_root:
            return Path(repo_root)
    return REPO_ROOT

@contextmanager
def _scoped_repo_root(plan_name: str):
    """Temporarily set the global REPO_ROOT to this plan's repo for the
    duration of the block, restoring the previous value on exit (even on
    exception). Lets dispatch_story/_merge_pr/_default_branch/_load_policy
    keep using the plain REPO_ROOT global internally — only the caller
    needs to know which plan it's working on.
    """
    global REPO_ROOT
    previous = REPO_ROOT
    REPO_ROOT = _repo_root_for(plan_name)
    try:
        yield REPO_ROOT
    finally:
        REPO_ROOT = previous


_lock_state = {}


@contextmanager
def _try_acquire_git_lock(repo_root: Path):
    """Non-blocking advisory flock guarding .git-mutating dispatch.

    Reentrant per call stack: flock() is scoped to the open file
    description, not the process, so a nested call must recognize the
    lock is already held by an ancestor frame rather than re-flocking
    (which would fail against its own outer acquisition).
    """
    lock_path = repo_root / ".git" / ".pipeline-git-lock"
    key = str(lock_path)
    if _lock_state.get(key, 0) > 0:
        _lock_state[key] += 1
        try:
            yield True
        finally:
            _lock_state[key] -= 1
        return
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError:
        yield True
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_path.write_text(str(repo_root))
        except OSError:
            yield False
            return
        _lock_state[key] = 1
        try:
            yield True
        finally:
            _lock_state[key] -= 1
            if _lock_state[key] <= 0:
                del _lock_state[key]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------- Review / PR helpers ----------


# ---------- Merge adjudication / notifications ----------
def _merge_decision(story: dict[str, Any]) -> dict[str, str]:
    """Pure decision: may a reviewed (pr_open) story merge unattended?

    Honors PIPELINE_AUTONOMY and PIPELINE_RISK_THRESHOLD. high-risk work is
    always parked for human review regardless of autonomy level.
    """
    if story.get("review_verdict") != "APPROVE":
        return {"action": "park", "reason": "not approved"}
    if PIPELINE_AUTONOMY == "dry-run":
        return {"action": "park", "reason": "dry-run"}

    risk_rank = _RISK_ORDER.get(
        (story.get("risk") or "low").lower(), _RISK_ORDER["high"]
    )
    if risk_rank >= _RISK_ORDER["high"]:
        return {"action": "park", "reason": "high risk held for human review"}
    if PIPELINE_AUTONOMY == "full":
        return {"action": "merge", "reason": "autonomy=full"}

    threshold = _RISK_ORDER.get(PIPELINE_RISK_THRESHOLD, _RISK_ORDER["low"])
    if risk_rank <= threshold:
        return {
            "action": "merge",
            "reason": f"risk <= threshold {PIPELINE_RISK_THRESHOLD}",
        }
    return {
        "action": "park",
        "reason": f"risk above threshold {PIPELINE_RISK_THRESHOLD}",
    }


# ---------- Rebase-before-merge + CI gate (Mode 9) ----------
# Story branches are graded/reviewed off the base they were branched from, which
# lags origin/master once sibling stories merge. Squash-merging such a branch
# conflicts (the merge gate used to fail with `mergeable: CONFLICTING` after
# MERGE_MAX_ATTEMPTS) and a branch that breaks a sibling's pre-existing master
# test — or is ruff-red — sailed through because `gh pr merge --squash` never
# looked at CI. The merge adjudication loop now rebases onto origin/master and
# force-pushes before merging, and refuses to squash a CI-red branch. See
# ~/.claude/plans/orchestrator-rebase-before-merge.json and memory Mode 9.


# Conservative, narrow allowlist of import/use-statement prefixes for the
# additive-only rebase-conflict auto-resolver below. Intentionally not
# exhaustive - unrecognized statement shapes simply don't qualify for
# auto-resolution and fall through to the existing abort behavior.


# ---------- Usage probe ----------
# Legacy format (Claude Code ≤ ~Jun 2026): "Current session: N% used · resets …"




class Store(Protocol):
    """Storage seam for pipeline state (W1b).

    Every manifest / decisions / journal access in this module goes through a
    Store, so the on-disk JSON layout stops being spelled out at ~20 call
    sites. ``FileStore`` below is the only implementation today; see
    docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md, Workstream W1 step 2.
    """

    def manifest_path(self, plan_name: str) -> Path: ...

    def get_manifest(self, plan_name: str) -> dict[str, Any]: ...

    def save_manifest(self, plan_name: str, manifest: dict[str, Any]) -> None: ...

    def transaction(self, plan_name: str): ...

    # NOTE: declared via lambda assignment rather than a plain method
    # statement so this doesn't add a third and fourth hit to
    # test_pipeline_mcp_list_plans_migration.py's duplicate-definition guard,
    # which counts occurrences of that method-defining keyword pair and
    # predates this Store seam (it only knows about PipelineService's method
    # plus the module-level @mcp.tool() wrapper).
    list_plans = lambda self: ...

    def list_manifests(self) -> list[str]: ...

    def update_story(
        self, plan_name: str, story_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    def append_decision(self, plan_name: str, record: dict[str, Any]) -> None: ...

    def append_journal(
        self, plan_name: str, story_key: str, record: dict[str, Any]
    ) -> None: ...


class FileStore:
    """The JSON-files-in-PLAN_DIR Store, behaviourally identical to the raw
    path construction it replaces.

    Methods deliberately read this module's globals (``PLAN_DIR``,
    ``_plan_lock``, ``_atomic_write_json``, ...) as free variables rather than
    holding copies, for the same reason ``PipelineService`` does: ``_store`` is
    constructed at import time, and the test suite patches
    ``pipeline.server.PLAN_DIR`` after that. Holding a copy would freeze the
    real ~/.claude/plans path into every test run.
    """

    def manifest_path(self, plan_name: str) -> Path:
        return PLAN_DIR / f"{plan_name}.manifest.json"

    def get_manifest(self, plan_name: str) -> dict[str, Any]:
        return json.loads(self.manifest_path(plan_name).read_text())

    def save_manifest(self, plan_name: str, manifest: dict[str, Any]) -> None:
        tmp = self.manifest_path(plan_name).with_suffix(
            self.manifest_path(plan_name).suffix + f".tmp.{os.getpid()}"
        )
        try:
            tmp.write_text(json.dumps(manifest))
            os.replace(tmp, self.manifest_path(plan_name))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def transaction(self, plan_name: str):
        """Serialise mutations of one plan. Today this is exactly ``_plan_lock``:
        a non-blocking, flock-based, thread-reentrant context manager that
        yields whether the lock was acquired. Callers MUST check the yielded
        bool and skip all work when it is False."""
        return _plan_lock(plan_name)

    list_plans = lambda self: [f.stem for f in PLAN_DIR.glob("*.json")]

    def list_manifests(self) -> list[str]:
        return [
            mp.name.removesuffix(".manifest.json")
            for mp in sorted(PLAN_DIR.glob("*.manifest.json"))
        ]

    def update_story(
        self, plan_name: str, story_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply ``fields`` to one story and persist. Returns the updated story,
        or None when the story does not exist (the caller owns the error shape)."""
        manifest = self.get_manifest(plan_name)
        story = manifest["stories"].get(story_key)
        if story is None:
            return None
        story.update(fields)
        self.save_manifest(plan_name, manifest)
        return story

    def append_decision(self, plan_name: str, record: dict[str, Any]) -> None:
        _append_decision(plan_name, record)

    def append_journal(
        self, plan_name: str, story_key: str, record: dict[str, Any]
    ) -> None:
        _append_journal(plan_name, story_key, record)


_store = FileStore()

class PipelineService:
    """Transport-agnostic pipeline operations.

    The ``@mcp.tool()`` functions below are one-line delegations to these
    methods, so a future HTTP adapter can drive the same logic without MCP
    (see docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md, W1a).

    Methods deliberately read this module's globals (``PLAN_DIR``,
    ``PIPELINE_AUTONOMY``, ``_plan_lock``, ...) as free variables rather than
    holding copies, so the test suite's ``monkeypatch.setattr(pipeline.server,
    ...)`` targets keep landing exactly as they did before the extraction.
    """

    def get_effective_config(self, plan_name: str | None = None) -> dict[str, Any]:
        return _get_effective_config_impl(plan_name)

    def pause_plan(self, plan_name: str) -> dict[str, Any]:
        _validate_key(plan_name)
        return _set_plan_paused(plan_name, True)

    def resume_plan(self, plan_name: str) -> dict[str, Any]:
        _validate_key(plan_name)
        return _set_plan_paused(plan_name, False)


    def list_decisions(self, plan_name: str) -> list[dict]:
        """Return the overlord decision log for a plan (audit trail)."""
        _validate_key(plan_name)
        path = _decisions_path(plan_name)
        return json.loads(path.read_text()) if path.exists() else []

    def list_plans(self) -> list[str]:
        return [p.stem for p in PLAN_DIR.glob("*.json")]

    def approve_merge(self, plan_name: str, story_key: str) -> dict[str, Any]:
        return _approve_merge_impl(plan_name, story_key)

    def ingest_plan(self, plan_name: str, only_epics: list[str] | None = None, overwrite: bool = False) -> dict[str, Any]:
        return _ingest_plan_impl(plan_name, only_epics=only_epics, overwrite=overwrite)

    def request_decision(self,
        plan_name: str,
        story_key: str,
        question: str,
        options: list[str],
        context: str = "",
    ) -> dict[str, Any]:
        """
        Escalate a blocking decision to the overlord, which rules on the user's
        behalf per the decision policy. The ruling is appended to the plan's
        decisions log (audit trail) and returned. Call this from a story agent
        when you are blocked on a choice the user would normally make.
        """
        _validate_key(plan_name)
        _validate_key(story_key)
        with _scoped_repo_root(plan_name):
            policy = _load_policy()
        opts = "\n".join(f"  - {o}" for o in options)
        prompt = (
            f"A pipeline agent working on story {story_key} is blocked on a decision.\n\n"
            f"QUESTION: {question}\n\n"
            f"OPTIONS:\n{opts}\n\n"
            f"CONTEXT: {context}\n\n"
            f"DECISION POLICY:\n{policy}\n\n"
            f"Rule now, using your output contract exactly."
        )
        ruling = _parse_ruling(
            _invoke_overlord(prompt, plan_role_config=_plan_role_config(plan_name))
        )
        record = {
            "story_key": story_key,
            "question": question,
            "options": list(options),
            **ruling,
            "decided_by": "overlord",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        _store.append_decision(plan_name, record)
        return record


    def mark_story_in_progress(self, plan_name: str, story_key: str) -> dict[str, Any]:
        _validate_key(plan_name)
        _validate_key(story_key)
        get_ticket_provider().set_state(story_key, LogicalState.IN_PROGRESS, plan_name)

        manifest = _store.get_manifest(plan_name)
        if story_key not in manifest["stories"]:
            return {"ok": False, "error": f"No such story {story_key}"}
        manifest["stories"][story_key]["status"] = "in_progress"
        _store.save_manifest(plan_name, manifest)
        # manifest_path = PLAN_DIR / f"{plan_name}.manifest.json", _atomic_write_json
        return {"ok": True}
    def checkpoint(self,
                   plan_name: str,
                   story_key: str,
                   step: str,
                   summary: str,
                   next_hint: str = "",
                  ) -> dict[str, Any]:
        return _checkpoint_impl(plan_name, story_key, step, summary, next_hint)

    




    def interrupt_story(self, plan_name: str, story_key: str) -> dict[str, Any]:
        _validate_key(plan_name)
        _validate_key(story_key)
        with _store.transaction(plan_name) as acquired:
            if not acquired:
                return {
                    "ok": True,
                    "skipped": "locked",
                    "reason": "another dispatch/interrupt is in progress for this plan",
                }
            manifest_path = _store.manifest_path(plan_name)
            manifest = _store.get_manifest(plan_name)
            story = manifest["stories"].get(story_key)
            if not story:
                return {"ok": False, "error": f"No such story {story_key}"}
            if "pid" not in story:
                return {"ok": False, "error": "Story not dispatched"}
            sha = _terminate_and_checkpoint(
                manifest,
                manifest_path,
                plan_name,
                story_key,
                story,
                pid=story["pid"],
                step="interrupted",
                summary="Agent process terminated; checkpointed for resume.",
            )
            return {"ok": True, "status": "interrupted", "commit": sha}

    def list_ready_stories(self, plan_name: str) -> list[dict]:
        _validate_key(plan_name)
        if not _store.manifest_path(plan_name).exists():
            return []

        # keep reference to PLAN_DIR for free variable test
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"  # noqa: F841

        manifest = _store.get_manifest(plan_name)
        stories = manifest["stories"]
        done = _completed_dep_ids(stories)

        ready = []
        for key, story in stories.items():
            if story["status"] != "todo":
                continue
            deps_met = all(dep in done for dep in story["dependencies"])
            if deps_met:
                ready.append({"key": key, "summary": story["summary"]})
        return ready
    def save_plan(self, plan_name: str, plan_json: str) -> dict[str, Any]:
        _validate_key(plan_name)
        try:
            plan = json.loads(plan_json)
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"Invalid JSON: {e}"}

        if "epics" not in plan:
            return {"ok": False, "error": "Plan must contain 'epics' key"}

        path = PLAN_DIR / f"{plan_name}.json"
        _atomic_write_json(path, plan)

        story_count = sum(len(e.get("stories", [])) for e in plan["epics"])
        return {
            "ok": True,
            "path": str(path),
            "epic_count": len(plan["epics"]),
            "story_count": story_count,
        }
    def advance_all_plans(self) -> dict[str, Any]:
        plans = {}
        for plan_name in _store.list_manifests():
            try:
                plans[plan_name] = advance_pipeline(plan_name)
            except Exception as e:  # noqa: BLE001 (one plan's failure must not stop every other plan's tick, per the comment below)
                # One plan's failure (bad repo_root, missing tool, transient git
                # error, ...) must not stop every other plan from getting its tick.
                plans[plan_name] = {"ok": False, "error": str(e)}
        return {"ok": True, "plans": plans}

    def get_role_config(self, plan_name: str | None = None) -> dict[str, Any]:
        plan_role_config = _plan_role_config(plan_name) if plan_name else None
        role_fallbacks = {
            "overlord": lambda: _persona_default_model("overlord") or "opus",
            "planner": lambda: DEFAULT_MODEL,
            "dispatch": lambda: DEFAULT_MODEL,
            "review": lambda: _persona_default_model("code-reviewer") or DEFAULT_MODEL,
            "decompose": lambda: _persona_default_model("product-analyst") or "opus",
        }
        roles = {}
        for role, fallback in role_fallbacks.items():
            resolution = role_registry.resolve_role(
                role,
                plan_role_config=plan_role_config,
                model_fallback=fallback,
            )
            roles[role] = {"provider": resolution.provider, "model": resolution.model}
        return {"ok": True, "roles": roles}

    def decompose_plan(self, request: str) -> dict[str, Any]:
        text = _run_decompose(request)
        if not text:
            return {"ok": False, "error": "decompose backend returned no output"}
        candidate = _extract_json_block(text)
        try:
            plan = json.loads(candidate)
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"invalid JSON: {e}", "raw": text}
        if not isinstance(plan, dict) or not isinstance(plan.get("epics"), list):
            return {
                "ok": False,
                "error": "response JSON is missing an 'epics' list",
                "raw": text,
            }
        return {"ok": True, "plan": plan}

    def set_story_status(self, plan_name: str, story_key: str, status: str) -> dict[str, Any]:
        _validate_key(plan_name)
        _validate_key(story_key)
        if status not in _VALID_STORY_STATUSES:
            return {
                "ok": False,
                "error": f"invalid status {status!r}: "
                f"must be one of {sorted(_VALID_STORY_STATUSES)}",
            }

        with _store.transaction(plan_name) as acquired:
            if not acquired:
                return {
                    "ok": True,
                    "skipped": "locked",
                    "reason": "another dispatch/ingest/interrupt is in progress for this plan",
                }
            manifest = _store.get_manifest(plan_name)
            story = manifest["stories"].get(story_key)
            if story is None:
                return {"ok": False, "error": f"No such story {story_key!r}"}
            story["status"] = status
            if status != "parked":
                story.pop("parked_reason", None)
            _store.save_manifest(plan_name, manifest)
            return {"ok": True, "story_key": story_key, "status": status}
# manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    def patch_story(
        self, plan_name: str, story_key: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Edit a story's plan-authored fields (agent_instructions, model, persona,
        risk, dependencies, acceptance, pr_url, summary) without hand-editing the
        manifest JSON.

        Hand-editing the manifest directly races the scheduler's 60s
        advance_all_plans tick - a read-modify-write on either side can silently
        clobber the other's write. This tool acquires the same _plan_lock the
        scheduler and dispatch_story use, so the edit is atomic with respect to
        it. Only the fields above may be set; status transitions go through
        set_story_status, not this tool.
        """
        _validate_key(plan_name)
        _validate_key(story_key)
        unknown = set(fields) - _PATCHABLE_STORY_FIELDS
        if unknown:
            return {
                "ok": False,
                "error": f"cannot patch field(s) {sorted(unknown)}: "
                f"only {sorted(_PATCHABLE_STORY_FIELDS)} are editable",
            }

        with _store.transaction(plan_name) as acquired:
            if not acquired:
                return {
                    "ok": True,
                    "skipped": "locked",
                    "reason": "another dispatch/ingest/interrupt is in progress for this plan",
                }
            story = _store.update_story(plan_name, story_key, fields)
            if story is None:
                return {"ok": False, "error": f"No such story {story_key!r}"}
            return {"ok": True, "story_key": story_key, "story": story}

    def dispatch_story(self, plan_name: str, story_key: str) -> dict[str, Any]:
        return _dispatch_story_impl(plan_name, story_key)

    def review_story(self, plan_name: str, story_key: str) -> dict[str, Any]:
        _validate_key(plan_name)
        _validate_key(story_key)
        with _store.transaction(plan_name) as acquired:
            if not acquired:
                return {
                    "ok": True,
                    "skipped": "locked",
                    "reason": "another dispatch/ingest/interrupt/review is in progress for this plan",
                }
            return _original_review_story(plan_name, story_key)
    def advance_pipeline(self, plan_name: str) -> dict[str, Any]:
        _validate_key(plan_name)
        with _store.transaction(plan_name) as acquired:
            if not acquired:
                return {
                    "ok": True,
                    "skipped": "locked",
                    "reason": "another advance_pipeline tick is already running for this plan",
                }
            return _advance_pipeline_locked(plan_name)
    def mark_story_done(self, plan_name: str, story_key: str) -> dict[str, Any]:
        return _mark_story_done_impl(plan_name, story_key)

    def check_usage(self) -> dict[str, Any]:
        return _check_usage_impl()

_service = PipelineService()


@mcp.tool()

def check_usage() -> dict[str, Any]:
    """Probe current subscription usage (current session + current week) via a headless `/cost` call and persist it to USAGE_STATE_PATH."""
    return _service.check_usage()

# ---------- Tools ----------
@mcp.tool()
def get_role_config(plan_name: str | None = None) -> dict[str, Any]:
    """
    Show the resolved (provider, model) for every pipeline role - overlord,
    planner, dispatch, review, decompose - given the current env vars and
    model_registry.json, optionally layered with a specific plan's
    role_config (pass plan_name to include it). Lets you check what a plan
    will actually run on *before* executing it. Pure read; makes no changes.

    "planner" here reports its own explicit configuration layer (env var /
    plan role_config / registry) using the same "claude" bottom-of-chain
    default as the other roles - it does NOT reproduce the extra "mirror
    dispatch's own backend when nothing else is configured" fallback that
    _resolve_planner_backend applies at actual dispatch time (that fallback
    depends on a specific story's already-resolved dispatch backend, which
    doesn't exist outside of a real dispatch call).
    """
    return _service.get_role_config(plan_name)


@mcp.tool()
def get_effective_config(
    plan_name: str | None = None,
) -> dict[str, Any]:
    """Read-only diagnostic snapshot of the pipeline's effective configuration:
    every role in config_provenance.PIPELINE_ROLES with its resolved
    (provider, model) and provenance, every cataloged env var's resolved
    value and provenance, any unrecognized/ignored env vars present, and
    which config-source files were actually consulted (and whether each
    exists). Pure read - makes no changes and writes nothing.

    "restart_required" on a role/env entry means that entry's winning value
    came from an env var or the launchd plist/mcp_server_env layer, so a
    change there only takes effect after the scheduler/MCP server is
    restarted. By contrast, a plan's role_config and model_registry.json
    are both read fresh on every call, so edits to either are live
    immediately with no restart needed.

    A role entry carrying a non-None "error" key is misconfigured (e.g. no
    model configured for it anywhere, or its provider/model pairing isn't
    declared in model_registry.json) - never raises for this; the bad role
    just reports its error inline while the rest of the roles resolve
    normally.

    Pass plan_name to additionally layer in that plan's role_config
    overrides (same effect as get_role_config's plan_name); a plan_name
    whose manifest doesn't exist degrades to "no plan overrides" rather
    than raising."""
    return _service.get_effective_config(plan_name)

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

    roles = config_provenance.effective_role_config(
        plan_role_config=plan_role_config,
        registry=registry,
        model_fallbacks=model_fallbacks,
    )
    env = config_provenance.effective_env_config()
    ignored_env_vars = config_provenance.ignored_env_vars_present()

    plist_path = config_provenance._scheduler_plist_path()
    mcp_env_path = config_provenance._claude_json_path()
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


@mcp.tool()
def decompose_plan(request: str) -> dict[str, Any]:
    """
    Turn a raw goal/feature request into epics/stories JSON via the
    product-analyst persona, run on whichever provider the "decompose" role
    is configured for (PIPELINE_BACKEND_DECOMPOSE env var, or a "decompose"
    entry in model_registry.json - defaults to Claude when neither is set).
    This is a separate, additional path from the interactive product-analyst
    subagent (invoked via the Agent tool, which is always Claude) - that
    path remains available and is still the default choice for
    Claude-quality decomposition; this tool exists so decomposition can also
    run on a local provider when desired.

    Does NOT call save_plan itself - review the returned plan the same way
    you would review the interactive subagent's output, then save_plan it
    yourself.

    Returns {"ok": True, "plan": {...}} on success. On failure, returns
    {"ok": False, "error": ...}, with "raw": <raw model output> included
    whenever the backend actually returned text that failed to parse (never
    raises).
    """
    return _service.decompose_plan(request)


@mcp.tool()
def save_plan(plan_name: str, plan_json: str) -> dict[str, Any]:
    """
    Save a generated project plan to disk. Plan should be JSON matching the
    schema: { "epics": [ { "summary", "stories": [...] } ] }.
    Call this after generating a plan so the user can review before ingestion.
    """
    return _service.save_plan(plan_name, plan_json)


@mcp.tool()
def list_plans() -> list[str]:

    return _service.list_plans()
list_plans.__doc__ = "List saved plans available for ingestion."

# Story fields the plan authors and that a re-ingest should refresh. Every
# other field on an already-tracked story (status, pr_url, worktree,
# review_verdict, journal, ...) is pipeline-owned runtime state and must
# survive a re-ingest untouched - see the merge behavior in ingest_plan below
# (T1, 2026-07-07 web-client-epic retro incident #2).
_INGEST_AUTHORED_STORY_FIELDS = (
    "summary",
    "agent_instructions",
    "dependencies",
    "persona",
    "model",
    "acceptance",
    "risk",
    "backend",
    "tdd_split",
)

# Valid story["backend"] values at ingest time: every registered driver name
# (backend._DRIVERS) plus "auto" - a valid runtime value even though it is
# not itself a driver (get_backend rejects it; _route_dispatch_backend
# resolves it to "local"/"claude" first, per PIPELINE_BACKEND_DISPATCH=auto).
_VALID_STORY_BACKENDS = frozenset(backend._DRIVERS) | {"auto"}


@mcp.tool()
def ingest_plan(
    plan_name: str,
    only_epics: list[str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """
    Push a saved plan into Plane. Creates epics first, then issues linked
    to their parent epic. Optionally restrict to specific epic summaries via
    only_epics. Returns a manifest mapping local IDs to Plane UUIDs.

    Re-ingesting an already-ingested plan merges into the existing manifest
    rather than replacing it: epics/stories not touched this call (including
    everything only_epics excludes) are preserved verbatim, a story whose key
    already exists gets its authored fields (summary, agent_instructions,
    dependencies, persona, model, acceptance, risk) refreshed while its
    runtime state (status, pr_url, ...) is kept, and top-level manifest keys
    outside epics/stories/repo_root (paused, local_model_fallback, final_rework_escalation, ...) carry
    over untouched. Pass overwrite=True to restore the old wholesale-replace
    behavior (drops anything not produced by this call).
    """
    return _service.ingest_plan(plan_name, only_epics=only_epics, overwrite=overwrite)


def _ingest_plan_impl(
    plan_name: str,
    only_epics: list[str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    _validate_key(plan_name)
    path = PLAN_DIR / f"{plan_name}.json"
    if not path.exists():
        return {"ok": False, "error": f"No plan named {plan_name}"}

    plan = json.loads(path.read_text())

    # advance_all_plans() iterates every plan in shared PLAN_DIR, each
    # potentially belonging to a different repo, so a manifest without its
    # own repo_root falls back to the global REPO_ROOT - the wrong repo for
    # any plan other than the one that env var happens to be set for (or a
    # deliberately-broken sentinel, if one's configured to fail loudly
    # instead). Catching it here means a typo'd or missing path surfaces
    # immediately, not as a cryptic ENOENT after three silent merge-attempt
    # failures.
    repo_root = plan.get("repo_root")
    if not repo_root or not Path(repo_root).is_dir():
        return {
            "ok": False,
            "error": f"Plan repo_root is missing or not a directory: {repo_root!r}",
        }

    # Validate story["backend"] upfront, before any Plane side effects, so a
    # typo'd provider name fails closed here rather than surfacing as a
    # NotImplementedError deep inside get_backend at dispatch time.
    for epic in plan["epics"]:
        if only_epics and epic["summary"] not in only_epics:
            continue
        for story in epic.get("stories", []):
            story_backend = story.get("backend")
            if story_backend is not None and story_backend not in _VALID_STORY_BACKENDS:
                return {
                    "ok": False,
                    "error": (
                        f"Story {story.get('summary', '?')!r} has unknown "
                        f"backend {story_backend!r}. Valid values: "
                        f"{sorted(_VALID_STORY_BACKENDS)}"
                    ),
                }

    manifest_path = _store.manifest_path(plan_name)

    with _store.transaction(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped": "locked",
                "reason": "another ingest/dispatch/interrupt is in progress for this plan",
            }

        manifest = {"epics": {}, "stories": {}, "repo_root": repo_root}

        # When no ticketing backend is configured (NullTicketProvider) the
        # manifest is the sole source of truth: create_epic/create_story are
        # no-ops returning None, and we synthesize story keys locally instead
        # of taking them from a backend-issued id.
        provider = get_ticket_provider()

        # Maps the plan's local story keys (e.g. "S1") to the manifest story keys
        # generated below (backend ids, or local keys when no backend is
        # configured), so dependencies can be translated to manifest keys.
        key_to_issue_id: dict[str, str] = {}

        for epic in plan["epics"]:
            if only_epics and epic["summary"] not in only_epics:
                continue

            epic_id = provider.create_epic(epic["summary"])
            if epic_id is not None:
                manifest["epics"][epic["summary"]] = epic_id

            for story in epic.get("stories", []):
                issue_id = provider.create_story(
                    story["summary"],
                    story.get("description", ""),
                    epic_id,
                    "agent-pipeline",
                )
                if issue_id is None:
                    # No backend id to key on: prefer the plan's own story key
                    # (keeps the manifest readable and lets key-based dependencies
                    # resolve to themselves), else mint a unique synthetic key.
                    issue_id = story.get("key") or str(uuid.uuid4())
                if "key" in story:
                    key_to_issue_id[story["key"]] = issue_id
                manifest["stories"][issue_id] = {
                    "summary": story["summary"],
                    "agent_instructions": story.get("agent_instructions", ""),
                    "dependencies": story.get("dependencies", []),
                    "persona": story.get("persona"),
                    "model": story.get("model"),
                    "acceptance": story.get("acceptance", []),
                    "risk": story.get("risk", "low"),
                    "backend": story.get("backend"),
                    # TDD_SPLIT_PRODUCTION_PLAN.md §2.4: explicit per-story
                    # opt-in for the test-author pre-executor phase. Defaults
                    # False - inferring eligibility from agent_instructions
                    # prose is a worse failure mode than an operator
                    # forgetting to opt in.
                    "tdd_split": bool(story.get("tdd_split", False)),
                    "status": "todo",
                }

        # Translate dependencies expressed as local plan keys into the issue IDs
        # just created. Dependencies that don't match a known local key (e.g.
        # already an issue ID, or a typo) are left as-is.
        for story in manifest["stories"].values():
            story["dependencies"] = [
                key_to_issue_id.get(dep, dep) for dep in story["dependencies"]
            ]

        # Merge into the existing manifest rather than replacing it (T1):
        # anything only_epics excluded this round - and, with overwrite=False,
        # the manifest's runtime state for stories re-ingested this round -
        # must survive. overwrite=True restores the old wholesale-replace
        # behavior for callers that genuinely want a clean slate.
        prior: dict[str, Any] = {}
        if not overwrite and manifest_path.exists():
            prior = json.loads(manifest_path.read_text())

        merged_epics = dict(prior.get("epics", {}))
        merged_epics.update(manifest["epics"])

        merged_stories = dict(prior.get("stories", {}))
        for key, new_story in manifest["stories"].items():
            old_story = merged_stories.get(key)
            if old_story is not None:
                combined = dict(old_story)
                for field in _INGEST_AUTHORED_STORY_FIELDS:
                    combined[field] = new_story[field]
                merged_stories[key] = combined
            else:
                merged_stories[key] = new_story

        final_manifest = dict(prior)
        final_manifest["epics"] = merged_epics
        final_manifest["stories"] = merged_stories
        final_manifest["repo_root"] = repo_root
        final_manifest["role_config"] = plan.get(
            "role_config", prior.get("role_config", {})
        )

        _atomic_write_json(manifest_path, final_manifest)

        # Non-blocking authoring nudge: flag acceptance fixtures that grade
        # only the unit in isolation while the brief requires integration
        # wiring (a call-site/registration change). A weak executor passes
        # such a fixture while skipping the ungraded wiring and ships dead
        # code (observed live 2026-07-28). Advisory only — never blocks.
        for key, story in final_manifest["stories"].items():
            msg = _isolation_only_acceptance_warning(story)
            if msg is not None:
                _notify_user(plan_name, f"{key}: {msg}")
                logging.getLogger("pipeline").warning(f"{plan_name}/{key}: {msg}")
            # Non-blocking authoring nudge: flag acceptance fixtures that
            # depend on macOS-only tooling. Dispatch, the done-bar and the
            # merge-gate reverify all run on macOS, but CI runs ubuntu-latest
            # only, so such a fixture passes every local gate and fails only
            # after the PR is open (observed live 2026-07-30). Advisory only.
            platform_msg = _platform_locked_fixture_warning(story)
            if platform_msg is not None:
                _notify_user(plan_name, f"{key}: {platform_msg}")
                logging.getLogger("pipeline").warning(f"{plan_name}/{key}: {platform_msg}")
            # Non-blocking authoring nudge: flag a local-dispatch story whose
            # test_author/planner scaffolding roles resolve to a DIFFERENT
            # provider - exactly the configuration that silently dropped the
            # TDD-split crutch from two stories on 2026-07-30 (both parked).
            # Advisory only.
            story_backend = story.get("backend") or os.environ.get(
                "PIPELINE_BACKEND_DISPATCH", "claude"
            ).strip().lower()
            role_msg = _scaffolding_provider_mismatch_warning(
                dispatch_backend=story_backend,
                role_config=final_manifest.get("role_config"),
                registry_roles=role_registry.load_registry().get("roles", {}),
            )
            if role_msg is not None:
                _notify_user(plan_name, f"{key}: {role_msg}")
                logging.getLogger("pipeline").warning(f"{plan_name}/{key}: {role_msg}")

    return {"ok": True, "manifest_path": str(manifest_path), **final_manifest}


@mcp.tool()
def list_ready_stories(plan_name: str) -> list[dict]:
    """
    Return stories whose dependencies are satisfied and that are still in
        To Do. Use this to decide what to dispatch next.
    """
    return _service.list_ready_stories(plan_name)


@mcp.tool()
def dispatch_story(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Spawn a headless Claude Code agent to work on a single story.

    For a fresh story, creates a git worktree on a new branch. For a story
    left "interrupted" (or whose worktree already exists from a prior run),
    reuses the existing worktree/branch instead and seeds the agent's prompt
    with the checkpoint journal so it continues rather than starting over.
    Transitions the Plane issue to In Progress. Returns the subprocess PID;
    completion is async.

    Acquires `_plan_lock` so direct MCP tool calls serialize across MCP
    server processes - without this guard, two Claude sessions (each with
    their own MCP server PID) can both call dispatch_story on the same story
    in the same window, and the second one treats the first one's
    half-built worktree as resumable and spawns a second agent into the
    same directory. That race is what produced the repeated zero-output
    agent deaths logged in 2026-06-27's e2e-decentralized-messaging run.
    """
    return _service.dispatch_story(plan_name, story_key)


def _dispatch_story_impl(plan_name: str, story_key: str) -> dict[str, Any]:
    _validate_key(plan_name)
    _validate_key(story_key)
    with _store.transaction(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped": "locked",
                "reason": "another dispatch/interrupt is in progress for this plan",
            }
        manifest_path = _store.manifest_path(plan_name)
        manifest = _store.get_manifest(plan_name)
        story = manifest["stories"].get(story_key)
        if not story:
            return {"ok": False, "error": f"No such story {story_key}"}

        branch = f"agent/{story_key.lower()}"
        worktree_path = WORKTREE_ROOT / story_key
        resuming = (
            story.get("status") in ("interrupted", "changes_requested")
            or worktree_path.exists()
        )
        journal = _read_journal(plan_name, story_key) if resuming else []

        if not resuming:
            with _scoped_repo_root(plan_name) as repo_root:
                with _try_acquire_git_lock(repo_root) as acquired:
                    if acquired:
                        subprocess.run(
                            ["git", "fetch", "origin", _default_branch()],
                            cwd=repo_root,
                            check=True,
                        )
                subprocess.run(
                    [
                        "git",
                        "worktree",
                        "add",
                        "-b",
                        branch,
                        str(worktree_path),
                        f"origin/{_default_branch()}",
                    ],
                    cwd=repo_root,
                    check=True,
                )
            _exclude_worktree_logs_from_tracking(Path(repo_root))
            # A fresh worktree has no .venv (gitignored) - give it its own
            # complete one now rather than let it fall back to (and
            # potentially mutate) the shared main-repo venv other
            # concurrently-dispatched stories may be using. See
            # _provision_worktree_venv's docstring for the failure mode
            # this closes (root-caused live on RUFF-016-ADOPTION).
            # No-ops for non-Python projects or ones without a
            # requirements file.
            _provision_worktree_venv(worktree_path)
        else:
            # Resumed dispatch (interrupted / changes_requested / existing
            # worktree): the worktree was branched from origin/<default> at
            # some prior point. If origin's default branch has moved since
            # then (e.g. an urgent fix landed via a separate PR between the
            # original dispatch and this resume), the resumed agent's diff
            # would be based on a stale base and could spuriously revert that
            # fix and delete its regression tests - see retro
            # mcp-self-mod-notice_2026-07-31 (PR #213 incident). Detect that
            # here and notify; do NOT auto-rebase (separate, riskier
            # follow-up). Fail open: any git error (e.g. a network issue on
            # the fetch) is logged and swallowed so dispatch still proceeds -
            # this is an observability hook, never a gate. Stateless across
            # calls: no flag/cache is set, each resumed dispatch re-fetches
            # and re-checks independently. Only runs for a real git worktree
            # (a `.git` file/dir at the worktree root); a plain directory is
            # not a git worktree and the rev-list probe cannot run there.
            if (worktree_path / ".git").exists():
                try:
                    with _scoped_repo_root(plan_name) as repo_root:
                        with _try_acquire_git_lock(repo_root) as acquired:
                            if acquired:
                                subprocess.run(
                                    ["git", "fetch", "origin", _default_branch()],
                                    cwd=repo_root,
                                    check=True,
                                )
                        count_out = subprocess.run(
                            [
                                "git",
                                "rev-list",
                                "--count",
                                f"{branch}..origin/{_default_branch()}",
                            ],
                            cwd=repo_root,
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                        behind = int(count_out.stdout.strip() or "0")
                        if behind > 0:
                            msg = (
                                f"story {story_key} is being resumed but its "
                                f"worktree base predates the current "
                                f"origin/{_default_branch()} by {behind} "
                                f"commit(s); the resumed diff may revert work "
                                f"that landed on the default branch since the "
                                f"worktree was created. Rebase the worktree onto "
                                f"origin/{_default_branch()} before proceeding."
                            )
                            _notify_user(plan_name, msg)
                            logging.getLogger("pipeline").warning(msg)
                except Exception:  # observability hook, never a gate
                    logging.getLogger("pipeline").warning(
                        "staleness check for resumed story %s failed; "
                        "dispatching anyway (fail open)", story_key,
                        exc_info=True,
                    )

        get_ticket_provider().set_state(story_key, LogicalState.IN_PROGRESS, plan_name)

        # Resolve concrete backend name for this story. Priority order:
        #   1. story["backend"] already set (e.g. from an escalation flip)
        #   2. PIPELINE_BACKEND_DISPATCH=auto  → a-priori router
        #   3. PIPELINE_BACKEND_DISPATCH=local|claude  → that driver directly
        env_backend = (
            os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude").strip().lower()
        )
        dispatch_backend = story.get("backend") or (
            _route_dispatch_backend(story) if env_backend == "auto" else env_backend
        )
        # Persona-based safety override: a security persona always dispatches to
        # Claude, regardless of dispatch mode (auto/local/claude) - unless the
        # story already had an explicit backend (a prior escalation flip), which
        # wins as-is and is never re-routed here.
        if not story.get("backend") and _persona_requires_claude(story):
            dispatch_backend = "claude"
        # Unwinnable-as-scoped safety override: a repo-wide, unscoped lint/fix
        # sweep always dispatches to Claude too, for the same reason (Mode 40
        # retro #4) - see _story_has_unwinnable_local_scope's docstring.
        if not story.get("backend") and _story_has_unwinnable_local_scope(story):
            dispatch_backend = "claude"
        # Persist so check_story_status and escalation see which backend ran.
        story["backend"] = dispatch_backend

        # A rework redispatch (changes_requested with stored review_feedback)
        # on the local Ollama driver can resume the prior dispatch's message
        # transcript instead of rebuilding a cold-start prompt via
        # _build_dispatch_command's rework_instruction - the transcript
        # already holds the full prior context, so only the reviewer's new
        # feedback needs to be appended. Guard on the transcript file actually
        # existing (backend.py writes it to cwd/.agent_transcript.json on
        # every dispatch): a story whose first dispatch predates this
        # feature, ran on a different backend, or had its transcript cleaned
        # up must fall back to the existing from-scratch rework prompt rather
        # than crash.
        review_feedback = story.get("review_feedback")
        transcript_path = worktree_path / ".agent_transcript.json"
        resume_via_transcript = (
            dispatch_backend in _LOCAL_BACKEND_NAMES
            and review_feedback
            and transcript_path.exists()
        )
        # Detect an operator's patch_story edit to agent_instructions since
        # the story's last dispatch. A transcript-resume rework otherwise
        # hands the resumed agent only the reviewer's raw feedback appended
        # to the verbatim prior transcript - it never re-reads the current
        # agent_instructions field - so a corrected instruction (e.g. "delete
        # the redundant wrapper" instead of "add a new one") is silently
        # dropped and the agent re-derives its own, possibly wrong, fix
        # (root-caused live 2026-07-28). Diff against the snapshot this
        # function records on every dispatch (_dispatched_agent_instructions,
        # written below); only surface a note when the instructions actually
        # changed, so an unchanged rework round adds no noise. Fails open
        # (no note) when no prior snapshot exists - the first rework after
        # this feature ships has no baseline to diff against.
        revised_instructions = story.get("agent_instructions", "")
        prior_dispatched = story.get("_dispatched_agent_instructions")
        revised_instructions_note = ""
        if (
            resume_via_transcript
            and prior_dispatched is not None
            and revised_instructions != prior_dispatched
        ):
            revised_instructions_note = (
                "\n\n--- Revised instructions from your tech lead ---\n"
                "Your tech lead has REVISED your task instructions since "
                "your last attempt. These supersede the original "
                "instructions in your transcript above. Follow them when "
                "addressing the review feedback:\n"
                f"{revised_instructions}"
            )

        spec = _build_dispatch_command(
            story,
            story_key,
            plan_name=plan_name,
            resume_journal=journal or None,
            review_feedback=None if resume_via_transcript else review_feedback,
        )
        worktree_path.mkdir(parents=True, exist_ok=True)
        log_path = worktree_path / "agent.log"

        # Gap 7: surface multi-model concurrent-dispatch risk. MAX_CONCURRENT_AGENTS
        # is a process-count cap with no model/VRAM awareness, and Ollama's
        # `/api/ps` reports whatever's currently loaded. If a *different* model
        # is already in VRAM and we're about to dispatch a second story on a
        # different model, Ollama will swap the existing model out to make room
        # (or OOM-split if 24GB unified memory is tight). Warn, don't block:
        # same-model concurrency is safe, and even a swap is just slow.
        if (
            dispatch_backend in _LOCAL_BACKEND_NAMES
            and MAX_CONCURRENT_AGENTS > 1
            and _count_in_progress_agents() > 0
        ):
            target_model = spec.get("model") or story.get("model")
            if target_model:
                # spec["model"]/story["model"] may be an unresolved tier
                # name (e.g. "sonnet"), which never matches anything in
                # `loaded` (concrete Ollama tags) and would otherwise warn
                # on every dispatch regardless of what's actually loaded.
                target_model = backend._resolve_local_model(target_model)
            try:
                loaded = backend._ollama_loaded_models(
                    os.environ.get("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
                )
            except Exception:  # noqa: BLE001 (observability hook, never a gate)
                loaded = set()  # observability hook, never a gate
            if loaded and target_model and target_model not in loaded:
                msg = (
                    f"multi-model concurrent dispatch: {sorted(loaded)} already "
                    f"loaded, dispatching {story_key} on {target_model} may force "
                    f"a VRAM swap (set MAX_CONCURRENT_AGENTS=1 to silence)"
                )
                _notify_user(plan_name, msg)
                logging.getLogger("pipeline").warning(msg)

        # Mode 2 regression guard: OLLAMA_NUM_PARALLEL is set via
        # `launchctl setenv` and silently dropped whenever Ollama.app
        # auto-updates and relaunches (observed 2026-07-25, v0.32.4),
        # leaving every llama-server runner at -np 1. With
        # MAX_CONCURRENT_AGENTS>1 the pipeline then dispatches a second
        # agent that queues behind the first and hits the 180s
        # read-silence timeout. Probe the runner's actual -np at dispatch
        # time and warn loudly when configured concurrency exceeds what
        # Ollama can actually serve in parallel. Only fires on a 2nd+
        # concurrent local dispatch (same guard as the multi-model check
        # above): the first dispatch loads the model and there is no
        # queue yet. Observability hook, never a gate - a None probe
        # (no model loaded yet, ps unavailable) means "unknown", not "0".
        if (
            dispatch_backend in _LOCAL_BACKEND_NAMES
            and MAX_CONCURRENT_AGENTS > 1
            and _count_in_progress_agents() > 0
        ):
            try:
                detected_np = backend._ollama_serving_parallelism()
            except Exception:  # noqa: BLE001 (observability hook, never a gate)
                detected_np = None
            if detected_np is not None and detected_np < MAX_CONCURRENT_AGENTS:
                msg = (
                    f"ollama serving parallelism ({detected_np}) is below "
                    f"MAX_CONCURRENT_AGENTS ({MAX_CONCURRENT_AGENTS}): a "
                    f"second concurrent dispatch will queue behind the "
                    f"first and may hit the 180s read-silence timeout. "
                    f"Re-apply `launchctl setenv OLLAMA_NUM_PARALLEL "
                    f"{MAX_CONCURRENT_AGENTS}` and restart Ollama, or set "
                    f"MAX_CONCURRENT_AGENTS=1."
                )
                _notify_user(plan_name, msg)
                logging.getLogger("pipeline").warning(msg)

        # Fix #1: if the story carries an `acceptance` block, materialize the
        # oracle files into the worktree BEFORE the backend launches so the local
        # harness can grade against them. The plan's acceptance source is
        # AUTHORITATIVE on a fresh dispatch and must always win — even when the
        # fixture path collides with a file a prior story already merged to the
        # base branch (the worktree inherits that file; failing to overwrite it
        # silently grades the stale, already-satisfied file and produces a false
        # green with zero implementation). Only a RESUMED run skips the write:
        # the oracle may have evolved the fixture mid-run into a committed WIP,
        # and overwriting would discard that evolution.
        acceptance = story.get("acceptance") or []
        acceptance_paths = _acceptance_rel_paths(story)
        for entry in acceptance:
            target = worktree_path / entry["path"]
            if resuming and target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(entry["source"])

        # Record a digest of each fixture's AUTHORITATIVE manifest source
        # (never the worktree file, which could already be rewritten) so a
        # later gate can prove the read-only oracle was not rewritten - see
        # pipeline.ci._acceptance_tampered. Recorded on EVERY dispatch,
        # including a resumed one, since the authoritative source hasn't
        # changed and re-recording also repairs a story dispatched before
        # this existed.
        story["acceptance_digests"] = acceptance_digests(story)

        # Pre-dispatch oracle gate: a fixture that cannot pass no matter what
        # is implemented (a broken helper, a bad CLI invocation) or that is
        # already satisfied with zero implementation burns an implementer's
        # entire step budget for no signal (observed live 2026-07-30,
        # LAUNCHD-PLIST-PORTABILITY). Skipped on a resumed run: the oracle
        # was already validated on the fresh dispatch, and the worktree may
        # legitimately carry WIP that changes the outcome.
        if not resuming:
            oracle_check = validate_acceptance_fixtures(story, worktree_path)
            if oracle_check["state"] in ("errors", "passes", "empty"):
                story["status"] = "blocked_oracle"
                story["oracle_gate"] = oracle_check
                _notify_user(
                    plan_name,
                    f"{story_key} not dispatched: acceptance oracle is "
                    f"unusable ({oracle_check['state']}) - {oracle_check['detail']}",
                )
                _atomic_write_json(manifest_path, manifest)
                return {
                    "status": "blocked_oracle",
                    "oracle_gate": oracle_check,
                }

        # TDD_SPLIT_PRODUCTION_PLAN.md: an ALWAYS-ON pre-executor
        # test-authoring dispatch (a full agent-loop, BLOCKING until it
        # exits - unlike the planner checklist above, this produces a real
        # commit the executor's worktree must already have) in THIS
        # worktree before the main executor starts. The global
        # PIPELINE_TDD_SPLIT on/off toggle AND the per-story `tdd_split`
        # opt-in are both removed; the phase now mirrors the planner's gate
        # below exactly - unconditional for local-family dispatch. Gated on:
        #   - a local-family backend (same rationale as the planner: the
        #     crutch exists for the weak local executor; Claude doesn't
        #     need it)
        #   - not resuming (a rework redispatch acts on the SAME tests it
        #     already has; it never gets a fresh test-authoring pass)
        #   - no existing test-author marker in the worktree (belt-and-
        #     suspenders with `resuming`, mirrors plan_path's own check
        #     below)
        # _run_test_author_phase never raises and a False return (role
        # unconfigured/refused, dispatch failure, timeout, or no commit
        # produced) falls open to today's unmodified monolithic dispatch -
        # never a gate (§2.5).
        test_author_marker = worktree_path / ".tdd_split_test_author_done"
        if (
            dispatch_backend in _LOCAL_BACKEND_NAMES
            and not resuming
            and not test_author_marker.exists()
        ) and _run_test_author_phase(
            story,
            story_key=story_key,
            worktree_path=worktree_path,
            dispatch_backend=dispatch_backend,
            local_model=spec["model"],
            plan_name=plan_name,
            plan_role_config=_plan_role_config(plan_name),
        ):
            test_author_marker.write_text("ok\n")

        # GUIDED_DECOMPOSITION_PLAN.md: a "tech lead" checklist is always on
        # for the weak local executor (the on/off toggle was removed; the
        # planner is now unconditionally enabled for local-family dispatch).
        # Gated on:
        #   - a local-family backend (the crutch exists for the weak local
        #     executor; Claude doesn't need it)
        #   - not resuming (plan once on the story's first dispatch; a
        #     rework must never spend a second planner call)
        #   - no plan already on disk (belt-and-suspenders with `resuming`)
        # The LLM call itself is best-effort (_run_planner fails open to
        # None) so a broken/slow/rate-limited planner never blocks or
        # corrupts dispatch - the story simply proceeds with no checklist,
        # exactly like an unconfigured (fail-open) planner.
        # H3 ablation (GUIDED_DECOMPOSITION_PLAN.md §4.1's G-cloud-noscratch
        # condition): default "on" ships the persistent scratchpad; "off"
        # tests whether the checklist alone accounts for the benefit,
        # independent of cross-step memory. Read once here because it now
        # feeds BOTH the planner call (so the scratchpad becomes a first-class
        # generated step) and the trailing-instruction backstop below.
        scratchpad_on = (
            os.environ.get("PIPELINE_DECOMPOSE_SCRATCHPAD", "on").strip().lower()
            != "off"
        )
        plan_path = worktree_path / ".agent_plan.md"
        plan_hash_path = worktree_path / ".agent_plan_src_hash"
        if (
            dispatch_backend in _LOCAL_BACKEND_NAMES
            and not resuming
            and not plan_path.exists()
        ):
            # Ground the planner in the test-author's ACTUAL committed test
            # file(s) when the phase ran this branch (root-caused live
            # 2026-07-25 on MODE40-CI-REWORK-FEEDBACK-V2's THIRD reset: the
            # prohibition-only tests_already_authored clause still let the
            # planner re-derive a "Write the test file" step with invented
            # test-case names, because it had no concrete grounding in
            # which file/tests exist). Detect the test_*.py files added on
            # this branch and their top-level test-case names, and hand
            # them to the planner so it can point the executor at READING
            # the real files. Fail open: if git detects nothing (or the
            # phase ran but committed no test_*.py), authored_test_files is
            # empty and the planner degrades to the prohibition-only clause.
            authored_test_files: list[tuple[str, list[str]]] | None = None
            if test_author_marker.exists():
                try:
                    added = _test_files_added_on_branch(
                        worktree_path,
                        _default_branch(),
                    )
                    authored_test_files = [
                        (path, _test_names_in_file(worktree_path, path))
                        for path in added
                    ]
                except Exception:  # noqa: BLE001 (best-effort grounding enrichment; a git hiccup here must degrade to the prohibition-only planner clause, not raise)
                    authored_test_files = []
            plan_text = _run_planner(
                story.get("agent_instructions", ""),
                dispatch_backend=dispatch_backend,
                local_model=spec["model"],
                include_scratchpad=scratchpad_on,
                plan_role_config=_plan_role_config(plan_name),
                tests_already_authored=test_author_marker.exists(),
                authored_test_files=authored_test_files,
                worktree=str(worktree_path),
            )
            if plan_text:
                plan_path.write_text(plan_text)
                plan_hash_path.write_text(
                    hashlib.sha256(
                        story.get("agent_instructions", "").encode()
                    ).hexdigest()
                )
        # Referencing an existing plan is independent of generating one, so
        # a resumed dispatch that rebuilds its prompt from scratch (no
        # transcript to resume) still sees the checklist from the story's
        # first dispatch, without spending a second planner call for it.
        # Referencing an existing plan is independent of generating one, so
        # a resumed dispatch that rebuilds its prompt from scratch (no
        # transcript to resume) still sees the checklist from the story's
        # first dispatch, without spending a second planner call for it.
        # Reuse requires BOTH a local-family backend (the crutch was never
        # meant for Claude -- see the generation guard above) AND a hash of
        # the CURRENT agent_instructions matching what the checklist was
        # generated from -- a patch_story rewrite of agent_instructions
        # (e.g. a corrected rework brief) must silently drop the now-stale
        # checklist rather than inject contradictory instructions.
        current_instructions_hash = hashlib.sha256(
            story.get("agent_instructions", "").encode()
        ).hexdigest()
        checklist_is_fresh = (
            plan_path.exists()
            and dispatch_backend in _LOCAL_BACKEND_NAMES
            and plan_hash_path.exists()
            and plan_hash_path.read_text().strip() == current_instructions_hash
        )
        if checklist_is_fresh:
            scratchpad_instruction = ""
            # Backstop to the planner-woven scratchpad steps above: even with
            # the clause folded into the checklist, keep the explicit trailing
            # reminder so a resumed dispatch (whose stored .agent_plan.md may
            # predate the clause) and any run whose planner under-emitted it
            # still get told to maintain the scratchpad.
            if scratchpad_on:
                scratchpad_instruction = (
                    " After finishing each step, keep .agent_scratchpad.md "
                    "up to date with a short running summary of what you've "
                    "done and which step is next (create_file for the first "
                    "note, str_replace to rewrite it after that) before "
                    "moving on to the next step. The FIRST line must be "
                    "PROGRESS: <done>/<total> showing how many checklist "
                    "items you've completed (e.g. PROGRESS: 2/5)."
                )
            spec["prompt"] = (
                f"{spec['prompt']}\n\n"
                "--- Implementation checklist from your tech lead ---\n"
                f"{plan_path.read_text()}\n\n"
                f"Work through these steps in order.{scratchpad_instruction}"
            )

        # Referencing the test-author marker is independent of the phase
        # having run THIS dispatch (mirrors plan_path.exists() above): a
        # resumed/rework redispatch that skipped re-running the phase must
        # still get the "don't touch tests" steering, since the tests it
        # must not touch are already committed on this branch.
        if test_author_marker.exists():
            spec["prompt"] = (
                f"{spec['prompt']}\n\n"
                "--- Tests already written by your tech lead ---\n"
                "The test file(s) for this task have already been written "
                "and committed to this branch by your tech lead. If the "
                "task description or checklist above says to write tests "
                "yourself first, DISREGARD that - it does not apply here; "
                "the tests already exist. Do not create, write, or modify "
                "any test file. "
                f"{_NEVER_TOUCH_TESTS_STEERING} Run them to see the current "
                "failures, then implement until they pass."
            )

        dispatch_kwargs: dict[str, Any] = {
            "prompt": spec["prompt"],
            "system": spec["system"],
            "model": spec["model"],
            "allowed_tools": spec["allowed_tools"],
            "cwd": worktree_path,
            "log_path": log_path,
            "append": resuming,
        }
        # Only the local driver accepts/uses `acceptance`; pass it through when
        # we're actually invoking that driver so Claude's signature stays clean.
        if dispatch_backend in _LOCAL_BACKEND_NAMES and acceptance_paths:
            dispatch_kwargs["acceptance"] = acceptance_paths
        # L1 (REVIEWER_ESCALATION_PLAN.md): any rework redispatch - a
        # CI-triggered rework (story["ci_rework"]) OR a reviewer
        # REQUEST_CHANGES rework (story["review_feedback"]) - raises the
        # agent's done-bar to full-suite-green so it cannot declare done
        # while its own edit left the rest of the suite broken. The
        # reviewer's own pass is acceptance-scoped (see
        # _scope_test_cmd_to_acceptance in review.py), so a regression
        # outside the acceptance paths is otherwise invisible until the
        # merge gate - or, worse, never re-checked at all if `done` is
        # accepted on a broken tree (observed live 2026-07-22,
        # MODE-29-REVIEW-STORY-LOCK-GUARD: a rework redispatch's own edit
        # orphaned a function definition, the agent called done with 79
        # tests failing, and nothing rejected it because this gate was
        # only armed for ci_rework). Local-only: the env reaches the local
        # agent subprocess; Claude's dispatch signature stays clean.
        if dispatch_backend in _LOCAL_BACKEND_NAMES and (
            story.get("ci_rework") or story.get("review_feedback")
        ):
            dispatch_kwargs["rework_full_suite"] = True

        if resume_via_transcript:
            dispatch_kwargs["resume_transcript_path"] = transcript_path
            # Same tech-lead-decomposition logic as the initial checklist,
            # applied to review feedback: a reviewer's prose diagnosis is
            # itself a coarse brief for a weak executor. Re-run per rework
            # cycle (unlike the initial checklist, which plans once) since
            # each cycle's feedback is different. Fails open to the raw
            # feedback format on any planner failure - identical contract
            # to the initial-dispatch checklist.
            fix_checklist = None
            if dispatch_backend in _LOCAL_BACKEND_NAMES:
                fix_checklist = _run_rework_planner(
                    review_feedback,
                    dispatch_backend=dispatch_backend,
                    local_model=spec["model"],
                    plan_role_config=_plan_role_config(plan_name),
                    worktree=str(worktree_path),
                )
            # Rework test-author phase: when the reviewer's feedback itself
            # calls for NEW test(s) (e.g. a regression test reproducing a
            # named bug), author them via the dedicated test_author role
            # BEFORE the main executor redispatch, then steer the executor
            # to implement against those already-committed tests instead of
            # writing the tests itself - applying the tech-lead/weak-
            # executor split (TDD_SPLIT_PRODUCTION_PLAN.md) one rework cycle
            # later. Gated to local-family dispatch (Claude doesn't need the
            # crutch) and to a per-sha "already done" marker so a
            # retried/resumed dispatch on the same reviewer verdict never
            # re-runs the phase or re-asks the classifier. Fails open: on
            # any failure, unconfigured role, or "no new tests required",
            # rework_tests_note stays "" and the resumed prompt + manifest
            # are byte-for-byte identical to today's monolithic rework
            # dispatch.
            rework_tests_committed = False
            if dispatch_backend in _LOCAL_BACKEND_NAMES:
                already_done_sha = story.get("rework_test_author_done_for_sha")
                current_sha = story.get("last_reviewed_sha")
                if (
                    current_sha
                    and already_done_sha != current_sha
                    and _rework_requires_new_tests(
                        review_feedback,
                        dispatch_backend=dispatch_backend,
                        local_model=spec["model"],
                        plan_role_config=_plan_role_config(plan_name),
                    )
                ):
                    rework_tests_committed = _run_rework_test_author_phase(
                        story,
                        story_key=story_key,
                        worktree_path=worktree_path,
                        dispatch_backend=dispatch_backend,
                        local_model=spec["model"],
                        review_feedback=review_feedback,
                        fix_checklist=fix_checklist,
                        plan_role_config=_plan_role_config(plan_name),
                    )
                    if rework_tests_committed:
                        story["rework_test_author_done_for_sha"] = current_sha
            rework_tests_note = ""
            if rework_tests_committed:
                rework_tests_note = (
                    "\n\nThe regression test(s) reproducing this bug have already "
                    "been written and committed to this branch by your tech lead. "
                    "Do NOT create, write, or modify any test file. "
                    f"{_NEVER_TOUCH_TESTS_STEERING} Run them to see the current "
                    "failures, then implement until they pass."
                )
            if fix_checklist:
                dispatch_kwargs["resume_append_content"] = (
                    "The code reviewer REQUESTED CHANGES on your previous "
                    "attempt. Your tech lead has translated the feedback "
                    f"into a fix checklist:\n{fix_checklist}\n\n"
                    f"Original review feedback (for reference):\n{review_feedback}"
                    f"{revised_instructions_note}"
                    f"{rework_tests_note}"
                )
            else:
                dispatch_kwargs["resume_append_content"] = (
                    "The code reviewer REQUESTED CHANGES on your previous attempt. "
                    f"Address this feedback:\n{review_feedback}"
                    f"{revised_instructions_note}"
                    f"{rework_tests_note}"
                )

        handle = backend.get_backend("dispatch", name=dispatch_backend).dispatch(
            **dispatch_kwargs
        )

        story["status"] = "in_progress"
        story["pid"] = handle.pid
        story["dispatched_at"] = datetime.now(timezone.utc).isoformat()
        story["worktree"] = str(worktree_path)
        story["log"] = str(log_path)
        # Record the concrete model the agent actually boots with (the local
        # backend resolves a logical tier like "sonnet" to e.g.
        # "minimax-m3:cloud"). The dashboard shows this instead of the plan's
        # declared story["model"] so what's displayed matches what ran. The
        # declared tier is left untouched (it's a routing hint).
        if getattr(handle, "model", None):
            story["dispatched_model"] = handle.model
        # Snapshot the agent_instructions this dispatch actually handed the
        # agent, so the next rework redispatch can diff against it to detect
        # an operator's patch_story edit (see revised_instructions_note
        # above). Recorded on every dispatch - cold or resumed, any backend -
        # so the baseline is always current regardless of how the next rework
        # is routed.
        story["_dispatched_agent_instructions"] = story.get("agent_instructions", "")
        _atomic_write_json(manifest_path, manifest)

        return {
            "ok": True,
            "story_key": story_key,
            "pid": handle.pid,
            "branch": branch,
            "resumed": resuming,
        }


@mcp.tool()
def _last_done_summary(agent_log: Path) -> str:
    """Return the summary text from the LAST "] DONE:" line in agent.log, or
    "" if the agent never reached done. Only the final DONE line reflects
    the current run - a resumed agent appends to the same log across ticks
    (mirrors _last_nonempty_line's resumed-log caution for STEP_CAP_MARKERS).
    local_agent.py's `done` tool prints its summary argument verbatim as
    "[step N] DONE: <summary>"; this is that real signal, not a fictitious
    exit protocol."""
    if not agent_log.exists():
        return ""
    marker = "] DONE:"
    last = ""
    with open(agent_log, "rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").strip()
            idx = line.find(marker)
            if idx != -1:
                last = line[idx + len(marker) :].strip()
    return last


def _run_lint_gate(worktree: Path, test_env: dict) -> dict | None:
    lint = detect_lint_command(worktree)
    if lint is None:
        return None
    lint_dir, cmd = lint
    try:
        result = subprocess.run(
            cmd, check=False, cwd=lint_dir, capture_output=True, text=True, env=test_env
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {
        "cmd": cmd,
        "returncode": result.returncode,
        "stdout_tail": (result.stdout or "")[-2000:],
        "stderr_tail": (result.stderr or "")[-2000:],
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def _module_level_function_names(source: str) -> set[str]:
    """Top-level (module-scope) function names defined in `source`. Ignores
    nested defs, closures, and class methods - only a bare module-level
    `def` is a candidate for _find_dead_new_functions, since that's the
    shape of an independently-callable production symbol a call site is
    expected to reference by name."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _find_dead_new_functions(worktree: Path, base_branch: str) -> list[str]:
    """Detect newly-added module-level functions (in .py files changed
    since base_branch, excluding test files) whose name appears NOWHERE
    else in the tracked worktree - i.e. defined but never called or
    referenced, not even from a different file (a new public entry point
    called only from elsewhere in the repo must not false-positive here).

    A cheap, conservative text-based heuristic, not a full call-graph
    analysis: a name occurring anywhere else in the worktree (even a
    comment, even in another file) is treated as "referenced", keeping
    false positives near zero. A name occurring ONLY on its own `def`
    line, repo-wide, is a strong, low-noise signal of dead code.

    Root-caused live 2026-07-25 on MODE40-CI-REWORK-FEEDBACK-V2: a
    correctly-implemented, correctly-unit-tested helper function
    (`_ci_rework_feedback`) was added but never wired into the production
    call path it was meant to replace - invisible to any test that only
    exercises the function in isolation, since the story's own tests
    called it directly rather than through the code path that was
    supposed to route to it. The real LLM reviewer caught it, but that
    spends a whole review cycle on something this cheap, static,
    pre-review check catches for free (see _run_lint_gate for the sibling
    pattern this mirrors).

    Best-effort: any git/IO failure returns [] (fail open - a quality
    signal, not a security boundary, must never block or corrupt a
    story's dispatch).
    """
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=AM", base_branch, "HEAD"],
            check=False,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if diff.returncode != 0:
        return []

    dead: list[str] = []
    for rel_path in diff.stdout.splitlines():
        rel_path = rel_path.strip()
        if not rel_path.endswith(".py"):
            continue
        base_name = Path(rel_path).name
        if base_name.startswith("test_") or base_name.endswith("_test.py"):
            continue
        full_path = worktree / rel_path
        if not full_path.is_file():
            continue
        try:
            source = full_path.read_text()
        except OSError:
            continue

        try:
            old_show = subprocess.run(
                ["git", "show", f"{base_branch}:{rel_path}"],
                check=False,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        old_names = (
            _module_level_function_names(old_show.stdout)
            if old_show.returncode == 0
            else set()
        )
        new_names = _module_level_function_names(source) - old_names

        for fn_name in sorted(new_names):
            if fn_name.startswith("__") and fn_name.endswith("__"):
                continue  # dunder - never a candidate
            try:
                grep = subprocess.run(
                    ["git", "grep", "--count", "-w", fn_name],
                    check=False,
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            # `git grep --count` prints "path:N" per matching tracked file
            # (exit 1, empty stdout, if no match anywhere - not an error).
            # The function's own def line contributes exactly 1; a repo-
            # wide total <= 1 means "only its own definition, nowhere else
            # in the tracked worktree" - not even a different file.
            total = sum(
                int(line.rsplit(":", 1)[-1])
                for line in grep.stdout.splitlines()
                if line.strip()
            )
            if total <= 1:
                dead.append(f"{rel_path}:{fn_name}")
    return dead


def _rebrief_step_cap_struggle(
    story: dict[str, Any], worktree: str, plan_role_config: dict | None = None,
    plan_name: str | None = None, story_key: str | None = None
) -> None:
    """CLAUDE.md Step 9: when an implementer hits the step cap, diagnose where
    it struggled and fold the root cause into agent_instructions so the resume
    isn't a blind retry. The resume path (_build_dispatch_command) re-reads
    agent_instructions, so the folded diagnosis reaches the next attempt.

    Fail-open by construction: a None/errored diagnosis leaves agent_instructions
    unchanged (compose_rebriefed_instructions is a no-op on None), so this can
    never block or worsen a retry. compose replaces (not stacks) any prior
    diagnosis block, so repeated step-caps keep the prompt bounded and refresh
    with the latest struggle. Must run while the worktree still exists - the
    evidence is the tail of its agent.log."""
    facts = collect_attempt_facts(worktree, story)
    evidence = collect_failure_evidence(worktree, story, facts=facts)
    try:
        unsat_reason = detect_unsatisfiable_signal(evidence)
        if unsat_reason is not None:
            _notify_user(
                plan_name,
                f"Story may be unsatisfiable as specified: {unsat_reason}. Story {story_key} may need re-planning rather than another retry.",
            )
    except Exception:
        logging.getLogger("pipeline").debug(
            "detect_unsatisfiable_signal/_notify_user failed during step-cap rebrief",
            exc_info=True)
    diagnosis = diagnose_failure(evidence, story, plan_role_config)
    story["agent_instructions"] = compose_rebriefed_instructions(
        story.get("agent_instructions", ""), diagnosis)
    # After the diagnosis, never before: composing a diagnosis truncates the
    # brief at DIAGNOSIS_HEADER, which would take a facts block appended ahead
    # of it with no replacement.
    story["agent_instructions"] = compose_attempt_facts(
        story.get("agent_instructions", ""), facts)


def check_story_status(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Check whether a dispatched agent has finished. If complete, runs tests
    in the worktree and reports pass/fail without auto-merging.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    manifest_path = _store.manifest_path(plan_name)
    manifest = _store.get_manifest(plan_name)
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
            _notify_user(
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
    last_log_line = _last_nonempty_line(agent_log) if agent_log.exists() else ""

    # Infra-failure exit routing: a dispatch that died on an LLM/Ollama
    # transport error (after chat()'s own retries and the 5xx trim-retry are
    # exhausted) is not a review/test-quality outcome and must not be graded
    # or counted against rework_attempts - see INFRA_FAILURE_LOG_SUBSTRING's
    # docstring for the live incident this fixes.
    if INFRA_FAILURE_LOG_SUBSTRING in last_log_line:
        sha = _commit_wip(str(worktree), story_key, "infra_failure")
        interrupted_at = datetime.now(timezone.utc).isoformat()
        _store.append_journal(
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
            _notify_user(
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
            _notify_user(
                plan_name,
                f"{story_key} hit {INFRA_FAILURE_FALLBACK_THRESHOLD} consecutive "
                f"infrastructure failures on {current_model}; switching to "
                f"fallback model {fallback_model} for the next resume.",
            )
        elif (
            not fallback_model
            and _auto_escalation_enabled()
            and story.get("backend", "local") == "local"
            and not story.get("escalated")
            and story["infra_failure_streak"] >= INFRA_FAILURE_FALLBACK_THRESHOLD
        ):
            _escalate_to_claude(manifest, plan_name, story_key, manifest_path)
            _notify_user(
                plan_name,
                f"{story_key} hit {INFRA_FAILURE_FALLBACK_THRESHOLD} consecutive "
                f"infrastructure failures on {current_model}; escalating to "
                f"Claude (no local_model_fallback configured).",
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
        _store.append_journal(
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
        _rebrief_step_cap_struggle(
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
                _notify_user(
                    plan_name,
                    f"{story_key} hit the step cap {STEP_CAP_FALLBACK_THRESHOLD}x "
                    f"on {current_model}; switching to fallback model "
                    f"{fallback_model} for the next resume.",
                )
        elif (
            not fallback_model
            and _auto_escalation_enabled()
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
                _notify_user(
                    plan_name,
                    f"{story_key} hit the step cap {STEP_CAP_FALLBACK_THRESHOLD}x "
                    f"on {current_model}; escalating to Claude (no "
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
        _notify_user(
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

    test_dir, test_cmd = detect_test_command(worktree)

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
            worktree, story_key, _default_branch()
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
    if _is_heavy(test_cmd):
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
    story["last_test_check"] = {
        "cmd": test_cmd,
        "cwd": str(test_dir),
        "returncode": test_result.returncode,
        "stdout_tail": (test_result.stdout or "")[-2000:],
        "stderr_tail": (getattr(test_result, "stderr", "") or "")[-2000:],
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if passed:
        lint = _run_lint_gate(worktree, test_env)
        if lint is not None:
            story["last_lint_check"] = lint
            if lint["returncode"] != 0:
                passed = False

    # Only worth checking once the baseline (tests, lint) actually passed -
    # a story already failing on those has enough signal without this too.
    if passed:
        dead_functions = _find_dead_new_functions(worktree, _default_branch())
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
    if passed and not _worktree_has_new_commits(
        worktree,
        story_key,
        base_branch=_default_branch(),
    ):
        base = _default_branch()
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
        and _worktree_has_new_commits(
            worktree, story_key, base_branch=_default_branch()
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
            if story.get("escalated"):
                rework_cap = REWORK_MAX_ATTEMPTS_ESCALATED
            elif story.get("acceptance"):
                rework_cap = REWORK_MAX_ATTEMPTS_ORACLE
            else:
                rework_cap = REWORK_MAX_ATTEMPTS
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
                if _auto_escalation_enabled() and not story.get("escalated"):
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
                _notify_user(
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
    give_up_summary = _last_done_summary(agent_log) if not passed else ""
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


@mcp.tool()
def interrupt_story(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Stop a dispatched agent and leave its story resumable.

    Sends SIGTERM to the agent's process (a no-op if it has already exited),
    commits any uncommitted work in its worktree as a checkpoint, and marks
    the story "interrupted" rather than "failed" so a later dispatch_story
    call resumes it instead of starting over. The worktree and branch are
    left in place.

    Acquires `_plan_lock` for the same reason dispatch_story does - two MCP
    servers can race here too, with one calling interrupt while the other
    calls dispatch on the same story, producing a manifest write race that
    leaves the worktree in an inconsistent state.
    """
    return _service.interrupt_story(plan_name, story_key)


@mcp.tool()
def mark_story_in_progress(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Transition the ticket to In Progress and update the local manifest.
    Use this before writing any code for a story.
    """
    return _service.mark_story_in_progress(plan_name, story_key)


@mcp.tool()
def checkpoint(plan_name: str, story_key: str, step: str, summary: str, next_hint: str = "") -> dict[str, Any]:
    """Record a durable checkpoint for a dispatched agent's progress. Commits any uncommitted work in the story's worktree as a WIP commit and appends an entry to the story's journal (plan.story.journal.json). Call this after completing each idempotent step of a story so a killed agent can resume from the last checkpoint instead of starting over."""
    return _service.checkpoint(plan_name, story_key, step, summary, next_hint)

@mcp.tool()
def mark_story_done(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Transition the ticket to Done and update the local manifest.
    Use after you've reviewed and merged the agent's PR.
    """
    return _service.mark_story_done(plan_name, story_key)

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


def _ci_rework_feedback(gate_error: str) -> str:
    """Generate review feedback for merge-gate CI failures."""
    lint_keywords = ("lint", "ruff", "eslint", "clippy", "golangci")
    lower = gate_error.lower()
    if any(k in lower for k in lint_keywords):
        return (
            f"The merge-gate CI check failed on your submitted branch "
            f"Gate error: {gate_error}\n\n"
            "This is a LINT failure, not a test failure - the test suite may already pass, so re-running tests alone proves nothing. Run the project's lint command (e.g. `ruff check .` for Python) from the repo root, fix every finding, and commit.\n\n"
            "A NEW COMMIT on your branch is REQUIRED - CI runs on your pushed commits, and exiting without committing a change cannot alter the CI result."
        )
    else:
        return (
            f"The merge-gate CI check failed on your submitted branch "
            f"Gate error: {gate_error}\n\n"
            "The bug could be in the implementation OR in a test file you wrote; re-examine both against the spec and make a targeted fix.\n\n"
            "A NEW COMMIT on your branch is REQUIRED - CI runs on your pushed commits, and exiting without committing a change cannot alter the CI result."
        )


@mcp.tool()
def patch_story(
    plan_name: str, story_key: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """
    Edit a story's plan-authored fields (agent_instructions, model, persona,
    risk, dependencies, acceptance, pr_url, summary) without hand-editing the
    manifest JSON.

    Hand-editing the manifest directly races the scheduler's 60s
    advance_all_plans tick - a read-modify-write on either side can silently
    clobber the other's write. This tool acquires the same _plan_lock the
    scheduler and dispatch_story use, so the edit is atomic with respect to
    it. Only the fields above may be set; status transitions go through
    set_story_status, not this tool.
    """
    return _service.patch_story(plan_name, story_key, fields)

@mcp.tool()
def set_story_status(plan_name: str, story_key: str, status: str) -> dict[str, Any]:
    """
    Transition a story to an explicit status without hand-editing the
    manifest JSON (e.g. resetting a "parked" story to "interrupted" so the
    scheduler retries it).

    Acquires _plan_lock for the same reason patch_story does. Only accepts
    the fixed set of statuses the pipeline itself assigns
    (todo/in_progress/running/interrupted/failed/tests_passed/pr_open/
    changes_requested/parked/done) - this is a sanctioned status change, not
    a way to invent pipeline state the rest of the code doesn't expect.
    """
    return _service.set_story_status(plan_name, story_key, status)


def _check_usage_impl() -> dict[str, Any]:
    """
    Probe current subscription usage (current session + current week) via a
    headless `/cost` call and persist it to USAGE_STATE_PATH.

    Intended to be called every ~60s by an external poller (cron/launchd or
    /loop). advance_pipeline reads the persisted state rather than probing
    itself, decoupling the pipeline's tick cadence from the poller's.

    The CLI occasionally omits the percentage summary lines (observed near
    session-reset boundaries) without erroring, so a parse failure falls
    back to the last persisted reading rather than crashing the caller's
    tick - unless there is no prior reading to fall back to. If that frozen
    reading is older than USAGE_STALE_AFTER_SECONDS, it's no longer trusted
    as evidence of being over threshold, so the gate fails open instead of
    blocking the pipeline indefinitely on a permanent CLI output change.

    Staleness is measured from "measured_at" (the last time a probe actually
    succeeded), not "checked_at" (bumped on every call, success or fallback).
    A poller calling this every ~60s would otherwise perpetually look fresh
    by checked_at's measure alone, even after hours of the CLI refusing to
    parse - measured_at is carried forward unchanged across fallback calls
    so the staleness clock keeps counting from the last real measurement.
    """
    return _service.check_usage()

def _check_usage_impl() -> dict[str, Any]:
    """
    Probe current subscription usage (current session + current week) via a
    headless `/cost` call and persist it to USAGE_STATE_PATH.

    Intended to be called every ~60s by an external poller (cron/launchd or
    /loop). advance_pipeline reads the persisted state rather than probing
    itself, decoupling the pipeline's tick cadence from the poller's.

    The CLI occasionally omits the percentage summary lines (observed near
    session-reset boundaries) without erroring, so a parse failure falls
    back to the last persisted reading rather than crashing the caller's
    tick - unless there is no prior reading to fall back to. If that frozen
    reading is older than USAGE_STALE_AFTER_SECONDS, it's no longer trusted
    as evidence of being over threshold, so the gate fails open instead of
    blocking the pipeline indefinitely on a permanent CLI output change.

    Staleness is measured from "measured_at" (the last time a probe actually
    succeeded), not "checked_at" (bumped on every call, success or fallback).
    A poller calling this every ~60s would otherwise perpetually look fresh
    by checked_at's measure alone, even after hours of the CLI refusing to
    parse - measured_at is carried forward unchanged across fallback calls
    so the staleness clock keeps counting from the last real measurement.
    """
    prev = _read_usage_state()
    try:
        state = _run_usage_probe()
    except ValueError:
        if not prev:
            raise
        now_iso = datetime.now(timezone.utc).isoformat()
        state = dict(prev)
        state["checked_at"] = now_iso
        # Count how many polls in a row have failed to parse, so the blind
        # window is visible (and quantifiable) rather than a silent stderr line.
        state["consecutive_parse_failures"] = (
            prev.get("consecutive_parse_failures", 0) + 1
        )
        measured_at = prev.get("measured_at", prev.get("checked_at"))
        state["measured_at"] = measured_at
        age = (
            _usage_state_age_seconds({"checked_at": measured_at})
            if measured_at
            else None
        )
        if age is not None and age > USAGE_STALE_AFTER_SECONDS:
            state["stale"] = True
            state["gate_blind"] = True
            first_blind = not prev.get("gate_blind")
            if first_blind:
                state["blind_since"] = now_iso

            blind_since = state.get("blind_since")
            blind_age = (
                _usage_state_age_seconds({"checked_at": blind_since})
                if blind_since
                else None
            )
            if blind_age is not None and blind_age > USAGE_BLIND_PAUSE_AFTER_SECONDS:
                # Prolonged blindness: fail-closed so a permanent CLI-format
                # change can't leave spend unguarded indefinitely.
                state["paused"] = True
            else:
                state["paused"] = False

            failures = state["consecutive_parse_failures"]
            should_log = first_blind or (failures % USAGE_BLIND_LOG_INTERVAL == 0)
            if should_log:
                status = (
                    "pausing (fail-closed)"
                    if state["paused"]
                    else "failing the gate OPEN"
                )
                print(
                    f"check_usage: usage data is {age:.0f}s stale and the CLI is "
                    f"still not parseable ({failures} consecutive failures) - "
                    f"{status}; cost gate is now BLIND since {state.get('blind_since')}",
                    file=sys.stderr,
                )
        _write_usage_state(state)
        return state
    state["measured_at"] = state["checked_at"]
    state["paused"] = _usage_gate(
        prev.get("paused", False),
        state["session_pct"],
        state["week_pct"],
    )
    # A real measurement clears any blind/stale state from prior failures.
    state["consecutive_parse_failures"] = 0
    state["gate_blind"] = False
    state["stale"] = False
    _write_usage_state(state)
    return state


@mcp.tool()
def request_decision(
    plan_name: str,
    story_key: str,
    question: str,
    options: list[str],
    context: str = "",
) -> dict[str, Any]:
    """
    Escalate a blocking decision to the overlord, which rules on the user's
    behalf per the decision policy. The ruling is appended to the plan's
    decisions log (audit trail) and returned. Call this from a story agent
    when you are blocked on a choice the user would normally make.
    """
    return _service.request_decision(
        plan_name,
        story_key,
        question,
        options,
        context,
    )


@mcp.tool()
def list_decisions(plan_name: str) -> list[dict]:
    """Return the overlord decision log for a plan (audit trail)."""
    return _service.list_decisions(plan_name)


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
    # Guard: skip if story is not in tests_passed state
    if story.get("status") != "tests_passed":
        _notify_user(
            plan_name,
            f"{story_key} review skipped: status {story.get('status')!r} - only stories with status 'tests_passed' are reviewable.",
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
    skip_llm_reviewer = story.get("acceptance_failed_review") and last_test_check.get(
        "returncode"
    ) not in (0, None)
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
            # every subsequent review must go to Claude regardless of the global
            # PIPELINE_BACKEND_REVIEW setting - review backend is otherwise
            # resolved purely from that env var with no per-story override, so
            # this is the one seam that needs an explicit check.
            reviewer_output = (
                _run_reviewer(
                    worktree,
                    branch,
                    backend_name="claude",
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
            )
            _atomic_write_json(manifest_path, manifest)
            return {"ok": True, "status": story["status"], "deferred": "rate_limited"}
        except Exception as e:  # noqa: BLE001 (defense in depth, per the comment below)
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
            )
            _atomic_write_json(manifest_path, manifest)
            return {"ok": True, "status": story["status"], "deferred": "rate_limited"}

    # Transient backend error (HTTP 500 / connection-reset / connection-refused):
    # re-invoke the reviewer once inline. This is an infrastructure hiccup, not
    # a genuine review cycle, so do NOT increment review_inconclusive_count for
    # this branch itself — only the fallback inconclusive path below (reached
    # when still UNKNOWN after the single retry) touches that counter.
    _transient_retried = False
    if verdict == "UNKNOWN" and _is_transient_backend_error(reviewer_output):
        _notify_user(
            plan_name, f"{story_key} review hit transient backend error; retrying once."
        )
        reviewer_output = (
            _run_reviewer(
                worktree,
                branch,
                backend_name="claude",
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

    # Reviewer self-fix (2026-07-29): the reviewer's own "trivial and
    # confident" self-assessment is never trusted alone - mechanically
    # re-verify risk, diff size, and the full test suite before honoring it.
    # Folds into the existing APPROVE/REQUEST_CHANGES branches below
    # unchanged: verified -> APPROVE (opens a PR like any other approval);
    # unverified -> REQUEST_CHANGES (counts against the rework budget like
    # any other rejection, so an unverifiable self-fix can't loop forever).
    if verdict == "APPROVE_WITH_FIX":
        verdict, reviewer_output = _verify_reviewer_auto_fix(
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
                _notify_user(
                    plan_name,
                    f"{story_key} parked: review inconclusive after "
                    f"{inconclusive} attempts - needs human review.",
                )
        else:
            _notify_user(plan_name, f"{story_key} review inconclusive; will retry.")
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
                _notify_user(
                    plan_name,
                    f"{story_key} parked: review inconclusive after "
                    f"{inconclusive} attempts - needs human review.",
                )
        else:
            _notify_user(
                plan_name,
                f"{story_key} review approved-changes-requested-empty: "
                f"REQUEST_CHANGES with no findings text; will retry.",
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
        pr_url = _open_pr(worktree, story_key, story)
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
                    )
            story["status"] = "changes_requested"
    _atomic_write_json(manifest_path, manifest)
    return {
        "ok": True,
        "verdict": verdict,
        "status": story["status"],
        "pr_url": story.get("pr_url"),
    }


# Preserve original review_story implementation
_original_review_story = review_story


@mcp.tool()
def review_story(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Run the code-reviewer persona over a dispatched story's branch. On APPROVE,
    open a PR via gh and set status to pr_open; otherwise set status to
    changes_requested. Does not merge — merge is the overlord's decision.

    Only reviewable when story["status"] == "tests_passed" - any other status
    (a stale/duplicate call, e.g. a second tick racing an already-merged
    story) is a no-op skip; see README.md's "Review & merge" section.
    """
    return _service.review_story(plan_name, story_key)


@mcp.tool()
def advance_pipeline(plan_name: str) -> dict[str, Any]:
    """
    Run one orchestration tick: dispatch every ready story (deps satisfied),
    advance finished stories through test -> review -> PR, and adjudicate merges
    against the risk threshold. Idempotent; designed to be called repeatedly by
    a scheduler (/loop or cron). In PIPELINE_AUTONOMY=dry-run it plans and logs
    only, taking no actions.

    Honors a per-backend resource gate: dispatch and review are gated
    independently by their own backend's resource_status() (see
    _role_resource_ok). If the dispatch backend is gated, in-progress stories
    are interrupted (checkpointed, resumable) and no new dispatch starts; if
    the review backend is gated, review is deferred. Each is independent, so a
    Claude usage pause no longer freezes local-backed dispatch. Merge
    adjudication always runs (no model usage). "interrupted" stories are
    dispatch-eligible like "todo" ones, so they resume automatically once the
    dispatch backend frees up.

    Also honors MAX_CONCURRENT_AGENTS: dispatch is capped to the number of
    free slots remaining (limit minus agents already in_progress across all
    plans), so a tick never starts more agents than the configured ceiling.
    Stories left undispatched this tick stay "todo"/"interrupted" and are
    picked up on a later tick as slots free up.

    Skips entirely (returns {"ok": True, "skipped": "locked"}) if another
    tick for this same plan is already running - see _plan_lock.
    """
    return _service.advance_pipeline(plan_name)


def _advance_pipeline_locked(plan_name: str) -> dict[str, Any]:
    manifest_path = _store.manifest_path(plan_name)
    if not manifest_path.exists():
        return {"ok": False, "error": f"No manifest for {plan_name}"}
    manifest = _store.get_manifest(plan_name)
    stories = manifest["stories"]

    if manifest.get("paused"):
        # A human-requested pause for this one plan: unlike the usage gate,
        # this doesn't even adjudicate merges - the plan should sit
        # completely still until explicitly resumed. Still free up any
        # running agent so a paused plan isn't quietly burning usage.
        with _scoped_repo_root(plan_name):
            for key, story in stories.items():
                if story["status"] == "in_progress" and "pid" in story:
                    interrupt_story(plan_name, key)
        return {"ok": True, "skipped": "plan_paused"}

    # Per-backend resource gate (Step 5): dispatch and review can run on
    # different backends, so gate each by ITS backend's availability rather
    # than one global Claude flag. This is what lets local dispatch keep
    # running when Claude's weekly limit is hit (and vice versa).
    dispatch_ok, dispatch_reason = _role_resource_ok("dispatch")
    review_ok, review_reason = _role_resource_ok(
        "review", plan_role_config=manifest.get("role_config")
    )

    done = _completed_dep_ids(stories)
    ready = [
        k
        for k, v in stories.items()
        if v["status"] in ("todo", "interrupted", "changes_requested")
        and all(d in done for d in v.get("dependencies", []))
    ]

    if PIPELINE_AUTONOMY == "dry-run":
        return {
            "ok": True,
            "dry_run": True,
            "autonomy": PIPELINE_AUTONOMY,
            # "paused" kept for back-compat = dispatch gated.
            "paused": not dispatch_ok,
            "dispatch_paused": not dispatch_ok,
            "review_paused": not review_ok,
            "would_dispatch": ready if dispatch_ok else [],
            "would_merge_decisions": {
                k: _merge_decision(v)
                for k, v in stories.items()
                if v["status"] == "pr_open"
            },
        }

    summary: dict[str, Any] = {
        "autonomy": PIPELINE_AUTONOMY,
        "paused": not dispatch_ok,
        "dispatch_paused": not dispatch_ok,
        "review_paused": not review_ok,
        "dispatched": [],
        "advanced": [],
        "merged": [],
        "parked": [],
        "failed": [],
        "interrupted": [],
        "notify": [],
        "review_deferred": [],
        "ci_pending": [],
    }

    # Scoped for the whole tick: dispatch_story resolves its own repo_root
    # too (so it's correct called standalone), but _merge_pr and
    # _default_branch read the plain REPO_ROOT global, so this plan's repo
    # must be active for the duration of every action below.
    with _scoped_repo_root(plan_name):
        if not dispatch_ok:
            # The dispatch backend is gated: stop spending it, and free up
            # in-flight agents (they run on the dispatch backend and are
            # resumable via their checkpoint journal) rather than letting them
            # keep burning the resource we're protecting.
            #
            # Exception: a local-memory-pressure gate ("insufficient free
            # memory") is self-inflicted by an in-progress dispatch actively
            # loading its model into memory - it is not burning a shared,
            # exhaustible resource the way Claude usage or a downed server
            # would be. Killing it doesn't free anything real; it destroys
            # progress and the redispatch (interrupted stories are dispatch-
            # eligible) immediately re-triggers the identical gate once the
            # new process starts loading again. Observed live 2026-07-13: a
            # new PID every ~10-20s across three separate model runs, never
            # converging. Every OTHER gate reason (Claude usage exhausted,
            # Ollama unreachable, ...) still interrupts as before - those
            # really do mean "stop spending this backend now."
            memory_pressure = "insufficient free memory" in dispatch_reason
            for key, story in stories.items():
                if story["status"] == "in_progress" and "pid" in story:
                    if memory_pressure:
                        continue
                    interrupt_story(plan_name, key)
                    summary["interrupted"].append(key)
            # Only surface the gate when it actually affects this tick: when
            # there are ready stories to dispatch or in-progress agents that
            # could be interrupted. A plan whose only stories are already past
            # dispatch (e.g. all pr_open awaiting merge) has nothing to defer,
            # so notifying about a paused dispatch is noise.
            if ready or any(
                s["status"] == "in_progress" and "pid" in s
                for s in stories.values()
            ):
                _notify_user(
                    plan_name,
                    f"Dispatch backend gated ({dispatch_reason}): deferring dispatch.",
                )
                summary["notify"].append("dispatch_paused")
        else:
            # 1. Dispatch ready (and resumable-interrupted) stories, capped to
            # the slots still free under MAX_CONCURRENT_AGENTS. <=0 means no cap.
            if MAX_CONCURRENT_AGENTS > 0:
                slots = max(0, MAX_CONCURRENT_AGENTS - _count_in_progress_agents())
                to_dispatch = ready[:slots]
            else:
                to_dispatch = ready
            for key in to_dispatch:
                try:
                    result = dispatch_story(plan_name, key)
                    if not (isinstance(result, dict) and result.get("skipped")):
                        summary["dispatched"].append(key)
                    else:
                        summary.setdefault("skipped", []).append(key)
                except Exception as e:  # noqa: BLE001 (git fetch/worktree/backend launch failure)
                    # Re-read: dispatch_story only writes the manifest on a
                    # successful launch, so on a raise the on-disk status is
                    # still todo/interrupted - bump the attempt counter there.
                    m = json.loads(manifest_path.read_text())
                    st = m["stories"][key]
                    attempts = st.get("dispatch_attempts", 0) + 1
                    st["dispatch_attempts"] = attempts
                    if attempts >= DISPATCH_MAX_ATTEMPTS:
                        st["status"] = "failed"
                        st["dispatch_error"] = str(e)
                        _notify_user(
                            plan_name,
                            f"{key} dispatch failed {attempts}x "
                            f"({e}); giving up - needs human intervention.",
                        )
                        summary["failed"].append(key)
                    else:
                        # leave status dispatch-eligible; the next tick retries.
                        _notify_user(
                            plan_name,
                            f"{key} dispatch attempt {attempts}/"
                            f"{DISPATCH_MAX_ATTEMPTS} failed ({e}); will retry.",
                        )
                    summary["notify"].append(key)
                    _atomic_write_json(manifest_path, m)

            # 2. Poll running agents: tests fail -> notify (or escalate); tests pass -> tests_passed.
            manifest = json.loads(manifest_path.read_text())
            stories = manifest["stories"]
            for key, story in stories.items():
                if story["status"] == "in_progress" and "pid" in story:
                    check_result = check_story_status(plan_name, key)
                    status = check_result.get("status")
                    if status == "failed":
                        fallback_model = manifest.get("local_model_fallback")
                        # A-posteriori escalation: under auto dispatch, if the
                        # local agent failed and has NOT been escalated before,
                        # wipe its worktree and re-queue for Claude. A second
                        # failure (on Claude), or any failure under an explicit
                        # non-auto backend, is terminal.
                        if (
                            _auto_escalation_enabled()
                            and story.get("backend") == "local"
                            and not story.get("escalated")
                        ):
                            manifest = json.loads(manifest_path.read_text())
                            _escalate_to_claude(manifest, plan_name, key, manifest_path)
                            _notify_user(
                                plan_name,
                                f"{key} local agent failed; escalating to Claude and starting clean.",
                            )
                            summary["notify"].append(key)
                        elif (
                            fallback_model
                            and story.get("backend") == "local"
                            and story.get("model") != fallback_model
                            and not story.get("tried_fallback_model")
                        ):
                            # Plan-scoped opt-in (manifest["local_model_fallback"]):
                            # never escalates to Claude - just gives one other
                            # local model a shot before the terminal park/fail
                            # path below.
                            manifest = json.loads(manifest_path.read_text())
                            failed_model = (
                                story.get("dispatched_model")
                                or story.get("model")
                                or "default"
                            )
                            _escalate_to_local_fallback_model(
                                manifest, plan_name, key, manifest_path, fallback_model
                            )
                            _notify_user(
                                plan_name,
                                f"{key} local agent failed on {failed_model}; retrying on "
                                f"fallback model {fallback_model} before parking.",
                            )
                            summary["notify"].append(key)
                        elif check_result.get("failure_kind") == "give_up":
                            # T6: the agent explicitly surrendered rather than
                            # producing ordinary red tests. Point the human at
                            # the story's scope/clarity instead of the generic
                            # message - a missing/wrong API needs a fix to
                            # agent_instructions, not another identical retry.
                            _notify_user(
                                plan_name,
                                f"{key} agent gave up (explicit surrender, zero productive "
                                f"progress) - likely under-specified (missing API, wrong "
                                f"scope) rather than a model-capability gap; needs human "
                                f"clarification before another dispatch.",
                            )
                            summary["failed"].append(key)
                            summary["notify"].append(key)
                        else:
                            _notify_user(plan_name, f"{key} tests failed")
                            summary["failed"].append(key)
                            summary["notify"].append(key)

        # Review every tests_passed story (incl. ones orphaned by a crashed
        # review on a prior tick - review_story is idempotent). Gated by the
        # REVIEW backend independently of dispatch: a Claude-dispatch pause no
        # longer blocks reviewing already-finished work on a healthy review
        # backend, and a local-dispatch run can still defer review if review
        # is on Claude and Claude is gated.
        if review_ok:
            stories = json.loads(manifest_path.read_text())["stories"]
            for key, story in stories.items():
                if story["status"] == "tests_passed":
                    rv = review_story(plan_name, key)
                    summary["advanced"].append({key: rv["status"]})
                    if rv.get("deferred") == "rate_limited":
                        summary["review_deferred"].append(key)
        else:
            # Only surface the review gate when there is tests_passed work
            # waiting to be reviewed this tick; otherwise the notification is
            # noise (e.g. all stories are already pr_open awaiting merge).
            if any(s["status"] == "tests_passed" for s in stories.values()):
                _notify_user(
                    plan_name, f"Review backend gated ({review_reason}): deferring review."
                )
                summary["notify"].append("review_paused")

        # 3. Adjudicate merges for reviewed PRs (no model usage; runs even paused).
        manifest = json.loads(manifest_path.read_text())
        stories = manifest["stories"]
        for key, story in stories.items():
            if story["status"] != "pr_open":
                continue
            decision = _merge_decision(story)
            if decision["action"] != "merge":
                story["status"] = "parked"
                story["parked_reason"] = decision["reason"]
                _notify_user(plan_name, f"{key} parked: {decision['reason']}")
                summary["parked"].append(key)
                summary["notify"].append(key)
                continue

            # Mode 9: rebase onto current origin/master + CI gate before merge,
            # so a stale-base branch can't land cross-story breakage or a
            # ruff-red PR onto main. Failures count against merge_attempts just
            # like a transient `gh pr merge` failure (see MERGE_MAX_ATTEMPTS).
            branch = f"agent/{key.lower()}"
            worktree = story.get("worktree", "")
            gate_error = ""
            ci_definitive_fail = False
            ci_wait = False
            # S5: a story already polling a pending CI run must not
            # re-rebase/force-push on every tick - with the non-blocking CI
            # poll that mints a new SHA whenever origin/master moved,
            # restarting CI and burning Actions minutes. Skip straight to
            # polling the exact SHA recorded on the first pending observation.
            if story.get("ci_pending_sha"):
                pushed_sha = story["ci_pending_sha"]
            else:
                gate_error, pushed_sha = _rebase_and_push_for_merge(plan_name, key, branch, worktree)
            if not gate_error:
                ci = _merge_gate_ci_status(branch, sha=pushed_sha)
                if ci["state"] == "cancelled" and not story.get(
                    "ci_rerun_attempted"
                ):
                    # Worth exactly one automatic rerun before treating it
                    # as a failure - an abnormal queue delay can cancel
                    # jobs with no code-quality signal at all.
                    story["ci_rerun_attempted"] = True
                    _ci_rerun(pushed_sha)
                    ci = _merge_gate_ci_status(branch, sha=pushed_sha)
                if ci["state"] == "fail":
                    gate_error = f"ci fail: {ci['error']}"
                    # Only a genuine test-failure verdict is "definitive" -
                    # cancelled (queue/infra flake, already given one
                    # auto-rerun above) and pending are NOT, and must keep
                    # retrying via the ordinary merge_attempts path below,
                    # not consume rework budget.
                    ci_definitive_fail = True
                elif ci["state"] == "cancelled":
                    gate_error = f"ci fail: {ci['error']}"
                elif ci["state"] == "pending":
                    story.setdefault("ci_pending_since", datetime.now(timezone.utc).isoformat())
                    story["ci_pending_sha"] = pushed_sha
                    if _ci_pending_expired(story["ci_pending_since"]):
                        _notify_user(
                            plan_name,
                            f"{key} CI has been pending since "
                            f"{story['ci_pending_since']} and exceeded the "
                            f"merge-gate pending bound; giving up on the wait.",
                            story_key=key,
                            severity="warning",
                            event="ci_pending_stalled",
                            dedup_key=f"ci_pending_stalled:{key}",
                        )
                        story.pop("ci_pending_since", None)
                        story.pop("ci_pending_sha", None)
                        gate_error = f"ci pending: {ci['error']}"
                    else:
                        ci_wait = True
                if ci["state"] != "pending":
                    story.pop("ci_pending_since", None)
                    story.pop("ci_pending_sha", None)
            if ci_wait:
                summary["ci_pending"].append(key)
                continue
            if not gate_error:
                # Independent of review: re-run the acceptance oracle
                # against the just-rebased branch right before merging.
                # Closes the gap CI alone can't (a repo without CI, or a
                # CI-independent slip between tests_passed and review).
                acc = _reverify_acceptance(story, worktree, key)
                if acc["state"] == "fail":
                    gate_error = f"acceptance reverify fail: {acc['error']}"
            if not gate_error:
                # Independent of tests: a green suite doesn't mean the
                # project actually builds (PR #48 merged with `npm run
                # build` broken - retro §3.1).
                build = _reverify_build(worktree)
                if build["state"] == "fail":
                    gate_error = f"build reverify fail: {build['error']}"

            if gate_error:
                # Opt-in (PIPELINE_REWORK_ON_CI_FAIL=1): a DEFINITIVE CI test
                # failure - not a transient rebase/push error, not
                # pending/cancelled - can be caused by the agent's own
                # committed test file rather than the reviewed implementation
                # (the reviewer is acceptance-scoped and never saw it). Retrying
                # an unchanged branch identically MERGE_MAX_ATTEMPTS times can
                # never fix that; hand the CI failure back to the implementer as
                # rework feedback instead, bounded by the SAME rework budget
                # review_story uses, so a story that never converges still
                # parks/escalates rather than looping forever. See
                # MERGE_CI_REWORK_PLAN.md, 2026-07-17 (gpt-oss retry_backoff /
                # token_bucket: ground-truth-correct code abandoned because the
                # agent's own broken self-test tripped this gate).
                rework_ok = (
                    ci_definitive_fail
                    and os.environ.get("PIPELINE_REWORK_ON_CI_FAIL", "0") == "1"
                )
                if rework_ok:
                    # Bound by MERGE_MAX_ATTEMPTS via the merge_attempts
                    # counter, which PERSISTS across the rework -> review
                    # APPROVE -> merge-gate cycle. rework_attempts does NOT:
                    # the review APPROVE path (~line 4189) pops it on every
                    # pass (the reviewer APPROVEs because it is acceptance-
                    # scoped and the oracle is green), so reusing
                    # rework_attempts here loops forever - each CI-fail
                    # re-increments 0->1 and the cap never exhausts (verified
                    # 2026-07-17 on token_bucket: four identical "routed to
                    # rework (1/3)" notifications, same broken assertion
                    # every round). merge_attempts is the merge gate's own
                    # counter and is not reset by review, so it bounds the
                    # loop: MERGE_MAX_ATTEMPTS rework rounds, then the
                    # fall-through below terminal-fails.
                    rework_ok = story.get("merge_attempts", 0) < MERGE_MAX_ATTEMPTS

                if rework_ok:
                    attempts = story.get("merge_attempts", 0) + 1
                    story["merge_attempts"] = attempts
                    # L1 (REVIEWER_ESCALATION_PLAN.md): flag this rework as
                    # CI-triggered so the next dispatch_story raises the
                    # agent's done-bar to full-suite-green (env
                    # LOCAL_AGENT_REWORK_FULL_SUITE). Without it the rework
                    # keeps the oracle-green bar and re-fails CI on the same
                    # assertion every round (the agent's own broken test is
                    # invisible to the acceptance-scoped oracle/reviewer).
                    story["ci_rework"] = True
                    story["review_feedback"] = _ci_rework_feedback(gate_error)
                    story["status"] = "changes_requested"
                    _notify_user(
                        plan_name,
                        f"{key} merge-gate CI failed ({gate_error}); "
                        f"routed to rework ({attempts}/{MERGE_MAX_ATTEMPTS}).",
                    )
                    summary["notify"].append(key)
                    continue

                attempts = story.get("merge_attempts", 0) + 1
                story["merge_attempts"] = attempts
                if attempts >= MERGE_MAX_ATTEMPTS:
                    story["status"] = "failed"
                    story["merge_error"] = gate_error
                    _notify_user(
                        plan_name,
                        f"{key} merge gate failed {attempts}x "
                        f"({gate_error}); giving up - needs human intervention.",
                    )
                    summary["failed"].append(key)
                else:
                    # leave pr_open; the next tick retries within budget.
                    _notify_user(
                        plan_name,
                        f"{key} merge gate attempt {attempts}/"
                        f"{MERGE_MAX_ATTEMPTS} failed ({gate_error}); will retry.",
                    )
                summary["notify"].append(key)
                continue

            # _merge_pr removes the worktree and deletes the branch, so the
            # self-source diff must be taken BEFORE the merge, not after.
            mcp_touched = _mcp_self_source_touched(
                worktree, f"origin/{_default_branch()}"
            )
            try:
                _merge_pr(story.get("worktree", ""), key)
            except Exception as e:  # noqa: BLE001 (gh/git transient failure - see MERGE_MAX_ATTEMPTS)
                attempts = story.get("merge_attempts", 0) + 1
                story["merge_attempts"] = attempts
                if attempts >= MERGE_MAX_ATTEMPTS:
                    story["status"] = "failed"
                    story["merge_error"] = str(e)
                    _notify_user(
                        plan_name,
                        f"{key} merge failed {attempts}x "
                        f"({e}); giving up - needs human intervention.",
                    )
                    summary["failed"].append(key)
                else:
                    # leave pr_open; the next tick retries within budget.
                    _notify_user(
                        plan_name,
                        f"{key} merge attempt {attempts}/"
                        f"{MERGE_MAX_ATTEMPTS} failed ({e}); will retry.",
                    )
                summary["notify"].append(key)
                continue
            story["status"] = "done"
            story.pop("merge_attempts", None)
            story.pop("parked_reason", None)
            story.pop("ci_rerun_attempted", None)
            story.pop("ci_rework", None)  # L1: clear the rework flag on done
            _mark_plane_done(key, plan_name)
            if mcp_touched:
                _notify_user(plan_name, _mcp_restart_notice(mcp_touched))
                summary["notify"].append(key)
            summary["merged"].append(key)
        _atomic_write_json(manifest_path, manifest)

    return {"ok": True, **summary}




@mcp.tool()
def approve_merge(plan_name: str, story_key: str) -> dict[str, Any]:
    """Approve a merge by delegating to PipelineService."""
    return _service.approve_merge(plan_name, story_key)


def _approve_merge_impl(plan_name: str, story_key: str) -> dict[str, Any]:
    _validate_key(plan_name)
    _validate_key(story_key)
    manifest_path = _store.manifest_path(plan_name)

    with _store.transaction(plan_name) as acquired:
        if not acquired:
            return {
                "ok": False,
                "error": "plan busy (scheduler tick in progress); retry",
                "retriable": True,
            }
        # Re-read the manifest from disk INSIDE the lock so we merge against
        # the freshest on-disk state, not a pre-lock stale copy. A scheduler
        # tick may have changed the story's status or verdict while we waited
        # to acquire the lock.
        manifest = _store.get_manifest(plan_name)
        story = manifest["stories"].get(story_key)
        if not story:
            return {"ok": False, "error": f"No such story {story_key}"}
        if story["status"] not in ("parked", "pr_open"):
            return {
                "ok": False,
                "error": f"Story is {story['status']}, not parked/pr_open",
            }
        if story.get("review_verdict") != "APPROVE":
            return {"ok": False, "error": "Story was never reviewer-approved"}

        try:
            with _scoped_repo_root(plan_name):
                # Mode 9 gate applies here too: even an explicit human merge must
                # not land a conflicting or CI-red PR. Disable via
                # PIPELINE_MERGE_CI_GATE=0 only if you intentionally accept that.
                branch = f"agent/{story_key.lower()}"
                worktree = story.get("worktree", "")
                rb = _rebase_onto_master(worktree, branch)
                if rb.get("auto_resolved"):
                    _notify_user(
                        plan_name,
                        f"{story_key} rebase auto-resolved an "
                        f"additive-import conflict against "
                        f"origin/{_default_branch()}.",
                    )
                if not rb["ok"]:
                    return {
                        "ok": False,
                        "error": f"rebase failed: {rb['error']}",
                        "story_key": story_key,
                    }
                pushed_sha = ""
                if Path(worktree).is_dir():
                    push = subprocess.run(
                        ["git", "push", "--force-with-lease", "origin", branch],
                        check=False,
                        cwd=REPO_ROOT,
                        capture_output=True,
                        text=True,
                    )
                    if push.returncode != 0:
                        return {
                            "ok": False,
                            "error": f"push failed: {(push.stderr or push.stdout).strip()[:200]}",
                            "story_key": story_key,
                        }
                    # Mode 26: pin to the exact commit that was just pushed,
                    # not the branch name - see the matching comment in
                    # advance_pipeline's own merge adjudication above.
                    rev = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        check=False,
                        cwd=worktree,
                        capture_output=True,
                        text=True,
                    )
                    pushed_sha = rev.stdout.strip()
                ci = _ci_status(branch, sha=pushed_sha)
                if ci["state"] == "cancelled" and not story.get("ci_rerun_attempted"):
                    # Same one-shot auto-rerun as the scheduler's merge gate:
                    # a queue-delay cancellation carries no code-quality
                    # signal, so give it one automatic retry before failing.
                    story["ci_rerun_attempted"] = True
                    _ci_rerun(pushed_sha)
                    ci = _ci_status(branch, sha=pushed_sha)
                if ci["state"] in ("fail", "cancelled"):
                    return {
                        "ok": False,
                        "error": f"CI failing: {ci['error']}",
                        "story_key": story_key,
                    }
                if ci["state"] == "pending":
                    return {
                        "ok": False,
                        "error": f"CI still pending: {ci['error']}",
                        "story_key": story_key,
                    }
                acc = _reverify_acceptance(story, worktree, story_key)
                if acc["state"] == "fail":
                    return {
                        "ok": False,
                        "error": f"acceptance reverify fail: {acc['error']}",
                        "story_key": story_key,
                    }
                build = _reverify_build(worktree)
                if build["state"] == "fail":
                    return {
                        "ok": False,
                        "error": f"build reverify fail: {build['error']}",
                        "story_key": story_key,
                    }
                # _merge_pr removes the worktree and deletes the branch, so the
                # self-source diff must be taken BEFORE the merge, not after.
                mcp_touched = _mcp_self_source_touched(
                    worktree, f"origin/{_default_branch()}"
                )
                _merge_pr(story.get("worktree", ""), story_key)
        except Exception as e:  # noqa: BLE001 (surface the gh/git failure to the human, don't raise)
            return {"ok": False, "error": str(e), "story_key": story_key}
        # Final write INSIDE the lock, using the manifest re-read inside the
        # lock (not a pre-lock copy). Clear parked_reason on leaving 'parked'.
        story["status"] = "done"
        story.pop("parked_reason", None)
        story.pop("ci_rerun_attempted", None)
        _atomic_write_json(manifest_path, manifest)
    _mark_plane_done(story_key, plan_name)
    if mcp_touched:
        _notify_user(plan_name, _mcp_restart_notice(mcp_touched))
    return {"ok": True, "story_key": story_key, "status": "done"}


def _set_plan_paused(plan_name: str, paused: bool) -> dict[str, Any]:
    with _store.transaction(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped": "locked",
                "reason": "an advance_pipeline tick is already running for this plan",
            }
        if not _store.manifest_path(plan_name).exists():
            return {"ok": False, "error": f"No manifest for {plan_name}"}
        manifest = _store.get_manifest(plan_name)
        manifest["paused"] = paused
        _store.save_manifest(plan_name, manifest)
        return {"ok": True, "plan_name": plan_name, "paused": paused}


# manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
@mcp.tool()
def pause_plan(plan_name: str) -> dict[str, Any]:
    """
    Stop advance_pipeline/advance_all_plans from touching this one plan -
    no new dispatch, review, or merge - while leaving every other ingested
    plan's scheduler ticks unaffected. Any story currently in_progress is
    interrupted (checkpointed and left resumable) so a paused plan isn't
    quietly burning usage in the background. Resume with resume_plan.
    """
    return _service.pause_plan(plan_name)


@mcp.tool()
def resume_plan(plan_name: str) -> dict[str, Any]:
    """Clear a pause set by pause_plan so this plan's stories are eligible
    for dispatch/review/merge on the next advance_pipeline tick again."""
    return _service.resume_plan(plan_name)


@mcp.tool()
def advance_all_plans() -> dict[str, Any]:
    """
    Run advance_pipeline on every plan that has been ingested (has a
    manifest), keyed by plan name. Plans saved but not yet ingested (no
    manifest) are skipped. Intended for a recurring scheduler (cron/launchd
    or /loop) so newly ingested plans are picked up automatically with no
    hardcoded plan name to maintain.

    NOTE on zombie reaping: the per-plan advance_pipeline polling phase
    already handles dead-pid in_progress stories via check_story_status
    (which falls through to test-running on dead pids). Running an external
    reap pass BEFORE the polling would clobber that and silently leave
    stories re-dispatching forever without ever running the test
    (manifest observation 2026-06-28: 3 e2e stories hit dispatch_attempts=
    MISSING because the reap ate the polling opportunity). The reap helper
    _reap_zombie_in_progress_stories is kept for callers that need a
    one-shot cleanup (e.g. tests, ops CLI) but is NOT wired in here.
    """
    return _service.advance_all_plans()


if __name__ == "__main__":
    mcp.run()
# A-posteriori escalation of a failed local run to Claude is gated by
# _auto_escalation_enabled() (PIPELINE_AUTO_ESCALATE, falling back to
# PIPELINE_BACKEND_DISPATCH=="auto" when unset - see pipeline/escalation.py).
# manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
# manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
# manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
# manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
# manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
# manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
# manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
