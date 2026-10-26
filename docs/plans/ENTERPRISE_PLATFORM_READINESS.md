# Enterprise platform readiness — findings

- **Status:** assessment only, written 2026-10-25. Nothing here is scheduled
  except the Tier 1 prep plan `enterprise-prep-tier1`. Read this as a backlog
  of known gaps, not as an implementation plan.
- **Companion records:** [ADR 0006](../adr/0006-decision-routing-on-a-shared-server.md),
  [ADR 0007](../adr/0007-plan-approval-at-activation.md),
  [ADR 0008](../adr/0008-shared-control-plane-per-repo-workers.md), and
  [`SHARED_SERVICE_TOPOLOGY_PLAN.md`](SHARED_SERVICE_TOPOLOGY_PLAN.md).

## Verdict

| Area | Current state | Enterprise-ready? | Gap class |
|---|---|---|---|
| Eventing | `EventBus` ABC, in-process bus, durable notification outbox and channel layer | **Seam is right, semantics are not** | Event contract + consumer model |
| Observability | Strong per-story forensics; weak service-level | **Not platform-grade** | JSON app logs, request correlation, metrics sink |
| Auditability | Decisions, journals, notifications, agent logs | **Not audit-grade** | Actor identity + tamper-evidence |
| AuthN / AuthZ | One shared secret; origin headers explicitly not auth | **Not abstracted** | `Principal` + `IdentityProvider` + service-layer policy |

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

- **Application logs are not structured.** There is no JSON formatter
  configured in-repo; the MCP servers call `logging.basicConfig`, whose
  default formatter drops `extra={...}`. Structured fields survive only in the
  explicit JSONL sidecars. This is a direct mismatch with the project's own
  logging standard.
- **No request-level correlation.** `correlation_id` is minted per *story* at
  dispatch and propagated to the agent subprocess, notifications, events, and
  logs. The dashboard and MCP paths do not assign one per inbound request and
  do not return one in an error response.
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

- **No actor identity.** Nothing stamps the human caller on ingest, dispatch,
  pause, approve, or answer. `decided_by` exists only for overlord merge
  rulings. This is the same prerequisite as ADR 0006 / 0007 / 0008.
- **Not tamper-evident.** `_append_decision` is a JSON-array
  read-modify-write in a user-writable directory; there is no hash chain,
  signature, WORM storage, or external shipping.
- **No unified audit stream.** Decisions, journals, notifications, events, and
  security-audit state are separate files with different shapes.
- `record_security_audit` records *that* an audit happened and at which SHA,
  not a detailed action trail. Authentication success/failure and
  authorization-denial events do not exist yet; they must be added when authn
  lands.

For enterprise use: one append-only, hash-chained (or externally shipped)
audit stream with `actor`, `action`, `target`, `outcome`, `correlation_id`,
and `timestamp`. The ADR 0007 approval record should be its first tenant.

---

## 4. AuthN / AuthZ and the IDP seam

### Current state

- `app/auth.py` is one shared secret in a 0600 file, a `Host` allowlist for
  DNS-rebinding defense, and `X-Pipeline-Origin` for UI/chat origin. The module
  states plainly that the origin header is **not** an authentication decision.
- The MCP path has no authentication at all (`mcp.run()` is stdio).
- There is no `Principal`, `Role`, `Permission`, `Scope`, OIDC, or JWT type
  anywhere. `app/role_registry.py` holds **model** roles, not user roles; keep
  that naming separate.
- The chat tool registry excludes privileged tools (`approve_merge`,
  `set_story_status`, `risk`) as a hardcoded allowlist — a coarse authorization
  list, not a policy layer.

### The right abstraction is two layers

**AuthN is an adapter concern.** Add an `IdentityProvider` Protocol (in
`app/auth.py` or a new `app/identity.py`):

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

---

## 5. Deferred low-hanging fruit, ranked

These are behavior-preserving candidates for a later Tier 1-style plan. None
is registered today.

| # | Item | Default-preserving shape | Value | Blocker |
|---|---|---|---|---|
| 1 | Request correlation middleware | Generate a request ID when absent, echo it in an `X-Correlation-ID` response header and structured logs. No error-body change. | Closes the per-request correlation gap; high value for support. | None in `app/dashboard.py` (874 lines). MCP/stdio needs the remote transport first. |
| 2 | Opt-in JSON log formatter | `PIPELINE_LOG_FORMAT=json`; unset keeps the current plain output. | Closes the dropped-`extra` gap; prerequisite for a log pipeline. | Wiring the entrypoints touches `pipeline/server.py` (1000 lines) and `pipeline/scheduler_daemon.py` (999) — both at the hard line gate. A split is needed first, or configure from `pipeline/__init__.py`. |
| 3 | Optional event-envelope fields | Add `event_id`, `schema_version`, `idempotency_key` only when supplied; the legacy dict shape is unchanged. | Preps a broker without changing today's semantics. | `pipeline/events.py` has room; tests pin only the no-optional-args shape. |
| 4 | Event bus factory | `PIPELINE_EVENT_BUS=inprocess`, unknown fails closed. | Makes a broker adapter selectable. | Cosmetic until a second bus exists; pair with #3 (ADR 0004's warning about invisible gates). |
| 5 | `IdentityProvider` / `Principal` seam | Wrap the existing shared-secret check in a Protocol with a `SharedSecretProvider`; behavior identical. | Creates the IDP seam. | Speculative until an OIDC provider is imminent; defer unless it is. |

### Not low-hanging (the fork)

A broker consumer worker; a metrics/OTel sink; a tamper-evident audit stream;
OIDC plus RBAC/ownership/scope; request-scoped repo/config; and a
`scheduler_daemon.py` / `pipeline/server.py` split. These are the multi-tenant
implementation, not prep.

---

## 6. Concrete blockers found during Tier 1 preflight

These are mechanical facts that will bite the next plan author:

- **Line-limit gate.** `pipeline/server.py` is exactly 1000 lines and
  `pipeline/scheduler_daemon.py` is 999. Adding a wiring line to either trips
  `tests/unit/test_check_line_limit.py`,
  `tests/unit/test_dashboard_split.py`, and
  `tests/unit/test_scheduler_timeouts_extraction.py`. This is why the Tier 1
  topology story is scoped to `/api/health` only; the scheduler health-file
  half waits for a split (e.g. moving `config_fingerprint` out).
- **Source-digest guards.** `tests/unit/test_service_active_workspace.py`
  pins SHA-256 digests of `PipelineService.save_plan`, `resolve_workspace`,
  and `list_workspaces` as do-not-modify guards. Any story that intentionally
  changes a guarded function must pre-authorize the exact re-pin in its brief.
- **Verbatim-pinned REFERENCE.md section.** `REFERENCE.md`'s
  `## Configuration (environment variables)` body is compared byte-for-byte by
  `tests/unit/test_readme_reference_split.py`. New env vars belong in a new
  spec doc or a new H2 section, never inside that table.

## Cross-references

- [`docs/specs/REMOTE_EXECUTION.md`](../specs/REMOTE_EXECUTION.md) — the
  existing execution seam; explicitly "not a remote queue or a multi-host
  scheduler."
- [`docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md`](PLATFORM_DECOUPLING_AND_SCALE_PLAN.md)
  — W4 / T3 history.
- `enterprise-prep-tier1` — the three behavior-preserving prep stories
  registered 2026-10-25.
