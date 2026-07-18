"""Plan-file persistence helpers for the pipeline MCP server.

Reads/writes the small JSON files that live alongside a plan's manifest in
PLAN_DIR: notifications log, decisions log, per-story checkpoint journal,
and a plan's role_config block. All read PLAN_DIR as a free variable - the
binding is imported from pipeline_paths at module load, and tests patch
pipeline_persistence.PLAN_DIR directly (the Option B pattern from
PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).

_notify_user is the most heavily patched helper in the suite (~50 tests
silence it to keep assertion output clean); patches land on
pipeline_persistence._notify_user.

_atomic_write_json is imported from pipeline_parsers (it has its own
monkeypatch surface there - see pipeline_parsers module docstring).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline_paths import PLAN_DIR
from pipeline_parsers import _atomic_write_json


def _notify_user(plan_name: str, message: str) -> None:
    """Durably record a notice for the user. The orchestrating agent surfaces
    these (e.g. via PushNotification) from advance_pipeline's summary."""
    path = PLAN_DIR / f"{plan_name}.notifications.log"
    with open(path, "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")


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


__all__ = [
    "_notify_user",
    "_decisions_path",
    "_append_decision",
    "_journal_path",
    "_append_journal",
    "_read_journal",
    "_plan_role_config",
]