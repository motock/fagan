"""Read-only monitoring dashboard for the agent pipeline.

A small FastAPI app, separate from pipeline_mcp_server.py (the MCP server
the orchestrator drives). It only reads the plan/manifest/notification/
decision files PLAN_DIR already holds - it never touches those files, so
it carries none of the pipeline's risk surface. The one exception is a
small dashboard-owned UI-preference file (.dashboard_ui_state.json, see
_read_archived_plans/_write_archived_plans below) tracking which plans the
user has archived/dismissed from the sidebar - this is purely a view
preference, never read by pipeline_mcp_server.py or any orchestration path.

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
    if lines > _LOG_TAIL_CAP:
        lines = _LOG_TAIL_CAP

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


# Mounted last so it never shadows the /api/* routes above; html=True serves
# static/index.html for "/".
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
