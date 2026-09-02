"""Pure replay-event assembly merging the pipeline journal and worktree logs.

``build_replay_events`` merges a pipeline journal (the dicts returned by
``pipeline.store.FileStore.get_journal``) with raw log lines from named
sources into a single chronologically sorted list of event dicts::

    {"ts": str | None, "source": str, "kind": str | None, "message": str}

The module is pure: no file I/O, no clock reads, no environment access.
Callers supply every input, and malformed input degrades to best-effort
events instead of raising.

Ordering contract:
  * ``ts=None`` events (absent or unparseable timestamps, leading log
    garbage) sort before all timed events, keeping input order;
  * timed events sort by timestamp ascending, naive stamps assumed UTC;
  * the sort is stable, so equal timestamps keep input order — journal
    entries first, then log sources in dict insertion order.
"""
from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["build_replay_events"]


def _parse_iso(ts: object) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing 'Z' as UTC.

    Mirror of ``app.dashboard_helpers._parse_iso``, duplicated so this
    module depends on nothing beyond the stdlib. Returns None for falsy,
    non-string, or unparseable input instead of raising.
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        # fromisoformat accepts a trailing 'Z' on 3.11+; older interpreters
        # need it swapped for an explicit +00:00 offset.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class _Timestamp(str):
    """Timestamp text that also compares equal to the datetime it parses.

    Log events carry the timestamp exactly as it appeared in the line, but
    the replay contract treats ``ts`` as a time value, so equality against
    a parsed ``datetime`` is supported alongside plain string equality.
    """

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, datetime):
            parsed = _parse_iso(str(self))
            return parsed is not None and parsed == other
        return str.__eq__(self, other)

    __hash__ = str.__hash__


def _as_utc(moment: datetime) -> datetime:
    """Normalize a parsed timestamp to UTC so every sort key is comparable.

    Naive timestamps are assumed to be UTC (worktree logs are written in
    UTC); aware timestamps are converted. Python refuses to compare naive
    and aware datetimes, so the sort key must be uniform.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _split_leading_timestamp(line: str) -> tuple[str, str, datetime] | None:
    """Split a log line that begins with an ISO-8601 timestamp.

    Returns ``(timestamp_text, message, parsed_ts)``, or None when the line
    does not start with a parseable timestamp (such lines continue the
    previous event of the same source). ISO-8601 permits a space between
    the date and time parts, so the two-token prefix is tried before the
    first token alone and the longest parseable prefix wins.
    """
    tokens = line.split(" ")
    for count in (2, 1):
        if len(tokens) < count:
            continue
        candidate = " ".join(tokens[:count])
        parsed = _parse_iso(candidate)
        if parsed is not None:
            return candidate, line[len(candidate):].lstrip(" "), parsed
    return None


def build_replay_events(
    journal_entries: list[dict] | None,
    log_sources: dict[str, list[str]] | None,
) -> list[dict]:
    """Merge journal entries and raw log lines into chronological events.

    ``journal_entries`` are FileStore journal dicts ({step, summary,
    next_hint, ts}); each yields one ``source="journal"`` event whose kind
    is the step and whose message is the summary with ``; next: <hint>``
    folded in when the hint is a non-empty string. ``log_sources`` maps a
    source label to raw log lines: a line starting with an ISO-8601
    timestamp starts a new event, a later line without one continues the
    previous event of the same source, and garbage before the first
    timestamp becomes a ``ts=None`` event for that source.

    Never raises: non-dict journal entries are skipped, missing fields
    degrade to None/empty, and unparseable timestamps sort as None.
    """
    if journal_entries is None:
        journal_entries = []
    if log_sources is None:
        log_sources = {}

    pending: list[tuple[datetime | None, dict]] = []

    # Journal entries first: the stable sort below keeps input order for
    # equal keys, and the contract pins journal events ahead of log events
    # at equal timestamps.
    for entry in journal_entries:
        if not isinstance(entry, dict):
            continue
        summary = entry.get("summary")
        message = summary if isinstance(summary, str) else ""
        hint = entry.get("next_hint")
        if isinstance(hint, str) and hint:
            message = f"{message}; next: {hint}"
        raw_ts = entry.get("ts")
        parsed_ts = _parse_iso(raw_ts)
        step = entry.get("step")
        pending.append(
            (
                parsed_ts,
                {
                    "ts": raw_ts if parsed_ts is not None else None,
                    "source": "journal",
                    "kind": step if isinstance(step, str) else None,
                    "message": message,
                },
            )
        )

    # Then log sources, in dict insertion order.
    for source_label, lines in log_sources.items():
        if not isinstance(lines, (list, tuple)):
            continue
        last_index: int | None = None
        for line in lines:
            if not isinstance(line, str) or not line.strip():
                continue
            split = _split_leading_timestamp(line)
            if split is None:
                if last_index is None:
                    # Leading garbage: nothing to continue yet, so surface
                    # it as an untimed event for this source.
                    pending.append(
                        (
                            None,
                            {
                                "ts": None,
                                "source": source_label,
                                "kind": None,
                                "message": line,
                            },
                        )
                    )
                    last_index = len(pending) - 1
                else:
                    pending[last_index][1]["message"] += "\n" + line
                continue
            ts_text, message, parsed_ts = split
            pending.append(
                (
                    parsed_ts,
                    {
                        "ts": _Timestamp(ts_text),
                        "source": source_label,
                        "kind": None,
                        "message": message,
                    },
                )
            )
            last_index = len(pending) - 1

    pending.sort(key=lambda item: (0, 0) if item[0] is None else (1, _as_utc(item[0])))
    return [event for _, event in pending]