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
import re
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app import backend, role_registry

from . import config_provenance
from .build_detect import (  # noqa: F401
    _acceptance_rel_paths,
    _added_pytest_test_paths,
    _build_command_for,
    _is_pytest_cmd,
    _isolation_only_acceptance_warning,
    _last_done_summary,
    _module_level_function_names,
    _platform_locked_fixture_warning,
    _provision_worktree_venv,
    _run_lint_gate,
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
    _checkpoint_impl,  # noqa: F401 (re-exported for pipeline.service free vars)
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
    _PATCHABLE_STORY_FIELDS,
    _VALID_STORY_STATUSES,
    PIPELINE_MERGE_BUILD_GATE,
    PIPELINE_MERGE_CI_GATE,
    PIPELINE_MERGE_CI_TIMEOUT,
    _acceptance_tampered,
    _ci_pending_expired,
    _ci_rerun,
    _ci_status,
    _ci_status_once,
    _get_effective_config_impl,
    _mark_story_done_impl,
    _parse_pytest_excerpt,
    _record_retro_pending,
    _repo_has_ci_configured,
    _reverify_acceptance,
    _reverify_build,
)

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
    REWORK_MAX_ATTEMPTS_NO_COMMIT,
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
    _escalation_label,
    _escalation_target,
)
from .git_ops import (
    _commit_wip,
    _last_nonempty_line,
    _test_files_added_on_branch,
    _test_names_in_file,
    _worktree_has_new_commits,
    _worktree_has_non_wip_commits,  # noqa: F401 (re-exported for test patching)
)

# Merge adjudication helpers (moved to pipeline/merge.py). Re-exported so
# tests that monkeypatch pipeline.server.<name> still resolve.
from .merge import (  # noqa: F401
    _approve_merge_impl,
    _merge_decision,
    _merge_gate_ci_status,
    _rebase_and_push_for_merge,
    _set_plan_paused,
    _try_acquire_git_lock,
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
    _invoke_overlord,  # noqa: F401 (re-exported for pipeline.service free vars)
    _load_policy,  # noqa: F401 (re-exported for pipeline.service free vars)
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
from .persistence import (
    _append_decision,  # noqa: F401  (re-exported for pipeline.store free vars)
    _append_journal,  # noqa: F401  (re-exported for pipeline.store free vars)
    _decisions_path,  # noqa: F401 (re-exported for pipeline.service free vars)
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
    _format_review_comment,
    _merge_pr,
    _open_pr,
    _post_pr_comment,
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
from .store import FileStore, Store, _TransactionLock  # noqa: F401

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
from .triage import run_triage_sweep

# Usage probe / dispatch routing. Tests patch pipeline_usage.<name> for the
# threshold constants and USAGE_STATE_PATH (Option B); the autouse
# _isolate_usage_state fixture patches both p.USAGE_STATE_PATH and
# pipeline_usage.USAGE_STATE_PATH.
# Usage probe / dispatch routing. Tests patch pipeline_usage.<name> for the
# threshold constants and USAGE_STATE_PATH (Option B); the autouse
# _isolate_usage_state fixture patches both p.USAGE_STATE_PATH and
# pipeline_usage.USAGE_STATE_PATH.
# Usage probe / dispatch routing. Tests patch pipeline_usage.<name> for the
# threshold constants and USAGE_STATE_PATH (Option B); the autouse
# _isolate_usage_state fixture patches both p.USAGE_STATE_PATH and
# pipeline_usage.USAGE_STATE_PATH.
from .usage import (  # noqa: F401
    _check_usage_impl,
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


# ---------- Review / PR helpers ----------


# ---------- Merge adjudication / notifications ----------


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









_store = FileStore()

from pipeline.service import PipelineService

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


from pipeline.ingest import _ingest_plan_impl


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


from pipeline.story_status import check_story_status


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


# The documented allowlist of valid story `backend` values
# (see pipeline-story-schema.md). patch_story validates against this BEFORE
# writing so an invalid value fails closed and leaves the manifest unchanged.
_VALID_STORY_BACKENDS = frozenset(
    {"claude", "local", "ollama", "lmstudio", "mlx", "auto"}
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


from pipeline.review_orchestrator import (  # verbatim move; re-export for monkeypatch compatibility
    _original_review_story,
    _verify_reviewer_auto_fix,
)


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


from pipeline.advance import _advance_pipeline_locked, _advance_pipeline_locked_impl


@mcp.tool()
def approve_merge(plan_name: str, story_key: str) -> dict[str, Any]:
    """Approve a merge by delegating to PipelineService."""
    return _service.approve_merge(plan_name, story_key)




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


def _ci_rework_feedback(gate_error: str, attempts: int) -> str:
    """Generate review feedback for merge-gate CI failures.

    Thin delegating wrapper over :func:`pipeline.ci._ci_rework_feedback`
    (the implementation lives in ``pipeline/ci.py``). Kept here so the
    ``pipeline.server`` binding stays the monkeypatch target the test suite
    patches, and so the call site in ``pipeline/advance.py`` (which reads
    this name via ``_ServerRef``) resolves to the live server binding.
    """
    from .ci import _ci_rework_feedback as _impl

    return _impl(gate_error, attempts)


if __name__ == "__main__":
    mcp.run()
# A-posteriori escalation of a failed local run to Claude is gated by
# _auto_escalation_enabled() (PIPELINE_AUTO_ESCALATE, falling back to
# PIPELINE_BACKEND_DISPATCH=="auto" when unset - see pipeline/escalation.py).
from pipeline.dispatch import (  # noqa: F401
    _dispatch_story_impl,
    _find_dead_new_functions,
    _rebrief_step_cap_struggle,
    _resolve_dispatch_backend,
)
