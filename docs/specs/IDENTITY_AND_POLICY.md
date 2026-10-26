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
principal to `request.state.principal`, then binds it as the request-scoped
actor (§2, `pipeline/actor.py`). **Nothing enforces it.** No service-layer
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

### Actor propagation (`pipeline/actor.py`)

The process that runs a tool is not the caller. This module carries the caller
across the layers that do not take one as an argument.

Three states, in order of authority:

1. **A principal a transport bound.** The dashboard middleware binds the
   principal it authenticated for the request; the remote MCP HTTP transport
   will bind the principal it verified from the token.
2. **`ANONYMOUS`** — `Principal(subject="anonymous", auth_method="none")`, with
   no roles and no scopes. A transport binds this when it went out of its way to
   authenticate nobody (the chat-stream `k-` pass-through). It is deliberately
   *not* the local operator: an unauthenticated remote caller must never be
   recorded as the operator of this machine.
3. **`LOCAL_OPERATOR`** — the fallback when nothing is bound at all. That is a
   process that *is* the local operator: the stdio MCP server, the launchd
   scheduler daemon, a CLI script.

| Symbol | What it is |
| --- | --- |
| `ANONYMOUS` | The principal for a caller whose transport refused to identify them. No roles, no scopes, `auth_method="none"`. |
| `bind_principal(principal)` | Context manager that binds *principal* for the duration of one request. |
| `current_principal()` | The bound principal, or `LOCAL_OPERATOR` when nothing is bound. |
| `actor_id()` | `current_principal().subject`, for stamping onto records. |

The binding is a `ContextVar`, so it is scoped to one request: `bind_principal`
restores the previous value on exit — including when the handler raises — and a
binding never crosses threads. A `threading.Thread` started inside a request
begins with an empty context and sees `LOCAL_OPERATOR`.

Worked examples:

- A remote MCP HTTP request whose token verifies as `alice` → `actor_id()` is
  `"alice"`.
- A remote request whose token is missing or invalid → the transport binds
  `ANONYMOUS`, so `actor_id()` is `"anonymous"`, **not** `"local-operator"`.
- A local stdio or CLI process with nothing bound → `actor_id()` is
  `"local-operator"`.
- Request 1 binds `alice`; inside the handler `current_principal()` is `alice`.
  When the handler returns — or raises — the binding is restored, so the next
  call on that thread or event loop sees `LOCAL_OPERATOR` again, not `alice`.

**The rule: no tool takes an actor.** No tool may take an actor or principal
parameter. Identity comes from the transport that received the request: a
remote MCP HTTP transport binds the principal it verified from the token, and
the recording sites do not change. A parameter named `actor` would be set by
whoever holds the credential, which makes every record it stamps forgeable.
`tests/unit/test_actor.py` enforces the rule by scanning the parameters of every
registered MCP tool and failing on any named `actor`, `principal`, `decided_by`,
`user` or `whoami`.

**What records an actor today.**

| Record | Field | Written by |
| --- | --- | --- |
| Plan manifest | top-level `ingested_by` | `pipeline/ingest.py` |
| Plan-approval decision record | `decided_by` and `role` | `pipeline/plan_approval.py` |
| Decisions log | `actor` | the dashboard route (`app/dashboard.py`) |

- `ingested_by` is `actor_id()` at ingest time.
- The approval record's `decided_by` is the deciding principal's subject, and
  `role` is the most privileged ADR 0009 role that principal held (`admin` >
  `owner` > `approver` > `delegate` > `author`), falling back to `operator` when
  it holds none. A decider holding `["author", "approver"]` is recorded as
  `"approver"` — not `"author"` (first in the list) and not `"author,approver"`.
- The decisions log row written by the dashboard has `actor` = `actor_id()` and
  keeps `decided_by` as the caller-declared channel from the request body. A
  request bound to `alice` whose body declares `decided_by: "ops-channel"`
  produces `actor: "alice"` and `decided_by: "ops-channel"`.

Everything else still records no actor — see §4.

**Request correlation.**

The actor seam answers *who* is asking. A second `ContextVar` with the same
request lifetime answers *which request* it is, so a support report can name
both the inbound request and the log lines that served it.

| Symbol | What it is |
| --- | --- |
| `new_correlation_id()` | Mints a fresh id: 12 lowercase hex characters, the same shape dispatch mints per story. |
| `request_correlation_id(candidate)` | Returns *candidate* when it is well formed, else a freshly minted id. Pure: it never binds. |
| `CORRELATION_ID_RE` | `^[A-Za-z0-9._-]{1,64}$` — the shape an inbound id must have to be accepted. |
| `bind_correlation_id(value)` | Context manager that binds *value* for the duration of one request. |
| `current_correlation_id()` | The bound id, or `""` outside a request. |

The shape check exists because an inbound header is caller-controlled. Echoing
it unchecked into a response header and into logs is log forging: a value
carrying a newline or a space can split a log line or inject a header. So
`request_correlation_id` accepts only 1–64 characters drawn from
`[A-Za-z0-9._-]` and mints otherwise. It matches with `fullmatch`, not `match`,
because Python's `$` also matches just before a single trailing newline —
`"9f3c1a7b2e04\n"` would otherwise pass the guard and be echoed back verbatim.

Worked examples:

- `new_correlation_id()` → `"9f3c1a7b2e04"` (12 lowercase hex).
- `request_correlation_id("9f3c1a7b2e04")` → `"9f3c1a7b2e04"` — preserved.
- `request_correlation_id("9F3C1A7B2E04")` → `"9F3C1A7B2E04"` — preserved: the
  accepted class is case-insensitive, so an uppercase id is echoed as given.
- `request_correlation_id("")` → a freshly minted id.
- `request_correlation_id("9f3c1a7b2e04\n")` → a freshly minted id: the trailing
  newline fails `fullmatch`.
- `request_correlation_id("x y")` → a freshly minted id: a space is outside the
  accepted class.
- `request_correlation_id("a" * 65)` → a freshly minted id: longer than 64.
- The mistake to avoid is accepting any non-empty string and echoing it into
  the header and the logs — that is the log-forging and header-injection hole
  the shape check closes.

`bind_correlation_id` is a `ContextVar` binding with the same lifetime as the
principal: a transport nests the two `with` statements, the binding is restored
on exit including when the handler raises, and outside any request
`current_correlation_id()` is `""` — not `None`. The empty string is what lets a
stdio MCP call record an empty id rather than a fabricated one.

Worked examples:

- `current_correlation_id()` before any bind → `""`.
- Inside `with bind_correlation_id("9f3c1a7b2e04")` → `"9f3c1a7b2e04"`; after the
  block exits → `""` again.
- Nested: bind `"aaaaaaaaaaaa"`, then an inner bind of `"bbbbbbbbbbbb"`; the
  inner exit restores `"aaaaaaaaaaaa"`, the outer exit restores `""`, and a
  follow-up call after both exits is `""`.
- The mistakes to avoid: storing the id in a module global without restoring it
  in a `finally`, so the next request on that thread sees the previous request's
  id (a cross-request leak); or restoring to `None` so `current_correlation_id()`
  returns `None` instead of `""` and the stdio path records a fabricated id.

**Division of labour with dispatch.** The two ids are different lifetimes and
are never conflated. A *story's* correlation id (`pipeline/dispatch.py`)
identifies the agent run: dispatch mints it once, persists it on the manifest,
and exports it to the agent subprocess as `PIPELINE_CORRELATION_ID`. A
*request's* id identifies the inbound HTTP request and lives only for that
request. The environment variable is never written from a request handler,
because it is process-global: with two concurrent requests A and B, a handler
that did `os.environ["PIPELINE_CORRELATION_ID"] = <request id>` would clobber
A's subprocess environment with B's id. The request id travels in the
`ContextVar` and the response header, never in the environment.

**Where it lands.** The dashboard middleware mints or preserves one id per
inbound request and returns it in the `X-Correlation-ID` response header on
every response, including the 401 and 403 refusals. The same id is stamped as
`correlation_id` on the dashboard decisions record and on the plan-approval
record. It also reaches application logs: when the opt-in
`PIPELINE_LOG_FORMAT=json` formatter is enabled, every JSON line carries
`correlation_id`, read from this same bound context — so a line inside request
A (bound to `corr-abc`) shows `"correlation_id":"corr-abc"`, and the next line
after that request ends shows `"correlation_id":""` rather than a stale id.
With the formatter off, logs are unchanged.

### The one consumer: `app/dashboard.py`

The `/api/` middleware (`_enforce_api_key`) already gates every API request on
the shared secret. After the gate passes it resolves the presented key through
a module-level `SharedSecretProvider(get_or_create_api_key)` and attaches the
result to `request.state.principal`. Two cases deliberately attach `None`
rather than a principal:

- the chat-stream pass-through, where the caller-supplied `k-` value is
  validated upstream by the pipeline, so the dashboard has no identity for it;
- any credential the provider rejects.

That is the whole of the wiring. The middleware then binds the principal — or
`ANONYMOUS` when it attached `None` — as the request-scoped actor, so the
recording sites in §2 read it through `actor_id()`. **Nothing enforces it.** No
route, no service method, no tool consults it or `is_allowed()` to allow or deny
anything.

## 3. The invariant that keeps local mode unchanged

With **no identity provider configured** and **nothing calling `is_allowed`**,
the pipeline behaves exactly as it did before this plan:

- `SharedSecretProvider` is only constructed inside `app/dashboard.py`, and it
  authenticates the same shared secret the middleware already required — it
  adds no new gate and rejects no request the old check accepted.
- `LocalPolicy` is only reachable through `default_authorizer()`, which no
  production call site invokes.
- No event, journal, notification or API response shape changes. The manifest
  gains one additive field, `ingested_by` (§2), which no existing reader
  requires.
- `pipeline/plan_approval.py` records the deciding principal's subject as
  `decided_by` and its most privileged role as `role`; with nothing bound that
  principal is `LOCAL_OPERATOR`, so local mode records the same actor it always
  did.

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
| **Actor on the remaining mutations** | Ingest (`ingested_by`), plan approval (`decided_by`/`role`) and dashboard decisions (`actor`) record an actor; nothing else does. Dispatch, story status changes, pause/resume and decision answers record no actor at all. `Principal.roles` is recorded but never checked — enforcement is still outstanding. |

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
