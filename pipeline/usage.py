"""Usage-probe / dispatch-routing helpers for the pipeline MCP server.

_parse_usage_output / _run_usage_probe / _write_usage_state / _read_usage_state
/ _usage_state_age_seconds / _usage_gate implement the spend gate (poll /cost,
derive session/week percentages, hysteresis pause/resume, persist state).
_route_dispatch_backend / _role_resource_ok decide whether a story can use
the local backend and whether a role's backend can take work right now.

Constants imported from pipeline_config (DAILY_REQUEST_THRESHOLD,
WEEKLY_REQUEST_THRESHOLD, SESSION_*_THRESHOLD, WEEK_*_THRESHOLD,
PIPELINE_LOCAL_MAX_RISK, _RISK_ORDER) and pipeline_paths (USAGE_STATE_PATH)
are read as free variables; tests that patch them patch pipeline_usage.<name>
directly (Option B - see PIPELINE_MCP_DECOMPOSITION_PLAN.md §4). The autouse
_isolate_usage_state fixture patches both p.USAGE_STATE_PATH and
pipeline_usage.USAGE_STATE_PATH so server-side reads and usage-module reads
both see the same isolated temp path.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import backend
from .config import (
    DAILY_REQUEST_THRESHOLD,
    WEEKLY_REQUEST_THRESHOLD,
    SESSION_PAUSE_THRESHOLD,
    SESSION_RESUME_THRESHOLD,
    WEEK_PAUSE_THRESHOLD,
    WEEK_RESUME_THRESHOLD,
    PIPELINE_LOCAL_MAX_RISK,
    _RISK_ORDER,
)
from .paths import USAGE_STATE_PATH
from .parsers import _atomic_write_json
from .persona import _persona_requires_claude


# ---------- Usage probe ----------
# Legacy format (Claude Code ≤ ~Jun 2026): "Current session: N% used · resets …"
_SESSION_USAGE_RE = re.compile(r"Current session:\s*(\d+)%\s*used\s*·\s*resets\s*(.+)")
_WEEK_USAGE_RE = re.compile(r"Current week \(all models\):\s*(\d+)%\s*used\s*·\s*resets\s*(.+)")
# Current format (post-Jun 2026): "Last 24h · N requests …" / "Last 7d · N requests …"
_DAILY_REQ_RE = re.compile(r"Last 24h\s*·\s*(\d+)\s*requests")
_WEEKLY_REQ_RE = re.compile(r"Last 7d\s*·\s*(\d+)\s*requests")


def _parse_usage_output(text: str) -> dict[str, Any]:
    """Parse the text result of a headless `/cost` call into session/week pcts.

    Supports both the legacy "Current session: N% used" format and the current
    "Last 24h · N requests" format. Raises ValueError if neither format is
    recognised, so callers can fall back rather than act on bad data.
    """
    # Try legacy percentage format first (preserves backward compatibility).
    session_m = _SESSION_USAGE_RE.search(text)
    week_m = _WEEK_USAGE_RE.search(text)
    if session_m and week_m:
        return {
            "session_pct": int(session_m.group(1)),
            "session_reset": session_m.group(2).strip(),
            "week_pct": int(week_m.group(1)),
            "week_reset": week_m.group(2).strip(),
        }

    # Current format: derive percentages from request counts vs. configurable thresholds.
    daily_m = _DAILY_REQ_RE.search(text)
    weekly_m = _WEEKLY_REQ_RE.search(text)
    if daily_m and weekly_m:
        daily = int(daily_m.group(1))
        weekly = int(weekly_m.group(1))
        return {
            "session_pct": min(100, daily * 100 // DAILY_REQUEST_THRESHOLD),
            "week_pct": min(100, weekly * 100 // WEEKLY_REQUEST_THRESHOLD),
        }

    raise ValueError(f"Could not parse usage output: {text!r}")


def _run_usage_probe() -> dict[str, Any]:
    """Check current subscription usage via a headless `/cost` call.

    External boundary: delegates to the configured Backend. Tests mock
    backend.subprocess.run. `/cost` is answered from local session data
    without invoking the model, so this is fast and free to poll frequently.
    (The percentage summary used to be on `/usage`, but that command dropped
    it in favor of a "what's contributing to your usage" breakdown; `/cost`
    still has it.)
    """
    text = backend.get_backend().usage_probe_text()
    usage = _parse_usage_output(text)
    usage["checked_at"] = datetime.now(timezone.utc).isoformat()
    return usage


def _write_usage_state(state: dict[str, Any]) -> None:
    _atomic_write_json(USAGE_STATE_PATH, state)


def _read_usage_state() -> dict[str, Any]:
    if not USAGE_STATE_PATH.exists():
        return {}
    return json.loads(USAGE_STATE_PATH.read_text())


def _usage_state_age_seconds(prev: dict[str, Any]) -> float | None:
    """Seconds since prev's checked_at, or None if missing/unparseable.

    None means "can't prove staleness" - callers should treat that like a
    fresh reading (keep existing hysteresis), not like a stale one.
    """
    checked_at = prev.get("checked_at")
    if not checked_at:
        return None
    try:
        ts = datetime.fromisoformat(checked_at)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _usage_gate(prev_paused: bool, session_pct: int, week_pct: int) -> bool:
    """Decide the paused state for this probe, with hysteresis.

    Trips paused when either window reaches its own PAUSE_THRESHOLD. Once
    paused, it stays paused until both windows drop back below their own
    RESUME_THRESHOLD — "either window still high (by its own bar)" keeps
    it paused.
    """
    if session_pct >= SESSION_PAUSE_THRESHOLD or week_pct >= WEEK_PAUSE_THRESHOLD:
        return True
    if prev_paused and (
        session_pct >= SESSION_RESUME_THRESHOLD or week_pct >= WEEK_RESUME_THRESHOLD
    ):
        return True
    return False


# ---------- Dispatch routing / resource gate ----------
def _route_dispatch_backend(story: dict[str, Any]) -> str:
    """A-priori backend choice for a new dispatch (called only when
    PIPELINE_BACKEND_DISPATCH=auto). Returns "local" or "claude".

    Routes to Claude when the story is above the local risk ceiling or uses a
    security persona; otherwise tries local first (the orchestrator escalates
    to Claude a-posteriori if the local run fails).
    """
    # Read at call time so tests can monkeypatch the env and re-import isn't needed.
    max_risk = os.environ.get("PIPELINE_LOCAL_MAX_RISK", PIPELINE_LOCAL_MAX_RISK).lower()
    max_risk_rank = _RISK_ORDER.get(max_risk, _RISK_ORDER["low"])
    story_risk = (story.get("risk") or "low").lower()
    risk_rank = _RISK_ORDER.get(story_risk, _RISK_ORDER["high"])
    if risk_rank > max_risk_rank:
        return "claude"
    if _persona_requires_claude(story):
        return "claude"
    return "local"


def _role_resource_ok(role: str) -> tuple[bool, str]:
    """Whether the backend serving `role` ("dispatch"/"review") can take work
    right now. Delegates to that backend's resource_status() (Step 5): the
    Claude driver reports the poller-fed usage gate; the local driver reports
    Ollama reachability. So a Claude usage pause gates only Claude-backed roles
    and never freezes local dispatch. Returns (ok, reason)."""
    env_backend = os.environ.get(f"PIPELINE_BACKEND_{role.upper()}", "claude").strip().lower()
    if env_backend == "auto":
        # "auto" is not a concrete driver (get_backend rejects it): the role
        # routes per-story (local-first; Claude for high-risk or a-posteriori
        # escalation). It can take work whenever EITHER concrete backend is
        # available, so a Claude usage pause alone must not freeze local-routed
        # dispatch. Prefer the local route; fall back to Claude's gate/reason.
        local = backend.get_backend(role, name="local").resource_status()
        if local.get("ok", True):
            return True, ""
        claude = backend.get_backend(role, name="claude").resource_status()
        return bool(claude.get("ok", True)), claude.get("reason", "")
    status = backend.get_backend(role).resource_status()
    return bool(status.get("ok", True)), status.get("reason", "")


__all__ = [
    "_SESSION_USAGE_RE",
    "_WEEK_USAGE_RE",
    "_DAILY_REQ_RE",
    "_WEEKLY_REQ_RE",
    "_parse_usage_output",
    "_run_usage_probe",
    "_write_usage_state",
    "_read_usage_state",
    "_usage_state_age_seconds",
    "_usage_gate",
    "_route_dispatch_backend",
    "_role_resource_ok",
]