# Telemetry contract

This document is the contract that `pipeline/telemetry.py` and the usage record
must satisfy. It fixes the planes the harness emits to, the audiences each
profile serves, the attributes every signal carries, the naming rule, the data
that may never leave the process, the shape of a usage record, and the retention
and tenancy rules. Code that violates a clause here is wrong, even if the
numbers it produces look plausible.

## Planes

Fagan has three planes, and they are not interchangeable. The **telemetry**
plane is OTLP traces and metrics. It is lossy and sampled: the exporter
batches, the collector may drop, and a sampling decision may discard a span
before it is ever written. Telemetry is a projection of state, never a source of
truth, and nothing reads it back as state. No reconciliation, no billing total
and no compliance report is derived from it.

The **events** plane is the at-least-once broker contract described in
`docs/adr/0010-event-delivery-contract-and-nats-jetstream.md`. It is the
integration surface other systems attach to, and the notification outbox is the
durable record of what was published, so a consumer that comes back after an
outage replays from the outbox rather than from telemetry.

The **evidence** plane is the hash-chained audit stream in `pipeline/audit.py`
with the external anchor maintained by `pipeline/audit_anchor.py`. It is the only
plane that supports compliance claims. Audit evidence and billing-grade counts
never rely on the telemetry plane: sampled, lossy spans cannot prove that
something happened or that it happened exactly once, while the audit chain and
the usage record can.

## Profiles

| Profile | Audience | Contents |
| --- | --- | --- |
| `customer` | The tenant's organization and team operators | Low-cardinality, enum-bounded metadata only: outcomes, statuses, counts, durations |
| `harness` | The operator improving the harness | Everything in `customer`, plus per-attempt attributes: role, backend, model, attempt number, failure reason, token counts, estimated cost; still metadata only |

Content capture — prompts, briefs, agent transcripts, diffs — is a separate
opt-in per namespace. It is off by default, retained separately with a short
retention, and reachable only under restricted access. The `customer` profile can
never enable it: enabling content capture is an operator action on a deployment,
not a tenant setting.

One worked trace shows the split. A single model call carries `role=dispatch`,
`backend=claude`, `model=claude-sonnet-4-5`, `attempt=2`, outcome `success`,
`duration_ms=2340`, `input_tokens=1200`, `output_tokens=350`,
`cache_read_input_tokens=800`, `cache_creation_input_tokens=0` and
`cost_usd=0.0042`. The `customer`-profile signal carries only
`fagan.outcome=success`, `fagan.status=ok` and `fagan.duration_ms=2340`, plus the
mandatory resource attributes. The `harness`-profile signal carries those *and*
`fagan.role=dispatch`, `fagan.backend=claude`, `fagan.model=claude-sonnet-4-5`,
`fagan.attempt=2`, `fagan.input_tokens=1200`, `fagan.output_tokens=350`,
`fagan.cache_read_input_tokens=800`, `fagan.cache_creation_input_tokens=0` and
`fagan.cost_usd_estimate=0.0042`. The two mistakes to avoid are putting `model`,
token counts or cost on the `customer` signal, and letting a request ask for the
`harness` profile. The profile is a deployment setting, not request data, exactly
like the content-capture switch. The prompt text of that call appears under
neither profile.

## Mandatory attributes

Every signal — trace or metric — carries these resource attributes:
`service.name` is `fagan` unless `OTEL_SERVICE_NAME` overrides it;
`service.instance.id` identifies the process emitting; `fagan.namespace` is an
opaque string that is `default` in core. The
namespace comes from deployment configuration through
`OTEL_RESOURCE_ATTRIBUTES` and from nowhere else. It is never taken from request
data, a plan name or a repo path.

The reason is visible in a trace. Deployment sets
`OTEL_RESOURCE_ATTRIBUTES="service.name=fagan,service.instance.id=7f3c1a9e,fagan.namespace=default"`.
Call #1 arrives from a request carrying plan `acme` and repo path
`/srv/repos/acme-web`; the correct resource attributes are `service.name=fagan`,
`service.instance.id=7f3c1a9e` and `fagan.namespace=default`. Call #2 is an
ordinary core request and still resolves to `fagan.namespace=default`. The
mistake a careless implementation makes is copying the request's plan or repo
path into `fagan.namespace`: call #1 then emits `fagan.namespace=acme` (or the
repo path), and because the attribute map is process-global and mutated in
place, call #2 emits `acme` too even though that request carried nothing.
Deriving the namespace from request data is therefore forbidden outright —
getting one call's labels right is not enough if the shared map is left tainted
for every later signal, and per-namespace retention would then delete the wrong
tenant's data.

## Naming

Every Fagan metric, span and attribute name lives under the `fagan.` prefix:
`fagan.lifecycle`, `fagan.dashboard.request`, `fagan.dispatch.outcomes`,
`fagan.story_key`, `fagan.outcome`. The prefix is what makes the harness's own
signals separable from framework and runtime instrumentation sharing the same
exporter. The only exceptions are the OTEL-standard resource keys mandated by
the previous section, `service.name` and `service.instance.id`, which keep their
upstream names.

Renaming one of these is a breaking change. It needs a CHANGELOG entry marked
**BREAKING**, because dashboards, alerts and downstream queries are written
against the published names and no alias is kept.

## Trace propagation

Fagan speaks the W3C trace-context vocabulary, within limits set by who controls
each value.

**Inbound.** The dashboard accepts the W3C `traceparent` header on `/api/`
requests. The header is caller-controlled, so it is read only through
`pipeline.trace_context.parse_traceparent`, which returns `None` for anything
that is not exactly version `00`, a 32-character lowercase hex trace id that is
not all zeros, a 16-character lowercase hex span id that is not all zeros and 2
hex flags. `00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01` is accepted;
an uppercase trace id, an all-zero trace id or a version other than `00` is
rejected. A rejected value is ignored, and the raw header is never logged.
`tracestate` is never propagated: it is unbounded, caller-controlled data, so it
is dropped rather than forwarded.

**Outbound.** A published event carries an optional top-level `traceparent` key,
present only when a span is active or the emitter supplied one. A consumer
validates it with `parse_traceparent` before using it.

**Attempts.** The lifecycle spans form one trace per dispatch attempt. The trace
id is derived from the story's correlation id and the attempt number, so every
event of an attempt lands in one trace and a new attempt opens a new one.
Attempt 1 and later carry a span link to the previous attempt's root span, which
is how an operator walks a rework chain. The agent subprocess receives
`TRACEPARENT` for the attempt it was launched for, so the agent's own spans join
the trace that dispatched it.

**Logs and metrics.** A log line carries `trace_id` and `span_id` when a span is
active, and neither key otherwise. Trace and span ids are never metric labels:
they are unbounded, per-request values, and a label per value buys cardinality
without buying a query.

**Loss.** Telemetry is lossy by contract. The attempt number travels only on the
`agent_dispatched` event, and a process files each event under the last attempt
it saw for that story — attempt 0 when it has seen none. Missing the event never
buffers or drops an event, and the process never advances its notion of the
attempt by inferring one from the order of the events it emits: with correlation
id `c-7f3a`, a process that has seen no dispatch event files the events it emits
during attempts 3, 4 and 5 under attempt 0 — the trace id derived from `c-7f3a`
and `0` — not under 3, 4 or 5.

## Data classification

| Allowed | Never |
| --- | --- |
| Enum-bounded outcome, status and action labels | Prompts |
| Plan name | Briefs |
| Story key | Model output |
| Event type | Agent transcripts |
| Backend, provider and model names | Diffs |
| Counts | File contents |
| Durations | Principals or user identifiers as metric labels |
| Token counts | Query strings |
| Estimated cost | Request headers or bodies |
| | Secrets |

The left column is metadata that is never content; the profile a signal is
emitted under still decides which of these it carries. The right column is
content, and content is never an attribute, a metric label or a span name on
either profile. A value that does not fit an enum belongs on the usage
record or in the audit stream, not in a label: unbounded values are also how
cardinality explodes.

## Usage record

There is exactly one usage record per model call, with exactly these fields:
`ts`, `plan`, `story_key`, `attempt`, `correlation_id`, `role`, `backend`,
`model`, `served_model`, `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`, `cost_usd` and
`duration_ms`. The record carries no content — no prompt, no output, no
principal.

A first record looks like this: `ts=2026-11-05T10:12:33.412Z`, `plan=default`,
`story_key=STORY-42`, `attempt=2`, `correlation_id=0f9c4b7e`, `role=dispatch`,
`backend=claude`, `model=claude-sonnet-4-5`,
`served_model=claude-sonnet-4-5-20250929`, `input_tokens=1200`,
`output_tokens=350`, `cache_creation_input_tokens=0`,
`cache_read_input_tokens=800`, `cost_usd=0.0042`, `duration_ms=2340`. The
follow-up call on the same story is `attempt=3` with `model=claude-sonnet-4-5`,
but the router fell back, so its record must report
`served_model=gpt-5.1-2026-09-01` and `cost_usd=0.0031`. `model` is what was
requested; `served_model` is the model that actually served the call. Dropping
`served_model` because it "usually equals `model`" destroys attribution exactly
when a fallback happens, reusing the previous record's `served_model` for the
follow-up is equally wrong, and appending the prompt or a principal breaks the
no-content rule.

The four token counts — `input_tokens`, `output_tokens`,
`cache_creation_input_tokens` and `cache_read_input_tokens` — are measured and
reliable. `cost_usd` is the figure the provider reports, and under a subscription
that figure is notional rather than billed, so treat it as an estimate. The
record is still the billing-grade source for token volume; the cost column is the
part that is approximate.

## Retention and tenancy

Telemetry and usage records are deleted per namespace, so one tenant's retention
policy never reaches another's data. Content, when a namespace has enabled
capture, has its own retention and is purged on its own schedule rather than with
the metadata. Because deletion is keyed by namespace, a signal carrying the wrong
`fagan.namespace` is a retention defect, not a cosmetic one.

Each deployment serves one organization, as recorded in
`docs/adr/0012-store-contract-v2.md`. A deployment is therefore not a multi-tenant
router: isolation between customers comes from deploying separately, and
`fagan.namespace` distinguishes the environments and namespaces inside the one
organization a deployment serves.
