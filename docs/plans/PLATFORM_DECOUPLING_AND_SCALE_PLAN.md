# Plan: Decouple the platform from Claude Code, and scale from single-host to multi-tenant

> Status: **In execution (last updated 2026-08-27).** W3a, W1a, W1b, W1c, W2,
> W3b, and `server-app-file-split` are all done and merged.
> `server-app-file-split` (22 stories, PRs #435-#457) landed since the prior
> update: `pipeline/server.py` is down from 5,466 to 992 lines, split into
> `store.py`/`service.py`/`merge.py`/`ingest.py`/`dispatch.py`/
> `review_orchestrator.py`/`story_status.py`/`advance.py`/`usage.py`; and
> `static/app.js` (2,730 lines) is now ES modules
> (`state.js`/`routing.js`/`api.js`/`render/*.js`/`comms.js`/`usage.js`/
> `main.js`), every file under 1,000 lines. `comms-ui-design-alignment` (Comms
> nav/hero/toast polish) also landed on top of the split modules.
> **New instance of the same scaling concern (#5 below), found 2026-08-27:**
> `scripts/local_agent.py` (1,723 lines) and `scripts/local_agent_oracle.py`
> (1,599 lines) were the two largest files in the repo, both over the
> 1,000-line guideline. **Partial pass done directly, same day**: the
> environment-derived config constants + TOOLS/HARNESS_RULES schemas (pure
> data) moved into `scripts/local_agent_config.py` /
> `scripts/local_agent_oracle_config.py`, bringing the two files to 1,530
> and 1,436 lines respectively — real but incomplete progress. `run_tool`,
> the chat/transport functions, and `_main_impl` remain in place: they read
> mutable module state (`CWD`, `chat`, `time`, several constants) that 40+
> tests monkeypatch directly on the module object, so moving them requires
> the same `_ServerRef`-proxy rigor `pipeline/service.py` applies, verified
> function-by-function — a scoped follow-up, not a same-session mechanical
> move.
> W4 (multi-tenant) remains deferred until there's a real second deployment to
> validate against. See "Suggested sequencing" below for the live state of
> each workstream. This doc exists to capture the target shape and the real
> scope of each move, so the workstreams can be sequenced deliberately rather
> than discovered mid-implementation.
>
> Supersedes/absorbs the two standalone idea notes: "dashboard env config" and
> "dashboard agent chatbot".

## Goals

1. **Claude Code is one client, not the entry point.** The primary way to create
   plans, dispatch work, and answer decisions should be a UI (chat + forms).
   Claude Code keeps working via MCP, unchanged, as one of several clients.
2. **The dashboard is a client too**, not a second reader of the pipeline's
   private file layout.
3. **Configuration is managed in the UI**, from one authoritative store.
4. **One codebase serves both** a single-user local install and a multi-user,
   multi-repo enterprise deployment — without forking.

---

## Current state (assessment)

The good news first, because it reframes the scope considerably.

### The orchestration core is *already* headless

`advance_all_plans()` (`pipeline/server.py:4101`) is the whole run loop, and
launchd invokes it as a bare Python call — no Claude Code, no MCP, no agent in
the loop:

```
launchd/com.claude.pipeline.advance-scheduler.plist.template:
  .venv/bin/python3 -c "import app.pipeline_mcp_server as p; p.advance_all_plans()"
  StartInterval 60
```

So Claude Code is **not** required for the autonomous loop today. What it *is*
required for is the human-facing half: authoring/ingesting plans, claiming and
dispatching a specific story, answering `request_decision`, inspecting status,
approving merges. That is the actual decoupling target — a much smaller and
better-defined surface than "decouple Claude Code from the harness" implies.

### Three distinct "Claude couplings" that must not be conflated

| # | Coupling | Where | Verdict |
|---|---|---|---|
| 1 | **Claude Code as control plane / UX** — the MCP client a human drives | `pipeline/server.py`'s 23 `@mcp.tool()` functions | **This is what we're replacing.** |
| 2 | **`claude` CLI as an execution backend** — runs dispatch/review/planner roles | `app/backend.py:189` `ClaudeCliDriver`, behind the `Backend` protocol | **Already abstracted. Keep.** Ollama/LM Studio/MLX already sit alongside it. |
| 3 | **Claude as escalation bottom-of-chain** — hardcoded policy, not plumbing | `_escalate_to_claude`, `_escalate_review_to_claude`, `_persona_requires_claude`, and `os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude")` in ~4 places | **Policy leak.** Should become a configurable escalation ladder, not a literal. |

Only #1 blocks the UI-entry-point goal. #3 is a smaller cleanup that matters for
a deployment where Claude may not be available or licensed at all.

### The MCP tool layer and the state machine are the same module

`pipeline/server.py` is 5,466 lines and mixes the `@mcp.tool()` decorated
entrypoints with the state machine, gating, dispatch, review, and merge logic
they call. The `PipelineService` and `Store`/`FileStore` seams W1a/W1b extracted
now exist, but the file itself has *grown* (~1,300 lines) since W1 landed as the
chat and dashboard workstreams piled behavior onto it — the `server-app-file-split`
workstream is the direct response. Extracting one is the single largest piece of work in this
plan.

Mitigating factor: the decomposition already started —
`pipeline/{persistence,concurrency,parsers,review,git_ops,ci,rebase,...}.py` are
extracted leaf modules, and `app/pipeline_mcp_server.py` is already a 32-line
re-export shim. The pattern is established; it just hasn't reached the tools.

### State is a directory of JSON files

Everything lives in `PLAN_DIR` (`~/.claude/plans`), one file per concern:

- `{plan}.manifest.json` — the authoritative story state machine
- `{plan}.notifications.log` — append-only text
- `{plan}.decisions.json` — append-only JSON array, read-modify-write
- `{plan}.{story}.journal.json` — checkpoint journal, read-modify-write
- `{plan}.lock` — flock target
- `.dashboard_ui_state.json` — dashboard-owned view prefs

Concurrency control is `fcntl.flock` per plan (`pipeline/concurrency.py:110`)
plus a `threading.local` reentrance set, and a blocking `_heavy_lock` that
serializes builds/tests. **This is correct and well-reasoned for one host and
one filesystem, and does not survive leaving either.**

### The dashboard is import-decoupled but storage-coupled

`app/dashboard.py` (1,008 lines, 31 route handlers) deliberately does *not* import
`pipeline_mcp_server` — it re-reads `PLAN_DIR`/`WORKTREE_ROOT` from env and
parses the file layout itself. That keeps the write surface out of its import
graph, but it means **the manifest's on-disk shape is a de facto public API with
two independent parsers.** It was read-only except one write
(`/api/plans/{name}/archive`, a view preference) until W3b rerouted the GET
handlers through `Store`/`PipelineService` (W3b-A2) and retired the local parse
helpers (W3b-A2b), and added the config-write surface (W3b-B1..B5) — so the
second parser is now retired and the dashboard reads/writes through the service.
No auth; binds `127.0.0.1` by default via `scripts/dashboard.sh`.

### Configuration has three sources that can silently disagree

`model_registry.json` (roles → provider/model), the launchd plist's
`EnvironmentVariables` block (~25 `PIPELINE_*` vars), and the MCP server env in
`~/.claude.json`. Plus plan-level `role_config` in each plan JSON. There is a
documented priority chain, but no single place to read or set the effective
value, and no validation that the three agree. This has already caused live
misconfiguration incidents.

---

## Workstream W1 — Extract a service core and put an API in front of it

**The keystone.** Everything else depends on it.

### Target shape

```
                 ┌───────────────────────────────┐
                 │  clients                      │
                 │  • Web UI (chat + forms)      │
                 │  • Claude Code (MCP)          │
                 │  • CLI / CI                   │
                 └──────────────┬────────────────┘
                                │
              ┌─────────────────┴──────────────────┐
              │  adapters (thin, no logic)         │
              │  • FastAPI HTTP + SSE/WebSocket    │
              │  • FastMCP stdio  (existing tools) │
              └─────────────────┬──────────────────┘
                                │
              ┌─────────────────┴──────────────────┐
              │  PipelineService                   │
              │  (state machine, gating, policy)   │
              └──────┬──────────────────┬──────────┘
                     │                  │
        ┌────────────┴──────┐  ┌────────┴─────────────┐
        │ Store             │  │ Backend / Runner     │
        │ (files → DB)      │  │ (claude/ollama/...)  │
        └───────────────────┘  └──────────────────────┘
```

### Steps

1. **Define `PipelineService`** — a plain class whose methods are today's 23
   tools, taking/returning typed objects instead of MCP-shaped dicts. Move the
   bodies; leave the `@mcp.tool()` functions as one-line delegations. This is
   mechanical but touches the largest file in the repo.
2. **Introduce a `Store` protocol** — `get_manifest`, `update_story`,
   `append_decision`, `append_journal`, `list_plans`, plus a
   `transaction(plan)` context manager that today wraps `_plan_lock`. Ship
   `FileStore` as the only implementation initially, behaviorally identical.
3. **Add the HTTP adapter** — FastAPI, same operations as the MCP tools, plus a
   streaming channel (SSE) for live status so the UI doesn't poll.
4. **Keep the MCP adapter passing its existing tests unchanged.** That's the
   regression bar for the whole workstream.

### Scope signal

Steps 1–2 are large-but-safe refactors — high test-count, low behavioral risk, and
the existing 2,300-test suite is the safety net. Step 3 is genuinely new surface.
This should be split across many small stories (the extraction has an established
precedent to follow in `PIPELINE_MCP_DECOMPOSITION_PLAN.md`).

### Risk to watch

The test suite monkeypatches *heavily* against module-global bindings
(`p.PLAN_DIR`, `p._count_in_progress_agents`, `pipeline_persistence._notify_user`
— ~50 tests on that last one alone). Moving free variables into instance state
will break patch targets en masse. The decomposition plan's "Option B" pattern
(re-export the binding so patches still land) is the mitigation, and it must be
applied deliberately rather than discovered per-test.

---

## Workstream W2 — The chat entry point

> **DONE 2026-08-20** — plan `W2_CHAT_ENTRY_POINT_PLAN`, 9/9 stories merged
> (W2-01..W2-06, PRs #391-#398 + #406; W2-02 was split into W2-02a..d during
> rework for local dispatch). The `chat` role is in `model_registry.json`;
> `ChatService` (`app/chat.py`) runs the agent loop with a `TOOL_CALL`/
> `TOOL_RESULT` parsing protocol; read-only ops tools, plan-authoring tools
> (wired to `POST /api/decompose`), and decision-answering tools (`POST
> /api/plans/{plan}/decisions`) are registered; `POST /api/chat` is mounted
> into `app/dashboard.py`. W2-06 was the security gate: chat tool calls route
> through the HTTP API with no `PipelineService` backdoor, and `approve_merge`/
> `set_story_status`/the `risk` field are excluded from the chat tool registry
> so a prompt-injected chatbot cannot widen its own authority. Follow-on
> hardening (`chat-security-hardening`, `chat-ingest-risk-lock`) landed in
> PRs #402-#407. The direct-repair worktree file-read/propose-patch/apply
> surface this doc's open question raised is **not** part of W2 and remains a
> scoped follow-on (see Open questions).

The UI chatbot is **a client of W1's API**, and its own agent loop. It is not a
new orchestrator, and it must not grow its own copy of the state machine.

### What it actually needs to do

Only three things, in order of value:

1. **Plan authoring** — conversationally decompose a goal into epics/stories, and
   call `ingest_plan`. This is what `decompose_plan` already does headlessly;
   the chatbot is the interactive front-end to it, letting the user iterate on
   the plan before ingest.
2. **Operational Q&A and control** — "what's blocked?", "why did s4 fail?",
   "retry s6 with more context" → reads status/journals/logs, calls
   `dispatch_story`/`interrupt_story`/`patch_story`.
3. **Decision answering** — surface `request_decision` items and record the
   answer via `list_decisions`/the decision log.

### Key design constraints

- **Reuse the role registry.** The chatbot's own model is just another role
  (`chat`) in `model_registry.json`, resolved through `role_registry.resolve_role`.
  It can run on Claude, or on a local model, per deployment — that's what makes
  it a *decoupling* rather than a re-coupling.
- **The chatbot must not be in the critical path.** The launchd/scheduler loop
  keeps running with the chat service down. Chat is control + observability, not
  execution.
- **Tool surface = the HTTP API.** The chatbot calls the same endpoints the UI
  buttons call. No privileged back door, so every action is auditable through one
  path.
- **Confirmation gates stay server-side.** `risk=high` merge gating and
  `PIPELINE_AUTONOMY` are enforced in `PipelineService`, not in the chat prompt.
  A prompt-injected chatbot must not be able to widen its own authority.

### Scope signal

Moderate — but only *after* W1. Attempting it against the MCP-stdio surface
directly would mean either embedding an MCP client in the web service or
duplicating file parsing, both of which are dead ends.

---

## Workstream W3 — Dashboard decoupling + configuration UI

> **DONE 2026-08-24** — plan `w3b-dashboard-config-ui`, 11/11 stories merged
> (PRs #421-#432). W3b-A1a/A1b added `Store`/`PipelineService` read accessors
> (notifications, decisions, manifest, journal, story log, worktree file);
> W3b-A2/A2a/A2b rerouted every dashboard GET handler through the service and
> retired `app/dashboard.py`'s local `PLAN_DIR` parse helpers (with a regression
> guard); W3b-B1/B2/B3 added the config-write service + HTTP endpoints for
> global role defaults, per-plan `role_config`, and per-story `backend`;
> W3b-B4a/B4b shipped the frontend configuration view (HTML/CSS + app.js
> fetch/render/edit); W3b-B5 was the security-engineer gate (boundary
> validation + negative tests). The second manifest parser is retired — the
> on-disk layout is private again — and config is editable in the UI with
> effective-value + provenance display.

### Decoupling

Point the dashboard at W1's HTTP API instead of `PLAN_DIR` globs. This retires
the second manifest parser and makes the on-disk layout private again — which is
a precondition for ever changing it (see W4's move to a database).

The current read-only-by-design constraint was the right call while it shared a
filesystem with the orchestrator. Once it goes through the API, the constraint
should be restated as *"the dashboard has no privileged access"* rather than
*"the dashboard cannot write"* — writes then flow through the same gated service
methods every other client uses.

### Configuration UI

Collapse the three config sources into one store, exposed and editable in the UI:

- **Global defaults** — provider/model per role, concurrency caps, timeouts,
  autonomy level, thresholds. Today: `model_registry.json` + plist env.
- **Per-plan overrides** — today's `role_config` block.
- **Per-story overrides** — today's `backend` field.

Requirements that matter:

- **Show the *effective* value and where it came from.** The single most useful
  feature here is not editing — it's `PIPELINE_LOCAL_MAX_STEPS = 60 (from
  launchd plist, overriding registry default 40)`. Most past config incidents
  were "two sources disagreed and nobody could see which won."
- **Validate on write** — `role_registry.RoleRegistryError` already fails closed
  on a model not declared under `providers.*`; the UI should surface that
  pre-save rather than at next dispatch.
- **Flag env vars that are silently ignored.** `backend.py` already warns at
  import for six transport-only vars that have no effect; the UI should show the
  same warnings, since nobody reads import-time logs.
- **Know what needs a restart.** Plan `role_config` takes effect immediately;
  plist env does not. The UI must say which.

### Scope signal

Small-to-moderate, and it's the highest value-per-line item in this document.
Partially doable *before* W1 completes — the "effective config + provenance"
read-only view needs no service extraction.

---

## Workstream W4 — Local vs. enterprise

The honest framing: the current system is a **well-built single-tenant,
single-host appliance**. Most of its cleverness is *about* being single-host
(flock, pid liveness, a shared heavy-build lock, local worktrees, local GPU).
Enterprise is not a config flag on this; it's a second deployment topology that
the W1 seams make expressible.

### What changes, dimension by dimension

| Dimension | Local (today) | Enterprise target | Blocking issue today |
|---|---|---|---|
| **State** | JSON files in `PLAN_DIR` | Postgres | Read-modify-write on `.decisions.json`/journals is not multi-writer safe; flock doesn't span hosts. |
| **Locking** | `fcntl.flock` per plan | Row-level locks / advisory locks in DB | `pipeline/concurrency.py:110` is filesystem-bound by construction. |
| **Slot accounting** | Glob every manifest + `os.kill(pid, 0)` | Worker leases with TTL heartbeats | `_count_in_progress_agents` proves liveness with a host-local pid. Meaningless across hosts. |
| **Scheduler** | One launchd job, 60s interval | Leader-elected, or a queue with N consumers | Two schedulers on one `PLAN_DIR` would double-dispatch: the global slot count is read *outside* the per-plan lock. |
| **Workspace** | Git worktrees under `WORKTREE_ROOT` | Per-story ephemeral container/volume | Worker must be on the host that has the repo checkout. |
| **Execution** | Local Ollama/MLX + `claude` CLI | Pooled inference endpoints w/ queueing | Local inference is one shared, memory-bound resource — see scaling section. |
| **Identity** | None (single user, `127.0.0.1`) | AuthN + per-plan RBAC + audit | No auth anywhere; `dispatch_story` can run arbitrary code in a repo. |
| **Tenancy** | One `REPO_ROOT` per plan | Repo/org scoping, credential isolation | `REPO_ROOT` env + plan field; `gh`/git creds are ambient user creds. |
| **Logging** | Text files, newsyslog rotation | Structured JSON → collector | `dashboard.log` is 9.5 MB, `mlx-server.log` 7 MB, `usage-poller.err.log` 1.6 MB. No correlation IDs. |

### Story status tracking

The manifest is a *state machine*, not an event log — it records where a story
*is*, and history is scattered across the journal, notifications log, decisions
log, `agent.log`/`review.log` in the worktree, and the dashboard's log endpoint.
For enterprise, invert this: **an append-only event stream is the source of
truth, and the manifest becomes a projection.** That buys, in one move:

- audit ("who dispatched this, when, on what model, at whose approval")
- multi-writer safety (append-only, no read-modify-write races)
- live UI updates (stream events to the browser instead of polling)
- the retro/failure-mode analysis this project already does by hand, queryable

This is a bigger change than it looks, but it's also the thing that makes the
50-and-counting documented failure modes analyzable rather than archaeological.

### Logging

Concretely, and largely independent of everything else:

- Structured JSON logs with a stable field set (`plan`, `story_key`, `attempt`,
  `role`, `provider`, `model`, `correlation_id`).
- **A correlation ID minted at dispatch and carried through** dispatch → review →
  rework → merge, including into the agent subprocess env so `agent.log` lines
  join up with orchestrator lines.
- Per-story log files rather than one 9.5 MB append target.
- Retention/rotation as policy, not a newsyslog afterthought.

The project's own CLAUDE.md already mandates structured logging with correlation
IDs. The orchestrator does not currently meet its own standard.

**Partial progress (2026-08-14):** `event-driven-pipeline-phase3` (12/12
stories, PRs #318-#346) put `_notify_user` events onto a structured,
process-wide bus with a file-log sink and a JSONL sidecar, and the dashboard
now serves/dedup-collapses/filters those as structured records, including
per-story rendering. That's a real piece of "structured logs with a stable
field set," but it's notification events only — it doesn't mint a
correlation ID or carry one through dispatch → review → rework → merge, and
the dispatched agent's own `agent.log` subprocess output is still off the
bus. This bullet stays open.

---

## Are there scaling concerns with the current runtimes?

Yes — five, in rough order of how soon they bite.

### 1. Local inference is a hard serialization point (bites first)

`PIPELINE_MAX_CONCURRENT_AGENTS` is **1** in the shipped plist, and that isn't
conservatism — it's the memory ceiling. A 24 GB machine fits roughly one ~20B
model plus its context, and `_heavy_lock` exists because three concurrent cold
builds already pushed the box to 33 GB of pressure. **Throughput is one story at
a time, and no amount of orchestration improvement changes that** without
separating the inference tier from the orchestration tier. Enterprise means a
shared inference service (vLLM/TGI-class, batching, multiple GPUs) with the
pipeline as a client — at which point `MAX_CONCURRENT_AGENTS` becomes a real
tunable rather than a hardware constant.

### 2. The scheduler is a single non-elected process with a 60s floor

`StartInterval 60` polling everything sequentially: `advance_all_plans` iterates
plans in sorted order, and one slow plan delays every plan behind it. Latency
floor is 60s + the serial tick. It scales to maybe tens of plans; it does not
scale to hundreds, and there is no safe way to run a second scheduler.

### 3. Slot accounting is O(plans) per check, and pid-based

Every dispatch decision globs and JSON-parses every manifest in `PLAN_DIR`.
Fine at ~20 plans, wasteful at ~500, and correctness — not just cost — breaks the
moment a worker is on another host, because `os.kill(pid, 0)` is host-local.
Leases with heartbeats replace both properties.

### 4. Read-modify-write on shared JSON

`_append_decision` and `_append_journal` do read → append → atomic-write. The
atomic write prevents *torn* files, not *lost updates* — two writers between read
and write silently drop one record. Today the `_plan_lock` makes this mostly
moot; it stops being moot with any concurrency the flock doesn't cover.

### 5. Large files are themselves a scaling limit — recurring, not one-off

Not runtime scaling — *change* scaling. A file every workstream has to touch is
the hardest kind for a local model to edit safely (documented repeatedly in the
failure-mode log), and it's the reason a second adapter can't be added cheaply.

**DONE 2026-08-25** — `pipeline/server.py` had grown to 5,466 lines (from
chat/W2 and dashboard/W3b piling behavior onto it rather than into new
modules); the `server-app-file-split` plan (22 stories, PRs #435-#457) split
it into `store.py`/`service.py`/`merge.py`/`ingest.py`/`dispatch.py`/
`review_orchestrator.py`/`story_status.py`/`advance.py`/`usage.py`, all under
1,000 lines, plus split `static/app.js` (2,730 lines) into ES modules the
same way.

**New instance, found 2026-08-27:** `scripts/local_agent.py` (1,723 lines) and
`scripts/local_agent_oracle.py` (1,599 lines) — the dispatched agent's own
tool-calling loop and its acceptance-oracle counterpart — were the two
largest files in the repo, both well past the 1,000-line guideline, despite
already having several helper modules split out (`local_agent_guards.py`,
`local_agent_repair.py`, `local_agent_oracle_guards.py`,
`local_agent_oracle_repair.py`, `pipeline/local_agent_common.py`).

**Partial pass done directly, same day:** the config constants + tool
schemas — pure data, no function bodies — moved into
`scripts/local_agent_config.py` / `scripts/local_agent_oracle_config.py`,
landing at 1,530 / 1,436 lines. Verified safe with the full suite: one
regression surfaced (tests that `exec_module` the file fresh after mutating
`os.environ`, expecting env-derived constants to recompute, got a stale
cached copy of the new config module instead) and was fixed by having each
harness evict its config module from `sys.modules` before importing it, plus
one source-scan test updated to check both files. Full suite green after
(5,649 passed, matching baseline) — this is the kind of behavioral edge a
"just move the code" pass can silently miss without running it.

**Not done, and why:** `run_tool` (~280 lines), the chat/transport functions,
and `_main_impl` (~600 lines) all remain in place. Every one of them reads
mutable module-level state (`CWD` alone is monkeypatched 263 times across the
test suite; `chat`, `time.sleep`, `_measured_chars_per_token`, and several
constants are also directly patched) that a real reader/writer must resolve
against the *live* `local_agent`/`local_agent_oracle` module object, not a
snapshot captured at the new leaf module's import time. Moving them requires
converting every such reference to a live-lookup proxy — the same
`_ServerRef` pattern `pipeline/service.py` already uses for exactly this
problem — applied function-by-function and verified against the full suite,
which is the same rigor the 22-story `server-app-file-split` epic spent on
`pipeline/server.py`. That's a scoped, reviewable follow-up, not a
same-session mechanical move; forcing it through unreviewed risks a subtle
regression in the production dispatch loop that only a specific monkeypatch
combination would catch. This class of file — the one every future feature
keeps adding a branch to — needs a standing size check, not a one-time fix,
or it silently regrows either way.

### Not a concern

FastAPI/uvicorn, the file layout at local scale, flock, and the `Backend`
protocol are all appropriately sized for what they do now. The problem isn't that
the current runtimes are badly built — it's that they're built for *exactly one
host*, deliberately and consistently. That assumption is load-bearing in about
six places, and all six are listed above.

---

## Suggested sequencing

The dependency order is fairly rigid:

1. ~~**W3a — effective-config view (read-only, with provenance).** No
   prerequisites, immediate payoff, retires a live class of incident.~~
   **DONE 2026-08-09** — plan `w3a-effective-config-provenance`, PRs
   #247-#258 (`pipeline/config_provenance.py`, `get_effective_config` MCP
   tool, `/api/config` dashboard endpoint).
2. ~~**W1a — extract `PipelineService`**, MCP tools become delegations. The
   keystone; nothing else is cheap before it.~~ **DONE 2026-08-12** — 22
   stories, PRs #263-#286.
3. ~~**W1b — `Store` protocol** with `FileStore` as the only implementation.~~
   **DONE 2026-08-15** — 20/20 stories, PRs #315-#349.
4. ~~**W1c — HTTP adapter + event stream.**~~ **DONE 2026-08-17** — 9/9
   stories, PRs #350, #360-#366 (W1c-01 wires `PipelineService` into
   `app/dashboard.py`'s import graph; W1c-02..08 add POST routes per
   operation group, all delegating to the same singleton; W1c-09 adds a
   live SSE tail of a plan's `notifications.jsonl`). Extends the existing
   `app/dashboard.py` FastAPI app rather than standing up a second service
   (decided 2026-08-15: simplest for a single local-install process; a
   split-service topology remains open for W4 enterprise). No auth story
   included — write routes trust the same localhost-only bind
   (`DASHBOARD_HOST=127.0.0.1` default) the dashboard already relies on;
   real auth is W4's job.
5. ~~**W2 — chat entry point** on the HTTP API.~~ **DONE 2026-08-20** — plan
   `W2_CHAT_ENTRY_POINT_PLAN`, 9/9 stories merged (PRs #391-#398 + #406;
   W2-02 split into W2-02a..d for local dispatch). The `chat` role,
   `ChatService` + agent loop, read/ops/plan-authoring/decision tool
   registries, `POST /api/chat` + `POST /api/decompose` + `POST
   /api/plans/{plan}/decisions`, and the W2-06 security gate (all tool calls
   route through the HTTP API; `approve_merge`/`set_story_status`/`risk`
   excluded from the chat registry) all landed. Follow-on hardening in PRs
   #402-#407.
6. ~~**W3b — dashboard reads the API**, config becomes editable.~~ **DONE
   2026-08-24** — plan `w3b-dashboard-config-ui`, 11/11 stories merged (PRs
   #421-#432). Dashboard GET handlers reroute through `Store`/`PipelineService`
   and the local `PLAN_DIR` parse helpers are retired (A1/A2); the config-write
   service + HTTP endpoints + frontend config view landed (B1-B4); B5 was the
   security-engineer gate. The second manifest parser is gone; config is
   editable in the UI with effective-value + provenance.
7. ~~**`server-app-file-split` — split the two monolith files.**~~ **DONE
   2026-08-25** — 22 stories, PRs #435-#457. `pipeline/server.py` (was 5,466
   lines) and `static/app.js` (was 2,730) are both now under 1,000 lines per
   module — see the top-of-doc status note and scaling concern #5.
8. **Re-split `scripts/local_agent.py`/`local_agent_oracle.py`.** **PARTIAL,
   2026-08-27, direct (no pipeline)** — config constants + tool schemas
   extracted (1,723→1,530 / 1,599→1,436 lines); `run_tool`/transport/
   `_main_impl` remain, blocked on the same `_ServerRef`-proxy rigor
   `server-app-file-split` used, applied per-function. See scaling concern
   #5 above for detail. Scoped follow-up, not force-completed same-session.
9. **W4 — enterprise topology** (`PostgresStore`, leases, auth, structured logs),
   only where there's a real second deployment to validate against. Building it
   speculatively against an imagined tenant is exactly the over-engineering the
   project's own standards warn about.

Steps 1–6 are worth doing even if enterprise never happens: they're what make the
system usable without a Claude Code session open, which is the stated goal. Steps
7–8 are the change-scaling debt those workstreams accrued, and are worth doing on
the same grounds — it's what keeps the files every future workstream touches
editable.

---

## Relationship to `MATURITY_AND_UNIQUENESS_PLANS.md`

Substantial overlap. This doc is the *design detail* for several items that the
maturity doc deliberately records as bare TODOs ("no design detail yet").

| Maturity item | Here | Relationship |
|---|---|---|
| **B3** Scale & multi-tenancy | W4 | Nearly the same scope. W4 supplies the mechanics B3 leaves open: store, leases vs. pid liveness, leader election, the six load-bearing single-host assumptions. |
| **B4** Writable dashboard / control plane | W3b | The same item. B4's framing ("explicit, audit-logged action surface") is the right one and is adopted here. |
| **A2** Shrink the config surface | W3a | The same problem from opposite ends: A2 *reduces* the number of sources; W3a *makes the effective value and its provenance visible*. |
| **B1** Remote execution backend | W4 (execution/workspace rows) | B1's remote-exec is the prerequisite that relieves scaling concern #1 (local inference ceiling). |
| **A4** Externalize per-machine assumptions | W4 | Same single-host bias; different motivation (adoption vs. scale). |

### Where the two docs disagree, and what to do about it

- **The service extraction (W1) is missing from the maturity plan entirely.**
  B4 and most of B3 were listed as if independently startable. They were not:
  both needed a `PipelineService` seam that did not exist until W1a/W1b landed
  (2026-08-12/15) — the `@mcp.tool()` entrypoints and the state machine were
  the same module (then 4,132 lines; now 5,466). That seam now exists, B4
  (writable dashboard) is DONE via W3b, and B3's prerequisite is satisfied —
  what remains under B3 is the genuine multi-tenant work (W4), not the
  refactor. The maturity doc records this as a prerequisite line under both
  B3 and B4.
- **B1's "abstract the agent runner" conflates two axes.** The *inference
  provider* axis is already abstracted (`Backend` protocol; Claude/Ollama/
  LM Studio/MLX). The *harness* axis (Claude Code vs. Codex vs. Aider vs.
  Goose) is not, and lives in a different seam. Split into two bullets there.
- **Ordering conflict — RESOLVED 2026-08-06.** The maturity plan's suggested
  order (A1 → A3 → A2 → B1 → B5 → B2 → B4 → B6) puts B4 second-to-last and
  omits B3 from the sequence, deferring the UI-entry-point goal behind six
  other workstreams. **Decision: this plan's own sequencing governs.** With
  A1/A2 closed and A3 wound down to two items that are either tabled (CI-green
  release tag, blocked on the GHA billing cap) or themselves blocked on this
  plan's W4 structured logging, the next work after A3 is **this doc's
  sequence**: ~~W3a (effective-config+provenance view, no prerequisites)~~
  **DONE 2026-08-09, PRs #247-#258** → ~~W1a (extract `PipelineService`, the
  keystone)~~ **DONE 2026-08-12, PRs #263-#286** → ~~W1b (`Store` protocol)~~
  **DONE 2026-08-15, PRs #315-#349** → ~~**W1c (HTTP adapter)**~~ **DONE
  2026-08-17, PRs #350, #360-#366** → ~~**W2 (chat entry point)**~~ **DONE
  2026-08-20, 9/9 stories, PRs #391-#398 + #406** → ~~**W3b (writable
  dashboard — closes B4)**~~ **DONE 2026-08-24, 11/11 stories, PRs #421-#432**
  → ~~**`server-app-file-split` (split the two monolith files)**~~ **DONE
  2026-08-25, 22 stories, PRs #435-#457** → **re-split
  `local_agent.py`/`local_agent_oracle.py` (same concern recurring; config
  extracted 2026-08-27, run_tool/`_main_impl` split remains as a scoped
  follow-up)** → W4 (multi-tenant, closes B3), with B1
  (sandboxing) and B5 (export the moat) picked up after the service seam
  exists rather than before it — the seam now exists (W1a/W1b landed), so
  B1/B5 are unblocked whenever they're prioritized ahead of W4.
  Rationale: B1/B5 don't unblock anything else, while W1 was the single
  prerequisite blocking B3, B4, and the UI-entry-point goal simultaneously —
  front-loading it retired the most dependent work fastest.

### Items each doc has that the other should borrow

- **From maturity → here:** B4's *stuck-agent / wedge detection* as a
  first-class health signal belongs in W4. `os.kill(pid, 0)` cannot detect a
  zombie worker, and lease-based liveness (W4) is the natural place to fix it.
- **From here → maturity:** A3's "bound the failure-mode discovery rate" is
  trending the wrong way (19 → 50+). W4's structured logging with a correlation
  ID carried dispatch → review → rework → merge is the tooling that makes that
  trend analyzable rather than archaeological — every retro today is
  hand-reconstructed from five separate log files.

---

## Open questions

- ~~**Does the chat entrypoint need to replace Claude Code for *implementation*
  supervision, or only for planning and control?**~~ **RESOLVED 2026-08-20** —
  yes, chat must cover direct-repair of a stuck worktree, not just planning and
  control. This is a substantially bigger ask than W2's read/control tool
  registry: it needs a worktree file-read + propose-patch + apply HTTP surface
  (no such route or chat tool exists yet), and — because it's a prompt-reachable
  arbitrary-write path — it needs its own security review per this doc's
  existing constraint that "a prompt-injected chatbot must not be able to widen
  its own authority." Sequencing: ship the Comms UI (nav promotion + toasts)
  first since it's scoped against tools that already exist; scope the
  direct-repair tool surface as its own follow-on workstream with a
  security-engineer gate before any implementation stories are written.
- ~~**Single binary or split services?**~~ **RESOLVED for local 2026-08-15**
  — W1c decided to extend the existing `app/dashboard.py` FastAPI app rather
  than stand up a second service (simplest for a single local-install process;
  chat's `POST /api/chat` and the config routes mount into the same app).
  The enterprise half (split services) stays open and is a W4 decision, not a
  W1c one — deferred until there's a real multi-host deployment to validate
  against.
- **How much history to keep?** Event-sourcing story state is compelling but
  unbounded. Retention policy should be decided before, not after.
- **Auth model for enterprise** — is a plan owned by a user, a team, or a repo?
  This determines the RBAC shape and is hard to retrofit.
