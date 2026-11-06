"""Service-level measures read off the audit stream (liveness S3).

The audit stream records three marks of a story's life, and this module turns
them into two fleet-wide aggregates for the operator: the per-story timeline
and nearest-rank percentiles over the merged stories inside a window.

What the stream records, and what it therefore cannot say:

* ``story.dispatch`` (``outcome == "allowed"``) - the story was dispatched.
* ``story.review_start`` - the START of a review call, carrying
  ``detail.started_at``. It marks when the review began, never its verdict.
* ``story.merge`` (``outcome == "allowed"``) - the story was merged.

There is no "this story became eligible" event, so queue time (eligibility to
dispatch) is deliberately NOT measured here rather than approximated from the
first mark that happens to exist. ``story:merge`` (the authorization action) is
a different thing from ``story.merge`` and is never used.

Everything here is read-only and aggregate: nothing verifies the hash chain,
nothing writes, and no plan name, story key, actor or path is returned to the
caller - :func:`service_level_percentiles` returns counts and percentiles only.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline import audit

logger = logging.getLogger(__name__)

#: The measures this module computes, as the closed set of label values.
MEASURES: tuple[str, ...] = ("dispatch_to_review", "review_to_merge", "cycle")

#: The audit actions the timeline is built from.
ACTION_DISPATCH = "story.dispatch"
ACTION_REVIEW_START = "story.review_start"
ACTION_MERGE = "story.merge"

#: ``(plan, story) -> the three marks, each ``None`` until it is recorded``.
Timeline = dict[tuple[str, str], dict[str, datetime | None]]

#: The audit stream's file suffix; the plan name is the rest of the file name.
SUFFIX = ".audit.jsonl"


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, treating a naive one as UTC.

    Returns ``None`` for anything that is not a parseable timestamp, so one bad
    record is skipped instead of failing the whole read.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _stream_paths(plan_dir: str | Path) -> list[Path]:
    """Return *plan_dir*'s plan audit streams, excluding the host-scoped one.

    The host stream (``audit.host_audit_path()``) belongs to no plan, so it is
    excluded by resolved path rather than by name: a redirected host stream can
    otherwise sit in the plan directory under any file name the glob matches.
    """
    directory = Path(plan_dir)
    try:
        host = audit.host_audit_path().resolve()
    except OSError:  # pragma: no cover - a path that cannot be resolved
        host = None
    try:
        candidates = sorted(directory.glob(f"*{SUFFIX}"))
    except OSError:  # pragma: no cover - a directory that cannot be listed
        return []
    streams: list[Path] = []
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:  # pragma: no cover - a path that cannot be resolved
            continue
        if host is not None and resolved == host:
            continue
        streams.append(path)
    return streams


def _records(path: Path) -> list[dict[str, Any]]:
    """Return *path*'s well-formed records in file order, skipping bad lines."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _story_key(record: dict[str, Any]) -> str | None:
    """Return the record's story key, or ``None`` when it carries no usable one."""
    target = record.get("target")
    story = target.get("story") if isinstance(target, dict) else None
    return story if isinstance(story, str) and story else None


def _detail(record: dict[str, Any]) -> dict[str, Any]:
    detail = record.get("detail")
    return detail if isinstance(detail, dict) else {}


def _first(moment: datetime | None, candidate: datetime | None) -> datetime | None:
    """Keep the earliest known mark (file order decides, so ``moment`` wins)."""
    return moment if moment is not None or candidate is None else candidate


def _apply(
    timeline: Timeline,
    key: tuple[str, str],
    record: dict[str, Any],
) -> None:
    """Fold one record into *key*'s marks (first dispatch/merge, earliest review)."""
    marks = timeline[key]
    action = record.get("action")
    allowed = record.get("outcome") == "allowed"
    if action == ACTION_DISPATCH and allowed:
        marks["dispatched"] = _first(marks["dispatched"], _parse_ts(record.get("ts")))
    elif action == ACTION_REVIEW_START:
        started = _parse_ts(_detail(record).get("started_at"))
        current = marks["review_started"]
        if started is not None and (current is None or started < current):
            marks["review_started"] = started
    elif action == ACTION_MERGE and allowed:
        marks["merged"] = _first(marks["merged"], _parse_ts(record.get("ts")))


def read_story_timeline(plan_dir: str | Path) -> Timeline:
    """Read every plan audit stream directly under *plan_dir* into a timeline.

    Streams are read in sorted path order and their lines in file order, never
    recursively. Per ``(plan, story)``:

    * ``dispatched`` - the ``ts`` of the FIRST ``story.dispatch`` record with
      ``outcome == "allowed"`` (a later dispatch never replaces it);
    * ``review_started`` - the EARLIEST ``detail.started_at`` of the
      ``story.review_start`` records (so a rework loop moves it back to when
      the first review began, not the last);
    * ``merged`` - the ``ts`` of the FIRST ``story.merge`` record with
      ``outcome == "allowed"``.

    Unparseable lines and timestamps are skipped, naive timestamps are UTC, a
    missing or unreadable *plan_dir* yields ``{}``, and this function never
    raises. It reads only: the hash chain is neither verified nor extended.
    """
    timeline: Timeline = {}
    try:
        for path in _stream_paths(plan_dir):
            plan = path.name.removesuffix(SUFFIX)
            for record in _records(path):
                story = _story_key(record)
                if story is None:
                    continue
                key = (plan, story)
                timeline.setdefault(
                    key,
                    {"dispatched": None, "review_started": None, "merged": None},
                )
                _apply(timeline, key, record)
    except Exception:
        logger.warning("reading the story timeline failed", exc_info=True)
    return timeline


def _nearest_rank(sorted_values: list[float], quantile: float) -> float:
    """Return the nearest-rank *quantile* of an ascending, non-empty list.

    ``rank = ceil(quantile * n)`` on 1-indexed sorted data, so the answer is
    always one of the observed durations (no interpolation).
    """
    rank = max(1, math.ceil(quantile * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def _window(now: datetime | None, window_days: int) -> tuple[datetime, datetime]:
    """Return ``(cutoff, now)``, the inclusive window a merge must fall inside."""
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now - timedelta(days=window_days), now


def _durations(timeline: Timeline, cutoff: datetime, now: datetime) -> dict[str, list[float]]:
    """Collect the in-window, non-negative duration of each measure per story."""
    durations: dict[str, list[float]] = {measure: [] for measure in MEASURES}
    for marks in timeline.values():
        merged = marks["merged"]
        if merged is None or not cutoff <= merged <= now:
            continue
        for measure, start, end in (
            ("dispatch_to_review", marks["dispatched"], marks["review_started"]),
            ("review_to_merge", marks["review_started"], merged),
            ("cycle", marks["dispatched"], merged),
        ):
            if start is None or end is None:
                continue
            seconds = (end - start).total_seconds()
            if seconds < 0:
                continue
            durations[measure].append(seconds)
    return durations


def service_level_percentiles(
    plan_dir: str | Path,
    now: datetime | None = None,
    window_days: int = 30,
) -> dict[str, dict[str, float]]:
    """Return fleet-wide percentiles per measure over the last *window_days*.

    A story counts only when its MERGE falls inside the window ending at *now*
    (the lower edge is inclusive: ``merged >= now - timedelta(days=window_days)``
    and ``merged <= now``); dispatch and review time never gate it. Each of the
    measures in :data:`MEASURES` is computed only for stories that have both of
    its endpoints and a non-negative duration - a negative duration (marks out
    of order) is skipped, never clamped to zero.

    Percentiles are nearest-rank: for the ``n`` durations of a measure sorted
    ascending, ``rank = ceil(quantile * n)`` on 1-indexed data, so ``p50`` is
    the ``ceil(0.5 * n)``-th duration and ``p90`` the ``ceil(0.9 * n)``-th (no
    interpolation). A measure with no samples is exactly ``{"n": 0}`` - no
    percentile keys, because an unknown p50 is not a zero.

    Read-only: the hash chain is neither verified nor extended, and this never
    raises - an unreadable stream set becomes "no samples".
    """
    cutoff, end = _window(now, window_days)
    try:
        durations = _durations(read_story_timeline(plan_dir), cutoff, end)
    except Exception:
        logger.warning("computing service-level percentiles failed", exc_info=True)
        durations = {measure: [] for measure in MEASURES}
    result: dict[str, dict[str, float]] = {}
    for measure in MEASURES:
        values = sorted(durations[measure])
        if not values:
            result[measure] = {"n": 0}
            continue
        result[measure] = {
            "n": len(values),
            "p50": _nearest_rank(values, 0.5),
            "p90": _nearest_rank(values, 0.9),
        }
    return result
