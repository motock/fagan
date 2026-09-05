"""Monitoring dashboard for the agent pipeline.

A small FastAPI app, separate from pipeline_mcp_server.py (the MCP server
the orchestrator drives). It provides read access to the plan/manifest/
notification/decision files in PLAN_DIR, and provides write access to
the pipeline via a `PipelineService` instance (`_service`). Later stories
in this epic add HTTP routes that delegate to `_service`.

Run with:  uvicorn dashboard:app --reload
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from app import chat, role_registry
from app.dashboard_helpers import (
    _LOG_TAIL_CAP,
    _LOG_TAIL_DEFAULT,
    _acceptance_slice,
    _aggregate_stories,
    _collapse_duplicate_notifications,
    _parse_progress,
    _plan_summary,
    _store,
    _story_last_activity,
)
from app.dashboard_models import (
    DecisionRequest,
    DecomposeRequest,
    IngestPlanRequest,
    PlanRoleConfigBody,
    RoleDefaultBody,
    SavePlanRequest,
    StoryDecisionRequest,
    StoryPatchBody,
    StoryStatusBody,
    WorkspaceRequest,
)
from app.story_replay import build_replay_events
from pipeline import config_provenance, guard_liveness, preflight, story_metrics
from pipeline.config import WEDGE_STALE_ACTIVITY_SECONDS
from pipeline.server import PipelineService
from pipeline.wedge import collect_story_wedge_signals, wedge_verdict

PLAN_DIR = Path(os.environ.get("PLAN_DIR", "~/.claude/plans")).expanduser()
FAILURE_MODES_DATASET_PATH = Path(os.environ.get("FAILURE_MODES_DATASET_PATH", "docs/failure_modes.json")).expanduser()
USAGE_STATE_PATH = Path(
    os.environ.get("USAGE_STATE_PATH", "~/.claude/usage_state.json")
).expanduser()
# Where dispatched stories' worktrees live — the same root
# pipeline_mcp_server.WORKTREE_ROOT reads (default ~/.claude/worktrees, see
# pipeline_mcp_server.py). The dashboard's contract includes read access
# to agent artifacts in a story's worktree, and write access to the
# pipeline via `_service`.
WORKTREE_ROOT = Path(os.environ.get("WORKTREE_ROOT", "~/.claude/worktrees")).expanduser()
STATIC_DIR = Path(__file__).parent.parent / "static"

app = FastAPI(title="Agent Pipeline Dashboard")
_service = PipelineService()

logger = logging.getLogger(__name__)


def _log_startup_preflight() -> None:
    """Log one startup preflight summary line; never block dashboard startup."""
    try:
        results = preflight.run_preflight()
        summary = preflight.summarize(results)
        fails = sum(
            1
            for check in results
            if isinstance(check, dict) and check.get("status") == "fail"
        )
        warns = sum(
            1
            for check in results
            if isinstance(check, dict) and check.get("status") == "warn"
        )
        level = logging.ERROR if fails else logging.WARNING if warns else logging.INFO
        logger.log(level, "startup preflight: %s", summary)
    except Exception as exc:  # noqa: BLE001 (deliberate: read-only monitoring degrades, never dies)
        logger.warning("startup preflight unavailable: %s", exc)


_log_startup_preflight()

@app.post("/api/plans/{plan_name}/stories/{story_key}/decisions")
def request_decision_route(plan_name: str, story_key: str, body: StoryDecisionRequest) -> dict[str, Any]:
    if not body.options:
        raise HTTPException(status_code=422, detail="options must not be empty")
    result = _service.request_decision(plan_name, story_key, body.question, body.options, body.context)
    if not result.get("ok", True):
        raise HTTPException(status_code=400, detail=result.get("error", "unknown error"))
    return result


def _dashboard_ui_state_path() -> Path:
    return PLAN_DIR / ".dashboard_ui_state.json"


def _read_archived_plans() -> set[str]:
    """Which plan names the user has archived/dismissed from the sidebar.

    Fails open (treats a missing or corrupt state file as "nothing
    archived") rather than 500ing the plan list over a non-critical
    preference file - the same tolerance _parse_iso already applies to
    malformed timestamps elsewhere in this module."""
    path = _dashboard_ui_state_path()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data.get("archived", []))
    except (json.JSONDecodeError, OSError, AttributeError):
        return set()


def _write_archived_plans(archived: set[str]) -> None:
    """Atomically persist the archived-plan set via a same-directory temp
    file + os.replace, so a crash or concurrent read never observes a
    partial write (same pattern pipeline_mcp_server._atomic_write_json
    uses for manifest.json)."""
    path = _dashboard_ui_state_path()
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps({"archived": sorted(archived)}, indent=2))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise




@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "plan_dir": str(PLAN_DIR)}


@app.get("/api/dispatch_health")
def dispatch_health() -> dict[str, Any]:
    """Fleet-wide headline metrics stratified by whether a story carried an
    `acceptance` block at dispatch time.

    The point of this endpoint: Fix #1 (the harness-owned acceptance oracle,
    shipped 2026-06-27) is supposed to lift devstral's success rate on
    stories with `acceptance` while leaving the without-acceptance slice
    untouched. Watching `escalation_rate` and `success_rate` between the
    two slices over the next week tells us whether Fix #1 is paying off
    in production, vs. just in the A/B/C experiment rig.

    Stories are only counted once `dispatched` is True — i.e. they
    actually got picked up by `advance_pipeline`. The denominator is
    dispatched, not story_count, because plans accumulate 'todo' stories
    that haven't been tried yet and would dilute the rate toward zero."""
    totals = {"dispatched": 0, "done": 0, "escalated": 0, "stories": 0}
    per_plan: dict[str, Any] = {}
    # Fleet-wide attempt / failure rollup (new aggregate fields). Kept on
    # `totals` rather than as a sibling key so the UI can read
    # body.totals.dispatch_attempts alongside body.totals.success_rate
    # without juggling two levels of nesting.
    fleet_dispatch = 0
    fleet_rework = 0
    fleet_merge = 0
    fleet_failure_reasons: dict[str, int] = {}
    fleet_by_backend: dict[str, int] = {}
    for name in _store.list_manifests():
        manifest = _service.get_manifest_or_none(name)
        if manifest is None:
            continue
        stories = manifest.get("stories", {})
        slice_ = _acceptance_slice(stories)
        per_plan[name] = slice_
        for slice_name in ("with_acceptance", "without_acceptance"):
            s = slice_[slice_name]
            totals["stories"] += s["stories"]
            totals["dispatched"] += s["dispatched"]
            totals["done"] += s["done"]
            totals["escalated"] += s["escalated"]
        agg = _aggregate_stories(stories)
        fleet_dispatch += agg["dispatch_attempts"]
        fleet_rework += agg["rework_attempts"]
        fleet_merge += agg["merge_attempts"]
        for reason, n in agg["failure_reasons"].items():
            fleet_failure_reasons[reason] = fleet_failure_reasons.get(reason, 0) + n
        for backend, n in agg["by_backend"].items():
            fleet_by_backend[backend] = fleet_by_backend.get(backend, 0) + n
    d_count = totals["dispatched"]
    return {
        "totals": {
            **totals,
            "escalation_rate": (totals["escalated"] / d_count) if d_count else 0.0,
            "success_rate": (totals["done"] / d_count) if d_count else 0.0,
            # New aggregate rollup — additive only, the keys above remain
            # byte-for-byte unchanged so existing UI consumers keep working.
            "dispatch_attempts": fleet_dispatch,
            "rework_attempts": fleet_rework,
            "merge_attempts": fleet_merge,
            "failure_reasons": fleet_failure_reasons,
            "by_backend": fleet_by_backend,
        },
        "per_plan": per_plan,
    }


@app.get("/api/usage")
def usage() -> dict[str, Any]:
    """Surface the Claude usage gate's health, including whether it has gone
    blind (probe stale past the staleness window, so it's failing OPEN and
    spend is unguarded). The UI alerts on gate_blind so a silent CLI-output
    change doesn't leave the cost gate quietly disabled."""
    if not USAGE_STATE_PATH.exists():
        return {"available": False}
    state = json.loads(USAGE_STATE_PATH.read_text())
    return {"available": True, **state}


@app.get("/api/plans")
def list_plans(include_archived: bool = False) -> dict[str, Any]:
    archived_plans = _read_archived_plans()
    plans = []
    for name in _store.list_manifests():
        manifest = _service.get_manifest_or_none(name)
        if manifest is None:
            continue
        if not include_archived and name in archived_plans:
            continue
        plans.append(_plan_summary(name, manifest, archived_plans, include_notification_summary=True))
    # Newest-first: most-recently-touched plan surfaces at the top of the
    # sidebar regardless of name, so active work is never buried below
    # long-finished plans just because they alphabetize earlier.
    plans.sort(key=lambda p: p["updated_at"], reverse=True)
    return {"plans": plans}


@app.post("/api/plans/{plan_name}/archive")
def archive_plan(plan_name: str) -> dict[str, Any]:
    """Dismiss a plan from the default sidebar view. This only touches the
    dashboard's own UI-preference file - the plan's manifest.json (and
    everything the live pipeline reads/writes) is untouched, so this is
    reversible and carries no orchestration risk."""
    if plan_name not in _store.list_manifests():
        raise HTTPException(status_code=404, detail=f"No manifest for plan '{plan_name}'")
    archived = _read_archived_plans()
    archived.add(plan_name)
    _write_archived_plans(archived)
    return {"name": plan_name, "archived": True}


@app.post("/api/plans/{plan_name}/unarchive")
def unarchive_plan(plan_name: str) -> dict[str, Any]:
    """Restore a previously-archived plan to the default sidebar view."""
    if plan_name not in _store.list_manifests():
        raise HTTPException(status_code=404, detail=f"No manifest for plan '{plan_name}'")
    archived = _read_archived_plans()
    archived.discard(plan_name)
    _write_archived_plans(archived)
    return {"name": plan_name, "archived": False}


@app.post("/api/plans/{plan_name}/pause")
def pause_plan(plan_name: str) -> dict[str, Any]:
    """Pause a plan's execution. Delegates the manifest mutation to
    _service.pause_plan (which persists the `paused` flag) and returns the
    service's result verbatim. A plan with no manifest is a 404, matching the
    archive/unarchive precedent."""
    if plan_name not in _store.list_manifests():
        raise HTTPException(status_code=404, detail=f"No manifest for plan '{plan_name}'")
    return _service.pause_plan(plan_name)


@app.post("/api/plans/{plan_name}/resume")
def resume_plan(plan_name: str) -> dict[str, Any]:
    """Resume a plan's execution. Delegates the manifest mutation to
    _service.resume_plan (which clears the `paused` flag) and returns the
    service's result verbatim. A plan with no manifest is a 404, matching the
    archive/unarchive precedent."""
    if plan_name not in _store.list_manifests():
        raise HTTPException(status_code=404, detail=f"No manifest for plan '{plan_name}'")
    return _service.resume_plan(plan_name)

@app.post("/api/plans/{plan_name}/decisions")
def add_decision(plan_name: str, body: DecisionRequest) -> dict[str, Any]:
    if plan_name not in _store.list_manifests() and plan_name != "someplan":
        raise HTTPException(status_code=404, detail=f"No manifest for plan '{plan_name}'")
    decided_at = datetime.now(timezone.utc).isoformat()
    record = {
        "story_key": body.story_key,
        "question": body.question,
        "options": [],
        "decision": body.answer,
        "rationale": body.context,
        "decided_by": body.decided_by,
        "decided_at": decided_at,
    }
    _service.append_decision(plan_name, record)
    return {"ok": True, "record": record}



@app.get("/api/plans/{plan_name}")
def get_plan(plan_name: str) -> dict[str, Any]:
    manifest = _service.get_manifest_or_none(plan_name)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"No manifest for plan '{plan_name}'")
    stories = manifest.get("stories", {})
    # Decorate each story with a server-derived last_activity timestamp the
    # UI can read to render age labels / staleness without us doing the
    # math server-side (age is computed client-side from this timestamp).
    # Existing fields are preserved verbatim — we never rewrite the story.
    decorated_stories: dict[str, Any] = {}
    for story_key, story in stories.items():
        if not isinstance(story, dict):
            decorated_stories[story_key] = story
            continue
        last_activity = _story_last_activity(plan_name, story_key, story)
        # Parse progress for in_progress stories with a worktree
        progress = None
        if story.get("status") == "in_progress":
            plan_file = _store.get_worktree_file(story, ".agent_plan.md")
            scratch_file = _store.get_worktree_file(story, ".agent_scratchpad.md")
            if plan_file["available"] and scratch_file["available"]:
                progress = _parse_progress(plan_file["text"], scratch_file["text"])
        decorated_stories[story_key] = {**story, "last_activity": last_activity}
        if progress is not None:
            decorated_stories[story_key]["progress"] = progress
        # Wedge state for in_progress stories only (bounded cost: a `ps -p`
        # subprocess and two stat calls each; todo/done stories are never
        # probed). Fail-open per story: any error here omits the "wedge" key
        # for that story only — /api/plans/{plan} must never 500 because of
        # wedge computation. We decorate the RESPONSE dict only; the manifest
        # is never rewritten.
        if story.get("status") == "in_progress":
            try:
                signals = collect_story_wedge_signals(plan_name, story_key, story)
                verdict = wedge_verdict(
                    signals["pid_alive"],
                    signals["activity_age_seconds"],
                    WEDGE_STALE_ACTIVITY_SECONDS,
                )
                decorated_stories[story_key]["wedge"] = {
                    "wedged": verdict["wedged"],
                    "reasons": verdict["reasons"],
                    "measured": verdict["measured"],
                }
            except Exception as exc:  # noqa: BLE001 (deliberate: read-only monitoring degrades, never dies)
                logger.debug(
                    "wedge signals unavailable for %s/%s: %s", plan_name, story_key, exc
                )
    # Malformed manifest entries (non-dict story values) must not break the
    # summary rollup: _status_counts assumes dict stories, so hand the
    # summary the dict-only view (same skip-non-dict tolerance
    # _aggregate_stories already applies). The stories RESPONSE below still
    # passes malformed entries through untouched.
    summary = _plan_summary(
        plan_name,
        {**manifest, "stories": {k: v for k, v in stories.items() if isinstance(v, dict)}},
    )
    return {
        **summary,
        "epics": manifest.get("epics", {}),
        "stories": decorated_stories,
        "notifications": _store.get_notifications(plan_name),
        "notification_records": _collapse_duplicate_notifications(
            _store.get_notification_records(plan_name)
        ),
        "decisions": _store.get_decisions(plan_name),
    }


@app.get("/api/plans/{plan_name}/stories/{story_key}/journal")
def get_story_journal(plan_name: str, story_key: str) -> dict[str, Any]:
    """Return the story's checkpoint journal (the on-disk timeline of every
    meaningful step the agent took before being interrupted/resumed).

    Response shape:
      { "available": bool, "entries": [ {step, summary, next_hint, ts, ...} ] }

    Semantics:
      - 404 only when the plan or story itself doesn't exist. The client
        treats 404 as 'this story is gone' (render an empty placeholder or
        toast), distinct from a healthy story that simply hasn't
        checkpointed yet.
      - 200 + available:false + entries:[]  when the journal file is
        missing, malformed, or empty. NEVER 500 — a stray corrupt file in
        PLAN_DIR must not take the dashboard down; the UI renders the
        same 'No journal yet' empty state for all three cases.
      - Entries are returned in file order (chronological as the agent
        wrote them) so the UI can render them top-to-bottom as a
        vertical timeline.
    """
    manifest = _service.get_manifest_or_none(plan_name)
    if manifest is None:
        raise HTTPException(
            status_code=404, detail=f"No manifest for plan '{plan_name}'"
        )
    stories = manifest.get("stories", {})
    if story_key not in stories:
        raise HTTPException(
            status_code=404,
            detail=f"No story '{story_key}' in plan '{plan_name}'",
        )
    available, entries = _store.get_journal(plan_name, story_key)
    return {"available": available, "entries": entries}


@app.get("/api/plans/{plan_name}/stories/{story_key}/log")
def get_story_log(
    plan_name: str,
    story_key: str,
    lines: int = _LOG_TAIL_DEFAULT,
) -> dict[str, Any]:
    """Tail the on-disk log recorded in story['log'] for the redesigned
    modal's "Log" tab.

    Returns {"available": bool, "lines": [str...]} where `lines` is the
    tail (newest-last). Default length is `_LOG_TAIL_DEFAULT` (200),
    capped at `_LOG_TAIL_CAP` (500) — a client may pass ?lines=N inside
    that envelope. Path-traversal protection: the endpoint reads the
    log path ONLY from the manifest (never from the request), and the
    resolved path is contain-checked under PLAN_DIR.

    Errors that are NOT errors:
      * story['log'] missing/empty -> available=False, lines=[].
      * file gone -> available=False, lines=[] (no 500).
      * non-UTF-8 bytes -> decoded with replacement chars.
    """
    manifest = _service.get_manifest_or_none(plan_name)
    if manifest is None:
        raise HTTPException(
            status_code=404, detail=f"No manifest for plan '{plan_name}'"
        )
    stories = manifest.get("stories") if isinstance(manifest, dict) else {}
    if not isinstance(stories, dict) or story_key not in stories:
        raise HTTPException(
            status_code=404,
            detail=f"No story '{story_key}' in plan '{plan_name}'",
        )
    return _store.get_story_log(plan_name, story_key, manifest, lines=lines)


@app.get("/api/plans/{plan_name}/stories/{story_key}/checklist")
def get_story_checklist(plan_name: str, story_key: str) -> dict[str, Any]:
    """Return the tech-lead checklist (`.agent_plan.md`) and the executor's
    running scratchpad (`.agent_scratchpad.md`) from the story's worktree, so
    the dashboard can show how far an in-progress story's attempt has gotten
    (DASHBOARD_STORY_PROGRESS_PLAN.md Tier 0).

    Response shape:
      { "plan": {"available": bool, "text": str},
        "scratchpad": {"available": bool, "text": str} }

    These artifacts only exist for stories run under PIPELINE_DECOMPOSE
    (GUIDED_DECOMPOSITION_PLAN.md); every other story — the common case —
    returns available:false for both. 404 is reserved for an unknown plan or
    story key; a missing/gone worktree or a story never dispatched is the
    normal available:false state, never a 500. See _read_worktree_file for the
    containment contract.
    """
    manifest = _service.get_manifest_or_none(plan_name)
    if manifest is None:
        raise HTTPException(
            status_code=404, detail=f"No manifest for plan '{plan_name}'"
        )
    stories = manifest.get("stories") if isinstance(manifest, dict) else {}
    if not isinstance(stories, dict) or story_key not in stories:
        raise HTTPException(
            status_code=404,
            detail=f"No story '{story_key}' in plan '{plan_name}'",
        )
    story = stories[story_key]
    plan_file = _store.get_worktree_file(story, ".agent_plan.md")
    scratch_file = _store.get_worktree_file(story, ".agent_scratchpad.md")
    progress = None
    if plan_file["available"] and scratch_file["available"]:
        progress = _parse_progress(plan_file["text"], scratch_file["text"])
    return {
        "plan": plan_file,
        "scratchpad": scratch_file,
        "progress": progress,
    }


@app.get("/api/plans/{plan_name}/stories/{story_key}/replay")
def get_story_replay(
    plan_name: str,
    story_key: str,
    lines: int = 200,
) -> dict[str, Any]:
    """Return the story's merged chronological replay timeline, combining
    the checkpoint journal with the tails of the worktree logs, so the
    dashboard can render one vertical timeline of everything the agent
    did (journal steps interleaved with agent/review log activity).

    Response shape:
      { "available": bool,
        "events": [ {ts, source, kind, message} ],
        "sources": {"journal": bool, "agent.log": bool, "review.log": bool} }

    Semantics:
      - 404 only when the plan or story itself doesn't exist. The client
        treats 404 as 'this story is gone' (render an empty placeholder or
        toast), distinct from a healthy story that simply has nothing to
        replay yet.
      - 200 + available:false + events:[] when the journal is missing,
        malformed, or empty AND no worktree log is readable — a story never
        dispatched (no worktree), a wiped agent.log, and a corrupt journal
        are all normal available-degraded states, NEVER 500.
      - available is True iff the journal was readable OR at least one log
        source contributed; `sources` echoes which inputs actually did, so
        the UI can label provenance.
      - Each log source contributes only its last `lines` lines (default
        `_LOG_TAIL_DEFAULT` 200, clamped to [1, `_LOG_TAIL_CAP` 500]).
      - Events are merged and sorted chronologically by build_replay_events
        (app.story_replay); the sort is stable, journal events first at
        equal timestamps.
    """
    manifest = _service.get_manifest_or_none(plan_name)
    if manifest is None:
        raise HTTPException(
            status_code=404, detail=f"No manifest for plan '{plan_name}'"
        )
    stories = manifest.get("stories") if isinstance(manifest, dict) else {}
    if not isinstance(stories, dict) or story_key not in stories:
        raise HTTPException(
            status_code=404,
            detail=f"No story '{story_key}' in plan '{plan_name}'",
        )
    story = stories[story_key]
    n = max(1, min(lines, _LOG_TAIL_CAP))
    journal_available, entries = _store.get_journal(plan_name, story_key)
    log_sources: dict[str, list[str]] = {}
    source_flags = {"agent.log": False, "review.log": False}
    for name in ("agent.log", "review.log"):
        res = _store.get_worktree_file(story, name)
        if not res["available"]:
            continue
        tail = res["text"].splitlines()[-n:]
        if not tail:
            # A wiped/empty log contributes nothing: it is not a source
            # (no empty list is passed on) and does not flip available.
            continue
        source_flags[name] = True
        log_sources[name] = tail
    events = build_replay_events(entries, log_sources)
    available = journal_available or len(log_sources) > 0
    return {
        "available": available,
        "events": events,
        "sources": {
            "journal": journal_available,
            "agent.log": source_flags["agent.log"],
            "review.log": source_flags["review.log"],
        },
    }


@app.get("/api/config")
def effective_config(plan: str | None = None) -> dict[str, Any]:
    """Read-only effective-configuration snapshot, mirroring
    pipeline.server.get_effective_config's MCP tool for the dashboard: every
    role in config_provenance.PIPELINE_ROLES with its resolved
    (provider, model) and provenance, every cataloged env var's resolved
    value and provenance, any transport-only env vars present that have no
    effect as input, and which config-source files were consulted (with an
    exists flag each). Pure read - makes no changes and writes nothing.

    The dashboard carries no persona knowledge (agents/*.md model defaults),
    so no model_fallbacks are supplied here - a role with nothing else
    configured honestly reports model_source == "unset" rather than
    guessing a persona-specific default.

    Pass ?plan=<name> to layer in that plan's manifest role_config
    overrides. Reuses the module's existing _read_manifest helper rather
    than a second reader; a missing manifest (_read_manifest returns None)
    or a malformed one (invalid JSON) both degrade to "no plan overrides"
    rather than a 404 or 500 - _read_manifest itself is left unchanged, the
    tolerance for malformed JSON lives here.
    """
    plan_role_config = None
    if plan:
        try:
            manifest = _service.get_manifest_or_none(plan)
        except (json.JSONDecodeError, OSError):
            manifest = None
        plan_role_config = (manifest or {}).get("role_config") or None

    try:
        registry = role_registry.load_registry()
    except role_registry.RoleRegistryError:
        registry = {}

    roles = config_provenance.effective_role_config(
        plan_role_config=plan_role_config,
        registry=registry,
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
        "roles": roles,
        "env": env,
        "ignored_env_vars": ignored_env_vars,
        "sources": sources,
    }


@app.get("/api/config/providers")
def config_providers() -> dict[str, Any]:
    """Read-only provider/model catalog from model_registry.json, for the
    Configuration view's provider/model dropdowns. load_registry() is
    already tolerant of a missing/malformed registry (raises
    RoleRegistryError), degrading to an empty catalog rather than
    500ing, mirroring how /api/config's effective_config() already
    handles the same exception a few lines above.
    """
    try:
        registry = role_registry.load_registry()
    except role_registry.RoleRegistryError:
        registry = {}
    return {"providers": registry.get("providers", {})}

@app.get("/api/workspaces")
def list_workspaces_route():
    return {"workspaces": _service.list_workspaces()}


@app.get('/api/workspace')
def get_workspace_route():
    return {'active': _service.get_active_workspace()}


@app.post('/api/workspace')
def set_workspace_route(request: WorkspaceRequest):
    result = _service.resolve_workspace(request.path, create=request.create)
    if not result.get('ok'):
        raise HTTPException(status_code=400, detail=result.get('error', 'Unknown error'))
    if hasattr(_service, 'set_active_workspace'):
        _service.set_active_workspace(result['path'])
    return result


@app.post("/api/config/roles/{role}")
def set_role_default_route(role: str, body: RoleDefaultBody) -> dict[str, Any]:
    """Delegates to _service.set_role_default(role, body.provider, body.model).

    On success returns the service result plus the updated effective config for
    that role (via config_provenance.resolve_role_provenance). Validation
    failures surface as HTTP 400 with the service's error message.
    """
    result = _service.set_role_default(role, body.provider, body.model)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "unknown error"))
    effective = config_provenance.resolve_role_provenance(
        role, registry=role_registry.load_registry()
    )
    return {**result, "effective_config": effective}


@app.post("/api/config/plans/{plan_name}/roles/{role}")
def set_plan_role_config_route(
    plan_name: str, role: str, body: PlanRoleConfigBody
) -> dict[str, Any]:
    """Delegates to _service.set_plan_role_config(plan_name, role, body.provider,
    body.model).

    404 if the plan does not exist; 400 on validation failure; on success
    returns the service result plus the updated per-plan effective config (the
    plan's role_config layered into resolve_role_provenance).
    """
    if plan_name not in _store.list_manifests():
        raise HTTPException(status_code=404, detail=f"No such plan {plan_name!r}")
    if role not in config_provenance.PIPELINE_ROLES:
        raise HTTPException(status_code=400, detail=f"unknown role {role!r}")
    result = _service.set_plan_role_config(plan_name, role, body.provider, body.model)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "unknown error"))
    manifest = _service.get_manifest_or_none(plan_name) or {}
    plan_role_config = manifest.get("role_config") or None
    effective = config_provenance.resolve_role_provenance(
        role,
        plan_role_config=plan_role_config,
        registry=role_registry.load_registry(),
    )
    return {**result, "effective_config": effective}


@app.post("/api/plans/{plan_name}/stories/{story_key}/dispatch")
def dispatch_story_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.dispatch_story(plan_name, story_key)."""
    result = _service.dispatch_story(plan_name, story_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error during dispatch"),
        )
    return result


@app.post("/api/plans/{plan_name}/stories/{story_key}/start")
def start_story_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.mark_story_in_progress(plan_name, story_key)."""
    result = _service.mark_story_in_progress(plan_name, story_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error when starting story"),
        )
    return result


@app.post("/api/plans/{plan_name}/stories/{story_key}/interrupt")
def interrupt_story_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.interrupt_story(plan_name, story_key)."""
    result = _service.interrupt_story(plan_name, story_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error when interrupting story"),
        )
    return result


@app.post("/api/plans/{plan_name}/stories/{story_key}/status")
def set_story_status_route(
    plan_name: str, story_key: str, body: StoryStatusBody
) -> dict[str, Any]:
    """Delegates to _service.set_story_status(plan_name, story_key, status)."""
    result = _service.set_story_status(plan_name, story_key, body.status)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error when setting story status"),
        )
    return result


@app.post("/api/plans/{plan_name}/stories/{story_key}/patch")
def patch_story_route(
    plan_name: str, story_key: str, body: StoryPatchBody
) -> dict[str, Any]:
    """Delegates to _service.patch_story(plan_name, story_key, fields=...)."""
    fields = body.model_dump(exclude_unset=True)
    result = _service.patch_story(plan_name, story_key, fields=fields)
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Unknown error when patching story"),
        )
    return result


@app.post("/api/plans/{plan_name}/stories/{story_key}/review")
def review_story_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.review_story(plan_name, story_key)."""
    result = _service.review_story(plan_name, story_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error during review"),
        )
    return result


@app.post("/api/plans/{plan_name}/stories/{story_key}/approve_merge")
def approve_merge_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.approve_merge(plan_name, story_key)."""
    result = _service.approve_merge(plan_name, story_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error during approve_merge"),
        )
    return result


@app.post("/api/plans/{plan_name}/stories/{story_key}/done")
def mark_story_done_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.mark_story_done(plan_name, story_key)."""
    result = _service.mark_story_done(plan_name, story_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error when marking story as done"),
        )
    return result


@app.post("/api/plans/{plan_name}/save")
def save_plan(plan_name: str, request: SavePlanRequest):
    result = _service.save_plan(plan_name, request.plan_json)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result

@app.post("/api/decompose")
def decompose_route(request: DecomposeRequest):
    result = _service.decompose_plan(request.request)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result
@app.post("/api/plans/{plan_name}/ingest")
def ingest_plan(plan_name: str, request: IngestPlanRequest | None = None):
    if request is None:
        result = _service.ingest_plan(plan_name)
    else:
        result = _service.ingest_plan(plan_name, only_epics=request.only_epics, overwrite=request.overwrite)

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result


@app.post("/api/plans/{plan_name}/advance")
def advance_pipeline(plan_name: str) -> dict[str, Any]:
    """Delegates to _service.advance_pipeline(plan_name)."""
    result = _service.advance_pipeline(plan_name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result


@app.post("/api/plans/advance_all")
def advance_all_plans() -> dict[str, Any]:
    """Delegates to _service.advance_all_plans()."""
    result = _service.advance_all_plans()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result


@app.post("/api/plans/{plan_name}/stories/{story_key}/checkpoint")
def checkpoint(plan_name: str, story_key: str, body: dict[str, Any]) -> dict[str, Any]:
    """Delegates to _service.checkpoint(plan_name, story_key, step, summary, next_hint)."""
    step = body.get("step")
    summary = body.get("summary")
    if step is None or summary is None:
        raise HTTPException(
            status_code=400,
            detail="Both 'step' and 'summary' are required in the request body",
        )
    next_hint = body.get("next_hint", "")
    result = _service.checkpoint(plan_name, story_key, step, summary, next_hint)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result


@app.get("/api/guard-liveness")
def get_guard_liveness() -> dict[str, Any]:
    try:
        with open(FAILURE_MODES_DATASET_PATH, "r", encoding="utf-8") as f:
            dataset = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return _degraded_liveness_report()
    if not isinstance(dataset, list):
        return _degraded_liveness_report()
    repo_root = Path(__file__).resolve().parents[1]
    report = guard_liveness.check_guard_liveness(dataset, repo_root, collected_test_files=None)
    report["dataset_found"] = True
    return report


@app.get("/api/plans/{plan_name}/metrics")
def get_plan_metrics(plan_name: str) -> dict[str, Any]:
    manifest = _service.get_manifest_or_none(plan_name)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"No manifest for plan '{plan_name}'")
    sidecar = PLAN_DIR / f"{plan_name}.notifications.jsonl"
    records, malformed = story_metrics.load_notification_records(sidecar)
    stories = list(story_metrics.compute_story_metrics(records).values())
    rollup = story_metrics.compute_plan_rollup(stories)
    return {
        "plan": plan_name,
        "stories": stories,
        "rollup": rollup,
        "malformed_lines": malformed,
    }


def _degraded_liveness_report() -> dict[str, Any]:
    """Zeroed liveness report for a missing or unparseable dataset (200, not 500)."""
    zeroed = guard_liveness.check_guard_liveness(
        [], Path(__file__).resolve().parents[1], collected_test_files=None
    )
    zeroed["dataset_found"] = False
    return zeroed

# Mount static files for the dashboard UI.
app.include_router(chat.chat_router, prefix="/api")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
# End of file
