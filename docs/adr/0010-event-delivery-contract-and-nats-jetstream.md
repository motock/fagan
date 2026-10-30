# 0010. Events have an at-least-once delivery contract; NATS JetStream is the default broker

- **Status:** Accepted 2026-10-29 (implemented by `eventing-broker-adapter`); rule 6 superseded by [0011](0011-scheduler-scale-out-leased-plan-advancement.md)
- **Date:** 2026-10-28

## Context

Every pipeline event today travels on `InProcessEventBus`
(`pipeline/events.py`, wired in `pipeline/event_wiring.py`). Handlers run
synchronously inside the publishing process, an exception in a handler is
logged and dropped, and nothing survives a crash. That works for one process on
one host. It stops working at ADR 0008 stage 3, where workers on other hosts
report `agent_done` and heartbeats to a shared control plane.

Three current behaviors constrain any broker:

- `persistence._notify_user` decides whether to write a notification itself by
  reading the bus's private `_handlers` map. If the consumer lives in another
  process, the publisher sees no handler, writes the notification directly, and
  the consumer writes it again.
- `watchers.scan_done_markers` deliberately never re-publishes an `agent_done`
  whose marker it has already claimed. A double publish piles up plan locks,
  and the reconcile sweep already recovers a lost completion within one tick.
  `wake_handler` runs `event_guards.check_precondition`, which returns a
  stale or redelivered event unchanged, and `advance_pipeline` returns
  `skipped: locked` while another tick holds the plan.
- Event volume is low: thousands per day, not millions per second. The
  per-message semantics matter far more than throughput: retry a failed sink,
  dead-letter it after N attempts, and suppress duplicates.

The broker options were compared in conversation on 2026-10-28:

- **Kafka:** a partitioned log, very high throughput, the largest ecosystem.
  Per-message retry and dead-lettering are not native, and it means at least one
  JVM broker.
- **Pulsar:** native multi-tenancy and geo-replication, but three components to
  operate.
- **NATS JetStream:** a single Go binary. Per-message acknowledgement,
  redelivery, a maximum delivery count and publish-side duplicate suppression
  are built in, and accounts isolate tenants.

## Decision

**We define a broker-neutral delivery contract, and NATS JetStream is the
default broker that implements it.** The in-process bus remains the default
when `PIPELINE_EVENT_BUS` is unset.

The contract has two tiers. Every `EventBus` implementation satisfies rules 4
and 5. A **durable** implementation, meaning any broker adapter, also satisfies
rules 1–3 and 6. `InProcessEventBus` is best-effort by design: a handler
exception is logged and the event is not redelivered. It stays that way.

1. **At-least-once delivery to durable subscribers.** A handler's message is
   acknowledged only after the handler returns. If the handler raises, the
   message is redelivered with backoff, up to a bounded count
   (`PIPELINE_EVENT_MAX_DELIVER`, default 5). It is then dead-lettered to
   `pipeline.deadletter`, and an `event_dead_lettered` notification is
   written directly to the plan's sidecar, never through the bus that just
   failed.
2. **Handlers are idempotent.** There is no exactly-once delivery. Duplicates
   are suppressed at publish time by message id (the `event_id`, or the
   `idempotency_key` when there is one) within a dedup window. A handler must
   still tolerate a replay; `wake_handler` already does.
3. **No ordering guarantee across events.** Per-subject order holds only while
   nothing is redelivered. Consumers must not depend on order; state lives in
   the manifest, not in the event sequence.
4. **Publishing is synchronous and bounded.** `publish` returns once the broker
   has persisted the event (a PubAck), or raises `EventPublishError` after
   `PIPELINE_EVENT_PUBLISH_TIMEOUT_S`. Call sites keep their existing
   fallbacks. `_notify_user` writes directly, and the watcher relies on the
   reconcile sweep.
5. **Delivery is explicit.** `EventBus.delivers(event_type) -> bool` replaces
   every read of a private handler map. It answers "will a subscriber, in this
   process or a durable remote consumer, receive this type?"
6. **Exactly one consuming process per deployment.** The scheduler daemon
   consumes; MCP servers and the dashboard only publish.

The NATS JetStream mapping:

- Subjects are `pipeline.<event_type>.<plan>`. Plan names already pass
  `_validate_key`, and event types come from the fixed `EVENT_TYPES` set, so no
  caller-controlled text reaches a subject token unchecked.
- There is one stream, with a configured maximum age and size.
- Durable pull consumers are named per event type.
- `Nats-Msg-Id` carries the dedup id.
- A tenant or trust boundary is a NATS account (ADR 0008's federation).
- Credentials come from a creds file with mode 0600. Any URL that is not
  loopback requires TLS (fail closed, ADR 0004).
- The pipeline creates the stream only when `PIPELINE_NATS_MANAGE_STREAM=1`;
  otherwise it requires an existing stream and needs only publish/consume
  permissions.

## Consequences

- A second broker, Kafka first, can be added as another implementation that
  passes the same contract suite. Kafka would need a retry topic and a
  dead-letter topic to provide rule 1, because Kafka acknowledges by offset, not
  per message.
- One more service to run and secure in a shared deployment. On one host with
  a single process it buys almost nothing over the in-process bus and the
  notification outbox. Leave it off there.
- Rule 3 is a constraint on every future handler. A handler that reads "the
  previous event" will break under redelivery. Do not "fix" that by adding
  ordering to the bus; read the manifest.
- Dead-lettered notifications must still reach a human, which is why the
  dead-letter notice bypasses the bus.
- `nats-py` is asyncio-only, so the synchronous facade runs a private event
  loop thread per process. That is a source of shutdown and fork hazards, so
  the facade must be created lazily and never inherited across a fork.

Related: [0004](0004-safety-gates-default-on-and-fail-closed.md),
[0008](0008-shared-control-plane-per-repo-workers.md),
`docs/plans/EVENTING_BROKER_ADAPTER_PLAN.json`.
