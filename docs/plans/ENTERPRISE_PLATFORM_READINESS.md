# Enterprise platform readiness — findings

- **Status:** assessment only, written 2026-10-25. Three plans came out of it and
  all shipped 2026-10-26: `enterprise-prep-tier1` (the three Tier 1 prep
  stories), `enterprise-plan-approval` (the ADR 0007 gate, spec
  [`docs/specs/PLAN_APPROVAL.md`](../specs/PLAN_APPROVAL.md)), and
  `tenancy-seams` (the identity/policy seam only, spec
  [`docs/specs/IDENTITY_AND_POLICY.md`](../specs/IDENTITY_AND_POLICY.md)).
  Nothing else here is scheduled. Read it as a backlog of known gaps, not as an
  implementation plan.
- **Companion records:** [ADR 0006](../adr/0006-decision-routing-on-a-shared-server.md),
  [ADR 0007](../adr/0007-plan-approval-at-activation.md),
  [ADR 0008](../adr/0008-shared-control-plane-per-repo-workers.md), and
  [`SHARED_SERVICE_TOPOLOGY_PLAN.md`](SHARED_SERVICE_TOPOLOGY_PLAN.md).

## Verdict

| Area | Current state | Enterprise-ready? | Gap class |
|---|---|---|---|
| Eventing | `EventBus` ABC, in-process bus, durable notification outbox and channel layer | **Seam is right, semantics are not** | Event contract + consumer model |
| Observability | Strong per-story forensics; weak service-level | **Not platform-grade** | Metrics sink and tracing; JSON app logs and request correlation now exist but are opt-in |
| Auditability | One append-only, hash-chained stream per plan (`<plan>.audit.jsonl`) carrying actor, action, target, outcome, correlation id and timestamp — enforcement decisions, approvals, dispatch provenance, merges and pause/resume; decisions, journals and notifications keep their own files | **Tamper-evident; head anchor opt-in, file-only** | A sink the pipeline host cannot reach (HTTP/S3 with object lock, or a WORM export) so truncation is detectable without a local file; fold the remaining sidecars in |
| AuthN / AuthZ | Shared secret by default, or OIDC bearer-token verification selected by `PIPELINE_IDENTITY_PROVIDER`; `Principal` / `IdentityProvider` / `Authorizer` seam with deny-by-default service-layer enforcement for every action in the `Action` enum, every enforced decision audited; plan owners and delegates may approve, reject and merge their plan; an OIDC-gated remote MCP transport serves an explicit tool allowlist behind the same principal | **Seam, not a control** | Mutating tools outside the `Action` enum are unchecked; reads are not tenant-scoped; decisions are not routed to the plan owner; OIDC covers API clients only (no browser login); the remote MCP transport has no TLS of its own and no rate limiting at the gate |

---

## 1. Eventing

### What exists and is worth keeping

- `pipeline/events.py` defines an `EventBus` ABC (`publish`/`subscribe`) and a
  single `make_event(type, plan, story_key, payload, correlation_id)` contract.
  The contract validates `type` against `EVENT_TYPES` and stamps an ISO-8601
  `ts`.
- Every producer publishes through `get_bus().publish(...)`: `dispatch.py`
  (`agent_dispatched`), `persistence.py` (`notification`),
  `worktree_patch.py`, and `watchers.py` (`agent_done`). The producer side is
  therefore swappable without touching call sites.
- `pipeline/notification_channels.py` is the most broker-ready module in the
  system: a `Channel` Protocol, a type registry, per-instance outboxes, and a
  real `DeliveryResult` / `DeliveryStatus` (`DELIVERED`, `RETRY`, `REJECTED`,
  `retry_after_seconds`). `notification_outbox.py` spools per-plan JSONL and
  retains a record until a sender accepts it. That is an outbox plus retry
  pattern already.

### What is not broker-ready

- Production is **poll plus in-process wake**, not event-driven.
  `event_wiring.build_bus()` hardcodes `InProcessEventBus`;
  `watchers.scan_all_plans()` walks every manifest on the scheduler interval
  looking for `.agent_done` marker files. The marker rename is the claim, and
  the docstring deliberately accepts a dropped completion (reconcile catches
  it). `JsonlEventBus` exists but is test-only, and its `drain()` truncates
  after dispatch (at-most-once).
- The envelope has no `event_id`, schema version, partition key, actor,
  causation ID, or idempotency key.
- `subscribe` is an in-process callback registry. There are no consumer
  groups, offsets, or ack/nack. `wake_handler` reads `PLAN_DIR` directly and
  calls `advance_pipeline` synchronously, so the consumer is coupled to the
  filesystem and the monolithic tick.
- The bus has no retry or dead-letter path; `InProcessEventBus.publish` logs
  and swallows handler exceptions. Only notifications have retry semantics.

### Migration shape (all additive)

1. Enrich the envelope: `event_id`, `schema_version`, `partition_key=plan`,
   `correlation_id`, `actor`, `idempotency_key`.
2. Split producer from consumer: keep `publish` as the enqueue point, move
   `wake_handler` into a consumer worker that loads state through `Store`, not
   `PLAN_DIR`.
3. Make the bus factory config-selected (default `inprocess`, unknown fails
   closed), and adopt the notification layer's `DeliveryResult`/retry shape
   for bus consumption.
4. Generalize the outbox beyond notifications.
5. Long term, let agents/workers publish `agent_done` directly; the marker
   scanner becomes a fallback rather than the trigger.

`build_bus()` always returns `InProcessEventBus`; `JsonlEventBus` is
test-only. The notification channel layer is the template for the rest.

---

## 2. Observability

### What exists and is strong

- Canonical agent-log grammar (`pipeline/agent_log_format.py`) with
  `[cid=...]`, per-phase logs, and `.log.ts` sidecars.
- Durable structured sidecars: notification JSONL, decisions, journals.
- Health and divergence detection: `/api/health`, `/api/dispatch_health`,
  scheduler health file plus config fingerprint
  (`scheduler_daemon.config_fingerprint`, `pipeline/config_provenance.py`),
  guard liveness, and security-audit-due checks.
- Per-story cost metrics (`pipeline/story_metrics.py`) and
  `/api/plans/{plan}/metrics`.

### Gaps

- **Application logs are structured only on request.** An opt-in
  `PIPELINE_LOG_FORMAT=json` formatter now renders the standard fields
  (`timestamp`, `level`, `service`, `correlation_id`, `message`) and preserves
  every `extra=` key instead of dropping it. It is off by default, so an
  unconfigured process still logs plain text. `correlation_id` reaches log
  lines because the formatter reads the request-scoped id, and is empty outside
  a request.
- **Request correlation exists on the dashboard path only.** The dashboard
  middleware mints or preserves one id per inbound request and returns it in the
  `X-Correlation-ID` response header on every response, including the 401/403
  refusals, and stamps it as `correlation_id` on the decisions record and the
  plan-approval record. A story's `correlation_id` is still minted per *story* at
  dispatch and propagated to the agent subprocess, notifications, events, and
  logs. What still does not exist: the stdio MCP path has no request to
  correlate, so those calls record an empty id. The HTTP MCP transport does not
  have that gap — its gate mints or preserves a correlation id per request,
  echoes it in `X-Correlation-ID`, and binds it for the tools it calls.
- **No tracing and no metrics sink.** There is no OpenTelemetry, Prometheus,
  StatsD, or span instrumentation. Metrics are computed on demand by scanning
  notification JSONL (`story_metrics.load_notification_records`), which is
  O(records) per request, not an emitted time series, and is not alertable.
- **Metrics derive from best-effort notifications.** Sinks swallow failures by
  design, so the metric source is not authoritative.

---

## 3. Auditability

### What exists

- Plan decisions log (`<plan>.decisions.json`), per-story journals
  (`<plan>.<story>.journal.json`), notification JSONL, agent logs, and a
  per-repo last-audited-SHA record (`pipeline/security_audit.py`).
- Overlord rulings carry `decided_by: "overlord"`.

### Gaps

- **Actor identity is partial.** Ingest stamps `ingested_by`, plan approval
  records `decided_by` and `role`, and dashboard decisions record `actor`; the
  dashboard binds a principal per request (`pipeline/actor.py`, see
  [the identity spec](../specs/IDENTITY_AND_POLICY.md#actor-propagation-pipelineactorpy)).
  Dispatch, story status changes, pause/resume and decision answers still record
  no actor, and no role is ever checked. This is the same prerequisite as
  ADR 0006 / 0007 / 0008.
- **One hash-chained stream per plan.** `<plan>.audit.jsonl` in the plan directory
  is append-only and chains each record to its predecessor through `prev_hash`;
  every record carries `actor`, `action`, `target`, `outcome`, `correlation_id`
  and a timestamp, and `verify_audit_chain` names the first altered record. The
  ADR 0007 approval record is its first tenant, joined by the authorization
  decisions, plan pause/resume, `story.dispatch` and `story.merge`.
- **External anchor: opt-in, file-only for now.** `PIPELINE_AUDIT_ANCHOR` ships
  each chain's `(seq, hash)` head to an append-only log on every daemon tick, and
  `verify_audit()` reports a truncation or a mismatch against it. It is off by
  default, and the only sink is `file:<absolute path>`: worth anything only where
  the pipeline host cannot rewrite that path (a WORM or object-lock mount, another
  host's append-only NFS export) — on local disk a forger who can rewrite the
  chain can rewrite the anchor too. Nothing is stored write-once by the pipeline
  itself, an HTTP or S3/WORM sink does not exist yet, and while the variable is
  unset truncation of the tail is undetectable, so a clean verification only means
  that no record in the middle was altered. Decisions
  (`<plan>.decisions.json`) and per-story journals
  (`<plan>.<story>.journal.json`) also keep their own files for now.
- `record_security_audit` records *that* an audit happened and at which SHA,
  not a detailed action trail. Authentication success/failure and
  authorization-denial events do not exist yet; they must be added when authn
  lands.

For enterprise use, this is the append-only, hash-chained stream with `actor`,
`action`, `target`, `outcome`, `correlation_id`, and `timestamp` this section
called for, and the ADR 0007 approval record is its first tenant. What a shared
server still needs is the external anchor that does not exist yet: shipping the
head hash somewhere the pipeline cannot rewrite it.

---

## 4. AuthN / AuthZ and the IDP seam

### Current state

- `app/auth.py` is one shared secret in a 0600 file, a `Host` allowlist for
  DNS-rebinding defense, and `X-Pipeline-Origin` for UI/chat origin. The module
  states plainly that the origin header is **not** an authentication decision.
- MCP authentication depends on the transport. The default stdio path
  (`mcp.run()`) still has no authentication at all; naming an HTTP transport
  (`PIPELINE_MCP_TRANSPORT=streamable-http` or `sse`) serves the same tools
  behind OIDC bearer verification, which refuses to start unless
  `PIPELINE_IDENTITY_PROVIDER=oidc`. That gate has no TLS of its own and no
  rate limiting.
- A `Principal` / `IdentityProvider` / `Authorizer` seam exists
  (`app/identity.py`, `app/policy.py`, wired into the dashboard middleware), and
  the gate is in the service: `pipeline/authorization.py` enforces it,
  deny-by-default, for every action in the `Action` enum.
  `PIPELINE_POLICY_ENFORCE` is the operator's kill switch; see
  [`docs/specs/IDENTITY_AND_POLICY.md`](../specs/IDENTITY_AND_POLICY.md).
  `app/role_registry.py` still holds **model** roles, not user roles; keep that
  naming separate.
- The chat tool registry excludes privileged tools (`approve_merge`,
  `set_story_status`, `risk`) as a hardcoded allowlist — a coarse authorization
  list, not a policy layer.

### The right abstraction is two layers

**AuthN is an adapter concern.** An `IdentityProvider` Protocol exists in
`app/identity.py` (`app/auth.py` cannot host it: it may import no other `app.*`
module):

- `SharedSecretProvider` — today's local single-operator behavior.
- `OidcProvider` — JWKS-backed JWT verification with issuer, audience, and
  expiry checks.
- Remote MCP verifies the same token at the HTTP transport layer and yields
  the same `Principal`.

A `Principal` carries subject, email, groups/roles, tenant/repo scopes, auth
method, and request/correlation ID.

**AuthZ is a service concern.** A policy interface
(`allow(principal, action, resource) -> bool`, deny by default) enforced in
`PipelineService`, so the dashboard, chat, and MCP adapters cannot bypass it.
That is ADR 0006's rule, and it is why the check must not live only in the
gateway.

The shared secret remains the default in local mode; the IDP provider is
selected by configuration and fails closed if misconfigured (ADR 0004). The
`Principal` must carry **repo/tenant scope**, so authorization is
`principal → team → repo → plan → story`, not a global role bit.

**What exists now.** OIDC verification has landed: `app/oidc.py` holds an
`OidcProvider` that verifies a bearer JWT against the issuer's JWKS, comparing
`iss` and `aud` against `PIPELINE_OIDC_ISSUER` / `PIPELINE_OIDC_AUDIENCE` and
requiring `exp`, `iss`, `aud` and `sub`. It is selected with
`PIPELINE_IDENTITY_PROVIDER=oidc` and yields a `Principal` with the token's
claims mapped onto it; the mapping (issuer-qualified subject,
group-to-role intersection, scope handling) and the full rule set are in
[`docs/specs/IDENTITY_AND_POLICY.md`](../specs/IDENTITY_AND_POLICY.md) §2,
"OIDC provider". Two limits matter: it authenticates **API clients only** — the
bundled dashboard has no login flow and mints no token, so under `oidc` its
pages load but every `/api/` call is a `401` without a bearer token — and the
`k-` chat-stream pass-through of shared-secret mode does not exist under it.

The remote MCP transport has landed too: `PIPELINE_MCP_TRANSPORT=streamable-http`
(or `sse`) serves the same tools over HTTP behind the same `OidcProvider`,
yielding the same `Principal` and binding it to the request. It refuses to start
without `PIPELINE_IDENTITY_PROVIDER=oidc`. What it does not have is TLS (put a
reverse proxy in front) or rate limiting at the gate. Remote callers are limited
to an explicit tool allowlist (`pipeline/remote_tool_policy.py`), a proxied host
name is accepted through `PIPELINE_MCP_ALLOWED_HOSTS`, and the transport
refuses to start with policy enforcement off — see
[REFERENCE.md](../../REFERENCE.md#remote-mcp-transport).

**Still outstanding.** Every action in the `Action` enum is enforced, but
mutating MCP tools outside the enum (`save_plan`, `set_story_status`,
`record_security_audit` and others) are not, and reads are not tenant-scoped.
Plan ownership is stored and enforced for `plan:approve`, `plan:reject` and
`story:merge`, but decisions are not yet routed to the owner. The audit
stream of allow/deny decisions exists locally (hash-chained per plan); what it
still lacks is the external anchor that would make tail truncation detectable.

---

## 5. Deferred low-hanging fruit, ranked

These are behavior-preserving candidates for a later Tier 1-style plan. Items 1,
2, and 5 have shipped; the rest are unregistered.

| # | Item | Default-preserving shape | Value | Blocker |
|---|---|---|---|---|
| 1 | Request correlation middleware | Generate a request ID when absent, echo it in an `X-Correlation-ID` response header and structured logs. No error-body change. | Closes the per-request correlation gap; high value for support. | **Shipped in `request-correlation`** — the header on every response and the id on the decision and approval records. The structured-log half is item 2, now shipped. |
| 2 | Opt-in JSON log formatter | `PIPELINE_LOG_FORMAT=json`; unset keeps the current plain output. | Closes the dropped-`extra` gap; prerequisite for a log pipeline. | **Shipped** — `pipeline/log_format.py`, wired into `app/pipeline_mcp_server.py`, `run_daemon`, and the dashboard startup preflight. Off by default. |
| 3 | Optional event-envelope fields | Add `event_id`, `schema_version`, `idempotency_key` only when supplied; the legacy dict shape is unchanged. | Preps a broker without changing today's semantics. | `pipeline/events.py` has room; tests pin only the no-optional-args shape. |
| 4 | Event bus factory | `PIPELINE_EVENT_BUS=inprocess`, unknown fails closed. | Makes a broker adapter selectable. | Cosmetic until a second bus exists; pair with #3 (ADR 0004's warning about invisible gates). |
| 5 | `IdentityProvider` / `Principal` seam | Wrap the existing shared-secret check in a Protocol with a `SharedSecretProvider`; behavior identical. | Creates the IDP seam. | **Shipped** — see [`docs/specs/IDENTITY_AND_POLICY.md`](../specs/IDENTITY_AND_POLICY.md). Service-layer enforcement landed afterwards for every action in the `Action` enum (ADR 0009 §2), with `PIPELINE_POLICY_ENFORCE` as the kill switch; the mutating tools outside the enum are listed as outstanding in §4. |

### Not low-hanging (the fork)

A broker consumer worker; a metrics/OTel sink; an audit-stream anchor outside
the pipeline (the per-plan hash-chained stream landed in AUD-06, but nothing
off-box witnesses its head hash yet); OIDC plus RBAC/ownership/scope;
request-scoped repo/config; and a
`scheduler_daemon.py` / `pipeline/server.py` split. These are the multi-tenant
implementation, not prep.

---

## 6. Concrete blockers found during Tier 1 preflight

These are mechanical facts that will bite the next plan author:

- **Line-limit gate.** `pipeline/scheduler_daemon.py` is 999 lines and
  `pipeline/server.py` was exactly 1000 until `ENT-LC-00` replaced the
  lifecycle-tool registrations with a `TOOLS` loop (994 now). Adding a wiring
  line to either trips `tests/unit/test_check_line_limit.py`,
  `tests/unit/test_dashboard_split.py`, and
  `tests/unit/test_scheduler_timeouts_extraction.py`. This is why the Tier 1
  topology story is scoped to `/api/health` only; the scheduler health-file
  half waits for a split (e.g. moving `config_fingerprint` out).
- **Source-digest guards.** `tests/unit/test_service_active_workspace.py`
  pins SHA-256 digests of `PipelineService.save_plan`, `resolve_workspace`,
  and `list_workspaces` as do-not-modify guards. Any story that intentionally
  changes a guarded function must pre-authorize the exact re-pin in its brief.
- **REFERENCE.md's pinned sections.** `tests/unit/test_readme_reference_split.py`
  compares most moved sections byte-for-byte, but the
  `## Configuration (environment variables)` section is explicitly exempt from
  the final comparison (it only asserts two known corrections), so adding a
  row to its env table is allowed — FORGE-S3 does exactly that and the test
  passes. The other moved sections still reject insertions inside their
  bodies.

## Cross-references

- [`docs/specs/REMOTE_EXECUTION.md`](../specs/REMOTE_EXECUTION.md) — the
  existing execution seam; explicitly "not a remote queue or a multi-host
  scheduler."
- [`docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md`](PLATFORM_DECOUPLING_AND_SCALE_PLAN.md)
  — W4 / T3 history.
- `enterprise-prep-tier1` — the three behavior-preserving prep stories
  registered 2026-10-25.
