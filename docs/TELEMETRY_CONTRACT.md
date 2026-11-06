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
`service.name` is always `fagan`; `service.instance.id` identifies the process
emitting; `fagan.namespace` is an opaque string that is `default` in core. The
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
exporter.

Renaming one of these is a breaking change. It needs a CHANGELOG entry marked
**BREAKING**, because dashboards, alerts and downstream queries are written
against the published names and no alias is kept.
