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
import os
import re
import signal
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

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

# Cap on agents dispatched and running at once, across all plans in this
# session. The usage gate above reacts to a polled /cost snapshot, which lags
# real spend — dispatching every ready story in one tick can let that many
# agents collectively burn through the window before the next poll trips the
# pause. Capping concurrency bounds how much can be spent between polls.
# <=0 disables the cap (dispatch every ready story each tick).
MAX_CONCURRENT_AGENTS = int(os.environ.get("PIPELINE_MAX_CONCURRENT_AGENTS", "3"))

PLAN_DIR.mkdir(parents=True, exist_ok=True)
WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("pipeline")

# ---------- Helpers ----------
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
        return ["pytest"]
    if (cwd / "Cargo.toml").exists():
        return ["cargo", "test"]
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
) -> list[str]:
    """Build the headless `claude` argv for a story, applying its persona and model.

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
            f"{checkpoint_instruction}"
            f"When finished, commit your work, push the branch, and exit."
        )
    else:
        prompt = (
            f"You are completing issue {story_key}: {story['summary']}\n\n"
            f"{story.get('agent_instructions', '')}\n\n"
            f"{checkpoint_instruction}"
            f"When finished, commit your work, push the branch, and exit."
        )
    model = (
        story.get("model")
        or (_persona_default_model(persona) if persona else None)
        or DEFAULT_MODEL
    )
    cmd = ["claude", "-p", prompt, "--model", model]
    if persona:
        cmd += ["--append-system-prompt", _persona_body(persona)]
    cmd += ["--allowedTools", _allowed_tools_for(persona)]
    return cmd


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

    External boundary: spawns the `claude` CLI. Tests mock this function.
    """
    model = _persona_default_model("overlord") or "opus"
    system = _persona_body("overlord")
    proc = subprocess.run(
        ["claude", "-p", prompt,
         "--model", model,
         "--append-system-prompt", system,
         "--allowedTools", "Read"],
        capture_output=True, text=True,
    )
    return proc.stdout


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
def _run_reviewer(worktree: str, branch: str) -> str:
    """Run the code-reviewer persona over a branch and return its raw output.

    External boundary: spawns the `claude` CLI. Tests mock this function.
    """
    body = _persona_body("code-reviewer")
    model = _persona_default_model("code-reviewer") or DEFAULT_MODEL
    prompt = (
        f"Review the changes on branch {branch} in this worktree against our "
        f"standards. Run the test suite. End with your VERDICT line; if you "
        f"APPROVE, also include a PR title and body."
    )
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", model,
         "--append-system-prompt", body, "--allowedTools", "Bash,Read"],
        cwd=worktree, capture_output=True, text=True,
    )
    return proc.stdout


def _parse_verdict(text: str) -> str:
    m = re.search(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)", text, re.IGNORECASE)
    return m.group(1).upper() if m else "UNKNOWN"


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
    path.write_text(json.dumps(log, indent=2))


# ---------- Checkpoint journal ----------
def _journal_path(plan_name: str, story_key: str) -> Path:
    return PLAN_DIR / f"{plan_name}.{story_key}.journal.json"


def _append_journal(plan_name: str, story_key: str, record: dict[str, Any]) -> None:
    path = _journal_path(plan_name, story_key)
    log = json.loads(path.read_text()) if path.exists() else []
    log.append(record)
    path.write_text(json.dumps(log, indent=2))


def _read_journal(plan_name: str, story_key: str) -> list[dict[str, Any]]:
    path = _journal_path(plan_name, story_key)
    return json.loads(path.read_text()) if path.exists() else []


# ---------- Usage probe ----------
_SESSION_USAGE_RE = re.compile(r"Current session:\s*(\d+)%\s*used\s*·\s*resets\s*(.+)")
_WEEK_USAGE_RE = re.compile(r"Current week \(all models\):\s*(\d+)%\s*used\s*·\s*resets\s*(.+)")


def _parse_usage_output(text: str) -> dict[str, Any]:
    """Parse the text result of a headless `claude -p "/usage"` call.

    This is human-readable CLI output, not a documented API contract — a
    future Claude Code release could reword it. Raises ValueError if the
    expected lines aren't found, so callers can log and skip a tick rather
    than act on bad data.
    """
    session_m = _SESSION_USAGE_RE.search(text)
    week_m = _WEEK_USAGE_RE.search(text)
    if not session_m or not week_m:
        raise ValueError(f"Could not parse usage output: {text!r}")
    return {
        "session_pct": int(session_m.group(1)),
        "session_reset": session_m.group(2).strip(),
        "week_pct": int(week_m.group(1)),
        "week_reset": week_m.group(2).strip(),
    }


def _run_usage_probe() -> dict[str, Any]:
    """Check current subscription usage via a headless `/cost` call.

    External boundary: spawns the `claude` CLI. Tests mock subprocess.run.
    `/cost` is answered from local session data without invoking the model,
    so this is fast and free to poll frequently. (The percentage summary
    used to be on `/usage`, but that command dropped it in favor of a
    "what's contributing to your usage" breakdown; `/cost` still has it.)
    """
    proc = subprocess.run(
        ["claude", "-p", "/cost", "--output-format", "json"],
        capture_output=True, text=True, check=True,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Usage probe returned invalid JSON: {e}") from e
    usage = _parse_usage_output(payload.get("result", ""))
    usage["checked_at"] = datetime.now(timezone.utc).isoformat()
    return usage


def _write_usage_state(state: dict[str, Any]) -> None:
    USAGE_STATE_PATH.write_text(json.dumps(state, indent=2))


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


def _mark_plane_done(story_key: str) -> None:
    """Best-effort transition of a Plane issue to Done.

    Mirrors dispatch_story's in-progress transition: swallows errors rather
    than raising, since not every plan is Plane-backed and a Plane outage
    must not block a local merge that has already happened in git.
    """
    try:
        issue_uuid = _resolve_issue_uuid(story_key)
        plane_request("PATCH", f"/projects/{PLANE_PROJECT}/work-items/{issue_uuid}/",
                      json={"state": _get_state("completed")})
    except Exception as e:
        print(f"Warning: could not transition {story_key} to done: {e}")


def _commit_wip(worktree: str, story_key: str, step: str) -> str:
    """Commit any uncommitted work in the worktree as a WIP checkpoint.

    External boundary: spawns `git`. Tests mock subprocess.run. If there is
    nothing to commit (the agent already committed its own work), this is
    not an error — the existing HEAD sha is returned so the journal still
    records a checkpoint marker.

    Excludes agent.log: it's the dispatcher's own session-narration file
    written into the worktree root, not project code, and must never be
    swept into a commit.
    """
    subprocess.run(["git", "add", "-A", "--", ".", ":!agent.log"], cwd=worktree,
                    check=True, capture_output=True, text=True)
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


# ---------- Tools ----------
@mcp.tool()
def save_plan(plan_name: str, plan_json: str) -> dict[str, Any]:
    """
    Save a generated project plan to disk. Plan should be JSON matching the
    schema: { "epics": [ { "summary", "stories": [...] } ] }.
    Call this after generating a plan so the user can review before ingestion.
    """
    try:
        plan = json.loads(plan_json)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Invalid JSON: {e}"}

    if "epics" not in plan:
        return {"ok": False, "error": "Plan must contain 'epics' key"}

    path = PLAN_DIR / f"{plan_name}.json"
    path.write_text(json.dumps(plan, indent=2))

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


@mcp.tool()
def ingest_plan(plan_name: str, only_epics: list[str] | None = None) -> dict[str, Any]:
    """
    Push a saved plan into Plane. Creates epics first, then issues linked
    to their parent epic. Optionally restrict to specific epic summaries via
    only_epics. Returns a manifest mapping local IDs to Plane UUIDs.
    """
    path = PLAN_DIR / f"{plan_name}.json"
    if not path.exists():
        return {"ok": False, "error": f"No plan named {plan_name}"}

    plan = json.loads(path.read_text())
    manifest = {"epics": {}, "stories": {}, "repo_root": plan.get("repo_root")}
    label_id = _get_or_create_label("agent-pipeline")
    backlog_state = _get_state("backlog")

    # Maps the plan's local story keys (e.g. "S1") to the Plane issue UUIDs
    # generated below, so dependencies can be translated to manifest keys.
    key_to_issue_id: dict[str, str] = {}

    for epic in plan["epics"]:
        if only_epics and epic["summary"] not in only_epics:
            continue

        # Epics are an optional Plane module; some instances/API versions do
        # not expose the /epics/ endpoint. Fall back to ungrouped issues.
        try:
            epic_resp = plane_request("POST", f"/projects/{PLANE_PROJECT}/epics/",
                                      json={"name": epic["summary"]})
            epic_id = epic_resp["id"]
            manifest["epics"][epic["summary"]] = epic_id
        except RuntimeError:
            epic_id = None

        for story in epic.get("stories", []):
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
            if "key" in story:
                key_to_issue_id[story["key"]] = issue_id
            manifest["stories"][issue_id] = {
                "summary": story["summary"],
                "agent_instructions": story.get("agent_instructions", ""),
                "dependencies": story.get("dependencies", []),
                "persona": story.get("persona"),
                "model": story.get("model"),
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

    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    return {"ok": True, "manifest_path": str(manifest_path), **manifest}


@mcp.tool()
def list_ready_stories(plan_name: str) -> list[dict]:
    """
    Return stories whose dependencies are satisfied and that are still in
    To Do. Use this to decide what to dispatch next.
    """
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    if not manifest_path.exists():
        return []

    manifest = json.loads(manifest_path.read_text())
    stories = manifest["stories"]
    done_keys = {k for k, v in stories.items() if v["status"] == "done"}

    ready = []
    for key, story in stories.items():
        if story["status"] != "todo":
            continue
        deps_met = all(dep in done_keys for dep in story["dependencies"])
        if deps_met:
            ready.append({"key": key, "summary": story["summary"]})
    return ready


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
    """
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    story = manifest["stories"].get(story_key)
    if not story:
        return {"ok": False, "error": f"No such story {story_key}"}

    branch = f"agent/{story_key.lower()}"
    worktree_path = WORKTREE_ROOT / story_key
    resuming = story.get("status") == "interrupted" or worktree_path.exists()
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

    try:
        issue_uuid = _resolve_issue_uuid(story_key)
        plane_request("PATCH", f"/projects/{PLANE_PROJECT}/work-items/{issue_uuid}/",
                      json={"state": _get_state("started")})
    except Exception as e:
        print(f"Warning: could not transition {story_key} to in-progress: {e}")

    cmd = _build_dispatch_command(story, story_key, plan_name=plan_name, resume_journal=journal or None)
    worktree_path.mkdir(parents=True, exist_ok=True)
    log_path = worktree_path / "agent.log"
    log_file = open(log_path, "a" if resuming else "w")
    proc = subprocess.Popen(
        cmd,
        cwd=worktree_path,
        stdout=log_file,
        stderr=log_file,
    )
    log_file.close()

    story["status"] = "in_progress"
    story["pid"] = proc.pid
    story["worktree"] = str(worktree_path)
    story["log"] = str(log_path)
    manifest_path.write_text(json.dumps(manifest, indent=2))

    return {"ok": True, "story_key": story_key, "pid": proc.pid, "branch": branch,
            "resumed": resuming}


@mcp.tool()
def check_story_status(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Check whether a dispatched agent has finished. If complete, runs tests
    in the worktree and reports pass/fail without auto-merging.
    """
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
        # The agent process exited without ever writing a byte of output -
        # a failed launch, not a real attempt. Running tests against the
        # untouched worktree would just record a misleading "failed" for
        # work that was never tried, and unlike "failed", nothing retries
        # it automatically. "interrupted" is dispatch-eligible like "todo".
        story["status"] = "interrupted"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        return {"status": "interrupted", "pid": pid}

    test_dir, test_cmd = detect_test_command(worktree)
    test_result = subprocess.run(
        test_cmd, cwd=test_dir, capture_output=True, text=True,
    )
    passed = test_result.returncode == 0

    story["status"] = "tests_passed" if passed else "failed"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    return {
        "status": story["status"],
        "tests_passed": passed,
        "test_command": test_cmd,
        "output_tail": test_result.stdout[-500:],
    }


@mcp.tool()
def interrupt_story(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Stop a dispatched agent and leave its story resumable.

    Sends SIGTERM to the agent's process (a no-op if it has already exited),
    commits any uncommitted work in its worktree as a checkpoint, and marks
    the story "interrupted" rather than "failed" so a later dispatch_story
    call resumes it instead of starting over. The worktree and branch are
    left in place.
    """
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
    manifest_path.write_text(json.dumps(manifest, indent=2))

    return {"ok": True, "status": "interrupted", "commit": sha}


@mcp.tool()
def mark_story_in_progress(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Transition a Plane issue to In Progress and update the local manifest.
    Use this before writing any code for a story.
    """
    issue_uuid = _resolve_issue_uuid(story_key)
    plane_request("PATCH", f"/projects/{PLANE_PROJECT}/work-items/{issue_uuid}/",
                  json={"state": _get_state("started")})

    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if story_key not in manifest["stories"]:
        return {"ok": False, "error": f"No such story {story_key}"}
    manifest["stories"][story_key]["status"] = "in_progress"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {"ok": True}


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
def mark_story_done(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Transition a Plane issue to Done and update the local manifest.
    Use after you've reviewed and merged the agent's PR.
    """
    issue_uuid = _resolve_issue_uuid(story_key)
    plane_request("PATCH", f"/projects/{PLANE_PROJECT}/work-items/{issue_uuid}/",
                  json={"state": _get_state("completed")})

    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stories"][story_key]["status"] = "done"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {"ok": True}


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
        state = dict(prev)
        state["checked_at"] = datetime.now(timezone.utc).isoformat()
        measured_at = prev.get("measured_at", prev.get("checked_at"))
        state["measured_at"] = measured_at
        age = _usage_state_age_seconds({"checked_at": measured_at}) if measured_at else None
        if age is not None and age > USAGE_STALE_AFTER_SECONDS:
            state["paused"] = False
            state["stale"] = True
            print(
                f"check_usage: usage data is {age:.0f}s stale and the CLI is "
                f"still not parseable - failing the gate open", file=sys.stderr,
            )
        _write_usage_state(state)
        return state
    state["measured_at"] = state["checked_at"]
    state["paused"] = _usage_gate(
        prev.get("paused", False), state["session_pct"], state["week_pct"],
    )
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
    path = _decisions_path(plan_name)
    return json.loads(path.read_text()) if path.exists() else []


@mcp.tool()
def review_story(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Run the code-reviewer persona over a dispatched story's branch. On APPROVE,
    open a PR via gh and set status to pr_open; otherwise set status to
    changes_requested. Does not merge — merge is the overlord's decision.
    """
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    story = manifest["stories"].get(story_key)
    if not story:
        return {"ok": False, "error": f"No such story {story_key}"}

    branch = f"agent/{story_key.lower()}"
    verdict = _parse_verdict(_run_reviewer(story.get("worktree", ""), branch))
    story["review_verdict"] = verdict

    if verdict == "APPROVE":
        pr_url = _open_pr(story.get("worktree", ""), story_key, story)
        story["pr_url"] = pr_url
        story["status"] = "pr_open"
    else:
        story["status"] = "changes_requested"

    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {
        "ok": True,
        "verdict": verdict,
        "status": story["status"],
        "pr_url": story.get("pr_url"),
    }


@contextmanager
def _plan_lock(plan_name: str):
    """Exclusive, non-blocking lock scoped to one plan's advance_pipeline tick.

    Overlapping invocations (e.g. launchd firing a burst of missed
    StartIntervals after the machine wakes from sleep) would otherwise both
    read the same "todo"/"interrupted" story before either has written its
    in_progress status back to the manifest, and both dispatch it - the
    second dispatch_story call sees the first one's half-built worktree via
    worktree_path.exists(), treats itself as "resuming", and spawns its own
    agent into the *same* directory as the first. Multiple agents fighting
    over one worktree's git state is what actually produced the repeated
    zero-output agent deaths this guards against, not per-story flakiness.

    Yields whether the lock was acquired; the caller must check it and skip
    all work if not - this never blocks waiting for the lock.
    """
    lock_path = PLAN_DIR / f"{plan_name}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        try:
            yield acquired
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@mcp.tool()
def advance_pipeline(plan_name: str) -> dict[str, Any]:
    """
    Run one orchestration tick: dispatch every ready story (deps satisfied),
    advance finished stories through test -> review -> PR, and adjudicate merges
    against the risk threshold. Idempotent; designed to be called repeatedly by
    a scheduler (/loop or cron). In PIPELINE_AUTONOMY=dry-run it plans and logs
    only, taking no actions.

    Honors the usage gate (see check_usage): while paused, in-progress stories
    are interrupted (checkpointed and made resumable) and no new dispatch or
    review is started, since both spend usage. Merge adjudication still runs,
    since it costs no model usage. "interrupted" stories are dispatch-eligible
    like "todo" ones, so they resume automatically once usage allows.

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
    stories = json.loads(manifest_path.read_text())["stories"]

    paused = _read_usage_state().get("paused", False)

    done_keys = {k for k, v in stories.items() if v["status"] == "done"}
    ready = [
        k for k, v in stories.items()
        if v["status"] in ("todo", "interrupted")
        and all(d in done_keys for d in v.get("dependencies", []))
    ]

    if PIPELINE_AUTONOMY == "dry-run":
        return {
            "ok": True,
            "dry_run": True,
            "autonomy": PIPELINE_AUTONOMY,
            "paused": paused,
            "would_dispatch": ready,
            "would_merge_decisions": {
                k: _merge_decision(v)
                for k, v in stories.items() if v["status"] == "pr_open"
            },
        }

    summary: dict[str, Any] = {
        "autonomy": PIPELINE_AUTONOMY,
        "paused": paused,
        "dispatched": [], "advanced": [], "merged": [],
        "parked": [], "failed": [], "interrupted": [], "notify": [],
    }

    # Scoped for the whole tick: dispatch_story resolves its own repo_root
    # too (so it's correct called standalone), but _merge_pr and
    # _default_branch read the plain REPO_ROOT global, so this plan's repo
    # must be active for the duration of every action below.
    with _scoped_repo_root(plan_name):
        if paused:
            # Usage gate tripped: stop spending more, and free up in-flight
            # agents (resumable via their checkpoint journal) rather than
            # letting them keep burning the quota we're trying to protect.
            for key, story in stories.items():
                if story["status"] == "in_progress" and "pid" in story:
                    interrupt_story(plan_name, key)
                    summary["interrupted"].append(key)
            _notify_user(plan_name, "Usage gate paused: deferring dispatch/review.")
            summary["notify"].append("paused")
        else:
            # 1. Dispatch ready (and resumable-interrupted) stories, capped to
            # the slots still free under MAX_CONCURRENT_AGENTS. <=0 means no cap.
            if MAX_CONCURRENT_AGENTS > 0:
                slots = max(0, MAX_CONCURRENT_AGENTS - _count_in_progress_agents())
                to_dispatch = ready[:slots]
            else:
                to_dispatch = ready
            for key in to_dispatch:
                dispatch_story(plan_name, key)
                summary["dispatched"].append(key)

            # 2. Poll running agents: tests fail -> notify; tests pass -> tests_passed.
            stories = json.loads(manifest_path.read_text())["stories"]
            for key, story in stories.items():
                if story["status"] == "in_progress" and "pid" in story:
                    status = check_story_status(plan_name, key).get("status")
                    if status == "failed":
                        _notify_user(plan_name, f"{key} tests failed")
                        summary["failed"].append(key)
                        summary["notify"].append(key)

            # Review every tests_passed story, including ones orphaned by a
            # review that crashed (e.g. gh failure) on a prior tick —
            # review_story is idempotent, so retrying is safe.
            stories = json.loads(manifest_path.read_text())["stories"]
            for key, story in stories.items():
                if story["status"] == "tests_passed":
                    rv = review_story(plan_name, key)
                    summary["advanced"].append({key: rv["status"]})

        # 3. Adjudicate merges for reviewed PRs (no model usage; runs even paused).
        manifest = json.loads(manifest_path.read_text())
        stories = manifest["stories"]
        for key, story in stories.items():
            if story["status"] != "pr_open":
                continue
            decision = _merge_decision(story)
            if decision["action"] == "merge":
                _merge_pr(story.get("worktree", ""), key)
                story["status"] = "done"
                _mark_plane_done(key)
                summary["merged"].append(key)
            else:
                story["status"] = "parked"
                _notify_user(plan_name, f"{key} parked: {decision['reason']}")
                summary["parked"].append(key)
                summary["notify"].append(key)
        manifest_path.write_text(json.dumps(manifest, indent=2))

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
    manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    story = manifest["stories"].get(story_key)
    if not story:
        return {"ok": False, "error": f"No such story {story_key}"}
    if story["status"] not in ("parked", "pr_open"):
        return {"ok": False, "error": f"Story is {story['status']}, not parked/pr_open"}
    if story.get("review_verdict") != "APPROVE":
        return {"ok": False, "error": "Story was never reviewer-approved"}

    with _scoped_repo_root(plan_name):
        _merge_pr(story.get("worktree", ""), story_key)
    story["status"] = "done"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    _mark_plane_done(story_key)
    return {"ok": True, "story_key": story_key, "status": "done"}


@mcp.tool()
def advance_all_plans() -> dict[str, Any]:
    """
    Run advance_pipeline on every plan that has been ingested (has a
    manifest), keyed by plan name. Plans saved but not yet ingested (no
    manifest) are skipped. Intended for a recurring scheduler (cron/launchd
    or /loop) so newly ingested plans are picked up automatically with no
    hardcoded plan name to maintain.
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
