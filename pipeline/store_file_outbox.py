"""The file store's spool reader: read-only summaries of the outbox spool files.

The outbox is a set of JSONL spool files in a directory: one ``<plan>.outbox.jsonl``
per plan plus one ``<plan>.outbox.<channel>.jsonl`` per configured delivery
channel. This module reads those files and nothing else; it never creates,
writes, truncates or locks one, and it never parses past each line's ``"ts"``.

The directory is a parameter, so the caller decides which spool it reads. The
health payload and the per-plan view in :mod:`pipeline.outbox_health` re-export
these names, so their callers do not change.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from .parsers import _validate_key

logger = logging.getLogger(__name__)

#: Every spool file, both per-plan (``<plan>.outbox.jsonl``) and per-channel
#: (``<plan>.outbox.<channel>.jsonl``), recursively.
OUTBOX_GLOB = "*.outbox*.jsonl"


def _age_seconds(raw_ts, now: datetime) -> int | None:
    """Seconds between *raw_ts* and *now*, or ``None`` when it is unusable.

    A naive timestamp is treated as UTC. A non-string or unparseable value is
    unknown rather than zero: guessing would either mask a stall or invent one.
    """
    if not isinstance(raw_ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw_ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        age = (now - parsed).total_seconds()
    except (OverflowError, ValueError):  # pragma: no cover - exotic timestamps
        return None
    if age < 0:
        # A timestamp in the future (clock skew) clamps to an age of zero: as
        # far as this probe can tell the record was written just now. Keeping
        # the negative value would count as *very* fresh, which is the one
        # direction that can hide a growing backlog.
        return 0
    return int(age)


def _scan_file(path: Path, now: datetime) -> tuple[int, int, int | None]:
    """Return ``(files, records, oldest_age_s)`` for one spool file."""
    records = 0
    oldest: int | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                records += 1
                try:
                    # Only ``ts`` is read; the parsed record is dropped at once
                    # so no message text can reach the caller.
                    raw_ts = json.loads(line).get("ts")
                except (ValueError, AttributeError, TypeError):
                    continue
                age = _age_seconds(raw_ts, now)
                if age is not None and (oldest is None or age > oldest):
                    oldest = age
    except OSError:
        # Unreadable file: contribute nothing, neither a file nor records.
        logger.warning("outbox health probe could not read %s", path.name)
        return 0, 0, None
    return 1, records, oldest


def outbox_summary(spool_dir) -> dict:
    """Summarise every outbox spool file under *spool_dir*. Never raises.

    Returns ``{"files": int, "records": int, "oldest_age_s": int | None}`` where
    ``oldest_age_s`` is the age of the oldest *valid* record, or ``None`` when
    no record had a usable timestamp. A missing directory, or a directory that
    cannot be listed, is simply empty.
    """
    summary: dict = {"files": 0, "records": 0, "oldest_age_s": None}
    try:
        root = Path(spool_dir)
        paths = sorted(root.rglob(OUTBOX_GLOB))
    except OSError:
        logger.warning("outbox health probe could not list the plan dir")
        return summary

    now = datetime.now(UTC)
    for path in paths:
        try:
            if not path.is_file():
                continue
        except OSError:  # pragma: no cover - race with a delete
            continue
        files, records, oldest = _scan_file(path, now)
        summary["files"] += files
        summary["records"] += records
        if oldest is not None and (
            summary["oldest_age_s"] is None or oldest > summary["oldest_age_s"]
        ):
            summary["oldest_age_s"] = oldest
    return summary


def _spool_name_matches(name: str, plan_name: str) -> bool:
    """True when *name* is one of *plan_name*'s own spool files.

    Exact, anchored matching rather than a prefix test, so plan ``a`` never
    counts the files of plan ``ab`` or of a plan literally named ``a.outbox``.
    The per-channel form only matches while the channel carries no dot: a
    dotted remainder (``a.outbox.outbox.slack.jsonl``) is another plan's name,
    not a channel of this one.
    """
    per_plan = f"{plan_name}.outbox.jsonl"
    per_channel_prefix = f"{plan_name}.outbox."
    if name == per_plan:
        return True
    if not (name.startswith(per_channel_prefix) and name.endswith(".jsonl")):
        return False
    channel = name[len(per_channel_prefix) : -len(".jsonl")]
    return bool(channel) and "." not in channel


def outbox_summary_for_plan(spool_dir, plan_name) -> dict:
    """Summarise only *plan_name*'s outbox spool files directly in *spool_dir*.

    The same ``{"files": int, "records": int, "oldest_age_s": int | None}``
    shape as :func:`outbox_summary`, so a caller that already renders the
    aggregate view can render a single plan's backlog with no special case.
    Only the ``<plan>.outbox.jsonl`` and ``<plan>.outbox.<channel>.jsonl``
    files -- matched by exact name, never by prefix -- are counted, and only in
    *spool_dir* itself, not below it.

    Never raises. An invalid *plan_name* is rejected by the same plan-key rule
    the rest of the package uses and yields the empty summary, as do a missing
    directory and a directory that cannot be listed. An unreadable spool file
    contributes nothing, exactly as in :func:`outbox_summary`.
    """
    summary: dict = {"files": 0, "records": 0, "oldest_age_s": None}
    try:
        _validate_key(plan_name)
    except (TypeError, ValueError):
        logger.warning("outbox health probe rejected an invalid plan name")
        return summary

    try:
        paths = sorted(Path(spool_dir).iterdir())
    except OSError:
        logger.warning("outbox health probe could not list the plan dir")
        return summary

    now = datetime.now(UTC)
    for path in paths:
        if not _spool_name_matches(path.name, plan_name):
            continue
        try:
            if not path.is_file():
                continue
        except OSError:  # pragma: no cover - race with a delete
            continue
        files, records, oldest = _scan_file(path, now)
        summary["files"] += files
        summary["records"] += records
        if oldest is not None and (
            summary["oldest_age_s"] is None or oldest > summary["oldest_age_s"]
        ):
            summary["oldest_age_s"] = oldest
    return summary

