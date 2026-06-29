"""Read-only monitoring dashboard for the agent pipeline.

A small FastAPI app, separate from pipeline_mcp_server.py (the MCP server
the orchestrator drives). It only reads the plan/manifest/notification/
decision files PLAN_DIR already holds - it never dispatches, advances, or
mutates anything, so it carries none of the pipeline's risk surface.

Run with:  uvicorn dashboard:app --reload
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

PLAN_DIR = Path(os.environ.get("PLAN_DIR", "~/.claude/plans")).expanduser()
USAGE_STATE_PATH = Path(
    os.environ.get("USAGE_STATE_PATH", "~/.claude/usage_state.json")
).expanduser()
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Agent Pipeline Dashboard")


def _manifest_path(plan_name: str) -> Path:
    return PLAN_DIR / f"{plan_name}.manifest.json"


def _notifications_path(plan_name: str) -> Path:
    return PLAN_DIR / f"{plan_name}.notifications.log"


def _decisions_path(plan_name: str) -> Path:
    return PLAN_DIR / f"{plan_name}.decisions.json"


def _list_plan_names() -> list[str]:
    suffix = ".manifest.json"
    return sorted(p.name[: -len(suffix)] for p in PLAN_DIR.glob(f"*{suffix}"))


def _read_manifest(plan_name: str) -> dict[str, Any] | None:
    path = _manifest_path(plan_name)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _status_counts(stories: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for story in stories.values():
        status = story.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _tail_notifications(plan_name: str, limit: int = 100) -> list[str]:
    path = _notifications_path(plan_name)
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    return lines[-limit:]


def _read_decisions(plan_name: str) -> list[dict[str, Any]]:
    path = _decisions_path(plan_name)
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _plan_summary(plan_name: str, manifest: dict[str, Any]) -> dict[str, Any]:
    stories = manifest.get("stories", {})
    return {
        "name": plan_name,
        "paused": bool(manifest.get("paused", False)),
        "story_count": len(stories),
        "status_counts": _status_counts(stories),
    }


# Staleness threshold (minutes) for the in_progress "aged" indicator on
# cards. Anything older than this with status=in_progress is presumed to
# have stalled the agent and gets a muted warning style in the UI.
STALE_IN_PROGRESS_MINUTES = 30


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; tolerate a trailing 'Z' as UTC.

    Returns None if ts is falsy, not a string, or not parseable. The
    dashboard never raises on malformed timestamps — a bad row should
    drop out of staleness reporting rather than 500 the endpoint."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        # datetime.fromisoformat in 3.11+ accepts trailing 'Z'; older
        # versions need it swapped for an explicit +00:00 offset.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _journal_final_ts(plan_name: str, story_key: str) -> str | None:
    """Return the ISO timestamp of the last entry in the story's
    checkpoint journal, or None if the journal is missing/empty/unreadable.

    The journal file is named '<plan>.<story_key>.journal.json' and
    contains a list of checkpoint records; only the last entry's 'ts'
    is consulted, because that's the most recent observable activity
    the agent left on disk before being interrupted/resumed."""
    path = PLAN_DIR / f"{plan_name}.{story_key}.journal.json"
    if not path.exists():
        return None
    try:
        entries = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(entries, list) or not entries:
        return None
    final = entries[-1]
    return final.get("ts") if isinstance(final, dict) else None


def _story_last_activity(plan_name: str, story_key: str, story: dict[str, Any]) -> str | None:
    """Derive the most recent observable activity timestamp for a story.

    Priority (latest signal wins):
      1. The story's checkpoint journal final-entry timestamp, if present.
      2. story['last_commit'].
      3. story['interrupted_at'].
    Returns the latest by value (ISO-8601 strings sort chronologically
    when all use the same offset), or None if no signal is available.

    This derivation is read-only — we never write back to the manifest
    or journal. The dashboard computes it on every /api/plans/{name}
    request so the age stays current as the clock advances on the
    client (UI computes age_seconds = now - last_activity client-side)."""
    candidates: list[str] = []

    journal_ts = _journal_final_ts(plan_name, story_key)
    if isinstance(journal_ts, str) and journal_ts:
        candidates.append(journal_ts)

    for field in ("last_commit", "interrupted_at"):
        val = story.get(field)
        if isinstance(val, str) and val:
            candidates.append(val)

    if not candidates:
        return None
    return max(candidates)


# A story is considered "dispatched" if it ever made it past the todo state.
# This includes interrupted, pr_open, parked/changes_requested/failed, and
# done — i.e. the orchestrator tried it. Stories with a `backend` field set
# are also counted even if status is still todo, since the backend was
# resolved (escalation can flip backend on a still-todo story).
_DISPATCHED_STATUSES = frozenset({
    "in_progress", "interrupted", "pr_open", "changes_requested",
    "parked", "failed", "done",
})


def _story_outcome(story: dict[str, Any]) -> dict[str, Any]:
    """Bucket a single story into one of the headline outcomes."""
    status = story.get("status")
    dispatched = status in _DISPATCHED_STATUSES or bool(story.get("backend"))
    return {
        "dispatched": dispatched,
        "done": status == "done",
        "escalated": bool(story.get("escalated")),
    }


def _acceptance_slice(stories: dict[str, Any]) -> dict[str, Any]:
    """Roll up the two headline slices — stories whose plan carried an
    `acceptance` block vs those that didn't — across every manifest.

    The headline number is `escalation_rate` = escalated / dispatched. With
    Fix #1's acceptance-oracle harness in place, we expect the
    `with_acceptance` slice's rate to fall relative to `without_acceptance`
    over the next week of fleet runs."""
    with_acc = {"dispatched": 0, "done": 0, "escalated": 0, "stories": 0}
    without_acc = {"dispatched": 0, "done": 0, "escalated": 0, "stories": 0}
    for story in stories.values():
        has_acceptance = bool(story.get("acceptance"))
        bucket = with_acc if has_acceptance else without_acc
        bucket["stories"] += 1
        out = _story_outcome(story)
        if out["dispatched"]:
            bucket["dispatched"] += 1
        if out["done"]:
            bucket["done"] += 1
        if out["escalated"]:
            bucket["escalated"] += 1

    def _rates(b: dict[str, int]) -> dict[str, Any]:
        d = b["dispatched"]
        return {
            **b,
            "escalation_rate": (b["escalated"] / d) if d else 0.0,
            "success_rate": (b["done"] / d) if d else 0.0,
        }

    return {"with_acceptance": _rates(with_acc),
            "without_acceptance": _rates(without_acc)}


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
    for name in _list_plan_names():
        manifest = _read_manifest(name)
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
    d_count = totals["dispatched"]
    return {
        "totals": {
            **totals,
            "escalation_rate": (totals["escalated"] / d_count) if d_count else 0.0,
            "success_rate": (totals["done"] / d_count) if d_count else 0.0,
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
def list_plans() -> dict[str, Any]:
    plans = []
    for name in _list_plan_names():
        manifest = _read_manifest(name)
        if manifest is None:
            continue
        plans.append(_plan_summary(name, manifest))
    return {"plans": plans}


@app.get("/api/plans/{plan_name}")
def get_plan(plan_name: str) -> dict[str, Any]:
    manifest = _read_manifest(plan_name)
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
        decorated_stories[story_key] = {**story, "last_activity": last_activity}
    summary = _plan_summary(plan_name, manifest)
    return {
        **summary,
        "epics": manifest.get("epics", {}),
        "stories": decorated_stories,
        "notifications": _tail_notifications(plan_name),
        "decisions": _read_decisions(plan_name),
    }


# Mounted last so it never shadows the /api/* routes above; html=True serves
# static/index.html for "/".
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
