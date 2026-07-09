"""
Pipeline MCP Server
Exposes tools for: planning, Plane ingestion, agent dispatch, status monitoring.

Run with: python pipeline_mcp_server.py
Register globally: claude mcp add -s user pipeline ~/.claude/mcp-servers/pipeline/.venv/bin/python3 ~/.claude/mcp-servers/pipeline/pipeline_mcp_server.py

Required env vars (set in ~/.zshrc or ~/.zprofile):
  PLANE_BASE       e.g. https://plane.yourcompany.com
  PLANE_API_KEY    Plane personal access token (never commit this)
  PLANE_WORKSPACE  workspace slug, e.g. my-team
  PLANE_PROJECT    project UUID from Plane settings

Per-project overrides (set in project .mcp.json env block):
  REPO_ROOT    absolute path to the git repo being worked on
  PLAN_DIR     override plan storage location (default: ~/.claude/plans)
  WORKTREE_ROOT override worktree location (default: ~/.claude/worktrees)
  PIPELINE_MAX_CONCURRENT_AGENTS  cap on agents dispatched/running at once
    across all plans in this session (default: 3; <=0 disables the cap)
"""

import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

import backend

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
STEP_CAP_FALLBACK_THRESHOLD = int(
    os.environ.get("PIPELINE_STEP_CAP_FALLBACK_THRESHOLD", "3"))

# Layered local-first dispatch (PIPELINE_BACKEND_DISPATCH=auto):
#   1. A-priori: stories with risk above PIPELINE_LOCAL_MAX_RISK (default "low")
#      or a security persona go straight to Claude.
#   2. A-posteriori: if the local agent fails (bad code / test failure), the
#      orchestrator escalates that specific story to Claude and starts clean.
# Explicit "local" or "claude" values bypass this router entirely.
PIPELINE_LOCAL_MAX_RISK = os.environ.get("PIPELINE_LOCAL_MAX_RISK", "low").lower()
_LOCAL_SKIP_PERSONAS = {"security-engineer"}

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
    return AGENTS_DIR / f"{persona}.md"


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


def _invoke_overlord(prompt: str) -> str:
    """Run the overlord persona headless and return its raw stdout.

    External boundary: delegates to the configured Backend. Tests mock this
    function.
    """
    model = _persona_default_model("overlord") or "opus"
    system = _persona_body("overlord")
    return backend.get_backend("overlord").complete(
        prompt, system=system, model=model, allowed_tools="Read",
    )


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
def _run_reviewer(worktree: str, branch: str, backend_name: str | None = None) -> str:
    """Run the code-reviewer persona over a branch and return its raw output.

    External boundary: delegates to the configured Backend. Tests mock this
    function. backend_name lets a caller override the env-resolved default
    (e.g. review_story's rate-limit fallback routing to "local");
    get_backend already treats name=None as "use the env-resolved default".
    """
    body = _persona_body("code-reviewer")
    model = _persona_default_model("code-reviewer") or DEFAULT_MODEL
    # Asymmetric review: software-engineer.md and code-reviewer.md both
    # declare `model: sonnet`, so without an override dispatch and review
    # resolve to the identical concrete local model - a model reviewing its
    # own work with identical weights. When the review backend is actually
    # local, an explicit PIPELINE_LOCAL_REVIEW_MODEL overrides the tier so
    # review can run on a different (e.g. stronger) local model. Gated on
    # backend == "local" so a bare Ollama tag never leaks into a cloud
    # review as a bogus --model value. backend_name may already be the
    # explicit "local" (review_story's FM-B rate-limit fallback); otherwise
    # fall back to the env-resolved default, mirroring how get_backend
    # itself treats name=None.
    resolved_backend = (
        backend_name or os.environ.get("PIPELINE_BACKEND_REVIEW", "claude")
    ).strip().lower()
    if resolved_backend == "local":
        review_model_override = os.environ.get("PIPELINE_LOCAL_REVIEW_MODEL")
        if review_model_override:
            model = review_model_override
    prompt = (
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
    return backend.get_backend("review", name=backend_name).complete(
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
    return backend.get_backend("review").complete(
        prompt, system=body, model=model, allowed_tools="Bash,Read", cwd=worktree,
        max_tokens=int(os.environ.get("PIPELINE_SECURITY_REVIEW_MAX_TOKENS",
                                      os.environ.get("PIPELINE_REVIEW_MAX_TOKENS", "4096"))),
        cell_dir=cell_dir,
    )


def _parse_verdict(text: str) -> str:
    m = re.search(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)", text, re.IGNORECASE)
    return m.group(1).upper() if m else "UNKNOWN"


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


def _rebase_onto_master(worktree: str, branch: str) -> dict[str, Any]:
    """Rebase `branch` onto current origin/master inside its worktree so the
    merge gate sees the branch against current master, not the stale base the
    agent branched from. Fetches origin/master first (from REPO_ROOT, the shared
    repo) so the rebase target is current.

    Returns ``{"ok": bool, "conflict": bool, "error": str}``:
      - ok=True            rebase succeeded; the branch is on top of origin/master.
      - ok=False, conflict=True  rebase hit a merge conflict; the rebase was
        aborted so the worktree is back to its pre-rebase state and the caller
        can park/re-dispatch for resolution instead of force-anything.
      - ok=False, conflict=False some other git failure (dirty tree, missing
        ref); rebase aborted if one was in progress.
    """
    def _run(argv: list[str], cwd) -> subprocess.CompletedProcess:
        # `git` may be absent or non-executable (e.g. a minimal container).
        # Catch OSError so this helper honors its never-raises contract and
        # reports a non-conflict failure instead of crashing the tick.
        try:
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
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
    or the timeout elapses. Returns ``{"state": "pass"|"fail"|"pending"|"none",
    "error": str}``.

      - ``pass``   every check passed -> safe to merge.
      - ``fail``   at least one check failed/errored/cancelled -> do not merge.
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
        if buckets & {"fail", "error", "cancelled", "action_required"}:
            return {"state": "fail", "error": ""}
        if buckets <= {"pass"}:
            return {"state": "pass", "error": ""}
        time.sleep(10)  # still pending — keep polling
    return {"state": "pending", "error": "CI did not complete within timeout"}


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
    # an acceptance block AND the runner is pytest (the only runner where
    # `pytest <files>` is well-defined); otherwise the full suite. The
    # full-suite path is the MBW safety net — a story without an acceptance
    # block (the common case for real-project stories) still gets the
    # rebased branch's full test suite re-run before merge.
    scope_to_acceptance = bool(acceptance) and _is_pytest_cmd(test_cmd)
    if scope_to_acceptance:
        acceptance_paths = [str(Path(worktree) / p) for p in _acceptance_rel_paths(story)]
        test_cmd = test_cmd + acceptance_paths
    elif not (acceptance and _is_pytest_cmd(test_cmd)):
        # No acceptance block, or non-pytest runner: run the full suite
        # unless the operator opted out.
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
    to resume from a failed local run when Claude is starting over.
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
    for key in ("pid", "worktree", "log", "dispatch_attempts", "dispatch_error"):
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
    persona = (story.get("persona") or "").lower()
    if persona in _LOCAL_SKIP_PERSONAS:
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
    """Best-effort transition of a Plane issue to Done.

    Mirrors dispatch_story's in-progress transition: swallows errors rather
    than raising, since not every plan is Plane-backed and a Plane outage
    must not block a local merge that has already happened in git.
    """
    _plane_set_state(story_key, "completed", plan_name)


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
    "acceptance", "risk",
)


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

    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"

    with _plan_lock(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True, "skipped": "locked",
                "reason": "another ingest/dispatch/interrupt is in progress for this plan",
            }

        manifest = {"epics": {}, "stories": {}, "repo_root": repo_root}

        # When Plane isn't configured the manifest is the sole source of truth:
        # skip every Plane call and synthesize story keys locally instead of
        # taking them from Plane-issued UUIDs.
        plane_on = _plane_enabled()
        label_id = _get_or_create_label("agent-pipeline") if plane_on else None
        backlog_state = _get_state("backlog") if plane_on else None

        # Maps the plan's local story keys (e.g. "S1") to the manifest story keys
        # generated below (Plane issue UUIDs, or local keys when Plane is off), so
        # dependencies can be translated to manifest keys.
        key_to_issue_id: dict[str, str] = {}

        for epic in plan["epics"]:
            if only_epics and epic["summary"] not in only_epics:
                continue

            epic_id = None
            if plane_on:
                # Epics are an optional Plane module; some instances/API versions
                # do not expose the /epics/ endpoint. Fall back to ungrouped issues.
                try:
                    epic_resp = plane_request("POST", f"/projects/{PLANE_PROJECT}/epics/",
                                              json={"name": epic["summary"]})
                    epic_id = epic_resp["id"]
                    manifest["epics"][epic["summary"]] = epic_id
                except RuntimeError:
                    epic_id = None

            for story in epic.get("stories", []):
                if plane_on:
                    issue_resp = plane_request("POST", f"/projects/{PLANE_PROJECT}/work-items/", json={
                        "name": story["summary"],
                        "description": story.get("description", ""),
                        "state": backlog_state,
                        "labels": [label_id],
                    })
                    issue_id = issue_resp["id"]
                    if epic_id is not None:
                        plane_request("POST", f"/projects/{PLANE_PROJECT}/epics/{epic_id}/issues/",
                                      json={"issue_id": issue_id})
                else:
                    # No Plane UUID to key on: prefer the plan's own story key
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
_WORKTREE_LOG_EXCLUDES = ("agent.log", "review.log")


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

        _plane_set_state(story_key, "started", plan_name)

        # Resolve concrete backend name for this story. Priority order:
        #   1. story["backend"] already set (e.g. from an escalation flip)
        #   2. PIPELINE_BACKEND_DISPATCH=auto  → a-priori router
        #   3. PIPELINE_BACKEND_DISPATCH=local|claude  → that driver directly
        env_backend = os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude").strip().lower()
        dispatch_backend = story.get("backend") or (
            _route_dispatch_backend(story) if env_backend == "auto" else env_backend
        )
        # Persist so check_story_status and escalation see which backend ran.
        story["backend"] = dispatch_backend

        spec = _build_dispatch_command(
            story, story_key, plan_name=plan_name, resume_journal=journal or None,
            review_feedback=story.get("review_feedback"),
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
        if (dispatch_backend == "local"
                and MAX_CONCURRENT_AGENTS > 1
                and _count_in_progress_agents() > 0):
            target_model = spec.get("model") or story.get("model")
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

        dispatch_kwargs: dict[str, Any] = dict(
            prompt=spec["prompt"], system=spec["system"], model=spec["model"],
            allowed_tools=spec["allowed_tools"],
            cwd=worktree_path, log_path=log_path, append=resuming,
        )
        # Only the local driver accepts/uses `acceptance`; pass it through when
        # we're actually invoking that driver so Claude's signature stays clean.
        if dispatch_backend == "local" and acceptance_paths:
            dispatch_kwargs["acceptance"] = acceptance_paths

        handle = backend.get_backend("dispatch", name=dispatch_backend).dispatch(**dispatch_kwargs)

        story["status"] = "in_progress"
        story["pid"] = handle.pid
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
        _atomic_write_json(manifest_path, manifest)
        return {"status": "interrupted", "pid": pid, "reason": "step_cap_reached"}

    test_dir, test_cmd = detect_test_command(worktree)

    # FM-A: when the story carries an acceptance block, gate on only those
    # oracle test files rather than the full worktree suite. The model's own
    # tests can contain wrong assertions (the "graded on own buggy tests"
    # failure mode); the harness-owned oracle is the authoritative bar.
    # Scoping is only safe for pytest, which accepts path args; other runners
    # fall back to the whole suite (documented limitation — all benchmark tasks
    # and the pipeline's own test stories are pytest).
    #
    # Paths are materialized relative to the worktree root (dispatch_story),
    # but test_dir can be a child subdirectory when the buildable project
    # doesn't live at the worktree root (detect_test_command's fallback).
    # Use absolute paths so the scoped run works regardless of test_dir.
    acceptance = story.get("acceptance") or []
    if acceptance and _is_pytest_cmd(test_cmd):
        acceptance_paths = [str(worktree / p) for p in _acceptance_rel_paths(story)]
        test_cmd = test_cmd + acceptance_paths

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

        try:
            os.kill(story["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass

        sha = _commit_wip(story["worktree"], story_key, "interrupted")
        interrupted_at = datetime.now(timezone.utc).isoformat()
        _append_journal(plan_name, story_key, {
            "step": "interrupted",
            "summary": "Agent process terminated; checkpointed for resume.",
            "next_hint": "",
            "commit": sha,
            "ts": interrupted_at,
        })

        story["status"] = "interrupted"
        story["last_commit"] = sha
        story["interrupted_at"] = interrupted_at
        _atomic_write_json(manifest_path, manifest)

        return {"ok": True, "status": "interrupted", "commit": sha}


@mcp.tool()
def mark_story_in_progress(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Transition a Plane issue to In Progress and update the local manifest.
    Use this before writing any code for a story.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    if _plane_enabled():
        issue_uuid = _resolve_issue_uuid(story_key)
        plane_request("PATCH", f"/projects/{PLANE_PROJECT}/work-items/{issue_uuid}/",
                      json={"state": _get_state("started")})

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
    Transition a Plane issue to Done and update the local manifest.
    Use after you've reviewed and merged the agent's PR.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    if _plane_enabled():
        issue_uuid = _resolve_issue_uuid(story_key)
        plane_request("PATCH", f"/projects/{PLANE_PROJECT}/work-items/{issue_uuid}/",
                      json={"state": _get_state("completed")})

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
    ruling = _parse_ruling(_invoke_overlord(prompt))
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
    try:
        # Once a story is escalated (see _escalate_review_to_claude below),
        # every subsequent review must go to Claude regardless of the global
        # PIPELINE_BACKEND_REVIEW setting - review backend is otherwise
        # resolved purely from that env var with no per-story override, so
        # this is the one seam that needs an explicit check.
        reviewer_output = (
            _run_reviewer(worktree, branch, backend_name="claude")
            if story.get("escalated") else _run_reviewer(worktree, branch)
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
        if fallback_mode == "local" and story["review_deferred_count"] >= fallback_after:
            _notify_user(plan_name, f"{story_key} review falling back to local backend "
                                    f"after {story['review_deferred_count']} rate-limited attempts.")
            reviewer_output = _run_reviewer(worktree, branch, backend_name="local")
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
            _run_reviewer(worktree, branch, backend_name="claude")
            if story.get("escalated") else _run_reviewer(worktree, branch)
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
        story["review_feedback"] = reviewer_output
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
            for key, story in stories.items():
                if story["status"] == "in_progress" and "pid" in story:
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
            rb = _rebase_onto_master(worktree, branch)
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
                    if ci["state"] == "fail":
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
                if ci["state"] == "fail":
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
