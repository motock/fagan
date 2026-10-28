# Architecture Decision Records

Short records of the architectural decisions behind this project: what was
decided, why, and what it costs. Format follows Michael Nygard's ADR pattern -
Context, Decision, Consequences.

An ADR is written when a decision is **non-obvious, load-bearing, and expensive
to reverse**. Routine choices do not need one. A decision that surprised a
contributor, or that someone later tried to "fix" without knowing the history,
is exactly what belongs here.

ADRs are immutable once merged. If a decision changes, add a new ADR that
supersedes the old one and mark the old one Superseded - do not edit history.

| # | Title | Status |
|---|---|---|
| [0001](0001-review-is-the-primary-quality-gate.md) | Review is the primary quality gate, not tests | Accepted |
| [0002](0002-shipped-model-registry-is-provider-neutral.md) | The shipped model registry is provider-neutral | Accepted |
| [0003](0003-dispatch-backend-resolves-from-environment.md) | Dispatch backend resolves from the environment, not the registry | Accepted |
| [0004](0004-safety-gates-default-on-and-fail-closed.md) | Safety gates default on and fail closed | Accepted |
| [0005](0005-external-context-sources.md) | External context sources are reference data, pinned by digest | Accepted |
| [0006](0006-decision-routing-on-a-shared-server.md) | Decisions on a shared server route to the plan owner | Accepted |
| [0007](0007-plan-approval-at-activation.md) | Plans are approved before activation, not before ingestion | Accepted |
| [0008](0008-shared-control-plane-per-repo-workers.md) | Enterprise topology is a shared control plane with per-repo workers | Proposed |
| [0009](0009-tenancy-and-authorization-model.md) | Tenancy and authorization are a service-layer policy over a verified principal | Proposed |
| [0010](0010-event-delivery-contract-and-nats-jetstream.md) | Events have an at-least-once delivery contract; NATS JetStream is the default broker | Proposed |

See [`template.md`](template.md) when adding one.