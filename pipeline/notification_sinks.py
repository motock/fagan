"""
Notification sink that writes free‑text and structured JSONL logs for
``notification`` bus events.

Two distinct concepts are called *event* here:

* ``event["type"] == "notification"`` – the **bus‑level** event type.  It is a
  member of :data:`pipeline.events.EVENT_TYPES`.
* ``event["payload"]["event"]`` – the **notification’s own** event name, e.g.
  ``"ci_pending_stalled"``.  The sink forwards this payload data to the log
  files.

The module exposes a single public function:

``file_log_sink(event: dict) -> None``
    Writes the notification to two destinations inside :data:`pipeline.persistence.PLAN_DIR`.

* ``<plan>.notifications.log`` – free‑text line in the format
  ``"{ts} {message}\n"``.  The timestamp comes from the bus event; if absent
  ``datetime.now(timezone.utc).isoformat()`` is used.

* ``<plan>.notifications.jsonl`` – a JSONL record created by
  :func:`pipeline.persistence._notification_record`.  The sink re‑uses that
  helper so the two writers cannot drift.

The implementation swallows all exceptions, logs them at ERROR level with
``exc_info=True``, and never propagates failures.  This mirrors the behaviour of
the in‑process event bus.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .persistence import PLAN_DIR, _notification_record, _write_notification_record

logger = logging.getLogger(__name__)


def file_log_sink(event: dict[str, Any]) -> None:
    """Sink a ``notification`` bus event to the plan directory.

    Parameters
    ----------
    event:
        The bus‑level notification event.  It must contain at least a truthy
        ``plan`` key and may include a ``payload`` dict with optional keys
        ``message``, ``story_key``, ``severity``, ``event`` and ``dedup_key``.

    Returns
    -------
    None
        The function always returns ``None``; any error is logged but not
        raised.
    """
    try:
        plan = event.get("plan")
        if not plan:
            logger.error("Missing or falsy 'plan' in notification event: %s", event)
            return

        payload = event.get("payload") or {}
        ts = event.get("ts") or datetime.now(timezone.utc).isoformat()
        message = payload.get("message", "")

        # Free‑text line
        log_path = PLAN_DIR / f"{plan}.notifications.log"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{ts} {message}\n")

        # Structured JSONL record via helper
        record = _notification_record(
            plan_name=plan,
            message=payload.get("message"),
            story_key=payload.get("story_key"),
            severity=payload.get("severity"),
            event=payload.get("event"),
            dedup_key=payload.get("dedup_key"),
            ts=ts,
        )
        _write_notification_record(plan, record)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to write notification sink for event %s", event)
        return
