"""Per-story and per-plan cost metrics from a plan's notifications sidecar.

Pure computation over the structured notification records that the
event-stamping stories write through ``pipeline.persistence._notification_record``.
Each JSONL line is a dict with keys ``ts`` (ISO-8601 str), ``plan``, ``message``,
``story_key``, ``severity``, ``event``, ``dedup_key`` and OPTIONALLY
``correlation_id``/``attempt``/``role``/``provider``/``model``.  Optional keys
are ABSENT, not null, on older records, so every lookup treats a missing key
and a ``None`` value the same way.

Public surface (exactly three functions):

* ``load_notification_records(path)`` - the single I/O point.  Parses a JSONL
  file and returns ``(records, malformed_count)``: blank lines are skipped,
  lines that fail ``json.loads`` (or parse to a non-dict, which is not a
  notification record) are counted in ``malformed_count`` and never raise, and
  a missing file yields ``([], 0)`` because a plan with no sidecar yet has zero
  metrics, not an error.
* ``compute_story_metrics(records)`` - groups records by ``correlation_id``
  when present, else by ``story_key``, else the literal key
  ``"<uncorrelated>"``, and returns one payload per group sorted by
  ``story_key``.  Records whose ``event`` is absent/unrecognized are still
  attributed to their group but change no counter: the metrics are raw counts
  of the known events, and deduplication of repeated ``dedup_key`` values is
  the sink's job, not ours.
* ``compute_plan_rollup(stories)`` - reduces group payloads to plan totals,
  including ``cost_per_merged_story`` (``total_cost / stories_merged`` rounded
  to 1 decimal; ``None`` when nothing merged).

This module is stdlib-only and I/O-free except the single file read in
``load_notification_records``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "compute_plan_rollup",
    "compute_story_metrics",
    "load_notification_records",
]

#: Grouping key used for records that carry neither a ``correlation_id`` nor a
#: ``story_key``.
UNCORRELATED_KEY = "<uncorrelated>"

_DISPATCH_FAILED_EVENT = "dispatch_failed"
_MERGED_EVENT = "story_merged"

_REWORK_EVENTS = frozenset(
    {"tests_failed", "merge_ci_rework", "merge_gate_retry", "merge_retry"}
)
_ESCALATION_EVENTS = frozenset({"escalated", "model_fallback"})

_FIRST_PASS_DISQUALIFYING_EVENTS = frozenset(
    {"escalated", "model_fallback", "story_parked", "brief_patched"}
)


def load_notification_records(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Parse a notifications JSONL sidecar into records.

    Returns ``(records, malformed_count)``.  Blank lines are skipped.  Lines
    that fail ``json.loads`` - or that parse to something other than a dict,
    which cannot be a notification record - are counted in ``malformed_count``
    instead of raising.  A missing file returns ``([], 0)``: a plan with no
    sidecar yet simply has zero metrics.
    """
    records: list[dict[str, Any]] = []
    malformed = 0
    try:
        # The single I/O point in this module.
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except ValueError:
                    malformed += 1
                    continue
                if isinstance(parsed, dict):
                    records.append(parsed)
                else:
                    malformed += 1
    except FileNotFoundError:
        return [], 0
    return records, malformed


def _group_key(record: dict[str, Any]) -> tuple[str, str]:
    """Return ``(kind, key)`` naming the group a record belongs to."""
    correlation_id = record.get("correlation_id")
    if correlation_id:
        return ("correlation_id", str(correlation_id))
    story_key = record.get("story_key")
    if story_key:
        return ("story_key", str(story_key))
    return ("uncorrelated", UNCORRELATED_KEY)


def compute_story_metrics(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Compute per-story cost metrics from notification records.

    Records are grouped by ``correlation_id`` when the record has one, else by
    ``story_key``, else into the single ``"<uncorrelated>"`` group, and the
    returned mapping is keyed by that same group key.  Each group payload has
    ``story_key``, ``correlation_id``, ``dispatch_failures``, ``rework_cycles``,
    ``escalations``, ``merged``, ``merged_ts`` and ``cost`` (``1 +
    dispatch_failures + rework_cycles + escalations``).  Groups are ordered by
    ``story_key`` (groups with no story key last).

    Records with no recognizable ``event`` - absent, ``None``, or an event name
    outside the known sets - are still attributed to their group but change no
    counter.  Duplicate events (the same ``dedup_key`` written twice) are
    counted twice: these are raw counts, and deduplication is the sink's job.
    """
    groups: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}
    disqualifying = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        kind, group_id = _group_key(record)
        group = groups.get(group_id)
        if group is None:
            group = (
                (kind, group_id),
                {
                    "story_key": None,
                    "correlation_id": None,
                    "dispatch_failures": 0,
                    "rework_cycles": 0,
                    "escalations": 0,
                    "merged": False,
                    "merged_ts": None,
                },
            )
            groups[group_id] = group
        _, payload = group
        if kind == "correlation_id" and payload["correlation_id"] is None:
            payload["correlation_id"] = str(record.get("correlation_id"))
        story_key = record.get("story_key")
        if story_key and payload["story_key"] is None:
            payload["story_key"] = str(story_key)

        event = record.get("event")
        if event in _FIRST_PASS_DISQUALIFYING_EVENTS:
            disqualifying[group_id] = disqualifying.get(group_id, 0) + 1
        if event == _DISPATCH_FAILED_EVENT:
            payload["dispatch_failures"] += 1
        elif event in _REWORK_EVENTS:
            payload["rework_cycles"] += 1
        elif event in _ESCALATION_EVENTS:
            payload["escalations"] += 1
        elif event == _MERGED_EVENT:
            payload["merged"] = True
            if payload["merged_ts"] is None:
                payload["merged_ts"] = record.get("ts")

    ordered = sorted(
        groups.values(),
        key=lambda entry: (entry[1]["story_key"] is None, entry[1]["story_key"] or ""),
    )
    result: dict[str, dict[str, Any]] = {}
    for _, payload in ordered:
        payload = dict(payload)
        payload["cost"] = (
            1
            + payload["dispatch_failures"]
            + payload["rework_cycles"]
            + payload["escalations"]
        )
        result[_group_id_for(payload)] = payload
        payload["disqualifying_events"] = disqualifying.get(_group_id_for(payload), 0)
        payload["first_pass_clean"] = bool(payload["merged"]) and payload["disqualifying_events"] == 0
    return result


def _group_id_for(payload: dict[str, Any]) -> str:
    """Return the mapping key for a computed group payload."""
    if payload["correlation_id"] is not None:
        return str(payload["correlation_id"])
    if payload["story_key"] is not None:
        return str(payload["story_key"])
    return UNCORRELATED_KEY


def compute_plan_rollup(stories: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce per-story payloads to plan-level totals.

    ``cost_per_merged_story`` is ``total_cost / stories_merged`` rounded to one
    decimal, and is ``None`` when nothing merged (no division by zero).
    """
    stories_total = len(stories)
    stories_merged = sum(1 for story in stories if story.get("merged"))
    total_dispatch_failures = sum(story.get("dispatch_failures", 0) or 0 for story in stories)
    total_rework_cycles = sum(story.get("rework_cycles", 0) or 0 for story in stories)
    total_escalations = sum(story.get("escalations", 0) or 0 for story in stories)
    total_cost = sum(story.get("cost", 0) or 0 for story in stories)
    if stories_merged:
        cost_per_merged_story = round(total_cost / stories_merged, 1)
    else:
        cost_per_merged_story = None
    return {
        "stories_total": stories_total,
        "stories_merged": stories_merged,
        "total_rework_cycles": total_rework_cycles,
        "total_escalations": total_escalations,
        "total_dispatch_failures": total_dispatch_failures,
        "total_cost": total_cost,
        "cost_per_merged_story": cost_per_merged_story,
        # Eligible = a real story: story_key or correlation_id is present. The
        # "<uncorrelated>" group is not a story and never counts. A payload
        # without a first_pass_clean key counts as not clean.
        "first_pass_clean_rate": (
            round(
                sum(
                    1
                    for story in stories
                    if (
                        story.get("story_key") is not None
                        or story.get("correlation_id") is not None
                    )
                    and story.get("first_pass_clean") is True
                )
                / sum(
                    1
                    for story in stories
                    if (
                        story.get("story_key") is not None
                        or story.get("correlation_id") is not None
                    )
                ),
                3,
            )
            if any(
                story.get("story_key") is not None
                or story.get("correlation_id") is not None
                for story in stories
            )
            else None
        ),
    }
