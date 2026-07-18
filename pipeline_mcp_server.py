"""
Pipeline MCP Server
Exposes tools for: planning, Plane ingestion, agent dispatch, status monitoring.

Run with: python pipeline_mcp_server.py
Register globally: claude mcp add -s user pipeline ~/.claude/mcp-servers/pipeline/.venv/bin/python3 ~/.claude/mcp-servers/pipeline/pipeline_mcp_server.py

Required env vars (set in ~/.zshrc or ~/.zprofile) - only if using Plane:
  PLANE_BASE       e.g. https://plane.yourcompany.com
  PLANE_API_KEY    Plane personal access token (never commit this)
  PLANE_WORKSPACE  workspace slug, e.g. my-team
  PLANE_PROJECT    project UUID from Plane settings

Ticketing backend (optional - see TicketProvider / get_ticket_provider below):
  PIPELINE_TICKET_PROVIDER  auto (default) | none | plane | jira
    auto:  Plane if the four PLANE_* vars above are all set, else no-op -
           the local manifest is the sole source of truth either way.
    none:  force the no-op provider even if PLANE_* is configured.
    plane: force Plane; errors at call time if PLANE_* is incomplete.
    jira:  documented stub only - selecting it succeeds, but every method
           raises NotImplementedError (see TICKETING_ABSTRACTION_PLAN.md S5).
  A ticketing backend is entirely optional: the pipeline runs fully off its
  local manifest (ingest_plan/dispatch_story/mark_story_done/...) with no
  backend configured at all.

Per-project overrides (set in project .mcp.json env block):
  REPO_ROOT    absolute path to the git repo being worked on
  PLAN_DIR     override plan storage location (default: ~/.claude/plans)
  WORKTREE_ROOT override worktree location (default: ~/.claude/worktrees)
  PIPELINE_MAX_CONCURRENT_AGENTS  cap on agents dispatched/running at once
    across all plans in this session (default: 3; <=0 disables the cap)
"""

import difflib
import fcntl
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import httpx
from mcp.server.fastmcp import FastMCP

import backend
import role_registry

# ---------- Config ----------
PLANE_BASE      = os.environ.get("PLANE_BASE", "http://localhost").rstrip("/")
PLANE_API_KEY   = os.environ.get("PLANE_API_KEY", "")
PLANE_WORKSPACE = os.environ.get("PLANE_WORKSPACE", "")
PLANE_PROJECT   = os.environ.get("PLANE_PROJECT", "")

PLAN_DIR = Path(os.environ.get("PLAN_DIR", "~/.claude/plans")).expanduser()
WORKTREE_ROOT = Path(os.environ.get("WORKTREE_ROOT", "~/.claude/worktrees")).expanduser()
AGENTS_DIR = Path(os.environ.get("AGENTS_DIR", "~/.claude/agents")).expanduser()
POLICY_PATH = Path(os.environ.get("OVERLORD_POLICY", "~/.claude/overlord-policy.md")).expanduser()
USAGE_STATE_PATH = Path(os.environ.get("USAGE_STATE_PATH", "~/.claude/usage_state.json")).expanduser()

REPO_ROOT = Path(os.environ.get("REPO_ROOT", ".")).resolve()

# Autonomy: dry-run (plan/log only) | gated (act up to threshold) | full.
PIPELINE_AUTONOMY = os.environ.get("PIPELINE_AUTONOMY", "gated").lower()
# Highest story risk the overlord may act on unattended.
PIPELINE_RISK_THRESHOLD = os.environ.get("PIPELINE_RISK_THRESHOLD", "low").lower()
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# Default model per persona when a story does not override it.
DEFAULT_MODEL = os.environ.get("PIPELINE_DEFAULT_MODEL", "sonnet")

# Usage gate: pause new dispatch/review when either window reaches its own
# PAUSE_THRESHOLD%; once paused, stay paused until both windows drop back
# below their own RESUME_THRESHOLD% (hysteresis prevents flapping right at
# the boundary). Session and week have independent thresholds because the
# week window resets far less often, so a high weekly total shouldn't gate
# session-level work as tightly as a high session total should.
SESSION_PAUSE_THRESHOLD = int(os.environ.get("PIPELINE_PAUSE_THRESHOLD", "90"))
SESSION_RESUME_THRESHOLD = int(os.environ.get("PIPELINE_RESUME_THRESHOLD", "70"))
WEEK_PAUSE_THRESHOLD = int(os.environ.get("PIPELINE_WEEK_PAUSE_THRESHOLD", "90"))
WEEK_RESUME_THRESHOLD = int(os.environ.get("PIPELINE_WEEK_RESUME_THRESHOLD", "70"))
# How long a frozen (parse-failure) usage reading is trusted before the gate
# fails open. Guards against a CLI output-format change turning a transient
# blackout into a permanent pause.
USAGE_STALE_AFTER_SECONDS = int(os.environ.get("PIPELINE_USAGE_STALE_AFTER_SECONDS", "1800"))
# Request-count thresholds for the new CLI format (post percentage removal).
# session_pct = min(100, daily_requests * 100 // DAILY_REQUEST_THRESHOLD)
# week_pct   = min(100, weekly_requests * 100 // WEEKLY_REQUEST_THRESHOLD)
DAILY_REQUEST_THRESHOLD = int(os.environ.get("PIPELINE_DAILY_REQUEST_THRESHOLD", "3000"))
WEEKLY_REQUEST_THRESHOLD = int(os.environ.get("PIPELINE_WEEKLY_REQUEST_THRESHOLD", "15000"))
# After the gate has been blind this long, flip to fail-closed (paused=True)
# so a permanent CLI-format change doesn't leave spend unguarded indefinitely.
USAGE_BLIND_PAUSE_AFTER_SECONDS = int(os.environ.get("USAGE_BLIND_PAUSE_AFTER_SECONDS", str(6 * 3600)))
# Emit a blind-gate stderr log only on the first blind transition and every
# Nth poll thereafter (default hourly at 60 s poll cadence = 60 polls).
USAGE_BLIND_LOG_INTERVAL = int(os.environ.get("USAGE_BLIND_LOG_INTERVAL", "60"))

# Cap on agents dispatched and running at once, across all plans in this
# session. The usage gate above reacts to a polled /cost snapshot, which lags
# real spend — dispatching every ready story in one tick can let that many
# agents collectively burn through the window before the next poll trips the
# pause. Capping concurrency bounds how much can be spent between polls.
# <=0 disables the cap (dispatch every ready story each tick).
MAX_CONCURRENT_AGENTS = int(os.environ.get("PIPELINE_MAX_CONCURRENT_AGENTS", "3"))

# Error budget for the merge step. _merge_pr shells out to `gh`/`git push`,
# any of which can fail transiently (network, a momentary GitHub 5xx). Rather
# than crash the tick or burn the story on the first hiccup, a failed merge
# leaves the story pr_open and bumps its attempt counter; once attempts reach
# MERGE_MAX_ATTEMPTS the story is marked failed for human intervention.
MERGE_MAX_ATTEMPTS = int(os.environ.get("PIPELINE_MERGE_MAX_ATTEMPTS", "3"))

# Error budget for dispatch (per-story, across ticks). A dispatch can fail two
# ways: dispatch_story raises (git pull/worktree/backend error), or the agent
# launches but produces no output (empty agent.log - a failed launch). Either
# bumps the story's dispatch_attempts; while under budget the story stays
# dispatch-eligible (todo/interrupted) and the next tick retries it, but once
# attempts reach DISPATCH_MAX_ATTEMPTS it is marked failed (terminal - failed
# is not dispatch-eligible) so a story that can never launch stops looping.
# Cleared once a launch actually produces output. Legitimate usage-gate
# interrupts go through interrupt_story and never touch this counter.
DISPATCH_MAX_ATTEMPTS = int(os.environ.get("PIPELINE_DISPATCH_MAX_ATTEMPTS", "3"))

# How long after Popen to trust that an empty agent.log means the agent is
# still bootstrapping (alive but its first print() hasn't flushed) rather
# than genuinely dead. 90s covers Ollama's `-np 1` queue waits for one
# request against devstral:24b even when 2-3 dispatches collide, while still
# flagging a script-crash-before-any-print within a couple of polls.
# Defense-in-depth with local_agent.py's `[boot]` heartbeat - even older
# agents without the heartbeat still benefit from this grace window.
DISPATCH_STARTUP_GRACE_SECONDS = int(
    os.environ.get("PIPELINE_DISPATCH_STARTUP_GRACE_SECONDS", "90")
)

# Absolute ceiling on how long a dispatched agent process may stay alive
# before check_story_status treats it as hung rather than "running". The
# step cap and per-call LLM timeout are supposed to bound a dispatch, but a
# blocking, non-streaming provider call can stall indefinitely on a single
# stuck request (observed directly during MLX provider validation: a
# dispatch subprocess sat at 0% CPU with no error, past the outer harness's
# own timeout, and was found still running minutes later — the harness never
# killed it). Generous default so a legitimately slow local run isn't killed
# mid-flight.
DISPATCH_WATCHDOG_SECONDS = int(
    os.environ.get("PIPELINE_DISPATCH_WATCHDOG_SECONDS", "3600")
)

# Terminal markers the headless agent prints on the LAST line of its
# agent.log when it hits its step cap and exits with code 2. The agent
# has already WIP-committed its in-progress work before printing these,
# so the right thing for the orchestrator to do is mark the story
# "interrupted" (dispatch-eligible, resumable from the existing worktree
# and journal) — NOT run the test suite against the WIP commit and label
# it tests_passed (which is merge-eligible and was how incomplete work
# landed on master in PR #49 / commit 90a3cf1). Local-agent marker first,
# oracle marker second; check_story_status matches the LAST non-empty line
# of agent.log against this tuple.
STEP_CAP_MARKERS = (
    "[ended without done — step cap reached]",
    "[ended without oracle green — step cap reached]",
)

# A story that keeps hitting the step cap is classified "interrupted" (see the
# STEP_CAP_MARKERS branch below), never "failed" - so it never reaches the
# "failed"-gated local_model_fallback check in advance_pipeline's polling loop
# and can cycle on a struggling model forever. This threshold gates a SEPARATE
# fallback: after this many consecutive step-cap interrupts on the same model,
# switch story["model"] (never story["backend"] - stays local, never Claude)
# for the next resume. Only takes effect when the plan has opted in via
# manifest["local_model_fallback"] (see _escalate_to_local_fallback_model).
#
# When the plan has NOT opted into local_model_fallback, the same threshold
# and the same streak fields instead gate escalation to Claude (see
# _escalate_to_claude, called from check_story_status) once
# PIPELINE_BACKEND_DISPATCH=auto - a repeated step-cap streak indicates a
# local-model capability problem, not a review-convergence problem, so under
# auto dispatch it is treated the same as a local test-failure escalation.
# The two fallbacks are mutually exclusive (no chaining): a plan with
# local_model_fallback configured always takes the local-fallback path.
STEP_CAP_FALLBACK_THRESHOLD = int(
    os.environ.get("PIPELINE_STEP_CAP_FALLBACK_THRESHOLD", "3"))

# Layered local-first dispatch (PIPELINE_BACKEND_DISPATCH=auto):
#   1. A-priori: stories with risk above PIPELINE_LOCAL_MAX_RISK (default "low")
#      or a security persona go straight to Claude.
#   2. A-posteriori: if the local agent fails (bad code / test failure), the
#      orchestrator escalates that specific story to Claude and starts clean.
# Explicit "local" or "claude" values bypass the risk-ceiling half of this
# router, but NOT the security-persona half: dispatch_story applies the
# persona override (see _persona_requires_claude) regardless of dispatch
# mode, unless the story already has an explicit story["backend"] (e.g. from
# a prior escalation), which always wins as-is.
PIPELINE_LOCAL_MAX_RISK = os.environ.get("PIPELINE_LOCAL_MAX_RISK", "low").lower()
_LOCAL_SKIP_PERSONAS = {"security-engineer"}

# RELIABILITY_PLAN.md T16: "local" (env-resolved via PIPELINE_LOCAL_PROVIDER)
# is a permanent back-compat alias; "ollama"/"lmstudio"/"mlx" let a role name
# the actual local transport directly (backend._DRIVERS). A resolved backend
# name that could be any of these four must be treated identically by any
# gate keyed on "is this dispatch local-family" - NOT the two-value
# {"local","claude"} space _route_dispatch_backend()'s auto router returns,
# which is intentionally left alone (auto-mode doesn't route to a specific
# provider, only local-vs-claude).
_LOCAL_BACKEND_NAMES = frozenset({"local", "ollama", "lmstudio", "mlx"})

# Rework budget. When the reviewer returns REQUEST_CHANGES the story is sent
# back for rework (redispatched with the reviewer's feedback). To stop a story
# the reviewer keeps rejecting from looping through review/rework forever, cap
# the cycles: once rework_attempts reaches REWORK_MAX_ATTEMPTS the story parks
# for human review instead of redispatching again. Cleared on APPROVE.
REWORK_MAX_ATTEMPTS = int(os.environ.get("PIPELINE_REWORK_MAX_ATTEMPTS", "3"))

# Oracle-aware rework budget: a story carrying a non-empty `acceptance`
# block already has an objective, pre-verified correctness signal (it only
# reaches review after tests - including the oracle - pass), so a reviewer
# that keeps finding beyond-oracle issues on 3 full cycles is mostly
# spending time, not changing the outcome (2026-07-03 replication run: 5 of
# 12 non-successes were ground-truth-correct code that still burned the
# full budget before parking). A lower cap converges to the same "parked
# for human review" endpoint faster. Falls back to REWORK_MAX_ATTEMPTS for
# any story without a truthy `acceptance` list (ordinary TDD, where the
# reviewer's judgment is the primary correctness signal and deserves the
# full budget).
REWORK_MAX_ATTEMPTS_ORACLE = int(os.environ.get("PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE", "1"))

# Rework budget for a story that has already been escalated to Claude (see
# _escalate_review_to_claude). REWORK_MAX_ATTEMPTS_ORACLE exists to converge
# LOCAL review fast; once escalation has already paid its cost (real Claude
# usage, and often real wall-clock time - see 2026-07-04's benchmark
# validation, where a story that escalated could take hours if the Claude
# reviewer got rate-limited), reusing that same tight 1-attempt cap just
# throttles Claude's shot at the SAME feedback for no benefit - 6 of 11
# escalated cells in that validation run parked after exactly 1 post-
# escalation cycle. Takes priority over REWORK_MAX_ATTEMPTS_ORACLE
# regardless of whether the story has an acceptance oracle, since once
# escalated the story is on the "give it a real shot" track, not the
# "converge fast" track.
REWORK_MAX_ATTEMPTS_ESCALATED = int(os.environ.get("PIPELINE_REWORK_MAX_ATTEMPTS_ESCALATED", "3"))

# Inconclusive-review budget. A non-rate-limited UNKNOWN verdict (a reviewer
# response with no parseable VERDICT line, or the fail-safe path for a
# reviewer backend's own internal error) is not evidence the story needs
# rework - it's an infrastructure hiccup. Counting it against
# REWORK_MAX_ATTEMPTS would let a flaky reviewer silently exhaust the rework
# budget and park a correct implementation, and redispatching the agent with
# the (empty) reviewer output would make it rework blind. So leave the
# story's status untouched and let the next advance_pipeline tick retry
# review instead - but cap the retries too, since an inconclusive reviewer
# that never recovers would otherwise loop forever just like an unbounded
# rework cycle would. Cleared on any conclusive verdict (APPROVE or
# REQUEST_CHANGES).
REVIEW_INCONCLUSIVE_MAX = int(os.environ.get("PIPELINE_REVIEW_INCONCLUSIVE_MAX", "2"))

# Error budget for Plane state transitions. Plane sync is a best-effort side
# effect of an action that already succeeded in git, so its budget is an inline
# retry (not an across-ticks retry like merge/dispatch): _plane_set_state
# retries a transient failure up to PLANE_MAX_ATTEMPTS, then records the drop
# durably (notify, not a silent print) rather than raising.
PLANE_MAX_ATTEMPTS = int(os.environ.get("PIPELINE_PLANE_MAX_ATTEMPTS", "3"))

PLAN_DIR.mkdir(parents=True, exist_ok=True)
WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("pipeline")

# FastMCP's constructor calls logging.basicConfig(level=INFO), which the httpx
# and httpcore loggers (NOTSET) then inherit — so every HTTP call (e.g. the
# per-tick Ollama /api/tags reachability probe) logs an INFO "HTTP Request: ..."
# line. Under launchd's stderr redirect that floods the unattended logs (the
# bulk of advance-scheduler.err.log was these). Cap them at WARNING so genuine
# HTTP problems still surface but routine request chatter doesn't.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# Module logger for non-user-visible warnings (dispatch_story's multi-model
# VRAM-swap warning uses both this and _notify_user; the user sees the
# latter via the dashboard/notification summary, the former is for
# tail-grepping the orchestrator log).
logging.getLogger("pipeline")

# ---------- Helpers ----------
def _plane_enabled() -> bool:
    """True only when Plane is fully configured (API key + workspace + project).

    When it isn't, the local manifest is the sole source of truth and every
    Plane call is skipped entirely rather than fired at an unconfigured
    endpoint. Without this guard an unconfigured deployment (the common case
    when running purely off manifests) 404s on every scheduled tick — burning
    the PLANE_MAX_ATTEMPTS retry budget and flooding the logs with dead
    requests to http://localhost/api/v1/workspaces//projects//...
    """
    return bool(PLANE_API_KEY and PLANE_WORKSPACE and PLANE_PROJECT)


def plane_request(method: str, path: str, **kwargs) -> dict:
    """Thin wrapper around Plane's REST API."""
    url = f"{PLANE_BASE}/api/v1/workspaces/{PLANE_WORKSPACE}{path}"
    headers = {"X-API-Key": PLANE_API_KEY, "Content-Type": "application/json"}
    r = httpx.request(method, url, headers=headers, timeout=30, **kwargs)
    if not r.is_success:
        raise RuntimeError(f"Plane {method} {path} → {r.status_code}: {r.text}")
    return r.json() if r.content else {}


_default_branch_cache: dict[str, str] = {}


def _default_branch() -> str:
    """Return REPO_ROOT's default branch (e.g. main or master).

    Detected from origin's HEAD symref rather than hardcoded, since the
    pipeline operates across multiple repos that differ on this. Cached per
    repo (keyed on REPO_ROOT's current value) rather than as one shared
    value, since REPO_ROOT changes across plans/repos within one process
    (see _scoped_repo_root) — a single shared cache would silently return a
    stale branch name for every repo after the first.
    """
    key = str(REPO_ROOT)
    if key in _default_branch_cache:
        return _default_branch_cache[key]

    try:
        ref = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
        ).stdout.strip()
        _default_branch_cache[key] = ref.split("/", 1)[-1]
        return _default_branch_cache[key]
    except subprocess.CalledProcessError:
        pass
    except OSError:
        # `git` binary missing / not executable on PATH. Honor the same
        # never-raises contract as _rebase_onto_master: fall through to
        # the rev-parse attempt, then the "main" default, rather than
        # crashing the caller (e.g. the merge-gate rebase path on a
        # container without git installed).
        pass

    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
        ).stdout.strip()
        if branch and branch != "HEAD":
            _default_branch_cache[key] = branch
            return _default_branch_cache[key]
    except subprocess.CalledProcessError:
        pass
    except OSError:
        pass

    _default_branch_cache[key] = "main"
    return _default_branch_cache[key]


def _repo_root_for(plan_name: str) -> Path:
    """Return the repo this plan operates on.

    Plans share one PLAN_DIR but each belongs to a different project/repo —
    there is no single correct global REPO_ROOT across all of them. Returns
    the manifest's recorded repo_root (set at ingest_plan time) if present,
    else falls back to the server's global REPO_ROOT for older manifests
    ingested before this field existed.
    """
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    if manifest_path.exists():
        repo_root = json.loads(manifest_path.read_text()).get("repo_root")
        if repo_root:
            return Path(repo_root)
    return REPO_ROOT


@contextmanager
def _scoped_repo_root(plan_name: str):
    """Temporarily set the global REPO_ROOT to this plan's repo for the
    duration of the block, restoring the previous value on exit (even on
    exception). Lets dispatch_story/_merge_pr/_default_branch/_load_policy
    keep using the plain REPO_ROOT global internally — only the caller
    needs to know which plan it's working on.
    """
    global REPO_ROOT
    previous = REPO_ROOT
    REPO_ROOT = _repo_root_for(plan_name)
    try:
        yield REPO_ROOT
    finally:
        REPO_ROOT = previous


_state_cache: dict[str, str] = {}


def _get_state(group: str) -> str:
    """Return the first state UUID matching the given Plane state group."""
    if group not in _state_cache:
        resp = plane_request("GET", f"/projects/{PLANE_PROJECT}/states/")
        for s in resp.get("results", []):
            _state_cache.setdefault(s["group"], s["id"])
    return _state_cache[group]


_label_cache: dict[str, str] = {}

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def _resolve_issue_uuid(story_key: str) -> str:
    """Return the Plane work-item UUID for story_key.

    Manifest keys are either plain UUIDs (plans created after ingest_plan was
    fixed) or human-readable identifiers like PIPE-7 (older plans / manual
    dispatch).  Plane's REST API only accepts the UUID form in URL paths, so
    we look up the UUID by sequence number when the key is not already a UUID.
    """
    if _UUID_RE.match(story_key):
        return story_key
    m = re.search(r'(\d+)$', story_key)
    if not m:
        return story_key  # can't parse — pass through and let Plane error
    seq = int(m.group(1))
    resp = plane_request(
        "GET",
        f"/projects/{PLANE_PROJECT}/work-items/",
        params={"sequence_id": seq},
    )
    results = resp.get("results", [])
    if results:
        return results[0]["id"]
    return story_key  # fallback — will produce a 404 from Plane


def _get_or_create_label(name: str) -> str:
    """Return the UUID for a label, creating it if it does not exist."""
    if name not in _label_cache:
        resp = plane_request("GET", f"/projects/{PLANE_PROJECT}/labels/")
        for lbl in resp.get("results", []):
            _label_cache[lbl["name"]] = lbl["id"]
    if name not in _label_cache:
        resp = plane_request("POST", f"/projects/{PLANE_PROJECT}/labels/",
                             json={"name": name, "color": "#6366f1"})
        _label_cache[name] = resp["id"]
    return _label_cache[name]


# ---------------------------------------------------------------------------
# TicketProvider abstraction
#
# Plane is one possible ticketing backend, not the only one, and the pipeline
# already runs fully off its local manifest when no backend is configured
# (see _plane_enabled above). TicketProvider makes that choice explicit and
# swappable via PIPELINE_TICKET_PROVIDER instead of an implicit side effect
# of unset env vars: "auto" (default) picks Plane if configured else no-op,
# "none" forces the no-op provider even if Plane vars are present, "plane"
# forces Plane (erroring if unconfigured), and "jira" is a documented
# extension point for a future real implementation.
#
# Every provider method mirrors the four operations the orchestrator actually
# needs: create_epic, create_story, set_state (a transition, never raises),
# and resolve_key (human key -> backend id). PlaneTicketProvider is a thin
# delegate to the existing module-level Plane functions above rather than a
# reimplementation, so it shares their caches, retry budget, and (in tests)
# their exact plane_request call shape.
# ---------------------------------------------------------------------------
class LogicalState(Enum):
    """Backend-agnostic story lifecycle states, mapped to each provider's own
    vocabulary (e.g. Plane's state *groups*: backlog/started/completed)."""
    BACKLOG = "backlog"
    IN_PROGRESS = "in_progress"
    DONE = "done"


_PLANE_STATE_GROUP = {
    LogicalState.BACKLOG: "backlog",
    LogicalState.IN_PROGRESS: "started",
    LogicalState.DONE: "completed",
}


class TicketProvider(Protocol):
    """The seam between orchestration and whatever ticketing backend (or
    none) is configured. See the module comment above for the rationale."""

    enabled: bool

    def create_epic(self, summary: str) -> str | None:
        """Create an epic and return its backend id, or None if the backend
        has no epic concept / the call isn't supported (never an error)."""
        ...

    def create_story(
        self, summary: str, description: str, epic_id: str | None, label: str,
    ) -> str | None:
        """Create a story and return its backend id, or None if there is no
        backend to create it in (caller falls back to a local/synthetic key)."""
        ...

    def set_state(
        self, story_key: str, state: LogicalState, plan_name: str | None = None,
    ) -> bool:
        """Best-effort state transition. Never raises - returns whether it
        landed, mirroring _plane_set_state's contract."""
        ...

    def resolve_key(self, story_key: str) -> str:
        """Resolve a human-readable key (e.g. PIPE-7) to the backend's id."""
        ...


class NullTicketProvider:
    """No ticketing backend configured: every op is a no-op that reports
    success. The manifest remains the sole source of truth."""

    enabled = False

    def create_epic(self, summary: str) -> str | None:
        return None

    def create_story(
        self, summary: str, description: str, epic_id: str | None, label: str,
    ) -> str | None:
        return None

    def set_state(
        self, story_key: str, state: LogicalState, plan_name: str | None = None,
    ) -> bool:
        return True

    def resolve_key(self, story_key: str) -> str:
        return story_key


class PlaneTicketProvider:
    """Delegates to the existing module-level Plane functions unchanged, so
    it shares their caches (_state_cache/_label_cache) and their retry/
    never-raise contract (_plane_set_state) rather than reimplementing it."""

    @property
    def enabled(self) -> bool:
        return _plane_enabled()

    def create_epic(self, summary: str) -> str | None:
        # Epics are an optional Plane module; some instances/API versions do
        # not expose the /epics/ endpoint. Any failure here - a 404 from an
        # unsupported module just as much as a connection refused/timeout/
        # other transport error - must fall back to ungrouped issues rather
        # than propagate out of ingest_plan, matching create_story below.
        try:
            resp = plane_request("POST", f"/projects/{PLANE_PROJECT}/epics/",
                                  json={"name": summary})
            return resp["id"]
        except Exception as e:
            print(f"Warning: Plane create_epic({summary!r}) failed, "
                  f"continuing without a Plane epic: {e}")
            return None

    def create_story(
        self, summary: str, description: str, epic_id: str | None, label: str,
    ) -> str | None:
        # Mirrors create_epic: a ticketing outage (connection refused,
        # timeout, non-2xx response, ...) must not block ingest_plan, which
        # falls back to a local/synthetic story key when this returns None.
        try:
            label_id = _get_or_create_label(label)
            backlog_state = _get_state("backlog")
            issue_resp = plane_request("POST", f"/projects/{PLANE_PROJECT}/work-items/", json={
                "name": summary,
                "description": description,
                "state": backlog_state,
                "labels": [label_id],
            })
            issue_id = issue_resp["id"]
        except Exception as e:
            print(f"Warning: Plane create_story({summary!r}) failed, "
                  f"falling back to a local/synthetic story key: {e}")
            return None
        # The epic link is a second, separate call after a real issue has
        # already been created. Its failure must only degrade the link (the
        # issue stays ungrouped, like create_epic's own optional-module
        # fallback) - not discard the just-created issue_id, which would
        # orphan a real Plane ticket and cause a retried ingest_plan to
        # create a duplicate.
        if epic_id is not None:
            try:
                plane_request("POST", f"/projects/{PLANE_PROJECT}/epics/{epic_id}/issues/",
                              json={"issue_id": issue_id})
            except Exception as e:
                print(f"Warning: Plane create_story({summary!r}) succeeded but "
                      f"linking issue {issue_id!r} to epic {epic_id!r} failed, "
                      f"continuing without the epic link: {e}")
        return issue_id

    def set_state(
        self, story_key: str, state: LogicalState, plan_name: str | None = None,
    ) -> bool:
        return _plane_set_state(story_key, _PLANE_STATE_GROUP[state], plan_name)

    def resolve_key(self, story_key: str) -> str:
        return _resolve_issue_uuid(story_key)


class JiraTicketProvider:
    """Documented extension point, not a working implementation.

    A real Jira Cloud integration needs to handle three things this stub
    does not: transitions go through POST /issue/{key}/transitions (Jira has
    no single `state` PATCH like Plane's), auth is `Authorization: Basic
    base64(email:api_token)` (or OAuth bearer) rather than Plane's
    `X-API-Key`, and issue descriptions must be Atlassian Document Format
    (ADF), not plain text. See TICKETING_ABSTRACTION_PLAN.md S5.
    """

    enabled = True

    def _unimplemented(self) -> None:
        raise NotImplementedError(
            "JiraTicketProvider is a documented stub, not a working "
            "integration. See TICKETING_ABSTRACTION_PLAN.md S5 for what a "
            "real implementation must handle (transition IDs, Basic/Bearer "
            "auth, ADF description format)."
        )

    def create_epic(self, summary: str) -> str | None:
        self._unimplemented()

    def create_story(
        self, summary: str, description: str, epic_id: str | None, label: str,
    ) -> str | None:
        self._unimplemented()

    def set_state(
        self, story_key: str, state: LogicalState, plan_name: str | None = None,
    ) -> bool:
        self._unimplemented()

    def resolve_key(self, story_key: str) -> str:
        self._unimplemented()


_TICKET_PROVIDERS: dict[str, type] = {
    "plane": PlaneTicketProvider,
    "jira": JiraTicketProvider,
}


def get_ticket_provider() -> TicketProvider:
    """Resolve the active ticketing backend from PIPELINE_TICKET_PROVIDER.

    "auto" (default) preserves today's behavior exactly: Plane if configured,
    else the no-op provider. "none" forces the no-op provider even when Plane
    vars are set. "plane"/"jira" force that backend, erroring clearly if its
    required config is missing rather than silently falling back.
    """
    choice = os.environ.get("PIPELINE_TICKET_PROVIDER", "auto").strip().lower()
    if choice == "auto":
        return PlaneTicketProvider() if _plane_enabled() else NullTicketProvider()
    if choice == "none":
        return NullTicketProvider()
    if choice == "plane" and not _plane_enabled():
        raise ValueError(
            "PIPELINE_TICKET_PROVIDER=plane requires PLANE_API_KEY, "
            "PLANE_WORKSPACE, and PLANE_PROJECT to all be set."
        )
    provider_cls = _TICKET_PROVIDERS.get(choice)
    if provider_cls is None:
        raise ValueError(
            f"Unknown PIPELINE_TICKET_PROVIDER={choice!r}; expected one of "
            f"'auto', 'none', {sorted(_TICKET_PROVIDERS)}."
        )
    return provider_cls()


def _venv_python_for(cwd: Path) -> Path | None:
    """Locate a project venv interpreter for running pytest, or None.

    A git worktree does not contain ``.venv`` (it is gitignored), so a bare
    ``pytest`` run from a worktree resolves to whatever interpreter is on PATH
    — which may be a different Python than the project venv and lack its deps
    (e.g. fastapi). That makes the test gate false-fail on otherwise-green
    work (collection error / import errors), blocking every Python story.

    Resolve the venv via the worktree's git link: ``git rev-parse
    --git-common-dir`` points at the main repo's ``.git``, whose parent holds
    ``.venv``. Also check ``cwd/.venv`` directly for a non-worktree checkout.
    Returns None when no venv is found so the caller falls back to bare
    ``pytest`` (preserving the existing contract for repos without a venv).
    """
    candidates = [cwd / ".venv" / "bin" / "python"]
    try:
        common = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if common:
            common_path = Path(common)
            if not common_path.is_absolute():
                common_path = (cwd / common_path).resolve()
            candidates.append(common_path.parent / ".venv" / "bin" / "python")
    except Exception:
        pass
    for cand in candidates:
        if cand.exists():
            return cand
    return None


def _test_command_for(cwd: Path) -> list[str] | None:
    """Return the test command for cwd if a recognized build marker is present."""
    if (cwd / "pom.xml").exists():
        return ["mvn", "test"]
    if (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists():
        return ["./gradlew", "test"]
    if (cwd / "package.json").exists():
        if (cwd / "yarn.lock").exists():
            return ["yarn", "test"]
        return ["npm", "test"]
    if (cwd / "Makefile").exists():
        result = subprocess.run(
            ["grep", "-q", "^test:", "Makefile"], cwd=cwd, capture_output=True
        )
        if result.returncode == 0:
            return ["make", "test"]
    if (cwd / "pyproject.toml").exists() or (cwd / "setup.py").exists():
        venv_python = _venv_python_for(cwd)
        if venv_python is not None:
            return [str(venv_python), "-m", "pytest"]
        return ["pytest"]
    if (cwd / "Cargo.toml").exists():
        return ["cargo", "test"]
    return None


def _build_command_for(cwd: Path) -> list[str] | None:
    """Return the build command for cwd if a recognized build marker
    declares one, or None if this project has no detectable build step (a
    library with no bundling step, a package.json with no "build" script,
    etc). Deliberately conservative/allow-listed - only ecosystems where a
    build step is unambiguous."""
    pkg = cwd / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
        except ValueError:
            data = {}
        if isinstance(data.get("scripts"), dict) and "build" in data["scripts"]:
            if (cwd / "yarn.lock").exists():
                return ["yarn", "build"]
            return ["npm", "run", "build"]
    if (cwd / "Cargo.toml").exists():
        return ["cargo", "build"]
    return None


def detect_build_command(cwd: Path) -> tuple[Path, list[str]] | None:
    """Detect the build command and directory to run it in, mirroring
    detect_test_command's cwd-then-immediate-subdirectory search. Returns
    None if no recognized build marker declares a build step anywhere - a
    repo without a build step must not be blocked by the build gate (unlike
    detect_test_command, there is no reasonable universal fallback for
    "build")."""
    cmd = _build_command_for(cwd)
    if cmd is not None:
        return cwd, cmd
    for child in sorted(p for p in cwd.iterdir() if p.is_dir() and not p.name.startswith(".")):
        cmd = _build_command_for(child)
        if cmd is not None:
            return child, cmd
    return None


def detect_test_command(cwd: Path) -> tuple[Path, list[str]]:
    """Detect the appropriate test command and the directory to run it in.

    Checks cwd first, then falls back to an immediate subdirectory (e.g.
    engine/) for projects where the buildable project does not live at the
    repo root.
    """
    cmd = _test_command_for(cwd)
    if cmd is not None:
        return cwd, cmd

    for child in sorted(p for p in cwd.iterdir() if p.is_dir() and not p.name.startswith(".")):
        cmd = _test_command_for(child)
        if cmd is not None:
            return child, cmd

    return cwd, ["npm", "test"]  # fallback


# ---------- Persona helpers ----------
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


def _persona_path(persona: str) -> Path:
    # Persona files are named lowercase-hyphenated (e.g. "security-engineer.md").
    # Normalize here so every caller matches regardless of the case the persona
    # string arrives in - callers like _persona_requires_claude already lowercase
    # for their own comparison, and this must agree or a mixed-case persona value
    # (e.g. "Security-Engineer") resolves the routing decision correctly but then
    # crashes loading the prompt body (masked on case-insensitive filesystems).
    return AGENTS_DIR / f"{persona.lower()}.md"


def _persona_body(persona: str) -> str:
    """Return a persona's system prompt (the .md body with frontmatter stripped)."""
    path = _persona_path(persona)
    if not path.exists():
        raise FileNotFoundError(f"No persona named {persona} at {path}")
    return _FRONTMATTER_RE.sub("", path.read_text(), count=1).strip()


def _persona_default_model(persona: str) -> str | None:
    """Return the model declared in a persona's frontmatter, or None if unknown."""
    path = _persona_path(persona)
    if not path.exists():
        return None
    m = re.search(r'^model:\s*"?([\w.-]+)"?\s*$', path.read_text(), re.MULTILINE)
    return m.group(1) if m else None


# Tool allow-lists per persona; reviewers must not modify the tree.
_PERSONA_TOOLS = {
    "code-reviewer": "Bash,Read",
}


def _allowed_tools_for(persona: str | None) -> str:
    return _PERSONA_TOOLS.get(persona or "", "Bash,Edit,Write,Read")


def _build_dispatch_command(
    story: dict[str, Any], story_key: str,
    plan_name: str | None = None,
    resume_journal: list[dict[str, Any]] | None = None,
    review_feedback: str | None = None,
) -> dict[str, Any]:
    """Build the backend-agnostic dispatch spec for a story: its prompt,
    persona system prompt, model tier, and tool allow-list. The chosen
    Backend (see backend.py) turns this into whatever it needs to actually
    run - a `claude` argv, an OpenHands invocation, etc.

    If resume_journal is given (a non-empty checkpoint journal from an
    interrupted run), the prompt is seeded with the steps already completed
    and committed plus the last checkpoint's next_hint, so the agent
    continues instead of redoing finished work.

    If plan_name is given, the prompt instructs the agent to call the
    checkpoint tool after each idempotent step, so a kill (e.g. the usage
    gate interrupting it) leaves it resumable rather than losing the run.

    Raises FileNotFoundError if the story names a persona that does not exist.
    """
    persona = story.get("persona")
    checkpoint_instruction = ""
    if plan_name:
        checkpoint_instruction = (
            f"After completing each meaningful, idempotent step, call the "
            f'checkpoint tool (plan_name="{plan_name}", story_key="{story_key}", '
            f"step=<short id>, summary=<what you did>, next_hint=<what to do "
            f"next>) so your progress is resumable if you are interrupted.\n\n"
        )
    rework_instruction = ""
    if review_feedback:
        rework_instruction = (
            f"The code reviewer REQUESTED CHANGES on the previous attempt. "
            f"Address this feedback before finishing:\n{review_feedback}\n\n"
        )
    if resume_journal:
        completed = "\n".join(
            f"  - [{e['step']}] {e['summary']}" for e in resume_journal
        )
        next_hint = resume_journal[-1].get("next_hint") or "Review the worktree state and continue."
        prompt = (
            f"You are RESUMING issue {story_key}: {story['summary']}\n\n"
            f"{story.get('agent_instructions', '')}\n\n"
            f"This story was previously interrupted. The following steps are "
            f"already completed and committed — do not redo them:\n{completed}\n\n"
            f"Continue from here: {next_hint}\n\n"
            f"{rework_instruction}"
            f"{checkpoint_instruction}"
            f"When finished, commit your work, push the branch, and exit."
        )
    else:
        prompt = (
            f"You are completing issue {story_key}: {story['summary']}\n\n"
            f"{story.get('agent_instructions', '')}\n\n"
            f"{rework_instruction}"
            f"{checkpoint_instruction}"
            f"When finished, commit your work, push the branch, and exit."
        )
    model = (
        story.get("model")
        or (_persona_default_model(persona) if persona else None)
        or DEFAULT_MODEL
    )
    return {
        "prompt": prompt,
        "system": _persona_body(persona) if persona else None,
        "model": model,
        "allowed_tools": _allowed_tools_for(persona),
    }


# ---------- Overlord / decision helpers ----------
def _load_policy() -> str:
    """Concatenate the global decision policy with any per-repo override."""
    parts = []
    if POLICY_PATH.exists():
        parts.append(POLICY_PATH.read_text())
    override = REPO_ROOT / ".overlord-policy.md"
    if override.exists():
        parts.append("\n\n## Per-repository override\n\n" + override.read_text())
    return "\n".join(parts)


def _invoke_overlord(prompt: str, plan_role_config: dict | None = None) -> str:
    """Run the overlord persona headless and return its raw stdout.

    External boundary: delegates to the configured Backend. Tests mock this
    function. Provider/model fall through role_registry (PIPELINE_BACKEND_
    OVERLORD / a plan's role_config / model_registry.json's "overlord"
    entry), falling back to the persona's declared tier ("opus") when none
    of those apply - so an unconfigured install resolves identically to
    before role_registry existed. Passing name=resolution.provider
    explicitly (rather than relying on get_backend's own internal env
    lookup, as before) is required so a registry/plan-configured provider
    actually takes effect.
    """
    system = _persona_body("overlord")
    resolution = role_registry.resolve_role(
        "overlord", plan_role_config=plan_role_config,
        model_fallback=lambda: _persona_default_model("overlord") or "opus",
    )
    return backend.get_backend("overlord", name=resolution.provider).complete(
        prompt, system=system, model=resolution.model, allowed_tools="Read",
    )


# GUIDED_DECOMPOSITION_PLAN.md: a "tech lead" planner call that turns a
# coarse story into an ordered sub-step checklist for the weak local
# executor to work through inside its own single worktree/transcript. This
# is deliberately NOT the story-splitting approach already tried and
# disproven (tests/benchmark/PRODUCT_ANALYST_VALIDATION_PLAN.md) - the
# checklist augments one story's prompt, it never creates new stories or
# new cold dispatches.
_PLANNER_SYSTEM = (
    "You are a tech lead writing an implementation checklist for a junior "
    "engineer who will work alone and may not reason precisely through "
    "subtle edge cases unassisted. Read the task below and produce an "
    "ordered checklist of concrete sub-steps (as many as the task genuinely "
    "needs - typically 3-10; split a step further rather than bundling "
    "tricky reasoning into one line), each with a short, verifiable "
    "done-criterion. Preserve test-driven-development ordering: a failing "
    "test before the implementation that makes it pass. For any step "
    "involving timing, state mutation, or a behavior that is easy to get "
    "subtly wrong (e.g. what happens on a rejected/failed call, a "
    "backwards-moving clock, or a read that must not have side effects), "
    "include a concrete worked example with actual numbers showing the "
    "correct result, and name the specific mistake a less careful "
    "implementation would make there. If the edge case touches state that "
    "persists across calls (a clock, counter, or high-water mark), the "
    "worked example must not stop at that one call's return value - trace "
    "at least one follow-up call afterward and confirm the state left "
    "behind still produces the correct result for it. A common mistake: "
    "correctly computing the CURRENT call's result (e.g. clamping a "
    "rejected/backwards step to no-op) while still overwriting the tracked "
    "state with a value that corrupts a later comparison - getting the "
    "immediate return value right is not sufficient if it leaves the "
    "object in a bad state for what comes next. Do not invent scope beyond "
    "what the task describes. "
    "CRITICAL STEERING for the junior engineer: all implementation work goes "
    "in the ONE implementation file named by the task; NEVER edit, rename, "
    "weaken, or delete the test files (anything matching test_*.py). If a "
    "test fails, the bug is in the implementation file - fix it there, never "
    "change the test. "
    "EDITING MECHANICS: this engineer reliably fails at surgical str_replace "
    "edits - they cannot construct a unique, matching old_str (observed "
    "live: every str_replace in a stubs-then-edit loop is rejected as "
    "'old_str occurs N times' or 'old_str not found', so they never make "
    "progress past stubs). Direct them to write COMPLETE files via "
    "create_file in one shot instead of a stubs-then-surgically-edit "
    "sequence: each implementation step should produce the WHOLE file with "
    "every method fully implemented (no `raise NotImplementedError` stubs "
    "to be filled in later by str_replace). If a fix is needed after running "
    "tests, rewrite the whole file via create_file again, do not str_replace. "
    "Make the checklist's FIRST line the steering rule above (name the "
    "implementation file and say: do not edit the test files), then the "
    "numbered sub-steps. Output ONLY the checklist - the steering line, "
    "then the numbered steps - no other preamble, no closing remarks."
)

# Optional clause spliced into _PLANNER_SYSTEM when the H3 scratchpad is on.
# Rationale (GUIDED_DECOMPOSITION_PLAN.md, 2026-07-16): a trailing "also keep
# a scratchpad" aside appended after the checklist was consumed in only 2 of
# 22 guided runs (9%) - the executor follows the numbered checklist and
# ignores anything outside it. Making the planner fold the scratchpad update
# INTO each step (a first-class item with its own action) is the fix, so the
# executor treats it as part of the work rather than an afterthought. Kept
# separate from _PLANNER_SYSTEM (not concatenated into the constant) so the
# ablation "off" arm and the existing by-reference tests still see the base
# prompt unchanged.
_PLANNER_SCRATCHPAD_CLAUSE = (
    " SCRATCHPAD (state memory): the junior engineer keeps a running note "
    "file .agent_scratchpad.md across steps. Fold this into the checklist as "
    "explicit actions, not a side remark: make the very first numbered step "
    "create .agent_scratchpad.md (via create_file) listing the planned steps, "
    "and end each subsequent numbered step with '- then update "
    ".agent_scratchpad.md: mark this step done and note the next step (rewrite "
    "the whole file via create_file).' Treat updating the scratchpad as part "
    "of a step's done-criterion, so it is never skipped."
)


def _planner_system(*, include_scratchpad: bool = False) -> str:
    """The planner's system prompt, optionally augmented with the scratchpad
    clause. Base prompt (_PLANNER_SYSTEM) is returned unchanged when the
    scratchpad is off, preserving the H3 ablation and the by-reference tests."""
    if not include_scratchpad:
        return _PLANNER_SYSTEM
    return _PLANNER_SYSTEM + _PLANNER_SCRATCHPAD_CLAUSE


def _resolve_planner_backend(
    mode: str, dispatch_backend: str, local_model: str,
    plan_role_config: dict | None = None,
) -> tuple[str, str]:
    """Shared backend/model resolution for both the initial-dispatch planner
    and the rework-feedback planner (same mode semantics, same "principal
    tech lead vs weak local model" choice - see _run_planner).

    mode="local" makes the planner independently routable
    (PIPELINE_BACKEND_PLANNER, a plan's role_config, or model_registry.json's
    "planner" entry) instead of always mirroring dispatch's own
    backend/model - e.g. dispatch on ollama with planner pinned to mlx. When
    none of those name a provider, it mirrors dispatch_backend/local_model
    exactly as before this existed, so an unconfigured install is unchanged.
    """
    if mode == "cloud":
        return "claude", (
            os.environ.get("PIPELINE_DECOMPOSE_CLOUD_MODEL")
            or _persona_default_model("overlord") or "opus"
        )
    plan_cfg = (plan_role_config or {}).get("planner", {})
    registry = role_registry.load_registry()
    provider_override = (
        plan_cfg.get("provider")
        or os.environ.get("PIPELINE_BACKEND_PLANNER")
        or registry.get("roles", {}).get("planner", {}).get("provider")
    )
    if not provider_override:
        return dispatch_backend, local_model
    resolution = role_registry.resolve_role(
        "planner", plan_role_config=plan_role_config, registry=registry,
        model_fallback=lambda: local_model,
    )
    return resolution.provider, resolution.model


def _run_planner(
    agent_instructions: str, *, mode: str, dispatch_backend: str, local_model: str,
    include_scratchpad: bool = False, plan_role_config: dict | None = None,
) -> str | None:
    """Call a bounded, single-turn LLM to produce an ordered sub-step
    checklist for agent_instructions.

    This is one complete() call, never an agent loop - it must stay cheap
    relative to the story's own dispatch or the economics this feature
    exists for collapse (see GUIDED_DECOMPOSITION_PLAN.md §3.1/§4.5).

    mode="cloud" routes the call to the Claude backend at the same
    "principal" tier _invoke_overlord uses - the primary, expected
    configuration (a strong tech lead planning for a weak jr executor).
    mode="local" routes the call to the same backend/model the executor
    itself will run on (the H2 ablation: is planner *strength* the active
    ingredient, or does having any checklist help regardless of who wrote
    it?).

    Returns the raw checklist text, or None on any failure. Callers MUST
    treat None as "no plan" and fall open to the existing no-plan dispatch
    path - a broken, slow, or rate-limited planner call must never block or
    corrupt a story's dispatch. External boundary: delegates to the
    configured Backend. Tests mock this function.
    """
    backend_name, model = _resolve_planner_backend(
        mode, dispatch_backend, local_model, plan_role_config=plan_role_config,
    )
    try:
        text = backend.get_backend("planner", name=backend_name).complete(
            agent_instructions,
            system=_planner_system(include_scratchpad=include_scratchpad),
            model=model,
        )
    except Exception:
        # Broad and intentional: this call must never be a gate. Mirrors
        # the Gap-7 multi-model warning's "observability hook, never a
        # gate" except-Exception pattern elsewhere in dispatch_story.
        return None
    text = (text or "").strip()
    return text or None


# The same "too high-level for a jr model" problem that motivates the
# initial-dispatch checklist applies to rework: a reviewer's prose feedback
# (diagnosis + implicit fix reasoning) is itself a coarse brief. Translating
# it into an explicit fix-checklist before handing it to the weak executor
# is the same tech-lead-decomposition logic applied one step later in the
# story's lifecycle.
_REWORK_PLANNER_SYSTEM = (
    "You are a tech lead helping a junior engineer act on code review "
    "feedback; they may not reason precisely through subtle edge cases "
    "unassisted. Read the review feedback below and produce an ordered "
    "checklist of concrete fix steps: what is wrong, which file/lines are "
    "implicated, and how to verify the fix (e.g. a test to add or run). If "
    "the bug involves timing, state mutation, or another subtle edge case, "
    "include a concrete worked example with actual numbers showing the "
    "correct result, and name the specific mistake that produced the wrong "
    "one. If the edge case touches state that persists across calls (a "
    "clock, counter, or high-water mark), the worked example must not stop "
    "at that one call's return value - trace at least one follow-up call "
    "afterward and confirm the state left behind still produces the "
    "correct result for it; getting the immediate return value right is "
    "not sufficient if it leaves the object in a bad state for what comes "
    "next. Preserve test-driven-development ordering where it applies "
    "(reproduce the bug with a failing test before fixing it). Do not "
    "invent issues beyond what the feedback describes. "
    "CRITICAL STEERING for the junior engineer: fixes go in the "
    "implementation file named by the task; NEVER edit, rename, weaken, or "
    "delete the test files (anything matching test_*.py) to make a test "
    "pass - if a test fails, the bug is in the implementation, so fix it "
    "there. "
    "EDITING MECHANICS: this engineer reliably fails at surgical str_replace "
    "edits (cannot construct a unique matching old_str). Direct them to "
    "rewrite the WHOLE implementation file via create_file with every method "
    "fully fixed in one shot, not a sequence of str_replace patches. "
    "Make the checklist's FIRST line the steering rule above, then the "
    "numbered fix steps. Output ONLY the checklist - the steering line, "
    "then the numbered steps - no other preamble, no closing remarks."
)


def _run_rework_planner(
    review_feedback: str, *, mode: str, dispatch_backend: str, local_model: str,
    plan_role_config: dict | None = None,
) -> str | None:
    """Like _run_planner, but translates code-review feedback into an
    ordered fix-checklist instead of translating a coarse task into an
    implementation checklist. Same bounded single-call contract, same
    mode="cloud"/"local" backend resolution, same fail-open-to-None
    contract - see _run_planner's docstring for the shared rationale.
    """
    backend_name, model = _resolve_planner_backend(
        mode, dispatch_backend, local_model, plan_role_config=plan_role_config,
    )
    try:
        text = backend.get_backend("planner", name=backend_name).complete(
            review_feedback, system=_REWORK_PLANNER_SYSTEM, model=model,
        )
    except Exception:
        return None
    text = (text or "").strip()
    return text or None


# ---------- Decompose (provider-configurable product-analyst) ----------
def _extract_json_block(text: str) -> str:
    """Strip a ```json ... ``` / ``` ... ``` fence around a JSON payload, if
    present, else return the text unchanged (trimmed). Models routinely wrap
    JSON output in a markdown fence even when asked not to; callers
    json.loads() the result themselves and handle a parse failure - this
    only handles the fence, not validation."""
    stripped = text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", stripped, re.DOTALL)
    return m.group(1).strip() if m else stripped


def _run_decompose(request: str, *, plan_role_config: dict | None = None) -> str | None:
    """Call a bounded, single-turn LLM (the product-analyst persona) to turn
    a raw goal/feature request into epics/stories JSON matching save_plan's
    schema.

    Structurally identical to _run_planner/_invoke_overlord: one complete()
    call, never an agent loop. Provider/model fall through role_registry
    (PIPELINE_BACKEND_DECOMPOSE / a plan's role_config / model_registry
    .json's "decompose" entry), falling back to Claude at the persona's
    declared tier when none of those apply. Fails open (returns None) on
    any exception - a broken/slow/rate-limited decompose call must never
    raise past this function, mirroring _run_planner's contract.
    """
    resolution = role_registry.resolve_role(
        "decompose", plan_role_config=plan_role_config,
        model_fallback=lambda: _persona_default_model("product-analyst") or "opus",
    )
    try:
        text = backend.get_backend("decompose", name=resolution.provider).complete(
            request, system=_persona_body("product-analyst"), model=resolution.model,
            allowed_tools="Read",
        )
    except Exception:
        return None
    text = (text or "").strip()
    return text or None


def _parse_ruling(text: str) -> dict[str, Any]:
    """Parse the overlord's output contract into a structured ruling."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"\s*(RULING|TIER|RISK|RATIONALE|NOTIFY_USER)\s*:\s*(.*)", line)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return {
        "ruling": fields.get("RULING", ""),
        "tier": fields.get("TIER", "").lower(),
        "risk": fields.get("RISK", "").lower(),
        "rationale": fields.get("RATIONALE", ""),
        "notify_user": fields.get("NOTIFY_USER", "no").lower() in ("yes", "true"),
    }


# ---------- Review / PR helpers ----------
def _run_reviewer(
    worktree: str, branch: str, backend_name: str | None = None,
    plan_role_config: dict | None = None,
    acceptance: list[dict] | None = None,
) -> str:
    """Run the code-reviewer persona over a branch and return its raw output.

    External boundary: delegates to the configured Backend. Tests mock this
    function. backend_name lets a caller override the env-resolved default
    (e.g. review_story's rate-limit fallback routing to "local");
    get_backend already treats name=None as "use the env-resolved default".

    acceptance is the story's acceptance block (Mode 20, 2026-07-17): when
    present and the detected test command is pytest, the reviewer's test
    command is scoped to ONLY those paths, exactly like _reverify_acceptance
    scopes the pre-merge re-check. Without this, the reviewer's own free-form
    `pytest` invocation can rediscover and block on a bug in the AGENT'S OWN
    test file even when the harness's acceptance oracle already passes -
    FM-A's exact root cause (see project-benchmark-failure-modes memory),
    resurrected here because the harness test gate was scoped but the
    reviewer never was.
    """
    body = _persona_body("code-reviewer")
    # Provider/model fall through role_registry (PIPELINE_BACKEND_REVIEW /
    # a plan's role_config / model_registry.json's "review" entry), falling
    # back to the persona's declared tier when none of those apply - so an
    # unconfigured install resolves identically to before role_registry
    # existed. backend_name (an explicit caller override, e.g. review_story's
    # FM-B rate-limit fallback) always wins over the registry-resolved
    # provider, exactly as it already won over the plain env lookup.
    resolution = role_registry.resolve_role(
        "review", plan_role_config=plan_role_config,
        model_fallback=lambda: _persona_default_model("code-reviewer") or DEFAULT_MODEL,
    )
    model = resolution.model
    # Asymmetric review: software-engineer.md and code-reviewer.md both
    # declare `model: sonnet`, so without an override dispatch and review
    # resolve to the identical concrete local model - a model reviewing its
    # own work with identical weights. When the review backend is actually
    # local, an explicit PIPELINE_LOCAL_REVIEW_MODEL overrides the tier so
    # review can run on a different (e.g. stronger) local model - and stays
    # the top-priority override even when the registry also configures a
    # model, since it is the most specific, most recently-set knob. Gated on
    # backend == "local" so a bare Ollama tag never leaks into a cloud
    # review as a bogus --model value. backend_name may already be the
    # explicit "local" (review_story's FM-B rate-limit fallback); otherwise
    # fall back to the registry-resolved provider, mirroring how get_backend
    # itself treats name=None.
    resolved_backend = (backend_name or resolution.provider).strip().lower()
    if resolved_backend in _LOCAL_BACKEND_NAMES:
        review_model_override = os.environ.get("PIPELINE_LOCAL_REVIEW_MODEL")
        if review_model_override:
            model = review_model_override
    # Only pass an explicit resolved name to get_backend when a plan/registry
    # override actually named a provider - otherwise keep passing
    # backend_name (None in the common case) unchanged, so an unconfigured
    # install still relies on get_backend's own internal PIPELINE_BACKEND_
    # REVIEW lookup exactly as before (behaviorally identical either way,
    # but this preserves what a mocked get_backend observes).
    plan_cfg_review = (plan_role_config or {}).get("review", {})
    registry_review_provider = (
        role_registry.load_registry().get("roles", {}).get("review", {}).get("provider")
    )
    name_for_get_backend = backend_name
    if backend_name is None and (plan_cfg_review.get("provider") or registry_review_provider):
        name_for_get_backend = resolution.provider
    # The reviewer model has no access to detect_test_command's Python-level
    # venv resolution, so a bare "Run the test suite" instruction leaves it
    # to guess a shell command - e.g. the relative `.venv/bin/python -m
    # pytest`, which does not exist inside a worktree (worktrees are
    # gitignored and never contain .venv). Resolve the same command
    # check_story_status's test gate trusts and hand it over verbatim. Any
    # resolution failure (nonexistent worktree, no recognized build marker)
    # must not block review - fall back to the generic instruction below.
    test_command_instruction = ""
    try:
        test_dir, test_cmd = detect_test_command(Path(worktree))
        scope_note = ""
        if acceptance:
            acceptance_paths = [
                str(test_dir / entry["path"]) for entry in acceptance
            ]
            scoped = _scope_test_cmd_to_acceptance(test_cmd, acceptance_paths, test_dir)
            if scoped is not None:
                test_cmd = scoped
                scope_note = (
                    "This story carries a harness-owned acceptance oracle; the "
                    "command below is scoped to ONLY those acceptance tests, "
                    "which are the authoritative spec for required behavior. A "
                    "failure in the implementer's OWN test file that the "
                    "acceptance oracle does not require is not sufficient "
                    "grounds for REQUEST_CHANGES on its own - note it as a "
                    "Suggestion if you notice it, but base your verdict on the "
                    "acceptance oracle plus your own code-quality/security "
                    "review, not on re-running the implementer's full test "
                    "file.\n\n"
                )
        test_command_instruction = (
            f"{scope_note}"
            f"Run the test suite with exactly this command (do not "
            f"substitute a different interpreter path): cd "
            f"{shlex.quote(str(test_dir))} && {shlex.join(test_cmd)}\n\n"
        )
    except Exception:
        pass
    prompt = (
        f"{test_command_instruction}"
        f"Review the changes on branch {branch} in this worktree against our "
        f"standards. Run the test suite. Specifically check: (1) any function "
        f"taking a mutable argument (list, dict, set) does not mutate it in "
        f"place unless that is the documented contract; (2) inputs are "
        f"validated at system boundaries, including negative/out-of-range "
        f"numeric arguments, not just the happy path; (3) documentation - "
        f"but calibrate this to our Blocking-vs-Suggestion policy, don't treat every doc gap as a blocker; (4) if the change adds a module that other production files import from (a wrapper/adapter/binding shim), it must trace what that module actually calls and flag any module that reimplements logic it should delegate to as Blocking. For example, replacing a crypto/WASM/native binding with a pure-language no-op or a base64 round‑trip placeholder is not sufficient evidence; a green test suite alone does not prove delegation is real."
        f"that EXISTING callers/users already depend on (a public API "
        f"contract, configuration, CLI flag, or user-facing functionality "
        f"that predates this change) and no documentation update accompanies "
        f"it, that's a genuine problem: REQUEST_CHANGES and name the "
        f"specific doc (a README or other in-repo doc) that needs updating. "
        f"For a brand-new addition with no "
        f"existing external callers yet (e.g. a new module/class/function "
        f"nothing else in the repo calls), a missing doc update is a "
        f"Suggestion, not a blocker - note it in your summary but don't "
        f"REQUEST_CHANGES for that reason alone if the code itself is "
        f"correct and tested.\n\n"
        f"For large diffs: bash output is truncated to 3000 chars per call, "
        f"so a bare `git diff` may silently cut off. Start with "
        f"`git diff --stat` to see the scope, then use `git diff -- <file>` "
        f"per file (or `git diff <commit>` for a range), and `view_file` "
        f"for surrounding context. Do NOT rely on a single `git diff` for "
        f"a multi-file change. End with your VERDICT line; if you APPROVE, "
        f"also include a PR title and body."
    )
    # cell_dir points at the worktree's parent directory. In production
    # that's ~/.claude/worktrees/; in the benchmark it's
    # <cell>/worktrees/, which the harness preserves across all trials
    # of a cell (worktrees/<story_key>/ is removed on merge, but the
    # surrounding worktrees/ dir is not). The driver writes a per-call
    # token-cost sidecar there so the data survives the worktree
    # cleanup that wipes review.log. None for live (non-benchmark)
    # reviews whose worktree lives somewhere we shouldn't be
    # scribbling new files into: in that case the driver silently
    # skips the sidecar.
    if Path(worktree).parent.name == "worktrees":
        cell_dir = str(Path(worktree).resolve().parent)
    else:
        cell_dir = None
    return backend.get_backend("review", name=name_for_get_backend).complete(
        prompt, system=body, model=model, allowed_tools="Bash,Read", cwd=worktree,
        max_tokens=int(os.environ.get("PIPELINE_REVIEW_MAX_TOKENS", "4096")),
        cell_dir=cell_dir,
    )


def _run_security_reviewer(worktree: str, branch: str) -> str:
    """Run the security-engineer persona over a branch and return its raw output.

    External boundary: delegates to the configured Backend (always Claude —
    security-engineer is in _LOCAL_SKIP_PERSONAS). Tests mock this function.
    """
    body = _persona_body("security-engineer")
    model = _persona_default_model("security-engineer") or DEFAULT_MODEL
    prompt = (
        f"Perform a security review of the changes on branch {branch} in this "
        f"worktree. Check for OWASP issues, secrets, injection, auth/authz "
        f"bypasses, and Secure-by-Design violations. Run the test suite. "
        f"End with your VERDICT line: APPROVE or REQUEST_CHANGES."
    )
    if Path(worktree).parent.name == "worktrees":
        cell_dir = str(Path(worktree).resolve().parent)
    else:
        cell_dir = None
    return backend.get_backend("review", name="claude").complete(
        prompt, system=body, model=model, allowed_tools="Bash,Read", cwd=worktree,
        max_tokens=int(os.environ.get("PIPELINE_SECURITY_REVIEW_MAX_TOKENS",
                                      os.environ.get("PIPELINE_REVIEW_MAX_TOKENS", "4096"))),
        cell_dir=cell_dir,
    )


def _parse_verdict(text: str) -> str:
    m = re.search(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)", text, re.IGNORECASE)
    return m.group(1).upper() if m else "UNKNOWN"


# T11: a REQUEST_CHANGES response with no substantive findings text - just
# the VERDICT line itself, or whitespace around it - gives a redispatched
# agent nothing to act on. Checked only when _parse_verdict returns
# REQUEST_CHANGES, mirroring how _is_rate_limited/_is_transient_backend_error
# are checked only after UNKNOWN. Deliberately a bare emptiness check, not a
# length floor: this codebase's own reviewer-stub convention (see
# test_review_story_parks_after_rework_budget_exhausted and its siblings, all
# using "still bad\nVERDICT: REQUEST_CHANGES") treats even a terse one-line
# finding as genuine, so any non-whitespace content beyond the verdict line
# must count.
def _has_review_findings(text: str) -> bool:
    """True when `text` contains findings beyond the bare VERDICT line."""
    stripped = re.sub(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)", "", text, flags=re.IGNORECASE)
    return bool(stripped.strip())


# Anchors that identify an infrastructure rate-limit response, not a genuine
# review. Checked only when _parse_verdict returns UNKNOWN (i.e. no VERDICT
# line) so that a review discussing rate-limiting code is never misclassified.
# Deliberately specific to the backend's own rate-limit banner phrasing —
# generic terms like "429" or "resets" are excluded because a review of
# rate-limiter code (e.g. this repo's own token_bucket benchmark task) can
# legitimately contain them, which would misfire this check on a truncated
# but otherwise genuine review.
_RATE_LIMIT_PATTERNS = [
    r"hit your session limit",
    r"usage limit reached",
    r"out_of_credits",
    r"overageDisabledReason",
]


def _acceptance_rel_paths(story: dict[str, Any]) -> list[str]:
    """Return the worktree-root-relative paths of a story's acceptance fixtures."""
    return [entry["path"] for entry in (story.get("acceptance") or [])]


def _is_pytest_cmd(cmd: list[str]) -> bool:
    """True when `cmd` invokes pytest and can accept path arguments for scoping.

    Matches both `["pytest", ...]` and `[python, "-m", "pytest", ...]` forms
    produced by detect_test_command's venv-aware path (pipeline_mcp_server.py
    lines 392-393). Other runners (cargo, npm, mvn, ...) return False.
    """
    if not cmd:
        return False
    last = cmd[-1]
    return last == "pytest" or last.endswith("/pytest")


def _scope_test_cmd_to_acceptance(
    test_cmd: list[str], acceptance_paths: list[str], test_dir: Path
) -> list[str] | None:
    """Return ``test_cmd`` scoped to run ONLY the acceptance fixtures, or
    ``None`` when the runner can't be safely scoped to specific files (caller
    falls back to the full suite — the MBW safety net).

    This closes the FM-A family for non-pytest runners. A story carrying a
    harness-owned ``acceptance`` block must be graded on those oracle files
    alone, not on the implementer's own test file, whose assertions may be
    wrong (observed live: interval_merge_js wrote a correct src/merge.js —
    gt=True — but a buggy merge.test.js; unscoped ``npm test`` ran both and
    rejected correct work). Previously only pytest was scoped (``pytest
    <files>`` accepts path args); cargo/npm fell back to the full suite, so
    every non-pytest benchmark cell was graded on the implementer's own tests.

    Scoping is applied only where it is well-defined and safe; anything we
    can't scope correctly falls back to the full suite (no regression vs. the
    prior behavior for real-project stories using jest/mocha/etc.):

      - pytest: ``[pytest, *paths]`` (path args; unchanged).
      - cargo:  ``cargo test --test <stem>`` per acceptance fixture under
        ``tests/``. cargo names integration tests by file stem
        (``tests/test_acceptance.rs`` -> ``--test test_acceptance``), so this
        runs ONLY the oracle, excluding the implementer's own
        ``tests/test_<name>.rs``. Only applied when every acceptance path is
        a ``tests/*.rs`` integration test.
      - npm/yarn whose package.json ``test`` script IS ``node --test``:
        ``node --test <paths>``. Node's test runner accepts explicit paths.
        Only applied when the script starts with ``node --test`` (jest/mocha
        can't be safely scoped without knowing their filter flags).
    """
    if not test_cmd or not acceptance_paths:
        return None
    if _is_pytest_cmd(test_cmd):
        return [*test_cmd, *acceptance_paths]
    # cargo test --test <stem> ...
    if test_cmd[:2] == ["cargo", "test"]:
        stems: list[str] = []
        for p in acceptance_paths:
            pp = Path(p)
            if pp.suffix == ".rs" and pp.parent.name == "tests":
                stems.append(pp.stem)
            else:
                return None
        args: list[str] = []
        for s in stems:
            args += ["--test", s]
        return ["cargo", "test", *args]
    # npm test / yarn test whose script is `node --test ...`
    if test_cmd[:2] in (["npm", "test"], ["yarn", "test"]):
        pkg = Path(test_dir) / "package.json"
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
            test_script = str(scripts.get("test") or "").strip()
        except Exception:
            return None
        if test_script.startswith("node --test"):
            return ["node", "--test", *acceptance_paths]
        return None
    return None


def _is_rate_limited(text: str) -> bool:
    """True when `text` looks like an infra rate-limit message, not a review.

    Intentionally called only after _parse_verdict returns UNKNOWN, so a
    reviewer discussing rate-limit handling in the diff (which ends with a real
    VERDICT line) is never mistaken for a rate-limited call.
    """
    return any(re.search(pat, text, re.IGNORECASE) for pat in _RATE_LIMIT_PATTERNS)


# Transient-backend-error signatures distinct from rate-limiting. Like
# _RATE_LIMIT_PATTERNS, checked only when _parse_verdict returns UNKNOWN (no
# VERDICT line) so a review discussing HTTP 500 handling is never misclassified.
_TRANSIENT_BACKEND_PATTERNS = [
    r"500\s+internal\s+server\s+error",
    r"internal\s+server\s+error",
    r"connection\s+reset",
    r"connection\s+refused",
]


def _is_transient_backend_error(text: str) -> bool:
    """True when `text` looks like a transient backend error (HTTP 500,
    connection-reset/refused), not a rate-limit message or a genuine review.

    Intentionally called only after _parse_verdict returns UNKNOWN, so a
    reviewer discussing 500-handling code (which ends with a real VERDICT
    line) is never mistaken for a transient backend failure.
    """
    return any(re.search(pat, text, re.IGNORECASE) for pat in _TRANSIENT_BACKEND_PATTERNS)


def _open_pr(worktree: str, story_key: str, story: dict[str, Any]) -> str:
    """Push the story's branch and open a PR for it via the gh CLI.

    External boundary: spawns `git`/`gh`. Tests mock this function (or
    subprocess.run) rather than hitting a real remote.
    """
    branch = f"agent/{story_key.lower()}"
    title = f"{story_key}: {story['summary']}"
    body = story.get("pr_body") or (
        f"Automated PR for {story_key} produced by the agent pipeline."
    )

    subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=worktree, check=True, capture_output=True, text=True,
    )
    try:
        proc = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body, "--head", branch],
            cwd=worktree, check=True, capture_output=True, text=True,
        )
        return proc.stdout.strip()
    except subprocess.CalledProcessError as e:
        # A dispatched agent's own Bash access can include `gh pr create`,
        # so a PR may already exist by the time review_story gets here.
        # Recover its URL instead of failing the whole pipeline tick.
        if "already exists" not in (e.stderr or ""):
            raise
        proc = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
            cwd=worktree, check=True, capture_output=True, text=True,
        )
        return proc.stdout.strip()


# ---------- Merge adjudication / notifications ----------
def _merge_decision(story: dict[str, Any]) -> dict[str, str]:
    """Pure decision: may a reviewed (pr_open) story merge unattended?

    Honors PIPELINE_AUTONOMY and PIPELINE_RISK_THRESHOLD. high-risk work is
    always parked for human review regardless of autonomy level.
    """
    if story.get("review_verdict") != "APPROVE":
        return {"action": "park", "reason": "not approved"}
    if PIPELINE_AUTONOMY == "dry-run":
        return {"action": "park", "reason": "dry-run"}

    risk_rank = _RISK_ORDER.get((story.get("risk") or "low").lower(), _RISK_ORDER["high"])
    if risk_rank >= _RISK_ORDER["high"]:
        return {"action": "park", "reason": "high risk held for human review"}
    if PIPELINE_AUTONOMY == "full":
        return {"action": "merge", "reason": "autonomy=full"}

    threshold = _RISK_ORDER.get(PIPELINE_RISK_THRESHOLD, _RISK_ORDER["low"])
    if risk_rank <= threshold:
        return {"action": "merge", "reason": f"risk <= threshold {PIPELINE_RISK_THRESHOLD}"}
    return {"action": "park", "reason": f"risk above threshold {PIPELINE_RISK_THRESHOLD}"}


def _merge_pr(worktree: str, story_key: str) -> str:
    """Squash-merge the story's PR, then remove its worktree and branches.

    External boundary: spawns `git`/`gh`. Tests mock this function (or
    subprocess.run) rather than hitting a real remote.

    Deliberately does not pass --delete-branch to `gh pr merge`: that asks
    gh to switch the local checkout away from the branch being deleted,
    which fails here because the branch is checked out in its own worktree
    while REPO_ROOT has another branch checked out (the normal state for
    this pipeline's one-worktree-per-story model). Branch/worktree cleanup
    is done explicitly below, from REPO_ROOT, after the merge succeeds.
    """
    branch = f"agent/{story_key.lower()}"
    proc = subprocess.run(
        ["gh", "pr", "merge", branch, "--squash"],
        cwd=worktree, check=True, capture_output=True, text=True,
    )
    result = proc.stdout.strip()

    subprocess.run(["git", "worktree", "remove", "--force", worktree],
                    cwd=REPO_ROOT, capture_output=True, text=True)
    subprocess.run(["git", "branch", "-D", branch],
                    cwd=REPO_ROOT, capture_output=True, text=True)
    subprocess.run(["git", "push", "origin", "--delete", branch],
                    cwd=REPO_ROOT, capture_output=True, text=True)

    return result


# ---------- Rebase-before-merge + CI gate (Mode 9) ----------
# Story branches are graded/reviewed off the base they were branched from, which
# lags origin/master once sibling stories merge. Squash-merging such a branch
# conflicts (the merge gate used to fail with `mergeable: CONFLICTING` after
# MERGE_MAX_ATTEMPTS) and a branch that breaks a sibling's pre-existing master
# test — or is ruff-red — sailed through because `gh pr merge --squash` never
# looked at CI. The merge adjudication loop now rebases onto origin/master and
# force-pushes before merging, and refuses to squash a CI-red branch. See
# ~/.claude/plans/orchestrator-rebase-before-merge.json and memory Mode 9.

PIPELINE_MERGE_CI_GATE = os.environ.get("PIPELINE_MERGE_CI_GATE", "1") != "0"
PIPELINE_MERGE_CI_TIMEOUT = int(os.environ.get("PIPELINE_MERGE_CI_TIMEOUT", "300"))
# Mirrors PIPELINE_MERGE_CI_GATE's opt-out pattern for operators with slow
# builds who don't want a build re-run at the merge gate (see
# _reverify_build below, T4).
PIPELINE_MERGE_BUILD_GATE = os.environ.get("PIPELINE_MERGE_BUILD_GATE", "1") != "0"


# Conservative, narrow allowlist of import/use-statement prefixes for the
# additive-only rebase-conflict auto-resolver below. Intentionally not
# exhaustive - unrecognized statement shapes simply don't qualify for
# auto-resolution and fall through to the existing abort behavior.
_AUTO_RESOLVE_IMPORT_PATTERN = re.compile(
    r"^\s*(import\s|from\s.+\simport\s|use\s|#include\s|require\()"
)


def _parse_conflict_blocks(text: str) -> list[tuple[int, int, list[str], list[str]]] | None:
    """Parse every ``<<<<<<<``/``=======``/``>>>>>>>`` block in `text`.

    Returns a list of (start_line, end_line, ours_lines, theirs_lines) - line
    indices into ``text.splitlines(keepends=True)`` spanning the whole marker
    block (inclusive) - or None if the file has no conflict markers at all,
    or has malformed/unterminated markers (never guess in that case; the
    caller disqualifies the whole rebase step)."""
    lines = text.splitlines(keepends=True)
    blocks: list[tuple[int, int, list[str], list[str]]] = []
    i = 0
    n = len(lines)
    found_any = False
    while i < n:
        if lines[i].startswith("<<<<<<<"):
            found_any = True
            start = i
            ours: list[str] = []
            i += 1
            while i < n and not lines[i].startswith("======="):
                ours.append(lines[i])
                i += 1
            if i >= n:
                return None
            i += 1  # skip the "=======" separator itself
            theirs: list[str] = []
            while i < n and not lines[i].startswith(">>>>>>>"):
                theirs.append(lines[i])
                i += 1
            if i >= n:
                return None
            end = i
            blocks.append((start, end, ours, theirs))
            i += 1
        else:
            i += 1
    return blocks if found_any else None


def _resolve_conflict_blocks(text: str, blocks: list[tuple[int, int, list[str], list[str]]]) -> str:
    """Replace each conflict-marker block with the union of both sides' added
    lines: ours followed by theirs, verbatim, no reordering/dedup/editing."""
    lines = text.splitlines(keepends=True)
    for start, end, ours, theirs in reversed(blocks):  # back-to-front: indices stay valid
        lines[start:end + 1] = ours + theirs
    return "".join(lines)


def _git_show_stage(worktree: str, stage: int, fname: str) -> str | None:
    """Read a file's content at conflict stage 1 (merge base)/2 (ours)/3
    (theirs) from the index. None on any failure (missing stage - e.g. a
    rename/delete conflict has no stage-1 entry - or a git/OSError), which
    the caller treats as "can't verify, disqualify"."""
    try:
        r = subprocess.run(["git", "show", f":{stage}:{fname}"], cwd=worktree,
                           capture_output=True, text=True)
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def _is_pure_additive_import_diff(base: str, other: str) -> bool:
    """True iff `other` differs from `base` by pure line insertions only (no
    deletion or modification of any base line), and every non-blank inserted
    line matches the conservative import/use pattern."""
    base_lines = base.splitlines(keepends=True)
    other_lines = other.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=base_lines, b=other_lines, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            return False
        if tag == "insert":
            for line in other_lines[j1:j2]:
                if line.strip() and not _AUTO_RESOLVE_IMPORT_PATTERN.match(line):
                    return False
    return True


def _try_auto_resolve_conflict(worktree: str) -> list[str]:
    """Attempt the narrow, fail-closed additive-import auto-resolution.

    Eligible only if EVERY conflicted file's whole-file diff from its merge
    base, on BOTH the "ours" (rebase target) and "theirs" (incoming commit)
    side, is pure-insertion-only and every inserted line is a conservative
    import/use statement - i.e. a genuine add/add conflict, never a case
    where either side deleted or modified a pre-existing line. One
    disqualifying file anywhere disqualifies the whole rebase step (no
    partial per-file resolution).

    On success, every eligible file's working-tree content is rewritten with
    its conflict markers replaced by the union of both sides' added lines,
    and the list of resolved filenames is returned (still needs `git add`).
    Returns an empty list if not eligible - the working tree is left
    untouched so the caller's abort path is unaffected."""
    try:
        diff = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"],
                              cwd=worktree, capture_output=True, text=True)
    except OSError:
        return []
    if diff.returncode != 0:
        return []
    conflicted = [f for f in diff.stdout.splitlines() if f.strip()]
    if not conflicted:
        return []

    resolutions: dict[str, str] = {}
    for fname in conflicted:
        try:
            text = (Path(worktree) / fname).read_text()
        except (OSError, UnicodeDecodeError):
            return []  # unreadable/binary - disqualify the whole step

        blocks = _parse_conflict_blocks(text)
        if blocks is None:
            return []  # no/malformed markers - can't verify, disqualify

        base = _git_show_stage(worktree, 1, fname)
        ours = _git_show_stage(worktree, 2, fname)
        theirs = _git_show_stage(worktree, 3, fname)
        if base is None or ours is None or theirs is None:
            return []  # rename/delete conflict (missing a stage) - disqualify

        if not _is_pure_additive_import_diff(base, ours):
            return []
        if not _is_pure_additive_import_diff(base, theirs):
            return []

        resolutions[fname] = _resolve_conflict_blocks(text, blocks)

    # Wrap the write loop in try/except so a write failure (ENOSPC, EROFS,
    # quota, etc.) disqualifies the whole step instead of propagating and
    # leaving the worktree mid-rebase. Matches the read-side handling above
    # and honors _rebase_onto_master's never-raises contract.
    try:
        for fname, resolved_text in resolutions.items():
            (Path(worktree) / fname).write_text(resolved_text)
    except (OSError, UnicodeDecodeError):
        return []
    return list(resolutions.keys())


def _rebase_onto_master(worktree: str, branch: str) -> dict[str, Any]:
    """Rebase `branch` onto current origin/master inside its worktree so the
    merge gate sees the branch against current master, not the stale base the
    agent branched from. Fetches origin/master first (from REPO_ROOT, the shared
    repo) so the rebase target is current.

    Returns ``{"ok": bool, "conflict": bool, "error": str}``, plus
    ``"auto_resolved": True`` when a conflict was narrowly auto-resolved (see
    below) instead of aborted:
      - ok=True            rebase succeeded; the branch is on top of origin/master.
      - ok=True, auto_resolved=True  the rebase hit a conflict, but every
        conflicted file was a pure add/add of import/use statements (never a
        deletion or modification of an existing line) - both sides' added
        lines were unioned and the rebase continued. Fail-closed: any doubt
        anywhere (a modified/deleted line, a non-import addition, a
        rename/delete conflict, one disqualifying file among several) falls
        straight through to the ordinary abort path below - there is no
        partial per-file resolution.
      - ok=False, conflict=True  rebase hit a merge conflict that either
        wasn't a pure additive-import case or couldn't be safely verified as
        one; the rebase was aborted so the worktree is back to its pre-rebase
        state and the caller can park/re-dispatch for human resolution.
      - ok=False, conflict=False some other git failure (dirty tree, missing
        ref); rebase aborted if one was in progress.
    """
    def _run(argv: list[str], cwd, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        # `git` may be absent or non-executable (e.g. a minimal container).
        # Catch OSError so this helper honors its never-raises contract and
        # reports a non-conflict failure instead of crashing the tick.
        try:
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env)
        except OSError as e:
            return subprocess.CompletedProcess(argv, 127, "", str(e))

    if not Path(worktree).is_dir():
        # No worktree to rebase in (missing/anomalous). The merge gate falls
        # back to the CI gate + the original conflict-at-`gh pr merge` check;
        # rebasing is impossible without the worktree the branch lives in.
        return {"ok": True, "conflict": False, "error": "worktree missing - rebase skipped"}
    # Full `git fetch origin` (not `fetch origin <branch>`) so every
    # remote-tracking ref is updated on configs with a narrow/custom refspec,
    # keeping the rebase target current. The rebase target itself must follow
    # the repo's default branch (`main` on fresh `gh repo create`, `master` on
    # legacy / local bench clones); hardcoding `origin/master` would break
    # every merge-gate attempt on a main-default repo (live-`gh` probe,
    # PROOF.md note #1).
    _run(["git", "fetch", "origin"], REPO_ROOT)
    r = _run(["git", "rebase", f"origin/{_default_branch()}"], worktree)
    if r.returncode == 0:
        return {"ok": True, "conflict": False, "error": ""}
    blob = (r.stdout + "\n" + r.stderr).lower()
    conflict = "fix conflicts" in blob or "could not apply" in blob or "conflict" in blob

    if conflict:
        resolved_files = _try_auto_resolve_conflict(worktree)
        if resolved_files:
            add_ok = True
            for fname in resolved_files:
                if _run(["git", "add", fname], worktree).returncode != 0:
                    add_ok = False
                    break
            if add_ok:
                # GIT_EDITOR=true: --continue reuses the original commit
                # message by default, but pin a no-op editor defensively so
                # this can never block on an interactive prompt.
                env = dict(os.environ, GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true")
                cont = _run(["git", "rebase", "--continue"], worktree, env=env)
                if cont.returncode == 0:
                    return {"ok": True, "conflict": False, "auto_resolved": True, "error": ""}
            # Resolution or --continue failed (e.g. a second conflicting
            # commit further down the rebase) - never attempt recursively;
            # fall through to the ordinary abort below.

    # Abort so we never leave the worktree mid-rebase (a half-rebased tree would
    # break the next dispatch into it). Best-effort: --abort is a no-op if no
    # rebase is in progress.
    _run(["git", "rebase", "--abort"], worktree)
    return {"ok": False, "conflict": conflict,
            "error": (r.stdout + r.stderr).strip()[:500]}


def _repo_has_ci_configured() -> bool:
    """Whether the plan's repo (module-level REPO_ROOT, set by
    _scoped_repo_root for the duration of the merge gate) declares any GitHub
    Actions workflows at all. Distinguishes "genuinely no CI" from "CI exists
    but hasn't registered checks for this branch yet" in _ci_status - PR #48
    merged with a red Linux CI job because an empty `gh pr checks` result was
    treated identically to "no CI configured" (2026-07-07 web-client-epic
    retro §4)."""
    return (Path(REPO_ROOT) / ".github" / "workflows").is_dir()


def _ci_status(branch: str, *, timeout_s: int | None = None) -> dict[str, str]:
    """Poll ``gh pr checks <branch>`` until all checks reach a terminal bucket
    or the timeout elapses. Returns ``{"state": "pass"|"fail"|"cancelled"|
    "pending"|"none", "error": str}``.

      - ``pass``   every check passed -> safe to merge.
      - ``fail``   at least one check failed/errored/needs-action -> do not
        merge.
      - ``cancelled`` at least one check was cancelled (e.g. an abnormal
        queue delay) and no check failed/errored - worth exactly one
        automatic rerun before being treated as a failure; callers retry via
        `_ci_rerun` once, then re-poll.
      - ``pending`` checks still running (or a configured repo's checks
        haven't registered yet) at timeout -> do not merge (retry/park).
      - ``none``   no PR / unparseable output / a repo with no
        .github/workflows at all -> treat as pass (a repo without CI must
        not be blocked by this gate).
    Never raises; the merge adjudication loop decides what to do with the result.
    """
    if not PIPELINE_MERGE_CI_GATE:
        return {"state": "pass", "error": "CI gate disabled"}
    deadline = time.monotonic() + (timeout_s if timeout_s is not None else PIPELINE_MERGE_CI_TIMEOUT)
    while time.monotonic() < deadline:
        # `gh` may be absent or non-executable; treat that as "no CI" (none)
        # rather than letting OSError escape and crash the scheduler tick.
        try:
            r = subprocess.run(["gh", "pr", "checks", branch, "--json", "bucket"],
                               capture_output=True, text=True)
        except OSError as e:
            return {"state": "none", "error": f"gh unavailable: {e}"}
        if r.returncode != 0:
            return {"state": "none", "error": r.stderr.strip()[:200]}
        try:
            buckets = {c.get("bucket") for c in json.loads(r.stdout or "[]")}
        except ValueError:
            return {"state": "none", "error": "unparseable gh pr checks output"}
        if not buckets:
            if not _repo_has_ci_configured():
                return {"state": "none", "error": ""}
            # Checks are configured but haven't registered for this branch
            # yet - keep polling within the deadline rather than fast-pathing
            # to pass; falls through to "pending" below if they never do.
            time.sleep(10)
            continue
        if buckets & {"fail", "error", "action_required"}:
            return {"state": "fail", "error": ""}
        if "cancelled" in buckets:
            return {"state": "cancelled", "error": ""}
        if buckets <= {"pass"}:
            return {"state": "pass", "error": ""}
        time.sleep(10)  # still pending — keep polling
    return {"state": "pending", "error": "CI did not complete within timeout"}


def _ci_rerun(branch: str) -> bool:
    """Rerun the most recent CI run's failed/cancelled jobs for `branch` via
    `gh run rerun --failed`, for the one-shot auto-retry on a `cancelled`
    `_ci_status` result. Never raises - `gh`/network failures return False so
    the caller falls through to the ordinary fail/retry path rather than
    crashing the scheduler tick."""
    try:
        r = subprocess.run(
            ["gh", "run", "list", "--branch", branch, "--limit", "1", "--json", "databaseId"],
            capture_output=True, text=True,
        )
    except OSError:
        return False
    if r.returncode != 0:
        return False
    try:
        runs = json.loads(r.stdout or "[]")
    except ValueError:
        return False
    if not runs:
        return False
    run_id = runs[0].get("databaseId")
    if not run_id:
        return False
    try:
        rerun = subprocess.run(
            ["gh", "run", "rerun", str(run_id), "--failed"],
            capture_output=True, text=True,
        )
    except OSError:
        return False
    return rerun.returncode == 0


def _reverify_acceptance(story: dict[str, Any], worktree: str) -> dict[str, str]:
    """Re-run a story's acceptance oracle against its (rebased) worktree right
    before merge, as a second check independent of review and of whatever
    `check_story_status` decided when it set `tests_passed`.

    A repo's own CI (`_ci_status`) only exists if the repo has one configured;
    a story graded against a harness-owned `acceptance` block deserves the
    same re-verification regardless. Returns ``{"state": "pass"|"fail"|"none",
    "error": str}`` — ``"none"`` only when there's no worktree to test
    against. Stories with an `acceptance` block get the scoped oracle re-run;
    stories WITHOUT one (ordinary TDD stories) and non-pytest runners fall
    back to re-running the full suite, so a post-rebase break can't slip
    through (this is the MBW safety net — see commit history). Operators
    with slow suites can opt out via ``PIPELINE_REVERIFY_FULL_SUITE=0`` to
    restore the old silent-pass behavior.
    """
    acceptance = story.get("acceptance") or []
    if not worktree or not Path(worktree).is_dir():
        return {"state": "none", "error": ""}
    test_dir, test_cmd = detect_test_command(Path(worktree))
    # Decide what to run: scoped to acceptance paths when the story carries
    # an acceptance block AND the runner can be safely scoped (pytest path
    # args, cargo --test, npm/yarn node --test — see _scope_test_cmd_to_acceptance);
    # otherwise the full suite. The full-suite path is the MBW safety net — a
    # story without an acceptance block (the common case for real-project
    # stories) still gets the rebased branch's full test suite re-run before
    # merge.
    scoped = None
    if acceptance:
        acceptance_paths = [str(Path(worktree) / p) for p in _acceptance_rel_paths(story)]
        scoped = _scope_test_cmd_to_acceptance(test_cmd, acceptance_paths, test_dir)
    if scoped is not None:
        test_cmd = scoped
    elif not acceptance:
        # No acceptance block: run the full suite unless the operator opted out.
        if os.environ.get("PIPELINE_REVERIFY_FULL_SUITE", "1") == "0":
            return {"state": "none", "error": ""}
    # Same operational-env stripping as check_story_status: PIPELINE_*/
    # LOCAL_AGENT_*/REPO_ROOT are harness config, not developer defaults the
    # suite asserts against.
    test_env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }
    if _is_heavy(test_cmd):
        with _heavy_lock():
            r = subprocess.run(test_cmd, cwd=test_dir, capture_output=True, text=True, env=test_env)
    else:
        r = subprocess.run(test_cmd, cwd=test_dir, capture_output=True, text=True, env=test_env)
    if r.returncode == 0:
        return {"state": "pass", "error": ""}
    return {"state": "fail", "error": (r.stdout + r.stderr).strip()[-500:]}


def _reverify_build(worktree: str) -> dict[str, str]:
    """Run the rebased worktree's build command (if one is detectable)
    right before merge, alongside _reverify_acceptance's test re-run.

    Neither the reviewer nor the dispatched agent's own "tests pass" report
    is proof the project actually builds - PR #48 merged with `npm run
    build` broken (Node's `crypto` module can't bundle for a browser
    target, a real pre-existing bug) because nobody ran it before merge
    (2026-07-07 web-client-epic retro §3.1). Returns {"state":
    "pass"|"fail"|"none", "error": str} - "none" when the gate is disabled,
    there's no worktree to build against, or no build command is
    detectable (a repo without a build step must merge freely).
    """
    if not PIPELINE_MERGE_BUILD_GATE:
        return {"state": "none", "error": "build gate disabled"}
    if not worktree or not Path(worktree).is_dir():
        return {"state": "none", "error": ""}
    detected = detect_build_command(Path(worktree))
    if detected is None:
        return {"state": "none", "error": ""}
    build_dir, build_cmd = detected
    # Same operational-env stripping as _reverify_acceptance: PIPELINE_*/
    # LOCAL_AGENT_*/REPO_ROOT are harness config, not developer defaults the
    # build asserts against.
    build_env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }
    if _is_heavy(build_cmd):
        with _heavy_lock():
            r = subprocess.run(build_cmd, cwd=build_dir, capture_output=True, text=True, env=build_env)
    else:
        r = subprocess.run(build_cmd, cwd=build_dir, capture_output=True, text=True, env=build_env)
    if r.returncode == 0:
        return {"state": "pass", "error": ""}
    return {"state": "fail", "error": (r.stdout + r.stderr).strip()[-500:]}


def _escalate_to_claude(
    manifest: dict, plan_name: str, story_key: str, manifest_path: Path
) -> None:
    """Flip a failed local story to Claude and start clean.

    Tears down the local worktree+branch (the local agent left it dirty/broken;
    Claude gets a fresh branch from main so it doesn't inherit that state), clears
    the dispatch counters, and resets status to 'todo' so the next tick
    re-dispatches on Claude. The journal is also cleared: there's nothing useful
    to resume from a failed local run when Claude is starting over. Also invoked
    from check_story_status's step-cap streak path (see
    STEP_CAP_FALLBACK_THRESHOLD), not just the test-failure caller - the same
    clean-slate teardown applies since a repeated step-cap streak isn't a
    trustworthy foundation for Claude to build on either.
    """
    story = manifest["stories"][story_key]
    worktree = story.get("worktree", "")
    branch = f"agent/{story_key.lower()}"
    # Remove worktree and branch — best-effort (may already be gone).
    if worktree:
        subprocess.run(["git", "worktree", "remove", "--force", worktree],
                        cwd=REPO_ROOT, capture_output=True, text=True)
    subprocess.run(["git", "branch", "-D", branch],
                    cwd=REPO_ROOT, capture_output=True, text=True)
    # Clear journal so Claude starts fresh (not from a broken local checkpoint).
    journal_path = PLAN_DIR / f"{plan_name}.{story_key}.journal.json"
    if journal_path.exists():
        journal_path.unlink()
    # Reset the story: Claude dispatch on next tick.
    story["backend"] = "claude"
    story["escalated"] = True
    story["status"] = "todo"
    for key in ("pid", "worktree", "log", "dispatch_attempts", "dispatch_error",
                "step_cap_streak", "step_cap_streak_model"):
        story.pop(key, None)
    _atomic_write_json(manifest_path, manifest)


def _escalate_to_local_fallback_model(
    manifest: dict, plan_name: str, story_key: str, manifest_path: Path,
    fallback_model: str,
) -> None:
    """Flip a failed local story to a different local model and start clean.

    Plan-scoped opt-in (see manifest["local_model_fallback"]): when a plan
    designates a fallback model, a story whose primary local model failed
    gets one retry on that fallback before falling through to the terminal
    park/fail path, instead of parking immediately. Stays on the "local"
    backend throughout - unlike _escalate_to_claude, this never spends Claude;
    it exists for plans that want a second local opinion (e.g. a larger/
    different Ollama model) without escalating to Claude at all. Mirrors
    _escalate_to_claude's clean-slate teardown (fresh worktree/branch/journal)
    since the prior run may have left broken/half-written state a different
    model shouldn't inherit.
    """
    story = manifest["stories"][story_key]
    worktree = story.get("worktree", "")
    branch = f"agent/{story_key.lower()}"
    # Remove worktree and branch — best-effort (may already be gone).
    if worktree:
        subprocess.run(["git", "worktree", "remove", "--force", worktree],
                        cwd=REPO_ROOT, capture_output=True, text=True)
    subprocess.run(["git", "branch", "-D", branch],
                    cwd=REPO_ROOT, capture_output=True, text=True)
    # Clear journal so the fallback model starts fresh, not from a broken
    # checkpoint left by the model that just failed.
    journal_path = PLAN_DIR / f"{plan_name}.{story_key}.journal.json"
    if journal_path.exists():
        journal_path.unlink()
    # Reset the story: fallback-model dispatch on next tick. backend is left
    # untouched (stays "local") - only the model changes.
    story["model"] = fallback_model
    story["tried_fallback_model"] = True
    story["status"] = "todo"
    for key in ("pid", "worktree", "log", "dispatch_attempts", "dispatch_error",
                "dispatched_model"):
        story.pop(key, None)
    _atomic_write_json(manifest_path, manifest)


def _escalate_review_to_claude(story: dict[str, Any], story_key: str, plan_name: str, reason: str) -> None:
    """Under PIPELINE_BACKEND_DISPATCH=auto, when local review can't converge
    (rework budget or inconclusive-review budget exhausted), give the story
    to Claude instead of parking for a human - for both review and any
    further rework, going forward.

    Unlike _escalate_to_claude (the dispatch-failure path), this does NOT
    wipe the worktree/branch: the existing code is very often already
    correct (2026-07-03's benchmark validation showed most of these parks
    hold ground-truth-correct implementations a local reviewer just
    couldn't cleanly resolve), so Claude reviewing/reworking the SAME
    worktree in place is cheaper and more likely to succeed than discarding
    it and starting over. Sets story["backend"] = "claude" so a subsequent
    redispatch (rework case) also runs on Claude - dispatch_story's own
    priority order already honors story["backend"] first, so no dispatch
    changes are needed. Resets the local rework/inconclusive counters as a
    fresh budget for Claude; a second exhaustion after escalation (checked
    by the caller via story.get("escalated")) is terminal - there is no
    further fallback past Claude, so it must park rather than escalate
    again or loop forever."""
    story["backend"] = "claude"
    story["escalated"] = True
    story.pop("rework_attempts", None)
    story.pop("review_inconclusive_count", None)
    _notify_user(plan_name, f"{story_key} escalating to Claude ({reason}); "
                            f"retrying the same worktree with a fresh budget.")


def _auto_escalation_enabled() -> bool:
    return os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude").strip().lower() == "auto"


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Write *obj* as JSON to *path* atomically via a same-directory temp file.

    Uses os.replace() (POSIX-atomic on the same filesystem) so a crash or
    concurrent reader never observes a partial write. Raises on I/O error and
    leaves *path* untouched.
    """
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(obj, indent=2))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_key(name: str) -> None:
    """Raise ValueError if *name* could be used for path traversal.

    plan_name and story_key flow into filesystem paths; this boundary check
    rejects anything containing path separators, null bytes, or characters
    outside the safe alphanumeric-plus-symbols set.
    """
    if not _KEY_RE.match(name):
        raise ValueError(f"invalid plan/story key {name!r}: only [A-Za-z0-9._-] allowed")


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


def _persona_requires_claude(story: dict[str, Any]) -> bool:
    """Whether story["persona"] (case-insensitive) is in _LOCAL_SKIP_PERSONAS.

    Shared by _route_dispatch_backend (auto-mode a-priori routing) and
    dispatch_story's explicit-mode override, so both apply the identical
    normalization to the same safety boundary instead of drifting apart.
    """
    persona = (story.get("persona") or "").lower()
    return persona in _LOCAL_SKIP_PERSONAS


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


def _count_in_progress_agents() -> int:
    """Count *actually running* dispatched agents (status in_progress with a
    live pid) across every plan's manifest, not just one plan — the usage
    window MAX_CONCURRENT_AGENTS protects is shared across all plans running
    in this session.

    Checks each pid is still alive rather than trusting the status field: a
    story can be stuck at in_progress with a pid whose process already
    exited (e.g. a plan whose own advance_pipeline tick never ran again to
    notice, or a zombie left by a crashed agent) - left uncorrected, that
    permanently consumes a concurrency slot for every other plan forever.

    Skips dead-pid stories rather than reaping them here so this function
    remains a pure read for callers that size dispatch slots. The reap
    itself runs separately in _reap_zombie_in_progress_stories (called from
    advance_all_plans before any per-plan tick), so a dead-pid story in one
    plan doesn't get clobbered before another plan's check_story_status
    has a chance to grade it.
    """
    count = 0
    for manifest_path in PLAN_DIR.glob("*.manifest.json"):
        manifest = json.loads(manifest_path.read_text())
        for story in manifest.get("stories", {}).values():
            if story.get("status") != "in_progress" or "pid" not in story:
                continue
            try:
                os.kill(story["pid"], 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            count += 1
    return count


def _reap_zombie_in_progress_stories() -> int:
    """In-place reap of in_progress stories whose pid has exited, so they
    stop consuming a MAX_CONCURRENT_AGENTS slot forever.

    Sets status → todo and drops pid. Returns the number reaped. Idempotent:
    a manifest already free of zombies is rewritten only if at least one
    reap happened (avoids touching mtime on every tick).

    Called from advance_all_plans before the per-plan advance_pipeline tick,
    so a freshly crashed agent from plan X doesn't block dispatch sizing
    for plan Y on the same scheduler tick. Per-plan advance_pipeline callers
    (e.g. tests, MCP `advance` tool) don't go through here, so a zombie in
    one plan doesn't get clobbered before another plan's check_story_status
    has a chance to grade it on the same tick.

    Without this, observed 2026-06-28: two audio-bugfixes stories with dead
    pids held 2 of 3 concurrency slots for ~19h, blocking all e2e dispatch.
    """
    reaped = 0
    for manifest_path in PLAN_DIR.glob("*.manifest.json"):
        manifest = json.loads(manifest_path.read_text())
        changed = False
        for story in manifest.get("stories", {}).values():
            if story.get("status") != "in_progress" or "pid" not in story:
                continue
            try:
                os.kill(story["pid"], 0)
                # pid is alive — leave the story alone.
                continue
            except ProcessLookupError:
                pass
            except PermissionError:
                # Process exists but we can't signal it (owned by another
                # user). Trust that it's alive and don't reap.
                continue
            # Zombie: agent exited but no one updated the manifest. Reap
            # so the slot frees up and the story becomes dispatchable on
            # the next tick. Setting status back to todo is the correct
            # recovery — the work is unfinished and needs another agent
            # pass; we don't have signal that it was the model's fault
            # vs a harness crash, so don't penalize it with 'failed'.
            story["status"] = "todo"
            story.pop("pid", None)
            changed = True
            reaped += 1
        if changed:
            _atomic_write_json(manifest_path, manifest)
    return reaped


def _plane_set_state(story_key: str, state_group: str, plan_name: str | None = None) -> bool:
    """Best-effort Plane state transition with an inline retry budget.

    Plane sync is a side effect of an action that already succeeded in git, so
    it never raises: a transient failure is retried up to PLANE_MAX_ATTEMPTS,
    and once the budget is spent the drop is recorded durably (via _notify_user
    when a plan is known, else a stderr-style print) rather than propagated.
    Returns True if the transition landed, False if it was given up on.
    """
    if not _plane_enabled():
        return True  # no Plane to sync to; the manifest is the source of truth
    last_err: Exception | None = None
    for _ in range(max(1, PLANE_MAX_ATTEMPTS)):
        try:
            issue_uuid = _resolve_issue_uuid(story_key)
            plane_request("PATCH", f"/projects/{PLANE_PROJECT}/work-items/{issue_uuid}/",
                          json={"state": _get_state(state_group)})
            return True
        except Exception as e:
            last_err = e
    msg = (f"Plane sync for {story_key} → {state_group} failed after "
           f"{PLANE_MAX_ATTEMPTS} attempts: {last_err}")
    if plan_name:
        _notify_user(plan_name, msg)
    else:
        print(f"Warning: {msg}")
    return False


def _mark_plane_done(story_key: str, plan_name: str | None = None) -> None:
    """Best-effort transition of a story's ticket to Done, via whichever
    TicketProvider is configured (PIPELINE_TICKET_PROVIDER).

    Mirrors dispatch_story's in-progress transition: swallows errors rather
    than raising, since not every plan is ticket-backed and a ticketing
    outage must not block a local merge that has already happened in git.
    Named for the merge-path callers (advance_pipeline, approve_merge) that
    predate the TicketProvider abstraction; kept as the call site so their
    existing test mocks don't need to change.
    """
    get_ticket_provider().set_state(story_key, LogicalState.DONE, plan_name)


def _last_nonempty_line(path: Path) -> str:
    """Return the last stripped-non-empty line of `path`, or "" if the file
    has no non-empty lines (or doesn't exist — caller should check).

    Used by check_story_status to classify the agent's terminal exit by the
    tail of agent.log. We must NOT substring-match the whole file: a resumed
    agent appends to the log, so an earlier step-cap marker from a prior
    tick may still be present when the resumed run completes successfully.
    Only the final terminal line classifies the current run.

    Iterates line by line so we don't materialize a multi-MB log into memory
    just to grab the last line; the file is read in binary mode and decoded
    per-line so a partial trailing line (no newline) is still considered."""
    last = ""
    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                last = line
    return last


def _commit_wip(worktree: str, story_key: str, step: str) -> str:
    """Commit any uncommitted work in the worktree as a WIP checkpoint.

    External boundary: spawns `git`. Tests mock subprocess.run. If there is
    nothing to commit (the agent already committed its own work), this is
    not an error — the existing HEAD sha is returned so the journal still
    records a checkpoint marker.

    Excludes agent.log: it's the dispatcher's own session-narration file
    written into the worktree root, not project code, and must never be
    swept into a commit. We stage everything, then unstage agent.log, rather
    than naming it in an exclude pathspec (`:!agent.log`): if the worktree has
    agent.log locally git-ignored (.git/info/exclude or .gitignore, e.g. a
    reviewer keeping it out of diffs), naming it in the pathspec makes `git
    add` reject the whole add ("paths are ignored... use -f", exit 1), which
    would lose the checkpoint. `git add -A` with no pathspec silently skips
    ignored files, and the unstage is a no-op when agent.log is absent or
    ignored.
    """
    subprocess.run(["git", "add", "-A"], cwd=worktree,
                    check=True, capture_output=True, text=True)
    subprocess.run(["git", "reset", "-q", "--", "agent.log"], cwd=worktree,
                    check=False, capture_output=True, text=True)
    commit = subprocess.run(
        ["git", "commit", "-m", f"wip({story_key}): {step}"],
        cwd=worktree, capture_output=True, text=True,
    )
    output = commit.stdout + commit.stderr
    nothing_to_commit = "nothing to commit" in output or "nothing added to commit" in output
    if commit.returncode != 0 and not nothing_to_commit:
        raise RuntimeError(f"git commit failed: {commit.stderr}")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _worktree_has_new_commits(worktree: Path, story_key: str, base_branch: str) -> bool:
    """True iff the agent branch has any commits not on base_branch.

    `git log <base>..HEAD --oneline` lists commits reachable from HEAD
    that aren't reachable from <base>. For an empty branch (agent
    parked without writing code), this list is empty even though the
    test command would pass against main's untouched suite. That's the
    false-positive trap this guards against in check_story_status.

    Returns False on any git error — a broken worktree is the
    orchestrator's problem to surface elsewhere; we'd rather mark a
    real attempt failed than let a transient git hiccup silently
    re-dispatch. The branch name follows the same convention as the
    rest of the orchestrator (line 521 et seq.).
    """
    branch = f"agent/{story_key.lower()}"
    r = subprocess.run(
        ["git", "log", f"{base_branch}..{branch}", "--oneline"],
        cwd=str(worktree), capture_output=True, text=True,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


# ---------- Tools ----------
@mcp.tool()
def get_role_config(plan_name: str | None = None) -> dict[str, Any]:
    """
    Show the resolved (provider, model) for every pipeline role - overlord,
    planner, dispatch, review, decompose - given the current env vars and
    model_registry.json, optionally layered with a specific plan's
    role_config (pass plan_name to include it). Lets you check what a plan
    will actually run on *before* executing it. Pure read; makes no changes.

    "planner" here reports its own explicit configuration layer (env var /
    plan role_config / registry) using the same "claude" bottom-of-chain
    default as the other roles - it does NOT reproduce the extra "mirror
    dispatch's own backend when nothing else is configured" fallback that
    _resolve_planner_backend applies at actual dispatch time (that fallback
    depends on a specific story's already-resolved dispatch backend, which
    doesn't exist outside of a real dispatch call).
    """
    plan_role_config = _plan_role_config(plan_name) if plan_name else None
    role_fallbacks = {
        "overlord": lambda: _persona_default_model("overlord") or "opus",
        "planner": lambda: DEFAULT_MODEL,
        "dispatch": lambda: DEFAULT_MODEL,
        "review": lambda: _persona_default_model("code-reviewer") or DEFAULT_MODEL,
        "decompose": lambda: _persona_default_model("product-analyst") or "opus",
    }
    roles = {}
    for role, fallback in role_fallbacks.items():
        resolution = role_registry.resolve_role(
            role, plan_role_config=plan_role_config, model_fallback=fallback,
        )
        roles[role] = {"provider": resolution.provider, "model": resolution.model}
    return {"ok": True, "roles": roles}


@mcp.tool()
def decompose_plan(request: str) -> dict[str, Any]:
    """
    Turn a raw goal/feature request into epics/stories JSON via the
    product-analyst persona, run on whichever provider the "decompose" role
    is configured for (PIPELINE_BACKEND_DECOMPOSE env var, or a "decompose"
    entry in model_registry.json - defaults to Claude when neither is set).
    This is a separate, additional path from the interactive product-analyst
    subagent (invoked via the Agent tool, which is always Claude) - that
    path remains available and is still the default choice for
    Claude-quality decomposition; this tool exists so decomposition can also
    run on a local provider when desired.

    Does NOT call save_plan itself - review the returned plan the same way
    you would review the interactive subagent's output, then save_plan it
    yourself.

    Returns {"ok": True, "plan": {...}} on success. On failure, returns
    {"ok": False, "error": ...}, with "raw": <raw model output> included
    whenever the backend actually returned text that failed to parse (never
    raises).
    """
    text = _run_decompose(request)
    if not text:
        return {"ok": False, "error": "decompose backend returned no output"}
    candidate = _extract_json_block(text)
    try:
        plan = json.loads(candidate)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"invalid JSON: {e}", "raw": text}
    if not isinstance(plan, dict) or not isinstance(plan.get("epics"), list):
        return {
            "ok": False,
            "error": "response JSON is missing an 'epics' list",
            "raw": text,
        }
    return {"ok": True, "plan": plan}


@mcp.tool()
def save_plan(plan_name: str, plan_json: str) -> dict[str, Any]:
    """
    Save a generated project plan to disk. Plan should be JSON matching the
    schema: { "epics": [ { "summary", "stories": [...] } ] }.
    Call this after generating a plan so the user can review before ingestion.
    """
    _validate_key(plan_name)
    try:
        plan = json.loads(plan_json)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Invalid JSON: {e}"}

    if "epics" not in plan:
        return {"ok": False, "error": "Plan must contain 'epics' key"}

    path = PLAN_DIR / f"{plan_name}.json"
    _atomic_write_json(path, plan)

    story_count = sum(len(e.get("stories", [])) for e in plan["epics"])
    return {
        "ok": True,
        "path": str(path),
        "epic_count": len(plan["epics"]),
        "story_count": story_count,
    }


@mcp.tool()
def list_plans() -> list[str]:
    """List saved plans available for ingestion."""
    return [p.stem for p in PLAN_DIR.glob("*.json")]


# Story fields the plan authors and that a re-ingest should refresh. Every
# other field on an already-tracked story (status, pr_url, worktree,
# review_verdict, journal, ...) is pipeline-owned runtime state and must
# survive a re-ingest untouched - see the merge behavior in ingest_plan below
# (T1, 2026-07-07 web-client-epic retro incident #2).
_INGEST_AUTHORED_STORY_FIELDS = (
    "summary", "agent_instructions", "dependencies", "persona", "model",
    "acceptance", "risk", "backend",
)

# Valid story["backend"] values at ingest time: every registered driver name
# (backend._DRIVERS) plus "auto" - a valid runtime value even though it is
# not itself a driver (get_backend rejects it; _route_dispatch_backend
# resolves it to "local"/"claude" first, per PIPELINE_BACKEND_DISPATCH=auto).
_VALID_STORY_BACKENDS = frozenset(backend._DRIVERS) | {"auto"}


@mcp.tool()
def ingest_plan(
    plan_name: str, only_epics: list[str] | None = None, overwrite: bool = False,
) -> dict[str, Any]:
    """
    Push a saved plan into Plane. Creates epics first, then issues linked
    to their parent epic. Optionally restrict to specific epic summaries via
    only_epics. Returns a manifest mapping local IDs to Plane UUIDs.

    Re-ingesting an already-ingested plan merges into the existing manifest
    rather than replacing it: epics/stories not touched this call (including
    everything only_epics excludes) are preserved verbatim, a story whose key
    already exists gets its authored fields (summary, agent_instructions,
    dependencies, persona, model, acceptance, risk) refreshed while its
    runtime state (status, pr_url, ...) is kept, and top-level manifest keys
    outside epics/stories/repo_root (paused, local_model_fallback, ...) carry
    over untouched. Pass overwrite=True to restore the old wholesale-replace
    behavior (drops anything not produced by this call).
    """
    _validate_key(plan_name)
    path = PLAN_DIR / f"{plan_name}.json"
    if not path.exists():
        return {"ok": False, "error": f"No plan named {plan_name}"}

    plan = json.loads(path.read_text())

    # advance_all_plans() iterates every plan in shared PLAN_DIR, each
    # potentially belonging to a different repo, so a manifest without its
    # own repo_root falls back to the global REPO_ROOT - the wrong repo for
    # any plan other than the one that env var happens to be set for (or a
    # deliberately-broken sentinel, if one's configured to fail loudly
    # instead). Catching it here means a typo'd or missing path surfaces
    # immediately, not as a cryptic ENOENT after three silent merge-attempt
    # failures.
    repo_root = plan.get("repo_root")
    if not repo_root or not Path(repo_root).is_dir():
        return {"ok": False, "error": f"Plan repo_root is missing or not a directory: {repo_root!r}"}

    # Validate story["backend"] upfront, before any Plane side effects, so a
    # typo'd provider name fails closed here rather than surfacing as a
    # NotImplementedError deep inside get_backend at dispatch time.
    for epic in plan["epics"]:
        if only_epics and epic["summary"] not in only_epics:
            continue
        for story in epic.get("stories", []):
            story_backend = story.get("backend")
            if story_backend is not None and story_backend not in _VALID_STORY_BACKENDS:
                return {
                    "ok": False,
                    "error": (
                        f"Story {story.get('summary', '?')!r} has unknown "
                        f"backend {story_backend!r}. Valid values: "
                        f"{sorted(_VALID_STORY_BACKENDS)}"
                    ),
                }

    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"

    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "another ingest/dispatch/interrupt is in progress for this plan",
            }

        manifest = {"epics": {}, "stories": {}, "repo_root": repo_root}

        # When no ticketing backend is configured (NullTicketProvider) the
        # manifest is the sole source of truth: create_epic/create_story are
        # no-ops returning None, and we synthesize story keys locally instead
        # of taking them from a backend-issued id.
        provider = get_ticket_provider()

        # Maps the plan's local story keys (e.g. "S1") to the manifest story keys
        # generated below (backend ids, or local keys when no backend is
        # configured), so dependencies can be translated to manifest keys.
        key_to_issue_id: dict[str, str] = {}

        for epic in plan["epics"]:
            if only_epics and epic["summary"] not in only_epics:
                continue

            epic_id = provider.create_epic(epic["summary"])
            if epic_id is not None:
                manifest["epics"][epic["summary"]] = epic_id

            for story in epic.get("stories", []):
                issue_id = provider.create_story(
                    story["summary"], story.get("description", ""), epic_id,
                    "agent-pipeline",
                )
                if issue_id is None:
                    # No backend id to key on: prefer the plan's own story key
                    # (keeps the manifest readable and lets key-based dependencies
                    # resolve to themselves), else mint a unique synthetic key.
                    issue_id = story.get("key") or str(uuid.uuid4())
                if "key" in story:
                    key_to_issue_id[story["key"]] = issue_id
                manifest["stories"][issue_id] = {
                    "summary": story["summary"],
                    "agent_instructions": story.get("agent_instructions", ""),
                    "dependencies": story.get("dependencies", []),
                    "persona": story.get("persona"),
                    "model": story.get("model"),
                    "acceptance": story.get("acceptance", []),
                    "risk": story.get("risk", "low"),
                    "backend": story.get("backend"),
                    "status": "todo",
                }

        # Translate dependencies expressed as local plan keys into the issue IDs
        # just created. Dependencies that don't match a known local key (e.g.
        # already an issue ID, or a typo) are left as-is.
        for story in manifest["stories"].values():
            story["dependencies"] = [
                key_to_issue_id.get(dep, dep) for dep in story["dependencies"]
            ]

        # Merge into the existing manifest rather than replacing it (T1):
        # anything only_epics excluded this round - and, with overwrite=False,
        # the manifest's runtime state for stories re-ingested this round -
        # must survive. overwrite=True restores the old wholesale-replace
        # behavior for callers that genuinely want a clean slate.
        prior: dict[str, Any] = {}
        if not overwrite and manifest_path.exists():
            prior = json.loads(manifest_path.read_text())

        merged_epics = dict(prior.get("epics", {}))
        merged_epics.update(manifest["epics"])

        merged_stories = dict(prior.get("stories", {}))
        for key, new_story in manifest["stories"].items():
            old_story = merged_stories.get(key)
            if old_story is not None:
                combined = dict(old_story)
                for field in _INGEST_AUTHORED_STORY_FIELDS:
                    combined[field] = new_story[field]
                merged_stories[key] = combined
            else:
                merged_stories[key] = new_story

        final_manifest = dict(prior)
        final_manifest["epics"] = merged_epics
        final_manifest["stories"] = merged_stories
        final_manifest["repo_root"] = repo_root

        _atomic_write_json(manifest_path, final_manifest)

    return {"ok": True, "manifest_path": str(manifest_path), **final_manifest}


def _completed_dep_ids(stories: dict[str, Any]) -> set[str]:
    """Identifiers a dependency string may legitimately reference for a *done*
    story, covering both forms a dependency can take.

    Ingest only rewrites a summary-string dependency to a manifest key when the
    source story carried a local `key` (see ingest_plan); plans whose stories
    have no key — and which therefore express dependencies as the prerequisite's
    exact summary string, per the documented save_plan schema — keep those
    summary deps verbatim while the manifest itself is keyed by UUID. Matching a
    dependency against both done keys and done summaries resolves it regardless
    of which form it took, so a dependent story is never stranded as unready."""
    done_keys = {k for k, v in stories.items() if v["status"] == "done"}
    done_summaries = {v["summary"] for v in stories.values() if v["status"] == "done"}
    return done_keys | done_summaries


@mcp.tool()
def list_ready_stories(plan_name: str) -> list[dict]:
    """
    Return stories whose dependencies are satisfied and that are still in
    To Do. Use this to decide what to dispatch next.
    """
    _validate_key(plan_name)
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    if not manifest_path.exists():
        return []

    manifest = json.loads(manifest_path.read_text())
    stories = manifest["stories"]
    done = _completed_dep_ids(stories)

    ready = []
    for key, story in stories.items():
        if story["status"] != "todo":
            continue
        deps_met = all(dep in done for dep in story["dependencies"])
        if deps_met:
            ready.append({"key": key, "summary": story["summary"]})
    return ready


# Observability artifacts a dispatched/reviewed agent writes into its own
# worktree (agent.log, review.log) but must NEVER be trackable by git. Mode
# 17: review.log starts untracked (harmless), but a rework cycle's auto
# WIP-commit (`git add -A`) tracks it if the story gets REQUEST_CHANGES;
# the next review cycle's append then makes it a modified tracked file, and
# the pre-merge rebase (Mode 9's gate) refuses on "unstaged changes" -
# failing an already-APPROVED, ground-truth-correct story 3 retries running.
# .git/info/exclude is shared across every worktree of a repo (verified:
# `git rev-parse --git-path info/exclude` from inside a worktree resolves to
# the MAIN repo's .git/info/exclude, not a per-worktree file), so writing it
# once per repo, idempotently, covers every past and future worktree.
#
# .agent_plan.md/.agent_scratchpad.md (GUIDED_DECOMPOSITION_PLAN.md) are the
# same kind of untracked runtime artifact as agent.log/review.log - written
# into the worktree outside of any commit, and vulnerable to the identical
# Mode 17 failure (a rework's `git add -A` WIP-commit would track them,
# dirtying the tree ahead of the pre-merge rebase) if not excluded up front.
_WORKTREE_LOG_EXCLUDES = ("agent.log", "review.log", ".agent_plan.md", ".agent_scratchpad.md")


def _exclude_worktree_logs_from_tracking(repo_root: Path) -> None:
    """Best-effort: append _WORKTREE_LOG_EXCLUDES to repo_root/.git/info/exclude
    if not already present. Never raises - this is a hygiene fix, not a
    correctness requirement, and must not break dispatch if the repo's .git
    layout is unexpected (e.g. a submodule, or repo_root not actually a git
    repo yet in some caller)."""
    try:
        info_dir = repo_root / ".git" / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude_path = info_dir / "exclude"
        existing = exclude_path.read_text() if exclude_path.exists() else ""
        missing = [name for name in _WORKTREE_LOG_EXCLUDES if name not in existing]
        if missing:
            with exclude_path.open("a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                for name in missing:
                    f.write(f"{name}\n")
    except OSError:
        pass


@mcp.tool()
def dispatch_story(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Spawn a headless Claude Code agent to work on a single story.

    For a fresh story, creates a git worktree on a new branch. For a story
    left "interrupted" (or whose worktree already exists from a prior run),
    reuses the existing worktree/branch instead and seeds the agent's prompt
    with the checkpoint journal so it continues rather than starting over.
    Transitions the Plane issue to In Progress. Returns the subprocess PID;
    completion is async.

    Acquires `_plan_lock` so direct MCP tool calls serialize across MCP
    server processes - without this guard, two Claude sessions (each with
    their own MCP server PID) can both call dispatch_story on the same story
    in the same window, and the second one treats the first one's
    half-built worktree as resumable and spawns a second agent into the
    same directory. That race is what produced the repeated zero-output
    agent deaths logged in 2026-06-27's e2e-decentralized-messaging run.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "another dispatch/interrupt is in progress for this plan",
            }
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
        manifest = json.loads(manifest_path.read_text())
        story = manifest["stories"].get(story_key)
        if not story:
            return {"ok": False, "error": f"No such story {story_key}"}

        branch = f"agent/{story_key.lower()}"
        worktree_path = WORKTREE_ROOT / story_key
        resuming = (
            story.get("status") in ("interrupted", "changes_requested")
            or worktree_path.exists()
        )
        journal = _read_journal(plan_name, story_key) if resuming else []

        if not resuming:
            with _scoped_repo_root(plan_name) as repo_root:
                subprocess.run(
                    ["git", "pull", "--ff-only", "origin", _default_branch()],
                    cwd=repo_root, check=True,
                )
                subprocess.run(
                    ["git", "worktree", "add", "-b", branch, str(worktree_path)],
                    cwd=repo_root, check=True,
                )
                _exclude_worktree_logs_from_tracking(Path(repo_root))

        get_ticket_provider().set_state(story_key, LogicalState.IN_PROGRESS, plan_name)

        # Resolve concrete backend name for this story. Priority order:
        #   1. story["backend"] already set (e.g. from an escalation flip)
        #   2. PIPELINE_BACKEND_DISPATCH=auto  → a-priori router
        #   3. PIPELINE_BACKEND_DISPATCH=local|claude  → that driver directly
        env_backend = os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude").strip().lower()
        dispatch_backend = story.get("backend") or (
            _route_dispatch_backend(story) if env_backend == "auto" else env_backend
        )
        # Persona-based safety override: a security persona always dispatches to
        # Claude, regardless of dispatch mode (auto/local/claude) - unless the
        # story already had an explicit backend (a prior escalation flip), which
        # wins as-is and is never re-routed here.
        if not story.get("backend") and _persona_requires_claude(story):
            dispatch_backend = "claude"
        # Persist so check_story_status and escalation see which backend ran.
        story["backend"] = dispatch_backend

        # A rework redispatch (changes_requested with stored review_feedback)
        # on the local Ollama driver can resume the prior dispatch's message
        # transcript instead of rebuilding a cold-start prompt via
        # _build_dispatch_command's rework_instruction - the transcript
        # already holds the full prior context, so only the reviewer's new
        # feedback needs to be appended. Guard on the transcript file actually
        # existing (backend.py writes it to cwd/.agent_transcript.json on
        # every dispatch): a story whose first dispatch predates this
        # feature, ran on a different backend, or had its transcript cleaned
        # up must fall back to the existing from-scratch rework prompt rather
        # than crash.
        review_feedback = story.get("review_feedback")
        transcript_path = worktree_path / ".agent_transcript.json"
        resume_via_transcript = (
            dispatch_backend in _LOCAL_BACKEND_NAMES and review_feedback and transcript_path.exists()
        )

        spec = _build_dispatch_command(
            story, story_key, plan_name=plan_name, resume_journal=journal or None,
            review_feedback=None if resume_via_transcript else review_feedback,
        )
        worktree_path.mkdir(parents=True, exist_ok=True)
        log_path = worktree_path / "agent.log"

        # Gap 7: surface multi-model concurrent-dispatch risk. MAX_CONCURRENT_AGENTS
        # is a process-count cap with no model/VRAM awareness, and Ollama's
        # `/api/ps` reports whatever's currently loaded. If a *different* model
        # is already in VRAM and we're about to dispatch a second story on a
        # different model, Ollama will swap the existing model out to make room
        # (or OOM-split if 24GB unified memory is tight). Warn, don't block:
        # same-model concurrency is safe, and even a swap is just slow.
        if (dispatch_backend in _LOCAL_BACKEND_NAMES
                and MAX_CONCURRENT_AGENTS > 1
                and _count_in_progress_agents() > 0):
            target_model = spec.get("model") or story.get("model")
            if target_model:
                # spec["model"]/story["model"] may be an unresolved tier
                # name (e.g. "sonnet"), which never matches anything in
                # `loaded` (concrete Ollama tags) and would otherwise warn
                # on every dispatch regardless of what's actually loaded.
                target_model = backend._resolve_local_model(target_model)
            try:
                loaded = backend._ollama_loaded_models(
                    os.environ.get("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
                )
            except Exception:
                loaded = set()  # observability hook, never a gate
            if loaded and target_model and target_model not in loaded:
                msg = (
                    f"multi-model concurrent dispatch: {sorted(loaded)} already "
                    f"loaded, dispatching {story_key} on {target_model} may force "
                    f"a VRAM swap (set MAX_CONCURRENT_AGENTS=1 to silence)"
                )
                _notify_user(plan_name, msg)
                logging.getLogger("pipeline").warning(msg)

        # Fix #1: if the story carries an `acceptance` block, materialize the
        # oracle files into the worktree BEFORE the backend launches so the local
        # harness can grade against them. On a resumed story skip the write —
        # the oracle may already be in a committed WIP, and overwriting would
        # discard whatever test evolution happened mid-run.
        acceptance = story.get("acceptance") or []
        acceptance_paths = _acceptance_rel_paths(story)
        for entry in acceptance:
            target = worktree_path / entry["path"]
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(entry["source"])

        # GUIDED_DECOMPOSITION_PLAN.md: PIPELINE_DECOMPOSE=cloud|local turns
        # on a "tech lead" checklist for the weak local executor. Default
        # "off" - opt-in, per Secure Defaults. Gated on:
        #   - a local-family backend (the crutch exists for the weak local
        #     executor; Claude doesn't need it)
        #   - not resuming (plan once on the story's first dispatch; a
        #     rework must never spend a second planner call)
        #   - no plan already on disk (belt-and-suspenders with `resuming`)
        # The LLM call itself is best-effort (_run_planner fails open to
        # None) so a broken/slow/rate-limited planner never blocks or
        # corrupts dispatch - the story simply proceeds with no checklist,
        # exactly like PIPELINE_DECOMPOSE=off.
        decompose_mode = os.environ.get("PIPELINE_DECOMPOSE", "off").strip().lower()
        # H3 ablation (GUIDED_DECOMPOSITION_PLAN.md §4.1's G-cloud-noscratch
        # condition): default "on" ships the persistent scratchpad; "off"
        # tests whether the checklist alone accounts for the benefit,
        # independent of cross-step memory. Read once here because it now
        # feeds BOTH the planner call (so the scratchpad becomes a first-class
        # generated step) and the trailing-instruction backstop below.
        scratchpad_on = (
            os.environ.get("PIPELINE_DECOMPOSE_SCRATCHPAD", "on").strip().lower() != "off"
        )
        plan_path = worktree_path / ".agent_plan.md"
        if (
            decompose_mode in ("cloud", "local")
            and dispatch_backend in _LOCAL_BACKEND_NAMES
            and not resuming
            and not plan_path.exists()
        ):
            plan_text = _run_planner(
                story.get("agent_instructions", ""), mode=decompose_mode,
                dispatch_backend=dispatch_backend, local_model=spec["model"],
                include_scratchpad=scratchpad_on,
                plan_role_config=_plan_role_config(plan_name),
            )
            if plan_text:
                plan_path.write_text(plan_text)
        # Referencing an existing plan is independent of generating one, so
        # a resumed dispatch that rebuilds its prompt from scratch (no
        # transcript to resume) still sees the checklist from the story's
        # first dispatch, without spending a second planner call for it.
        if plan_path.exists():
            scratchpad_instruction = ""
            # Backstop to the planner-woven scratchpad steps above: even with
            # the clause folded into the checklist, keep the explicit trailing
            # reminder so a resumed dispatch (whose stored .agent_plan.md may
            # predate the clause) and any run whose planner under-emitted it
            # still get told to maintain the scratchpad.
            if scratchpad_on:
                scratchpad_instruction = (
                    " After finishing each step, keep .agent_scratchpad.md "
                    "up to date with a short running summary of what you've "
                    "done and which step is next (create_file for the first "
                    "note, str_replace to rewrite it after that) before "
                    "moving on to the next step."
                )
            spec["prompt"] = (
                f"{spec['prompt']}\n\n"
                "--- Implementation checklist from your tech lead ---\n"
                f"{plan_path.read_text()}\n\n"
                f"Work through these steps in order.{scratchpad_instruction}"
            )

        dispatch_kwargs: dict[str, Any] = dict(
            prompt=spec["prompt"], system=spec["system"], model=spec["model"],
            allowed_tools=spec["allowed_tools"],
            cwd=worktree_path, log_path=log_path, append=resuming,
        )
        # Only the local driver accepts/uses `acceptance`; pass it through when
        # we're actually invoking that driver so Claude's signature stays clean.
        if dispatch_backend in _LOCAL_BACKEND_NAMES and acceptance_paths:
            dispatch_kwargs["acceptance"] = acceptance_paths
        # L1 (REVIEWER_ESCALATION_PLAN.md): a CI-triggered rework
        # (story["ci_rework"], set by the merge-CI rework router) raises the
        # agent's done-bar to full-suite-green so it cannot declare done while
        # its own broken test still fails. Local-only: the env reaches the
        # local agent subprocess; Claude's dispatch signature stays clean.
        if dispatch_backend in _LOCAL_BACKEND_NAMES and story.get("ci_rework"):
            dispatch_kwargs["rework_full_suite"] = True

        if resume_via_transcript:
            dispatch_kwargs["resume_transcript_path"] = transcript_path
            # Same tech-lead-decomposition logic as the initial checklist,
            # applied to review feedback: a reviewer's prose diagnosis is
            # itself a coarse brief for a weak executor. Re-run per rework
            # cycle (unlike the initial checklist, which plans once) since
            # each cycle's feedback is different. Fails open to the raw
            # feedback format on any planner failure - identical contract
            # to the initial-dispatch checklist.
            fix_checklist = None
            if (
                decompose_mode in ("cloud", "local")
                and dispatch_backend in _LOCAL_BACKEND_NAMES
            ):
                fix_checklist = _run_rework_planner(
                    review_feedback, mode=decompose_mode,
                    dispatch_backend=dispatch_backend, local_model=spec["model"],
                    plan_role_config=_plan_role_config(plan_name),
                )
            if fix_checklist:
                dispatch_kwargs["resume_append_content"] = (
                    "The code reviewer REQUESTED CHANGES on your previous "
                    "attempt. Your tech lead has translated the feedback "
                    f"into a fix checklist:\n{fix_checklist}\n\n"
                    f"Original review feedback (for reference):\n{review_feedback}"
                )
            else:
                dispatch_kwargs["resume_append_content"] = (
                    "The code reviewer REQUESTED CHANGES on your previous attempt. "
                    f"Address this feedback:\n{review_feedback}"
                )

        handle = backend.get_backend("dispatch", name=dispatch_backend).dispatch(**dispatch_kwargs)

        story["status"] = "in_progress"
        story["pid"] = handle.pid
        story["dispatched_at"] = datetime.now(timezone.utc).isoformat()
        story["worktree"] = str(worktree_path)
        story["log"] = str(log_path)
        # Record the concrete model the agent actually boots with (the local
        # backend resolves a logical tier like "sonnet" to e.g.
        # "minimax-m3:cloud"). The dashboard shows this instead of the plan's
        # declared story["model"] so what's displayed matches what ran. The
        # declared tier is left untouched (it's a routing hint).
        if getattr(handle, "model", None):
            story["dispatched_model"] = handle.model
        _atomic_write_json(manifest_path, manifest)

        return {"ok": True, "story_key": story_key, "pid": handle.pid, "branch": branch,
                "resumed": resuming}


@mcp.tool()
def _last_done_summary(agent_log: Path) -> str:
    """Return the summary text from the LAST "] DONE:" line in agent.log, or
    "" if the agent never reached done. Only the final DONE line reflects
    the current run - a resumed agent appends to the same log across ticks
    (mirrors _last_nonempty_line's resumed-log caution for STEP_CAP_MARKERS).
    local_agent.py's `done` tool prints its summary argument verbatim as
    "[step N] DONE: <summary>"; this is that real signal, not a fictitious
    exit protocol."""
    if not agent_log.exists():
        return ""
    marker = "] DONE:"
    last = ""
    with open(agent_log, "rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").strip()
            idx = line.find(marker)
            if idx != -1:
                last = line[idx + len(marker):].strip()
    return last


# Literal, narrow phrases only - broad keyword matching would false-positive
# on legitimate completion summaries that happen to mention difficulty
# encountered along the way.
_GIVE_UP_PHRASES = (
    "i can't complete this task",
    "i cannot complete this task",
    "i'm unable to complete this task",
    "i am unable to complete this task",
    "i give up",
)


def _is_give_up_summary(summary: str) -> bool:
    """Whether a DONE summary reads as an explicit surrender rather than a
    genuine completion claim (2026-07-07 web-client-epic retro §3.2: the
    WASM story's second attempt called done with "I'm sorry, I can't
    complete this task" after real research, zero commits)."""
    lowered = summary.lower()
    return any(phrase in lowered for phrase in _GIVE_UP_PHRASES)


def check_story_status(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Check whether a dispatched agent has finished. If complete, runs tests
    in the worktree and reports pass/fail without auto-merging.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    story = manifest["stories"].get(story_key)
    if not story or "pid" not in story:
        return {"ok": False, "error": "Story not dispatched"}
    if story["status"] == "interrupted":
        # Incomplete by definition — running tests here would just record a
        # spurious failure instead of leaving it resumable.
        return {"status": "interrupted", "pid": story["pid"]}

    pid = story["pid"]
    try:
        os.kill(pid, 0)
        # os.kill succeeds for zombie (defunct) processes too — check ps stat
        ps = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True,
        )
        stat = ps.stdout.strip()
        if stat and not stat.startswith("Z"):
            dispatched_at = story.get("dispatched_at")
            if dispatched_at is not None:
                elapsed = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(dispatched_at)
                ).total_seconds()
                if elapsed > DISPATCH_WATCHDOG_SECONDS:
                    _terminate_and_checkpoint(
                        manifest, manifest_path, plan_name, story_key, story,
                        pid=pid, step="dispatch_watchdog_timeout",
                        summary=(
                            f"Dispatch watchdog: no completion after "
                            f"{elapsed:.0f}s; process terminated."
                        ),
                    )
                    story["dispatch_error"] = (
                        f"watchdog killed after {elapsed:.0f}s with no completion"
                    )
                    _atomic_write_json(manifest_path, manifest)
                    return {"status": "interrupted", "pid": pid, "watchdog_killed": True}
            return {"status": "running", "pid": pid}
        # process is zombie or gone — fall through to test detection
    except ProcessLookupError:
        pass

    worktree = Path(story["worktree"])
    agent_log = worktree / "agent.log"
    if agent_log.exists() and agent_log.stat().st_size == 0:
        # Empty log within the startup grace window means the agent is alive
        # and bootstrapping - its first print() hasn't flushed yet, especially
        # when queued on Ollama's -np 1 worker behind another request. The
        # PID-alive check above already passed, so trust that and don't burn
        # dispatch_attempts on a process that's just slow to print. After the
        # grace window elapses with the log still empty, the agent is
        # presumed genuinely dead (failed launch) and we count it.
        log_age = time.time() - agent_log.stat().st_mtime
        if log_age < DISPATCH_STARTUP_GRACE_SECONDS:
            return {"status": "running", "pid": pid}
        # The agent process exited without ever writing a byte of output -
        # a failed launch, not a real attempt. Running tests against the
        # untouched worktree would just record a misleading "failed" for
        # work that was never tried. Within the dispatch error budget we keep
        # it "interrupted" (dispatch-eligible like "todo", so the next tick
        # retries it); once the budget is spent, a launch that never works
        # becomes a terminal "failed" so it stops looping forever.
        attempts = story.get("dispatch_attempts", 0) + 1
        story["dispatch_attempts"] = attempts
        if attempts >= DISPATCH_MAX_ATTEMPTS:
            story["status"] = "failed"
            story["dispatch_error"] = f"agent produced no output in {attempts} launch attempts"
            _notify_user(plan_name, f"{story_key} failed to launch {attempts}x; "
                                    f"giving up - needs human intervention.")
            _atomic_write_json(manifest_path, manifest)
            return {"status": "failed", "pid": pid}
        story["status"] = "interrupted"
        _atomic_write_json(manifest_path, manifest)
        return {"status": "interrupted", "pid": pid}

    # Step-cap exit routing (regression guard for PR #49 / commit 90a3cf1):
    # when the headless agent hits its step cap it prints a terminal marker
    # on the LAST line of agent.log, exits with code 2, and has already
    # WIP-committed. Classifying the run by its tail line (NOT a substring
    # search of the whole file — a resumed agent appends to agent.log, so an
    # old marker from a prior tick may appear earlier) lets us short-circuit
    # before the test suite runs. If we ran tests against the WIP commit and
    # it passed, we'd land the story on `tests_passed`, which is merge-
    # eligible — and that is exactly how incomplete step-capped work landed
    # on master. `interrupted` is dispatch-eligible, so the next
    # advance_pipeline tick resumes the agent in its existing worktree from
    # its WIP commit, seeded by the journal entry we write below.
    last_log_line = _last_nonempty_line(agent_log) if agent_log.exists() else ""
    if last_log_line in STEP_CAP_MARKERS:
        sha = _commit_wip(str(worktree), story_key, "step_cap_reached")
        interrupted_at = datetime.now(timezone.utc).isoformat()
        _append_journal(plan_name, story_key, {
            "step": "step_cap_reached",
            "summary": "Agent hit the step cap; checkpointed for resume.",
            "next_hint": "",
            "commit": sha,
            "ts": interrupted_at,
        })
        story["status"] = "interrupted"
        story["last_commit"] = sha
        story["interrupted_at"] = interrupted_at

        # See STEP_CAP_FALLBACK_THRESHOLD: track consecutive step-cap
        # interrupts on the current model and, past the threshold, switch to
        # the plan's opted-in fallback model for the next resume. Worktree
        # and journal are left untouched so the resumed run still benefits
        # from whatever real progress is already committed.
        fallback_model = manifest.get("local_model_fallback")
        current_model = story.get("dispatched_model") or story.get("model")
        # STEP_CAP_MARKERS are only ever printed by the local agent scripts, so
        # a Claude-backend story should never reach here in practice - guard
        # explicitly anyway (defense in depth) so a plan-scoped local model
        # name can never land in a Claude story's model field. Missing
        # "backend" defaults to local: dispatch_story always sets it
        # explicitly, so an absent key only occurs in tests exercising this
        # branch in isolation.
        if (fallback_model and current_model != fallback_model
                and story.get("backend", "local") == "local"):
            if story.get("step_cap_streak_model") == current_model:
                story["step_cap_streak"] = story.get("step_cap_streak", 0) + 1
            else:
                story["step_cap_streak"] = 1
                story["step_cap_streak_model"] = current_model
            if story["step_cap_streak"] >= STEP_CAP_FALLBACK_THRESHOLD:
                story["model"] = fallback_model
                story.pop("step_cap_streak", None)
                story.pop("step_cap_streak_model", None)
                _notify_user(
                    plan_name,
                    f"{story_key} hit the step cap {STEP_CAP_FALLBACK_THRESHOLD}x "
                    f"on {current_model}; switching to fallback model "
                    f"{fallback_model} for the next resume.")
        elif (not fallback_model and _auto_escalation_enabled()
                and story.get("backend", "local") == "local"
                and not story.get("escalated")):
            # No local_model_fallback opt-in for this plan: under auto
            # dispatch, escalate to Claude instead of cycling on the same
            # struggling local model forever. Mutually exclusive with the
            # local-fallback branch above (gated on `not fallback_model`) -
            # no chaining from local fallback to Claude.
            if story.get("step_cap_streak_model") == current_model:
                story["step_cap_streak"] = story.get("step_cap_streak", 0) + 1
            else:
                story["step_cap_streak"] = 1
                story["step_cap_streak_model"] = current_model
            if story["step_cap_streak"] >= STEP_CAP_FALLBACK_THRESHOLD:
                _escalate_to_claude(manifest, plan_name, story_key, manifest_path)
                _notify_user(
                    plan_name,
                    f"{story_key} hit the step cap {STEP_CAP_FALLBACK_THRESHOLD}x "
                    f"on {current_model}; escalating to Claude (no "
                    f"local_model_fallback configured).")
                return {"status": "todo", "reason": "step_cap_escalated_to_claude",
                        "pid": pid}
        _atomic_write_json(manifest_path, manifest)
        return {"status": "interrupted", "pid": pid, "reason": "step_cap_reached"}

    test_dir, test_cmd = detect_test_command(worktree)

    # FM-A: when the story carries an acceptance block, gate on only those
    # oracle test files rather than the full worktree suite. The model's own
    # tests can contain wrong assertions (the "graded on own buggy tests"
    # failure mode); the harness-owned oracle is the authoritative bar.
    # _scope_test_cmd_to_acceptance scopes pytest (path args), cargo
    # (--test <stem>), and npm/yarn-with-node --test; other runners fall back
    # to the whole suite (the MBW safety net — a story without an acceptance
    # block, or a runner we can't safely scope, still gets the full re-run).
    #
    # Paths are materialized relative to the worktree root (dispatch_story),
    # but test_dir can be a child subdirectory when the buildable project
    # doesn't live at the worktree root (detect_test_command's fallback).
    # Use absolute paths so the scoped run works regardless of test_dir.
    acceptance = story.get("acceptance") or []
    if acceptance:
        acceptance_paths = [str(worktree / p) for p in _acceptance_rel_paths(story)]
        scoped = _scope_test_cmd_to_acceptance(test_cmd, acceptance_paths, test_dir)
        if scoped is not None:
            test_cmd = scoped

    # Grade in a clean dev env, not the MCP server's operational one. The
    # server carries PIPELINE_* (pause/resume thresholds, backend dispatch,
    # model defaults) so advance_pipeline/check_usage see the real config —
    # but those same vars override the defaults the test suite asserts
    # against (e.g. usage_gate thresholds, dispatch backend routing) and
    # false-fail the gate for every Python story. Strip them so the suite
    # sees the same defaults a developer runs it under.
    #
    # Also strip LOCAL_AGENT_* and REPO_ROOT: LOCAL_AGENT_* (read-heavy
    # windows, chat retry, etc.) are harness-config the scheduler's plist may
    # set for a run (e.g. LOCAL_AGENT_READ_HEAVY_DISTINCT_WINDOWS raised to
    # let a model explore longer), and test_local_agent.py asserts the
    # DEFAULTS — an override that survives into the graded run false-fails
    # the suite for every story in a repo that vendors the pipeline's own
    # tests (the dashboard worktree is the pipeline repo, so its full suite
    # includes test_local_agent.py). REPO_ROOT is a per-plan sentinel
    # (/nonexistent-...) that likewise isn't a developer default.
    test_env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }
    # Heavy build/test commands (cargo, npm, mvn, gradle, etc.) can run GB-
    # seconds of memory each. Serialize against other in-flight agents so
    # we never have N concurrent builds saturating the host. Cheap commands
    # (pytest, mvn, gradle, make, npm — depending on the project) skip the
    # lock entirely.
    if _is_heavy(test_cmd):
        with _heavy_lock():
            test_result = subprocess.run(
                test_cmd, cwd=test_dir, capture_output=True, text=True,
                env=test_env,
            )
    else:
        test_result = subprocess.run(
            test_cmd, cwd=test_dir, capture_output=True, text=True,
            env=test_env,
        )
    passed = test_result.returncode == 0

    # The agent produced real output and the tests ran: the launch worked, so
    # clear any failed-launch attempts accumulated by earlier infra blips.
    story.pop("dispatch_attempts", None)

    # False-positive guard: tests passing against an untouched worktree
    # (e.g. main's suite against an empty branch because the agent parked
    # in a repetition loop without writing code) is not "the task is done."
    # require at least one commit on the agent branch beyond the base
    # branch before we count it as `tests_passed`. Mark `failed` (not
    # `interrupted`) because re-dispatching the same prompt to the same
    # model on the same empty worktree is unlikely to produce a different
    # outcome next tick; better to surface it for the dashboard.
    if passed and not _worktree_has_new_commits(
        worktree, story_key, base_branch=_default_branch(),
    ):
        base = _default_branch()
        story["status"] = "failed"
        story["failure_reason"] = (
            f"tests passed but agent branch has no new commits vs {base}; "
            "agent likely parked without writing code."
        )
        _atomic_write_json(manifest_path, manifest)
        return {"status": "failed", "reason": "empty_agent_branch"}

    story["status"] = "tests_passed" if passed else "failed"

    # Opt-in review-on-acceptance-fail (PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1):
    # route a dispatch whose acceptance oracle FAILED — but which produced real
    # work (new commits on the agent branch) — to review instead of straight to
    # "failed", so the reviewer evaluates the failing submission and the rework
    # loop re-dispatches the model up to REWORK_MAX_ATTEMPTS with the reviewer's
    # feedback. This engages the reviewer (previously unreachable for any
    # acceptance-failing cell: every such cell parked at "failed" with
    # rework_attempts=0, review_verdict=None, so the configured rework budget
    # and reviewer never ran — observed live, 2026-07-17, 0/9 mlx cells reached
    # review, zero GLM reviewer usage). Production-aligned: a reviewer sees
    # failing CI and REQUEST_CHANGES; the merge gate (_reverify_acceptance)
    # still blocks any APPROVEd-but-failing merge, so this never lands wrong
    # code. An empty-branch park (no real work) stays "failed" — re-dispatching
    # the same stuck prompt to the same model won't help. Opt-in so default
    # production behavior is unchanged; review_story's existing rework cap
    # (park/escalate after REWORK_MAX_ATTEMPTS) bounds the cycles.
    if (not passed
            and story["status"] == "failed"
            and os.environ.get("PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL", "0") == "1"
            and _worktree_has_new_commits(
                worktree, story_key, base_branch=_default_branch())):
        story["status"] = "tests_passed"  # reviewable; reviewer sees the failure
        story["acceptance_failed_review"] = True

    # T6: distinguish an explicit agent surrender from an ordinary red test
    # run. A missing/wrong API is a story-scoping bug, not a model-capability
    # gap - the terminal notify in advance_pipeline uses this to point a
    # human at "clarify the story" instead of the generic "tests failed".
    give_up_summary = _last_done_summary(agent_log) if not passed else ""
    if give_up_summary and _is_give_up_summary(give_up_summary):
        story["failure_kind"] = "give_up"
    else:
        story.pop("failure_kind", None)

    _atomic_write_json(manifest_path, manifest)

    result = {
        "status": story["status"],
        "tests_passed": passed,
        "test_command": test_cmd,
        "output_tail": test_result.stdout[-500:],
    }
    if story.get("failure_kind"):
        result["failure_kind"] = story["failure_kind"]
    return result


def _terminate_and_checkpoint(
    manifest: dict[str, Any], manifest_path: Path, plan_name: str, story_key: str,
    story: dict[str, Any], *, pid: int, step: str, summary: str,
) -> str:
    """SIGTERM the dispatched process, checkpoint its worktree, journal the
    event, and mark the story interrupted (dispatch-eligible for resume).
    Shared by interrupt_story (manual) and check_story_status's dispatch
    watchdog (automatic, on a hung process past DISPATCH_WATCHDOG_SECONDS)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    sha = _commit_wip(story["worktree"], story_key, step)
    interrupted_at = datetime.now(timezone.utc).isoformat()
    _append_journal(plan_name, story_key, {
        "step": step,
        "summary": summary,
        "next_hint": "",
        "commit": sha,
        "ts": interrupted_at,
    })

    story["status"] = "interrupted"
    story["last_commit"] = sha
    story["interrupted_at"] = interrupted_at
    _atomic_write_json(manifest_path, manifest)
    return sha


@mcp.tool()
def interrupt_story(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Stop a dispatched agent and leave its story resumable.

    Sends SIGTERM to the agent's process (a no-op if it has already exited),
    commits any uncommitted work in its worktree as a checkpoint, and marks
    the story "interrupted" rather than "failed" so a later dispatch_story
    call resumes it instead of starting over. The worktree and branch are
    left in place.

    Acquires `_plan_lock` for the same reason dispatch_story does - two MCP
    servers can race here too, with one calling interrupt while the other
    calls dispatch on the same story, producing a manifest write race that
    leaves the worktree in an inconsistent state.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "another dispatch/interrupt is in progress for this plan",
            }
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
        manifest = json.loads(manifest_path.read_text())
        story = manifest["stories"].get(story_key)
        if not story:
            return {"ok": False, "error": f"No such story {story_key}"}
        if "pid" not in story:
            return {"ok": False, "error": "Story not dispatched"}

        sha = _terminate_and_checkpoint(
            manifest, manifest_path, plan_name, story_key, story,
            pid=story["pid"], step="interrupted",
            summary="Agent process terminated; checkpointed for resume.",
        )

        return {"ok": True, "status": "interrupted", "commit": sha}


@mcp.tool()
def mark_story_in_progress(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Transition the ticket to In Progress and update the local manifest.
    Use this before writing any code for a story.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    get_ticket_provider().set_state(story_key, LogicalState.IN_PROGRESS, plan_name)

    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if story_key not in manifest["stories"]:
        return {"ok": False, "error": f"No such story {story_key}"}
    manifest["stories"][story_key]["status"] = "in_progress"
    _atomic_write_json(manifest_path, manifest)
    return {"ok": True}


def _checkpoint_impl(
    plan_name: str, story_key: str, step: str, summary: str, next_hint: str = "",
) -> dict[str, Any]:
    """Checkpoint logic, factored out of the `checkpoint` tool so it can be
    reused directly by the local dispatch agent loop (scripts/local_agent.py
    calls this in-process for its `checkpoint` tool) without exposing this
    whole server's orchestration toolset (dispatch_story, approve_merge,
    advance_pipeline, ...) to a dispatched agent."""
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    story = manifest["stories"].get(story_key)
    if not story:
        return {"ok": False, "error": f"No such story {story_key}"}

    sha = _commit_wip(story["worktree"], story_key, step)
    record = {
        "step": step,
        "summary": summary,
        "next_hint": next_hint,
        "commit": sha,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _append_journal(plan_name, story_key, record)
    return {"ok": True, **record}


@mcp.tool()
def checkpoint(
    plan_name: str, story_key: str, step: str, summary: str, next_hint: str = "",
) -> dict[str, Any]:
    """
    Record a durable checkpoint for a dispatched agent's progress.

    Commits any uncommitted work in the story's worktree as a WIP commit and
    appends an entry to the story's journal (plan.story.journal.json). Call
    this after completing each idempotent step of a story so a killed agent
    can resume from the last checkpoint instead of starting over.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    return _checkpoint_impl(plan_name, story_key, step, summary, next_hint)


@mcp.tool()
def mark_story_done(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Transition the ticket to Done and update the local manifest.
    Use after you've reviewed and merged the agent's PR.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    get_ticket_provider().set_state(story_key, LogicalState.DONE, plan_name)

    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stories"][story_key]["status"] = "done"
    manifest["stories"][story_key].pop("parked_reason", None)
    _atomic_write_json(manifest_path, manifest)
    return {"ok": True}


# Story fields patch_story may edit. Deliberately excludes "status" (use
# set_story_status), "worktree", "pid", "review_verdict" and other
# pipeline-owned runtime state - this tool is for correcting what the plan
# authored, not for mechanically bypassing the review/merge gates.
_PATCHABLE_STORY_FIELDS = frozenset((
    "agent_instructions", "model", "persona", "risk", "dependencies",
    "acceptance", "pr_url", "summary",
))

# Every status value the pipeline itself assigns to a story (see the
# "status"] = / "status": literal assignments throughout this file). Kept as
# an explicit allowlist so set_story_status can't be used to invent a status
# the rest of the code doesn't know how to handle.
_VALID_STORY_STATUSES = frozenset((
    "todo", "in_progress", "running", "interrupted", "failed",
    "tests_passed", "pr_open", "changes_requested", "parked", "done",
))


@mcp.tool()
def patch_story(plan_name: str, story_key: str, fields: dict[str, Any]) -> dict[str, Any]:
    """
    Edit a story's plan-authored fields (agent_instructions, model, persona,
    risk, dependencies, acceptance, pr_url, summary) without hand-editing the
    manifest JSON.

    Hand-editing the manifest directly races the scheduler's 60s
    advance_all_plans tick - a read-modify-write on either side can silently
    clobber the other's write. This tool acquires the same _plan_lock the
    scheduler and dispatch_story use, so the edit is atomic with respect to
    it. Only the fields above may be set; status transitions go through
    set_story_status, not this tool.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    unknown = set(fields) - _PATCHABLE_STORY_FIELDS
    if unknown:
        return {"ok": False, "error": f"cannot patch field(s) {sorted(unknown)}: "
                                       f"only {sorted(_PATCHABLE_STORY_FIELDS)} are editable"}

    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "another dispatch/ingest/interrupt is in progress for this plan",
            }
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
        manifest = json.loads(manifest_path.read_text())
        story = manifest["stories"].get(story_key)
        if story is None:
            return {"ok": False, "error": f"No such story {story_key!r}"}
        story.update(fields)
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "story_key": story_key, "story": story}


@mcp.tool()
def set_story_status(plan_name: str, story_key: str, status: str) -> dict[str, Any]:
    """
    Transition a story to an explicit status without hand-editing the
    manifest JSON (e.g. resetting a "parked" story to "interrupted" so the
    scheduler retries it).

    Acquires _plan_lock for the same reason patch_story does. Only accepts
    the fixed set of statuses the pipeline itself assigns
    (todo/in_progress/running/interrupted/failed/tests_passed/pr_open/
    changes_requested/parked/done) - this is a sanctioned status change, not
    a way to invent pipeline state the rest of the code doesn't expect.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    if status not in _VALID_STORY_STATUSES:
        return {"ok": False, "error": f"invalid status {status!r}: "
                                       f"must be one of {sorted(_VALID_STORY_STATUSES)}"}

    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "another dispatch/ingest/interrupt is in progress for this plan",
            }
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
        manifest = json.loads(manifest_path.read_text())
        story = manifest["stories"].get(story_key)
        if story is None:
            return {"ok": False, "error": f"No such story {story_key!r}"}
        story["status"] = status
        if status != "parked":
            story.pop("parked_reason", None)
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "story_key": story_key, "status": status}


@mcp.tool()
def check_usage() -> dict[str, Any]:
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
    prev = _read_usage_state()
    try:
        state = _run_usage_probe()
    except ValueError:
        if not prev:
            raise
        now_iso = datetime.now(timezone.utc).isoformat()
        state = dict(prev)
        state["checked_at"] = now_iso
        # Count how many polls in a row have failed to parse, so the blind
        # window is visible (and quantifiable) rather than a silent stderr line.
        state["consecutive_parse_failures"] = prev.get("consecutive_parse_failures", 0) + 1
        measured_at = prev.get("measured_at", prev.get("checked_at"))
        state["measured_at"] = measured_at
        age = _usage_state_age_seconds({"checked_at": measured_at}) if measured_at else None
        if age is not None and age > USAGE_STALE_AFTER_SECONDS:
            state["stale"] = True
            state["gate_blind"] = True
            first_blind = not prev.get("gate_blind")
            if first_blind:
                state["blind_since"] = now_iso

            blind_since = state.get("blind_since")
            blind_age = _usage_state_age_seconds({"checked_at": blind_since}) if blind_since else None
            if blind_age is not None and blind_age > USAGE_BLIND_PAUSE_AFTER_SECONDS:
                # Prolonged blindness: fail-closed so a permanent CLI-format
                # change can't leave spend unguarded indefinitely.
                state["paused"] = True
            else:
                state["paused"] = False

            failures = state["consecutive_parse_failures"]
            should_log = first_blind or (failures % USAGE_BLIND_LOG_INTERVAL == 0)
            if should_log:
                status = "pausing (fail-closed)" if state["paused"] else "failing the gate OPEN"
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
        prev.get("paused", False), state["session_pct"], state["week_pct"],
    )
    # A real measurement clears any blind/stale state from prior failures.
    state["consecutive_parse_failures"] = 0
    state["gate_blind"] = False
    state["stale"] = False
    _write_usage_state(state)
    return state


@mcp.tool()
def request_decision(
    plan_name: str,
    story_key: str,
    question: str,
    options: list[str],
    context: str = "",
) -> dict[str, Any]:
    """
    Escalate a blocking decision to the overlord, which rules on the user's
    behalf per the decision policy. The ruling is appended to the plan's
    decisions log (audit trail) and returned. Call this from a story agent
    when you are blocked on a choice the user would normally make.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    with _scoped_repo_root(plan_name):
        policy = _load_policy()
    opts = "\n".join(f"  - {o}" for o in options)
    prompt = (
        f"A pipeline agent working on story {story_key} is blocked on a decision.\n\n"
        f"QUESTION: {question}\n\n"
        f"OPTIONS:\n{opts}\n\n"
        f"CONTEXT: {context}\n\n"
        f"DECISION POLICY:\n{policy}\n\n"
        f"Rule now, using your output contract exactly."
    )
    ruling = _parse_ruling(
        _invoke_overlord(prompt, plan_role_config=_plan_role_config(plan_name))
    )
    record = {
        "story_key": story_key,
        "question": question,
        "options": list(options),
        **ruling,
        "decided_by": "overlord",
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    _append_decision(plan_name, record)
    return record


@mcp.tool()
def list_decisions(plan_name: str) -> list[dict]:
    """Return the overlord decision log for a plan (audit trail)."""
    _validate_key(plan_name)
    path = _decisions_path(plan_name)
    return json.loads(path.read_text()) if path.exists() else []


@mcp.tool()
def review_story(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Run the code-reviewer persona over a dispatched story's branch. On APPROVE,
    open a PR via gh and set status to pr_open; otherwise set status to
    changes_requested. Does not merge — merge is the overlord's decision.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    story = manifest["stories"].get(story_key)
    if not story:
        return {"ok": False, "error": f"No such story {story_key}"}

    branch = f"agent/{story_key.lower()}"
    worktree = story.get("worktree", "")
    plan_role_config = _plan_role_config(plan_name)
    try:
        # Once a story is escalated (see _escalate_review_to_claude below),
        # every subsequent review must go to Claude regardless of the global
        # PIPELINE_BACKEND_REVIEW setting - review backend is otherwise
        # resolved purely from that env var with no per-story override, so
        # this is the one seam that needs an explicit check.
        reviewer_output = (
            _run_reviewer(worktree, branch, backend_name="claude",
                          plan_role_config=plan_role_config,
                          acceptance=story.get("acceptance"))
            if story.get("escalated") else
            _run_reviewer(worktree, branch, plan_role_config=plan_role_config,
                          acceptance=story.get("acceptance"))
        )
    except backend.RateLimitedError:
        # FM-B: an Ollama-cloud (or any Ollama-proxied) 429 on the review path
        # is an infrastructure event, not a real review cycle. Treat it the
        # same as Claude's weekly-usage pause: defer and retry on the next
        # tick, do NOT burn REVIEW_INCONCLUSIVE_MAX. Without this, a
        # misclassified rate-limit would eventually park a correct impl.
        story["review_deferred_count"] = story.get("review_deferred_count", 0) + 1
        _notify_user(plan_name,
                     f"{story_key} review deferred: local reviewer rate-limited; will retry next tick.")
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "status": story["status"], "deferred": "rate_limited"}
    except Exception as e:
        # Defense in depth: a reviewer backend's own internal error (a bad
        # tool-call shape, a malformed backend response, ...) must not crash
        # the pipeline process. Fail safe into the same UNKNOWN-verdict path
        # a genuinely inconclusive review already takes below - never treat
        # this as an APPROVE (fail-closed). Log only the exception type, not
        # its text, which could carry sensitive detail.
        _notify_user(plan_name, f"{story_key} review failed with an unexpected "
                                f"{type(e).__name__}; treating as inconclusive.")
        reviewer_output = ""
    verdict = _parse_verdict(reviewer_output)

    # FM-B: a rate-limit response from the reviewer is an infrastructure event,
    # not a genuine review cycle. Leave the story at tests_passed so the next
    # advance_pipeline tick retries review once the backend recovers. Do NOT
    # touch rework_attempts — burning the rework budget on rate-limits parks
    # correct implementations silently.
    if verdict == "UNKNOWN" and _is_rate_limited(reviewer_output):
        story["review_deferred_count"] = story.get("review_deferred_count", 0) + 1
        fallback_mode = os.environ.get("PIPELINE_REVIEW_FALLBACK", "off").strip().lower()
        fallback_after = int(os.environ.get("PIPELINE_REVIEW_FALLBACK_AFTER", "3"))
        if fallback_mode in _LOCAL_BACKEND_NAMES and story["review_deferred_count"] >= fallback_after:
            _notify_user(plan_name, f"{story_key} review falling back to {fallback_mode} backend "
                                    f"after {story['review_deferred_count']} rate-limited attempts.")
            reviewer_output = _run_reviewer(
                worktree, branch, backend_name=fallback_mode,
                plan_role_config=plan_role_config,
                acceptance=story.get("acceptance"),
            )
            verdict = _parse_verdict(reviewer_output)
            # Fall through into the normal verdict-handling code below —
            # this is a genuine review attempt now, not a deferral.
        else:
            _notify_user(plan_name, f"{story_key} review deferred: reviewer rate-limited; will retry next tick.")
            _atomic_write_json(manifest_path, manifest)
            return {"ok": True, "status": story["status"], "deferred": "rate_limited"}

    # Transient backend error (HTTP 500 / connection-reset / connection-refused):
    # re-invoke the reviewer once inline. This is an infrastructure hiccup, not
    # a genuine review cycle, so do NOT increment review_inconclusive_count for
    # this branch itself — only the fallback inconclusive path below (reached
    # when still UNKNOWN after the single retry) touches that counter.
    _transient_retried = False
    if verdict == "UNKNOWN" and _is_transient_backend_error(reviewer_output):
        _notify_user(plan_name, f"{story_key} review hit transient backend error; retrying once.")
        reviewer_output = (
            _run_reviewer(worktree, branch, backend_name="claude",
                          plan_role_config=plan_role_config,
                          acceptance=story.get("acceptance"))
            if story.get("escalated") else
            _run_reviewer(worktree, branch, plan_role_config=plan_role_config,
                          acceptance=story.get("acceptance"))
        )
        verdict = _parse_verdict(reviewer_output)
        _transient_retried = True

    story["review_verdict"] = verdict
    story["review_deferred_count"] = 0

    # High-risk stories require an additional security-engineer pass; both
    # must APPROVE before the story proceeds to pr_open.
    if verdict == "APPROVE" and story.get("risk") == "high":
        security_output = _run_security_reviewer(worktree, branch)
        security_verdict = _parse_verdict(security_output)

        # FM-B: same rate-limit deferral for the security-reviewer pass.
        if security_verdict == "UNKNOWN" and _is_rate_limited(security_output):
            _notify_user(plan_name,
                         f"{story_key} security review deferred: reviewer rate-limited; will retry next tick.")
            _atomic_write_json(manifest_path, manifest)
            return {"ok": True, "status": story["status"], "deferred": "rate_limited"}

        story["security_review_verdict"] = security_verdict
        if security_verdict != "APPROVE":
            verdict = security_verdict
            reviewer_output = security_output  # use security feedback for rework

    # A non-rate-limited UNKNOWN is inconclusive, not a rejection: don't touch
    # review_feedback or rework_attempts, and leave status at its pre-review
    # value so the next advance_pipeline tick retries review. Fail closed -
    # this must never fall through to the APPROVE branch. Only after repeated
    # inconclusive attempts does it park for a human.
    if verdict == "UNKNOWN":
        inconclusive = story.get("review_inconclusive_count", 0) + 1
        story["review_inconclusive_count"] = inconclusive
        if inconclusive >= REVIEW_INCONCLUSIVE_MAX:
            if _auto_escalation_enabled() and not story.get("escalated"):
                _escalate_review_to_claude(
                    story, story_key, plan_name,
                    f"review inconclusive after {inconclusive} attempts",
                )
                # status stays at its pre-review value (e.g. tests_passed) -
                # the next tick retries review, now resolved via Claude.
            else:
                story["status"] = "parked"
                story["parked_reason"] = (
                    f"review inconclusive after {inconclusive} attempts - needs human review"
                )
                _notify_user(plan_name, f"{story_key} parked: review inconclusive after "
                                        f"{inconclusive} attempts - needs human review.")
        else:
            _notify_user(plan_name, f"{story_key} review inconclusive; will retry.")
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "verdict": verdict, "status": story["status"]}

    # T11: a REQUEST_CHANGES with no substantive findings text is not a
    # genuine rejection - it gives the redispatched agent nothing to fix, and
    # treating it as one silently burns the rework budget on nothing (the
    # 2026-07-02 gpt-oss run parked a story this way). Route it through the
    # same inconclusive-handling shape as UNKNOWN above - before the
    # review_inconclusive_count reset below, so repeated empty responses
    # still accumulate toward REVIEW_INCONCLUSIVE_MAX - but leave the verdict
    # itself visible and never touch rework_attempts/review_feedback. Checked
    # here (not merged into the UNKNOWN branch above) because it applies
    # equally to a content-free REQUEST_CHANGES from either the ordinary
    # reviewer or a security-reviewer override.
    if verdict == "REQUEST_CHANGES" and not _has_review_findings(reviewer_output):
        inconclusive = story.get("review_inconclusive_count", 0) + 1
        story["review_inconclusive_count"] = inconclusive
        if inconclusive >= REVIEW_INCONCLUSIVE_MAX:
            if _auto_escalation_enabled() and not story.get("escalated"):
                _escalate_review_to_claude(
                    story, story_key, plan_name,
                    f"review inconclusive after {inconclusive} attempts (empty REQUEST_CHANGES)",
                )
            else:
                story["status"] = "parked"
                story["parked_reason"] = (
                    f"review inconclusive after {inconclusive} attempts - needs human review"
                )
                _notify_user(plan_name, f"{story_key} parked: review inconclusive after "
                                        f"{inconclusive} attempts - needs human review.")
        else:
            _notify_user(plan_name, f"{story_key} review approved-changes-requested-empty: "
                                    f"REQUEST_CHANGES with no findings text; will retry.")
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "verdict": verdict, "status": story["status"]}

    story["review_inconclusive_count"] = 0

    if verdict == "APPROVE":
        pr_url = _open_pr(worktree, story_key, story)
        story["pr_url"] = pr_url
        story["status"] = "pr_open"
        # The work passed: drop any stale rework state from earlier cycles.
        story.pop("review_feedback", None)
        story.pop("rework_attempts", None)
    else:
        # Persist the reviewer's reasoning (not just the verdict) so the
        # redispatched agent knows what to fix, and count the cycle against
        # the rework budget so a perpetually-rejected story eventually parks
        # for a human instead of looping review -> rework forever.
        #
        # Mode 20 (2026-07-17, verified by replay): a REQUEST_CHANGES verdict
        # on an acceptance-bearing story can be correct about SOMETHING
        # outside the oracle's scope while the oracle itself is currently
        # green - and a whole-file rework, given only the reviewer's raw
        # feedback, has no signal that it must not regress that already-
        # correct behavior (observed: this exact gap let a rework destroy a
        # passing backward-jump fix). Re-verify the oracle against the
        # CURRENT worktree state before dispatching rework and, if it still
        # passes, prepend an explicit warning. This does not change the
        # verdict or control flow - the story still goes to rework - it only
        # gives the next dispatch a fact the reviewer's own text can't convey.
        feedback = reviewer_output
        if story.get("acceptance"):
            oracle_now = _reverify_acceptance(story, worktree)
            if oracle_now.get("state") == "pass":
                feedback = (
                    "NOTE: the acceptance oracle is currently PASSING against "
                    "this worktree. The reviewer's feedback below may be about "
                    "something outside the oracle's required behavior - do "
                    "NOT regress the acceptance-oracle-passing behavior while "
                    "addressing it, and re-run the acceptance tests after your "
                    "change to confirm they are still green.\n\n" + reviewer_output
                )
        story["review_feedback"] = feedback
        attempts = story.get("rework_attempts", 0) + 1
        story["rework_attempts"] = attempts
        if story.get("escalated"):
            rework_cap = REWORK_MAX_ATTEMPTS_ESCALATED
        elif story.get("acceptance"):
            rework_cap = REWORK_MAX_ATTEMPTS_ORACLE
        else:
            rework_cap = REWORK_MAX_ATTEMPTS
        if attempts >= rework_cap:
            if _auto_escalation_enabled() and not story.get("escalated"):
                _escalate_review_to_claude(
                    story, story_key, plan_name,
                    f"rework budget exhausted after {attempts} review cycles",
                )
                # A redispatch will pick up the real review_feedback already
                # set above, now on Claude (story["backend"] was just set).
                story["status"] = "changes_requested"
            else:
                story["status"] = "parked"
                story["parked_reason"] = f"rework budget exhausted after {attempts} review cycles"
                _notify_user(plan_name, f"{story_key} parked: reviewer still requesting changes "
                                        f"after {attempts} cycles - needs human review.")
        else:
            story["status"] = "changes_requested"

    _atomic_write_json(manifest_path, manifest)
    return {
        "ok": True,
        "verdict": verdict,
        "status": story["status"],
        "pr_url": story.get("pr_url"),
    }


@contextmanager
def _plan_lock(plan_name: str):
    """Exclusive, non-blocking lock scoped to one plan's mutations.

    Used by every tool that mutates the manifest or the worktree
    (advance_pipeline, _set_plan_paused, dispatch_story, interrupt_story).
    The lock is `flock`-based, so it serializes across MCP server processes
    too - two Claude sessions with two MCP server PIDs calling
    dispatch_story on the same story in the same window both want to write
    to the same manifest and create the same worktree, and without this
    guard the second one treats the first's half-built worktree as
    resumable and spawns a second agent into the same directory. Multiple
    agents fighting over one worktree's git state is what produces the
    repeated zero-output agent deaths, not per-story flakiness.

    Reentrant within a single thread: advance_pipeline acquires this lock
    for its whole tick and then calls dispatch_story / interrupt_story,
    which each re-acquire it. flock locks are held per open-file-description
    (a fresh os.open makes a new description), so a nested exclusive flock
    on the same file fails with BlockingIOError *even within the same
    process* — without reentrance the nested call would return
    skipped:"locked" and advance_pipeline would falsely count it as
    dispatched/interrupted while doing nothing. The per-thread held-set
    lets the nested call proceed without re-flocking; cross-thread and
    cross-process serialization is still enforced by flock itself.

    Yields whether the lock was acquired; the caller must check it and skip
    all work if not - this never blocks waiting for the lock.
    """
    held = _held_plan_locks()
    if plan_name in held:
        # Same thread already holds the flock for this plan (nested call
        # from within an advance_pipeline tick). Don't re-flock — a second
        # exclusive flock on a new fd would fail.
        yield True
        return
    lock_path = PLAN_DIR / f"{plan_name}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        if acquired:
            held.add(plan_name)
        try:
            yield acquired
        finally:
            if acquired:
                held.discard(plan_name)
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _held_plan_locks() -> set[str]:
    """Per-thread set of plan names whose flock this thread currently holds,
    for _plan_lock reentrance. threading.local keeps each thread's view
    independent, so thread A holding a plan does not let thread B bypass the
    flock — B's set is empty, so it hits the real flock and serializes."""
    held = getattr(_plan_lock_state, "held", None)
    if held is None:
        held = set()
        _plan_lock_state.held = held
    return held


_plan_lock_state = threading.local()


@contextmanager
def _heavy_lock():
    """Serializes heavy build/test invocations across all local-agent
    dispatch paths.

    Three concurrent cold builds can push a 24GB M4 to its knees (observed
    in the post-PR #30 e2e rerun: 33GB total pressure, CPU saturated).
    Each worktree has its own target/ (or build/), so concurrent
    invocations don't share cache — they multiply memory pressure rather
    than amortizing it.

    Blocking acquire (LOCK_EX, not LOCK_EX | LOCK_NB) is the right call
    here: callers are already prepared to wait minutes for a build, and
    skipping entirely would just give the agent a false "build failed"
    error and waste more time. The queueing cost is invisible when the
    model is doing non-build work in the meantime.

    Held by every site that runs a heavy build/test:
      - check_story_status (orchestrator's post-dispatch grading)
      - local_agent.py / local_agent_oracle.py `bash` tool (model-invoked)
      - backend.py reviewer bash (reviewer-invoked)
    Decide what counts as heavy with `_is_heavy()`.
    """
    lock_path = PLAN_DIR / "heavy.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# Heavy build/test executables: these typically spend GB-seconds of memory
# running (linkers, type checkers, full compilers). Lock them across
# dispatchees so we never run more than one at a time, regardless of
# language. `make` is gated on a build/test target because make is also
# used for trivial scripts — we don't want to serialize `make clean`.
HEAVY_EXECUTABLES = frozenset({
    "cargo", "npm", "yarn", "pnpm", "npx",
    "mvn", "gradle", "./gradlew",
    "sbt", "bazel", "buck",
    "go", "rustc", "swift", "swiftc",
})


def _is_heavy(cmd: list[str]) -> bool:
    """True iff a subprocess command should acquire the heavy lock.

    Matched by argv[0] against a static list of build/test executables.
    No parsing of the command body — keep the check O(1) and language-
    agnostic. `make` is special-cased to only the well-known heavy
    targets (`test`/`build`/`check`/`all`/`ci`) because make is also
    used for trivial scripts where the lock would just add latency.
    """
    if not cmd:
        return False
    exe = cmd[0]
    if exe in HEAVY_EXECUTABLES:
        return True
    if exe == "make" and len(cmd) > 1 and cmd[1] in ("test", "build", "check", "all", "ci"):
        return True
    return False


@mcp.tool()
def advance_pipeline(plan_name: str) -> dict[str, Any]:
    """
    Run one orchestration tick: dispatch every ready story (deps satisfied),
    advance finished stories through test -> review -> PR, and adjudicate merges
    against the risk threshold. Idempotent; designed to be called repeatedly by
    a scheduler (/loop or cron). In PIPELINE_AUTONOMY=dry-run it plans and logs
    only, taking no actions.

    Honors a per-backend resource gate: dispatch and review are gated
    independently by their own backend's resource_status() (see
    _role_resource_ok). If the dispatch backend is gated, in-progress stories
    are interrupted (checkpointed, resumable) and no new dispatch starts; if
    the review backend is gated, review is deferred. Each is independent, so a
    Claude usage pause no longer freezes local-backed dispatch. Merge
    adjudication always runs (no model usage). "interrupted" stories are
    dispatch-eligible like "todo" ones, so they resume automatically once the
    dispatch backend frees up.

    Also honors MAX_CONCURRENT_AGENTS: dispatch is capped to the number of
    free slots remaining (limit minus agents already in_progress across all
    plans), so a tick never starts more agents than the configured ceiling.
    Stories left undispatched this tick stay "todo"/"interrupted" and are
    picked up on a later tick as slots free up.

    Skips entirely (returns {"ok": True, "skipped": "locked"}) if another
    tick for this same plan is already running - see _plan_lock.
    """
    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "another advance_pipeline tick is already running for this plan",
            }
        return _advance_pipeline_locked(plan_name)


def _advance_pipeline_locked(plan_name: str) -> dict[str, Any]:
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    if not manifest_path.exists():
        return {"ok": False, "error": f"No manifest for {plan_name}"}
    manifest = json.loads(manifest_path.read_text())
    stories = manifest["stories"]

    if manifest.get("paused"):
        # A human-requested pause for this one plan: unlike the usage gate,
        # this doesn't even adjudicate merges - the plan should sit
        # completely still until explicitly resumed. Still free up any
        # running agent so a paused plan isn't quietly burning usage.
        with _scoped_repo_root(plan_name):
            for key, story in stories.items():
                if story["status"] == "in_progress" and "pid" in story:
                    interrupt_story(plan_name, key)
        return {"ok": True, "skipped": "plan_paused"}

    # Per-backend resource gate (Step 5): dispatch and review can run on
    # different backends, so gate each by ITS backend's availability rather
    # than one global Claude flag. This is what lets local dispatch keep
    # running when Claude's weekly limit is hit (and vice versa).
    dispatch_ok, dispatch_reason = _role_resource_ok("dispatch")
    review_ok, review_reason = _role_resource_ok("review")
    # A-posteriori escalation of a failed local run to Claude is a feature of
    # auto dispatch only. Under an explicit local (or claude) backend the
    # operator has pinned the dispatcher on purpose, so a local failure is
    # terminal rather than silently spending Claude.
    dispatch_mode = os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude").strip().lower()

    done = _completed_dep_ids(stories)
    ready = [
        k for k, v in stories.items()
        if v["status"] in ("todo", "interrupted", "changes_requested")
        and all(d in done for d in v.get("dependencies", []))
    ]

    if PIPELINE_AUTONOMY == "dry-run":
        return {
            "ok": True,
            "dry_run": True,
            "autonomy": PIPELINE_AUTONOMY,
            # "paused" kept for back-compat = dispatch gated.
            "paused": not dispatch_ok,
            "dispatch_paused": not dispatch_ok,
            "review_paused": not review_ok,
            "would_dispatch": ready if dispatch_ok else [],
            "would_merge_decisions": {
                k: _merge_decision(v)
                for k, v in stories.items() if v["status"] == "pr_open"
            },
        }

    summary: dict[str, Any] = {
        "autonomy": PIPELINE_AUTONOMY,
        "paused": not dispatch_ok,
        "dispatch_paused": not dispatch_ok,
        "review_paused": not review_ok,
        "dispatched": [], "advanced": [], "merged": [],
        "parked": [], "failed": [], "interrupted": [], "notify": [],
        "review_deferred": [],
    }

    # Scoped for the whole tick: dispatch_story resolves its own repo_root
    # too (so it's correct called standalone), but _merge_pr and
    # _default_branch read the plain REPO_ROOT global, so this plan's repo
    # must be active for the duration of every action below.
    with _scoped_repo_root(plan_name):
        if not dispatch_ok:
            # The dispatch backend is gated: stop spending it, and free up
            # in-flight agents (they run on the dispatch backend and are
            # resumable via their checkpoint journal) rather than letting them
            # keep burning the resource we're protecting.
            #
            # Exception: a local-memory-pressure gate ("insufficient free
            # memory") is self-inflicted by an in-progress dispatch actively
            # loading its model into memory - it is not burning a shared,
            # exhaustible resource the way Claude usage or a downed server
            # would be. Killing it doesn't free anything real; it destroys
            # progress and the redispatch (interrupted stories are dispatch-
            # eligible) immediately re-triggers the identical gate once the
            # new process starts loading again. Observed live 2026-07-13: a
            # new PID every ~10-20s across three separate model runs, never
            # converging. Every OTHER gate reason (Claude usage exhausted,
            # Ollama unreachable, ...) still interrupts as before - those
            # really do mean "stop spending this backend now."
            memory_pressure = "insufficient free memory" in dispatch_reason
            for key, story in stories.items():
                if story["status"] == "in_progress" and "pid" in story:
                    if memory_pressure:
                        continue
                    interrupt_story(plan_name, key)
                    summary["interrupted"].append(key)
            _notify_user(plan_name, f"Dispatch backend gated ({dispatch_reason}): deferring dispatch.")
            summary["notify"].append("dispatch_paused")
        else:
            # 1. Dispatch ready (and resumable-interrupted) stories, capped to
            # the slots still free under MAX_CONCURRENT_AGENTS. <=0 means no cap.
            if MAX_CONCURRENT_AGENTS > 0:
                slots = max(0, MAX_CONCURRENT_AGENTS - _count_in_progress_agents())
                to_dispatch = ready[:slots]
            else:
                to_dispatch = ready
            for key in to_dispatch:
                try:
                    dispatch_story(plan_name, key)
                    summary["dispatched"].append(key)
                except Exception as e:  # git pull/worktree/backend launch failure
                    # Re-read: dispatch_story only writes the manifest on a
                    # successful launch, so on a raise the on-disk status is
                    # still todo/interrupted - bump the attempt counter there.
                    m = json.loads(manifest_path.read_text())
                    st = m["stories"][key]
                    attempts = st.get("dispatch_attempts", 0) + 1
                    st["dispatch_attempts"] = attempts
                    if attempts >= DISPATCH_MAX_ATTEMPTS:
                        st["status"] = "failed"
                        st["dispatch_error"] = str(e)
                        _notify_user(plan_name, f"{key} dispatch failed {attempts}x "
                                                f"({e}); giving up - needs human intervention.")
                        summary["failed"].append(key)
                    else:
                        # leave status dispatch-eligible; the next tick retries.
                        _notify_user(plan_name, f"{key} dispatch attempt {attempts}/"
                                                f"{DISPATCH_MAX_ATTEMPTS} failed ({e}); will retry.")
                    summary["notify"].append(key)
                    _atomic_write_json(manifest_path, m)

            # 2. Poll running agents: tests fail -> notify (or escalate); tests pass -> tests_passed.
            manifest = json.loads(manifest_path.read_text())
            stories = manifest["stories"]
            for key, story in stories.items():
                if story["status"] == "in_progress" and "pid" in story:
                    check_result = check_story_status(plan_name, key)
                    status = check_result.get("status")
                    if status == "failed":
                        fallback_model = manifest.get("local_model_fallback")
                        # A-posteriori escalation: under auto dispatch, if the
                        # local agent failed and has NOT been escalated before,
                        # wipe its worktree and re-queue for Claude. A second
                        # failure (on Claude), or any failure under an explicit
                        # non-auto backend, is terminal.
                        if (dispatch_mode == "auto"
                                and story.get("backend") == "local"
                                and not story.get("escalated")):
                            manifest = json.loads(manifest_path.read_text())
                            _escalate_to_claude(manifest, plan_name, key, manifest_path)
                            _notify_user(plan_name,
                                f"{key} local agent failed; escalating to Claude and starting clean.")
                            summary["notify"].append(key)
                        elif (fallback_model
                                and story.get("backend") == "local"
                                and story.get("model") != fallback_model
                                and not story.get("tried_fallback_model")):
                            # Plan-scoped opt-in (manifest["local_model_fallback"]):
                            # never escalates to Claude - just gives one other
                            # local model a shot before the terminal park/fail
                            # path below.
                            manifest = json.loads(manifest_path.read_text())
                            failed_model = story.get("dispatched_model") or story.get("model") or "default"
                            _escalate_to_local_fallback_model(
                                manifest, plan_name, key, manifest_path, fallback_model)
                            _notify_user(plan_name,
                                f"{key} local agent failed on {failed_model}; retrying on "
                                f"fallback model {fallback_model} before parking.")
                            summary["notify"].append(key)
                        elif check_result.get("failure_kind") == "give_up":
                            # T6: the agent explicitly surrendered rather than
                            # producing ordinary red tests. Point the human at
                            # the story's scope/clarity instead of the generic
                            # message - a missing/wrong API needs a fix to
                            # agent_instructions, not another identical retry.
                            _notify_user(plan_name,
                                f"{key} agent gave up (explicit surrender, zero productive "
                                f"progress) - likely under-specified (missing API, wrong "
                                f"scope) rather than a model-capability gap; needs human "
                                f"clarification before another dispatch.")
                            summary["failed"].append(key)
                            summary["notify"].append(key)
                        else:
                            _notify_user(plan_name, f"{key} tests failed")
                            summary["failed"].append(key)
                            summary["notify"].append(key)

        # Review every tests_passed story (incl. ones orphaned by a crashed
        # review on a prior tick - review_story is idempotent). Gated by the
        # REVIEW backend independently of dispatch: a Claude-dispatch pause no
        # longer blocks reviewing already-finished work on a healthy review
        # backend, and a local-dispatch run can still defer review if review
        # is on Claude and Claude is gated.
        if review_ok:
            stories = json.loads(manifest_path.read_text())["stories"]
            for key, story in stories.items():
                if story["status"] == "tests_passed":
                    rv = review_story(plan_name, key)
                    summary["advanced"].append({key: rv["status"]})
                    if rv.get("deferred") == "rate_limited":
                        summary["review_deferred"].append(key)
        else:
            _notify_user(plan_name, f"Review backend gated ({review_reason}): deferring review.")
            summary["notify"].append("review_paused")

        # 3. Adjudicate merges for reviewed PRs (no model usage; runs even paused).
        manifest = json.loads(manifest_path.read_text())
        stories = manifest["stories"]
        for key, story in stories.items():
            if story["status"] != "pr_open":
                continue
            decision = _merge_decision(story)
            if decision["action"] != "merge":
                story["status"] = "parked"
                story["parked_reason"] = decision["reason"]
                _notify_user(plan_name, f"{key} parked: {decision['reason']}")
                summary["parked"].append(key)
                summary["notify"].append(key)
                continue

            # Mode 9: rebase onto current origin/master + CI gate before merge,
            # so a stale-base branch can't land cross-story breakage or a
            # ruff-red PR onto main. Failures count against merge_attempts just
            # like a transient `gh pr merge` failure (see MERGE_MAX_ATTEMPTS).
            branch = f"agent/{key.lower()}"
            worktree = story.get("worktree", "")
            gate_error = ""
            ci_definitive_fail = False
            rb = _rebase_onto_master(worktree, branch)
            if rb.get("auto_resolved"):
                _notify_user(plan_name, f"{key} rebase auto-resolved an additive-import "
                                        f"conflict against origin/{_default_branch()}.")
            if not rb["ok"]:
                gate_error = f"rebase: {rb['error']}"
            else:
                # Force-push the rebased branch; only when we actually rebased
                # in a real worktree (a missing worktree skipped the rebase and
                # has nothing to push). Run from REPO_ROOT (the plan's repo).
                # A failed push (concurrent push rejected by --force-with-lease,
                # network/auth) MUST block: otherwise the remote HEAD stays at
                # the pre-rebase commit and the CI gate + squash merge operate
                # on stale code — the exact cross-story breakage Mode 9 closes.
                if Path(worktree).is_dir():
                    push = subprocess.run(["git", "push", "--force-with-lease", "origin",
                                           branch], cwd=REPO_ROOT,
                                          capture_output=True, text=True)
                    if push.returncode != 0:
                        gate_error = f"push: {(push.stderr or push.stdout).strip()[:200]}"
                if not gate_error:
                    ci = _ci_status(branch)
                    if ci["state"] == "cancelled" and not story.get("ci_rerun_attempted"):
                        # Worth exactly one automatic rerun before treating it
                        # as a failure - an abnormal queue delay can cancel
                        # jobs with no code-quality signal at all.
                        story["ci_rerun_attempted"] = True
                        _ci_rerun(branch)
                        ci = _ci_status(branch)
                    if ci["state"] == "fail":
                        gate_error = f"ci fail: {ci['error']}"
                        # Only a genuine test-failure verdict is "definitive" -
                        # cancelled (queue/infra flake, already given one
                        # auto-rerun above) and pending are NOT, and must keep
                        # retrying via the ordinary merge_attempts path below,
                        # not consume rework budget.
                        ci_definitive_fail = True
                    elif ci["state"] == "cancelled":
                        gate_error = f"ci fail: {ci['error']}"
                    elif ci["state"] == "pending":
                        gate_error = f"ci pending: {ci['error']}"
                if not gate_error:
                    # Independent of review: re-run the acceptance oracle
                    # against the just-rebased branch right before merging.
                    # Closes the gap CI alone can't (a repo without CI, or a
                    # CI-independent slip between tests_passed and review).
                    acc = _reverify_acceptance(story, worktree)
                    if acc["state"] == "fail":
                        gate_error = f"acceptance reverify fail: {acc['error']}"
                if not gate_error:
                    # Independent of tests: a green suite doesn't mean the
                    # project actually builds (PR #48 merged with `npm run
                    # build` broken - retro §3.1).
                    build = _reverify_build(worktree)
                    if build["state"] == "fail":
                        gate_error = f"build reverify fail: {build['error']}"

            if gate_error:
                # Opt-in (PIPELINE_REWORK_ON_CI_FAIL=1): a DEFINITIVE CI test
                # failure - not a transient rebase/push error, not
                # pending/cancelled - can be caused by the agent's own
                # committed test file rather than the reviewed implementation
                # (the reviewer is acceptance-scoped and never saw it). Retrying
                # an unchanged branch identically MERGE_MAX_ATTEMPTS times can
                # never fix that; hand the CI failure back to the implementer as
                # rework feedback instead, bounded by the SAME rework budget
                # review_story uses, so a story that never converges still
                # parks/escalates rather than looping forever. See
                # MERGE_CI_REWORK_PLAN.md, 2026-07-17 (gpt-oss retry_backoff /
                # token_bucket: ground-truth-correct code abandoned because the
                # agent's own broken self-test tripped this gate).
                rework_ok = (
                    ci_definitive_fail
                    and os.environ.get("PIPELINE_REWORK_ON_CI_FAIL", "0") == "1"
                )
                if rework_ok:
                    # Bound by MERGE_MAX_ATTEMPTS via the merge_attempts
                    # counter, which PERSISTS across the rework -> review
                    # APPROVE -> merge-gate cycle. rework_attempts does NOT:
                    # the review APPROVE path (~line 4189) pops it on every
                    # pass (the reviewer APPROVEs because it is acceptance-
                    # scoped and the oracle is green), so reusing
                    # rework_attempts here loops forever - each CI-fail
                    # re-increments 0->1 and the cap never exhausts (verified
                    # 2026-07-17 on token_bucket: four identical "routed to
                    # rework (1/3)" notifications, same broken assertion
                    # every round). merge_attempts is the merge gate's own
                    # counter and is not reset by review, so it bounds the
                    # loop: MERGE_MAX_ATTEMPTS rework rounds, then the
                    # fall-through below terminal-fails.
                    rework_ok = story.get("merge_attempts", 0) < MERGE_MAX_ATTEMPTS

                if rework_ok:
                    attempts = story.get("merge_attempts", 0) + 1
                    story["merge_attempts"] = attempts
                    # L1 (REVIEWER_ESCALATION_PLAN.md): flag this rework as
                    # CI-triggered so the next dispatch_story raises the
                    # agent's done-bar to full-suite-green (env
                    # LOCAL_AGENT_REWORK_FULL_SUITE). Without it the rework
                    # keeps the oracle-green bar and re-fails CI on the same
                    # assertion every round (the agent's own broken test is
                    # invisible to the acceptance-scoped oracle/reviewer).
                    story["ci_rework"] = True
                    story["review_feedback"] = (
                        "The merge-gate CI check failed on your submitted branch "
                        f"(reviewer already APPROVEd this work):\n{gate_error}\n\n"
                        "This is often caused by a test file YOU wrote containing "
                        "an incorrect assertion, not the implementation. Re-examine "
                        "your own test files against the spec, fix any incorrect "
                        "assertions, and ensure the full suite passes before "
                        "resubmitting."
                    )
                    story["status"] = "changes_requested"
                    _notify_user(plan_name, f"{key} merge-gate CI failed ({gate_error}); "
                                            f"routed to rework ({attempts}/{MERGE_MAX_ATTEMPTS}).")
                    summary["notify"].append(key)
                    continue

                attempts = story.get("merge_attempts", 0) + 1
                story["merge_attempts"] = attempts
                if attempts >= MERGE_MAX_ATTEMPTS:
                    story["status"] = "failed"
                    story["merge_error"] = gate_error
                    _notify_user(plan_name, f"{key} merge gate failed {attempts}x "
                                            f"({gate_error}); giving up - needs human intervention.")
                    summary["failed"].append(key)
                else:
                    # leave pr_open; the next tick retries within budget.
                    _notify_user(plan_name, f"{key} merge gate attempt {attempts}/"
                                            f"{MERGE_MAX_ATTEMPTS} failed ({gate_error}); will retry.")
                summary["notify"].append(key)
                continue

            try:
                _merge_pr(story.get("worktree", ""), key)
            except Exception as e:  # gh/git transient failure - see MERGE_MAX_ATTEMPTS
                attempts = story.get("merge_attempts", 0) + 1
                story["merge_attempts"] = attempts
                if attempts >= MERGE_MAX_ATTEMPTS:
                    story["status"] = "failed"
                    story["merge_error"] = str(e)
                    _notify_user(plan_name, f"{key} merge failed {attempts}x "
                                            f"({e}); giving up - needs human intervention.")
                    summary["failed"].append(key)
                else:
                    # leave pr_open; the next tick retries within budget.
                    _notify_user(plan_name, f"{key} merge attempt {attempts}/"
                                            f"{MERGE_MAX_ATTEMPTS} failed ({e}); will retry.")
                summary["notify"].append(key)
                continue
            story["status"] = "done"
            story.pop("merge_attempts", None)
            story.pop("parked_reason", None)
            story.pop("ci_rerun_attempted", None)
            story.pop("ci_rework", None)  # L1: clear the rework flag on done
            _mark_plane_done(key, plan_name)
            summary["merged"].append(key)
        _atomic_write_json(manifest_path, manifest)

    return {"ok": True, **summary}


@mcp.tool()
def approve_merge(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Manually merge a story a human has approved out-of-band, typically one
    "parked" by the risk gate (medium/high risk always parks regardless of
    autonomy level - this is the human's explicit override for that gate,
    not a way to bypass review). Also works on a still-"pr_open" story, for
    approving before the gate has even adjudicated it.

    Refuses unless the story already carries an APPROVE review verdict, and
    refuses any status other than "parked"/"pr_open" - this merges reviewed
    work, it does not re-review or fast-track anything.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"

    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {"ok": False,
                    "error": "plan busy (scheduler tick in progress); retry",
                    "retriable": True}
        # Re-read the manifest from disk INSIDE the lock so we merge against
        # the freshest on-disk state, not a pre-lock stale copy. A scheduler
        # tick may have changed the story's status or verdict while we waited
        # to acquire the lock.
        manifest = json.loads(manifest_path.read_text())
        story = manifest["stories"].get(story_key)
        if not story:
            return {"ok": False, "error": f"No such story {story_key}"}
        if story["status"] not in ("parked", "pr_open"):
            return {"ok": False, "error": f"Story is {story['status']}, not parked/pr_open"}
        if story.get("review_verdict") != "APPROVE":
            return {"ok": False, "error": "Story was never reviewer-approved"}

        try:
            with _scoped_repo_root(plan_name):
                # Mode 9 gate applies here too: even an explicit human merge must
                # not land a conflicting or CI-red PR. Disable via
                # PIPELINE_MERGE_CI_GATE=0 only if you intentionally accept that.
                branch = f"agent/{story_key.lower()}"
                worktree = story.get("worktree", "")
                rb = _rebase_onto_master(worktree, branch)
                if rb.get("auto_resolved"):
                    _notify_user(plan_name, f"{story_key} rebase auto-resolved an "
                                            f"additive-import conflict against "
                                            f"origin/{_default_branch()}.")
                if not rb["ok"]:
                    return {"ok": False, "error": f"rebase failed: {rb['error']}",
                            "story_key": story_key}
                if Path(worktree).is_dir():
                    push = subprocess.run(["git", "push", "--force-with-lease", "origin",
                                           branch], cwd=REPO_ROOT,
                                          capture_output=True, text=True)
                    if push.returncode != 0:
                        return {"ok": False,
                                "error": f"push failed: {(push.stderr or push.stdout).strip()[:200]}",
                                "story_key": story_key}
                ci = _ci_status(branch)
                if ci["state"] == "cancelled" and not story.get("ci_rerun_attempted"):
                    # Same one-shot auto-rerun as the scheduler's merge gate:
                    # a queue-delay cancellation carries no code-quality
                    # signal, so give it one automatic retry before failing.
                    story["ci_rerun_attempted"] = True
                    _ci_rerun(branch)
                    ci = _ci_status(branch)
                if ci["state"] in ("fail", "cancelled"):
                    return {"ok": False, "error": f"CI failing: {ci['error']}",
                            "story_key": story_key}
                if ci["state"] == "pending":
                    return {"ok": False, "error": f"CI still pending: {ci['error']}",
                            "story_key": story_key}
                acc = _reverify_acceptance(story, worktree)
                if acc["state"] == "fail":
                    return {"ok": False, "error": f"acceptance reverify fail: {acc['error']}",
                            "story_key": story_key}
                build = _reverify_build(worktree)
                if build["state"] == "fail":
                    return {"ok": False, "error": f"build reverify fail: {build['error']}",
                            "story_key": story_key}
                _merge_pr(story.get("worktree", ""), story_key)
        except Exception as e:  # surface the gh/git failure to the human, don't raise
            return {"ok": False, "error": str(e), "story_key": story_key}
        # Final write INSIDE the lock, using the manifest re-read inside the
        # lock (not a pre-lock copy). Clear parked_reason on leaving 'parked'.
        story["status"] = "done"
        story.pop("parked_reason", None)
        story.pop("ci_rerun_attempted", None)
        _atomic_write_json(manifest_path, manifest)
    _mark_plane_done(story_key, plan_name)
    return {"ok": True, "story_key": story_key, "status": "done"}


def _set_plan_paused(plan_name: str, paused: bool) -> dict[str, Any]:
    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "an advance_pipeline tick is already running for this plan",
            }
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
        if not manifest_path.exists():
            return {"ok": False, "error": f"No manifest for {plan_name}"}
        manifest = json.loads(manifest_path.read_text())
        manifest["paused"] = paused
        _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "plan_name": plan_name, "paused": paused}


@mcp.tool()
def pause_plan(plan_name: str) -> dict[str, Any]:
    """
    Stop advance_pipeline/advance_all_plans from touching this one plan -
    no new dispatch, review, or merge - while leaving every other ingested
    plan's scheduler ticks unaffected. Any story currently in_progress is
    interrupted (checkpointed and left resumable) so a paused plan isn't
    quietly burning usage in the background. Resume with resume_plan.
    """
    _validate_key(plan_name)
    return _set_plan_paused(plan_name, True)


@mcp.tool()
def resume_plan(plan_name: str) -> dict[str, Any]:
    """Clear a pause set by pause_plan so this plan's stories are eligible
    for dispatch/review/merge on the next advance_pipeline tick again."""
    _validate_key(plan_name)
    return _set_plan_paused(plan_name, False)


@mcp.tool()
def advance_all_plans() -> dict[str, Any]:
    """
    Run advance_pipeline on every plan that has been ingested (has a
    manifest), keyed by plan name. Plans saved but not yet ingested (no
    manifest) are skipped. Intended for a recurring scheduler (cron/launchd
    or /loop) so newly ingested plans are picked up automatically with no
    hardcoded plan name to maintain.

    NOTE on zombie reaping: the per-plan advance_pipeline polling phase
    already handles dead-pid in_progress stories via check_story_status
    (which falls through to test-running on dead pids). Running an external
    reap pass BEFORE the polling would clobber that and silently leave
    stories re-dispatching forever without ever running the test
    (manifest observation 2026-06-28: 3 e2e stories hit dispatch_attempts=
    MISSING because the reap ate the polling opportunity). The reap helper
    _reap_zombie_in_progress_stories is kept for callers that need a
    one-shot cleanup (e.g. tests, ops CLI) but is NOT wired in here.
    """
    plans = {}
    for manifest_path in sorted(PLAN_DIR.glob("*.manifest.json")):
        plan_name = manifest_path.name.removesuffix(".manifest.json")
        try:
            plans[plan_name] = advance_pipeline(plan_name)
        except Exception as e:
            # One plan's failure (bad repo_root, missing tool, transient git
            # error, ...) must not stop every other plan from getting its tick.
            plans[plan_name] = {"ok": False, "error": str(e)}
    return {"ok": True, "plans": plans}


if __name__ == "__main__":
    mcp.run()
