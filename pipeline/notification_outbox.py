"""
Notification sink that spools *selected* ``notification`` bus events to a
per-plan outbox file for a later out-of-band send.

The outbox is a plain JSONL sidecar inside :data:`pipeline.persistence.PLAN_DIR`:

* ``<plan>.outbox.jsonl`` – one JSON line per spooled notification.

The sink performs NO network I/O.  A later story (PLANNOTIFY-05) drains the
outbox and sends the queued records; this module only queues them, so a
notification can never block the sequential pipeline tick on a network call.

Two distinct concepts are called *event* here, mirroring
:mod:`pipeline.notification_sinks`:

* ``event["type"] == "notification"`` – the **bus-level** event type.
* ``event["payload"]["event"]`` – the **notification's own** structured event
  name, e.g. ``"plan_completed"``.  Selection uses ONLY this structured field;
  message text is never matched, so transient notices such as dispatch retries
  are never queued for delivery.

Selection rules:

* Disabled by default.  The sink writes nothing unless the environment
  variable ``PIPELINE_NOTIFY_OUTBOX_ENABLED`` is exactly ``"1"`` (the strings
  ``"0"``, ``"true"``, ``"01"`` … all leave it disabled).
* Event allowlist.  ``PIPELINE_NOTIFY_OUTBOX_EVENTS`` is a comma-separated
  list of structured event names (default ``"plan_completed"``); only
  notifications whose ``payload["event"]`` is in that set are spooled.
* Retention is delegated to :func:`pipeline.persistence._rotate_if_needed`
  with :data:`pipeline.persistence.NOTIFICATIONS_MAX_BYTES` and
  :data:`pipeline.persistence.NOTIFICATIONS_KEEP_N` — the same policy as
  ``file_log_sink``, so it cannot drift.

Like every sink, this one never raises into the bus: any failure is logged at
ERROR level with ``exc_info=True`` and swallowed, so a notification failure
can never break a pipeline tick.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from . import paths, persistence

logger = logging.getLogger(__name__)

# Re-bound from ``.paths`` at import time, exactly like ``persistence`` does;
# tests patch this name (and ``paths.PLAN_DIR``) to point the sink at a
# temporary plan directory.  Read at call time, never cached.
PLAN_DIR = paths.PLAN_DIR

OUTBOX_ENABLED_ENV = "PIPELINE_NOTIFY_OUTBOX_ENABLED"
OUTBOX_EVENTS_ENV = "PIPELINE_NOTIFY_OUTBOX_EVENTS"
DEFAULT_OUTBOX_EVENTS = "plan_completed"


def outbox_sink(event: dict[str, Any]) -> None:
    """Spool a ``notification`` bus event to the plan's outbox file.

    Parameters
    ----------
    event:
        The bus-level notification event.  It must contain a truthy ``plan``
        key and may include a ``payload`` dict whose optional ``event`` key
        carries the structured notification name used for allowlist matching.

    Returns
    -------
    None
        The function always returns ``None``; any error is logged at ERROR
        with ``exc_info=True`` but never raised.
    """
    try:
        plan = event.get("plan")
        if not plan:
            # Checked BEFORE the enabled gate so a malformed event is reported
            # even when the sink is disabled.
            logger.error(
                "Missing or falsy 'plan' in notification event: %s", event
            )
            return

        if os.environ.get(OUTBOX_ENABLED_ENV, "0") != "1":
            # Secure default: outbound sinks ship disabled.  Read per call so
            # tests (and operators) can flip it without a process restart.
            return

        payload = event.get("payload")
        evt_name = payload.get("event") if isinstance(payload, dict) else None

        raw = os.environ.get(OUTBOX_EVENTS_ENV, DEFAULT_OUTBOX_EVENTS)
        allowed = {s.strip() for s in raw.split(",") if s.strip()}
        if evt_name not in allowed:
            # Not allowlisted (or no structured event name): silently skip.
            return

        path = PLAN_DIR / f"{plan}.outbox.jsonl"
        persistence._rotate_if_needed(
            path,
            persistence.NOTIFICATIONS_MAX_BYTES,
            persistence.NOTIFICATIONS_KEEP_N,
        )
        record = dict(event)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "Failed to spool notification to outbox for event %s", event
        )
        return