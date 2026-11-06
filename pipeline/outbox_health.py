"""Aggregate depth and age of the notification outbox, for health and notices.

The outbox is a set of JSONL spool files: one ``<plan>.outbox.jsonl`` per plan
plus one ``<plan>.outbox.<channel>.jsonl`` per configured delivery channel.
When delivery works they stay near-empty; when it breaks they grow and the
oldest record keeps ageing, which is the signal ``/api/health`` needs and the
one the scheduler turns into a single notice per stall.

Two rules shape this module:

* **Read-only.** Probing the outbox must never create, write, truncate or lock
  anything. The drain holds a per-file lock, and a health probe that touched it
  could stall or race a delivery, so this module does not import the drain.
* **Aggregate only, no payloads.** Each line is parsed with :func:`json.loads`
  and only its top-level ``"ts"`` is read; the parsed object is dropped
  immediately. Message text, recipients and plan names never reach the result,
  so the health response carries nothing tenant-specific. The per-plan view
  (:func:`outbox_summary_for_plan`) selects files by exact name and stays just
  as payload-free.

"Counts as zero" is read literally: an unreadable file contributes neither a
file nor a record, so a permission error cannot make the outbox look healthy at
a glance while hiding records. A line that is not JSON, or whose ``"ts"`` is
missing or unparseable, is still a record -- we counted a line -- but
contributes an unknown age. An unknown age is *not* stale: a spool file we
cannot read is a reason to look, not a reason to page.

Timestamp parsing matches :func:`pipeline.notification_outbox._is_expired`: a
naive timestamp means UTC, and an unparseable one means "unknown", so an odd
clock never silently hides a growing backlog.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import UTC, datetime
from pathlib import Path

from .parsers import _validate_key

logger = logging.getLogger(__name__)

#: Env var carrying the staleness threshold, in seconds.
OUTBOX_STALE_ENV = "PIPELINE_OUTBOX_STALE_SECONDS"

#: Threshold applied when the env var is unset, malformed or not positive.
DEFAULT_OUTBOX_STALE_SECONDS = 900

#: Every spool file, both per-plan (``<plan>.outbox.jsonl``) and per-channel
#: (``<plan>.outbox.<channel>.jsonl``), recursively.
OUTBOX_GLOB = "*.outbox*.jsonl"


def outbox_stale_seconds() -> int:
    """Return the outbox staleness threshold, in seconds.

    Re-read on every call, never cached at import: an operator retuning the
    value should not need to restart the scheduler, and caching would also
    ignore whatever a test sets after import. Malformed, non-finite (``inf``,
    ``nan``) or non-positive values fall back to the default rather than
    disabling the check or making every outbox look stale.
    """
    raw = os.environ.get(OUTBOX_STALE_ENV)
    if raw is None:
        return DEFAULT_OUTBOX_STALE_SECONDS
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_OUTBOX_STALE_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_OUTBOX_STALE_SECONDS
    # Round a positive fractional threshold up instead of truncating: ``0.5``
    # would otherwise become ``0`` and make every non-empty outbox stale.
    return max(1, math.ceil(value))


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


def outbox_summary(plan_dir) -> dict:
    """Summarise every outbox spool file under *plan_dir*. Never raises.

    Returns ``{"files": int, "records": int, "oldest_age_s": int | None}`` where
    ``oldest_age_s`` is the age of the oldest *valid* record, or ``None`` when
    no record had a usable timestamp. A missing directory, or a directory that
    cannot be listed, is simply empty.
    """
    summary: dict = {"files": 0, "records": 0, "oldest_age_s": None}
    try:
        root = Path(plan_dir)
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


def outbox_summary_for_plan(plan_dir, plan_name) -> dict:
    """Summarise only *plan_name*'s outbox spool files directly in *plan_dir*.

    The same ``{"files": int, "records": int, "oldest_age_s": int | None}``
    shape as :func:`outbox_summary`, so a caller that already renders the
    aggregate view can render a single plan's backlog with no special case.
    Only the ``<plan>.outbox.jsonl`` and ``<plan>.outbox.<channel>.jsonl``
    files -- matched by exact name, never by prefix -- are counted, and only in
    *plan_dir* itself, not below it.

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
        paths = sorted(Path(plan_dir).iterdir())
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


def _age_is_stale(oldest_age_s: int | None, threshold: int) -> bool:
    """True when *oldest_age_s* is strictly older than *threshold* seconds.

    The single definition of "stale", so the health verdict and the notice
    clock cannot drift apart. Strictly older, so a record exactly at the
    threshold is not yet stale; a notice should mean "this is past the limit",
    not "this reached it". ``None`` (unknown age) is never stale.
    """
    return oldest_age_s is not None and oldest_age_s > threshold


def outbox_is_stale(summary: dict, threshold: int) -> bool:
    """True when the oldest record is strictly older than *threshold* seconds."""
    return _age_is_stale(summary.get("oldest_age_s"), threshold)


def stale_transition(
    prev_stale: bool, oldest_age_s: int | None, threshold: int
) -> tuple[str | None, bool]:
    """Return ``(event, new_stale)`` for one tick of the staleness clock.

    *prev_stale* is the flag stored by the previous tick. The returned flag is
    what the caller must store -- storing anything else either re-fires the
    same notice on the next tick or swallows the next real transition. The
    event fires once per transition into stale (``outbox_stale``) and once on
    recovery (``outbox_recovered``), and is ``None`` while the state holds, so
    a persistently stale outbox raises one notice rather than one per tick.

    Public because the scheduler tick (another module) owns the stored flag;
    the state machine lives here, next to the predicate it uses.
    """
    is_stale = _age_is_stale(oldest_age_s, threshold)
    if not prev_stale and is_stale:
        return "outbox_stale", True
    if prev_stale and not is_stale:
        return "outbox_recovered", False
    return None, prev_stale


def apply_outbox_health(result: dict, plan_dir, threshold: int | None = None) -> dict:
    """Add the aggregate ``outbox`` block to a health payload, in place.

    This is the whole ``/api/health`` contribution: ``result["outbox"]`` is the
    summary, and a stale outbox sets ``healthy`` False and appends
    ``"outbox_stale"`` to ``degraded`` without dropping the verdicts already
    there. *threshold* defaults to :func:`outbox_stale_seconds`, re-read per
    call.

    The block is aggregate only -- counts and one age -- so no plan name, file
    path or message body reaches a response body. Scoped, per-plan views are a
    later tenancy concern.
    """
    summary = outbox_summary(plan_dir)
    if threshold is None:
        threshold = outbox_stale_seconds()
    result["outbox"] = summary
    if outbox_is_stale(summary, threshold):
        result["healthy"] = False
        degraded = result.get("degraded")
        if isinstance(degraded, list):
            if "outbox_stale" not in degraded:
                degraded.append("outbox_stale")
        else:
            result["degraded"] = ["outbox_stale"]
    return result
