# Reference

This is a detailed reference for Fagan's quickstart guide (README.md). It contains all sections that were moved from README.

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
  `litellm` \| `auto`) pins its dispatch provider from the plan itself, independent of the
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
   `acceptance` block still run the full suite as before.
When a lint signal is detected (`detect_lint_command`), a story that passes its tests but fails lint is also routed to `failed`, not `tests_passed`; the result is recorded in `story['last_lint_check']` alongside `last_test_check`.
When `PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1`, an acceptance-failing dispatch that
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
  Separately, `review_story` only ever reviews a story whose status is
  `tests_passed` — its sole legitimate entry state, matching the gate
  `advance_pipeline` itself applies before ever calling it. A call on a story
  in any other state (`done`, `pr_open`, `parked`, `changes_requested`,
  `in_progress`, missing/`None`, ...) is a stale or duplicate call — most
  commonly a second `advance_pipeline`/`advance_all_plans` tick racing an
  already-completed review→merge→cleanup cycle for the same story — and is a
  no-op: it returns `{"ok": True, "status": <unchanged>, "skipped":
  "not_reviewable_state"}` immediately, without touching the worktree,
  invoking the reviewer backend, or writing the manifest. This guard runs
  before the `last_reviewed_sha` check above, so a stale call never reaches a
  removed/cleaned-up worktree at all.

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
todo ──dispatch──► in_progress ──tests+lint pass──► (review) ──► pr_open ──merge?──► done
   ▲                     │   │                       │                  └park──► parked
   │                     │   └──tests/lint fail──► failed └─REQUEST_CHANGES─► changes_requested
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

## Patch review & apply (WAP-9/WAP-10)

The chat model may PROPOSE a unified diff against a stuck story's
worktree; only the ui-origin-authenticated human may review and APPLY it.
All three routes are gated by `X-Pipeline-Origin`: review and apply are
UI-only, while propose is also chat-reachable by design (the chat tool
`propose_patch` drives it):

| Route | Origin | Effect |
|---|---|---|
| `POST /api/worktree/patch/propose` | `chat` or `ui` | Validate + store the proposed diff server-side; mint a `wp-` patch id and an HMAC confirmation token bound to `patch_id` + `diff_hash`. The token is withheld from chat-origin callers (the human must supply it). |
| `GET /api/worktree/patch/{patch_id}` | `ui` only | Return the full stored record for human review — `diff_text`, `paths`, `added_lines`, `status`, timestamps and the `confirmation_token` (re-derived via `worktree_patch.confirmation_token_for`). Read-only; responds `Cache-Control: no-store` because the body carries a live credential. Unknown/expired ids are `404 "no such patch"`. |
| `POST /api/worktree/patch/{patch_id}/apply` | `ui` only | Apply the SERVER-STORED record. The body is `ApplyPatchRequest` — exactly one field, `confirmation_token: str` — so a forged `unified_diff` in the body is inert: the diff applied is always the stored record, never anything the caller sends. Engine refusals map to `HTTPException(result["status_code"], result["error"])` (wrong token `403`, active story `409`, already applied `409`); a failed apply leaves the record pending and retryable. |

Neither new route is registered as a chat tool, and the `k-` API-key
pass-through stays route-exact on `/api/chat/stream` — a `k-`-prefixed key
on any of these routes is `401` like anywhere else.

Refused review/apply requests are logged by `app.dashboard` with identifiers only -- route, truncated `patch_id`, an origin class (`absent`/`chat`/`ui`/`other`), and for engine refusals the plan, story, status code and error string. An origin refusal or a wrong token logs at `WARNING`; not-found and other refusals at `INFO`. Tokens, request bodies, diff content and raw header values are never logged.
---

## Notifications: plan_completed and the outbound e-mail channel

Two externally visible behaviours sit on top of the notification bus
described above: a plan-completion notice and an opt-in outbound e-mail
channel.

**`plan_completed` notification.** When every story in a plan reaches `done`,
the pipeline emits one `plan_completed` notification whose message is the
plan summary (`pipeline/plan_completion.py`, called from the scheduler tick
after a story transition). It fires exactly once per plan: the once-only
guard is the `<plan>.plan_completed` marker file in `PLAN_DIR`
(`~/.claude/plans` by default). The marker is written only after the
notification has been emitted, so a failed emission is retried by a later
tick instead of being lost; once the marker exists the plan stays all-done
but no second notification is ever emitted.

**Outbox sink.** `pipeline.notification_outbox.outbox_sink` spools selected
notifications to a per-plan `<plan>.outbox.jsonl` spool file in `PLAN_DIR`
(one JSON line per queued record). The sink is disabled by default; set
`PIPELINE_NOTIFY_OUTBOX_ENABLED=1` to opt in. An event allowlist decides
which notifications are spooled at write time:
`PIPELINE_NOTIFY_OUTBOX_EVENTS` is a comma-separated list of structured
event names (default `plan_completed,story_parked,dispatch_failed,tests_failed,agent_gave_up`); a notification whose event is not in
the allowlist — a `story_done` notice, say — is never spooled and therefore
never e-mailed.

**Delivery happens in the scheduler tick's drain phase, not inline in the
notification path.** Each scheduler tick runs a drain phase
(`pipeline.notification_outbox.drain_outbox`, wired from
`pipeline.scheduler_daemon.run_once`) that reads every plan's outbox file and
hands each queued record to
`pipeline.notification_email.send_notification_email`, an SMTP sender. This
is sink rule 2 ("No inline network I/O"): posting to an external service
inside the notification path would block the sequential pipeline tick, so
the spooling sink performs no network I/O at all and only the scheduler's
drain pays that cost, on its own schedule. A record the sender accepts is
removed from the spool; a record it rejects — or that makes the sender raise
— is retained verbatim for the next drain, so a transient SMTP outage delays
delivery instead of losing the notification.

**Configuration** (all opt-in; the outbox spool and the e-mail send are
disabled by default):

| Variable | Default | Effect |
| --- | --- | --- |
| `PIPELINE_NOTIFY_OUTBOX_ENABLED` | `false` | Enables the per-plan `<plan>.outbox.jsonl` spool; only the exact string `1` turns it on |
| `PIPELINE_NOTIFY_OUTBOX_EVENTS` | `plan_completed,story_parked,dispatch_failed,tests_failed,agent_gave_up` | Comma-separated event allowlist applied when spooling |
| `PIPELINE_NOTIFY_EMAIL_HOST` | unset (empty) | SMTP relay host, e.g. `smtp.example.com`; required for a send |
| `PIPELINE_NOTIFY_EMAIL_PORT` | `587` | SMTP relay port (submission) |
| `PIPELINE_NOTIFY_EMAIL_ENABLED` | `0` | Master gate for the e-mail send; must be set to exactly `1` — any other value (`true`, `yes`, `0`, unset) silently skips the send and leaves records retained in the outbox |
| `PIPELINE_NOTIFY_EMAIL_USER` | unset (empty) | SMTP account name, e.g. `you@example.com` |
| `PIPELINE_NOTIFY_EMAIL_PASSWORD` | unset (empty) | SMTP credential — use a provider app-password, never a primary account password |
| `PIPELINE_NOTIFY_EMAIL_FROM` | unset (empty) | Envelope From address; falls back to the username |
| `PIPELINE_NOTIFY_EMAIL_TO` | unset (empty) | Recipient of the plan-completion e-mail |
| `PIPELINE_NOTIFY_EMAIL_TIMEOUT` | `20` | SMTP socket timeout in seconds; must be numeric or the send fails closed |

The subject line is derived from the record's structured event —
`record["payload"]["event"]` (`pipeline/notification_email.py:136`). A
`plan_completed` event renders `[pipeline] plan complete: <plan>`; a
`story_parked` event renders `[pipeline] story parked: <plan>/<story_key>`;
and a `dispatch_failed`, `tests_failed`, or `agent_gave_up` event renders
`[pipeline] story failed: <plan>/<story_key>`. The story key is read from the
record's top-level `story_key`, falling back to `payload.story_key`; when
neither is present the `/<story_key>` suffix is omitted. A missing, `None`, or
unrecognized event keeps the legacy `[pipeline] plan complete: <plan>` subject,
so records spooled before the event stamp still render identically. STARTTLS
with mandatory certificate verification is always on
(`pipeline/notification_email.py:176`–177); there is no subject or TLS toggle
to configure.

A failed send never drops the record: the drain rewrites the spool atomically
and keeps every record the sender did not accept, so delivery is
at-least-once. The e-mail sender fails closed on partial configuration
(missing host, recipient, or sender) and logs the missing variable names,
never their values.

### Which events are e-mailed by default

The default allowlist is
`plan_completed,story_parked,dispatch_failed,tests_failed,agent_gave_up` —
a comma-separated list with no spaces, so an operator can add or remove
events by editing `PIPELINE_NOTIFY_OUTBOX_EVENTS`. It contains only the
events that mean a human is needed:

- `plan_completed` — every story in the plan is done.
- `story_parked` — a story parked and needs a human.
- `dispatch_failed` / `tests_failed` / `agent_gave_up` — a story failed.

Healthy-progress events such as `story_merged` are deliberately NOT in the
default: they are routine and would only add noise. Because the allowlist is
comma-separated, an operator who wants merge notices by e-mail can add
`story_merged` to the value; an operator who does not want parked-story
mail can remove `story_parked`. A notification emitted without a structured
`event` at all is never spooled, whatever its message says.

## Notification records

The pipeline writes two notification artifacts per plan: a legacy free‑text log
`<plan>.notifications.log` and a structured JSON Lines file
`<plan>.notifications.jsonl`.  Both are bounded by a shared size‑cap rotation
policy: when a write would push the active file past
`PIPELINE_NOTIFICATIONS_MAX_BYTES` (default `2097152` bytes = 2 MiB), the
current file is renamed to `<name>.1`, older generations shift up
(`.1` → `.2`, `.2` → `.3`, …), and generations beyond
`PIPELINE_NOTIFICATIONS_KEEP` (default `3`) are deleted.  On‑disk history is
therefore bounded at roughly `(KEEP + 1) × MAX_BYTES` per artifact.  Setting
`PIPELINE_NOTIFICATIONS_MAX_BYTES` to `0` (or any value `<= 0`) disables
rotation entirely, restoring the historical append‑only, unbounded growth.

The free‑text log remains the human‑readable stream that older tooling
expects — it is additive only up to the cap, then rotated into numbered
generation files.  The JSONL file is likewise additive, appended each time
`_notify_user` is called, and contains one JSON object per line terminated by
a newline; past the cap it rotates under the same policy.

Each record has the following keys:

- `ts`: ISO‑8601 UTC timestamp of when the notice was emitted.
- `plan`: name of the plan that produced the notice.
- `message`: free‑text message passed to `_notify_user`.
- `story_key`: optional story key associated with the notice (may be `null`).
- `severity`: one of `"info"`, `"warning"`, or `"error"`.  Any other value is
  normalised to `"info"`; this guarantees that a failure path never aborts a
  pipeline tick.
- `event`: machine‑readable name for the kind of notice, e.g.
  `ci_pending_stalled`.  It lives in `payload["event"]` on the bus event and is
  distinct from the outer event envelope whose `type` is always `"notification"`.
  Story‑lifecycle notifications emitted by `pipeline/advance.py` carry one of
  these structured `event` names, drawn from a fixed vocabulary so the
  notifications JSONL sidecar can drive cost‑per‑merged‑story metrics:
  `dispatch_failed`, `escalated`, `model_fallback`, `agent_gave_up`,
  `tests_failed`, `story_parked`, `merge_ci_rework`, `merge_gate_failed`,
  `merge_gate_retry`, `merge_failed`, `merge_retry`, and `story_merged` (emitted on the
  successful‑merge path only, immediately after the story is marked done).
  One notification is intentionally excluded from this vocabulary: the
  dispatch‑retry notice (`"<key> dispatch attempt n/max failed …; will
  retry."`), a transient scheduling state rather than a terminal story
  outcome.  The parked notice (`"<key> parked: <reason>"`) carries
  `story_parked` so operators can allowlist the park alert for e-mail:
  `notification_outbox` selects records for e-mail using only
  `payload["event"]`, so an unstamped park notice is unreachable by e-mail.
  `story_metrics` ignores event names outside its known sets, so
  cost‑per‑merged‑story is unaffected.  Records whose message matches no
  vocabulary entry simply omit the `event` key (backward compatible).
- `dedup_key`: value captured at write time; it is **not** used to suppress a
  write.  Only the current file plus the `PIPELINE_NOTIFICATIONS_KEEP` most
  recent generations remain on disk — older generations are deleted at
  rotation time, so on‑disk history is bounded at roughly
  `(KEEP + 1) × PIPELINE_NOTIFICATIONS_MAX_BYTES`; the dashboard later
  collapses consecutive records with the same `dedup_key` into a single entry
  that adds `count` and `last_ts`.

The notification system uses a single event bus returned by
`pipeline.event_wiring.get_bus()`.  `_notify_user` publishes a `notification`
event on that bus; sinks subscribe to it.  Two sinks are built in:
`pipeline.notification_sinks.file_log_sink`, which reproduces the legacy log
behaviour, and `pipeline.notification_outbox.outbox_sink`, which spools
selected notifications (`payload["event"]` in an allowlist, default
`plan_completed,story_parked,dispatch_failed,tests_failed,agent_gave_up`) to a per‑plan `<plan>.outbox.jsonl` file. The outbox sink is
disabled by default (`PIPELINE_NOTIFY_OUTBOX_ENABLED=1` to opt in).

When writing a new sink, three rules must be obeyed:

1. **Never raise into the bus** – swallow any exception and log at ERROR so that
   a tick cannot fail because of a notification failure.
2. **No inline network I/O** – posting to an external webhook inside the
   notification path would block the sequential pipeline tick; queue or run in a
   background daemon instead.
3. **Redact sensitive data at the sink boundary** and ship outbound sinks
   disabled by default.  Notification text may contain gate errors, branch names,
   worktree paths, and raw CI stderr.

The dashboard API (`GET /api/plans/{plan}`) now returns two fields:

- `notification_records`: structured, deduped, severity‑normalised records.
- `notifications`: the legacy free‑text log strings (kept for backward
  compatibility).

One outbound sink exists: email. Each scheduler tick runs a drain phase
(`pipeline.notification_outbox.drain_outbox`, wired from
`pipeline.scheduler_daemon.run_once`) that reads every plan's outbox file and
hands each queued record to
`pipeline.notification_email.send_notification_email` (an SMTP sender gated
by its own credential/config env vars). A record the sender accepts is
removed; a record it rejects or that raises is retained for the next tick's
drain, so a transient SMTP outage never loses a notification. The drain runs
in its own try/except inside the tick and never propagates a failure. No
Slack or generic webhook sink is implemented yet.


### Runtime preflight

`run_preflight()` (`pipeline/preflight.py`) runs four read-only checks before
dispatch: the plan directory resolves and is writable (`PLAN_DIR` env var, else
`pipeline.paths.PLAN_DIR`), `git` is on `PATH`, the resolved dispatch backend
(`PIPELINE_BACKEND_DISPATCH`, default `claude`) is usable — the `claude` backend
requires the Claude Code CLI on `PATH` — and `model_registry.json` loads as a
JSON object.

The dispatch-backend check mirrors what real per-story dispatch execution
resolves (`pipeline/dispatch.py` reads `PIPELINE_BACKEND_DISPATCH` directly,
default `claude`) and deliberately does NOT consult `model_registry.json`'s
`roles.dispatch` key — real dispatch never reads it (the only registry path
into real routing is the separate `auto` → `routing.dispatch` lookup), so a
registry-based check could green-light a provider dispatch will never invoke.

| Check | Status when it fails | Observable behavior |
| --- | --- | --- |
| `PLAN_DIR` unwritable (or its parent not writable) | FAIL | `raise_on_failure()` raises `PreflightError` — fix permissions or point `PLAN_DIR` elsewhere |
| `git` absent from `PATH` | FAIL | `PreflightError` — install git and re-run |
| `claude` CLI absent while backend is `claude` | FAIL | `PreflightError` — install the CLI or reject the backend at startup |
| `model_registry.json` unloadable/malformed | FAIL | `PreflightError` — check the file exists, is valid JSON, is readable |
| local-family backend (`ollama`, `lmstudio`, `mlx`, `local`, `auto`) with its provider CLI absent | WARN only | logged in the summary; dispatch fails later until the provider is installed |
| unrecognized `PIPELINE_BACKEND_DISPATCH` value (not `claude` or local-family) | WARN only | logged in the summary with the known backend names; dispatch fails later until the variable is corrected |

The dashboard logs this summary once at startup (`startup preflight: N ok, N
warn, N fail`) but never blocks on it — only `raise_on_failure()` turns FAIL
statuses into a `PreflightError`, and warns never raise.

Graceful degradation for optional components — absence is never an error:

- **Ollama absent** — local dispatch via `PIPELINE_LOCAL_PROVIDER=ollama` fails
  at dispatch time with a clear message; the Claude backend and the rest of the
  pipeline are unaffected.
- **Docker absent** — `PIPELINE_SANDBOX=docker` falls back to unsandboxed
  execution in the isolated worktree; nothing crashes at startup.
- **Remote exec host unset** — `PIPELINE_EXEC_DISPATCH=ssh` fails closed at
  dispatch time with a `ValueError` naming both
  `PIPELINE_REMOTE_EXEC_HOST` and `PIPELINE_REMOTE_SYNC_ROOT`; it never
  silently degrades to local execution on the orchestrator host. See
  [Remote execution](docs/specs/REMOTE_EXECUTION.md) for the full contract.
- **MLX / launchd** — macOS-only; see the
  [Platform support](README.md#platform-support) section of README.md for the
  documented Linux alternatives. On other hosts the scheduler and MLX
  supervision simply do not start unless you run the same entry points
  yourself.

`scripts/install.sh` runs the install-time version of the same checks.

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
          "backend": "optional: claude | local | ollama | lmstudio | mlx | litellm | auto",
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
  `claude | local | ollama | lmstudio | mlx | litellm | auto`. Pins that one story to
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

## Acceptance fixture grading

An `acceptance` fixture that calls the changed function directly — never
touching the call site, registration path, or wiring the story actually
asks for — creates a graded path that bypasses the integration work. The
oracle goes green the moment the unit works in isolation, so a weak
executor (and even a capable one under time pressure) will skip the
ungraded wiring step no matter how clearly `agent_instructions` states it,
and the story ships dead code. This happened live: a fixture asserted a
helper function's return value directly, `agent_instructions` said to wire
the helper into an existing call site, and the dispatched agent shipped the
helper unwired — the oracle was green because it never exercised the call
site, and the full-suite done-bar didn't catch it either since it doesn't
exercise the call site any more than the fixture did.

Before writing an `acceptance` fixture, check: if `agent_instructions`
requires a call-site change, a decorator/registration move, or wiring one
piece into another, does the fixture's assertion actually fail when that
wiring is missing — or would it still pass if the wired piece were just
sitting there unconnected? If the latter, drive the real entrypoint
(`main()`, the CLI, the route handler) rather than calling the unit
directly, or assert against the production source/registry that the
connection landed (e.g. `mcp._tool_manager._tools["foo"].fn is foo`, not
just `foo(...)` returning the right value). `ingest_plan` runs a
non-blocking heuristic (`pipeline.build_detect._isolation_only_acceptance_warning`)
that flags fixtures which look isolation-only against instructions
mentioning wiring, and posts the warning to the plan's notification log —
treat it as a prompt to re-check the fixture, not a hard gate.

---

## Per-role provider/model configuration

Every pipeline role — **overlord**, **planner** (the guided-decomposition
checklist role), **dispatch** (the implementer), **review**, and
**decompose** (`decompose_plan`) — is independently configurable to a
provider (`claude` / `ollama` / `mlx` / `lmstudio` / `litellm`) and a model.
`model_registry.json` (repo root, or `PIPELINE_MODEL_REGISTRY_PATH`) is the
single editable place to see and change what's available, instead of
scattered env vars:

```json
{
  "providers": {
    "claude":   {"models": {"opus": {"tag": "opus"}, "sonnet": {"tag": "sonnet"}}},
    "ollama":   {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}, "glm": {"tag": "glm-5.3-flash:cloud"}}},
    "mlx":      {"models": {"qwen": {"tag": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"}}}
  },
  "roles": {
    "review": {"provider": "mlx", "model": "qwen"}
  }
}
```

`providers.<name>.models.<friendly name>.tag` maps a short name (what you'd
say out loud — "gpt-oss", "qwen") to the literal string a provider expects
(an Ollama tag, an MLX model path, or a Claude tier). A `roles` entry pairs
a role with a `(provider, model)` — resolved via the friendly name above,
so a typo is caught immediately rather than silently falling back to
some other model. Both sections are optional and can be partial; an
unconfigured role falls through to today's existing behavior unchanged.

The shipped `model_registry.json` deliberately ships **no `roles` block** —
this project decouples from any single provider, so the operator chooses.
Keep a personal `roles` block out of the repo in the gitignored
`model_registry.local.json` (repo root) and point
`PIPELINE_MODEL_REGISTRY_PATH` at it; the env var works with any registry
JSON path.

**Resolution priority** (`role_registry.resolve_role`, highest wins), the
same for provider and model independently:

1. A plan's `role_config` block (see the schema above) — set once per plan,
   applies to every tick.
2. The registry file's `roles.<role>` entry (`model_registry.json`, or
   whichever file `PIPELINE_MODEL_REGISTRY_PATH` names) — the single source
   of truth for role routing.
3. The existing `PIPELINE_BACKEND_<ROLE>` env var (and, for local models,
   the existing `PIPELINE_LOCAL_MODEL_*`/`PIPELINE_LOCAL_REVIEW_MODEL`/
   `PIPELINE_LOCAL_PLANNER_MODEL` vars) — consulted only when the registry
   has no entry for the role (the empty-state path, so a fresh clone still
   boots); still the fastest way to override ad hoc when the registry is
   silent.
4. The role's existing hardcoded/persona-frontmatter default — for
   dispatch/review this is the `claude` backend.

Use `get_role_config(plan_name=None)` to see what actually resolves right
now (optionally layered with a specific plan's `role_config`) before
running that plan.

---

## Dispatch implementation resolution

The `dispatch` role in "Per-role provider/model configuration" above is the
**implementer** — and its backend and model do *not* come from
`role_config.dispatch`. They resolve as follows (verified in
`pipeline/dispatch.py::_resolve_dispatch_backend` and
`pipeline/persona.py::_build_dispatch_command`):

- **Implementer backend** — story-level `backend` field (if set; also
  persisted onto the story after *every* dispatch, so a later env change
  never re-routes an already-dispatched story) → `PIPELINE_BACKEND_DISPATCH`
  env (read from the dispatch daemon's process env; `auto` routes a-priori) →
  the `claude` driver default when unset. The persona (security-engineer) and
  unwinnable-scope overrides force `claude` **only when the story has NO
  explicit backend** — a story with `backend: "codex"` stays on `codex`.
- **Implementer model** — `story['model']` → persona default model →
  `DEFAULT_MODEL` (`pipeline/config.py`; env `PIPELINE_DEFAULT_MODEL`, default
  `'sonnet'`). For local-family backends the driver then maps the resolved
  tier to a concrete tag (`PIPELINE_LOCAL_MODEL_DEFAULT` plus the
  `PIPELINE_LOCAL_MODEL_<TIER>` overrides) — that mapping happens *after*
  resolution and is not a fourth chain entry.
- **`role_config.dispatch` / `model_registry.json` `roles.dispatch`** —
  consulted only for the planner / tech-lead tier classification and the
  decompose strength-tier guidance (`pipeline/planner.py::_dispatch_strength_tier`)
  and by `get_role_config`'s display. It never selects the executor backend
  or model. (It DOES directly drive the review/overlord/test_author roles.)

Practical consequence: to pin an implementer model for one plan, set
`PIPELINE_LOCAL_MODEL_DEFAULT` (or pipe it through the dispatch backend), or
set story-level `backend` — a plan's `role_config.dispatch` does not do it.

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

It is **always on** for any **local-family** dispatch backend — Claude doesn't
need the crutch, so a `claude` dispatch skips it. There is no on/off toggle: the
planner runs on every local-family story's first dispatch (never on a resume)
and on every rework cycle. The planner is its own independently routable role
(default `claude`, via `default_provider`, unless the operator's own
`model_registry.local.json`/`role_config` pins it elsewhere — the shipped
`model_registry.json` no longer ships a `roles` block), so it can run on a
different provider than dispatch — set `PIPELINE_BACKEND_PLANNER` to pin the
provider (e.g. dispatch on `ollama`, plan on `mlx`) and
`PIPELINE_LOCAL_PLANNER_MODEL` to pin the model tag.

- **Initial checklist (`_run_planner`).** On a story's *first* dispatch (never
  on a resume — the checklist is planned once), the planner turns
  `agent_instructions` into an ordered checklist and writes it to
  `.agent_plan.md` in the worktree; the dispatch prompt appends it under
  "Implementation checklist from your tech lead." The call is **best-effort and
  fails open to `None`** — a broken, slow, or rate-limited planner never blocks
  or corrupts dispatch; the story simply proceeds with no checklist.
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

## TDD-split (test-author phase)

A weak local implementer often implements *against its own buggy tests* — it
writes the test and the impl in one pass, so a wrong test hides a wrong impl.
TDD-split breaks that coupling: a **separate test-author pass** writes the red
tests first (a real commit in the worktree), and only then does the executor
start, implementing against tests it did not author. The same-model split was
measured to *hurt* (a model authoring its own tests read-loop-parks vs. no
split — see `TDD_SPLIT_PRODUCTION_PLAN.md`), so the test-author role
**must resolve to a different backend+model than dispatch** or the split is
skipped entirely (fail-open to monolithic dispatch, never a gate).

It is **always on for local-family dispatch** — there is no global on/off
toggle (the legacy `PIPELINE_TDD_SPLIT` env var was removed and is now a
harmless dead letter if a stale operator environment still exports it) and no
per-story opt-in field either; the phase mirrors the guided-decomposition
planner's gate exactly. The phase is gated on:

- a local-family backend (`dispatch_backend in _LOCAL_BACKEND_NAMES` — same
  rationale as the planner: it's a crutch for the weak local executor, Claude
  doesn't need it),
- not resuming (a rework redispatch acts on the *same* committed tests; it
  never gets a fresh test-authoring pass), and
- no existing `.tdd_split_test_author_done` marker in the worktree
  (belt-and-suspenders with `not resuming`).

The test-author role is independently routable (set
`PIPELINE_BACKEND_TEST_AUTHOR` to pin its provider; configure `test_author` in
`model_registry.json`'s `roles` block or a plan's `role_config`). If the role
is unconfigured, resolves to the same backend+model as dispatch, or the
authoring dispatch fails/times out/produces no commit, the phase **fails open**
— no marker is written, the executor prompt is not augmented, and the story
proceeds as ordinary monolithic dispatch. On success the executor prompt is
augmented with a never-touch-tests steering line so the executor implements
against the committed tests rather than rewriting them.

---

## Configuration (environment variables)
### Launchd Install & Reload

The launchd plist files in this repository are templates. The running daemon reads only the installed agent under `~/Library/LaunchAgents/com.fagan.pipeline.advance-scheduler.plist`. To apply a change to the running daemon you must:

1. Edit the installed plist surgically (do not replace the whole file).
2. Reload the daemon with `scripts/reload_pipeline_daemon.sh` (or equivalently `launchctl unload` then `launchctl load` on that path).

The change has **no effect until this reload** happens.

**Drift warning**: The installed agent can differ from the repo copy. For example, on this machine the installed plist pins `PIPELINE_LOCAL_NUM_CTX=32768` and sets `PIPELINE_AUTO_TRIAGE` and `PIPELINE_MAX_CONCURRENT_AGENTS=4`, none of which the repo template specifies. A wholesale regenerate-and-install would silently drop these local overrides.

Set global vars in your shell profile; set per-project overrides in the project's
`.mcp.json` `env` block.

### Minimal configuration

This repo defines over a hundred `PIPELINE_*`/`LOCAL_AGENT_*` variables, but
the overwhelming majority are tuning knobs with sane defaults — empirically-set
timeouts, retry budgets, and per-model overrides that only matter once you're
running local dispatch at scale. **A first deployment using the default
`claude` backend for every role needs none of the `PIPELINE_*` tuning knobs
below — but it does need provider selection and tool authorization (see
*Per-role provider/model configuration* above and README's *Provider
selection & authorization*): the shipped registry
routes no roles, so set `PIPELINE_BACKEND_<ROLE>` (or a `roles` block in a
`PIPELINE_MODEL_REGISTRY_PATH` registry) per role, and run `gh auth login`
plus the Claude Code CLI's own login before the first dispatch. Set these,
and leave everything else at its default until you have a concrete reason to
change it:

| Variable | Why you'd set it on day one |
|---|---|
| `PIPELINE_BACKEND_<ROLE>` | **Required setup, not a tuning knob**: the shipped `model_registry.json` ships no `roles` routing, so each role falls back to the `claude` backend unless you select a provider — via this var (e.g. `PIPELINE_BACKEND_DISPATCH=ollama`), a plan's `role_config`, or a `roles` block in a registry file pointed at by `PIPELINE_MODEL_REGISTRY_PATH` |
| `PIPELINE_MODEL_REGISTRY_PATH` | Point the pipeline at your own registry JSON — conventionally the gitignored `model_registry.local.json`, which keeps personal per-role routing out of the repo |
| `REPO_ROOT` | Point the pipeline at the project it should operate on — almost always required; defaults to `.` |
| `PLAN_DIR` | Only if you don't want plans/manifests in the default `~/.claude/plans` |
| `PIPELINE_AUTONOMY` | Set to `dry-run` for your first plan on any new deployment (see Autonomy levels below); move to `gated` once you trust it |
| `PIPELINE_RISK_THRESHOLD` | Leave at `low` until you've watched a `gated` run go well |
| `PLANE_*` (4 vars) | Only if you're mirroring stories into a Plane project — skip entirely otherwise, the manifest is authoritative regardless |
| `PIPELINE_BACKEND_DISPATCH` / `_REVIEW` / `_OVERLORD` | Only to opt a role into local-model dispatch (`ollama`/`lmstudio`/`mlx`/`local`/`auto`) instead of the `claude` default |

Everything under `PIPELINE_LOCAL_*`, `LOCAL_AGENT_*`, the per-model tuning
table, the rework/escalation budgets, and the review-fallback knobs exists to
tune local-model dispatch once you've opted into it. They're documented in
full below for when you need them, not because you need to read them first.

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
| `PIPELINE_MAX_CONCURRENT_AGENTS` | `3` | Cap on ON-DEVICE dispatched agents running at once (across all plans); `<=0` = unlimited (the on-device cap is skipped entirely). Claude-routed and `:cloud`-tagged dispatches are exempt — they bypass the slot check and never consume a slot, so this flag no longer bounds total agent count or spend; cloud-backed dispatch is bounded by the usage pause thresholds instead. **Local-Ollama note:** with multiple local models in `MODELS`, set this to `1` to avoid Ollama swapping a different model into VRAM (dispatch_story will WARN when it detects a different model already loaded). Same-model concurrency is always safe. |
| `PIPELINE_MAX_DISPATCH_PER_TICK` | `1` | Cap on how many stories ONE plan may dispatch in a single tick, regardless of backend (LOCKSTARVE-A3) — an additional, independent bound layered on top of the on-device cap above, which `:cloud` dispatches bypass entirely. Each dispatch pays a synchronous test-author + planner call while the tick holds the plan's `_plan_lock`, so an unbounded tick holds that lock for minutes and refuses external `approve_merge` calls with `plan busy (scheduler tick in progress)`. Default `1`: with no env set, a tick dispatches at most one story per plan (previously every ready story). Values `<= 0` DISABLE the cap (every ready story dispatches in one tick, the pre-A3 behavior). A malformed or empty value degrades to the `1` default instead of raising — a bad operator override must not take a tick down. A story deferred by the cap keeps its current dispatch-eligible status (`todo`/`interrupted`), consumes no dispatch attempt, and is picked up by the NEXT tick (the budget is per-tick state that resets to 0 at each tick start, so three ready stories dispatch one per tick over three ticks). Failed dispatches (`ok: False`) do not consume cap budget, so a plan whose dispatches fail every tick still holds the plan lock unbounded — pre-existing exposure, unchanged by this knob. |
| `PIPELINE_MERGE_MAX_ATTEMPTS` | `3` | Merge error budget: how many ticks a failing `_merge_pr` (transient `gh`/`git`) is retried before the story is marked `failed` for human intervention |
| `PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD` | `3` | Abandon-restart escape hatch (LOCKSTARVE-C2): how many CONSECUTIVE watchdog-worker abandonments (scan or reconcile) the scheduler daemon tolerates before it writes its health file, logs an ERROR naming a leaked plan `_plan_lock` as the suspected cause, and exits 1 so launchd (`KeepAlive=true`) restarts it — process death is the only thing that releases a leaked flock. Read at call time, never cached; a malformed value degrades to the `3` default with a warning; values `<= 0` DISABLE the hatch entirely (never restart) rather than falling back to the default. SRR-2: the streak no longer resets on a healthy tick while an abandoned watchdog worker is still alive in the daemon's ledger — the reset's premise ("every phase completed normally") is false while that worker's leaked `_plan_lock` persists — so the hold ends only once the abandoned worker is actually gone (its blocked call returned and the ledger entry was pruned), after which the next healthy tick resets the streak to `0` exactly as before. Independently of this threshold, the grace-window exit path (see `PIPELINE_ABANDON_WORKER_GRACE_SECONDS` below) exits 1 on any tick where an abandoned worker is still alive past its grace window, even with a zero streak. |
| `PIPELINE_ABANDON_WORKER_GRACE_SECONDS` | `300` | Abandoned-worker leak detector (SRR-2): how long after its abandonment an abandoned watchdog worker may stay alive before the daemon treats the plan `_plan_lock` its wedged call holds as leaked — a worker still alive that long is a blocked call that has not returned, on a horizon the daemon cannot influence, so the daemon sets `last_error` naming the worker and the leaked lock, writes its health file, and exits 1 so launchd (`KeepAlive=true`) restarts it and the flock is released. Fires independently of `PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD` (even with a zero streak) and before the streak-reset/threshold block each tick; the exit fires only when the worker has been alive strictly longer than the grace (`elapsed > grace`, so exactly-at-grace does not exit), and a worker still under grace only logs a WARNING naming the remaining wait. The exit restarts the process, so the streak hold dies with it; within a live process the hold lifts the same way — once the worker is gone (its blocked call returned and the ledger entry pruned), the next healthy tick resets the streak to `0`. Read at call time, never cached; a malformed, non-finite, or non-positive value degrades to the `300` default with a warning — a bad operator override never crashes the tick and never silently disables the detector. |
| `PIPELINE_DISPATCH_LEASE_TTL_SECONDS` | `1800` | Dispatch-lease TTL in seconds (`pipeline/dispatch_lease.py`, LOCKSTARVE-B2): how long a story's `dispatch_lease_expires_at` lease stays live after a claim, so a released plan lock cannot double-dispatch a story. Read at call time, never cached at import. Blank/unparseable values, values `<= 0`, or values above the 30-day cap (`2592000`) degrade to the `1800` default — a malformed value never raises. An explicit `ttl_s` argument outside `(0, 2592000]` raises `ValueError` instead (fail secure, before any state mutation). |
| `PIPELINE_DISPATCH_MAX_ATTEMPTS` | `3` | Dispatch error budget: how many times a story whose launch keeps failing (raising `dispatch_story`, or an agent that produces no output) is retried before it is marked `failed` instead of looping forever |
| `PIPELINE_REWORK_MAX_ATTEMPTS` | `3` | Rework budget: how many times a `changes_requested` story is redispatched (with the reviewer's feedback) before it is `parked` for human review instead of looping through review↔rework |
| `PIPELINE_REVIEW_INCONCLUSIVE_MAX` | `2` | Inconclusive-review budget: how many consecutive non-rate-limited `UNKNOWN` verdicts (no parseable `VERDICT` line, or the reviewer-exception fail-safe) are retried on later ticks — without counting against `PIPELINE_REWORK_MAX_ATTEMPTS` or touching `review_feedback` — before the story is `parked` for human review instead of retrying forever |
| `PIPELINE_PLANE_MAX_ATTEMPTS` | `3` | Plane error budget: inline retries for a best-effort Plane state transition before the drop is recorded durably (Plane sync never blocks git work) |
| `PIPELINE_PAUSE_THRESHOLD` | `90` | `%` of the **session** window that trips the Claude usage gate |
| `PIPELINE_RESUME_THRESHOLD` | `70` | `%` the **session** window must drop below to clear the gate |
| `PIPELINE_WEEK_PAUSE_THRESHOLD` | `90` | `%` of the **week** window that trips the gate |
| `PIPELINE_WEEK_RESUME_THRESHOLD` | `70` | `%` the **week** window must drop below to clear the gate |
| `PIPELINE_STATE_DIR` | *(unset → the pipeline-owned plans/manifests root, i.e. `PLAN_DIR`'s base)* | Override for the detached-grading state root. The detached grade's `result.json`/`grading.log` are written under `<state_root>/grading/<story_id>/` (mode `0o700`) and read back from exactly the path persisted in the story's manifest. Security rationale: the grade verdict and its log must live where the code under test cannot write — an agent-writable worktree would let a graded agent forge a passing verdict (`{"returncode": 0, ...}`) during its own build and bypass the acceptance gate at the collect tick. The default derives from the same root that owns the plans/manifests, so no operator setup is required; a *relative* `PIPELINE_STATE_DIR` value resolves against that fixed plans/manifests base (never the scheduler's CWD), so the spawn tick and the later collect tick resolve the same absolute path even if the daemon's working directory changes between them. |
| `PIPELINE_BACKEND_DISPATCH` | `claude` | Backend for dispatch (coding) agents: `claude` \| `ollama` \| `lmstudio` \| `mlx` \| `litellm` \| `local` \| `auto` (layered local-first with Claude fallback — see below). EMPTY-STATE-ONLY: ignored when `model_registry.json` pins this role's provider (a `roles` entry outranks the env var) |
| `PIPELINE_BACKEND_REVIEW` | `claude` | Backend for the code-reviewer persona: `claude` \| `ollama` \| `lmstudio` \| `mlx` \| `litellm` \| `local`. EMPTY-STATE-ONLY: ignored when the registry pins this role's provider |
| `PIPELINE_BACKEND_OVERLORD` | `claude` | Backend for overlord decisions: `claude` \| `ollama` \| `lmstudio` \| `mlx` \| `litellm` \| `local`. EMPTY-STATE-ONLY: ignored when the registry pins this role's provider |
| `PIPELINE_BACKEND_PLANNER` | *(unset → `claude`)* | Backend for the guided-decomposition planner (the always-on in-story checklist + rework-feedback checklist role): `claude` \| `ollama` \| `lmstudio` \| `mlx` \| `litellm` \| `local`. Resolution priority: a plan's `role_config.planner` → `model_registry.json`'s `roles.planner` (absent from the shipped registry; only present if the operator supplies their own `model_registry.local.json`, selected via `PIPELINE_MODEL_REGISTRY_PATH`) → this env var (the EMPTY-STATE fallback, consulted only when the registry has no entry for the role) → code-level `default_provider` (`claude`). Unset resolves to `claude`, not a mirror of dispatch — set this to pin the planner to a different provider than dispatch, e.g. dispatch on `ollama` with the planner on `mlx`. Only local-family dispatch runs the planner; a `claude` dispatch skips it. |
| `PIPELINE_LOCAL_PLANNER_MODEL` | *(unset)* | Top-priority *model* override for the planner, mirroring `PIPELINE_LOCAL_REVIEW_MODEL`: a concrete provider tag (e.g. `gpt-oss:20b`, `qwen3-coder:30b`) that wins over both `role_config` and the registry, but only when the resolved planner provider is local-family (`ollama`/`lmstudio`/`mlx`/`local`) — a bare Ollama tag never leaks into a Claude planner. Unset leaves the model to `role_config`/registry resolution. |
| `PIPELINE_BACKEND_DECOMPOSE` | `claude` | Backend for the `decompose_plan` tool (turns a raw request into epics/stories JSON via the product-analyst persona): `claude` \| `ollama` \| `lmstudio` \| `mlx` \| `litellm` \| `local`. Independent of the interactive `product-analyst` subagent (invoked via the `Agent` tool), which is always Claude and unaffected by this setting. |
| `PIPELINE_DECOMPOSE_SCRATCHPAD` | `on` | Whether guided decomposition maintains the `.agent_scratchpad.md` cross-sub-step memory (`on` \| `off`). `off` runs the checklist-only ablation. |
| `PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV` | *(unset)* | Off by default: every `claude` subprocess call strips `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`/`ANTHROPIC_SMALL_FAST_MODEL`/`ANTHROPIC_DEFAULT_OPUS_MODEL`/`ANTHROPIC_DEFAULT_SONNET_MODEL`/`ANTHROPIC_DEFAULT_HAIKU_MODEL`/`CLAUDE_CODE_USE_BEDROCK`/`CLAUDE_CODE_USE_VERTEX` from its environment so an interactive session's 3rd-party-provider redirect can't silently leak into dispatch/review/overlord. Set truthy only for a legitimate enterprise Bedrock/Vertex deployment that intentionally routes the CLI elsewhere — see "Claude backend provider isolation" below. |
| `PIPELINE_LOCAL_PROVIDER` | `ollama` | Wire protocol the `local` alias's `complete()`/`resource_status()` speak when a role is set to the generic `local` name: `ollama` \| `mlx` \| `lmstudio`. Naming the provider directly in `PIPELINE_BACKEND_<ROLE>` (`ollama`/`lmstudio`/`mlx`, RELIABILITY_PLAN.md T16) pins that provider for that role regardless of this setting — `local` stays a permanent back-compat alias (existing manifests persist `"backend": "local"`) and is the only name this variable actually affects. All three providers are working, live-validated implementations (including real tool-calling round trips and, for `lmstudio`, a full multi-turn review-loop convergence to a verdict). Neither `mlx` (targets `mlx_lm.server`) nor `lmstudio` (targets LM Studio's local server) has a per-request context-window control like Ollama's `num_ctx` — both send that value as `max_tokens` instead. `lmstudio`'s loaded-model check uses its own `/api/v0/models` (`state: loaded/not-loaded`), not Ollama's `/api/ps`, and LM Studio JIT-loads a model on its first request (~30s for a small model) rather than expecting it pre-loaded. **Does not yet affect `dispatch()`** — the coding-agent subprocess always talks to Ollama's native API regardless of this setting (`MODEL_PROVIDER_ABSTRACTION_PLAN.md` S3, deferred). **MLX/LM Studio model names are Hugging Face repo ids with `/` rather than Ollama-style `:` tags** — `_resolve_local_model` treats any model string containing `:` or `/` as a concrete model tag, so a Hugging Face repo id (e.g. `mlx-community/Qwen2.5-1.5B-Instruct-4bit`, `google/gemma-4-e4b`) can be passed directly as a raw `model=` value; only a model string with neither marker is treated as a tier name and resolved via `PIPELINE_LOCAL_MODEL_DEFAULT`/`_OPUS`/`_SONNET`/`_HAIKU`. |
| `PIPELINE_LOCAL_ENDPOINT` | `http://localhost:11434` | Ollama base URL for the `local` driver (it uses Ollama's native `/api/chat`, the only surface that accepts `num_ctx`). Point at a remote Ollama to use another box. |
| `PIPELINE_LOCAL_ENDPOINT_LITELLM` | *(unset)* | Optional `api_base` override for the `litellm` wire provider. There is no local server to point at (the SDK talks straight to the upstream vendor), so the provider's `default_endpoint` is `""` and a non-empty value is forwarded to litellm as its `api_base`. Upstream API keys are read by litellm itself from its own provider env vars — see docs/specs/LITELLM_PROVIDER.md. |
| `PIPELINE_AGENT_HARNESS` | *(unset)* | Harness seam for dispatch: unset → each driver uses its native harness (`claude` for `ClaudeCliDriver`, `local` for `OllamaDriver`); set → must name a registered harness, which dispatch then selects; a cross-harness value (e.g. `local` while the claude driver dispatches) fails closed at dispatch with `NotImplementedError` naming the env var and the offending value, before any side effects. |
| `PIPELINE_LOCAL_MODEL_DEFAULT` | `devstral:24b` | Local model used for any tier without its own override below |
| `PIPELINE_LOCAL_MODEL_OPUS` | — | Local model for the `opus` tier (falls back to the default) |
| `PIPELINE_LOCAL_MODEL_SONNET` | — | Local model for the `sonnet` tier (falls back to the default) |
| `PIPELINE_LOCAL_MODEL_HAIKU` | — | Local model for the `haiku` tier (falls back to the default) |
| `PIPELINE_LOCAL_MODEL_<PROVIDER>_<TIER>` | — | Provider-scoped tier override, e.g. `PIPELINE_LOCAL_MODEL_MLX_SONNET`, `PIPELINE_LOCAL_MODEL_OLLAMA_OPUS`. Checked **before** the provider-agnostic `PIPELINE_LOCAL_MODEL_<TIER>` above. Exists because two roles on two different local providers (e.g. review on `mlx`, dispatch on `ollama`) previously shared one global tier→model mapping meant for a single wire format/model namespace — a real correctness gap, not just an ergonomics one, since an MLX model path and an Ollama tag are different string shapes entirely. Falls back to `PIPELINE_LOCAL_MODEL_<TIER>`, then `PIPELINE_LOCAL_MODEL_DEFAULT`, when unset. |
| `PIPELINE_LOCAL_NUM_CTX` | `16384` | Ollama context window for local calls (sized to fit 100% on a 24GB M4 GPU; raising it risks a slow CPU/GPU split). The advance-scheduler launchd plist no longer pins this value; locally-dispatched models fall through to this global default unless they have their own per-model tuning table entry. |
| `PIPELINE_LOCAL_TEMPERATURE` | `0.3` | Sampling temperature for local model calls |
| `PIPELINE_MODEL_REGISTRY_PATH` | `<repo root>/model_registry.json` | Path to the model/role registry file — see "Per-role provider/model configuration" below. |
| `routing` (block in `model_registry.json`) | *(absent — legacy `auto` routing)* | Optional per-role routing policy read by `app.role_registry.resolve_route` when `PIPELINE_BACKEND_DISPATCH=auto`; sibling of `providers`/`roles`. Each role carries `tiers` (name → `{provider, model}`), ordered first-match-wins `rules` (`{when, tier}`; `when` supports exactly `max_risk` — an inclusive ceiling — and `persona`; any other key raises), and `default_tier`. A missing block or role resolves to `None`, leaving legacy behavior untouched. Precedence in `pipeline.usage._route_dispatch_backend`: an explicit `PIPELINE_LOCAL_MAX_RISK` in the env overrides the policy (legacy gate decides); otherwise the policy's provider is honored verbatim; with no policy and no env, the legacy `PIPELINE_LOCAL_MAX_RISK`-default path applies. Malformed blocks fail open (logged), and `pipeline.dispatch._resolve_dispatch_backend`'s security-persona and unwinnable-scope overrides still apply after routing. Full contract: `docs/specs/MULTI_LLM_ROUTING.md`. |

**Per-model tuning table.** `backend.py`'s `_LOCAL_MODEL_TUNING` dict holds
empirically-settled `temperature`/`num_ctx` overrides keyed by the *resolved
concrete model tag* (e.g. `gpt-oss:20b`), not the tier. An explicit
`PIPELINE_LOCAL_TEMPERATURE`/`PIPELINE_LOCAL_NUM_CTX` env var still always
wins over a table entry; a model tag with no entry falls back to the global
defaults above. This exists so a tuning finding travels with the model
instead of requiring the operator to remember to flip a global env var every
time the active local model changes. The shipped advance-scheduler launchd
plist no longer pins `PIPELINE_LOCAL_NUM_CTX`, so locally-dispatched models
use the per-model table values (e.g. `gpt-oss-20b-high:latest` effectively
runs at `num_ctx` 131072); an operator-set `PIPELINE_LOCAL_NUM_CTX` env var
still overrides. Currently populated:

| Model tag | `temperature` | `num_ctx` | Why |
|---|---|---|---|
| `gpt-oss:20b` | `0.3` |  | 2026-07-03 A/B benchmark (`tests/benchmark/_runs/full_20260703_postfix` vs `temp_tune_20260703`, 15 cells each): `temperature=1.0` scored 6/15 success with 3 cells where the implementation never landed on disk at all; `temperature=0.3` scored 9/15 with only 1, at an unchanged 11/15 ground-truth-pass rate. num_ctx was never A/B-tested and removed from the table for that reason. |
| `gpt-oss-20b-high:latest` |  | 131072 | 2026-09-18 manual sweep (Apple M4, 24GB unified memory) of gpt-oss-20b-high:latest: swept num_ctx from 32768 to 131072 in 7 steps, 100% GPU throughout, 12GB → 13GB resident, no swap growth; 131072 is gpt-oss's own trained/Ollama-enforced ceiling (values up to 1048576 had no further effect). |
| `PIPELINE_LOCAL_TIMEOUT_SECONDS` | `600` | Legacy/unused — no longer read by the code; superseded by `PIPELINE_ROLE_CALL_TIMEOUT_SECONDS` below. |
| `PIPELINE_ROLE_CALL_TIMEOUT_SECONDS` | `600` | Total wall-clock budget in seconds for local role-call attempts (single-shot `complete()` and review-loop turns) **including retries and backoff** — the per‑attempt HTTP timeout and each backoff sleep are capped at the remaining budget, so the whole call is bounded no matter how many attempts fit. Default `600`; blank, unparseable, non‑positive, or non‑finite values fall back to `600`. |
| `PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS` | `900` | Legacy. Was the per-request timeout for the dispatch/review chat loop; since streaming landed this only seeds the harness boot log (`steps=… timeout=…s`). The live timeout is `LOCAL_AGENT_READ_SILENCE_SECONDS` below — kept set by `backend.py` for back-compat. |
| `LOCAL_AGENT_READ_SILENCE_SECONDS` | `180` | Per-chunk read timeout for the streamed `chat()`. Fires only on a genuine stall (no bytes for N s), not on a legitimately long generation that emits a chunk every ~1–2 s. Passed through from the shell/MCP env by `backend.py` (`**os.environ`). |
| `LOCAL_AGENT_CHAT_MAX_ATTEMPTS` | `3` | Retry attempts for a transient `chat()` failure (`httpx.TransportError` or 5xx). 4xx raises immediately. Passed through from the shell/MCP env. |
| `LOCAL_AGENT_CHAT_RETRY_BACKOFF` | `5` | Linear backoff seconds between chat retries (× attempt). Passed through from the shell/MCP env. |
| `LOCAL_AGENT_BASH_TIMEOUT_SECONDS` | `600` | Per-bash-command timeout in the dispatch loop, so a wedged build (e.g. a hung network index fetch) can't hang the agent. Passed through from the shell/MCP env. |
| `LOCAL_AGENT_READ_HEAVY_WINDOW` | `6` | Read-heavy guard window: this many consecutive non-mutating tool calls (reads / non-unique bash) triggers a nudge, then a park, so the loop can't burn the step budget on inspection. Passed through from the shell/MCP env. |
| `LOCAL_AGENT_READ_HEAVY_DISTINCT_WINDOWS` | `3` | Lenient post-nudge cap: after the read-heavy nudge, allow this many **all-distinct** read windows (each file read once) before parking. Total distinct-read ceiling = `READ_HEAVY_WINDOW + DISTINCT_WINDOWS * READ_HEAVY_WINDOW` (default 24). Tasks with many files to orient on (e.g. a multi-method test suite) may need to raise this — the live-`gh` probe had to set `=10` to let lru_cache reach its first commit (PROOF.md note #3). The strict-repetition park (re-reading an already-seen target) is unaffected; only the all-distinct exploration leash is tunable. Passed through from the shell/MCP env. |
| `PIPELINE_LOCAL_MAX_STEPS` | `40` | Max tool-call steps a local **dispatch** run takes before it parks (WIP-commits). **`PIPELINE_LOCAL_MAX_STEPS` is the input knob; `PIPELINE_TRANSPORT_MAX_STEPS` is transport-only and must not be set in the plist or shell** — `backend.py` re-reads `PIPELINE_LOCAL_MAX_STEPS` on every dispatch and writes the resolved value into `PIPELINE_TRANSPORT_MAX_STEPS` for the subprocess (the legacy duplicate write was removed; nothing reads it). Set this in `launchd/com.fagan.pipeline.advance-scheduler.plist` to change overnight run behavior; `launchctl unload && launchctl load` to apply.
| `PIPELINE_LOCAL_REVIEW_MAX_STEPS` | `20` | Max tool-call steps a local **review** takes before returning UNKNOWN (→ parks). Read in-process (not subprocess-spawned) — same input-knob contract as `PIPELINE_LOCAL_MAX_STEPS` and editable in the plist if you want one knob for both. |
| `PIPELINE_CHAT_API_BASE` | *(unset)* | Explicit base URL for the dashboard chat's **internal server-to-server tool calls** (e.g. `http://127.0.0.1:8001` when `DASHBOARD_PORT=8001` and a reverse proxy fronts the browser-facing port). Resolution precedence per `/api/chat` request: (1) this env var — an operator's deliberate override, e.g. a reverse-proxied deployment where the browser's own host:port is not the correct address for server-to-server calls; (2) derived from the incoming request's ASGI `server` scope entry + scheme (never from the client-supplied `Host` header, which is attacker-controlled — a client sending `Host: evil.example` must not be able to steer the internal call target that carries `X-Pipeline-Api-Key`); (3) ChatService's own hardcoded `127.0.0.1:8000` fallback. An empty-string value is NOT an override (falls through to step 2). |
| `PIPELINE_LOCAL_REVIEW_MODEL` | — | Concrete Ollama tag (e.g. `devstral:24b`) for **local review only** — asymmetric review. Both `software-engineer.md` and `code-reviewer.md` declare `model: sonnet`, so without this override dispatch and review resolve to the identical concrete model (a model reviewing its own work with identical weights). Only applied when the review backend is actually local-family (`local`/`ollama`/`lmstudio`/`mlx`, via `PIPELINE_BACKEND_REVIEW` or an explicit fallback); ignored for cloud review so a bare Ollama tag never leaks in as a bogus Claude `--model` value. |
| `PIPELINE_REVIEW_FALLBACK` | `off` | When Claude review hits repeated rate-limits, fall back to this backend inline: `local` \| `ollama` \| `lmstudio` \| `mlx` \| `off` (never fall back). Paired with `PIPELINE_REVIEW_FALLBACK_AFTER` (default `3`), the count of rate-limited attempts before falling back. |
| `PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL` | *(unset/off)* | When truthy (`1`), an acceptance-failing dispatch that still produced real work (new commits) is routed to the reviewer instead of straight to terminal `failed`, so the rework loop can re-dispatch it with feedback (bounded by `PIPELINE_REWORK_MAX_ATTEMPTS`). An empty-branch failure (no commits) stays `failed` — re-dispatching a stuck prompt won't help. The merge gate (`_reverify_acceptance`) still blocks any APPROVE'd-but-failing merge. |
| `PIPELINE_REWORK_ON_CI_FAIL` | *(unset/off)* | When truthy (`1`), a **definitive** merge-gate CI failure (`_ci_status` state `fail` — not `pending`, not `cancelled`, and not a transient rebase/push error) on an already-APPROVE'd branch is routed back to the implementer as rework feedback instead of retrying the unchanged branch toward terminal `failed`. Exists because the reviewer is acceptance-scoped and can APPROVE a story whose own committed test file is broken — the full-suite CI gate then blocks the merge with no way for the agent to fix it (observed 2026-07-17: gpt-oss `retry_backoff`/`token_bucket`, ground-truth-correct code abandoned over the agent's own self-contradictory test). Bounded by `PIPELINE_MERGE_MAX_ATTEMPTS` via the `merge_attempts` counter (which persists across the rework→review-APPROVE→merge-gate cycle, unlike `rework_attempts` which the review-APPROVE path resets) — `MERGE_MAX_ATTEMPTS` rework rounds, then the existing terminal-fail fall-through. Does **not** change what the CI gate checks (still the full suite, by design — see `_ci_status_stub`'s docstring on catching merged-but-wrong) — only what happens on a failure. |
| `PIPELINE_REVERIFY_FULL_SUITE` | `1` | For a story **without** an `acceptance` block, whether the pre-merge re-verification (`_reverify_acceptance`) runs the full worktree suite (`1`) or skips it (`0`). Stories with an `acceptance` block always re-verify against the scoped oracle regardless. |
| `PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE` | `1` | Rework budget for a story carrying a non-empty `acceptance` block — lower than `PIPELINE_REWORK_MAX_ATTEMPTS` because an oracle-backed story already has an objective, pre-verified correctness signal (it only reaches review after tests, including the oracle, pass); a reviewer that keeps finding beyond-oracle issues mostly spends cycles rather than changing the outcome, so a lower cap parks it for human review faster. Falls back to `PIPELINE_REWORK_MAX_ATTEMPTS` for any story without a truthy `acceptance` list. Superseded by `PIPELINE_REWORK_MAX_ATTEMPTS_ESCALATED` once a story is escalated (see below). |
| `PIPELINE_REWORK_MAX_ATTEMPTS_ESCALATED` | `3` | Rework budget for a story that has already been escalated to Claude (`story["escalated"]`), taking priority over `PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE` regardless of whether the story also carries an acceptance oracle. `_ORACLE`'s tight cap exists to converge *local* review fast; once escalation has already paid its cost (real Claude usage, and per 2026-07-04's benchmark validation, sometimes real wall-clock time if the Claude reviewer gets rate-limited), reusing that same cap just throttles Claude's shot at the same feedback for no benefit — 6 of 11 escalated cells in that run parked after exactly one post-escalation cycle. |
| `PIPELINE_LOCAL_MAX_RISK` | `low` | Highest story risk the `auto` router sends to the local agent: `low` \| `medium` \| `high`. Stories above this threshold go straight to Claude. Security-persona stories always go to Claude regardless of this setting. |
| `PIPELINE_STEP_CAP_FALLBACK_THRESHOLD` | `3` | Consecutive same-model step-cap interrupts before a story's `model` is switched to the plan's `local_model_fallback` (see below). Only takes effect on plans that set that manifest field. On a plan with **no** `local_model_fallback` set, the same threshold instead gates escalation **to Claude** under `PIPELINE_BACKEND_DISPATCH=auto` (see routing item 2 below) — a story with no fallback configured no longer hits the step cap indefinitely on the same model under `auto`; outside `auto` (explicit `local`/`claude`), behavior is unchanged. |
| `PIPELINE_BACKEND_DIAGNOSIS` | *(unset)* | Backend for the **diagnosis** role, which turns a step-capped or failed attempt's evidence into a root-cause statement folded into the next attempt's `agent_instructions` (see "Step-cap rebrief" below): `claude` \| `ollama` \| `lmstudio` \| `mlx` \| `local`. Resolution priority: a plan's `role_config.diagnosis` → this env var → `model_registry.json`'s `roles.diagnosis` → **the story's own local backend and model**. That last default never selects `claude`/`auto`, so an unconfigured diagnosis role can't quietly spend Claude budget; set this to give a struggling local story a stronger diagnoser than the model that got stuck. |
| `PIPELINE_REBRIEF_TEST_RERUN` | `1` | Whether the rebrief re-runs the specific tests the last recorded run reported failing (up to 3 node ids, 180s cap, pytest-style runners only) to capture a real traceback for the next attempt. The stored `last_test_check.stdout_tail` is only the last 2000 characters of test output, which on a multi-failure run is the `FAILED <name>` summary and nothing else — the tracebacks scrolled past long before, so the diagnosis role and the resumed agent never saw the actual failing line. Set `0` to skip the re-run (the rest of the measured-facts block still applies). |

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

   **Step-cap rebrief (what the next attempt is told).** Whichever of those
   paths a stalled attempt takes, `pipeline/rebrief.py` folds two blocks into
   the story's `agent_instructions` before it is re-dispatched, so the resume
   is not a blind retry (CLAUDE.md Step 9):

   - A `PRIOR-ATTEMPT FACTS` block — **measured**, not inferred, and therefore
     present even when no diagnosis model is configured at all. It reports the
     attempt's diff against its base commit (including a `NO CODE CHANGE`
     callout when nothing landed and a `POSSIBLE CLOBBER` callout when a file's
     diff has the shape of a whole-file rewrite), how the attempt spent its
     steps (tool histogram, plus explicit callouts when it never called an edit
     tool or never ran the tests), which harness guards fired on it
     (read-heavy/repetition/parking/`str_replace`-fail/churn nudges), how often
     its context was trimmed, and — unless `PIPELINE_REBRIEF_TEST_RERUN=0` — a
     fresh re-run of the previously-failing tests with a full traceback. Only
     the last attempt is measured: `agent.log` is appended across resumes, so
     the log is sliced at its final `[boot]` line first. The block is bounded
     (3000 characters) and replaced, never stacked, on each new attempt — a
     facts block describing an attempt that has since been superseded would
     point the next one at evidence that is no longer true.
   - A `PRIOR-ATTEMPT DIAGNOSIS` block — the **diagnosis** role's root-cause
     statement (see `PIPELINE_BACKEND_DIAGNOSIS`). Optional and fail-open: an
     unconfigured or erroring diagnosis role leaves this block out entirely
     and never blocks the redispatch. The measured facts are fed to it as
     evidence and it is instructed to ground its answer in them, so it cannot
     invent a code defect for a branch git reports as having no diff.

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
`ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`,
`ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`,
`CLAUDE_CODE_USE_BEDROCK`, and `CLAUDE_CODE_USE_VERTEX` are removed before the
call, regardless of what the
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
          /etc/newsyslog.d/com.fagan.pipeline.conf
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
