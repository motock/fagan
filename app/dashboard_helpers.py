"""Pure aggregation/parsing helpers for app/dashboard.py.

Pulled out of dashboard.py (which re-imports and re-exports every name
here) purely to shrink that file. None of these read module-level
config (PLAN_DIR/USAGE_STATE_PATH) directly — the two that touch
persisted state (`_plan_summary`, `_story_last_activity`) do so only via
`pipeline.server._store`, whose own behavior is controlled by
`pipeline.server.PLAN_DIR`/`WORKTREE_ROOT` (patched directly by tests),
so importing `_store` here rather than proxying through the dashboard
module is safe.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pipeline.server import _store

# Default line cap when the modal fetches the log tail. Capped at
# _LOG_TAIL_CAP so an accidental giant log file never gets slurped into
# a single HTTP response. UI sends ?lines=N to override the default
# within the [1, _LOG_TAIL_CAP] envelope.
_LOG_TAIL_DEFAULT = 200
_LOG_TAIL_CAP = 500

# Staleness threshold (minutes) for the in_progress "aged" indicator on
# cards. Anything older than this with status=in_progress is presumed to
# have stalled the agent and gets a muted warning style in the UI.
STALE_IN_PROGRESS_MINUTES = 30

# A story is considered "dispatched" if it ever made it past the todo state.
# This includes interrupted, pr_open, parked/changes_requested/failed, and
# done — i.e. the orchestrator tried it. Stories with a `backend` field set
# are also counted even if status is still todo, since the backend was
# resolved (escalation can flip backend on a still-todo story).
_DISPATCHED_STATUSES = frozenset({
    "in_progress", "interrupted", "pr_open", "changes_requested",
    "parked", "failed", "done",
})


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


def _consolidate_stories_by_key(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse ``compute_story_metrics`` groups that share a story_key.

    A story's correlation_id is minted on first dispatch, so notification
    records emitted before that point group by the bare story_key while
    later records (including ``story_merged``) group by correlation_id -
    the same story can therefore surface as two separate groups with
    conflicting ``merged`` flags. The maturity table's grain is one row per
    story, so merge any groups sharing a non-null story_key: sum the raw
    counters, OR the merged flags, and recompute ``cost`` as one plus the
    merged counters (never sum the raw ``cost`` fields, which would double
    the fixed +1 baseline). Groups with no story_key (a notification with
    neither story_key nor correlation_id, e.g. a plan-level notice) are
    dropped entirely - they describe no single story and don't belong in a
    per-story table.
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for story in stories:
        key = story.get("story_key")
        if not key:
            continue
        if key not in merged:
            merged[key] = {
                "story_key": key,
                "correlation_id": None,
                "dispatch_failures": 0,
                "rework_cycles": 0,
                "escalations": 0,
                "merged": False,
                "merged_ts": None,
            }
            order.append(key)
        target = merged[key]
        target["dispatch_failures"] += story.get("dispatch_failures", 0) or 0
        target["rework_cycles"] += story.get("rework_cycles", 0) or 0
        target["escalations"] += story.get("escalations", 0) or 0
        if story.get("merged"):
            target["merged"] = True
            if target["merged_ts"] is None:
                target["merged_ts"] = story.get("merged_ts")
        if target["correlation_id"] is None and story.get("correlation_id"):
            target["correlation_id"] = story["correlation_id"]
    result = []
    for key in order:
        payload = merged[key]
        payload["cost"] = (
            1
            + payload["dispatch_failures"]
            + payload["rework_cycles"]
            + payload["escalations"]
        )
        result.append(payload)
    return result


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
    *, include_notification_summary: bool = False,
) -> dict[str, Any]:
    stories = manifest.get("stories", {})
    try:
        updated_at = _store.manifest_path(plan_name).stat().st_mtime
    except OSError:
        # Manifest existed a moment ago (caller just read it) but vanished
        # under a race with a concurrent write - sort it last rather than
        # 500ing the whole list over one plan's timestamp.
        updated_at = 0.0
    latest_notification = None
    if include_notification_summary:
        records = _collapse_duplicate_notifications(_store.get_notification_records(plan_name, limit=5))
        latest_notification = records[-1] if records else None
    return {
        "name": plan_name,
        "paused": bool(manifest.get("paused", False)),
        "story_count": len(stories),
        "status_counts": _status_counts(stories),
        "aggregate": _aggregate_stories(stories),
        "updated_at": updated_at,
        "archived": plan_name in (archived_plans or set()),
        **({"latest_notification": latest_notification} if include_notification_summary else {}),
    }


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

    journal_ts = _store.get_journal_final_ts(plan_name, story_key)
    if isinstance(journal_ts, str) and journal_ts:
        candidates.append(journal_ts)

    for field in ("last_commit", "interrupted_at"):
        val = story.get(field)
        if isinstance(val, str) and val:
            candidates.append(val)

    if not candidates:
        return None
    return max(candidates)


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
