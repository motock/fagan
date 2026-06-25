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
| Pipeline MCP server | `pipeline_mcp_server.py` | All pipeline tools + orchestration |
| Backend seam | `backend.py` | Per-role driver routing (`claude` / `local`); single-shot, review, dispatch, resource gate |
| Local agent loop | `scripts/local_agent.py` | Native-tool-calling write loop for local dispatch (subprocess) |
| Install / deps | `scripts/install.sh`, `requirements*.txt` | venv + dependency setup |
| Tests | `test_pipeline_mcp_server.py`, `test_backend.py` | `pytest`, run via the venv |
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
  `agent/<key>`, spawn a headless agent on the configured dispatch backend
  (`claude -p`, or the local agent loop) **with the story's persona system
  prompt, model, and curated tools**, transition the Plane issue to In
  Progress. Returns immediately with the PID and a `resumed` flag.
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
  branch; on `APPROVE` open a PR via `gh` and set status `pr_open`; otherwise
  persist the reviewer's full feedback on the story and set `changes_requested`.
  Never merges. A `changes_requested` story is dispatch-eligible again: the next
  `advance_pipeline` tick redispatches it with that feedback seeded into the
  agent's prompt so it reworks the right thing. This rework loop is bounded by
  `PIPELINE_REWORK_MAX_ATTEMPTS` — once the reviewer has rejected the story that
  many times it is **parked** for human review instead of looping forever.

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
  (git/gh only, no model usage). A transient merge failure (`gh`/`git`) does
  not crash the tick: the story stays `pr_open` and is retried on subsequent
  ticks up to `PIPELINE_MERGE_MAX_ATTEMPTS`, after which it is marked `failed`
  and flagged for human intervention. A dispatch that keeps failing (a raising
  `dispatch_story`, or an agent that launches but produces no output) is
  likewise retried up to `PIPELINE_DISPATCH_MAX_ATTEMPTS` before the story is
  marked `failed` rather than looping forever; legitimate usage-gate interrupts
  do not count against this budget. Plane state transitions are best-effort
  with their own inline retry (`PIPELINE_PLANE_MAX_ATTEMPTS`) and never block
  git work. Returns a summary with `dispatched`,
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
  │                     └──usage gate trips──► interrupted ──dispatch (resume)──┘   │
  ├──────────────────────────── redispatch (rework, w/ feedback) ─────────────────┘
  └─ rework budget exhausted ─► parked
```

`interrupted` is distinct from `failed`: it means the agent was stopped (by
`interrupt_story`, e.g. the usage gate) with its work checkpointed, not that
it produced a bad result. `dispatch_story` treats it the same as `todo` —
deps-satisfied and ready — but resumes the existing worktree instead of
creating a new one.

`changes_requested` is likewise dispatch-eligible: the reviewer's feedback is
stored on the story and `advance_pipeline` redispatches it (resuming its
worktree) with that feedback in the prompt, so it reworks in place. After
`PIPELINE_REWORK_MAX_ATTEMPTS` rejections it is `parked` for human review
instead of cycling forever. An `APPROVE` clears the stored feedback and counter.

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
| `PIPELINE_MAX_CONCURRENT_AGENTS` | `3` | Cap on dispatched agents running at once (across all plans); `<=0` = unlimited |
| `PIPELINE_MERGE_MAX_ATTEMPTS` | `3` | Merge error budget: how many ticks a failing `_merge_pr` (transient `gh`/`git`) is retried before the story is marked `failed` for human intervention |
| `PIPELINE_DISPATCH_MAX_ATTEMPTS` | `3` | Dispatch error budget: how many times a story whose launch keeps failing (raising `dispatch_story`, or an agent that produces no output) is retried before it is marked `failed` instead of looping forever |
| `PIPELINE_REWORK_MAX_ATTEMPTS` | `3` | Rework budget: how many times a `changes_requested` story is redispatched (with the reviewer's feedback) before it is `parked` for human review instead of looping through review↔rework |
| `PIPELINE_PLANE_MAX_ATTEMPTS` | `3` | Plane error budget: inline retries for a best-effort Plane state transition before the drop is recorded durably (Plane sync never blocks git work) |
| `PIPELINE_PAUSE_THRESHOLD` | `90` | `%` of the **session** window that trips the Claude usage gate |
| `PIPELINE_RESUME_THRESHOLD` | `70` | `%` the **session** window must drop below to clear the gate |
| `PIPELINE_WEEK_PAUSE_THRESHOLD` | `90` | `%` of the **week** window that trips the gate |
| `PIPELINE_WEEK_RESUME_THRESHOLD` | `70` | `%` the **week** window must drop below to clear the gate |
| `PIPELINE_BACKEND_DISPATCH` | `claude` | Backend for dispatch (coding) agents: `claude` \| `local` |
| `PIPELINE_BACKEND_REVIEW` | `claude` | Backend for the code-reviewer persona: `claude` \| `local` |
| `PIPELINE_BACKEND_OVERLORD` | `claude` | Backend for overlord decisions: `claude` \| `local` |
| `PIPELINE_LOCAL_ENDPOINT` | `http://localhost:11434` | Ollama base URL for the `local` driver (it uses Ollama's native `/api/chat`, the only surface that accepts `num_ctx`). Point at a remote Ollama to use another box. |
| `PIPELINE_LOCAL_MODEL_DEFAULT` | `devstral:24b` | Local model used for any tier without its own override below |
| `PIPELINE_LOCAL_MODEL_OPUS` | — | Local model for the `opus` tier (falls back to the default) |
| `PIPELINE_LOCAL_MODEL_SONNET` | — | Local model for the `sonnet` tier (falls back to the default) |
| `PIPELINE_LOCAL_MODEL_HAIKU` | — | Local model for the `haiku` tier (falls back to the default) |
| `PIPELINE_LOCAL_NUM_CTX` | `16384` | Ollama context window for local calls (sized to fit 100% on a 24GB M4 GPU; raising it risks a slow CPU/GPU split) |
| `PIPELINE_LOCAL_TEMPERATURE` | `0.3` | Sampling temperature for local model calls |
| `PIPELINE_LOCAL_TIMEOUT_SECONDS` | `600` | Per-request timeout for local single-shot `complete()` calls |
| `PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS` | `900` | Per-request timeout inside the local dispatch/review agent loop |
| `PIPELINE_LOCAL_MAX_STEPS` | `40` | Max tool-call steps a local **dispatch** run takes before it parks (WIP-commits) |
| `PIPELINE_LOCAL_REVIEW_MAX_STEPS` | `20` | Max tool-call steps a local **review** takes before returning UNKNOWN (→ parks) |

**Backend routing:** dispatch, review, and overlord each resolve independently
via `backend.get_backend(role)` (see `backend.py`) — moving one role off Claude
never touches the others. Two drivers exist today:
- `claude` — wraps the `claude` CLI (unchanged behavior).
- `local` — talks to a local **Ollama** server (native `/api/chat`). All three
  roles can run local:
  - **overlord** — single-shot `complete()` (self-contained prompt in, ruling out).
  - **review** — a blocking **read-only** tool loop (the model runs the tests
    and reads files via `bash`/`view_file` — no edit tools — then submits a
    verdict). Routing review local is best kept to low-risk stories; see the
    tiering note in `Local_LLM_Port_Plan.md`.
  - **dispatch** — a native-tool-calling **write** agent loop
    (`scripts/local_agent.py`, run as a subprocess: `create_file`/`str_replace`/
    `view_file`/`bash`/`checkpoint`/`done`, with a non-destructive editor, loop
    guard, and commit enforcement).

  The local driver deliberately does **not** use OpenHands — see
  `Local_LLM_Port_Plan.md` for the full investigation (why, and the model
  caveats: local dispatch is reliable on small/mechanical stories but has a
  reasoning ceiling, so keep `PIPELINE_RISK_THRESHOLD` conservative and let
  bigger work park or stay on Claude).

Setting any `PIPELINE_BACKEND_*` var to a name that isn't registered raises
`NotImplementedError` naming the offending var.

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

A dispatched agent runs as a real subprocess — `claude -p` against your Claude
subscription, or a local-model agent loop against Ollama. If a backend's
resource (Claude usage, or Ollama availability) runs out, you want the pipeline
to stop spending more on that backend without losing whatever a story has
already done. Two mechanisms make that possible:

**Checkpointing.** Every dispatch prompt instructs the agent to call
`checkpoint(plan_name, story_key, step, summary, next_hint)` after each
meaningful, idempotent step. Each call commits any uncommitted worktree
changes (`wip(<key>): <step>`) and appends to the story's journal. This is
the durable record a killed agent can't erase — the loss window on a kill is
only the work since the last checkpoint, not the whole run. **Checkpoint
granularity is the one knob that matters here**: more frequent checkpoints
shrink the loss window at a small overhead cost.

**The resource gate (per-backend).** Each tick, `advance_pipeline` gates
dispatch and review **independently, by the backend serving each role**
(`backend.resource_status()`):
- **Claude-backed roles** consult the usage gate. Run `check_usage()` on an
  external ~60s cadence (cron, launchd, or `/loop`) — it probes
  `claude -p "/usage"`, computes `paused` with hysteresis (trip at the
  session/week `PAUSE_THRESHOLD`%, clear only once **both** windows drop below
  their `RESUME_THRESHOLD`%), and persists it to `USAGE_STATE_PATH`.
- **Local-backed roles** consult only Ollama reachability — there's no usage
  limit, so a local role is "available" whenever Ollama is up. **This is what
  lets local dispatch keep running while Claude's weekly limit is maxed.**

If the **dispatch** backend is gated, the tick `interrupt_story`s every running
agent (checkpoint + `SIGTERM`, not a kill -9 — the worktree survives) and starts
no new dispatch; if the **review** backend is gated, review is deferred. The two
are independent (so a Claude usage pause with dispatch routed local only defers
Claude review). Merge adjudication always runs (no model usage). Once a gated
backend frees up, the next tick dispatches `interrupted` stories exactly like
`todo` ones — `dispatch_story` detects the existing worktree and resumes instead
of recreating it. No separate "resume" step is needed.

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
scripts/install.sh --dev               # one-time: create .venv + install runtime + pytest
.venv/bin/python -m pytest -q          # run the suite
.venv/bin/python -m py_compile pipeline_mcp_server.py backend.py
```

Tests mock only external boundaries (`claude` CLI, `git`, `gh`, Plane HTTP, and
the local Ollama HTTP calls) and exercise internal logic directly; `@mcp.tool()`
leaves the functions directly callable. Runtime deps are in `requirements.txt`;
`pytest` is added by `requirements-dev.txt`.

When changing behavior, follow TDD (write the failing test first) and do not
modify existing tests without a deliberate reason — they are the regression
guard for the pipeline.

---

## Prerequisites

- **Python 3.10+** and the project venv. Run **`scripts/install.sh`** (add
  `--dev` for the test deps): it creates `.venv`, installs `requirements.txt`
  (`mcp`, `httpx`), and reports which external tools below are present. The
  `.venv` is gitignored, so this is the first step on a fresh clone.
- **git** on PATH — worktrees, branches, merges.
- **GitHub CLI** (`gh`) installed and authenticated (`gh auth login`) — required
  for the review gate and merges.
- **Claude Code CLI** (`claude`) on PATH — used for any role on the default
  `claude` backend.
- **Ollama** + the local model (`ollama pull devstral:24b`) — **only if** you
  route any role to the `local` backend (`PIPELINE_BACKEND_*=local`). Not needed
  for an all-Claude setup.
- The pipeline MCP server registered (globally or per-project `.mcp.json`).
  After editing the server, reload the MCP server (restart the Claude Code
  session) so new tools are picked up.
- A poller calling `check_usage()` every ~60s, if you want the Claude usage gate
  active (see **Usage gate & resumability**). Without it, `paused` simply
  never gets set and Claude-backed roles behave as if usage is always low.

---

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE) and
[NOTICE](NOTICE). Contributions submitted for inclusion in this work are
licensed under the same terms (Apache-2.0, §5), with no additional conditions.
