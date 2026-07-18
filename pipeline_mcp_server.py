"""
Pipeline MCP Server
Exposes tools for: planning, Plane ingestion, agent dispatch, status monitoring.

Run with: python pipeline_mcp_server.py
Register globally: claude mcp add -s user pipeline ~/.claude/mcp-servers/pipeline/.venv/bin/python3 ~/.claude/mcp-servers/pipeline/pipeline_mcp_server.py

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

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

import backend
import role_registry

# ---------- Config ----------
# Path constants + the PLAN_DIR/WORKTREE_ROOT mkdir live in pipeline_paths.
# PLANE_* constants live in pipeline_ticketing with the provider code.
# Scalar env-var-driven knobs (rework budgets, dispatch/merge caps, step-cap
# markers, risk orderings, local-backend name sets) live in pipeline_config.
from pipeline_config import (  # noqa: F401
    PIPELINE_AUTONOMY,
    PIPELINE_RISK_THRESHOLD,
    _RISK_ORDER,
    DEFAULT_MODEL,
    SESSION_PAUSE_THRESHOLD,
    SESSION_RESUME_THRESHOLD,
    WEEK_PAUSE_THRESHOLD,
    WEEK_RESUME_THRESHOLD,
    USAGE_STALE_AFTER_SECONDS,
    DAILY_REQUEST_THRESHOLD,
    WEEKLY_REQUEST_THRESHOLD,
    USAGE_BLIND_PAUSE_AFTER_SECONDS,
    USAGE_BLIND_LOG_INTERVAL,
    MAX_CONCURRENT_AGENTS,
    MERGE_MAX_ATTEMPTS,
    DISPATCH_MAX_ATTEMPTS,
    DISPATCH_STARTUP_GRACE_SECONDS,
    DISPATCH_WATCHDOG_SECONDS,
    STEP_CAP_MARKERS,
    STEP_CAP_FALLBACK_THRESHOLD,
    PIPELINE_LOCAL_MAX_RISK,
    _LOCAL_SKIP_PERSONAS,
    _LOCAL_BACKEND_NAMES,
    REWORK_MAX_ATTEMPTS,
    REWORK_MAX_ATTEMPTS_ORACLE,
    REWORK_MAX_ATTEMPTS_ESCALATED,
    REVIEW_INCONCLUSIVE_MAX,
)

from pipeline_paths import (  # noqa: F401
    PLAN_DIR,
    WORKTREE_ROOT,
    AGENTS_DIR,
    POLICY_PATH,
    USAGE_STATE_PATH,
    _exclude_worktree_logs_from_tracking,
)

from pipeline_build_detect import (  # noqa: F401
    _venv_python_for,
    _test_command_for,
    _build_command_for,
    detect_build_command,
    detect_test_command,
    _acceptance_rel_paths,
    _is_pytest_cmd,
    _scope_test_cmd_to_acceptance,
)

from pipeline_git_ops import (
    _last_nonempty_line,
    _commit_wip,
    _worktree_has_new_commits,
)

from pipeline_parsers import (  # noqa: F401
    _extract_json_block,
    _parse_ruling,
    _parse_verdict,
    _has_review_findings,
    _RATE_LIMIT_PATTERNS,
    _is_rate_limited,
    _TRANSIENT_BACKEND_PATTERNS,
    _is_transient_backend_error,
    _AUTO_RESOLVE_IMPORT_PATTERN,
    _parse_conflict_blocks,
    _resolve_conflict_blocks,
    _git_show_stage,
    _is_pure_additive_import_diff,
    _atomic_write_json,
    _KEY_RE,
    _validate_key,
    _completed_dep_ids,
    _GIVE_UP_PHRASES,
    _is_give_up_summary,
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
from pipeline_ticketing import (  # noqa: F401
    PLANE_BASE,
    PLANE_API_KEY,
    PLANE_WORKSPACE,
    PLANE_PROJECT,
    _plane_enabled,
    plane_request,
    _state_cache,
    _get_state,
    _label_cache,
    _UUID_RE,
    _resolve_issue_uuid,
    _get_or_create_label,
    LogicalState,
    _PLANE_STATE_GROUP,
    TicketProvider,
    NullTicketProvider,
    PlaneTicketProvider,
    JiraTicketProvider,
    _TICKET_PROVIDERS,
    get_ticket_provider,
    _plane_set_state,
    _mark_plane_done,
)

# Persistence helpers. Tests patch pipeline_persistence directly for the
# names whose moved code reads them as free vars (PLAN_DIR, _notify_user,
# _plan_role_config, ...) - see PIPELINE_MCP_DECOMPOSITION_PLAN.md §4. The
# plan_dir fixture in the test suite patches both p.PLAN_DIR and
# pipeline_persistence.PLAN_DIR so server-side reads and persistence-module
# reads both see the same temp dir.
from pipeline_persistence import (  # noqa: F401
    _notify_user,
    _decisions_path,
    _append_decision,
    _journal_path,
    _append_journal,
    _read_journal,
    _plan_role_config,
)

# Persona helpers. AGENTS_DIR is patched by the agents_dir fixture, which
# now patches both p.AGENTS_DIR and pipeline_persona.AGENTS_DIR (Option B -
# see PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).
from pipeline_persona import (  # noqa: F401
    _FRONTMATTER_RE,
    _persona_path,
    _persona_body,
    _persona_default_model,
    _PERSONA_TOOLS,
    _allowed_tools_for,
    _build_dispatch_command,
    _persona_requires_claude,
)

# Usage probe / dispatch routing. Tests patch pipeline_usage.<name> for the
# threshold constants and USAGE_STATE_PATH (Option B); the autouse
# _isolate_usage_state fixture patches both p.USAGE_STATE_PATH and
# pipeline_usage.USAGE_STATE_PATH.
from pipeline_usage import (  # noqa: F401
    _parse_usage_output,
    _run_usage_probe,
    _write_usage_state,
    _read_usage_state,
    _usage_state_age_seconds,
    _usage_gate,
    _route_dispatch_backend,
    _role_resource_ok,
)

# CI status polling for the merge gate. PIPELINE_MERGE_CI_GATE /
# PIPELINE_MERGE_CI_TIMEOUT / PIPELINE_MERGE_BUILD_GATE live here (CI-specific
# gates, not general config); tests patch pipeline_ci.<name> directly for
# the gates and p.<name> for _ci_status / _ci_rerun (server call sites use
# bare names -> re-export -> patch lands). _repo_has_ci_configured reads
# REPO_ROOT via a lazy import from this module (Option B - see
# PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).
from pipeline_ci import (  # noqa: F401
    PIPELINE_MERGE_CI_GATE,
    PIPELINE_MERGE_CI_TIMEOUT,
    PIPELINE_MERGE_BUILD_GATE,
    _repo_has_ci_configured,
    _ci_status,
    _ci_rerun,
    _reverify_acceptance,
    _reverify_build,
)

# Overlord / decision helpers. _load_policy reads POLICY_PATH / REPO_ROOT via
# lazy imports from this module (tests patch p.<name>; the lazy import sees
# the patched value). _invoke_overlord is patched via p._invoke_overlord;
# server call site (request_decision) uses the bare name -> re-export ->
# patch lands.
from pipeline_overlord import (  # noqa: F401
    _load_policy,
    _invoke_overlord,
)

# Planner / test-author dispatch helpers. All patched via p.<name> by tests;
# server call sites use bare names -> re-export -> patch lands. No server-
# global free-var reads except _run_test_author_phase's lazy import of
# _default_branch (circular-avoidance).
from pipeline_planner import (  # noqa: F401
    _PLANNER_SYSTEM,
    _PLANNER_SCRATCHPAD_CLAUSE,
    _planner_system,
    _resolve_planner_backend,
    _run_planner,
    _REWORK_PLANNER_SYSTEM,
    _run_rework_planner,
    _NEVER_TOUCH_TESTS_STEERING,
    _resolve_test_author_backend,
    _TEST_AUTHOR_SYSTEM,
    _TEST_AUTHOR_ALLOWED_TOOLS,
    _test_author_prompt,
    _wait_for_agent_exit,
    _run_test_author_phase,
    _run_decompose,
)

# Reviewer dispatch. Patched via p.<name> by tests; server call sites use
# bare names -> re-export -> patch lands. No server-global free-var reads.
from pipeline_review import (  # noqa: F401
    _run_reviewer,
    _run_security_reviewer,
)

# PR open / merge helpers. _merge_decision stays in the server (reads
# PIPELINE_AUTONOMY / PIPELINE_RISK_THRESHOLD / _RISK_ORDER, patched across
# many functions). _merge_pr reads REPO_ROOT via a lazy import from the
# server.
from pipeline_pr import (  # noqa: F401
    _open_pr,
    _merge_pr,
)

# Rebase + conflict auto-resolution. _rebase_onto_master reads REPO_ROOT /
# _default_branch via lazy imports from the server (circular-avoidance).
from pipeline_rebase import (  # noqa: F401
    _try_auto_resolve_conflict,
    _rebase_onto_master,
)

# Escalation helpers. Read REPO_ROOT / PLAN_DIR via lazy imports from the
# server (circular-avoidance).
from pipeline_escalation import (  # noqa: F401
    _escalate_to_claude,
    _escalate_to_local_fallback_model,
    _escalate_review_to_claude,
    _auto_escalation_enabled,
)

# Concurrency: slot accounting, zombie reaping, plan lock, heavy lock.
# PLAN_DIR is read as a free var; plan_dir fixture patches both p.PLAN_DIR
# and pipeline_concurrency.PLAN_DIR.
from pipeline_concurrency import (  # noqa: F401
    _count_in_progress_agents,
    _reap_zombie_in_progress_stories,
    _plan_lock,
    _plan_lock_state,
    _held_plan_locks,
    _heavy_lock,
    HEAVY_EXECUTABLES,
    _is_heavy,
)

# Checkpoint helpers. _checkpoint_impl reads PLAN_DIR via a lazy import from
# the server.
from pipeline_checkpoint import (  # noqa: F401
    _terminate_and_checkpoint,
    _checkpoint_impl,
)


REPO_ROOT = Path(os.environ.get("REPO_ROOT", ".")).resolve()

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
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
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
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
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
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    if manifest_path.exists():
        repo_root = json.loads(manifest_path.read_text()).get("repo_root")
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







# ---------- Overlord / decision helpers ----------


# GUIDED_DECOMPOSITION_PLAN.md: a "tech lead" planner call that turns a
# coarse story into an ordered sub-step checklist for the weak local
# executor to work through inside its own single worktree/transcript. This
# is deliberately NOT the story-splitting approach already tried and
# disproven (tests/benchmark/PRODUCT_ANALYST_VALIDATION_PLAN.md) - the
# checklist augments one story's prompt, it never creates new stories or
# new cold dispatches.




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

    risk_rank = _RISK_ORDER.get((story.get("risk") or "low").lower(), _RISK_ORDER["high"])
    if risk_rank >= _RISK_ORDER["high"]:
        return {"action": "park", "reason": "high risk held for human review"}
    if PIPELINE_AUTONOMY == "full":
        return {"action": "merge", "reason": "autonomy=full"}

    threshold = _RISK_ORDER.get(PIPELINE_RISK_THRESHOLD, _RISK_ORDER["low"])
    if risk_rank <= threshold:
        return {"action": "merge", "reason": f"risk <= threshold {PIPELINE_RISK_THRESHOLD}"}
    return {"action": "park", "reason": f"risk above threshold {PIPELINE_RISK_THRESHOLD}"}




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
            role, plan_role_config=plan_role_config, model_fallback=fallback,
        )
        roles[role] = {"provider": resolution.provider, "model": resolution.model}
    return {"ok": True, "roles": roles}


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


@mcp.tool()
def save_plan(plan_name: str, plan_json: str) -> dict[str, Any]:
    """
    Save a generated project plan to disk. Plan should be JSON matching the
    schema: { "epics": [ { "summary", "stories": [...] } ] }.
    Call this after generating a plan so the user can review before ingestion.
    """
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


@mcp.tool()
def list_plans() -> list[str]:
    """List saved plans available for ingestion."""
    return [p.stem for p in PLAN_DIR.glob("*.json")]


# Story fields the plan authors and that a re-ingest should refresh. Every
# other field on an already-tracked story (status, pr_url, worktree,
# review_verdict, journal, ...) is pipeline-owned runtime state and must
# survive a re-ingest untouched - see the merge behavior in ingest_plan below
# (T1, 2026-07-07 web-client-epic retro incident #2).
_INGEST_AUTHORED_STORY_FIELDS = (
    "summary", "agent_instructions", "dependencies", "persona", "model",
    "acceptance", "risk", "backend", "tdd_split",
)

# Valid story["backend"] values at ingest time: every registered driver name
# (backend._DRIVERS) plus "auto" - a valid runtime value even though it is
# not itself a driver (get_backend rejects it; _route_dispatch_backend
# resolves it to "local"/"claude" first, per PIPELINE_BACKEND_DISPATCH=auto).
_VALID_STORY_BACKENDS = frozenset(backend._DRIVERS) | {"auto"}


@mcp.tool()
def ingest_plan(
    plan_name: str, only_epics: list[str] | None = None, overwrite: bool = False,
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
    outside epics/stories/repo_root (paused, local_model_fallback, ...) carry
    over untouched. Pass overwrite=True to restore the old wholesale-replace
    behavior (drops anything not produced by this call).
    """
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
        return {"ok": False, "error": f"Plan repo_root is missing or not a directory: {repo_root!r}"}

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

    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"

    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
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
                    story["summary"], story.get("description", ""), epic_id,
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

        _atomic_write_json(manifest_path, final_manifest)

    return {"ok": True, "manifest_path": str(manifest_path), **final_manifest}




@mcp.tool()
def list_ready_stories(plan_name: str) -> list[dict]:
    """
    Return stories whose dependencies are satisfied and that are still in
    To Do. Use this to decide what to dispatch next.
    """
    _validate_key(plan_name)
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    if not manifest_path.exists():
        return []

    manifest = json.loads(manifest_path.read_text())
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
    _validate_key(plan_name)
    _validate_key(story_key)
    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "another dispatch/interrupt is in progress for this plan",
            }
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
        manifest = json.loads(manifest_path.read_text())
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
                subprocess.run(
                    ["git", "pull", "--ff-only", "origin", _default_branch()],
                    cwd=repo_root, check=True,
                )
                subprocess.run(
                    ["git", "worktree", "add", "-b", branch, str(worktree_path)],
                    cwd=repo_root, check=True,
                )
                _exclude_worktree_logs_from_tracking(Path(repo_root))

        get_ticket_provider().set_state(story_key, LogicalState.IN_PROGRESS, plan_name)

        # Resolve concrete backend name for this story. Priority order:
        #   1. story["backend"] already set (e.g. from an escalation flip)
        #   2. PIPELINE_BACKEND_DISPATCH=auto  → a-priori router
        #   3. PIPELINE_BACKEND_DISPATCH=local|claude  → that driver directly
        env_backend = os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude").strip().lower()
        dispatch_backend = story.get("backend") or (
            _route_dispatch_backend(story) if env_backend == "auto" else env_backend
        )
        # Persona-based safety override: a security persona always dispatches to
        # Claude, regardless of dispatch mode (auto/local/claude) - unless the
        # story already had an explicit backend (a prior escalation flip), which
        # wins as-is and is never re-routed here.
        if not story.get("backend") and _persona_requires_claude(story):
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
            dispatch_backend in _LOCAL_BACKEND_NAMES and review_feedback and transcript_path.exists()
        )

        spec = _build_dispatch_command(
            story, story_key, plan_name=plan_name, resume_journal=journal or None,
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
        if (dispatch_backend in _LOCAL_BACKEND_NAMES
                and MAX_CONCURRENT_AGENTS > 1
                and _count_in_progress_agents() > 0):
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
            except Exception:
                loaded = set()  # observability hook, never a gate
            if loaded and target_model and target_model not in loaded:
                msg = (
                    f"multi-model concurrent dispatch: {sorted(loaded)} already "
                    f"loaded, dispatching {story_key} on {target_model} may force "
                    f"a VRAM swap (set MAX_CONCURRENT_AGENTS=1 to silence)"
                )
                _notify_user(plan_name, msg)
                logging.getLogger("pipeline").warning(msg)

        # Fix #1: if the story carries an `acceptance` block, materialize the
        # oracle files into the worktree BEFORE the backend launches so the local
        # harness can grade against them. On a resumed story skip the write —
        # the oracle may already be in a committed WIP, and overwriting would
        # discard whatever test evolution happened mid-run.
        acceptance = story.get("acceptance") or []
        acceptance_paths = _acceptance_rel_paths(story)
        for entry in acceptance:
            target = worktree_path / entry["path"]
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(entry["source"])

        # TDD_SPLIT_PRODUCTION_PLAN.md: PIPELINE_TDD_SPLIT=on runs a
        # pre-executor test-authoring dispatch (a full agent-loop, BLOCKING
        # until it exits - unlike the planner checklist above, this
        # produces a real commit the executor's worktree must already have)
        # in THIS worktree before the main executor starts. Gated on:
        #   - the story explicitly opting in (story["tdd_split"] - §2.4:
        #     inferring eligibility from prose is a worse failure mode than
        #     an operator forgetting to opt in)
        #   - not resuming (a rework redispatch acts on the SAME tests it
        #     already has; it never gets a fresh test-authoring pass)
        #   - no existing test-author marker in the worktree (belt-and-
        #     suspenders with `resuming`, mirrors plan_path's own check
        #     below)
        # _run_test_author_phase never raises and a False return (role
        # unconfigured/refused, dispatch failure, timeout, or no commit
        # produced) falls open to today's unmodified monolithic dispatch -
        # never a gate (§2.5).
        tdd_split_mode = os.environ.get("PIPELINE_TDD_SPLIT", "off").strip().lower()
        test_author_marker = worktree_path / ".tdd_split_test_author_done"
        if (
            tdd_split_mode == "on"
            and story.get("tdd_split")
            and not resuming
            and not test_author_marker.exists()
        ):
            if _run_test_author_phase(
                story, story_key=story_key, worktree_path=worktree_path,
                dispatch_backend=dispatch_backend, local_model=spec["model"],
                plan_role_config=_plan_role_config(plan_name),
            ):
                test_author_marker.write_text("ok\n")

        # GUIDED_DECOMPOSITION_PLAN.md: PIPELINE_DECOMPOSE=cloud|local turns
        # on a "tech lead" checklist for the weak local executor. Default
        # "off" - opt-in, per Secure Defaults. Gated on:
        #   - a local-family backend (the crutch exists for the weak local
        #     executor; Claude doesn't need it)
        #   - not resuming (plan once on the story's first dispatch; a
        #     rework must never spend a second planner call)
        #   - no plan already on disk (belt-and-suspenders with `resuming`)
        # The LLM call itself is best-effort (_run_planner fails open to
        # None) so a broken/slow/rate-limited planner never blocks or
        # corrupts dispatch - the story simply proceeds with no checklist,
        # exactly like PIPELINE_DECOMPOSE=off.
        decompose_mode = os.environ.get("PIPELINE_DECOMPOSE", "off").strip().lower()
        # H3 ablation (GUIDED_DECOMPOSITION_PLAN.md §4.1's G-cloud-noscratch
        # condition): default "on" ships the persistent scratchpad; "off"
        # tests whether the checklist alone accounts for the benefit,
        # independent of cross-step memory. Read once here because it now
        # feeds BOTH the planner call (so the scratchpad becomes a first-class
        # generated step) and the trailing-instruction backstop below.
        scratchpad_on = (
            os.environ.get("PIPELINE_DECOMPOSE_SCRATCHPAD", "on").strip().lower() != "off"
        )
        plan_path = worktree_path / ".agent_plan.md"
        if (
            decompose_mode in ("cloud", "local")
            and dispatch_backend in _LOCAL_BACKEND_NAMES
            and not resuming
            and not plan_path.exists()
        ):
            plan_text = _run_planner(
                story.get("agent_instructions", ""), mode=decompose_mode,
                dispatch_backend=dispatch_backend, local_model=spec["model"],
                include_scratchpad=scratchpad_on,
                plan_role_config=_plan_role_config(plan_name),
            )
            if plan_text:
                plan_path.write_text(plan_text)
        # Referencing an existing plan is independent of generating one, so
        # a resumed dispatch that rebuilds its prompt from scratch (no
        # transcript to resume) still sees the checklist from the story's
        # first dispatch, without spending a second planner call for it.
        if plan_path.exists():
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
                    "moving on to the next step."
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
                "and committed to this branch by your tech lead. "
                f"{_NEVER_TOUCH_TESTS_STEERING} Run them to see the current "
                "failures, then implement until they pass."
            )

        dispatch_kwargs: dict[str, Any] = dict(
            prompt=spec["prompt"], system=spec["system"], model=spec["model"],
            allowed_tools=spec["allowed_tools"],
            cwd=worktree_path, log_path=log_path, append=resuming,
        )
        # Only the local driver accepts/uses `acceptance`; pass it through when
        # we're actually invoking that driver so Claude's signature stays clean.
        if dispatch_backend in _LOCAL_BACKEND_NAMES and acceptance_paths:
            dispatch_kwargs["acceptance"] = acceptance_paths
        # L1 (REVIEWER_ESCALATION_PLAN.md): a CI-triggered rework
        # (story["ci_rework"], set by the merge-CI rework router) raises the
        # agent's done-bar to full-suite-green so it cannot declare done while
        # its own broken test still fails. Local-only: the env reaches the
        # local agent subprocess; Claude's dispatch signature stays clean.
        if dispatch_backend in _LOCAL_BACKEND_NAMES and story.get("ci_rework"):
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
            if (
                decompose_mode in ("cloud", "local")
                and dispatch_backend in _LOCAL_BACKEND_NAMES
            ):
                fix_checklist = _run_rework_planner(
                    review_feedback, mode=decompose_mode,
                    dispatch_backend=dispatch_backend, local_model=spec["model"],
                    plan_role_config=_plan_role_config(plan_name),
                )
            if fix_checklist:
                dispatch_kwargs["resume_append_content"] = (
                    "The code reviewer REQUESTED CHANGES on your previous "
                    "attempt. Your tech lead has translated the feedback "
                    f"into a fix checklist:\n{fix_checklist}\n\n"
                    f"Original review feedback (for reference):\n{review_feedback}"
                )
            else:
                dispatch_kwargs["resume_append_content"] = (
                    "The code reviewer REQUESTED CHANGES on your previous attempt. "
                    f"Address this feedback:\n{review_feedback}"
                )

        handle = backend.get_backend("dispatch", name=dispatch_backend).dispatch(**dispatch_kwargs)

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
        _atomic_write_json(manifest_path, manifest)

        return {"ok": True, "story_key": story_key, "pid": handle.pid, "branch": branch,
                "resumed": resuming}


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
                last = line[idx + len(marker):].strip()
    return last




def check_story_status(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Check whether a dispatched agent has finished. If complete, runs tests
    in the worktree and reports pass/fail without auto-merging.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
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
            capture_output=True, text=True,
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
                        manifest, manifest_path, plan_name, story_key, story,
                        pid=pid, step="dispatch_watchdog_timeout",
                        summary=(
                            f"Dispatch watchdog: no completion after "
                            f"{elapsed:.0f}s; process terminated."
                        ),
                    )
                    story["dispatch_error"] = (
                        f"watchdog killed after {elapsed:.0f}s with no completion"
                    )
                    _atomic_write_json(manifest_path, manifest)
                    return {"status": "interrupted", "pid": pid, "watchdog_killed": True}
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
            story["dispatch_error"] = f"agent produced no output in {attempts} launch attempts"
            _notify_user(plan_name, f"{story_key} failed to launch {attempts}x; "
                                    f"giving up - needs human intervention.")
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
    if last_log_line in STEP_CAP_MARKERS:
        sha = _commit_wip(str(worktree), story_key, "step_cap_reached")
        interrupted_at = datetime.now(timezone.utc).isoformat()
        _append_journal(plan_name, story_key, {
            "step": "step_cap_reached",
            "summary": "Agent hit the step cap; checkpointed for resume.",
            "next_hint": "",
            "commit": sha,
            "ts": interrupted_at,
        })
        story["status"] = "interrupted"
        story["last_commit"] = sha
        story["interrupted_at"] = interrupted_at

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
        if (fallback_model and current_model != fallback_model
                and story.get("backend", "local") == "local"):
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
                    f"{fallback_model} for the next resume.")
        elif (not fallback_model and _auto_escalation_enabled()
                and story.get("backend", "local") == "local"
                and not story.get("escalated")):
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
                    f"local_model_fallback configured).")
                return {"status": "todo", "reason": "step_cap_escalated_to_claude",
                        "pid": pid}
        _atomic_write_json(manifest_path, manifest)
        return {"status": "interrupted", "pid": pid, "reason": "step_cap_reached"}

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
        k: v for k, v in os.environ.items()
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
                test_cmd, cwd=test_dir, capture_output=True, text=True,
                env=test_env,
            )
    else:
        test_result = subprocess.run(
            test_cmd, cwd=test_dir, capture_output=True, text=True,
            env=test_env,
        )
    passed = test_result.returncode == 0

    # The agent produced real output and the tests ran: the launch worked, so
    # clear any failed-launch attempts accumulated by earlier infra blips.
    story.pop("dispatch_attempts", None)

    # False-positive guard: tests passing against an untouched worktree
    # (e.g. main's suite against an empty branch because the agent parked
    # in a repetition loop without writing code) is not "the task is done."
    # require at least one commit on the agent branch beyond the base
    # branch before we count it as `tests_passed`. Mark `failed` (not
    # `interrupted`) because re-dispatching the same prompt to the same
    # model on the same empty worktree is unlikely to produce a different
    # outcome next tick; better to surface it for the dashboard.
    if passed and not _worktree_has_new_commits(
        worktree, story_key, base_branch=_default_branch(),
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
    if (not passed
            and story["status"] == "failed"
            and os.environ.get("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "0") == "1"
            and _worktree_has_new_commits(
                worktree, story_key, base_branch=_default_branch())):
        story["status"] = "tests_passed"  # reviewable; reviewer sees the failure
        story["acceptance_failed_review"] = True

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
    _validate_key(plan_name)
    _validate_key(story_key)
    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "another dispatch/interrupt is in progress for this plan",
            }
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
        manifest = json.loads(manifest_path.read_text())
        story = manifest["stories"].get(story_key)
        if not story:
            return {"ok": False, "error": f"No such story {story_key}"}
        if "pid" not in story:
            return {"ok": False, "error": "Story not dispatched"}

        sha = _terminate_and_checkpoint(
            manifest, manifest_path, plan_name, story_key, story,
            pid=story["pid"], step="interrupted",
            summary="Agent process terminated; checkpointed for resume.",
        )

        return {"ok": True, "status": "interrupted", "commit": sha}


@mcp.tool()
def mark_story_in_progress(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Transition the ticket to In Progress and update the local manifest.
    Use this before writing any code for a story.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    get_ticket_provider().set_state(story_key, LogicalState.IN_PROGRESS, plan_name)

    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if story_key not in manifest["stories"]:
        return {"ok": False, "error": f"No such story {story_key}"}
    manifest["stories"][story_key]["status"] = "in_progress"
    _atomic_write_json(manifest_path, manifest)
    return {"ok": True}




@mcp.tool()
def checkpoint(
    plan_name: str, story_key: str, step: str, summary: str, next_hint: str = "",
) -> dict[str, Any]:
    """
    Record a durable checkpoint for a dispatched agent's progress.

    Commits any uncommitted work in the story's worktree as a WIP commit and
    appends an entry to the story's journal (plan.story.journal.json). Call
    this after completing each idempotent step of a story so a killed agent
    can resume from the last checkpoint instead of starting over.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    return _checkpoint_impl(plan_name, story_key, step, summary, next_hint)


@mcp.tool()
def mark_story_done(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Transition the ticket to Done and update the local manifest.
    Use after you've reviewed and merged the agent's PR.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    get_ticket_provider().set_state(story_key, LogicalState.DONE, plan_name)

    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stories"][story_key]["status"] = "done"
    manifest["stories"][story_key].pop("parked_reason", None)
    _atomic_write_json(manifest_path, manifest)
    return {"ok": True}


# Story fields patch_story may edit. Deliberately excludes "status" (use
# set_story_status), "worktree", "pid", "review_verdict" and other
# pipeline-owned runtime state - this tool is for correcting what the plan
# authored, not for mechanically bypassing the review/merge gates.
_PATCHABLE_STORY_FIELDS = frozenset((
    "agent_instructions", "model", "persona", "risk", "dependencies",
    "acceptance", "pr_url", "summary", "tdd_split",
))

# Every status value the pipeline itself assigns to a story (see the
# "status"] = / "status": literal assignments throughout this file). Kept as
# an explicit allowlist so set_story_status can't be used to invent a status
# the rest of the code doesn't know how to handle.
_VALID_STORY_STATUSES = frozenset((
    "todo", "in_progress", "running", "interrupted", "failed",
    "tests_passed", "pr_open", "changes_requested", "parked", "done",
))


@mcp.tool()
def patch_story(plan_name: str, story_key: str, fields: dict[str, Any]) -> dict[str, Any]:
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
        return {"ok": False, "error": f"cannot patch field(s) {sorted(unknown)}: "
                                       f"only {sorted(_PATCHABLE_STORY_FIELDS)} are editable"}

    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "another dispatch/ingest/interrupt is in progress for this plan",
            }
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
        manifest = json.loads(manifest_path.read_text())
        story = manifest["stories"].get(story_key)
        if story is None:
            return {"ok": False, "error": f"No such story {story_key!r}"}
        story.update(fields)
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "story_key": story_key, "story": story}


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
    _validate_key(plan_name)
    _validate_key(story_key)
    if status not in _VALID_STORY_STATUSES:
        return {"ok": False, "error": f"invalid status {status!r}: "
                                       f"must be one of {sorted(_VALID_STORY_STATUSES)}"}

    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "another dispatch/ingest/interrupt is in progress for this plan",
            }
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
        manifest = json.loads(manifest_path.read_text())
        story = manifest["stories"].get(story_key)
        if story is None:
            return {"ok": False, "error": f"No such story {story_key!r}"}
        story["status"] = status
        if status != "parked":
            story.pop("parked_reason", None)
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "story_key": story_key, "status": status}


@mcp.tool()
def check_usage() -> dict[str, Any]:
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
        state["consecutive_parse_failures"] = prev.get("consecutive_parse_failures", 0) + 1
        measured_at = prev.get("measured_at", prev.get("checked_at"))
        state["measured_at"] = measured_at
        age = _usage_state_age_seconds({"checked_at": measured_at}) if measured_at else None
        if age is not None and age > USAGE_STALE_AFTER_SECONDS:
            state["stale"] = True
            state["gate_blind"] = True
            first_blind = not prev.get("gate_blind")
            if first_blind:
                state["blind_since"] = now_iso

            blind_since = state.get("blind_since")
            blind_age = _usage_state_age_seconds({"checked_at": blind_since}) if blind_since else None
            if blind_age is not None and blind_age > USAGE_BLIND_PAUSE_AFTER_SECONDS:
                # Prolonged blindness: fail-closed so a permanent CLI-format
                # change can't leave spend unguarded indefinitely.
                state["paused"] = True
            else:
                state["paused"] = False

            failures = state["consecutive_parse_failures"]
            should_log = first_blind or (failures % USAGE_BLIND_LOG_INTERVAL == 0)
            if should_log:
                status = "pausing (fail-closed)" if state["paused"] else "failing the gate OPEN"
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
        prev.get("paused", False), state["session_pct"], state["week_pct"],
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
    _append_decision(plan_name, record)
    return record


@mcp.tool()
def list_decisions(plan_name: str) -> list[dict]:
    """Return the overlord decision log for a plan (audit trail)."""
    _validate_key(plan_name)
    path = _decisions_path(plan_name)
    return json.loads(path.read_text()) if path.exists() else []


@mcp.tool()
def review_story(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Run the code-reviewer persona over a dispatched story's branch. On APPROVE,
    open a PR via gh and set status to pr_open; otherwise set status to
    changes_requested. Does not merge — merge is the overlord's decision.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    story = manifest["stories"].get(story_key)
    if not story:
        return {"ok": False, "error": f"No such story {story_key}"}

    branch = f"agent/{story_key.lower()}"
    worktree = story.get("worktree", "")
    plan_role_config = _plan_role_config(plan_name)
    try:
        # Once a story is escalated (see _escalate_review_to_claude below),
        # every subsequent review must go to Claude regardless of the global
        # PIPELINE_BACKEND_REVIEW setting - review backend is otherwise
        # resolved purely from that env var with no per-story override, so
        # this is the one seam that needs an explicit check.
        reviewer_output = (
            _run_reviewer(worktree, branch, backend_name="claude",
                          plan_role_config=plan_role_config,
                          acceptance=story.get("acceptance"))
            if story.get("escalated") else
            _run_reviewer(worktree, branch, plan_role_config=plan_role_config,
                          acceptance=story.get("acceptance"))
        )
    except backend.RateLimitedError:
        # FM-B: an Ollama-cloud (or any Ollama-proxied) 429 on the review path
        # is an infrastructure event, not a real review cycle. Treat it the
        # same as Claude's weekly-usage pause: defer and retry on the next
        # tick, do NOT burn REVIEW_INCONCLUSIVE_MAX. Without this, a
        # misclassified rate-limit would eventually park a correct impl.
        story["review_deferred_count"] = story.get("review_deferred_count", 0) + 1
        _notify_user(plan_name,
                     f"{story_key} review deferred: local reviewer rate-limited; will retry next tick.")
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "status": story["status"], "deferred": "rate_limited"}
    except Exception as e:
        # Defense in depth: a reviewer backend's own internal error (a bad
        # tool-call shape, a malformed backend response, ...) must not crash
        # the pipeline process. Fail safe into the same UNKNOWN-verdict path
        # a genuinely inconclusive review already takes below - never treat
        # this as an APPROVE (fail-closed). Log only the exception type, not
        # its text, which could carry sensitive detail.
        _notify_user(plan_name, f"{story_key} review failed with an unexpected "
                                f"{type(e).__name__}; treating as inconclusive.")
        reviewer_output = ""
    verdict = _parse_verdict(reviewer_output)

    # FM-B: a rate-limit response from the reviewer is an infrastructure event,
    # not a genuine review cycle. Leave the story at tests_passed so the next
    # advance_pipeline tick retries review once the backend recovers. Do NOT
    # touch rework_attempts — burning the rework budget on rate-limits parks
    # correct implementations silently.
    if verdict == "UNKNOWN" and _is_rate_limited(reviewer_output):
        story["review_deferred_count"] = story.get("review_deferred_count", 0) + 1
        fallback_mode = os.environ.get("PIPELINE_REVIEW_FALLBACK", "off").strip().lower()
        fallback_after = int(os.environ.get("PIPELINE_REVIEW_FALLBACK_AFTER", "3"))
        if fallback_mode in _LOCAL_BACKEND_NAMES and story["review_deferred_count"] >= fallback_after:
            _notify_user(plan_name, f"{story_key} review falling back to {fallback_mode} backend "
                                    f"after {story['review_deferred_count']} rate-limited attempts.")
            reviewer_output = _run_reviewer(
                worktree, branch, backend_name=fallback_mode,
                plan_role_config=plan_role_config,
                acceptance=story.get("acceptance"),
            )
            verdict = _parse_verdict(reviewer_output)
            # Fall through into the normal verdict-handling code below —
            # this is a genuine review attempt now, not a deferral.
        else:
            _notify_user(plan_name, f"{story_key} review deferred: reviewer rate-limited; will retry next tick.")
            _atomic_write_json(manifest_path, manifest)
            return {"ok": True, "status": story["status"], "deferred": "rate_limited"}

    # Transient backend error (HTTP 500 / connection-reset / connection-refused):
    # re-invoke the reviewer once inline. This is an infrastructure hiccup, not
    # a genuine review cycle, so do NOT increment review_inconclusive_count for
    # this branch itself — only the fallback inconclusive path below (reached
    # when still UNKNOWN after the single retry) touches that counter.
    _transient_retried = False
    if verdict == "UNKNOWN" and _is_transient_backend_error(reviewer_output):
        _notify_user(plan_name, f"{story_key} review hit transient backend error; retrying once.")
        reviewer_output = (
            _run_reviewer(worktree, branch, backend_name="claude",
                          plan_role_config=plan_role_config,
                          acceptance=story.get("acceptance"))
            if story.get("escalated") else
            _run_reviewer(worktree, branch, plan_role_config=plan_role_config,
                          acceptance=story.get("acceptance"))
        )
        verdict = _parse_verdict(reviewer_output)
        _transient_retried = True

    story["review_verdict"] = verdict
    story["review_deferred_count"] = 0

    # High-risk stories require an additional security-engineer pass; both
    # must APPROVE before the story proceeds to pr_open.
    if verdict == "APPROVE" and story.get("risk") == "high":
        security_output = _run_security_reviewer(worktree, branch)
        security_verdict = _parse_verdict(security_output)

        # FM-B: same rate-limit deferral for the security-reviewer pass.
        if security_verdict == "UNKNOWN" and _is_rate_limited(security_output):
            _notify_user(plan_name,
                         f"{story_key} security review deferred: reviewer rate-limited; will retry next tick.")
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
                    story, story_key, plan_name,
                    f"review inconclusive after {inconclusive} attempts",
                )
                # status stays at its pre-review value (e.g. tests_passed) -
                # the next tick retries review, now resolved via Claude.
            else:
                story["status"] = "parked"
                story["parked_reason"] = (
                    f"review inconclusive after {inconclusive} attempts - needs human review"
                )
                _notify_user(plan_name, f"{story_key} parked: review inconclusive after "
                                        f"{inconclusive} attempts - needs human review.")
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
                    story, story_key, plan_name,
                    f"review inconclusive after {inconclusive} attempts (empty REQUEST_CHANGES)",
                )
            else:
                story["status"] = "parked"
                story["parked_reason"] = (
                    f"review inconclusive after {inconclusive} attempts - needs human review"
                )
                _notify_user(plan_name, f"{story_key} parked: review inconclusive after "
                                        f"{inconclusive} attempts - needs human review.")
        else:
            _notify_user(plan_name, f"{story_key} review approved-changes-requested-empty: "
                                    f"REQUEST_CHANGES with no findings text; will retry.")
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "verdict": verdict, "status": story["status"]}

    story["review_inconclusive_count"] = 0

    if verdict == "APPROVE":
        pr_url = _open_pr(worktree, story_key, story)
        story["pr_url"] = pr_url
        story["status"] = "pr_open"
        # The work passed: drop any stale rework state from earlier cycles.
        story.pop("review_feedback", None)
        story.pop("rework_attempts", None)
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
            oracle_now = _reverify_acceptance(story, worktree)
            if oracle_now.get("state") == "pass":
                feedback = (
                    "NOTE: the acceptance oracle is currently PASSING against "
                    "this worktree. The reviewer's feedback below may be about "
                    "something outside the oracle's required behavior - do "
                    "NOT regress the acceptance-oracle-passing behavior while "
                    "addressing it, and re-run the acceptance tests after your "
                    "change to confirm they are still green.\n\n" + reviewer_output
                )
        story["review_feedback"] = feedback
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
                    story, story_key, plan_name,
                    f"rework budget exhausted after {attempts} review cycles",
                )
                # A redispatch will pick up the real review_feedback already
                # set above, now on Claude (story["backend"] was just set).
                story["status"] = "changes_requested"
            else:
                story["status"] = "parked"
                story["parked_reason"] = f"rework budget exhausted after {attempts} review cycles"
                _notify_user(plan_name, f"{story_key} parked: reviewer still requesting changes "
                                        f"after {attempts} cycles - needs human review.")
        else:
            story["status"] = "changes_requested"

    _atomic_write_json(manifest_path, manifest)
    return {
        "ok": True,
        "verdict": verdict,
        "status": story["status"],
        "pr_url": story.get("pr_url"),
    }




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
    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "another advance_pipeline tick is already running for this plan",
            }
        return _advance_pipeline_locked(plan_name)


def _advance_pipeline_locked(plan_name: str) -> dict[str, Any]:
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    if not manifest_path.exists():
        return {"ok": False, "error": f"No manifest for {plan_name}"}
    manifest = json.loads(manifest_path.read_text())
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
    review_ok, review_reason = _role_resource_ok("review")
    # A-posteriori escalation of a failed local run to Claude is a feature of
    # auto dispatch only. Under an explicit local (or claude) backend the
    # operator has pinned the dispatcher on purpose, so a local failure is
    # terminal rather than silently spending Claude.
    dispatch_mode = os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude").strip().lower()

    done = _completed_dep_ids(stories)
    ready = [
        k for k, v in stories.items()
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
                for k, v in stories.items() if v["status"] == "pr_open"
            },
        }

    summary: dict[str, Any] = {
        "autonomy": PIPELINE_AUTONOMY,
        "paused": not dispatch_ok,
        "dispatch_paused": not dispatch_ok,
        "review_paused": not review_ok,
        "dispatched": [], "advanced": [], "merged": [],
        "parked": [], "failed": [], "interrupted": [], "notify": [],
        "review_deferred": [],
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
            _notify_user(plan_name, f"Dispatch backend gated ({dispatch_reason}): deferring dispatch.")
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
                    dispatch_story(plan_name, key)
                    summary["dispatched"].append(key)
                except Exception as e:  # git pull/worktree/backend launch failure
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
                        _notify_user(plan_name, f"{key} dispatch failed {attempts}x "
                                                f"({e}); giving up - needs human intervention.")
                        summary["failed"].append(key)
                    else:
                        # leave status dispatch-eligible; the next tick retries.
                        _notify_user(plan_name, f"{key} dispatch attempt {attempts}/"
                                                f"{DISPATCH_MAX_ATTEMPTS} failed ({e}); will retry.")
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
                        if (dispatch_mode == "auto"
                                and story.get("backend") == "local"
                                and not story.get("escalated")):
                            manifest = json.loads(manifest_path.read_text())
                            _escalate_to_claude(manifest, plan_name, key, manifest_path)
                            _notify_user(plan_name,
                                f"{key} local agent failed; escalating to Claude and starting clean.")
                            summary["notify"].append(key)
                        elif (fallback_model
                                and story.get("backend") == "local"
                                and story.get("model") != fallback_model
                                and not story.get("tried_fallback_model")):
                            # Plan-scoped opt-in (manifest["local_model_fallback"]):
                            # never escalates to Claude - just gives one other
                            # local model a shot before the terminal park/fail
                            # path below.
                            manifest = json.loads(manifest_path.read_text())
                            failed_model = story.get("dispatched_model") or story.get("model") or "default"
                            _escalate_to_local_fallback_model(
                                manifest, plan_name, key, manifest_path, fallback_model)
                            _notify_user(plan_name,
                                f"{key} local agent failed on {failed_model}; retrying on "
                                f"fallback model {fallback_model} before parking.")
                            summary["notify"].append(key)
                        elif check_result.get("failure_kind") == "give_up":
                            # T6: the agent explicitly surrendered rather than
                            # producing ordinary red tests. Point the human at
                            # the story's scope/clarity instead of the generic
                            # message - a missing/wrong API needs a fix to
                            # agent_instructions, not another identical retry.
                            _notify_user(plan_name,
                                f"{key} agent gave up (explicit surrender, zero productive "
                                f"progress) - likely under-specified (missing API, wrong "
                                f"scope) rather than a model-capability gap; needs human "
                                f"clarification before another dispatch.")
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
            _notify_user(plan_name, f"Review backend gated ({review_reason}): deferring review.")
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
            rb = _rebase_onto_master(worktree, branch)
            if rb.get("auto_resolved"):
                _notify_user(plan_name, f"{key} rebase auto-resolved an additive-import "
                                        f"conflict against origin/{_default_branch()}.")
            if not rb["ok"]:
                gate_error = f"rebase: {rb['error']}"
            else:
                # Force-push the rebased branch; only when we actually rebased
                # in a real worktree (a missing worktree skipped the rebase and
                # has nothing to push). Run from REPO_ROOT (the plan's repo).
                # A failed push (concurrent push rejected by --force-with-lease,
                # network/auth) MUST block: otherwise the remote HEAD stays at
                # the pre-rebase commit and the CI gate + squash merge operate
                # on stale code — the exact cross-story breakage Mode 9 closes.
                if Path(worktree).is_dir():
                    push = subprocess.run(["git", "push", "--force-with-lease", "origin",
                                           branch], cwd=REPO_ROOT,
                                          capture_output=True, text=True)
                    if push.returncode != 0:
                        gate_error = f"push: {(push.stderr or push.stdout).strip()[:200]}"
                if not gate_error:
                    ci = _ci_status(branch)
                    if ci["state"] == "cancelled" and not story.get("ci_rerun_attempted"):
                        # Worth exactly one automatic rerun before treating it
                        # as a failure - an abnormal queue delay can cancel
                        # jobs with no code-quality signal at all.
                        story["ci_rerun_attempted"] = True
                        _ci_rerun(branch)
                        ci = _ci_status(branch)
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
                        gate_error = f"ci pending: {ci['error']}"
                if not gate_error:
                    # Independent of review: re-run the acceptance oracle
                    # against the just-rebased branch right before merging.
                    # Closes the gap CI alone can't (a repo without CI, or a
                    # CI-independent slip between tests_passed and review).
                    acc = _reverify_acceptance(story, worktree)
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
                    story["review_feedback"] = (
                        "The merge-gate CI check failed on your submitted branch "
                        f"(reviewer already APPROVEd this work):\n{gate_error}\n\n"
                        "This is often caused by a test file YOU wrote containing "
                        "an incorrect assertion, not the implementation. Re-examine "
                        "your own test files against the spec, fix any incorrect "
                        "assertions, and ensure the full suite passes before "
                        "resubmitting."
                    )
                    story["status"] = "changes_requested"
                    _notify_user(plan_name, f"{key} merge-gate CI failed ({gate_error}); "
                                            f"routed to rework ({attempts}/{MERGE_MAX_ATTEMPTS}).")
                    summary["notify"].append(key)
                    continue

                attempts = story.get("merge_attempts", 0) + 1
                story["merge_attempts"] = attempts
                if attempts >= MERGE_MAX_ATTEMPTS:
                    story["status"] = "failed"
                    story["merge_error"] = gate_error
                    _notify_user(plan_name, f"{key} merge gate failed {attempts}x "
                                            f"({gate_error}); giving up - needs human intervention.")
                    summary["failed"].append(key)
                else:
                    # leave pr_open; the next tick retries within budget.
                    _notify_user(plan_name, f"{key} merge gate attempt {attempts}/"
                                            f"{MERGE_MAX_ATTEMPTS} failed ({gate_error}); will retry.")
                summary["notify"].append(key)
                continue

            try:
                _merge_pr(story.get("worktree", ""), key)
            except Exception as e:  # gh/git transient failure - see MERGE_MAX_ATTEMPTS
                attempts = story.get("merge_attempts", 0) + 1
                story["merge_attempts"] = attempts
                if attempts >= MERGE_MAX_ATTEMPTS:
                    story["status"] = "failed"
                    story["merge_error"] = str(e)
                    _notify_user(plan_name, f"{key} merge failed {attempts}x "
                                            f"({e}); giving up - needs human intervention.")
                    summary["failed"].append(key)
                else:
                    # leave pr_open; the next tick retries within budget.
                    _notify_user(plan_name, f"{key} merge attempt {attempts}/"
                                            f"{MERGE_MAX_ATTEMPTS} failed ({e}); will retry.")
                summary["notify"].append(key)
                continue
            story["status"] = "done"
            story.pop("merge_attempts", None)
            story.pop("parked_reason", None)
            story.pop("ci_rerun_attempted", None)
            story.pop("ci_rework", None)  # L1: clear the rework flag on done
            _mark_plane_done(key, plan_name)
            summary["merged"].append(key)
        _atomic_write_json(manifest_path, manifest)

    return {"ok": True, **summary}


@mcp.tool()
def approve_merge(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Manually merge a story a human has approved out-of-band, typically one
    "parked" by the risk gate (medium/high risk always parks regardless of
    autonomy level - this is the human's explicit override for that gate,
    not a way to bypass review). Also works on a still-"pr_open" story, for
    approving before the gate has even adjudicated it.

    Refuses unless the story already carries an APPROVE review verdict, and
    refuses any status other than "parked"/"pr_open" - this merges reviewed
    work, it does not re-review or fast-track anything.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"

    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {"ok": False,
                    "error": "plan busy (scheduler tick in progress); retry",
                    "retriable": True}
        # Re-read the manifest from disk INSIDE the lock so we merge against
        # the freshest on-disk state, not a pre-lock stale copy. A scheduler
        # tick may have changed the story's status or verdict while we waited
        # to acquire the lock.
        manifest = json.loads(manifest_path.read_text())
        story = manifest["stories"].get(story_key)
        if not story:
            return {"ok": False, "error": f"No such story {story_key}"}
        if story["status"] not in ("parked", "pr_open"):
            return {"ok": False, "error": f"Story is {story['status']}, not parked/pr_open"}
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
                    _notify_user(plan_name, f"{story_key} rebase auto-resolved an "
                                            f"additive-import conflict against "
                                            f"origin/{_default_branch()}.")
                if not rb["ok"]:
                    return {"ok": False, "error": f"rebase failed: {rb['error']}",
                            "story_key": story_key}
                if Path(worktree).is_dir():
                    push = subprocess.run(["git", "push", "--force-with-lease", "origin",
                                           branch], cwd=REPO_ROOT,
                                          capture_output=True, text=True)
                    if push.returncode != 0:
                        return {"ok": False,
                                "error": f"push failed: {(push.stderr or push.stdout).strip()[:200]}",
                                "story_key": story_key}
                ci = _ci_status(branch)
                if ci["state"] == "cancelled" and not story.get("ci_rerun_attempted"):
                    # Same one-shot auto-rerun as the scheduler's merge gate:
                    # a queue-delay cancellation carries no code-quality
                    # signal, so give it one automatic retry before failing.
                    story["ci_rerun_attempted"] = True
                    _ci_rerun(branch)
                    ci = _ci_status(branch)
                if ci["state"] in ("fail", "cancelled"):
                    return {"ok": False, "error": f"CI failing: {ci['error']}",
                            "story_key": story_key}
                if ci["state"] == "pending":
                    return {"ok": False, "error": f"CI still pending: {ci['error']}",
                            "story_key": story_key}
                acc = _reverify_acceptance(story, worktree)
                if acc["state"] == "fail":
                    return {"ok": False, "error": f"acceptance reverify fail: {acc['error']}",
                            "story_key": story_key}
                build = _reverify_build(worktree)
                if build["state"] == "fail":
                    return {"ok": False, "error": f"build reverify fail: {build['error']}",
                            "story_key": story_key}
                _merge_pr(story.get("worktree", ""), story_key)
        except Exception as e:  # surface the gh/git failure to the human, don't raise
            return {"ok": False, "error": str(e), "story_key": story_key}
        # Final write INSIDE the lock, using the manifest re-read inside the
        # lock (not a pre-lock copy). Clear parked_reason on leaving 'parked'.
        story["status"] = "done"
        story.pop("parked_reason", None)
        story.pop("ci_rerun_attempted", None)
        _atomic_write_json(manifest_path, manifest)
    _mark_plane_done(story_key, plan_name)
    return {"ok": True, "story_key": story_key, "status": "done"}


def _set_plan_paused(plan_name: str, paused: bool) -> dict[str, Any]:
    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "an advance_pipeline tick is already running for this plan",
            }
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
        if not manifest_path.exists():
            return {"ok": False, "error": f"No manifest for {plan_name}"}
        manifest = json.loads(manifest_path.read_text())
        manifest["paused"] = paused
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "plan_name": plan_name, "paused": paused}


@mcp.tool()
def pause_plan(plan_name: str) -> dict[str, Any]:
    """
    Stop advance_pipeline/advance_all_plans from touching this one plan -
    no new dispatch, review, or merge - while leaving every other ingested
    plan's scheduler ticks unaffected. Any story currently in_progress is
    interrupted (checkpointed and left resumable) so a paused plan isn't
    quietly burning usage in the background. Resume with resume_plan.
    """
    _validate_key(plan_name)
    return _set_plan_paused(plan_name, True)


@mcp.tool()
def resume_plan(plan_name: str) -> dict[str, Any]:
    """Clear a pause set by pause_plan so this plan's stories are eligible
    for dispatch/review/merge on the next advance_pipeline tick again."""
    _validate_key(plan_name)
    return _set_plan_paused(plan_name, False)


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
    plans = {}
    for manifest_path in sorted(PLAN_DIR.glob("*.manifest.json")):
        plan_name = manifest_path.name.removesuffix(".manifest.json")
        try:
            plans[plan_name] = advance_pipeline(plan_name)
        except Exception as e:
            # One plan's failure (bad repo_root, missing tool, transient git
            # error, ...) must not stop every other plan from getting its tick.
            plans[plan_name] = {"ok": False, "error": str(e)}
    return {"ok": True, "plans": plans}


if __name__ == "__main__":
    mcp.run()
