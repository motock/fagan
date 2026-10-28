# 0009. Tenancy and authorization are a service-layer policy over a verified principal

- **Status:** Accepted direction (2026-10-26). The identity/authorization seam,
  service-layer enforcement for every action in the `Action` enum, OIDC token
  verification, the OIDC-gated remote MCP transport and the append-only,
  hash-chained audit stream are implemented; plan ownership is stored and
  enforced for `plan:approve`, `plan:reject` and `story:merge` only. Tenant
  scoping beyond the plan `team`, tenant-scoped reads, and
  authorization of the MCP tools outside the enum are still unimplemented.
- **Date:** 2026-10-26

**Implementation status.** `pipeline/authorization.py` enforces the policy in
the service for `story:merge`, `story:mark_done`, `plan:approve`,
`plan:reject` and `plan:pause` (both pause and resume), deny-by-default, with
`PIPELINE_POLICY_ENFORCE` as the kill switch. `app/oidc.py` verifies bearer
tokens against the issuer's JWKS — issuer, audience, expiry and the claims-to-
`Principal` mapping — when `PIPELINE_IDENTITY_PROVIDER=oidc`; it authenticates
API clients only, since the bundled dashboard has no login flow. The remote MCP
transport (`pipeline/remote_transport.py`) serves the same tools over HTTP
behind the same `OidcProvider`, refusing to start without it. Every action in
the `Action` enum is enforced in the service; what remains unimplemented is
the full tenant chain, ownership records beyond the plan `team`, and the
mutating MCP tools outside the enum. See the
"Enforcement (pipeline/authorization.py)" and "OIDC provider" sections of
[`docs/specs/IDENTITY_AND_POLICY.md`](../specs/IDENTITY_AND_POLICY.md).

## Context

Three earlier records establish the enterprise direction but leave the
authorization model undefined:

- **ADR 0006** routes decisions to a plan's owner and requires ownership to be
  enforced in the mutating tool surface, not only in routing. It makes
  authentication of the MCP caller a hard dependency.
- **ADR 0007** defines the plan-approval roles — `author`, `owner`,
  `approver`, `delegate` — and permits self-approval when the lead is also the
  author.
- **ADR 0008** chooses a shared control plane with per-repo workers and names
  row-level authorization as the product: `principal → team → repo → plan →
  story`.

What none of them says is how a principal is represented, what a role is
allowed to do, how scope is checked, or where enforcement lives. Building
identity code before answering those questions would produce exactly the
transport-first mistake ADR 0006 warns about.

The current code had no user-identity layer when this record was written; the
paragraph below is historical context for the decision, not a description of
today's code (see **Implementation status** above). `app/auth.py` was one shared
secret plus a `Host` allowlist and `X-Pipeline-Origin`; the module states that
the origin header is **not** an authentication decision. The MCP path was stdio
and unauthenticated. There was no `Principal`, `Role`, `Permission`, `Scope`,
OIDC, or JWT type anywhere. `app/role_registry.py` is about **model** roles,
not user roles; the two must not be conflated.

## Decision

**Authentication is an adapter concern; authorization is a service-layer
policy; both operate on a verified `Principal`.**

1. **Verified principal.** An `IdentityProvider` Protocol authenticates a
   request and returns a `Principal` carrying at least: subject, email,
   groups/roles, tenant and repo scopes, auth method, and the request/
   correlation ID. `SharedSecretProvider` is the local single-operator
   implementation (today's behavior). `OidcProvider` verifies a JWT against
   JWKS with issuer, audience, and expiry checks for the shared server. Remote
   MCP verifies the same token at the HTTP transport layer and yields the same
   `Principal`. A caller-supplied header — including `X-Pipeline-Origin` — is
   never identity.

2. **Policy in the service.** Every mutating `PipelineService` method takes
   the principal and calls `allow(principal, action, resource) -> bool`,
   which denies by default. A missing or malformed principal, an unknown
   action, or an exception inside the policy lands denied. Adapters
   authenticate and pass the principal; they do not make authorization
   decisions. This is ADR 0006's rule applied uniformly, so the dashboard,
   chat, and MCP adapters cannot bypass it.

3. **Roles.** `author` (drafts/ingests), `owner` (accountable; the routing
   target for `request_decision` per ADR 0006), `approver` (may activate;
   scoped to a team/repo), `delegate` (may answer decisions), and `admin`
   (explicit break-glass, audited). The owner is the author (ADR 0007).
   Self-approval is permitted and recorded; the approval gate remains a
   visibility/process control, not separation of duties.

4. **Scope model.** A principal acts only within its scopes:
   `principal → teams → repos → plans → stories`. Cross-repo access requires
   an explicit scope; there is no implicit global access. Plan names and story
   keys are namespaced per owner/tenant (ADR 0008), so a plan's identity is
   never a bare, globally claimable string.

5. **Actions.** Policy actions map one-to-one to the mutating surface:
   `plan:ingest`, `plan:approve`, `plan:reject`, `plan:dispatch`,
   `plan:pause`, `story:patch`, `story:interrupt`, `decision:answer`,
   `story:merge`, `story:mark_done`, and `config:write`. Read-only tools are
   not gated by this policy (they still respect repo scope for visibility).

6. **System principals.** `system:scheduler` (advance/dispatch/merge as the
   policy permits) and `system:worker` (execute only the assigned story) are
   distinct, narrowly scoped principals. They are audited like a human and
   cannot approve a plan. A scheduler that could approve its own work would
   defeat ADR 0007.

7. **Local mode is unchanged.** A `LocalOperator` principal with the full
   action set is derived from the shared-secret provider. With no IDP
   configured, the pipeline behaves exactly as today.

8. **Audit.** Every allow and deny records `{actor, action, target, outcome,
   correlation_id, at}`. Mutations record the same, plus their domain fields;
   plan approvals additionally record the content digest and `self_approved`
   (ADR 0007). The audit stream is append-only and hash-chained
   (`pipeline/audit.py`, one file per plan). Off-box shipment of the head hash —
   what a shared server needs before a truncated tail becomes detectable — is not
   implemented; the readiness findings record it as a separate gap.

## Consequences

- **Authentication is a hard prerequisite.** This record authorized no code when
  accepted; it defines the model that the identity implementation must satisfy.
  The `Principal`, `IdentityProvider`, and `Authorizer` seams were introduced
  behind behavior-preserving defaults in local mode, and service-layer
  enforcement for the actions in the implementation status note has since
  landed.
- **Negative tests are mandatory**, not optional: an unauthenticated caller,
  a non-owner patching a story, an out-of-scope repo, an unknown action, a
  system principal attempting approval, and a malformed principal must each
  land denied. A green happy-path suite proves nothing about a deny-by-default
  policy.
- **The control-plane store becomes sensitive.** It holds every repository's
  plans and briefs; a repo-scoping bug is a cross-tenant leak. Per-repo
  processes contain that by construction; the shared plane must earn the
  equivalent with row-level checks.
- **OIDC is an adapter, not the policy.** Group claims can seed roles, but
  plan/story scope and action rights are resolved server-side; a token cannot
  carry the whole authorization decision.
- **Model roles and user roles stay separate.** `app/role_registry.py` keeps
  its meaning; the user-facing modules should be named `app/identity.py` and
  `app/policy.py` (or `pipeline/policy.py`) to avoid the collision.
- Not decided here: the concrete OIDC claim-to-role mapping, the break-glass
  approval workflow, and the retention/tamper-evidence of the audit stream.

### Alternatives considered

- **Gateway-only authorization.** Authenticate at the MCP/HTTP edge and trust
  the call thereafter. Rejected: the dashboard and chat adapters, and the
  scheduler, would each need a private copy of the policy, and one missed
  check is an open door.
- **One role per repo.** Rejected: it multiplies with repository count and
  cannot express "this lead approves for team A's repos."
- **Team-owned plans.** Rejected in ADR 0006: no single accountable person
  when the team does not respond.
- **Implicit ownership (whoever started it).** Rejected: no accountability
  and no way to reassign; ADR 0006 chose one named owner plus delegates.
- **Claims-as-policy.** Rejected: a token can carry group membership but not
  the plan/story scope and action rights; those must be resolved against
  current server state.

Related: [0006](0006-decision-routing-on-a-shared-server.md),
[0007](0007-plan-approval-at-activation.md),
[0008](0008-shared-control-plane-per-repo-workers.md).
