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
* ``outbox`` is the aggregate depth/age of the notification outbox queues, as
  the record store reports it -- counts and one age, never a plan name, path or
  message body.
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
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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


def scheduler_health_path(
    directory: str | Path | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """Return the scheduler's health file for *directory*.

    *directory* wins; then the plan-directory entry of *environ* (the mapping a
    caller such as the metrics probe was handed); then the configured default,
    imported lazily so this module stays importable without the full runtime
    configuration. The resolver lives here because this module owns the file's
    name and is its reader, so the plans-directory knowledge ADR 0012 keeps on a
    shrink-only allowlist stays in one place instead of spreading into every
    caller that wants to know whether the scheduler is alive.
    """
    if directory is None and environ is not None:
        directory = environ.get("PLAN_DIR")
    if directory is None:
        from pipeline import paths

        directory = paths.PLAN_DIR
    return Path(directory) / HEALTH_FILENAME


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
    payload: object = None
    try:
        with open(scheduler_health_path(plan_dir), "r", encoding="utf-8") as handle:
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
    from pipeline.store import _ServerRef
    from pipeline.store_records import DEFAULT_NAMESPACE

    _store = _ServerRef("_store")
    summary = _store.records().outbox_summary(DEFAULT_NAMESPACE)
    outbox_health.apply_outbox_health(result, plan_dir, summary=summary)
    # Additive and never degrading: a deployment without an external anchor is
    # a property of the install, not a fault in this process.
    from pipeline.audit_anchor import anchor_status

    result["anchor"] = anchor_status()
    return result


# --- probes:begin ---
#
# Orchestrator-neutral probes, registered by :func:`register_probes`.
#
# They live outside ``/api/`` on purpose: the dashboard's middleware requires a
# bearer token on *every* ``/api/`` route under ``PIPELINE_IDENTITY_PROVIDER=oidc``,
# and an orchestrator has no credential to present.
#
# The two questions are deliberately different, and keeping them apart is the
# point:
#
# * ``/livez`` -- "restart me". Only the scheduler daemon's own heartbeat can
#   make this fail. It never contacts NATS, Keycloak, the outbox or any other
#   external service: a liveness probe that fails on a dependency restarts a
#   healthy process and turns a blip into a restart storm.
# * ``/readyz`` -- "do not route traffic to me". Fails when the instance is not
#   live *and* when a dependency the request path needs is down: an unreachable
#   broker (only when ``PIPELINE_EVENT_BUS=nats``) or a stale outbox.
#
# Every body carries a boolean plus verdicts from the closed set below -- fixed
# strings only, never a path, count, plan name or version -- and both probes
# answer 503 rather than 500 when something internal breaks.

#: Env var selecting the event bus; only ``"nats"`` has a broker to be ready for.
EVENT_BUS_ENV = "PIPELINE_EVENT_BUS"

#: The one bus value whose reachability ``/readyz`` reports.
NATS_BUS = "nats"

#: The scheduler snapshot is older than the threshold.
REASON_SCHEDULER_STALE = "scheduler_stale"

#: No scheduler has reported and the deployment requires one.
REASON_SCHEDULER_UNREPORTED = "scheduler_unreported"

#: The broker could not be queried (``consumer_status`` says ``reachable: False``).
REASON_BROKER_UNREACHABLE = "broker_unreachable"

#: Records are sitting in the outbox spool past the threshold.
REASON_OUTBOX_STALE = "outbox_stale"

#: The probe itself failed; the verdict is unknown, so it fails closed.
REASON_PROBE_ERROR = "probe_error"

#: The closed set of reasons either probe may ever emit.
PROBE_REASONS = (
    REASON_SCHEDULER_STALE,
    REASON_SCHEDULER_UNREPORTED,
    REASON_BROKER_UNREACHABLE,
    REASON_OUTBOX_STALE,
    REASON_PROBE_ERROR,
)

#: The subset of reasons that justify restarting the process.
LIVENESS_REASONS = frozenset({REASON_SCHEDULER_STALE, REASON_SCHEDULER_UNREPORTED})


class Live(BaseModel):
    """Body of a 2xx ``GET /livez``; a 503 adds the verdicts to ``degraded``."""

    live: bool


class Ready(BaseModel):
    """Body of a 2xx ``GET /readyz``; a 503 adds the verdicts to ``degraded``."""

    ready: bool


def _freshness_reasons(plan_dir: str | Path | None) -> list[str]:
    """Verdicts implied by the local freshness extras, in a fixed order.

    Read-only and side-effect free: :func:`health_extras` re-reads the scheduler
    snapshot and the outbox spool but touches neither, so probing twice in a
    row cannot make a stale scheduler look fresh. Unknown strings are dropped,
    which keeps the response body inside :data:`PROBE_REASONS` even if a
    contributor starts reporting something new.
    """
    extras = health_extras(plan_dir)
    degraded = extras.get("degraded") if isinstance(extras, dict) else None
    if not isinstance(degraded, list):
        return []
    return [reason for reason in PROBE_REASONS if reason in degraded]


def _broker_is_unreachable() -> bool:
    """Whether the event bus reports itself down, never raising.

    Uses the cached ``consumer_status`` -- the same 30 s cache ``/api/health``
    reads -- so a 10 s probe cadence cannot turn a broker into a per-request
    dialler. Only ``PIPELINE_EVENT_BUS=nats`` has a broker to ask about; the
    in-process bus is always "reachable".
    """
    if os.environ.get(EVENT_BUS_ENV) != NATS_BUS:
        return False
    from pipeline import events_nats_consumer

    return events_nats_consumer.consumer_status() == {"reachable": False}


def _degraded(field: str, reasons: list[str]) -> JSONResponse:
    """A 503 body: the boolean field plus the closed-set verdicts.

    Returned as a ``JSONResponse`` rather than a dict so the ``response_model``
    does not serialize it: a dict would be validated *through* ``Live`` /
    ``Ready`` and the ``degraded`` field would silently disappear.
    """
    return JSONResponse(status_code=503, content={field: False, "degraded": reasons})


def register_probes(
    app: FastAPI, plan_dir_fn: Callable[[], str | Path | None]
) -> None:
    """Register ``/livez`` and ``/readyz`` on *app*.

    *plan_dir_fn* is a zero-argument callable returning the resolved plan
    directory, read per request so a rebound plan dir is honoured.
    """

    @app.get("/livez", response_model=Live, responses={503: {"description": "degraded"}})
    def livez():
        """Liveness: restart-worthy problems only."""
        try:
            reasons = [
                reason
                for reason in _freshness_reasons(plan_dir_fn())
                if reason in LIVENESS_REASONS
            ]
        except Exception:  # noqa: BLE001 - a probe never answers 500
            return _degraded("live", [REASON_PROBE_ERROR])
        if reasons:
            return _degraded("live", reasons)
        return {"live": True}

    @app.get("/readyz", response_model=Ready, responses={503: {"description": "degraded"}})
    def readyz():
        """Readiness: live, broker reachable, outbox drained."""
        try:
            reasons = _freshness_reasons(plan_dir_fn())
            if _broker_is_unreachable():
                reasons.append(REASON_BROKER_UNREACHABLE)
        except Exception:  # noqa: BLE001 - a probe never answers 500
            return _degraded("ready", [REASON_PROBE_ERROR])
        if reasons:
            return _degraded("ready", reasons)
        return {"ready": True}
# --- probes:end ---
