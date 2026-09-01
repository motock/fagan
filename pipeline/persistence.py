"""Plan-file persistence helpers for the pipeline MCP server.

Reads/writes the small JSON files that live alongside a plan's manifest in
PLAN_DIR: notifications log, decisions log, per-story checkpoint journal,
and a plan's role_config block. All read PLAN_DIR as a free variable - the
binding is imported from pipeline_paths at module load, and tests patch
pipeline_persistence.PLAN_DIR directly (the Option B pattern from
PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).
"""

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any

from .parsers import _atomic_write_json
from .paths import PLAN_DIR

# Retention/rotation policy for the per-plan notification sinks (the JSONL
# sidecar written by _write_notification_record and the free-text .log written
# by notification_sinks.file_log_sink). Both writers share one rotation helper
# (_rotate_if_needed) so the two cannot drift. A cap <= 0 disables rotation
# (append-only, the historical behavior).
NOTIFICATIONS_MAX_BYTES = int(os.environ.get("PIPELINE_NOTIFICATIONS_MAX_BYTES", str(2 * 1024 * 1024)))
NOTIFICATIONS_KEEP_N = int(os.environ.get("PIPELINE_NOTIFICATIONS_KEEP", "3"))

# Module-level constant for notification severities
NOTIFY_SEVERITIES = frozenset({"info", "warning", "error"})

# Logger for this module
logger = logging.getLogger(__name__)



def _decisions_path(plan_name: str) -> Path:
    return PLAN_DIR / f"{plan_name}.decisions.json"


def _append_decision(plan_name: str, record: dict[str, Any]) -> None:
    path = _decisions_path(plan_name)
    log = json.loads(path.read_text()) if path.exists() else []
    log.append(record)
    _atomic_write_json(path, log)

# ---------- Checkpoint journal ----------

def _journal_path(plan_name: str, story_key: str) -> Path:
    return PLAN_DIR / f"{plan_name}.{story_key}.journal.json"


def _append_journal(plan_name: str, story_key: str, record: dict[str, Any]) -> None:
    path = _journal_path(plan_name, story_key)
    log = json.loads(path.read_text()) if path.exists() else []
    log.append(record)
    _atomic_write_json(path, log)


def _read_journal(plan_name: str, story_key: str) -> list[dict[str, Any]]:
    path = _journal_path(plan_name, story_key)
    return json.loads(path.read_text()) if path.exists() else []


def _plan_role_config(plan_name: str) -> dict:
    """A plan's role_config block (per-role provider/model overrides, set at
    save_plan/ingest_plan time - see role_registry.py's resolve_role()),
    or {} if the plan/manifest doesn't exist, doesn't set one, or the
    manifest is unreadable. Read fresh each call, mirroring the codebase's
    other small manifest readers (_read_journal above) - never a gate, so
    any read failure degrades to "no override" rather than raising.
    """
    path = PLAN_DIR / f"{plan_name}.manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("role_config", {})
    except (json.JSONDecodeError, OSError):
        return {}

# ---------- Notification JSONL helpers ----------

def _notifications_jsonl_path(plan_name: str) -> Path:
    """Return the path to the notifications.jsonl file for a plan."""
    return PLAN_DIR / f"{plan_name}.notifications.jsonl"


def _notification_record(
    plan_name,
    message,
    story_key,
    severity,
    event,
    dedup_key,
    ts,
    correlation_id=None,
    attempt=None,
    role=None,
    provider=None,
    model=None,
) -> dict:
    """Build a structured notification record.

    Parameters are passed in the same order as used by _notify_user. The
    function is pure and does not modify any global state.

    The optional correlation/context fields (correlation_id, attempt, role,
    provider, model) are included in the returned record ONLY when not None,
    so the legacy record shape is unchanged when none are set.
    """
    # Validate severity; fall back to "info" if invalid.
    if not isinstance(severity, str) or severity not in NOTIFY_SEVERITIES:
        logger.warning(
            "Invalid notification severity %r for plan %s; falling back to 'info'",
            severity,
            plan_name,
        )
        severity = "info"
    record = {
        "ts": ts,
        "plan": plan_name,
        "message": message,
        "story_key": story_key,
        "severity": severity,
        "event": event,
        "dedup_key": dedup_key,
    }
    for name, value in (
        ("correlation_id", correlation_id),
        ("attempt", attempt),
        ("role", role),
        ("provider", provider),
        ("model", model),
    ):
        if value is not None:
            record[name] = value
    return record


def _rotate_if_needed(path: Path, max_bytes: int, keep: int) -> None:
    """Rotate ``path`` to numbered generations when it exceeds ``max_bytes``.

    Shared by both notification writers (the JSONL sidecar and the free-text
    .log) so their retention policies cannot drift. Best-effort: any OSError
    (missing file, unreadable dir, failed rename) is swallowed so the caller
    falls back to a plain append.

    Policy:
    * ``max_bytes <= 0`` disables rotation entirely (append-only).
    * ``keep <= 0`` retains no generations: the deletion loop starts at
      ``keep + 1``, so with ``keep=0`` the just-rotated generation is deleted
      immediately (truncate-on-overflow, not "disabled").
    * Rotation happens only when the current size EXCEEDS ``max_bytes``.
    * The active file becomes ``<path>.1``; existing generations shift up
      (``.1`` -> ``.2``, ...) and generations beyond ``keep`` are deleted.
    """
    if max_bytes <= 0:
        return
    try:
        size = os.path.getsize(path)
    except OSError:
        # Missing (or unreadable) file: nothing to rotate, and stat'ing must
        # not create the file.
        return
    if size <= max_bytes:
        return
    try:
        # Shift existing generations up FIRST so the active file's rename to
        # .1 cannot clobber a generation that still needs to move.
        for i in range(keep - 1, 0, -1):
            if os.path.exists(f"{path}.{i}"):
                os.replace(f"{path}.{i}", f"{path}.{i + 1}")
        os.replace(path, f"{path}.1")
        # Delete generations beyond keep (e.g. keep was lowered since an
        # older run left more generations behind).
        i = keep + 1
        while os.path.exists(f"{path}.{i}"):
            os.remove(f"{path}.{i}")
            i += 1
    except OSError:
        # Rotation is best-effort; the caller falls back to a plain append.
        return


def _write_notification_record(plan_name: str, record: dict) -> None:
    """Append a JSONL line to the plan's notifications.jsonl file.

    Errors from opening or writing the file are swallowed and logged at ERROR
    level; callers should not see these exceptions.
    """
    try:
        path = _notifications_jsonl_path(plan_name)
        _rotate_if_needed(path, NOTIFICATIONS_MAX_BYTES, NOTIFICATIONS_KEEP_N)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:  # pragma: no cover - exercised by tests
        logger.error("Failed to write notification record for plan %s: %s", plan_name, exc)

# Reimplement _notify_user with keyword-only parameters and JSONL sidecar.

def _notify_user(
    plan_name: str,
    message: str,
    *,
    story_key=None,
    severity="info",
    event=None,
    dedup_key=None,
    correlation_id=None,
    attempt=None,
    role=None,
    provider=None,
    model=None,
) -> None:
    """Durably record a notice for the user with structured JSONL sidecar.

    The function keeps its original two-positional-argument contract while
    adding keyword-only parameters for structured data. It writes the free-text
    log line first, then appends a JSONL record.

    The optional correlation/context kwargs are forwarded to the record
    builder and the bus-event payload; when omitted (the default) the record
    and payload shapes are unchanged.
    """
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record = _notification_record(
        plan_name,
        message,
        story_key,
        severity,
        event,
        dedup_key,
        ts,
        correlation_id=correlation_id,
        attempt=attempt,
        role=role,
        provider=provider,
        model=model,
    )

    def _write_directly() -> None:
        path = PLAN_DIR / f"{plan_name}.notifications.log"
        _rotate_if_needed(path, NOTIFICATIONS_MAX_BYTES, NOTIFICATIONS_KEEP_N)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} {message}\n")
        _write_notification_record(plan_name, record)

    try:
        # Lazy import to avoid cycle: event_wiring imports notification_sinks, which imports persistence
        from .event_wiring import get_bus
        from .events import make_event
        bus = get_bus()
        payload = {
            "message": message,
            "story_key": story_key,
            "severity": record["severity"],
            "event": event,
            "dedup_key": dedup_key,
            "ts": ts,
        }
        # Forward the optional dispatch context so sinks persisting from the
        # bus payload produce the same record as the direct-write path.
        for name, value in (
            ("correlation_id", correlation_id),
            ("attempt", attempt),
            ("role", role),
            ("provider", provider),
            ("model", model),
        ):
            if value is not None:
                payload[name] = value
        evt = make_event(
            "notification",
            plan_name,
            story_key=story_key,
            payload=payload,
        )
        evt["ts"] = ts
        bus.publish(evt)
        # InProcessEventBus.publish swallows handler exceptions internally, so a
        # sink that fails to persist never raises here and can't be caught below.
        # Instead, check whether any handler is actually subscribed to
        # "notification": if no sink ran, write directly so the notification is
        # never silently dropped; if a sink is subscribed (the normal, wired
        # bus), it already persisted the record, so skip to avoid a double write.
        if not getattr(bus, "_handlers", {}).get("notification"):
            try:
                _write_directly()
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to write notification directly: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to publish notification event; falling back: %s", exc)
        try:
            _write_directly()
        except Exception as exc2:  # noqa: BLE001
            logger.error("Failed to write notification directly: %s", exc2)

__all__ = [
    "NOTIFICATIONS_KEEP_N",
    "NOTIFICATIONS_MAX_BYTES",
    "NOTIFY_SEVERITIES",
    "_append_decision",
    "_append_journal",
    "_decisions_path",
    "_journal_path",
    "_notification_record",
    "_notifications_jsonl_path",
    "_notify_user",
    "_plan_role_config",
    "_read_journal",
    "_write_notification_record",
]
