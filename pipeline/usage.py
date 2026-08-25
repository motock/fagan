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
import sys
from datetime import datetime, timezone
from typing import Any

from app import backend, role_registry

from .config import (
    _LOCAL_BACKEND_NAMES,
    _RISK_ORDER,
    DAILY_REQUEST_THRESHOLD,
    PIPELINE_LOCAL_MAX_RISK,
    SESSION_PAUSE_THRESHOLD,
    SESSION_RESUME_THRESHOLD,
    WEEK_PAUSE_THRESHOLD,
    WEEK_RESUME_THRESHOLD,
    WEEKLY_REQUEST_THRESHOLD,
)
from .parsers import _atomic_write_json
from .paths import USAGE_STATE_PATH
from .persona import _persona_requires_claude, _story_has_unwinnable_local_scope

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
    return bool(prev_paused and (session_pct >= SESSION_RESUME_THRESHOLD or week_pct >= WEEK_RESUME_THRESHOLD))


def _check_usage_impl() -> dict[str, Any]:
    """
    Probe current subscription usage (current session + current week) via a
    headless `/cost` call and persist it to USAGE_STATE_PATH.

    Intended to be called every ~60s by an external poller (cron/launchd or
    /loop). advance_pipeline reads the persisted state rather than probing
    itself, decoupling the pipeline's tick cadence from the poller's.

    The CLI occasionally omits the percentage summary lines (observed near
    session-reset boundaries) without erroring, so a parse failure falls
    back to the last persisted reading rather than crashing the caller's
    tick - unless there is no prior reading to fall back to. If that frozen
    reading is older than USAGE_STALE_AFTER_SECONDS, it's no longer trusted
    as evidence of being over threshold, so the gate fails open instead of
    blocking the pipeline indefinitely on a permanent CLI output change.

    Staleness is measured from "measured_at" (the last time a probe actually
    succeeded), not "checked_at" (bumped on every call, success or fallback).
    A poller calling this every ~60s would otherwise perpetually look fresh
    by checked_at's measure alone, even after hours of the CLI refusing to
    parse - measured_at is carried forward unchanged across fallback calls
    so the staleness clock keeps counting from the last real measurement.
    """
    # Lazy import: tests monkeypatch _run_usage_probe and the USAGE_* gate
    # constants on pipeline.server (the historical home of this function), so
    # read them from there at call time to honour those patches. Importing
    # server at module load would be circular (server imports this module).
    from pipeline import server as _server

    prev = _read_usage_state()
    try:
        state = _server._run_usage_probe()
    except ValueError:
        if not prev:
            raise
        now_iso = datetime.now(timezone.utc).isoformat()
        state = dict(prev)
        state["checked_at"] = now_iso
        # Count how many polls in a row have failed to parse, so the blind
        # window is visible (and quantifiable) rather than a silent stderr line.
        state["consecutive_parse_failures"] = (
            prev.get("consecutive_parse_failures", 0) + 1
        )
        measured_at = prev.get("measured_at", prev.get("checked_at"))
        state["measured_at"] = measured_at
        age = (
            _usage_state_age_seconds({"checked_at": measured_at})
            if measured_at
            else None
        )
        if age is not None and age > _server.USAGE_STALE_AFTER_SECONDS:
            state["stale"] = True
            state["gate_blind"] = True
            first_blind = not prev.get("gate_blind")
            if first_blind:
                state["blind_since"] = now_iso

            blind_since = state.get("blind_since")
            blind_age = (
                _usage_state_age_seconds({"checked_at": blind_since})
                if blind_since
                else None
            )
            if blind_age is not None and blind_age > _server.USAGE_BLIND_PAUSE_AFTER_SECONDS:
                # Prolonged blindness: fail-closed so a permanent CLI-format
                # change can't leave spend unguarded indefinitely.
                state["paused"] = True
            else:
                state["paused"] = False

            failures = state["consecutive_parse_failures"]
            should_log = first_blind or (failures % _server.USAGE_BLIND_LOG_INTERVAL == 0)
            if should_log:
                status = (
                    "pausing (fail-closed)"
                    if state["paused"]
                    else "failing the gate OPEN"
                )
                print(
                    f"check_usage: usage data is {age:.0f}s stale and the CLI is "
                    f"still not parseable ({failures} consecutive failures) - "
                    f"{status}; cost gate is now BLIND since {state.get('blind_since')}",
                    file=sys.stderr,
                )
        _write_usage_state(state)
        return state
    state["measured_at"] = state["checked_at"]
    state["paused"] = _usage_gate(
        prev.get("paused", False),
        state["session_pct"],
        state["week_pct"],
    )
    # A real measurement clears any blind/stale state from prior failures.
    state["consecutive_parse_failures"] = 0
    state["gate_blind"] = False
    state["stale"] = False
    _write_usage_state(state)
    return state


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
    if _story_has_unwinnable_local_scope(story):
        return "claude"
    return "local"


def _role_resource_ok(
    role: str, plan_role_config: dict | None = None
) -> tuple[bool, str]:
    """Whether the backend serving `role` ("dispatch"/"review") can take work
    right now. Delegates to that backend's resource_status() (Step 5): the
    Claude driver reports the poller-fed usage gate; the local driver reports
    Ollama reachability. So a Claude usage pause gates only Claude-backed roles
    and never freezes local dispatch. Returns (ok, reason).

    plan_role_config: a plan's top-level role_config. When it (or the registry)
    pins *review* to a concrete provider, gate on THAT provider's
    resource_status() — mirroring exactly how _run_reviewer resolves the review
    backend via role_registry.resolve_role("review", plan_role_config=...).
    Without this, a plan that pins review to a local backend (e.g. ollama/glm)
    is still gated by Claude's usage poller (the env default), freezing review
    whenever Claude's session/weekly usage maxes out even though review never
    touches Claude (live incident, 2026-07-28: a mode30 plan with
    role_config review=ollama/glm had review permanently deferred as
    review_paused while Claude usage sat at 100%). Only review is resolved this
    way: dispatch's real backend is per-story via _route_dispatch_backend
    (env-local-first), not role_registry, so passing plan_role_config for
    dispatch would mismatch and re-introduce the same bug. The override branch
    only fires when a plan/registry provider actually exists, so an
    unconfigured install or an env=auto review resolves identically to before.

    role_config is plan-level and OPTIONAL (pipeline-story-schema.md) - most
    plans never set it, so plan_role_config is routinely None here (a bare
    `manifest.get("role_config")`). The registry can still name a review
    provider on its own (model_registry.json's roles.review), and
    role_registry.resolve_role already null-safes a None plan_role_config
    internally (`(plan_role_config or {}).get(role, {})`). Gating this whole
    block on `plan_role_config is not None` therefore reintroduced exactly
    the bug this function exists to fix, just one layer up: a plan with no
    role_config at all fell straight to the env-based Claude default below
    regardless of what the registry said, while _run_reviewer's own
    resolve_role call (unconditional, no such guard) correctly routed the
    actual review to the registry's provider - gate and reviewer disagreed
    about the backend. Confirmed live on overlord-failure-triage
    (2026-08-18, 159 "Claude usage gate tripped" deferrals over 3.2h while
    registry roles.review was ollama/glm the whole time and the plan's own
    role_config had not yet been patched onto the manifest)."""
    if role == "review":
        plan_cfg = (plan_role_config or {}).get("review", {})
        registry = role_registry.load_registry()
        registry_provider = (
            registry.get("roles", {}).get("review", {}).get("provider")
        )
        if plan_cfg.get("provider") or registry_provider:
            # Mirror _run_reviewer's own override detection (review.py): only
            # use the registry-resolved provider when a plan/registry override
            # actually names one, else get_backend's env lookup (incl. auto)
            # must stay in charge. Fail open on a garbage/unresolvable provider
            # so a bad role_config can't crash advance_pipeline or freeze the
            # pipeline — fall through to the env-based gate below.
            try:
                resolution = role_registry.resolve_role(
                    "review",
                    plan_role_config=plan_role_config,
                    default_provider=os.environ.get(
                        "PIPELINE_BACKEND_REVIEW", "claude"
                    ).strip().lower(),
                )
                # Guard against a provider with no registered driver (mirrors
                # _resolve_planner_backend's known-providers check) so a
                # registry/plan misconfiguration fails open instead of crashing.
                known = set(registry.get("providers", {})) | _LOCAL_BACKEND_NAMES
                if resolution.provider not in known:
                    raise role_registry.RoleRegistryError(
                        f"review resolved to unknown provider {resolution.provider!r}"
                    )
                status = backend.get_backend(
                    "review", name=resolution.provider
                ).resource_status()
                return bool(status.get("ok", True)), status.get("reason", "")
            except (role_registry.RoleRegistryError, NotImplementedError, ValueError):
                pass  # fall through to the env-based path below
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
    "_DAILY_REQ_RE",
    "_SESSION_USAGE_RE",
    "_WEEKLY_REQ_RE",
    "_WEEK_USAGE_RE",
    "_check_usage_impl",
    "_parse_usage_output",
    "_read_usage_state",
    "_role_resource_ok",
    "_route_dispatch_backend",
    "_run_usage_probe",
    "_usage_gate",
    "_usage_state_age_seconds",
    "_write_usage_state",
]