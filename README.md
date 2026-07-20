# Autonomous SDLC Agent Pipeline

[![CI](https://github.com/fico-jessecarroll/claude-pipeline-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/fico-jessecarroll/claude-pipeline-mcp/actions/workflows/ci.yml)

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
| Backend seam | `backend.py` | Per-role driver routing (`claude` / `ollama` / `lmstudio` / `mlx` / `local`); single-shot, review, dispatch, resource gate |
| Local agent loop | `scripts/local_agent.py` | Native-tool-calling write loop for local dispatch (subprocess) |
| Monitoring dashboard | `dashboard.py`, `static/` | Read-only FastAPI status/lifecycle viewer |
| Install / deps | `scripts/install.sh`, `requirements*.txt` | venv + dependency setup |
| Tests | `test_pipeline_mcp_server.py`, `test_backend.py`, `test_dashboard.py` | `pytest`, run via the venv |
| Plans / manifests / logs | `~/.claude/plans/` | Plan, manifest, decisions, notifications |
| Worktrees | `~/.claude/worktrees/` | Isolated per-story branches |
| Issue tracker | Plane (external, optional) | Mirror of story state; skipped entirely when unconfigured (manifest is the source of truth) |

---

## Architecture

```
            ┌─────────────────────────────────────────────────┐
            │ Orchestrator loop (cron / /loop skill)          │
            │ advance_pipeline(plan) — one idempotent tick    │
            └───────────────────────┬─────────────────────────┘
    ready stories                   │  gates adjudicated by overlord
    (deps satisfied)                │
                                    ▼

 ┌───────────────┐ resolve backend + persona/model  ┌─────────────────────────────┐
 │ Plan/Manifest │─────────────────────────────────►│ Dispatch:                   │
 │ (JSON, Plane) │                                  │  • claude -p  OR  local loop│
 └──────────┴────┘                                  │  • tech-lead planner →      │
            │                                       │    .agent_plan.md (local)   │
            │                                       │                             │
            │                                       └──────────────┬──────────────┘
            │                                                      │
            │ audit → decisions log                                ▼
            │                                       ┌─────────────────────────────┐
            │                                       │ Headless story agent        │
            │                                       │ in git worktree             │
            │                                       └──────────────┬──────────────┘
            │                       tests + acceptance oracle      │
            │                   local fail → escalate to Claude    │
            │                                                      ▼
        ┌───┬───────────────────┐                   ┌─────────────────────────────┐
        │    OVERLORD           │◄────── merge ──── │ code-reviewer → VERDICT     │
        │  (Opus, policy)       │────── approve ──► │ APPROVE → gh pr create      │
        │  decides gates        │                   │ (auto-PR + merge)           │
        └───────────────────────┘                   └─────────────────────────────┘
        story agents escalate blocks via request_decision()
```

The diagram folds in the three dimensions that layer on the core flow; each has
its own section below:

- **Backends.** Dispatch, review, and overlord each resolve a backend
  independently (`claude` / `ollama` / `lmstudio` / `mlx` / `local`), so a role
  can run on a local model while the others stay on Claude — see *Per-role
  provider/model configuration* and the `PIPELINE_BACKEND_*` vars.
- **Tech-lead planner.** For a local dispatch backend, a stronger planner can
  first decompose the story into an ordered checklist the local agent executes
  in the same worktree (`.agent_plan.md` / `.agent_scratchpad.md`) — see
  *Guided decomposition*.
- **Auto escalation.** Under `PIPELINE_BACKEND_DISPATCH=auto`, stories start
  local and escalate to Claude (or to a fallback local model) on test failure,
  step-cap streaks, or a review loop that can't converge — see *`auto` —
  layered local-first dispatch*. The acceptance oracle grades local runs before
  they reach review.

---

## Personas (`~/.claude/agents/`)

Each persona is a Claude Code subagent: a markdown file with YAML frontmatter
(`name`, `description`, `model`, and optionally `memory: user`) and a
system-prompt body. The pipeline reads the body and dispatches a headless agent
with it as the role.

`memory: user` injects the user-memory directory into the system prompt on
every Claude call — high-leverage context but expensive in tokens. The
**reviewer personas** (`code-reviewer`, `security-engineer`) deliberately omit
it: their job is a mechanical check (run tests, read diff, emit `VERDICT`),
the CLAUDE.md rules they need are in the persona body, and skipping the
~132 KB memory injection shaves ~30-40% off every review call's input tokens.
The dispatch and overlord personas keep it because they benefit from project
context and are lower-volume.

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
- `decompose_plan(request)` — turn a raw goal/feature request into epics/
  stories JSON via the product-analyst persona, on whichever provider the
  `decompose` role is configured for (see "Per-role provider/model
  configuration" below). Does **not** call `save_plan` itself — review the
  returned plan (same as you would the interactive `product-analyst`
  subagent's output), then `save_plan` it yourself. Returns `{"ok": true,
  "plan": {...}}` on success, or `{"ok": false, "error": ..., "raw": ...}`
  if the model's response wasn't valid/shaped JSON.
- `save_plan(plan_name, plan_json)` — save a plan JSON to `~/.claude/plans/`.
- `list_plans()` — list saved plans.
- `ingest_plan(plan_name, only_epics=None)` — push a plan into Plane (epics +
  issues), tag with `agent-pipeline`, write `<plan>.manifest.json`. Carries each
  story's `persona`, `model`, `risk`, and `backend` into the manifest. A
  story's `backend` (`claude` \| `local` \| `ollama` \| `lmstudio` \| `mlx` \|
  `auto`) pins its dispatch provider from the plan itself, independent of the
  process-wide `PIPELINE_BACKEND_DISPATCH` — an unknown value is rejected at
  ingest time with a clear error, before any Plane side effects.
- `get_role_config(plan_name=None)` — show the resolved `(provider, model)`
  for every role (`overlord`, `planner`, `dispatch`, `review`, `decompose`)
  given the current env vars and `model_registry.json`, optionally layered
  with a specific plan's `role_config` (see below). Pure read — check what a
  plan will actually run on *before* executing it.

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
  incomplete tree. If the story carries an `acceptance` block, the gate runs
  **only** the acceptance fixture file(s), not the whole worktree suite —
  `_scope_test_cmd_to_acceptance` derives the scoped command per runner
  (pytest: append the fixture paths; cargo: `cargo test --test <stem>` per
  `tests/*.rs` fixture; npm/yarn: `node --test <paths>` when `scripts.test`
  is `node --test`); an unscopeable runner falls back to the full suite. This
  prevents a correct implementation from being blocked by the model's own wrong
  test assertions, but it also means the gate no longer catches regressions
  elsewhere in the worktree; the reviewer's own "run the test suite"
  instruction is the remaining backstop for those. Stories without an
  `acceptance` block still run the full suite as before. When
  `PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1`, an acceptance-failing dispatch that
  nonetheless produced real work (new commits) is routed to the reviewer
  instead of straight to terminal `failed`, so the rework loop can re-dispatch
  it with feedback; an empty-branch failure (no commits) still goes to
  `failed`. The merge gate (`_reverify_acceptance`) still blocks any
  APPROVE'd-but-failing merge.

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
  If the reviewer backend itself returns an infrastructure rate-limit response
  (not a genuine review), the story is left at `tests_passed` for the next
  `advance_pipeline` tick to retry — this does **not** count against
  `PIPELINE_REWORK_MAX_ATTEMPTS`, so a rate-limited backend can't silently park
  a correct implementation. A non-rate-limited `UNKNOWN` verdict (no parseable
  `VERDICT` line, or the reviewer-exception fail-safe) is likewise treated as
  inconclusive rather than a rejection: status is left untouched for a retry
  on the next tick, and neither `review_feedback` nor `rework_attempts` is
  touched, so the story is never redispatched to rework blind on empty
  feedback. This is bounded by its own budget, `PIPELINE_REVIEW_INCONCLUSIVE_MAX`
  — after that many consecutive inconclusive verdicts the story is **parked**
  for human review instead of retrying forever.
  On `REQUEST_CHANGES`, the worktree's current HEAD commit SHA is recorded on
  the story as `last_reviewed_sha`. If `review_story` is called again while
  HEAD is still that same SHA — i.e. no redispatch/rework has landed a new
  commit since the rejection — the reviewer is not invoked a second time;
  the call returns `{"ok": True, "status": <unchanged>, "skipped":
  "unchanged_since_last_review"}` instead. This closes a real gate-integrity
  gap: because LLM review is not fully deterministic, a second call on an
  unchanged diff could otherwise land on a different verdict than the first
  and silently override a real, unaddressed finding. The story remains
  dispatch-eligible at `changes_requested` throughout — a genuine rework that
  lands a new commit naturally clears the guard on its next review call, so
  this only blocks re-reviewing the exact same unchanged commit, never the
  story's forward progress. `last_reviewed_sha` is cleared on `APPROVE`.

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
  `advanced`, `merged`, `parked`, `failed`, `interrupted`, `paused`, `skipped`,
  and `notify`. `skipped` holds story keys where dispatch was deferred due to
  lock contention (another tick already running for the plan) — distinct from
  `failed`, since the story remains dispatch-eligible and is simply retried
  next tick. The orchestrating agent surfaces `notify` items (e.g. via
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

## Monitoring dashboard

`dashboard.py` is a small, **read-only** FastAPI app for watching the
lifecycle above without polling MCP tools by hand. It only reads the files
already described in this doc (`<plan>.manifest.json`,
`<plan>.notifications.log`, `<plan>.decisions.json`) — it never dispatches,
advances, or mutates anything, so it carries none of the pipeline's risk
surface and can be left running indefinitely.

```bash
cd ~/.claude/mcp-servers/pipeline
pip install -r requirements-dashboard.txt   # one-time: fastapi + uvicorn
scripts/dashboard.sh start                 # http://127.0.0.1:8000
```

`scripts/dashboard.sh` runs `uvicorn dashboard:app` detached in its own
session, records the pid to `.dashboard.<port>.pid`, and logs to `dashboard.log`
(both in the repo root, gitignored). Subcommands:

| Command | Effect |
|---|---|
| `scripts/dashboard.sh start` | Launch in the background; refuse if already running. |
| `scripts/dashboard.sh stop` | SIGTERM the recorded pid (escalates to SIGKILL), then remove the pid file. Clean no-op if not running. |
| `scripts/dashboard.sh restart` | `stop` then `start`. |
| `scripts/dashboard.sh status` | Print `running, pid N, http://host:port` (exit 0) or `not running` (exit 1). |

Host/port are env-configurable: `DASHBOARD_HOST` (default `127.0.0.1`),
`DASHBOARD_PORT` (default `8000`). Set `DASHBOARD_RELOAD=1` to pass `--reload`
to uvicorn (dev only — `stop` signals the whole process group so the reloader
and its worker both die). **Stopping the dashboard:** `scripts/dashboard.sh stop`.

On launch the dashboard opens on a **Fleet Overview** landing page that
aggregates every plan on disk. From there you drill into any plan to see
its kanban board.

### Per-plan kanban

Per plan: a kanban board of stories by `status` (click a card for the
modal — persona / model / risk / worktree / PR / attempt counts / errors,
the tail of the per-story log, and a checkpoint journal timeline). Polls
every 4s; honors `PLAN_DIR` the same way the MCP server does.

A **usage-gate banner** across the top reads `USAGE_STATE_PATH` (`/api/usage`)
and shows the current session/week %. If the gate goes **blind** — the usage
probe has been unparseable past the staleness window, so it's failing *open*
and spend is unguarded (see below) — the banner turns red and names the blind
duration / failure count, so a silent CLI-output change can't quietly disable
the cost gate.

### Observability surfaces

- **Fleet Overview landing page.** The first thing you see. Loads
  `/api/plans` and `/api/dispatch_health` and renders a single page with
  every plan's done/total, a per-plan "paused" tag, a fleet-wide status
  breakdown bar, and the headline escalation / success rates from
  `/api/dispatch_health`.
- **Acceptance-stratified escalation / success rates.** `/api/dispatch_health`
  splits its rollup into `with_acceptance` (the harness-owned acceptance
  oracle path) and `without_acceptance` and reports `escalation_rate` and
  `success_rate` per slice. The Overview surfaces both side-by-side so you
  can see whether the acceptance fixture is paying off in production vs.
  the ordinary TDD slice.
- **Aggregate attempt / failure metrics.** `/api/dispatch_health` totals
  exposes fleet-wide `dispatch_attempts`, `rework_attempts`,
  `merge_attempts`, a `failure_reasons` histogram, and a `by_backend`
  count alongside the existing rates. The Overview renders the attempt
  totals and the failure-reason histogram; the per-story modal renders
  the same numbers per story.
- **Per-story log tail.** Each story modal fetches
  `/api/plans/{plan}/stories/{key}/log` and appends the tail inline below
  the story metadata, so you can see the most recent agent output without
  tailing the file by hand.
- **Local review transcript (`review.log`).** When a story is reviewed on
  the `local` backend, `OllamaDriver._review_loop` appends each review
  cycle (tool calls, results, final verdict) to `review.log` in the
  story's worktree, alongside `agent.log` — best-effort (never breaks the
  review if the write fails), so a local reviewer that returns an
  inconclusive `UNKNOWN` verdict is still debuggable after the fact.
- **Checkpoint journal timeline viewer.** Each story modal also fetches
  `/api/plans/{plan}/stories/{key}/journal` and renders the
  `<plan>.<story>.journal.json` entries as a vertical timeline, so you
  can see what progress has been recorded and when.
- **Tech-lead checklist + scratchpad (Tier 0 progress).** For a story run
  under guided decomposition, the modal fetches
  `/api/plans/{plan}/stories/{key}/checklist` and renders the worktree's
  `.agent_plan.md` (the tech-lead's ordered checklist) and
  `.agent_scratchpad.md` (the executor's running state) read-only, so you
  can watch how far through the plan the local agent has worked. A story not
  run with guided decomposition gets an empty state.
- **Backend and escalated badges + filters.** Cards carry a `claude`
  backend badge when the story's `backend` is non-local and an `escalated`
  badge when it was escalated. The board exposes matching multi-select
  filter chips for `backend` and `escalated` (alongside the existing
  persona / risk filters and the column-level status filter), so a card
  only shows up if it matches all active filters.
- **Story age / staleness indicators.** Cards derive an `ageLabel` from the
  server-supplied `last_activity` (e.g. "12m", "3h"). `in_progress` stories
  older than the staleness window get a `stale` class so wedged agents are
  visible at a glance.
- **URL deep-linking.** The selected plan and every active filter are
  encoded into the URL hash (e.g.
  `#plan=PLAN&status=todo,in_progress&persona=engineer&escalated=yes`).
  The hash is read on load and replayed on `hashchange`, so back/forward
  and shared links restore the same view without a server round-trip.
- **Light / dark theme toggle.** A `theme-toggle` button in the header
  flips between dark (default) and light themes by setting
  `data-theme` on `<html>`. The choice is persisted in `localStorage` and
  re-applied on the next load.
- **Plan sidebar: newest-first sort + archive/dismiss.** `/api/plans`
  sorts by each manifest's file mtime, descending, so active work never
  gets buried below a long-finished plan that happens to sort earlier
  alphabetically. A plan can be dismissed from the default view (a
  "Dismiss" button per sidebar row) without touching its manifest — the
  archived set lives in a dashboard-owned `.dashboard_ui_state.json`
  sidecar file in `PLAN_DIR`, so this is purely a view preference, fully
  reversible via the "Show dismissed plans" toggle at the bottom of the
  sidebar.

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
          "description": "What and why (Plane issue body when Plane is enabled; otherwise unused)",
          "agent_instructions": "Implementation brief the agent receives — incl. the TDD expectation and the testable success criteria (what tests to write)",
          "acceptance": [{"path": "tests/acceptance_foo.rs", "source": "// optional read-only test fixture"}],
          "dependencies": ["<other story key/summary>"],
          "persona": "software-engineer",
          "model": "sonnet",
          "risk": "low",
          "backend": "optional: claude | local | ollama | lmstudio | mlx | auto",
          "key": "optional explicit story key; omit to auto-mint a UUID"
        }
      ]
    }
  ],
  "role_config": {
    "review": {"provider": "mlx", "model": "qwen"}
  }
}
```

- `repo_root` — **absolute path to this plan's git repo, required.**
  `ingest_plan` validates it's present and is an existing directory before
  doing anything else (no Plane calls on failure) and carries it into the
  manifest. Without it, `advance_all_plans()` — which iterates every plan in
  shared `PLAN_DIR`, each potentially belonging to a different repo — would
  fall back to the server's global `REPO_ROOT`: almost certainly the wrong
  repo for any plan other than the one that env var happens to be set for
  (or a deliberately-broken sentinel path, if one's configured to fail
  loudly instead of silently operating on the wrong repo).
- `persona` — which agent implements the story (defaults to a generic agent if
  omitted).
- `model` — `opus | sonnet | haiku`; falls back to the persona's frontmatter
  model, then to `PIPELINE_DEFAULT_MODEL`.
- `risk` — `low | medium | high`; drives the overlord's gating (default `low`).
- `agent_instructions` — the implementation brief the dispatched agent receives. This is where the testable success criteria belong (what tests to write, including negative/boundary cases); it is the single most influential field on outcome quality.
- `acceptance` — *optional* array of `{path, source}` read-only test fixtures. When present, the harness writes each `source` to `path` in the worktree (read-only — the agent may not edit them) and the oracle grades the run on whether the implementation makes them pass. Omit it for ordinary TDD stories where the agent writes its own tests per `agent_instructions`; the story then runs on the base harness with a "tests pass" bar. Do **not** use `acceptance_criteria` or a list of strings — `ingest_plan` reads `acceptance` and expects `{path, source}` dicts; a list of strings raises `TypeError: string indices must be integers` in `dispatch_story`.
- `key` — optional explicit story key; omit to auto-mint a UUID. `dependencies` may reference a story by its exact `summary` string or its explicit `key`.
- `backend` — *optional*, per-story dispatch provider override:
  `claude | local | ollama | lmstudio | mlx | auto`. Pins that one story to
  a specific provider from the plan itself, independent of the process-wide
  `PIPELINE_BACKEND_DISPATCH`. `ingest_plan` validates it against the
  registered drivers and rejects an unknown value before any Plane calls.
  Omit to use `PIPELINE_BACKEND_DISPATCH`'s normal resolution (unchanged).
- `role_config` — *optional*, plan-level (not per-story): per-role provider/
  model overrides for `overlord`, `planner`, `dispatch`, `review`, and
  `decompose`, e.g. `{"review": {"provider": "mlx", "model": "qwen"}}`. Set
  once at the top level of the plan JSON, alongside `epics`; carried into
  the manifest and consulted by `dispatch_story`, `review_story`, and
  `request_decision` on every tick for that plan. Not required — omitting
  it (or any individual role) falls through to `model_registry.json`, then
  the existing `PIPELINE_BACKEND_<ROLE>` env vars, then today's hardcoded
  defaults. See "Per-role provider/model configuration" below.

---

## Per-role provider/model configuration

Every pipeline role — **overlord**, **planner** (the guided-decomposition
checklist role), **dispatch** (the implementer), **review**, and
**decompose** (`decompose_plan`) — is independently configurable to a
provider (`claude` / `ollama` / `mlx` / `lmstudio`) and a model.
`model_registry.json` (repo root, or `PIPELINE_MODEL_REGISTRY_PATH`) is the
single editable place to see and change what's available, instead of
scattered env vars:

```json
{
  "providers": {
    "claude":   {"models": {"opus": {"tag": "opus"}, "sonnet": {"tag": "sonnet"}}},
    "ollama":   {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}, "glm": {"tag": "glm-4.7-flash:cloud"}}},
    "mlx":      {"models": {"qwen": {"tag": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"}}}
  },
  "roles": {
    "review": {"provider": "mlx", "model": "qwen"}
  }
}
```

`providers.<name>.models.<friendly name>.tag` maps a short name (what you'd
say out loud — "gpt-oss", "qwen") to the literal string a provider expects
(an Ollama tag, an MLX model path, or a Claude tier). `roles.<role>` sets
that role's default `(provider, model)` — resolved via the friendly name
above, so a typo is caught immediately rather than silently falling back to
some other model. Both sections are optional and can be partial; an
unconfigured role falls through to today's existing behavior unchanged.

**Resolution priority** (`role_registry.resolve_role`, highest wins), the
same for provider and model independently:

1. A plan's `role_config` block (see the schema above) — set once per plan,
   applies to every tick.
2. The existing `PIPELINE_BACKEND_<ROLE>` env var (and, for local models,
   the existing `PIPELINE_LOCAL_MODEL_*`/`PIPELINE_LOCAL_REVIEW_MODEL`/
   `PIPELINE_DECOMPOSE_CLOUD_MODEL` vars) — unchanged, still the fastest way
   to override ad hoc.
3. `model_registry.json`'s `roles.<role>` entry.
4. The role's existing hardcoded/persona-frontmatter default.

Use `get_role_config(plan_name=None)` to see what actually resolves right
now (optionally layered with a specific plan's `role_config`) before
running that plan.

---

## Guided decomposition (the tech-lead planner)

A constrained local implementer ("junior" level — gpt-oss, qwen3-coder)
succeeds more often when a stronger "tech-lead" planner first breaks a coarse
story into an ordered sub-step checklist that the local model executes **inside
one worktree/transcript**. This is deliberately *not* story-splitting:
fragmenting a story into multiple pipeline stories throws away the shared
transcript and was measured to *hurt* (see `GUIDED_DECOMPOSITION_PLAN.md` — the
Condition D/M validation: 33% vs 100%). Guided decomposition keeps the full
context and adds cross-*sub-step* structure instead.

It is **opt-in and off by default** (`PIPELINE_DECOMPOSE=off`, per Secure
Defaults) and only ever runs for a **local-family** dispatch backend — Claude
doesn't need the crutch. Set `PIPELINE_DECOMPOSE=cloud` to author the checklist
with Claude (`PIPELINE_DECOMPOSE_CLOUD_MODEL`) or `=local` to author it on the
same local provider the story dispatches to (`PIPELINE_BACKEND_PLANNER` pins the
planner to a different provider than dispatch if you want, e.g. dispatch on
`ollama`, plan on `mlx`).

- **Initial checklist (`_run_planner`).** On a story's *first* dispatch (never
  on a resume — the checklist is planned once), the planner turns
  `agent_instructions` into an ordered checklist and writes it to
  `.agent_plan.md` in the worktree; the dispatch prompt appends it under
  "Implementation checklist from your tech lead." The call is **best-effort and
  fails open to `None`** — a broken, slow, or rate-limited planner never blocks
  or corrupts dispatch; the story simply proceeds with no checklist, exactly
  like `PIPELINE_DECOMPOSE=off`.
- **Scratchpad (`PIPELINE_DECOMPOSE_SCRATCHPAD`, default `on`).** The executor
  keeps a running `.agent_scratchpad.md` (what's done, what's next) as durable
  cross-sub-step state. The planner folds "maintain the scratchpad" into the
  checklist as a first-class step, and a trailing prompt reminder backstops it.
  Set to `off` to run the H3 ablation (checklist alone, no cross-step memory).
- **Rework checklist (`_run_rework_planner`).** The same decomposition logic
  applied one step later: a reviewer's prose feedback is itself a coarse brief
  for a weak executor, so on each rework cycle the planner turns the feedback
  into an ordered fix-checklist before the executor sees it. Unlike the initial
  checklist (planned once), this re-runs per cycle since each cycle's feedback
  differs. Same best-effort, fail-open-to-raw-feedback contract.

Both `.agent_plan.md` and `.agent_scratchpad.md` live in the worktree, are
excluded from the per-story log tail, and are surfaced read-only in the
dashboard's per-story modal (the Tier 0 progress view).

---

## Configuration (environment variables)

Set global vars in your shell profile; set per-project overrides in the project's
`.mcp.json` `env` block.

**A ticketing backend is optional.** It's an issue-tracker mirror, not
load-bearing — the manifest (`<plan>.manifest.json`) is the actual source of
truth for story state. Which backend (if any) is active is resolved by
`get_ticket_provider()` from `PIPELINE_TICKET_PROVIDER`:

| `PIPELINE_TICKET_PROVIDER` | Behavior |
|---|---|
| `auto` (default) | Plane if `PLANE_API_KEY`/`PLANE_WORKSPACE`/`PLANE_PROJECT` are all set, else the no-op provider |
| `none` | Force the no-op provider even if Plane is configured |
| `plane` | Force Plane; raises at call time if the three vars above aren't all set |
| `jira` | Documented stub only — selecting it works, but every operation raises `NotImplementedError` until a real implementation lands |

With no backend (the `auto`/unconfigured default, or explicit `none`), every
ticket call is **skipped** (`ingest_plan` mints local story keys; state
transitions no-op) rather than fired at a dead endpoint — without that guard
an unconfigured deployment would 404 on every scheduled tick, burn the
`PIPELINE_PLANE_MAX_ATTEMPTS` retry budget, and flood the logs.

| Variable | Default | Purpose |
|---|---|---|
| `PIPELINE_TICKET_PROVIDER` | `auto` | `auto` \| `none` \| `plane` \| `jira` — see table above |
| `PLANE_BASE` | `http://localhost` | Plane instance URL |
| `PLANE_API_KEY` | — | Plane token (never commit). **Unset ⇒ Plane disabled** |
| `PLANE_WORKSPACE` | — | Plane workspace slug. **Unset ⇒ Plane disabled** |
| `PLANE_PROJECT` | — | Plane project UUID. **Unset ⇒ Plane disabled** |
| `REPO_ROOT` | `.` | Git repo the pipeline operates on |
| `PLAN_DIR` | `~/.claude/plans` | Plans/manifests/logs |
| `WORKTREE_ROOT` | `~/.claude/worktrees` | Per-story worktrees |
| `AGENTS_DIR` | `~/.claude/agents` | Persona files |
| `OVERLORD_POLICY` | `~/.claude/overlord-policy.md` | Global decision policy |
| `PIPELINE_AUTONOMY` | `gated` | `dry-run` \| `gated` \| `full` |
| `PIPELINE_RISK_THRESHOLD` | `low` | Highest risk merged unattended in `gated` |
| `PIPELINE_DEFAULT_MODEL` | `sonnet` | Model when no story/persona model |
| `USAGE_STATE_PATH` | `~/.claude/usage_state.json` | Where `check_usage` persists usage/paused state |
| `PIPELINE_MAX_CONCURRENT_AGENTS` | `3` | Cap on dispatched agents running at once (across all plans); `<=0` = unlimited. **Local-Ollama note:** with multiple local models in `MODELS`, set this to `1` to avoid Ollama swapping a different model into VRAM (dispatch_story will WARN when it detects a different model already loaded). Same-model concurrency is always safe. |
| `PIPELINE_MERGE_MAX_ATTEMPTS` | `3` | Merge error budget: how many ticks a failing `_merge_pr` (transient `gh`/`git`) is retried before the story is marked `failed` for human intervention |
| `PIPELINE_DISPATCH_MAX_ATTEMPTS` | `3` | Dispatch error budget: how many times a story whose launch keeps failing (raising `dispatch_story`, or an agent that produces no output) is retried before it is marked `failed` instead of looping forever |
| `PIPELINE_REWORK_MAX_ATTEMPTS` | `3` | Rework budget: how many times a `changes_requested` story is redispatched (with the reviewer's feedback) before it is `parked` for human review instead of looping through review↔rework |
| `PIPELINE_REVIEW_INCONCLUSIVE_MAX` | `2` | Inconclusive-review budget: how many consecutive non-rate-limited `UNKNOWN` verdicts (no parseable `VERDICT` line, or the reviewer-exception fail-safe) are retried on later ticks — without counting against `PIPELINE_REWORK_MAX_ATTEMPTS` or touching `review_feedback` — before the story is `parked` for human review instead of retrying forever |
| `PIPELINE_PLANE_MAX_ATTEMPTS` | `3` | Plane error budget: inline retries for a best-effort Plane state transition before the drop is recorded durably (Plane sync never blocks git work) |
| `PIPELINE_PAUSE_THRESHOLD` | `90` | `%` of the **session** window that trips the Claude usage gate |
| `PIPELINE_RESUME_THRESHOLD` | `70` | `%` the **session** window must drop below to clear the gate |
| `PIPELINE_WEEK_PAUSE_THRESHOLD` | `90` | `%` of the **week** window that trips the gate |
| `PIPELINE_WEEK_RESUME_THRESHOLD` | `70` | `%` the **week** window must drop below to clear the gate |
| `PIPELINE_BACKEND_DISPATCH` | `claude` | Backend for dispatch (coding) agents: `claude` \| `ollama` \| `lmstudio` \| `mlx` \| `local` \| `auto` (layered local-first with Claude fallback — see below) |
| `PIPELINE_BACKEND_REVIEW` | `claude` | Backend for the code-reviewer persona: `claude` \| `ollama` \| `lmstudio` \| `mlx` \| `local` |
| `PIPELINE_BACKEND_OVERLORD` | `claude` | Backend for overlord decisions: `claude` \| `ollama` \| `lmstudio` \| `mlx` \| `local` |
| `PIPELINE_BACKEND_PLANNER` | *(unset)* | Backend for the guided-decomposition planner (the in-story checklist + rework-feedback checklist role, `PIPELINE_DECOMPOSE`'s `mode="local"` path): `claude` \| `ollama` \| `lmstudio` \| `mlx` \| `local`. Unset means the planner mirrors whatever backend `dispatch` resolved to for that story (today's default) — set this to pin the planner to a specific provider independent of dispatch, e.g. dispatch on `ollama` with the planner on `mlx`. `mode="cloud"` is unaffected (always `claude`, see `PIPELINE_DECOMPOSE_CLOUD_MODEL`). |
| `PIPELINE_BACKEND_DECOMPOSE` | `claude` | Backend for the `decompose_plan` tool (turns a raw request into epics/stories JSON via the product-analyst persona): `claude` \| `ollama` \| `lmstudio` \| `mlx` \| `local`. Independent of the interactive `product-analyst` subagent (invoked via the `Agent` tool), which is always Claude and unaffected by this setting. |
| `PIPELINE_DECOMPOSE` | `off` | Guided decomposition (the tech-lead planner, see above): `off` \| `cloud` \| `local`. `cloud` authors the per-story checklist with Claude (`PIPELINE_DECOMPOSE_CLOUD_MODEL`); `local` authors it on the story's own local provider. Only ever runs for a local-family dispatch backend; best-effort (fails open to no checklist). Distinct from `PIPELINE_BACKEND_DECOMPOSE`, which is the `decompose_plan` *tool*, not the in-story planner. |
| `PIPELINE_DECOMPOSE_SCRATCHPAD` | `on` | Whether guided decomposition maintains the `.agent_scratchpad.md` cross-sub-step memory (`on` \| `off`). `off` runs the checklist-only ablation. No effect when `PIPELINE_DECOMPOSE=off`. |
| `PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV` | *(unset)* | Off by default: every `claude` subprocess call strips `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`/`ANTHROPIC_SMALL_FAST_MODEL`/`CLAUDE_CODE_USE_BEDROCK`/`CLAUDE_CODE_USE_VERTEX` from its environment so an interactive session's 3rd-party-provider redirect can't silently leak into dispatch/review/overlord. Set truthy only for a legitimate enterprise Bedrock/Vertex deployment that intentionally routes the CLI elsewhere — see "Claude backend provider isolation" below. |
| `PIPELINE_LOCAL_PROVIDER` | `ollama` | Wire protocol the `local` alias's `complete()`/`resource_status()` speak when a role is set to the generic `local` name: `ollama` \| `mlx` \| `lmstudio`. Naming the provider directly in `PIPELINE_BACKEND_<ROLE>` (`ollama`/`lmstudio`/`mlx`, RELIABILITY_PLAN.md T16) pins that provider for that role regardless of this setting — `local` stays a permanent back-compat alias (existing manifests persist `"backend": "local"`) and is the only name this variable actually affects. All three providers are working, live-validated implementations (including real tool-calling round trips and, for `lmstudio`, a full multi-turn review-loop convergence to a verdict). Neither `mlx` (targets `mlx_lm.server`) nor `lmstudio` (targets LM Studio's local server) has a per-request context-window control like Ollama's `num_ctx` — both send that value as `max_tokens` instead. `lmstudio`'s loaded-model check uses its own `/api/v0/models` (`state: loaded/not-loaded`), not Ollama's `/api/ps`, and LM Studio JIT-loads a model on its first request (~30s for a small model) rather than expecting it pre-loaded. **Does not yet affect `dispatch()`** — the coding-agent subprocess always talks to Ollama's native API regardless of this setting (`MODEL_PROVIDER_ABSTRACTION_PLAN.md` S3, deferred). **MLX/LM Studio model names have no `:` like Ollama tags do** — `_resolve_local_model` treats any model string without a `:` as a tier name, so a Hugging Face repo id (e.g. `mlx-community/Qwen2.5-1.5B-Instruct-4bit`, `google/gemma-4-e4b`) must be set via `PIPELINE_LOCAL_MODEL_DEFAULT`/`_OPUS`/`_SONNET`/`_HAIKU`, not passed as a raw `model=` value. |
| `PIPELINE_LOCAL_ENDPOINT` | `http://localhost:11434` | Ollama base URL for the `local` driver (it uses Ollama's native `/api/chat`, the only surface that accepts `num_ctx`). Point at a remote Ollama to use another box. |
| `PIPELINE_LOCAL_MODEL_DEFAULT` | `devstral:24b` | Local model used for any tier without its own override below |
| `PIPELINE_LOCAL_MODEL_OPUS` | — | Local model for the `opus` tier (falls back to the default) |
| `PIPELINE_LOCAL_MODEL_SONNET` | — | Local model for the `sonnet` tier (falls back to the default) |
| `PIPELINE_LOCAL_MODEL_HAIKU` | — | Local model for the `haiku` tier (falls back to the default) |
| `PIPELINE_LOCAL_MODEL_<PROVIDER>_<TIER>` | — | Provider-scoped tier override, e.g. `PIPELINE_LOCAL_MODEL_MLX_SONNET`, `PIPELINE_LOCAL_MODEL_OLLAMA_OPUS`. Checked **before** the provider-agnostic `PIPELINE_LOCAL_MODEL_<TIER>` above. Exists because two roles on two different local providers (e.g. review on `mlx`, dispatch on `ollama`) previously shared one global tier→model mapping meant for a single wire format/model namespace — a real correctness gap, not just an ergonomics one, since an MLX model path and an Ollama tag are different string shapes entirely. Falls back to `PIPELINE_LOCAL_MODEL_<TIER>`, then `PIPELINE_LOCAL_MODEL_DEFAULT`, when unset. |
| `PIPELINE_LOCAL_NUM_CTX` | `16384` | Ollama context window for local calls (sized to fit 100% on a 24GB M4 GPU; raising it risks a slow CPU/GPU split) |
| `PIPELINE_LOCAL_TEMPERATURE` | `0.3` | Sampling temperature for local model calls |
| `PIPELINE_MODEL_REGISTRY_PATH` | `<repo root>/model_registry.json` | Path to the model/role registry file — see "Per-role provider/model configuration" below. |

**Per-model tuning table.** `backend.py`'s `_LOCAL_MODEL_TUNING` dict holds
empirically-settled `temperature`/`num_ctx` overrides keyed by the *resolved
concrete model tag* (e.g. `gpt-oss:20b`), not the tier. An explicit
`PIPELINE_LOCAL_TEMPERATURE`/`PIPELINE_LOCAL_NUM_CTX` env var still always
wins over a table entry; a model tag with no entry falls back to the global
defaults above. This exists so a tuning finding travels with the model
instead of requiring the operator to remember to flip a global env var every
time the active local model changes. Currently populated:

| Model tag | `temperature` | `num_ctx` | Why |
|---|---|---|---|
| `gpt-oss:20b` | `0.3` | `32768` | 2026-07-03 A/B benchmark (`tests/benchmark/_runs/full_20260703_postfix` vs `temp_tune_20260703`, 15 cells each): `temperature=1.0` scored 6/15 success with 3 cells where the implementation never landed on disk at all; `temperature=0.3` scored 9/15 with only 1, at an unchanged 11/15 ground-truth-pass rate. |
| `PIPELINE_LOCAL_TIMEOUT_SECONDS` | `600` | Per-request timeout for local single-shot `complete()` calls |
| `PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS` | `900` | Legacy. Was the per-request timeout for the dispatch/review chat loop; since streaming landed this only seeds the harness boot log (`steps=… timeout=…s`). The live timeout is `LOCAL_AGENT_READ_SILENCE_SECONDS` below — kept set by `backend.py` for back-compat. |
| `LOCAL_AGENT_READ_SILENCE_SECONDS` | `180` | Per-chunk read timeout for the streamed `chat()`. Fires only on a genuine stall (no bytes for N s), not on a legitimately long generation that emits a chunk every ~1–2 s. Passed through from the shell/MCP env by `backend.py` (`**os.environ`). |
| `LOCAL_AGENT_CHAT_MAX_ATTEMPTS` | `3` | Retry attempts for a transient `chat()` failure (`httpx.TransportError` or 5xx). 4xx raises immediately. Passed through from the shell/MCP env. |
| `LOCAL_AGENT_CHAT_RETRY_BACKOFF` | `5` | Linear backoff seconds between chat retries (× attempt). Passed through from the shell/MCP env. |
| `LOCAL_AGENT_BASH_TIMEOUT_SECONDS` | `600` | Per-bash-command timeout in the dispatch loop, so a wedged build (e.g. a hung network index fetch) can't hang the agent. Passed through from the shell/MCP env. |
| `LOCAL_AGENT_READ_HEAVY_WINDOW` | `6` | Read-heavy guard window: this many consecutive non-mutating tool calls (reads / non-unique bash) triggers a nudge, then a park, so the loop can't burn the step budget on inspection. Passed through from the shell/MCP env. |
| `LOCAL_AGENT_READ_HEAVY_DISTINCT_WINDOWS` | `3` | Lenient post-nudge cap: after the read-heavy nudge, allow this many **all-distinct** read windows (each file read once) before parking. Total distinct-read ceiling = `READ_HEAVY_WINDOW + DISTINCT_WINDOWS * READ_HEAVY_WINDOW` (default 24). Tasks with many files to orient on (e.g. a multi-method test suite) may need to raise this — the live-`gh` probe had to set `=10` to let lru_cache reach its first commit (PROOF.md note #3). The strict-repetition park (re-reading an already-seen target) is unaffected; only the all-distinct exploration leash is tunable. Passed through from the shell/MCP env. |
| `PIPELINE_LOCAL_MAX_STEPS` | `40` | Max tool-call steps a local **dispatch** run takes before it parks (WIP-commits). **`PIPELINE_LOCAL_MAX_STEPS` is the input knob; `LOCAL_AGENT_MAX_STEPS` is transport-only and must not be set in the plist or shell** — `backend.py` re-reads `PIPELINE_LOCAL_MAX_STEPS` on every dispatch and writes the resolved value into `LOCAL_AGENT_MAX_STEPS` for the subprocess. Set this in `launchd/com.claude.pipeline.advance-scheduler.plist` to change overnight run behavior; `launchctl unload && launchctl load` to apply. |
| `PIPELINE_LOCAL_REVIEW_MAX_STEPS` | `20` | Max tool-call steps a local **review** takes before returning UNKNOWN (→ parks). Read in-process (not subprocess-spawned) — same input-knob contract as `PIPELINE_LOCAL_MAX_STEPS` and editable in the plist if you want one knob for both. |
| `PIPELINE_LOCAL_REVIEW_MODEL` | — | Concrete Ollama tag (e.g. `devstral:24b`) for **local review only** — asymmetric review. Both `software-engineer.md` and `code-reviewer.md` declare `model: sonnet`, so without this override dispatch and review resolve to the identical concrete model (a model reviewing its own work with identical weights). Only applied when the review backend is actually local-family (`local`/`ollama`/`lmstudio`/`mlx`, via `PIPELINE_BACKEND_REVIEW` or an explicit fallback); ignored for cloud review so a bare Ollama tag never leaks in as a bogus Claude `--model` value. |
| `PIPELINE_REVIEW_FALLBACK` | `off` | When Claude review hits repeated rate-limits, fall back to this backend inline: `local` \| `ollama` \| `lmstudio` \| `mlx` \| `off` (never fall back). Paired with `PIPELINE_REVIEW_FALLBACK_AFTER` (default `3`), the count of rate-limited attempts before falling back. |
| `PIPELINE_REVIEW_MAX_TOKENS` | `4096` | Output cap (passed as `--max-tokens`) for any Claude `complete()` call originating from `_run_reviewer`. Bounds the runaway-output failure mode where Claude emits a long findings list / PR body before the `VERDICT:` line; the redispatched agent has the diff and the file paths and does not need prose to navigate. Ignored when the review backend is `local` (Ollama caps via `num_ctx`). |
| `PIPELINE_SECURITY_REVIEW_MAX_TOKENS` | *fallback* | Same cap for the security-engineer pass on high-risk stories. Falls back to `PIPELINE_REVIEW_MAX_TOKENS` when unset, so the two can be tuned independently — the security pass typically produces shorter output (VERDICT only, no PR title/body), so a tighter cap is reasonable. |
| `PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL` | *(unset/off)* | When truthy (`1`), an acceptance-failing dispatch that still produced real work (new commits) is routed to the reviewer instead of straight to terminal `failed`, so the rework loop can re-dispatch it with feedback (bounded by `PIPELINE_REWORK_MAX_ATTEMPTS`). An empty-branch failure (no commits) stays `failed` — re-dispatching a stuck prompt won't help. The merge gate (`_reverify_acceptance`) still blocks any APPROVE'd-but-failing merge. |
| `PIPELINE_REWORK_ON_CI_FAIL` | *(unset/off)* | When truthy (`1`), a **definitive** merge-gate CI failure (`_ci_status` state `fail` — not `pending`, not `cancelled`, and not a transient rebase/push error) on an already-APPROVE'd branch is routed back to the implementer as rework feedback instead of retrying the unchanged branch toward terminal `failed`. Exists because the reviewer is acceptance-scoped and can APPROVE a story whose own committed test file is broken — the full-suite CI gate then blocks the merge with no way for the agent to fix it (observed 2026-07-17: gpt-oss `retry_backoff`/`token_bucket`, ground-truth-correct code abandoned over the agent's own self-contradictory test). Bounded by `PIPELINE_MERGE_MAX_ATTEMPTS` via the `merge_attempts` counter (which persists across the rework→review-APPROVE→merge-gate cycle, unlike `rework_attempts` which the review-APPROVE path resets) — `MERGE_MAX_ATTEMPTS` rework rounds, then the existing terminal-fail fall-through. Does **not** change what the CI gate checks (still the full suite, by design — see `_ci_status_stub`'s docstring on catching merged-but-wrong) — only what happens on a failure. |
| `PIPELINE_REVERIFY_FULL_SUITE` | `1` | For a story **without** an `acceptance` block, whether the pre-merge re-verification (`_reverify_acceptance`) runs the full worktree suite (`1`) or skips it (`0`). Stories with an `acceptance` block always re-verify against the scoped oracle regardless. |
| `PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE` | `1` | Rework budget for a story carrying a non-empty `acceptance` block — lower than `PIPELINE_REWORK_MAX_ATTEMPTS` because an oracle-backed story already has an objective, pre-verified correctness signal (it only reaches review after tests, including the oracle, pass); a reviewer that keeps finding beyond-oracle issues mostly spends cycles rather than changing the outcome, so a lower cap parks it for human review faster. Falls back to `PIPELINE_REWORK_MAX_ATTEMPTS` for any story without a truthy `acceptance` list. Superseded by `PIPELINE_REWORK_MAX_ATTEMPTS_ESCALATED` once a story is escalated (see below). |
| `PIPELINE_REWORK_MAX_ATTEMPTS_ESCALATED` | `3` | Rework budget for a story that has already been escalated to Claude (`story["escalated"]`), taking priority over `PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE` regardless of whether the story also carries an acceptance oracle. `_ORACLE`'s tight cap exists to converge *local* review fast; once escalation has already paid its cost (real Claude usage, and per 2026-07-04's benchmark validation, sometimes real wall-clock time if the Claude reviewer gets rate-limited), reusing that same cap just throttles Claude's shot at the same feedback for no benefit — 6 of 11 escalated cells in that run parked after exactly one post-escalation cycle. |
| `PIPELINE_LOCAL_MAX_RISK` | `low` | Highest story risk the `auto` router sends to the local agent: `low` \| `medium` \| `high`. Stories above this threshold go straight to Claude. Security-persona stories always go to Claude regardless of this setting. |
| `PIPELINE_STEP_CAP_FALLBACK_THRESHOLD` | `3` | Consecutive same-model step-cap interrupts before a story's `model` is switched to the plan's `local_model_fallback` (see below). Only takes effect on plans that set that manifest field. On a plan with **no** `local_model_fallback` set, the same threshold instead gates escalation **to Claude** under `PIPELINE_BACKEND_DISPATCH=auto` (see routing item 2 below) — a story with no fallback configured no longer hits the step cap indefinitely on the same model under `auto`; outside `auto` (explicit `local`/`claude`), behavior is unchanged. |

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
    tiering note in `Local_LLM_Port_Plan.md`. **Exception:** the additional
    security-engineer pass run for `risk: "high"` stories (`_run_security_reviewer`)
    always uses the Claude backend regardless of `PIPELINE_BACKEND_REVIEW` —
    unlike the ordinary code-reviewer pass, it is never routed local. This is
    distinct from (and in addition to) the `auto`-only, dispatch-side
    security-persona guarantee described below.
  - **dispatch** — a native-tool-calling **write** agent loop
    (`scripts/local_agent.py`, run as a subprocess: `create_file`/`str_replace`/
    `view_file`/`bash`/`checkpoint`/`done`, with a non-destructive editor, loop
    guard, and commit enforcement). `chat()` **streams** the Ollama response
    (per-chunk read timeout `LOCAL_AGENT_READ_SILENCE_SECONDS`) and **retries**
    transient failures (`LOCAL_AGENT_CHAT_MAX_ATTEMPTS`), so a single Ollama
    queue stall or network blip can't kill a run mid-iteration.

  The local driver deliberately does **not** use OpenHands — see
  `Local_LLM_Port_Plan.md` for the full investigation (why, and the model
  caveats: local dispatch is reliable on small/mechanical stories but has a
  reasoning ceiling, so keep `PIPELINE_RISK_THRESHOLD` conservative and let
  bigger work park or stay on Claude).

**`auto` — layered local-first dispatch:** `PIPELINE_BACKEND_DISPATCH=auto`
enables a layered routing strategy:

1. **A-priori (by story metadata):** before dispatching, the orchestrator checks
   story `risk` and `persona`. Stories with risk above `PIPELINE_LOCAL_MAX_RISK`
   (default `low`) or a `security-engineer` persona are sent directly to Claude
   without attempting local first.
2. **A-posteriori (escalation on failure):** all other stories start on the local
   agent. If the local run fails (tests don't pass), the orchestrator wipes the
   local worktree, resets the story, and re-dispatches it on Claude starting clean.
   A second failure on Claude is terminal (same behavior as today). The escalation
   flag (`story["escalated"]`) prevents infinite looping. The same escalation
   (`_escalate_to_claude`, same clean-slate teardown) also fires when a story
   keeps hitting the step cap on the same local model: see
   `PIPELINE_STEP_CAP_FALLBACK_THRESHOLD` and routing item 4 below — a
   repeated step-cap streak is treated as a local-model capability problem,
   the same way a test failure is. This only applies when the plan has **not**
   opted into `local_model_fallback` (item 4); the two are mutually exclusive
   with no chaining — a plan with `local_model_fallback` configured always
   stays on the local-fallback path, even past the threshold.
3. **Review-side escalation (local review can't converge):** under `auto`, a
   story that exhausts its rework budget (`PIPELINE_REWORK_MAX_ATTEMPTS[_ORACLE]`)
   or its inconclusive-review budget (`PIPELINE_REVIEW_INCONCLUSIVE_MAX`) escalates
   to Claude instead of parking for a human — `_escalate_review_to_claude` sets
   `story["backend"] = "claude"` and `story["escalated"] = True` with a fresh
   rework/inconclusive budget, now governed by the more generous
   `PIPELINE_REWORK_MAX_ATTEMPTS_ESCALATED` rather than the oracle cap (see
   below). Unlike (2), this does **not** wipe the worktree — the existing code
   is very often already correct (a local reviewer that can't converge doesn't
   mean the implementation is wrong), so Claude reviews/reworks the *same*
   worktree in place. A second exhaustion after escalation is terminal and
   parks for a human — there is no fallback past Claude.
4. **Local-model fallback, opt-in (never escalates to Claude):** a plan can
   set the top-level manifest field `local_model_fallback` to a concrete
   local model tag (e.g. `"glm-5.2:cloud"`). There is currently no
   `save_plan`/`ingest_plan` field, nor a `patch_story`/`set_story_status`-style
   tool, for this — it's plan-scoped rather than story-scoped, so today the
   only way to set it is to hand-edit `<plan>.manifest.json` directly (same
   scheduler-race caveat `patch_story`'s docstring warns about for story
   fields: do it between ticks, or expect to occasionally lose a race with
   `advance_all_plans`). It stays on the `local` backend throughout and gives a
   struggling local model one shot on a different local model before the
   terminal park/fail path, for teams that want a second local opinion
   without ever spending Claude:
   - **On test failure:** a story whose local run fails (tests don't pass)
     and hasn't already tried the fallback gets one retry on it —
     `_escalate_to_local_fallback_model` wipes the worktree/branch/journal for
     a clean start, same teardown as (2) but the backend never changes.
   - **On repeated step-cap interrupts:** a story that hits the step cap
     lands on `interrupted`, not `failed` — so it never reaches the
     test-failure path above and could otherwise loop on the same struggling
     model forever. `PIPELINE_STEP_CAP_FALLBACK_THRESHOLD` (default `3`)
     tracks consecutive step-cap interrupts on the same model and, once
     reached, switches `story["model"]` to the fallback for the next resume.
     Unlike the test-failure path, the worktree/journal are left in place —
     the resumed run picks up from its last WIP checkpoint instead of
     starting over. Once a story is already running on the fallback model,
     further step-cap hits are a no-op (there is no fallback past the
     fallback), and this path only ever applies to a `local`-backend story.

   On a plan that has **not** set `local_model_fallback`, a repeated
   step-cap streak takes a different path entirely: under
   `PIPELINE_BACKEND_DISPATCH=auto` it escalates the story to Claude instead
   (item 2 above), rather than looping on the same model forever. The two
   fallbacks never chain — whether a plan escalates to a different local
   model or to Claude is decided once, by whether `local_model_fallback` is
   set, not by trying one then the other.

To activate, set `PIPELINE_BACKEND_DISPATCH=auto` in your env (e.g.
`~/.claude.json` `mcpServers.pipeline.env`). Stories already carrying
`story["backend"]` take that value over the router (used internally to lock an
escalated story to Claude across ticks). Note that (3) only escalates the
*review/rework loop* — merge-gate exhaustion (`PIPELINE_MERGE_MAX_ATTEMPTS`, e.g.
a rebase conflict) and the high-risk `park-and-ping` floor are **not** escalated by
any of this: a merge conflict isn't a model-capability problem a stronger model
fixes, and the high-risk human-review floor is a deliberate safety gate, not a
capability gap — see Secure by Design.

Setting any `PIPELINE_BACKEND_*` var to a name that isn't registered raises
`NotImplementedError` naming the offending var.

**Claude backend provider isolation.** Every `claude` subprocess call
(`complete()`, `dispatch()`, `usage_probe_text()`) runs with a stripped
environment: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`,
`ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_USE_BEDROCK`, and
`CLAUDE_CODE_USE_VERTEX` are removed before the call, regardless of what the
invoking shell (an interactive session, or the scheduler's) has exported. This
matters because the pipeline MCP server is a child process of the top-level
`claude` session — without this isolation, pointing your *interactive* session
at a 3rd-party provider (a real, documented `claude` CLI feature) would
silently carry over into every `PIPELINE_BACKEND_<ROLE>=claude` dispatch/
review/overlord call, while the audit sidecar (`review_token_costs.jsonl`)
still reported `"backend": "claude"` regardless of what actually served the
request.

- `PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV` (default unset/off) restores full
  environment inheritance for legitimate enterprise Bedrock/Vertex
  deployments that intentionally route the `claude` CLI elsewhere. Leave this
  unset unless you know you need it — the default is deny-by-default per
  Secure by Design.
- `review_token_costs.jsonl` records both `"model"` (the requested tier, e.g.
  `"sonnet"`) and `"served_model"` (the CLI's own reported model string, from
  its `--output-format json` payload) on every structured `complete()` call —
  a `jq`-able trail that surfaces provider drift even for calls predating this
  isolation, or when the escape hatch above is set intentionally.
- `ClaudeCliDriver.verify_identity()` runs a one-time, cheap preflight
  (`claude -p "1+1" --model sonnet --output-format json`) confirming the
  served model's name genuinely starts with `claude-sonnet-`; the result is
  cached for the process's lifetime and folded into `resource_status()` —
  a confirmed mismatch blocks dispatch/review through the exact same gate a
  tripped Claude usage-pause already uses. An unchecked (never-probed)
  identity fails open, same as missing usage state.
- Independently, `complete()`'s own structured-output path raises
  `backend.ProviderIdentityMismatch` (a `RuntimeError` subclass) if a single
  call's served model diverges from its requested tier — `review_story`'s
  generic exception handler already treats this as an inconclusive review
  (fail-closed, never a false APPROVE) without any special-casing.

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
  `claude -p "/cost"`, computes `paused` with hysteresis (trip at the
  session/week `PAUSE_THRESHOLD`%, clear only once **both** windows drop below
  their `RESUME_THRESHOLD`%), and persists it to `USAGE_STATE_PATH`.
  The probe parses human-readable CLI output, which Claude Code can reword (and
  has). When a parse fails, `check_usage` falls back to the last reading;
  once that reading is older than `PIPELINE_USAGE_STALE_AFTER_SECONDS` (default
  1800) it stops trusting it and **fails the gate open** — the available
  default, so a permanent output change can't freeze the pipeline. Because
  failing open means spend is unguarded, that state is recorded loudly:
  `gate_blind`/`blind_since`/`consecutive_parse_failures` in `USAGE_STATE_PATH`,
  surfaced as the red dashboard banner above. (A `claude -p /cost` spawned
  *inside* a Claude Code session omits the percentages; the poller runs
  standalone, so it's unaffected.)
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

## Unattended operation & logs

The two background loops run under launchd (see `launchd/*.plist`):
`advance-scheduler` (`advance_all_plans()` every 5 min) and `usage-poller`
(`check_usage()` every 60s). Each writes stdout/stderr to a fixed file beside
the server (`advance-scheduler{,.err}.log`, `usage-poller{,.err}.log`).

- **HTTP log noise is capped.** `FastMCP`'s constructor sets the root logger to
  `INFO`, which the `httpx`/`httpcore` loggers inherit — so without intervention
  every HTTP call (notably the per-tick Ollama `/api/tags` reachability probe)
  logs an `INFO "HTTP Request: ..."` line. The server caps those two loggers at
  `WARNING` at import, so routine request chatter stays out of the logs while
  genuine HTTP failures still surface.
- **Rotation (recommended for long-running installs).** launchd does not rotate
  its `StandardError`/`StandardOut` files. Install the bundled `newsyslog` rule
  to bound them (keeps 5 × ~5 MB, bzip2-compressed):

  ```bash
  sudo cp launchd/pipeline-logs.newsyslog.conf \
          /etc/newsyslog.d/com.claude.pipeline.conf
  sudo newsyslog -nv   # dry-run: verify the rule parses and see what it'd do
  ```

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

See [`CLAUDE.md`](CLAUDE.md) for the full engineering standards (code quality,
testing, security, observability) and the mandatory agent workflow for
pipeline-tracked work (claim a story → TDD → detect the test runner → full
suite green → `review_story` → prompt before committing).

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
