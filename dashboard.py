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
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

PLAN_DIR = Path(os.environ.get("PLAN_DIR", "~/.claude/plans")).expanduser()
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


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "plan_dir": str(PLAN_DIR)}


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
    summary = _plan_summary(plan_name, manifest)
    return {
        **summary,
        "epics": manifest.get("epics", {}),
        "stories": stories,
        "notifications": _tail_notifications(plan_name),
        "decisions": _read_decisions(plan_name),
    }


# Mounted last so it never shadows the /api/* routes above; html=True serves
# static/index.html for "/".
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
