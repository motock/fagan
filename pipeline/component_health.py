"""Per-component liveness, for the dashboard's one Prometheus scrape target.

The dashboard already answers a scrape at ``/metrics``; this probe tells that
scrape whether the pieces *around* the dashboard are alive, so a dead MCP
transport or a stalled scheduler shows up in Grafana instead of being noticed by
an operator who happened to read a log:

* ``dashboard`` -- the process serving the scrape. It is always up with an age of
  zero: a dashboard that cannot answer was never asked.
* ``mcp`` -- the MCP transport, probed with a TCP connect to
  ``127.0.0.1:$PIPELINE_MCP_PORT`` (8080 by default). A listener means up; a
  refused or timed-out connection means down.
* ``scheduler`` -- the scheduler daemon, which rewrites its health file on every
  scan. That file is the one ``$PIPELINE_SCHEDULER_HEALTH_PATH`` names, otherwise
  the one in the plans directory this deployment resolves. Up means the file says
  ``alive`` *and* its ``last_scan_ts`` is younger than
  :data:`COMPONENT_STALE_SECONDS`; ``age_s`` is the age of that timestamp.

The probe NEVER raises and never waits longer than the MCP connect timeout: a
scrape must not hang on a dead component, and one broken probe must not cost the
caller the other two rows. An age the probe cannot establish is ``None`` -- it is
never fabricated as zero, because "unknown" and "just now" mean very different
things on a dashboard.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.health_freshness import scheduler_health_path

logger = logging.getLogger(__name__)

#: Seconds after its last scan at which the scheduler counts as stale.
COMPONENT_STALE_SECONDS = 180.0

#: Seconds the MCP connect may take before the transport counts as down.
PROBE_TIMEOUT_SECONDS = 0.5

#: The MCP transport host: always the loopback, never a remote address.
MCP_HOST = "127.0.0.1"

#: The MCP transport port used when ``PIPELINE_MCP_PORT`` is unset.
DEFAULT_MCP_PORT = 8080

#: Environment keys read by :func:`component_status`.
MCP_PORT_ENV = "PIPELINE_MCP_PORT"
HEALTH_PATH_ENV = "PIPELINE_SCHEDULER_HEALTH_PATH"
STALE_ENV = "COMPONENT_STALE_SECONDS"

#: The components reported, in the fixed order of :func:`component_status`.
COMPONENTS = ("dashboard", "mcp", "scheduler")


def _setting(environ: Mapping[str, str], key: str) -> str | None:
    """Return *key* from *environ*, or ``None`` when it is unset or blank."""
    raw = environ.get(key)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _stale_seconds(environ: Mapping[str, str]) -> float:
    """Return the staleness threshold, honouring an override when it parses."""
    raw = _setting(environ, STALE_ENV)
    if raw is None:
        return COMPONENT_STALE_SECONDS
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "ignoring an unparseable %s override; using %s seconds",
            STALE_ENV,
            COMPONENT_STALE_SECONDS,
        )
        return COMPONENT_STALE_SECONDS


def _health_path(environ: Mapping[str, str]) -> Path:
    """Return the scheduler health file this deployment actually writes.

    ``PIPELINE_SCHEDULER_HEALTH_PATH`` wins when set, so a deployment that moved
    the file is still probed at the right path. Otherwise
    :func:`pipeline.health_freshness.scheduler_health_path` resolves the file the
    scheduler daemon writes, from this probe's own *environ*. That resolver stays
    in the module which owns the file's name and reads it, because ADR 0012's
    shrink-only allowlist forbids a new module from naming the plans directory.
    """
    explicit = _setting(environ, HEALTH_PATH_ENV)
    if explicit is not None:
        return Path(explicit)
    return scheduler_health_path(environ=environ)


def _parse_timestamp(raw: Any) -> float | None:
    """Return *raw* as a POSIX timestamp, or ``None`` when it cannot be parsed."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def _dashboard_status(environ: Mapping[str, str]) -> dict[str, Any]:
    """The dashboard is the process serving the scrape, so it is always up."""
    return {"component": "dashboard", "up": True, "age_s": 0}


def _mcp_status(environ: Mapping[str, str]) -> dict[str, Any]:
    """Probe the MCP transport with one short loopback connect."""
    raw_port = _setting(environ, MCP_PORT_ENV)
    port = DEFAULT_MCP_PORT if raw_port is None else int(raw_port)
    sock = None
    try:
        sock = socket.create_connection((MCP_HOST, port), timeout=PROBE_TIMEOUT_SECONDS)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return {"component": "mcp", "up": True, "age_s": None}


def _scheduler_status(environ: Mapping[str, str]) -> dict[str, Any]:
    """Read the scheduler's health file and judge it fresh or stale."""
    now = time.time()
    path = _health_path(environ)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"component": "scheduler", "up": False, "age_s": None}
    last_scan = _parse_timestamp(payload.get("last_scan_ts"))
    if last_scan is None:
        return {"component": "scheduler", "up": False, "age_s": None}
    age = max(0.0, now - last_scan)
    alive = payload.get("alive") is True
    up = alive and age < _stale_seconds(environ)
    return {"component": "scheduler", "up": up, "age_s": age}


def _probe(name: str, probe, environ: Mapping[str, str]) -> dict[str, Any]:
    """Run one component probe, turning any failure into a down row."""
    try:
        row = probe(environ)
    except Exception as exc:  # noqa: BLE001 - a scrape must never raise
        logger.warning("component probe %r failed: %s", name, type(exc).__name__)
        return {"component": name, "up": False, "age_s": None}
    return {"component": name, "up": bool(row.get("up")), "age_s": row.get("age_s")}


def component_status(environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Return one ``{"component", "up", "age_s"}`` row per component, in order.

    *environ* is the mapping to read settings from; when it is ``None`` the
    process environment is used. Either way the function never raises: a probe
    that blows up reports its component down and the other rows survive.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    return [
        _probe("dashboard", _dashboard_status, source),
        _probe("mcp", _mcp_status, source),
        _probe("scheduler", _scheduler_status, source),
    ]
