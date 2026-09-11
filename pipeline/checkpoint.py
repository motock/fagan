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
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .git_ops import _commit_wip
from .parsers import _atomic_write_json
from .persistence import _append_journal

# last_log_line is truncated to this many characters before it is journalled.
_MAX_LOG_LINE = 300
# Explicit bound on the evidence git call: an unbounded git invocation here
# could stall the watchdog's own termination path (the exact failure class
# the watchdog exists to catch).
_EVIDENCE_GIT_TIMEOUT_SECONDS = 10


def _last_log_line(worktree: Any) -> str | None:
    """Last non-empty line of <worktree>/agent.log, truncated to 300 chars.

    Dispatch-watchdog evidence for the journal entry. Fails open: any error
    (missing worktree, unreadable log, decode problem) returns None and the
    field is simply omitted from the entry.
    """
    try:
        log_path = os.path.join(str(worktree), "agent.log")
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        for line in reversed(lines):
            if line.strip():
                return line[:_MAX_LOG_LINE]
        return None
    except Exception:  # noqa: BLE001 (fail-open by design: evidence must never gate the watchdog)
        return None


def _branch_commit_count(worktree: Any) -> int | None:
    """Commits on the worktree's checked-out branch ahead of origin's
    default branch, as an int (read-only rev-list, bounded by an explicit
    timeout). Fails open: returns None on any error so the watchdog's
    termination path is never disturbed."""
    try:
        if not worktree or not os.path.isdir(str(worktree)):
            return None
        result = subprocess.run(
            ["git", "rev-list", "--count", "origin/HEAD..HEAD"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=_EVIDENCE_GIT_TIMEOUT_SECONDS,
            check=True,
        )
        return int(result.stdout.strip())
    except Exception:  # noqa: BLE001 (fail-open by design: evidence must never gate the watchdog)
        return None


def _agent_done_marker(worktree: Any) -> bool | None:
    """Whether <worktree>/.agent_done exists. None only when the worktree
    itself is missing (or the probe fails) — a present worktree without the
    marker is a meaningful False, not a failed lookup."""
    try:
        if not worktree or not os.path.isdir(str(worktree)):
            return None
        return os.path.exists(os.path.join(str(worktree), ".agent_done"))
    except Exception:  # noqa: BLE001 (fail-open by design: evidence must never gate the watchdog)
        return None


def _watchdog_evidence(story: dict[str, Any], story_key: str) -> dict[str, Any]:
    """Completion evidence for the dispatch_watchdog_timeout journal entry.

    Each lookup is wrapped individually (one failing lookup must not
    suppress the others) and every lookup fails open: a field whose source
    is unavailable is simply absent from the entry. This must stay
    stateless — evidence is computed fresh on every call.
    """
    worktree = story.get("worktree")
    evidence: dict[str, Any] = {}
    last_log_line = _last_log_line(worktree)
    if last_log_line is not None:
        evidence["last_log_line"] = last_log_line
    branch_commit_count = _branch_commit_count(worktree)
    if branch_commit_count is not None:
        evidence["branch_commit_count"] = branch_commit_count
    agent_done_marker = _agent_done_marker(worktree)
    if agent_done_marker is not None:
        evidence["agent_done_marker"] = agent_done_marker
    return evidence


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
    except (ProcessLookupError, PermissionError):
        pass

    sha = _commit_wip(story["worktree"], story_key, step, guard_against_deletion=True)
    interrupted_at = datetime.now(timezone.utc).isoformat()
    record = {
        "step": step,
        "summary": summary,
        "next_hint": "",
        "commit": sha,
        "ts": interrupted_at,
    }
    # Completion evidence (LOCKSTARVE-D2): distinguish a run that actually
    # finished (last log line, branch commits, .agent_done) from one that
    # genuinely stalled. Each lookup fails open individually and the fields
    # are added only when their lookup succeeded, so this can never change
    # the termination outcome. Journal-only: never logged, never notified.
    # Scoped to the watchdog entry — the manual interrupt path shares this
    # function and keeps its existing contract.
    if step == "dispatch_watchdog_timeout":
        record.update(_watchdog_evidence(story, story_key))
    _append_journal(plan_name, story_key, record)

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
    from .server import PLAN_DIR, _validate_key
    _validate_key(plan_name)
    _validate_key(story_key)
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