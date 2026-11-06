"""The freshness extras merged into the ``/api/health`` payload.

``/api/health`` answers "is this instance alive, and does its own view of the
world agree with what the daemons report?". This module owns the *additive*
part of that answer: the extra blocks a caller can read without changing the
meaning of the pre-existing keys, plus the verdicts those blocks imply
(``healthy`` and ``degraded``).

The shape is deliberately a small dict, computed per call:

    {"scheduler_age_s": int | None, "healthy": bool, "degraded": [str, ...],
     "outbox": {...}}

* ``scheduler_age_s`` is the age of the newest timestamp the scheduler daemon
  wrote to ``<plan_dir>/.scheduler_health.json`` -- ``heartbeat_ts``,
  ``last_reconcile_ts`` or ``last_scan_ts``, whichever is newest -- or ``None``
  when the daemon has not reported (yet).
* ``outbox`` is the aggregate depth/age of the notification outbox spool, from
  :func:`pipeline.outbox_health.outbox_summary` -- counts and one age, never a
  plan name, path or message body.
* ``degraded`` collects verdict strings (``"scheduler_unreported"``,
  ``"scheduler_stale"``, ``"outbox_stale"``); each contributor clears
  ``healthy`` without dropping verdicts contributed by the others.
* Nothing here is cached: a health probe must reflect the filesystem as it is
  right now, and reads only -- neither probe takes a lock a writer needs and
  neither creates state.

Integration point: the dashboard's ``health()`` handler merges ``health_extras``
into its response, and this module applies the outbox health last, so a stale
scheduler and a stale outbox are both reported in the same payload.
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline import outbox_health

#: Env var carrying the scheduler staleness threshold, in seconds.
STALE_ENV = "PIPELINE_HEALTH_STALE_SECONDS"

#: Threshold applied when the env var is unset, malformed or not positive.
DEFAULT_STALE_SECONDS = 300.0

#: Env var that opts a deployment into requiring a live scheduler.
REQUIRE_SCHEDULER_ENV = "PIPELINE_HEALTH_REQUIRE_SCHEDULER"

#: The daemon's own snapshot, written by :meth:`SchedulerDaemon.write_health`.
HEALTH_FILENAME = ".scheduler_health.json"

#: The timestamp fields read from that snapshot; the newest parseable wins.
TIMESTAMP_FIELDS = ("heartbeat_ts", "last_reconcile_ts", "last_scan_ts")


def stale_seconds_from_env() -> float:
    """Return the scheduler staleness threshold, in seconds.

    Re-read on every call, never cached at import: an operator retuning the
    value should not need to restart the scheduler, and a test that changes it
    after import must see the new value. Malformed, non-finite (``inf``,
    ``nan``) or non-positive values fall back to the default rather than
    disabling the check or making every scheduler look stale.
    """
    raw = os.environ.get(STALE_ENV)
    if raw is None:
        return DEFAULT_STALE_SECONDS
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_STALE_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_STALE_SECONDS
    return value


def _parse_ts(raw_ts: object) -> datetime | None:
    """Parse *raw_ts* into an aware datetime, or ``None`` when unusable.

    A naive timestamp is treated as UTC -- the daemon writes
    ``datetime.now(UTC).isoformat()``, so a value without an offset is still
    most plausibly UTC, and comparing it as local time would shift the age by
    hours. A non-string or unparseable value is unknown rather than zero.
    """
    if not isinstance(raw_ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw_ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _newest_timestamp(payload: dict) -> datetime | None:
    """The newest parseable of :data:`TIMESTAMP_FIELDS`, or ``None``."""
    newest: datetime | None = None
    for field in TIMESTAMP_FIELDS:
        parsed = _parse_ts(payload.get(field))
        if parsed is not None and (newest is None or parsed > newest):
            newest = parsed
    return newest


def freshness(
    payload: object,
    now: datetime,
    stale_seconds: float,
    require_scheduler: bool,
) -> dict[str, Any]:
    """Grade the scheduler snapshot *payload* against the clock *now*.

    Pure: no I/O, no environment reads, and never raises. A payload that is not
    a dict, or that carries no parseable timestamp, counts as absent -- the
    file was missing, empty, malformed or written before the daemon started.

    Rules:

    * absent and *require_scheduler* false -> healthy: a dashboard without a
      daemon is a legitimate deployment.
    * absent and *require_scheduler* true -> ``"scheduler_unreported"``.
    * age strictly greater than *stale_seconds* -> ``"scheduler_stale"``; a
      timestamp exactly at the threshold is not yet stale.
    * ``last_error`` is never a failure by itself: an error the daemon already
      reported is still a live daemon.
    """
    newest = _newest_timestamp(payload) if isinstance(payload, dict) else None
    result: dict[str, Any] = {
        "scheduler_age_s": None,
        "healthy": True,
        "degraded": [],
    }
    if newest is None:
        if require_scheduler:
            result["healthy"] = False
            result["degraded"] = ["scheduler_unreported"]
        return result

    age = (now - newest).total_seconds()
    if age < 0:
        # A timestamp in the future (clock skew) clamps to an age of zero: as
        # far as this probe can tell the daemon reported just now. Keeping the
        # negative value would count as very fresh, hiding a stalled daemon.
        age = 0.0
    result["scheduler_age_s"] = int(age)
    if age > stale_seconds:
        result["healthy"] = False
        result["degraded"] = ["scheduler_stale"]
    return result


def health_extras(plan_dir: str | Path | None = None) -> dict[str, Any]:
    """Return the freshness extras for ``/api/health``.

    *plan_dir* defaults to :data:`pipeline.paths.PLAN_DIR` -- imported lazily so
    the module stays importable without the full runtime configuration, and so
    a caller (or a test) can point the probe at any plan directory.

    The scheduler snapshot is read from ``<plan_dir>/.scheduler_health.json``;
    any failure to read or parse it means "no payload", which is graded by
    :func:`freshness`. The outbox health is applied **last**, so its stale
    verdict lands alongside the scheduler's rather than replacing it.

    The returned dict is fresh on every call and aggregates only: no plan
    names, file paths or message text appear in it. A missing or unreadable
    outbox spool yields ``{"files": 0, "records": 0, "oldest_age_s": None}`` and
    leaves ``healthy`` alone -- an outbox we cannot read is a reason to look,
    not a reason to page.
    """
    if plan_dir is None:
        from pipeline import paths

        plan_dir = paths.PLAN_DIR
    payload: object = None
    try:
        with open(Path(plan_dir) / HEALTH_FILENAME, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:  # noqa: BLE001 (read-only probe: any failure means "absent")
        # Absent, unreadable (including a directory at that path), empty,
        # invalid-JSON or wrong-shaped snapshots all mean "not reported".
        payload = None
    result = freshness(
        payload,
        datetime.now(UTC),
        stale_seconds_from_env(),
        os.environ.get(REQUIRE_SCHEDULER_ENV) == "1",
    )
    outbox_health.apply_outbox_health(result, plan_dir)
    return result
