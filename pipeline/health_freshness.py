"""Scheduler freshness for ``/api/health``: is the daemon actually alive?

``/api/health`` answers ``ok: True`` literally — it means "the API answered",
which is the liveness signal orchestrators probe. Whether the *scheduler* is
doing work is a different question, and this module answers it:

* the daemon writes ``<plan_dir>/.scheduler_health.json`` every phase and every
  heartbeat interval while its watchdog join runs, with
  ``datetime.now(UTC).isoformat()`` timestamps;
* :func:`freshness` turns that payload into ``scheduler_age_s`` / ``healthy`` /
  ``degraded`` — pure, no I/O, never raising;
* :func:`health_extras` is the thin I/O wrapper the dashboard splats into its
  health dict, so the route stays a one-line hook.

A dashboard without a scheduler is a legitimate deployment, so an absent file
is healthy unless ``PIPELINE_HEALTH_REQUIRE_SCHEDULER=1`` says otherwise.
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from typing import Any

#: Freshness window used when the environment does not supply a valid one.
DEFAULT_STALE_SECONDS = 300.0

#: The timestamp fields the daemon writes, newest of which is the reference.
TIMESTAMP_FIELDS = ("heartbeat_ts", "last_reconcile_ts", "last_scan_ts")

#: Reason codes reported in ``degraded``.
UNREPORTED = "scheduler_unreported"
STALE = "scheduler_stale"


def stale_seconds_from_env() -> float:
    """Return the staleness window, read from the environment on every call.

    ``PIPELINE_HEALTH_STALE_SECONDS`` supplies the window; a missing,
    malformed, non-finite or non-positive value falls back to
    :data:`DEFAULT_STALE_SECONDS`. Deliberately uncached: an operator changing
    the value (or a test monkeypatching it) must take effect on the very next
    health call, never after a restart.
    """
    raw = os.environ.get("PIPELINE_HEALTH_STALE_SECONDS")
    if raw is None:
        return DEFAULT_STALE_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_STALE_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_STALE_SECONDS
    return value


def _parse(value: Any) -> datetime | None:
    """Parse one timestamp field, or ``None`` when it is not usable.

    Naive timestamps are read as UTC (the daemon always writes UTC). Anything
    that raises — a non-string such as an int or ``None``, or a garbage string
    like ``"yesterday"`` — is simply skipped by the caller.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _newest_timestamp(payload: dict) -> datetime | None:
    """The newest parseable timestamp in *payload*, or ``None`` for none."""
    newest: datetime | None = None
    for field in TIMESTAMP_FIELDS:
        parsed = _parse(payload.get(field))
        if parsed is None:
            continue
        if newest is None or parsed > newest:
            newest = parsed
    return newest


def _seconds_since(now: datetime, reference: datetime) -> float:
    """``now - reference`` in seconds, tolerating mixed awareness."""
    if (now.tzinfo is None) != (reference.tzinfo is None):
        now = now.replace(tzinfo=UTC) if now.tzinfo is None else now
        reference = (
            reference.replace(tzinfo=UTC) if reference.tzinfo is None else reference
        )
    try:
        return (now - reference).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return float("inf")


def freshness(
    payload: object,
    now: datetime,
    stale_seconds: float,
    require_scheduler: bool,
) -> dict:
    """Summarise a scheduler health *payload* as of *now*.

    Returns ``{"scheduler_age_s": <int|None>, "healthy": <bool>,
    "degraded": [<reason>...]}``. The reference time is the newest parseable
    of ``heartbeat_ts`` / ``last_reconcile_ts`` / ``last_scan_ts``; a payload
    that is not a dict, or that carries no parseable timestamp, is treated as
    absent. Staleness is decided on the raw float delta and only then rounded
    down for reporting. ``last_error`` is not a failure by itself: a scheduler
    that keeps reporting is healthy even when its last phase logged an error.
    """
    absent = {
        "scheduler_age_s": None,
        "healthy": not require_scheduler,
        "degraded": [UNREPORTED] if require_scheduler else [],
    }
    if not isinstance(payload, dict):
        return absent

    reference = _newest_timestamp(payload)
    if reference is None:
        return absent

    delta = _seconds_since(now, reference)
    stale = delta > stale_seconds
    return {
        "scheduler_age_s": int(delta),
        "healthy": not stale,
        "degraded": [STALE] if stale else [],
    }


def health_extras(plan_dir: Any) -> dict:
    """Read ``<plan_dir>/.scheduler_health.json`` and summarise its freshness.

    Any read or parse failure means "no payload" — a health endpoint must never
    raise because the scheduler file is absent, unreadable, empty, malformed or
    wrong-shaped. Neither the payload nor the environment is cached: the next
    call must observe a file that was just written or just removed.
    """
    payload: object = None
    try:
        path = os.path.join(str(plan_dir), ".scheduler_health.json")
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        payload = None
    return freshness(
        payload,
        datetime.now(UTC),
        stale_seconds_from_env(),
        os.environ.get("PIPELINE_HEALTH_REQUIRE_SCHEDULER") == "1",
    )
