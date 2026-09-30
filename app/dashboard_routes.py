"""Read-only plan/story/config inspection routes for app/dashboard.py.

Pulled out of dashboard.py (which re-exports every name here and registers
each on its ``app``) purely to shrink that file. The handler bodies are unchanged.

The names the test suite monkeypatches on ``app.dashboard`` (``_service``,
``collect_story_wedge_signals``, ``wedge_verdict``) are resolved through
that module at call time, so patching ``app.dashboard.<name>`` still
reaches these routes; ``app.dashboard`` is imported lazily to avoid a cycle.
"""
from __future__ import annotations

import importlib
import json
import logging
from typing import Any

from fastapi import HTTPException

from app import role_registry
from app.dashboard_helpers import (
    _LOG_TAIL_CAP,
    _LOG_TAIL_DEFAULT,
    _collapse_duplicate_notifications,
    _parse_progress,
    _plan_summary,
    _store,
    _story_last_activity,
)
from app.story_replay import build_replay_events
from pipeline import config_provenance
from pipeline.config import WEDGE_STALE_ACTIVITY_SECONDS

# Same logger name as app.dashboard so log filtering and caplog are unchanged.
logger = logging.getLogger("app.dashboard")

def _dashboard():
    return importlib.import_module("app.dashboard")


class _DashboardServiceProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_dashboard()._service, name)


_service = _DashboardServiceProxy()


def collect_story_wedge_signals(*args: Any, **kwargs: Any) -> Any:
    return _dashboard().collect_story_wedge_signals(*args, **kwargs)


def wedge_verdict(*args: Any, **kwargs: Any) -> Any:
    return _dashboard().wedge_verdict(*args, **kwargs)


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
                # Forward the completion-marker flag only when the collector
                # reports it: legacy 2-key signal dicts keep the exact
                # 3-argument wedge_verdict call pinned by the decoration
                # contract (tests/unit/test_dashboard_wedge_decoration.py).
                extra_kwargs = (
                    {"agent_done": signals["agent_done"]}
                    if "agent_done" in signals
                    else {}
                )
                verdict = wedge_verdict(
                    signals["pid_alive"],
                    signals["activity_age_seconds"],
                    WEDGE_STALE_ACTIVITY_SECONDS,
                    **extra_kwargs,
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
        if name == "agent.log":
            # AGENTLOGTS-1's sidecar: one ISO-8601 timestamp per
            # agent.log line, same order. Zip the matching tail of it
            # onto `tail` so build_replay_events' existing
            # _split_leading_timestamp parses real timestamps instead
            # of rendering every line as an untimed event. Fail open:
            # a missing/short/corrupt sidecar (older worktree from
            # before this feature, or a story this format never
            # reached) leaves `tail` exactly as it is today.
            ts_res = _store.get_worktree_file(story, "agent.log.ts")
            if ts_res["available"]:
                ts_lines = ts_res["text"].splitlines()
                ts_tail = ts_lines[-len(tail):]
                if len(ts_tail) == len(tail):
                    tail = [
                        f"{ts} {line}" for ts, line in zip(ts_tail, tail)
                    ]
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
