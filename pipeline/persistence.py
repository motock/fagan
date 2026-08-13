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
from pathlib import Path
from typing import Any

from .parsers import _atomic_write_json
from .paths import PLAN_DIR

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
) -> dict:
    """Build a structured notification record.

    Parameters are passed in the same order as used by _notify_user. The
    function is pure and does not modify any global state.
    """
    # Validate severity; fall back to "info" if invalid.
    if not isinstance(severity, str) or severity not in NOTIFY_SEVERITIES:
        logger.warning(
            "Invalid notification severity %r for plan %s; falling back to 'info'",
            severity,
            plan_name,
        )
        severity = "info"
    return {
        "ts": ts,
        "plan": plan_name,
        "message": message,
        "story_key": story_key,
        "severity": severity,
        "event": event,
        "dedup_key": dedup_key,
    }


def _write_notification_record(plan_name: str, record: dict) -> None:
    """Append a JSONL line to the plan's notifications.jsonl file.

    Errors from opening or writing the file are swallowed and logged at ERROR
    level; callers should not see these exceptions.
    """
    try:
        path = _notifications_jsonl_path(plan_name)
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
) -> None:
    """Durably record a notice for the user with structured JSONL sidecar.

    The function keeps its original two-positional-argument contract while
    adding keyword-only parameters for structured data. It writes the free-text
    log line first, then appends a JSONL record.
    """
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # Write free-text log line (may raise on OSError)
    path = PLAN_DIR / f"{plan_name}.notifications.log"
    with open(path, "a") as f:
        f.write(f"{ts} {message}\n")
    # Build and write structured record
    record = _notification_record(
        plan_name,
        message,
        story_key,
        severity,
        event,
        dedup_key,
        ts,
    )
    _write_notification_record(plan_name, record)

__all__ = [
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
