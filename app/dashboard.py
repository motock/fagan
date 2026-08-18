"""Monitoring dashboard for the agent pipeline.

A small FastAPI app, separate from pipeline_mcp_server.py (the MCP server
the orchestrator drives). It provides read access to the plan/manifest/
notification/decision files in PLAN_DIR, and provides write access to
the pipeline via a `PipelineService` instance (`_service`). Later stories
in this epic add HTTP routes that delegate to `_service`.

Run with:  uvicorn dashboard:app --reload
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict


class SavePlanRequest(BaseModel):
    plan_json: str


class IngestPlanRequest(BaseModel):
    only_epics: list[str] | None = None
    overwrite: bool = False

# config_provenance is a read-only leaf: its only non-stdlib import is
# app.role_registry (see both modules' docstrings), so pulling it in does
# NOT drag the orchestrator's write surface (pipeline.server /
# app.pipeline_mcp_server / app.backend) into the dashboard's import graph.
from app import role_registry
from pipeline import config_provenance
from pipeline.server import PipelineService

PLAN_DIR = Path(os.environ.get("PLAN_DIR", "~/.claude/plans")).expanduser()
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
def _manifest_path(plan_name: str) -> Path:
    return PLAN_DIR / f"{plan_name}.manifest.json"


def _notifications_path(plan_name: str) -> Path:
    return PLAN_DIR / f"{plan_name}.notifications.log"
def _notifications_jsonl_path(plan_name: str) -> Path:
    return PLAN_DIR / f"{plan_name}.notifications.jsonl"

def _decisions_path(plan_name: str) -> Path:
    return PLAN_DIR / f"{plan_name}.decisions.json"


def _journal_path(plan_name: str, story_key: str) -> Path:
    """Path to a story's checkpoint journal (mirrors pipeline_mcp_server.py).

    The same naming convention is used by both processes: pipeline_mcp_server
    *writes* `<plan>.<story_key>.journal.json` and the dashboard *reads* it
    via /api/plans/{plan}/stories/{story}/journal. Locking the naming here
    keeps the two sides from drifting silently to a 404 in the UI.
    """
    return PLAN_DIR / f"{plan_name}.{story_key}.journal.json"


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


def _aggregate_stories(stories: dict[str, Any]) -> dict[str, Any]:
    """Roll up attempt counters and failure reasons across a plan's stories.

    Field semantics (mirroring pipeline_mcp_server.py):
      - dispatch_attempts: how many times the orchestrator picked up the story
      - rework_attempts:   how many times a PR was sent back for changes
      - merge_attempts:    how many merge attempts against the base branch
      - escalated:         True if the overlord escalated the story
      - failure_reason:    free-text bucket the pipeline recorded on failure
      - backend:           'local' or 'claude' once the orchestrator resolved one

    Robustness contract: stories that are missing any of these fields
    (or carry null values) contribute 0 to the relevant bucket. The rollup
    never raises KeyError — a story that's still a bare {status, summary}
    shell from before any dispatch attempt must not break /api/plans.

    Failure-reason bucketing: an empty string ('') is treated as the
    '(none)' bucket so the UI never renders a literal blank reason chip.
    """
    dispatch = 0
    rework = 0
    merge = 0
    escalated = 0
    failure_reasons: dict[str, int] = {}
    by_backend: dict[str, int] = {}

    def _safe_int(value: Any) -> int:
        if isinstance(value, bool):  # bool is a subclass of int — guard.
            return int(value)
        if isinstance(value, int):
            return value
        return 0

    for story in stories.values():
        if not isinstance(story, dict):
            continue
        dispatch += _safe_int(story.get("dispatch_attempts"))
        rework += _safe_int(story.get("rework_attempts"))
        merge += _safe_int(story.get("merge_attempts"))
        if story.get("escalated") is True:
            escalated += 1
        backend = story.get("backend")
        if isinstance(backend, str) and backend:
            by_backend[backend] = by_backend.get(backend, 0) + 1
        reason = story.get("failure_reason")
        # Only bucket stories that actually attempted (a still-todo story
        # with no failure_reason set contributes to '(none)' so the empty
        # state isn't silently invisible to the rollup).
        if isinstance(reason, str):
            key = reason if reason else "(none)"
        else:
            key = "(none)"
        failure_reasons[key] = failure_reasons.get(key, 0) + 1

    return {
        "dispatch_attempts": dispatch,
        "rework_attempts": rework,
        "merge_attempts": merge,
        "escalated": escalated,
        "failure_reasons": failure_reasons,
        "by_backend": by_backend,
    }


def _tail_notifications(plan_name: str, limit: int = 100) -> list[str]:
    path = _notifications_path(plan_name)
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    return lines[-limit:]
def _tail_notification_records(plan_name: str, limit: int = 100) -> list[dict[str, Any]]:
    path = _notifications_jsonl_path(plan_name)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        ts = str(obj.get("ts", ""))
        message = str(obj.get("message", ""))
        severity = obj.get("severity")
        if severity not in ("info", "warning", "error"):
            severity = "info"
        else:
            severity = str(severity)
        story_key = obj.get("story_key")
        event = obj.get("event")
        dedup_key = obj.get("dedup_key")
        records.append({
            "ts": ts,
            "message": message,
            "severity": severity,
            "story_key": story_key,
            "event": event,
            "dedup_key": dedup_key,
        })
    return records[-limit:]

def _collapse_duplicate_notifications(records: list[dict]) -> list[dict]:
    if not records:
        return []
    result: list[dict] = []
    for rec in records:
        entry = {**rec}
        # ensure count and last_ts present
        entry["count"] = 1
        entry["last_ts"] = entry["ts"]
        if (
            result
            and entry.get("dedup_key")
            and result[-1].get("dedup_key")
            and entry["dedup_key"] == result[-1]["dedup_key"]
        ):
            prev = result[-1]
            if "count" not in prev:
                prev["count"] = 1
                prev["last_ts"] = prev["ts"]
            prev["count"] += 1
            prev["last_ts"] = entry["ts"]
            prev["message"] = entry["message"]
            prev["severity"] = entry["severity"]
        else:
            result.append(entry)
    return result


def _read_decisions(plan_name: str) -> list[dict[str, Any]]:
    path = _decisions_path(plan_name)
    if not path.exists():
        return []
    return json.loads(path.read_text())


# Default line cap when the modal fetches the log tail. Capped at
# _LOG_TAIL_CAP so an accidental giant log file never gets slurped into
# a single HTTP response. UI sends ?lines=N to override the default
# within the [1, _LOG_TAIL_CAP] envelope.
_LOG_TAIL_DEFAULT = 200
_LOG_TAIL_CAP = 500


def _read_story_log(
    plan_name: str,
    story_key: str,
    manifest: dict[str, Any],
    lines: int = _LOG_TAIL_DEFAULT,
) -> dict[str, Any]:
    """Tail the on-disk log recorded in story['log'] for the redesigned
    modal. Returns {"available": bool, "lines": [str...]}.

    Hard-restricted to story['log'] in the manifest: a missing or absent
    'log' field is a normal state and degrades to available=False; a
    recorded log whose file is missing on disk (deleted, never written)
    also degrades to available=False. Either path MUST NOT raise 500,
    because the in-modal log viewer needs to render an "No log available"
    empty state regardless of why the file isn't there.

    The recorded path is resolved under PLAN_DIR and the resolved target
    is contained-checked to PLAN_DIR so a manifest that was hand-edited
    with `../outside.log` cannot be used to read arbitrary files. We do
    NOT honour a path supplied by the request: only the manifest is
    trusted.

    Non-UTF-8 bytes are decoded with errors='replace' so the dashboard
    can still surface whatever was on disk rather than 500'ing on a
    binary log line.
    """
    if not isinstance(lines, int) or lines < 1:
        lines = _LOG_TAIL_DEFAULT
    lines = min(lines, _LOG_TAIL_CAP)

    stories = manifest.get("stories") if isinstance(manifest, dict) else None
    if not isinstance(stories, dict):
        return {"available": False, "lines": []}

    story = stories.get(story_key)
    if not isinstance(story, dict):
        # Plan exists but story key does not. The route layer turns this
        # into 404 before we ever get here; defended for safety only.
        return {"available": False, "lines": []}

    raw_log = story.get("log")
    if not isinstance(raw_log, str) or not raw_log:
        return {"available": False, "lines": []}

    # Manifest stores paths relative to PLAN_DIR historically; also
    # accept absolute paths but contain-check them so a hand-edited
    # manifest pointing outside PLAN_DIR cannot read arbitrary files.
    log_path = Path(raw_log)
    if not log_path.is_absolute():
        log_path = PLAN_DIR / raw_log

    try:
        log_path = log_path.resolve(strict=False)
        plan_dir_resolved = PLAN_DIR.resolve()
        # Path.is_relative_to (3.9+) — also works for the equal-root edge
        # case. We require the resolved log to live strictly under PLAN_DIR
        # so an empty/equal path cannot be smuggled in.
        if log_path != plan_dir_resolved and not log_path.is_relative_to(plan_dir_resolved):
            return {"available": False, "lines": []}
    except OSError:
        return {"available": False, "lines": []}

    if not log_path.exists() or not log_path.is_file():
        return {"available": False, "lines": []}

    try:
        # errors="replace" so binary garbage in a log file degrades to a
        # U+FFFD-per-byte line rather than 500'ing the endpoint. The UI
        # escapes the result, so replacement chars render harmlessly.
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"available": False, "lines": []}

    all_lines = text.splitlines()
    tail = all_lines[-lines:]
    return {"available": True, "lines": tail}


def _read_worktree_file(story: dict[str, Any], filename: str) -> dict[str, Any]:
    """Read a named agent artifact (e.g. .agent_plan.md, .agent_scratchpad.md)
    from a dispatched story's worktree, for the dashboard's per-story progress
    view (DASHBOARD_STORY_PROGRESS_PLAN.md Tier 0).

    Mirrors _read_story_log's containment discipline but roots the read at the
    story's worktree (story['worktree'], an absolute path the orchestrator
    records under WORKTREE_ROOT) rather than PLAN_DIR. A worktree is routinely
    deleted after merge/cleanup, so "gone" is a NORMAL state -> available=False,
    never a 500.

    Containment / path safety:
      - The worktree path is taken ONLY from the manifest (story['worktree']);
        the request supplies nothing the helper trusts.
      - filename is a fixed artifact name passed by the endpoint. It is
        hardened here regardless: a name with a path separator, a '..'
        component, or an absolute path is rejected so a future caller can't
        escape the worktree dir via this helper (a '..' filename would
        otherwise reach a sibling worktree's file while still staying under
        WORKTREE_ROOT, a mild cross-story leak).
      - The resolved target is contain-checked under WORKTREE_ROOT, so a
        hand-edited manifest pointing worktree outside WORKTREE_ROOT (e.g.
        '/etc') cannot read arbitrary files.

    Non-UTF-8 bytes decode with errors='replace' (same as _read_story_log) so
    a binary-corrupt artifact degrades to replacement chars, not a 500.

    Returns {"available": bool, "text": str}. Never raises.
    """
    empty = {"available": False, "text": ""}
    if not isinstance(story, dict):
        return empty
    # Reject any filename that could escape the worktree directory. A plain
    # artifact name like ".agent_plan.md" passes; "../x", "/etc/passwd", and
    # "a/b" do not.
    if not isinstance(filename, str) or not filename:
        return empty
    if "/" in filename or "\\" in filename or filename == ".." or filename == ".":
        return empty
    raw_wt = story.get("worktree")
    if not isinstance(raw_wt, str) or not raw_wt:
        return empty
    wt_path = Path(raw_wt)
    # The orchestrator records an absolute worktree; a relative value is a
    # corrupt manifest we fail closed on (resolving a relative path under a
    # CWD we don't control would be unsafe).
    if not wt_path.is_absolute():
        return empty
    target = wt_path / filename
    try:
        target = target.resolve(strict=False)
        root = WORKTREE_ROOT.resolve()
        if target != root and not target.is_relative_to(root):
            return empty
    except OSError:
        return empty
    if not target.exists() or not target.is_file():
        return empty
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return empty
    return {"available": True, "text": text}

def _parse_progress(plan_text: str | None, scratchpad_text: str | None) -> dict | None:
    """Parse progress from the tech-lead checklist and executor scratchpad.

    Counts numbered items (lines starting with a digit followed by a period)
    in the plan text for total. Parses the PROGRESS: <done>/<total> line from
    the scratchpad for done. Returns {done: int, total: int} or None if either
    source is unavailable or unparseable (fail open — never raises).
    """
    if not plan_text or not scratchpad_text:
        return None
    total = sum(1 for line in plan_text.splitlines() if re.match(r'^\d+\.\s*', line.strip()))
    if total == 0:
        return None
    for line in scratchpad_text.splitlines():
        m = re.match(r'^PROGRESS:\s*(\d{1,6})/(\d{1,6})\s*$', line)
        if m:
            done = int(m.group(1))
            return {"done": done, "total": total}
    return None


def _plan_summary(
    plan_name: str, manifest: dict[str, Any], archived_plans: set[str] | None = None,
) -> dict[str, Any]:
    stories = manifest.get("stories", {})
    try:
        updated_at = _manifest_path(plan_name).stat().st_mtime
    except OSError:
        # Manifest existed a moment ago (caller just read it) but vanished
        # under a race with a concurrent write - sort it last rather than
        # 500ing the whole list over one plan's timestamp.
        updated_at = 0.0
    return {
        "name": plan_name,
        "paused": bool(manifest.get("paused", False)),
        "story_count": len(stories),
        "status_counts": _status_counts(stories),
        "aggregate": _aggregate_stories(stories),
        "updated_at": updated_at,
        "archived": plan_name in (archived_plans or set()),
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
    path = _journal_path(plan_name, story_key)
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


def _read_journal(plan_name: str, story_key: str) -> tuple[bool, list[dict[str, Any]]]:
    """Read a story's checkpoint journal safely.

    Returns (available, entries):
      - available=False, entries=[]  when the journal file is missing,
        unreadable (malformed JSON / OSError), or empty (an empty list is
        surfaced the same way as no file at all — see test).
      - available=True, entries=[...] when the file parses to a non-empty
        list. Entries are returned in file order; only entries that are
        dicts are kept so a stray non-object row can't crash the renderer.

    This function never raises — a stray corrupt file in PLAN_DIR must
    not 500 the dashboard, the same way a missing file mustn't 404."""
    path = _journal_path(plan_name, story_key)
    if not path.exists():
        return False, []
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False, []
    if not isinstance(raw, list) or not raw:
        return False, []
    entries = [e for e in raw if isinstance(e, dict)]
    if not entries:
        return False, []
    # Normalize optional fields to None so the UI always sees consistent
    # keys; this lets the render template use `e.next_hint` without a
    # `hasOwnProperty` guard, matching the test that asserts the key is
    # still present (not stripped) when an entry lacks it.
    normalized: list[dict[str, Any]] = []
    for e in entries:
        normalized.append({
            **e,
            "step": e.get("step"),
            "summary": e.get("summary"),
            "next_hint": e.get("next_hint"),
        })
    return True, normalized


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
    # Fleet-wide attempt / failure rollup (new aggregate fields). Kept on
    # `totals` rather than as a sibling key so the UI can read
    # body.totals.dispatch_attempts alongside body.totals.success_rate
    # without juggling two levels of nesting.
    fleet_dispatch = 0
    fleet_rework = 0
    fleet_merge = 0
    fleet_failure_reasons: dict[str, int] = {}
    fleet_by_backend: dict[str, int] = {}
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
    for name in _list_plan_names():
        manifest = _read_manifest(name)
        if manifest is None:
            continue
        if not include_archived and name in archived_plans:
            continue
        plans.append(_plan_summary(name, manifest, archived_plans))
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
    if plan_name not in _list_plan_names():
        raise HTTPException(status_code=404, detail=f"No manifest for plan '{plan_name}'")
    archived = _read_archived_plans()
    archived.add(plan_name)
    _write_archived_plans(archived)
    return {"name": plan_name, "archived": True}


@app.post("/api/plans/{plan_name}/unarchive")
def unarchive_plan(plan_name: str) -> dict[str, Any]:
    """Restore a previously-archived plan to the default sidebar view."""
    if plan_name not in _list_plan_names():
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
    if plan_name not in _list_plan_names():
        raise HTTPException(status_code=404, detail=f"No manifest for plan '{plan_name}'")
    return _service.pause_plan(plan_name)


@app.post("/api/plans/{plan_name}/resume")
def resume_plan(plan_name: str) -> dict[str, Any]:
    """Resume a plan's execution. Delegates the manifest mutation to
    _service.resume_plan (which clears the `paused` flag) and returns the
    service's result verbatim. A plan with no manifest is a 404, matching the
    archive/unarchive precedent."""
    if plan_name not in _list_plan_names():
        raise HTTPException(status_code=404, detail=f"No manifest for plan '{plan_name}'")
    return _service.resume_plan(plan_name)


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
        # Parse progress for in_progress stories with a worktree
        progress = None
        if story.get("status") == "in_progress":
            plan_file = _read_worktree_file(story, ".agent_plan.md")
            scratch_file = _read_worktree_file(story, ".agent_scratchpad.md")
            if plan_file["available"] and scratch_file["available"]:
                progress = _parse_progress(plan_file["text"], scratch_file["text"])
        decorated_stories[story_key] = {**story, "last_activity": last_activity}
        if progress is not None:
            decorated_stories[story_key]["progress"] = progress
    summary = _plan_summary(plan_name, manifest)
    return {
        **summary,
        "epics": manifest.get("epics", {}),
        "stories": decorated_stories,
        "notifications": _tail_notifications(plan_name),
        "notification_records": _collapse_duplicate_notifications(
            _tail_notification_records(plan_name)
        ),
        "decisions": _read_decisions(plan_name),
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
    manifest = _read_manifest(plan_name)
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
    available, entries = _read_journal(plan_name, story_key)
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
    manifest = _read_manifest(plan_name)
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
    return _read_story_log(plan_name, story_key, manifest, lines=lines)


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
    manifest = _read_manifest(plan_name)
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
    plan_file = _read_worktree_file(story, ".agent_plan.md")
    scratch_file = _read_worktree_file(story, ".agent_scratchpad.md")
    progress = None
    if plan_file["available"] and scratch_file["available"]:
        progress = _parse_progress(plan_file["text"], scratch_file["text"])
    return {
        "plan": plan_file,
        "scratchpad": scratch_file,
        "progress": progress,
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
            manifest = _read_manifest(plan)
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


class StoryStatusBody(BaseModel):
    status: str


class StoryPatchBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_instructions: str | None = None
    model: str | None = None
    persona: str | None = None
    risk: str | None = None
    dependencies: str | None = None
    acceptance: str | None = None
    pr_url: str | None = None
    summary: str | None = None
    tdd_split: str | None = None


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
            status_code=404,
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


# Mounted last so it never shadows the /api/* routes above; html=True serves
# static/index.html for "/".
# @app.get("/api/plans/{plan_name}/events")
# async def stream_plan_events(plan_name: str, request: Request):
#     path = _notifications_jsonl_path(plan_name)
# 
#     async def event_generator():
#         last_pos = 0
#         if path.exists():
#             last_pos = path.stat().st_size
# 
#         def read_new_lines(current_pos: int):
#             new_lines: list[str] = []
#             with open(path, "r") as f:
#                 f.seek(current_pos)
#                 while True:
#                     line = f.readline()
#                     if not line:
#                         break
#                     new_lines.append(line)
#             return new_lines, f.tell()
# 
#         while True:
#             if await request.is_disconnected():
#                 break
#             if path.exists():
#                 new_lines, next_pos = await asyncio.to_thread(read_new_lines, last_pos)
#                 for line in new_lines:
#                     line = line.strip()
#                     if not line:
#                         continue
#                     try:
#                         record = json.loads(line)
#                         if not isinstance(record, dict):
#                             continue
#                         yield f"data: {json.dumps(record)}\n\n"
#                     except json.JSONDecodeError:
#                         continue
#                 last_pos = next_pos
#             await asyncio.sleep(1)
# 
#     return StreamingResponse(event_generator(), media_type="text/event-stream")

    # Mount static files for the dashboard UI.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
