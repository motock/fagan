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

See [`template.md`](template.md) when adding one.