# Identity and Policy Seams

- **Status:** Seam only — types, a local provider and a deny-by-default policy
  exist; **nothing enforces them yet**. Written 2026-10-26.
- **Stories:** TEN-ID-01 (`Principal`/roles/scopes), TEN-ID-02
  (`SharedSecretProvider` and `LOCAL_OPERATOR`), TEN-POL-01 (`LocalPolicy`),
  TEN-WIRE-01 (dashboard attaches the principal), TEN-DOC-01 (this spec).
- **Companion records:** [ADR 0009](../adr/0009-tenancy-and-authorization-model.md)
  (the tenancy and authorization model) and
  [ADR 0008](../adr/0008-shared-control-plane-per-repo-workers.md) (staging
  step 1, the behavior-preserving prep).
- **Audience:** Contributors touching `app/identity.py`, `app/policy.py`,
  `app/dashboard.py`, or the `PipelineService` mutation surface.

## 0. Summary

This plan lands the **shape** of authentication and authorization, not the
control. `app/identity.py` defines a `Principal`, the ADR 0009 roles, an
`IdentityProvider` Protocol and the local `SharedSecretProvider`.
`app/policy.py` defines the `Action` enum, the `Resource` scope chain, an
`Authorizer` Protocol, the deny-by-default `LocalPolicy`, `default_authorizer()`
and the fail-closed `is_allowed()` wrapper.

The dashboard authenticates each `/api/` request and attaches the resulting
principal to `request.state.principal`. **Nothing reads it.** No service-layer
method consults `is_allowed`, so a denied principal changes nothing today. The
seam is inert by construction: with no identity provider configured and no call
site invoking the policy, the pipeline behaves exactly as it did before this
plan.

Read this document as a map of the wiring points, not as evidence of a
security boundary. §4 lists, in one table, everything the seam does **not** do.

## 1. What the seam is

ADR 0009 splits the problem in two:

- **Authentication is an adapter concern.** An `IdentityProvider` turns a
  credential into a verified `Principal`. Adapters (the dashboard today; an
  OIDC verifier and the remote MCP transport later) own this step.
- **Authorization is a service-layer policy.** Every mutating
  `PipelineService` method will take the principal and ask
  `allow(principal, action, resource) -> bool`, which denies by default. The
  policy is pure and stateless; the service layer is the only place it is
  meant to be consulted.

Both halves operate on the same `Principal`, so the adapters cannot make
authorization decisions and the service layer cannot authenticate. This plan
builds the two halves and the one adapter that already exists (the dashboard);
it does not connect them.

## 2. What exists

### `app/identity.py`

Deliberately free-standing: it imports nothing from `app.*` or `pipeline.*`, so
both the dashboard and the service layer can depend on it without an import
cycle.

| Symbol | What it is |
| --- | --- |
| `Principal` | Frozen dataclass: `subject`, `roles`, `scopes`, `email`, `auth_method`, `correlation_id`. `__post_init__` rejects an empty subject, a role outside `ROLES`, and an empty scope with `IdentityError`. |
| `IdentityError` | Raised for every rejected principal or credential. Messages name the failure kind (`subject`/`role`/`scope`) and never echo the credential or scope value. |
| `ROLE_AUTHOR`, `ROLE_OWNER`, `ROLE_APPROVER`, `ROLE_DELEGATE`, `ROLE_ADMIN` | The five ADR 0009 roles. |
| `ROLES` | `frozenset` of the five roles; any other role is rejected. |
| `IdentityProvider` | `runtime_checkable` Protocol with `authenticate(credential) -> Principal`. Implementations raise `IdentityError` for every rejection and never leak the credential or the expected secret. |
| `LOCAL_OPERATOR` | Today's single-operator identity: subject `local-operator`, all roles, scope `*`, `auth_method="shared-secret"`. ADR 0009 section 7 defines local mode as this principal holding the full action set. |
| `SharedSecretProvider` | The local `IdentityProvider`. Compares the presented credential against a fixed string or a callable (the dashboard passes `app.auth.get_or_create_api_key`, so a lazily created or rotated key is read per call, never cached). Comparison is constant-time over UTF-8 bytes; a missing or non-matching credential raises `IdentityError`. |

### `app/policy.py`

Imports `app.identity` only — never `app.auth` (an adapter concern) and never
`pipeline.*` (the service layer that will call it).

| Symbol | What it is |
| --- | --- |
| `Action` | `str` enum of the eleven mutating actions ADR 0009 authorizes: `plan:ingest`, `plan:approve`, `plan:reject`, `plan:dispatch`, `plan:pause`, `story:patch`, `story:interrupt`, `decision:answer`, `story:merge`, `story:mark_done`, `config:write`. |
| `Resource` | Frozen dataclass for the ADR 0009 scope chain: `tenant`, `team`, `repo`, `plan`, `story`. |
| `Authorizer` | Protocol for `allow(principal, action, resource) -> bool`. |
| `LocalPolicy` | The local single-operator policy: the `local-operator` subject may do everything; every other principal — including any future system principal — is denied every action. A `None` principal, an unknown action, or a non-`Action`/non-string action is denied without raising. |
| `default_authorizer()` | Returns the authorizer for this deployment — `LocalPolicy()` today. |
| `is_allowed(authorizer, principal, action, resource)` | The wrapper every future call site uses: asks the authorizer and **fails closed** on any error, logging a warning that names the action and the principal subject. A policy that raises lands denied, never propagates. |

### The one consumer: `app/dashboard.py`

The `/api/` middleware (`_enforce_api_key`) already gates every API request on
the shared secret. After the gate passes it resolves the presented key through
a module-level `SharedSecretProvider(get_or_create_api_key)` and attaches the
result to `request.state.principal`. Two cases deliberately attach `None`
rather than a principal:

- the chat-stream pass-through, where the caller-supplied `k-` value is
  validated upstream by the pipeline, so the dashboard has no identity for it;
- any credential the provider rejects.

That is the whole of the wiring. **Nothing reads `request.state.principal`.**
No route, no service method, no tool consults it or `is_allowed()`.

## 3. The invariant that keeps local mode unchanged

With **no identity provider configured** and **nothing calling `is_allowed`**,
the pipeline behaves exactly as it did before this plan:

- `SharedSecretProvider` is only constructed inside `app/dashboard.py`, and it
  authenticates the same shared secret the middleware already required — it
  adds no new gate and rejects no request the old check accepted.
- `LocalPolicy` is only reachable through `default_authorizer()`, which no
  production call site invokes.
- No manifest, event, journal, notification or API response shape changes.
- `pipeline/plan_approval.py` still records the constant actor
  `local-operator` (`_DECISION_ACTOR`) on every approval decision; the seam does
  not touch it.

This is ADR 0009 section 7 ("local mode is unchanged") and the first part of
ADR 0008 staging step 1 ("verified caller identity at the gateway, RBAC and
ownership records, authorization enforced in `PipelineService`"). This plan
lands the identity-at-the-gateway piece only; the RBAC/ownership records and
the `PipelineService` enforcement in that same step are the follow-up in §6.
The seam is added without changing behavior so it can be adopted
incrementally.

## 4. What it does NOT do

Do not mistake any row of this table for a control that exists.

| Not implemented | Consequence today |
| --- | --- |
| **Enforcement in the service layer** | No `PipelineService` method takes a principal or calls `is_allowed()`. A principal denied by `LocalPolicy` changes nothing: the mutation still runs. |
| **Plan owner / author / delegate records** | Plans carry no ownership. `ROLE_OWNER`, `ROLE_AUTHOR` and `ROLE_DELEGATE` are defined but nothing assigns or reads them, and `request_decision` has no owner to route to. |
| **System principals** | `system:scheduler` and `system:worker` do not exist. The scheduler and worker act with no principal at all, and `LocalPolicy` would deny them if they did. |
| **OIDC provider** | There is no `OidcProvider`. `SharedSecretProvider` is the only `IdentityProvider`, so there is no JWT/JWKS verification, issuer/audience/expiry check, or group-to-role mapping. |
| **Remote MCP transport** | MCP is stdio only. There is no HTTP transport layer to verify a token and yield a `Principal`. |
| **Audit stream** | No allow/deny record is emitted. ADR 0009 section 8's `{actor, action, target, outcome, correlation_id, at}` stream does not exist. |
| **Identity on approvals** | `pipeline/plan_approval.py` still stamps the constant `_DECISION_ACTOR = "local-operator"` as `decided_by` and `role`. An approval records no real principal. |

## 5. The `app/auth.py` import rule

`app/auth.py` must contain **no** `app.*` or `pipeline.*` imports. It is the
low-level module `app.chat` imports from, and an internal import there would
close an import cycle. The rule is pinned by
`tests/unit/test_chat_origin_header.py::test_auth_module_has_no_app_internal_imports`,
which fails on any `from app…` / `import app…` line in that file.

This is why the dashboard — not `app/auth.py` — imports the provider: the
`SharedSecretProvider` is constructed in `app/dashboard.py` and handed
`app.auth.get_or_create_api_key` as a callable. `app/auth.py` stays a leaf;
`app/identity.py` and `app/policy.py` stay free of `app.auth` and
`pipeline.*` imports for the same reason.

## 6. Follow-up

The seam is prep, not the control. The remaining work, in dependency order:

1. **Enforcement in `PipelineService`** (ADR 0009 section 2). Every mutating
   method takes the principal and calls `is_allowed()`, so a denied principal
   actually blocks the mutation. This is the step that turns the seam into a
   control; until it lands, §4's first row stands.
2. **Ownership records.** Persist plan owner/author/delegate so the roles in
   §2 mean something and `request_decision` has a routing target (ADR 0006).
3. **System principals.** Add `system:scheduler` and `system:worker` as
   distinct, narrowly scoped principals that are audited like a human and
   cannot approve a plan (ADR 0009 section 6).
4. **The audit stream.** Emit the allow/deny record from ADR 0009 section 8 for
   every decision the policy makes.
5. **The OIDC adapter.** Add `OidcProvider` (JWT against JWKS, issuer,
   audience, expiry) and the remote MCP transport that verifies the same token
   and yields the same `Principal`, for the shared server.

Steps 1–4 are the multi-tenant implementation ADR 0008 calls "not prep"; step 5
is the provider swap the seam was built to accept.
