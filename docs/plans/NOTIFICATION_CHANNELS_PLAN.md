# Notification channels plan — Slack / Teams delivery

> Draft plan document, 2026-10-21. **Not ingested.** This is a persisted
> design/scope document to revisit later and decompose into an ingestable
> plan (per `.claude/rules/pipeline-story-schema.md`) when we're ready to
> execute. Nothing here has been preflighted against the local-dispatch
> checklist; that happens at decomposition time.

## Goal

Today the pipeline's outbound notification channel is email only
(`pipeline/notification_email.py`). Add the ability to push the same
spooled notifications to Slack and/or Microsoft Teams, following the
architecture's existing seams — without changing any emitting code.

## Current architecture (verified 2026-10-21)

Three seams, all already channel-agnostic:

1. **Bus → sinks** (`pipeline/event_wiring.py:62-63`): every `notification`
   bus event fans out to `file_log_sink` and `outbox_sink`. Sinks perform no
   network I/O and never raise into the bus.
2. **Outbox decouples send from the tick**
   (`pipeline/notification_outbox.py`): allowlisted structured events
   (`plan_completed, story_parked, dispatch_failed, tests_failed,
   agent_gave_up` by default) are spooled to `<plan>.outbox.jsonl`.
   `drain_outbox(plan, sender)` replays each record through an **injected
   `sender` callable** — any `record: dict -> bool`. Delivery is
   at-least-once: rejected or raising sends are retained atomically for the
   next drain; the rewrite merges records appended concurrently.
3. **Sender injection point** (`pipeline/scheduler_daemon.py:689`): the
   scheduler's drain phase binds the email sender:
   `drain_outbox(ALL_PLANS, send_notification_email)`. This is the *only*
   place the email channel is bound.

`send_notification_email` (`pipeline/notification_email.py:74`) is one
implementation of that contract: env knobs read at call time, disabled by
default (`PIPELINE_NOTIFY_EMAIL_ENABLED` must be exactly `1`), fail-closed
on partial config, credential never logged, never raises.

## Proposed design

### New module(s)

One new sender module per channel (or one generic webhook module — see
Open Questions), each implementing `record: dict -> bool`:

- **Slack**: POST `{"text": <rendered message>}` to an incoming webhook URL.
- **Teams**: POST a MessageCard/Adaptive Card payload to a connector webhook
  URL.

Both follow `notification_email.py`'s conventions: env config read at call
time, disabled-by-default knobs, fail-closed on partial config, the webhook
URL treated as a credential (never logged, never in exception text), no
payload logging at INFO or below, never raises, stdlib only (`urllib` — no
new dependency).

### Shared rendering extraction

The record → subject/body logic in `notification_email.py:127-154`
(`SUBJECT_LABELS`, `STORY_LEVEL_EVENTS`, subject construction, legacy
fallbacks) is channel-independent. Extract it into a small shared renderer
(e.g. `pipeline/notification_render.py`) that all senders import, so channel
formatting can't drift. The extraction story is behavior-preserving — email
output must be byte-identical before and after.

### Composition at the injection point

Replace the single sender at `pipeline/scheduler_daemon.py:689` with a
composite sender built from whichever channels are enabled, e.g.
`drain_outbox(ALL_PLANS, composite_sender([...]))`. The composite's boolean
(return `True`/retire policy) is a real decision — see Open Questions.

### Configuration knobs (naming follows the email precedent)

| Variable | Default | Purpose |
|---|---|---|
| `PIPELINE_NOTIFY_SLACK_ENABLED` | `0` | Channel off unless exactly `1` |
| `PIPELINE_NOTIFY_SLACK_WEBHOOK_URL` | `""` | Slack incoming-webhook URL (secret) |
| `PIPELINE_NOTIFY_SLACK_TIMEOUT` | `20` | HTTP timeout seconds |
| `PIPELINE_NOTIFY_TEAMS_ENABLED` | `0` | (if Teams in scope) |
| `PIPELINE_NOTIFY_TEAMS_WEBHOOK_URL` | `""` | (if Teams in scope) |
| `PIPELINE_NOTIFY_TEAMS_TIMEOUT` | `20` | (if Teams in scope) |

New knobs must be registered in `pipeline/config_provenance.py` (the
`EnvVarSpec` list near line 255) and documented in `REFERENCE.md`. URL
scheme must be validated as `https` at send time; a non-HTTPS URL fails
closed.

## Security requirements (invariant, from CLAUDE.md / Secure by Design)

- Disabled by default; exact-`"1"` opt-in, read per call.
- Partial config = ERROR log + `False` return, never a send attempt.
- Webhook URL is a credential: never logged, never in exception messages,
  masked anywhere it appears outside its env var.
- Only the exception *type* is logged; notification payloads are never
  logged at INFO or below.
- `never raises` contract, same as the email sender — the drain loop calls
  senders per record and must not break the scheduler tick.
- No new dependencies; stdlib HTTP with a verified TLS context.

## Candidate story breakdown (for later decomposition)

Roughly in dependency order; sizing/tier decisions deferred to
decomposition time:

1. **Extract `notification_render.py`** from `notification_email.py`:
   move `SUBJECT_LABELS`, `STORY_LEVEL_EVENTS`, and the
   record → (subject, body) rendering unchanged; email sender imports it.
   Behavior-preserving; existing email tests are the grade ([no-new-tests]
   may apply).
2. **Slack webhook sender** (`pipeline/notification_slack.py`) + unit tests
   covering: disabled gate, partial-config fail-closed, non-HTTPS URL
   rejected, timeout knob, malformed-record fail-closed, success path
   (mocked socket at the HTTP boundary only), and a no-URL-in-logs
   assertion. Sender uses the shared renderer.
3. **Teams webhook sender** (same shape as story 2) — *only if Teams is in
   scope.*
4. **Composite sender + wiring**: composite callable that fans one record
   to N senders per the agreed retire policy; swap in at
   `scheduler_daemon.py:689`; register the new env vars in
   `config_provenance.py`; update `REFERENCE.md`.
5. **Docs/contract**: REFERENCE.md notification section, README if setup
   steps change.

Note for the future decomposer: REFERENCE.md is ~1330 lines, over the
~1000-line weak-tier file cap (`agent-dispatch-story-sizing.md`) — any
story editing it must be routed to cloud open-source or Claude, or the doc
edit folded into a story with a stronger tier.

## Open questions (resolve before decomposition)

1. **Channel scope**: Slack and Teams both, or one? Or a single
   *generic webhook* sender (plain-text POST to any configured URL)
   with channel-specific formatting deferred? The generic option is the
   smallest viable change; both dedicated senders is ~2x story 2.
2. **Retire policy for the composite sender**: retire a record when *at
   least one* enabled channel accepted it, or only when *all* did?
   At-least-once semantics differ meaningfully: "all" retries a record on
   the succeeding channels too (duplicate messages); "any" can silently
   drop a permanently-misconfigured channel's failures.
3. **Per-channel event routing**: today the outbox allowlist
   (`PIPELINE_NOTIFY_OUTBOX_EVENTS`) is channel-agnostic. Do we want
   e.g. Slack to receive more event types than email? Out of scope for V1
   unless explicitly requested.
4. **Dispatch tier at execution time**: the live registry currently pins
   `dispatch` to `ollama/deepseek-v4.1-flash` (tag `:cloud`) —
   cloud open-source tier. Confirm it again when decomposing; tier drives
   story sizing per CLAUDE.md Step 1.
5. **Pre-existing-test impact**: the preflight impact runs
   (`local-dispatch-preflight.md` §1) have NOT been run against these
   changes. Candidates likely to pin current behavior: tests of
   `notification_email.py`'s internals (the extraction in story 1 must
   not break import-level references or monkeypatched names — check
   `tests/` for `SUBJECT_LABELS`/`send_notification_email` patches), and
   `config_provenance` tests that may pin the env-var list cardinality.

## Explicit non-goals (V1)

- No interactive Slack/Teams features (buttons, threading, approvals in
  chat).
- No inbound webhooks or two-way integration.
- No change to which events are emitted or to the outbox format.
- No change to the file-log sink.
- No new third-party dependencies.
