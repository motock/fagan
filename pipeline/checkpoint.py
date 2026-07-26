"""Checkpoint helpers for the pipeline MCP server.

_terminate_and_checkpoint SIGTERMs a dispatched process, commits its
worktree as a WIP checkpoint, journals the event, and marks the story
interrupted. _checkpoint_impl is the shared checkpoint logic used by both
the `checkpoint` MCP tool and the local dispatch agent loop.

Both read PLAN_DIR via a lazy import from the server (circular-avoidance;
tests patch p.PLAN_DIR which the plan_dir fixture also mirrors to
pipeline_persistence / pipeline_concurrency).
"""

import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .git_ops import _commit_wip
from .parsers import _atomic_write_json
from .persistence import _append_journal


def _terminate_and_checkpoint(
    manifest: dict[str, Any], manifest_path: Path, plan_name: str, story_key: str,
    story: dict[str, Any], *, pid: int, step: str, summary: str,
) -> str:
    """SIGTERM the dispatched process, checkpoint its worktree, journal the
    event, and mark the story interrupted (dispatch-eligible for resume).
    Shared by interrupt_story (manual) and check_story_status's dispatch
    watchdog (automatic, on a hung process past DISPATCH_WATCHDOG_SECONDS)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    sha = _commit_wip(story["worktree"], story_key, step, guard_against_deletion=True)
    interrupted_at = datetime.now(timezone.utc).isoformat()
    _append_journal(plan_name, story_key, {
        "step": step,
        "summary": summary,
        "next_hint": "",
        "commit": sha,
        "ts": interrupted_at,
    })

    story["status"] = "interrupted"
    story["last_commit"] = sha
    story["interrupted_at"] = interrupted_at
    _atomic_write_json(manifest_path, manifest)
    return sha


def _checkpoint_impl(
    plan_name: str, story_key: str, step: str, summary: str, next_hint: str = "",
) -> dict[str, Any]:
    """Checkpoint logic, factored out of the `checkpoint` tool so it can be
    reused directly by the local dispatch agent loop (scripts/local_agent.py
    calls this in-process for its `checkpoint` tool) without exposing this
    whole server's orchestration toolset (dispatch_story, approve_merge,
    advance_pipeline, ...) to a dispatched agent."""
    from .server import PLAN_DIR
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    story = manifest["stories"].get(story_key)
    if not story:
        return {"ok": False, "error": f"No such story {story_key}"}

    sha = _commit_wip(story["worktree"], story_key, step)
    record = {
        "step": step,
        "summary": summary,
        "next_hint": next_hint,
        "commit": sha,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _append_journal(plan_name, story_key, record)
    return {"ok": True, **record}


__all__ = [
    "_checkpoint_impl",
    "_terminate_and_checkpoint",
]