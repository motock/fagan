"""
Notification sink that spools *selected* ``notification`` bus events to a
per-plan outbox file for a later out-of-band send.

The outbox is a plain JSONL sidecar inside :data:`pipeline.persistence.PLAN_DIR`:

* ``<plan>.outbox.jsonl`` – one JSON line per spooled notification.

The sink itself performs NO network I/O.  :func:`drain_outbox` (below) reads
the queued records and hands each to an injected ``sender`` — in production
``pipeline.notification_email.send_notification_email`` (PLANNOTIFY-05) — so
spooling can never block the sequential pipeline tick on a network call; only
the scheduler's own drain phase (PLANNOTIFY-06) pays that cost, on its own
schedule.

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
import tempfile
from pathlib import Path
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

# Suffix shared by the sink (which writes it) and the drain (which reads it) —
# one constant so the two can never drift apart.
OUTBOX_SUFFIX = ".outbox.jsonl"

# Wildcard plan name for :func:`drain_outbox`: drain EVERY plan's outbox file
# in one call.  The scheduler tick is plan-agnostic (its scan and reconcile
# phases cover all plans at once), so its drain phase passes this instead of a
# single plan name.
ALL_PLANS = "*"


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

        path = PLAN_DIR / f"{plan}{OUTBOX_SUFFIX}"
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


def _drain_one_outbox(path: Path, sender) -> int:
    """Drain the single outbox file ``path``; return the sent-record count.

    Records the ``sender`` accepted (returned ``True``) are removed; records
    it rejected (``False``) or that made it RAISE are retained verbatim for
    the next drain.  The rewrite is atomic — a temp file in the same
    directory, then :func:`os.replace` over the original — so a crash
    mid-rewrite leaves the original intact: at-least-once delivery, never a
    lost notification.  Never raises.
    """
    sent = 0
    try:
        if not path.exists():
            # A read must not create state: no outbox file means nothing was
            # ever spooled for this plan, so return 0 without creating one.
            return 0

        with open(path, "r", encoding="utf-8") as fh:
            raw_lines = fh.read().splitlines()

        # Retained lines keep their ORIGINAL raw text (never a re-serialized
        # dict) so a drain that rejects everything leaves the file
        # byte-for-byte unchanged.
        retained_lines: list[str] = []
        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                # One corrupt line must not block the rest of the file — and
                # it is dropped from the rewrite entirely, so it can neither
                # be sent nor re-WARN on every future drain.
                logger.warning(
                    "Skipping malformed outbox line in %s (not valid JSON)",
                    path.name,
                )
                continue
            try:
                accepted = sender(record)
            except Exception:  # a raising sender is treated exactly like a False
                logger.exception(
                    "Outbox sender raised for a spooled record; retaining it"
                )
                retained_lines.append(raw_line)
                continue
            if accepted:
                sent += 1
            else:
                retained_lines.append(raw_line)

        # Re-read immediately before the replace: the outbox is append-only
        # (only outbox_sink ever appends), so our original snapshot is always
        # a prefix of the current file. A record the sink spooled WHILE this
        # drain was busy sending (e.g. a slow SMTP loop racing a freshly
        # abandoned worker's completion notice) lives past that prefix and
        # would otherwise be silently discarded by the replace below. Merge
        # it in unprocessed — it is picked up, parsed, and sent on the next
        # drain — so "never a lost notification" holds under concurrency too.
        try:
            with open(path, "r", encoding="utf-8") as fh:
                current_lines = fh.read().splitlines()
        except OSError:
            current_lines = raw_lines
        appended_since_snapshot = current_lines[len(raw_lines):]

        # The file existed, so always rewrite it — even when nothing is
        # retained, which leaves it empty (the pinned choice; the file is
        # never removed).
        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
                for retained in retained_lines + appended_since_snapshot:
                    tmp_fh.write(retained + "\n")
            # Never delete or truncate the original before the replace: a
            # failed replace must leave every record in place.
            os.replace(tmp_path, path)
            tmp_path = None  # replaced; nothing left to clean up
        except Exception:  # keep going, never raise
            logger.exception(
                "Failed to rewrite outbox %s atomically; retaining records",
                path.name,
            )
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:  # pragma: no cover - best-effort cleanup
                    pass
        return sent
    except Exception:  # the drain never raises into the tick
        logger.exception("Failed to drain notification outbox %s", path.name)
        return sent


def drain_outbox(plan_name: str, sender) -> int:
    """Drain spooled outbox records for ``plan_name`` through ``sender``.

    ``sender`` is an explicit argument (in production
    ``pipeline.notification_email.send_notification_email``), never imported
    here, so callers — and tests — can inject any callable that takes one
    record dict and returns a bool.

    Parameters
    ----------
    plan_name:
        The plan whose ``<plan>.outbox.jsonl`` file should be drained, or
        :data:`ALL_PLANS` (``"*"``) to drain every plan's outbox file in one
        call — the shape the scheduler tick uses, since a tick is
        plan-agnostic.
    sender:
        Callable taking one parsed record dict and returning ``True`` when the
        record was delivered (remove it) or ``False`` when it was not (retain
        it for the next drain).  A sender that RAISES is treated exactly like
        a ``False`` return.

    Returns
    -------
    int
        The count of records the sender accepted.  Never raises: a missing
        file, a malformed line, a raising sender, or a failed rewrite is
        logged and the drain moves on.
    """
    if plan_name == ALL_PLANS:
        total = 0
        for outbox_path in sorted(PLAN_DIR.glob(f"*{OUTBOX_SUFFIX}")):
            total += _drain_one_outbox(outbox_path, sender)
        return total
    return _drain_one_outbox(PLAN_DIR / f"{plan_name}{OUTBOX_SUFFIX}", sender)
