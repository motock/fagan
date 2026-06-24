# Autonomous SDLC Agent Pipeline

A persona-driven, semi-autonomous software development pipeline for Claude Code.
It turns a goal into a plan, dispatches headless agents to implement each story
in isolated git worktrees, reviews their work, and merges it — with an
**overlord** that makes decisions on your behalf so you are not in the loop for
every choice.

This document is the reference for the whole system: the personas, the overlord
decision protocol, the pipeline MCP tools, the end-to-end workflow, configuration,
and safety controls.

---

## Components at a glance

| Piece | Location | Role |
|---|---|---|
| Persona subagents | `~/.claude/agents/*.md` | The SDLC roles agents play |
| Decision policy | `~/.claude/overlord-policy.md` | How the overlord decides |
| Pipeline MCP server | `~/.claude/mcp-servers/pipeline/pipeline_mcp_server.py` | All pipeline tools |
| Tests | `~/.claude/mcp-servers/pipeline/test_pipeline_mcp_server.py` | `pytest`, run via the venv |
| Plans / manifests / logs | `~/.claude/plans/` | Plan, manifest, decisions, notifications |
| Worktrees | `~/.claude/worktrees/` | Isolated per-story branches |
| Issue tracker | Plane (external) | Source of truth for stories |

---

## Architecture

```
                ┌─────────────────────────────────────────┐
                │  Orchestrator loop (cron / /loop skill)  │
                │  advance_pipeline(plan)  — one tick      │
                └───────────────┬─────────────────────────┘
        ready stories           │ gates adjudicated by overlord
        (deps satisfied)        ▼
   ┌──────────────┐   dispatch w/ persona+model   ┌────────────────────┐
   │ Plan/Manifest│ ───────────────────────────►  │ Headless story agent│
   │ (JSON, Plane)│                                │ in git worktree     │
   └──────────────┘ ◄───── decision ruling ─────── │ (persona prompt +   │
        ▲                request_decision()         │  model)             │
        │                      │                    └─────────┬──────────┘
        │                      ▼                              │ tests pass
        │            ┌──────────────────┐                     ▼
        │            │   OVERLORD       │            ┌────────────────────┐
        └─ audit ────│  (Opus, policy)  │ ◄──────────│ code-reviewer →     │
           decisions │  decides gates   │  merge gate│ gh pr create        │
           log       └──────────────────┘ ──────────►│ (auto-PR + merge)   │
                                                      └────────────────────┘
```

---

## Personas (`~/.claude/agents/`)

Each persona is a Claude Code subagent: a markdown file with YAML frontmatter
(`name`, `description`, `model`, `memory: user`) and a system-prompt body. The
pipeline reads the body and dispatches a headless agent with it as the role.

| Persona | Default model | Responsibility |
|---|---|---|
| `product-analyst` | opus | Decompose a goal into epics/stories with acceptance criteria, dependencies, and per-story `persona`/`model`/`risk` |
| `solution-architect` | opus | General system design, tech selection, API design (delegates mobile to `mobile-architect`) |
| `software-engineer` | sonnet | Default TDD implementer for non-mobile work |
| `security-engineer` | opus | Threat modeling and security review (OWASP, Secure by Design) |
| `devops-release-engineer` | sonnet | Build/CI, branch & worktree hygiene, releases |
| `code-reviewer` | sonnet | Reviews a branch, emits a `VERDICT`, opens a PR |
| `tech-writer` | haiku | Docs for externally visible changes |
| `overlord` | opus | The decision authority (see below) |

Existing mobile specialists (`mobile-architect`, `mobile-engineer`,
`ux-mobile-principal`, `qa-test-engineer`) are unchanged and used for mobile work.

To change a persona's behavior or default model, edit its `.md` file. The
frontmatter `model:` line is the fallback model when a story does not specify one.

---

## The overlord and the decision policy

The **overlord** (`~/.claude/agents/overlord.md`) rules on the user's behalf when
a story agent is blocked, two personas disagree, or a gate needs adjudication. It
follows `~/.claude/overlord-policy.md` (plus an optional per-repo
`<repo>/.overlord-policy.md` override).

**Decision tiers:**

1. **Routine / reversible** → decide silently (naming, internal structure, a
   library within the approved stack, refactors).
2. **Notify-async** (`risk: medium`) → decide, proceed, flag the user (new
   dependency, schema change, additive API change).
3. **Park-and-ping** (`risk: high`) → do **not** act unattended; hold for human
   review and notify. Anything irreversible, security/auth, money, production
   config, or breaking changes. **Always parked regardless of autonomy level.**

The overlord returns a structured ruling (`RULING` / `TIER` / `RISK` /
`RATIONALE` / `NOTIFY_USER`) that is parsed and written to the plan's decisions
log as an audit record.

---

## MCP tools reference

### Planning
- `save_plan(plan_name, plan_json)` — save a plan JSON to `~/.claude/plans/`.
- `list_plans()` — list saved plans.
- `ingest_plan(plan_name, only_epics=None)` — push a plan into Plane (epics +
  issues), tag with `agent-pipeline`, write `<plan>.manifest.json`. Carries each
  story's `persona`, `model`, and `risk` into the manifest.

### Dispatch & status
- `list_ready_stories(plan_name)` — stories whose dependencies are all `done`.
- `dispatch_story(plan_name, story_key)` — create a worktree on
  `agent/<key>`, spawn a headless `claude -p` agent **with the story's persona
  system prompt, model, and curated tools**, transition the Plane issue to
  In Progress. Returns immediately with the PID and a `resumed` flag.
  If the story is `interrupted` (or its worktree already exists from a prior
  run), it **reuses** the existing worktree/branch instead of recreating them
  and seeds the prompt with the checkpoint journal so the agent continues
  rather than starting over.
- `check_story_status(plan_name, story_key)` — has the agent finished? If so,
  runs the detected test suite and sets `tests_passed`/`failed`. An
  `interrupted` story is reported as-is without running tests against its
  incomplete tree.

### Resumability (checkpoint / interrupt)
- `checkpoint(plan_name, story_key, step, summary, next_hint="")` — commits
  any uncommitted work in the story's worktree as `wip(<key>): <step>` and
  appends an entry to `<plan>.<story>.journal.json`. The dispatch prompt
  instructs the agent to call this after each meaningful, idempotent step.
- `interrupt_story(plan_name, story_key)` — sends `SIGTERM` to the agent's
  process (a no-op if it already exited), checkpoints whatever is
  uncommitted, and sets the story to `interrupted` rather than `failed`.
  The worktree and branch are left in place. A later `dispatch_story` call
  resumes it. Used by `advance_pipeline` when the usage gate trips.

### Decisions (overlord)
- `request_decision(plan_name, story_key, question, options, context="")` —
  escalate to the overlord; ruling is returned and appended to
  `<plan>.decisions.json`. Story agents call this when blocked.
- `list_decisions(plan_name)` — the decision audit log.

### Review & merge
- `review_story(plan_name, story_key)` — run the `code-reviewer` persona on the
  branch; on `APPROVE` open a PR via `gh` and set status `pr_open`; otherwise set
  `changes_requested`. Never merges.

### Usage gate
- `check_usage()` — probes current subscription usage via a headless
  `claude -p "/usage"` call (answered from local session data, so it costs
  nothing and is fast), and persists `session_pct`/`week_pct`/resets/`paused`
  to `USAGE_STATE_PATH`. Run this every ~60s from an external poller (cron,
  launchd, or `/loop`) — `advance_pipeline` only reads the cached state, it
  never probes itself, so the two cadences are independent.

### Orchestration
- `advance_pipeline(plan_name)` — one idempotent tick: dispatch ready (and
  resumable `interrupted`) stories → advance finished ones (test → review →
  PR) → adjudicate merges against the risk threshold. In `dry-run` it
  plans/logs only. **Honors the usage gate**: while `paused`, it interrupts
  every `in_progress` story instead of letting them keep running, and skips
  new dispatch/review (both spend usage); merge adjudication still runs
  (git/gh only, no model usage). Returns a summary with `dispatched`,
  `advanced`, `merged`, `parked`, `failed`, `interrupted`, `paused`, and
  `notify`. The orchestrating agent surfaces `notify` items (e.g. via
  PushNotification).
- `advance_all_plans()` — runs `advance_pipeline` on every plan that has a
  manifest (i.e. has been ingested), keyed by plan name. Plans saved but not
  yet ingested are skipped. Run this on a recurring schedule instead of
  hardcoding a plan name — newly ingested plans are picked up automatically.
  **Each plan's actions run scoped to that plan's own `repo_root`** (see
  below) — safe to call across plans belonging to different repos. A plan
  ingested without a `repo_root` falls back to the server's global
  `REPO_ROOT`, which is only correct if that plan happens to be the one the
  env var was set for.

### Manual status (kept for the human-driven flow)
- `mark_story_in_progress(plan_name, story_key)`, `mark_story_done(...)`.

---

## Status lifecycle

```
todo ──dispatch──► in_progress ──tests pass──► (review) ──► pr_open ──merge?──► done
  ▲                     │   │                       │                  └park──► parked
  │                     │   └──tests fail──► failed └─REQUEST_CHANGES─► changes_requested
  │                     └──usage gate trips──► interrupted ──dispatch (resume)──┘
  └─────────────────────────────────────────────────────────────────────────────┘
```

`interrupted` is distinct from `failed`: it means the agent was stopped (by
`interrupt_story`, e.g. the usage gate) with its work checkpointed, not that
it produced a bad result. `dispatch_story` treats it the same as `todo` —
deps-satisfied and ready — but resumes the existing worktree instead of
creating a new one.

State lives in `~/.claude/plans/<plan>.manifest.json` (one entry per story).
Checkpoint history lives in `~/.claude/plans/<plan>.<story>.journal.json`.

---

## Plan / story schema

`save_plan` accepts JSON of this shape (the `product-analyst` persona emits it):

```json
{
  "repo_root": "/absolute/path/to/this/plan's/git/repo",
  "epics": [
    {
      "summary": "Epic title",
      "stories": [
        {
          "summary": "Story title",
          "description": "What and why",
          "agent_instructions": "Precise instructions, incl. the TDD expectation",
          "acceptance_criteria": ["testable condition", "..."],
          "dependencies": ["<other story key/summary>"],
          "persona": "software-engineer",
          "model": "sonnet",
          "risk": "low"
        }
      ]
    }
  ]
}
```

- `repo_root` — **absolute path to this plan's git repo.** `ingest_plan` carries
  it into the manifest. Required if you ever run `advance_all_plans()` (which
  iterates every plan in shared `PLAN_DIR`, each potentially belonging to a
  different repo) — without it, dispatch/merge fall back to the server's
  global `REPO_ROOT`, which is almost certainly the wrong repo for any plan
  other than the one that env var happens to be set for. Optional if you only
  ever drive this plan from a session/`.mcp.json` whose `REPO_ROOT` already
  points at the right repo.
- `persona` — which agent implements the story (defaults to a generic agent if
  omitted).
- `model` — `opus | sonnet | haiku`; falls back to the persona's frontmatter
  model, then to `PIPELINE_DEFAULT_MODEL`.
- `risk` — `low | medium | high`; drives the overlord's gating (default `low`).

---

## Configuration (environment variables)

Set global vars in your shell profile; set per-project overrides in the project's
`.mcp.json` `env` block.

| Variable | Default | Purpose |
|---|---|---|
| `PLANE_BASE` | `http://localhost` | Plane instance URL |
| `PLANE_API_KEY` | — | Plane token (never commit) |
| `PLANE_WORKSPACE` | — | Plane workspace slug |
| `PLANE_PROJECT` | — | Plane project UUID |
| `REPO_ROOT` | `.` | Git repo the pipeline operates on |
| `PLAN_DIR` | `~/.claude/plans` | Plans/manifests/logs |
| `WORKTREE_ROOT` | `~/.claude/worktrees` | Per-story worktrees |
| `AGENTS_DIR` | `~/.claude/agents` | Persona files |
| `OVERLORD_POLICY` | `~/.claude/overlord-policy.md` | Global decision policy |
| `PIPELINE_AUTONOMY` | `gated` | `dry-run` \| `gated` \| `full` |
| `PIPELINE_RISK_THRESHOLD` | `low` | Highest risk merged unattended in `gated` |
| `PIPELINE_DEFAULT_MODEL` | `sonnet` | Model when no story/persona model |
| `USAGE_STATE_PATH` | `~/.claude/usage_state.json` | Where `check_usage` persists usage/paused state |
| `PIPELINE_PAUSE_THRESHOLD` | `90` | `%` usage (session or week) that trips the gate |
| `PIPELINE_RESUME_THRESHOLD` | `70` | `%` usage both windows must drop below to clear it |
| `PIPELINE_BACKEND_DISPATCH` | `claude` | Backend driver for dispatch (coding) agents |
| `PIPELINE_BACKEND_REVIEW` | `claude` | Backend driver for the code-reviewer persona |
| `PIPELINE_BACKEND_OVERLORD` | `claude` | Backend driver for overlord decisions |
| `PIPELINE_LOCAL_ENDPOINT` | `http://localhost:11434/v1` | OpenAI-compatible endpoint for the `local` driver (Ollama, vLLM, a hosted open-weights API — same driver, different URL) |
| `PIPELINE_LOCAL_MODEL_DEFAULT` | `devstral:24b` | Local model used for any tier without its own override below |
| `PIPELINE_LOCAL_MODEL_OPUS` | — | Local model for the `opus` tier (falls back to the default) |
| `PIPELINE_LOCAL_MODEL_SONNET` | — | Local model for the `sonnet` tier (falls back to the default) |
| `PIPELINE_LOCAL_MODEL_HAIKU` | — | Local model for the `haiku` tier (falls back to the default) |
| `PIPELINE_LOCAL_TIMEOUT_SECONDS` | `600` | Request timeout for local/cloud completions |

**Backend routing:** dispatch, review, and overlord each resolve independently
via `backend.get_backend(role)` (see `backend.py`) — moving one role off Claude
never touches the others. Two drivers exist today:
- `claude` — wraps the `claude` CLI (unchanged behavior).
- `local` — calls any OpenAI-compatible `/chat/completions` endpoint. It only
  implements `complete()` (a single prompt/system in, text out): safe for
  self-contained prompts like the overlord's decision flow, but routing
  `dispatch` or `review` to it raises `NotImplementedError` — those roles need
  real tool execution (running tests, editing files), which no driver
  provides yet.

Setting any `PIPELINE_BACKEND_*` var to a name that isn't registered also
raises `NotImplementedError` naming the offending var.

**Autonomy levels:**
- `dry-run` — plan and log only; never dispatch, merge, or take irreversible
  action. Start here when trying a new plan.
- `gated` (default) — act unattended up to `PIPELINE_RISK_THRESHOLD`; park higher.
- `full` — act on all tiers except `park-and-ping` (high risk), which is always
  held.

---

## End-to-end workflow

1. **Plan.** Use the `product-analyst` persona to produce a plan, then
   `save_plan`. Review it.
2. **Ingest.** `ingest_plan(plan)` → creates Plane issues, writes the manifest.
3. **Dry run.** Set `PIPELINE_AUTONOMY=dry-run` and call `advance_pipeline(plan)`
   to see what *would* happen (which stories dispatch, which PRs would merge).
4. **Run.** Switch to `gated`, then drive `advance_pipeline(plan)` on an interval:
   - watched: `/loop 5m advance_pipeline` (or call the tool manually),
   - unattended: a `/schedule` cron job.
   Separately, run `check_usage()` on its own ~60s cadence so the usage gate
   (see below) has fresh data — these two loops are independent.
5. **Adjudicate.** Story agents escalate via `request_decision`; the overlord
   rules per policy. Approved low-risk PRs auto-merge; high-risk PRs are parked
   and you are notified.
6. **Audit.** Review `<plan>.decisions.json` and `<plan>.notifications.log`
   anytime.

---

## Safety

- **Risk threshold** gates unattended merges; `high` risk is always parked.
- **Audit trail**: every overlord ruling is logged; every change is a branch +
  PR even when auto-merged — nothing is invisible or unrecoverable.
- **Kill switch**: `PIPELINE_AUTONOMY=dry-run` stops all actions.
- **Mainline protection** (recommended): for the first runs, have the overlord
  merge to an `integration` branch or require branch-protection green checks
  rather than merging straight to `master`.

---

## Usage gate & resumability

A dispatched agent runs as a real `claude -p` subprocess against your Claude
subscription. If usage runs high, you want the pipeline to stop spending more
without losing whatever a story has already done. Two mechanisms make that
possible:

**Checkpointing.** Every dispatch prompt instructs the agent to call
`checkpoint(plan_name, story_key, step, summary, next_hint)` after each
meaningful, idempotent step. Each call commits any uncommitted worktree
changes (`wip(<key>): <step>`) and appends to the story's journal. This is
the durable record a killed agent can't erase — the loss window on a kill is
only the work since the last checkpoint, not the whole run. **Checkpoint
granularity is the one knob that matters here**: more frequent checkpoints
shrink the loss window at a small overhead cost.

**The usage gate.** Run `check_usage()` on an external ~60s cadence (cron,
launchd, or `/loop`) — it probes `claude -p "/usage"`, computes `paused` with
hysteresis (trip at `PIPELINE_PAUSE_THRESHOLD`%, clear only once **both** the
session and week windows drop below `PIPELINE_RESUME_THRESHOLD`%), and
persists it to `USAGE_STATE_PATH`. `advance_pipeline` reads that flag each
tick: while paused, it `interrupt_story`s every running agent (checkpoint +
`SIGTERM`, not a kill -9 — the worktree survives) instead of letting them
keep burning the quota you're trying to protect, and skips starting new
dispatch/review. Once usage drops back below the resume threshold, the next
`advance_pipeline` tick dispatches `interrupted` stories exactly like `todo`
ones — `dispatch_story` detects the existing worktree and resumes instead of
recreating it. No separate "resume" step is needed.

**What this does not solve:** a resumed agent may redo whatever it was
mid-way through at its last checkpoint. Git-tracked file edits are naturally
safe to redo. **External side effects — API calls, DB writes, opening a
PR — are not**, and need an idempotency key or a "did I already do this?"
check written into the story's `agent_instructions`. This is a per-story
concern, not something the pipeline can guarantee for you.

---

## Development & testing

```bash
cd ~/.claude/mcp-servers/pipeline
.venv/bin/python -m pytest -q          # run the suite
.venv/bin/python -m py_compile pipeline_mcp_server.py
```

Tests mock only external boundaries (`claude` CLI, `git`, `gh`, Plane HTTP) and
exercise internal logic directly; `@mcp.tool()` leaves the functions directly
callable. `pytest` is installed in the project `.venv` as a dev dependency.

When changing behavior, follow TDD (write the failing test first) and do not
modify existing tests without a deliberate reason — they are the regression
guard for the pipeline.

---

## Prerequisites

- **Plane** instance with API access (issue tracker / source of truth).
- **GitHub CLI** (`gh`) installed and authenticated (`gh auth login`) — required
  for the review gate and merges.
- **Claude Code CLI** (`claude`) on PATH — used for all headless agents.
- The pipeline MCP server registered (globally or per-project `.mcp.json`).
  After editing the server, reload the MCP server (restart the Claude Code
  session) so new tools are picked up.
- A poller calling `check_usage()` every ~60s, if you want the usage gate
  active (see **Usage gate & resumability**). Without it, `paused` simply
  never gets set and `advance_pipeline` behaves as if usage is always low.
