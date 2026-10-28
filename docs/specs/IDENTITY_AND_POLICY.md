# Identity and Policy Seams

- **Status:** Seam plus partial enforcement — types, two providers (the shared
  secret and an OIDC bearer-token verifier) and a deny-by-default policy exist,
  and the service layer enforces them for every action in the `Action` enum
  (§3, `pipeline/authorization.py`). MCP tools outside those service paths are
  still unchecked (§4).
  Written 2026-10-26.
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
actor (§2, `pipeline/actor.py`). The service layer enforces the policy for the
actions in the `Action` enum — `plan:ingest`, `plan:approve`, `plan:reject`, `plan:dispatch`, `plan:pause`, `story:patch`, `story:interrupt`, `story:merge`, `story:mark_done`, `decision:answer` and `config:write` — through `pipeline/authorization.py`
(§2, "Enforcement"). Enforcement is **on by default** and
`PIPELINE_POLICY_ENFORCE` is the operator's kill switch.

It is still a partial boundary. The enum is not every mutation: MCP tools
whose service path never asks `authorize` — `save_plan`, `set_story_status`,
`record_security_audit` and others (§4) — run for any principal. With no
identity provider configured the bound principal is `LOCAL_OPERATOR`, which
`LocalPolicy` allows, so local mode behaves as it did before this plan.

Read this document as a map of the wiring points, not as evidence of a
complete security boundary. §4 lists, in one table, everything the seam does
**not** do.

## 1. What the seam is

ADR 0009 splits the problem in two:

- **Authentication is an adapter concern.** An `IdentityProvider` turns a
  credential into a verified `Principal`. Adapters (the dashboard with either
  the shared secret or `PIPELINE_IDENTITY_PROVIDER=oidc`, and the remote MCP
  transport) own this step.
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

### OIDC provider (`app/oidc.py`)

The second `IdentityProvider`. Selected with
`PIPELINE_IDENTITY_PROVIDER=oidc`; the default `shared-secret` is unchanged and
any other value raises at startup rather than downgrading authentication
(ADR 0004). Like `app/identity.py`, the module imports the identity types and
nothing else from `app.*`, so the service layer can depend on it without a
cycle. The variables it reads, and their defaults, are tabled in
[REFERENCE.md](../../REFERENCE.md#oidc-identity-verification).

`OidcProvider.authenticate(credential)` verifies a bearer JWT against the
issuer's JWKS. Every failure raises `IdentityError` with a fixed reason token —
never the token, the key material or a claims dump — and the dashboard turns it
into `401 {"detail": "Unauthorized"}`.

**Verification rules.** Each one is load-bearing, not a style choice:

| Rule | Why |
| --- | --- |
| The accepted algorithms are a fixed allowlist, `RS256` and `ES256`, checked against the header *before a key is selected*. A JWK's advertised `alg`/`kty` must also match. | The token never chooses its own algorithm. That is what makes `alg: none` and the HS256-for-RS256 confusion attack impossible here, and why a key published for ES256 cannot verify an RS256 token under the same `kid`. |
| `exp`, `iss`, `aud` and `sub` are all **required**. | A token that omits an expiry is rejected rather than treated as eternal. |
| `iss` and `aud` are compared against `PIPELINE_OIDC_ISSUER` / `PIPELINE_OIDC_AUDIENCE`, never against values taken from the token. | A token cannot nominate its own provenance. |
| One bounded clock skew (`PIPELINE_OIDC_CLOCK_SKEW_S`, default `60`, capped at `300`) covers `exp` and `nbf`. | Absorbs clock drift without un-expiring tokens; a large skew would make a stale credential valid again, so the value is bounded rather than merely validated. |
| Keys are selected by the header `kid`; a `kid` absent from the JWKS triggers at most one refetch per 10 seconds before the token is rejected. | Keeps signing-key rotation from becoming a 300-second outage, without letting a stream of bogus `kid`s turn into a stream of outbound requests. |
| The JWKS document is cached for 300 seconds, capped at 256 KiB, and fetched only over `https://` from a host that resolves to a public address. | A cached, size-bounded, address-checked fetch keeps a misconfigured URL from turning the shared server into an SSRF probe against loopback or a cloud metadata endpoint. `PIPELINE_OIDC_ALLOW_PRIVATE_JWKS` is the opt-in for a self-hosted IdP inside a VPC. |

**Claims → `Principal`.** The mapping is the security-relevant part:

| Claim | Becomes | Rule |
| --- | --- | --- |
| `sub` | `Principal.subject` = `<issuer>#<sub>` | The subject is **issuer-qualified**. A `sub` is unique per issuer and never globally (RFC 9068 §2.2); unqualified, another issuer's user could collide with this deployment's `local-operator` and be treated as the operator (CWE-290). `#` is refused in both halves — rejected in the token's `sub` and rejected in `PIPELINE_OIDC_ISSUER` — so the separator cannot be forged. |
| `groups`, or `roles` when `groups` is absent | `Principal.roles` | Intersected with the ADR 0009 role set (`ROLES`). A group the role set does not name is dropped: no role is invented from an unrecognised group. |
| `scope` (space-separated), or `scp` (list) when `scope` is absent | `Principal.scopes` | Empty and `*` parts are dropped. A token that says `*` means "no scope", not "every scope": the token cannot carry the whole authorization decision, so wildcards stay server-resolved. |
| `email` | `Principal.email` | Carried **only** when the issuer also sets `email_verified: true`. `email` is user-editable on several issuers, so an unverified address would invite spoofing. |

`auth_method` is `"oidc"` for every principal this provider returns.

**Boundary: OIDC authenticates API clients only.** The bundled browser
dashboard has no login flow and mints no token. Under
`PIPELINE_IDENTITY_PROVIDER=oidc` its pages still load, but every `/api/` call
returns `401` until the caller sends `Authorization: Bearer <token>`. OIDC mode
is for a token-bearing client — including the remote MCP transport, once it
exists — and it is **not** a drop-in replacement for the shared secret for a
browser-only deployment. The chat-stream `k-` pass-through that exists in
shared-secret mode does not exist under OIDC: every `/api/` request,
`/api/chat/stream` included, requires a bearer token.

### `app/policy.py`

Imports `app.identity` only — never `app.auth` (an adapter concern) and never
`pipeline.*` (the service layer that will call it).

| Symbol | What it is |
| --- | --- |
| `Action` | `str` enum of the eleven mutating actions ADR 0009 authorizes: `plan:ingest`, `plan:approve`, `plan:reject`, `plan:dispatch`, `plan:pause`, `story:patch`, `story:interrupt`, `decision:answer`, `story:merge`, `story:mark_done`, `config:write`. |
| `Resource` | Frozen dataclass for the ADR 0009 scope chain: `tenant`, `team`, `repo`, `plan`, `story`, plus the plan's ownership — `owner` (a subject, `""` when the plan has none) and `delegates` (a tuple of subjects, empty by default). Ownership is empty for every plan written before OWN-01. |
| `Authorizer` | Protocol for `allow(principal, action, resource) -> bool`. |
| `LocalPolicy` | The local single-operator policy: the `local-operator` subject may do everything. A plan's ownership is an **additional** authority for exactly the three actions its owner governs — `plan:approve`, `plan:reject`, `story:merge` — where `resource.owner` and any subject in `resource.delegates` are allowed those three actions even with no role at all. Ownership is never a subtraction: every principal the role rules already allow stays allowed, a plan with no owner (`owner == ""`) and no delegates falls through to the role rule unchanged, and the ownership grant is scoped to those three actions only. Every other principal — including any future system principal — is denied every other action. A `None` principal, an unknown action, or a non-`Action`/non-string action is denied without raising. |
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

The `/api/` middleware (`_enforce_api_key`) gates every API request with the
identity provider `build_identity_provider(get_or_create_api_key)` selects from
`PIPELINE_IDENTITY_PROVIDER` (§2, "OIDC provider"). With the default
`shared-secret` it resolves the presented key through a `SharedSecretProvider`
and attaches the result to `request.state.principal`; with `oidc` it reads
`Authorization: Bearer <token>`, verifies it and attaches the token's
principal, answering `401 {"detail": "Unauthorized"}` on any rejection. Two
cases attach no principal:

- in shared-secret mode, the chat-stream pass-through, where the
  caller-supplied `k-` value is validated upstream by the pipeline, so the
  dashboard has no identity for it (under `oidc` this pass-through does not
  exist — the request needs a bearer token like any other);
- any credential the provider rejects.

That is the whole of the wiring. The middleware then binds the principal — or
`ANONYMOUS` when it attached `None` — as the request-scoped actor, so the
recording sites in §2 read it through `actor_id()`. Whether that principal may
perform an action is a separate question, answered in the service layer by the
section below.

### Remote MCP transport (`pipeline/remote_transport.py`)

The second consumer of the OIDC provider, and the only one that is not the
dashboard. The MCP server speaks `stdio` by default; naming
`PIPELINE_MCP_TRANSPORT=streamable-http` (or `sse`) hands the same `mcp` object
to `pipeline.remote_transport.serve`, which mounts it behind
`BearerAuthMiddleware`. The configuration variables are tabled in
[REFERENCE.md](../../REFERENCE.md#remote-mcp-transport).

**Same token, same principal.** The gate verifies `Authorization: Bearer
<token>` with `build_identity_provider(...)` under the same `PIPELINE_OIDC_*`
configuration the dashboard reads, so a token that `/api/` accepts is a token
this endpoint accepts, and the `Principal` it yields is identical — same
issuer-qualified `subject`, same `roles`, same `scopes`. There is no second,
MCP-specific notion of identity to keep in sync.

**The principal is bound to the request.** On success the gate enters
`bind_principal(principal)` and `bind_correlation_id(correlation)` around the
wrapped app (§2, "Actor propagation"), so a tool invocation records
`actor_id()` as the caller rather than `ANONYMOUS`/`LOCAL_OPERATOR`, and tool-side
audit records attribute to the authenticated subject. This is the same
binding the dashboard's `/api/` middleware performs; the transport adds no new
actor mechanism.

**No OIDC, no transport.** A network transport refuses to start unless
`PIPELINE_IDENTITY_PROVIDER=oidc`, and `BearerAuthMiddleware` additionally
raises `TypeError` when constructed with anything that is not an `OidcProvider`.
Both guards run before a socket is opened, so a misconfiguration stops the
process instead of exposing an unauthenticated control plane over the network.

**The honest gap: authentication attempts are logged, not audited.** Every
attempt — success or failure — is written to the application log by the gate
with:

| Field | Content |
| --- | --- |
| `outcome` | `authenticated`, `denied`, or `denied_scope`. |
| `subject` | The principal subject, on success only. A failed attempt records none. |
| `correlation_id` | The request's correlation id (from `X-Correlation-ID`, or generated). |
| `client_ip` | The peer address from the ASGI scope. |
| `path` | The request path, truncated to 256 characters because it is attacker-controlled. |

The credential and the provider's exception text never appear (an exception can
quote the token); a rejection records a fixed reason instead. A worked example —
a successful request for subject `alice@example.com`, correlation `c-123`,
client `203.0.113.7`, path `/mcp` logs `outcome=authenticated`,
`subject=alice@example.com`; a request with no token and correlation `c-124`
logs `outcome=denied` with no `subject`.

What these records are **not** is part of the hash-chained audit stream (ADR
0009 §8, §2 "Audit stream"). That stream is per plan, and an authentication
attempt has no plan: nothing about it belongs to a `{actor, action, target}`
record. So an authentication attempt lives only in the application log, which
carries no hash chain and no external anchor — it is not tamper-evident, and it
is not made so retroactively. A later authenticated request *for a plan* does
append plan-bound records for whatever that request does, but those records
cover the action, not the earlier authentication attempts, which stay in the
log and outside the chain.

### Enforcement (pipeline/authorization.py)

`pipeline/authorization.py` is the only place the service asks the policy. It
is deliberately separate from `app/policy.py`, which stays pure: the
authorization module owns the environment switch, the plan manifest lookup, the
context principal and the refusal shape.

**`enforcement_enabled(environ=None) -> bool`** reads
`PIPELINE_POLICY_ENFORCE` (`ENFORCE_ENV`) from `os.environ`, or from the dict
passed in, and returns `False` only when the stripped, lowercased value is one
of `0`, `false`, `no`, `off`, `disabled`. Unset, empty, unrecognized and
misspelled values all keep enforcement **on** — the default is the restrictive
one, so a typo cannot silently disable a security control. Worked examples
against the implementation:

| Value | `enforcement_enabled` |
| --- | --- |
| unset | `True` |
| `""` | `True` |
| `"0"` / `"False"` / `" OFF "` / `"disabled"` | `False` |
| `"offf"` / `"noo"` / `"true"` | `True` |

**`authorize(action, *, plan_name, story_key=None, manifest=None, record_allowed=True) -> dict | None`**
is the single entry point the service calls. It returns `None` when the action
is allowed and a refusal dict `{"ok": False, "forbidden": True, "action": ...,
"error": ...}` when it is denied. `ok` and `error` are the shape every mutating
service operation already returns, so a call site that only checks `ok` needs no
change; `forbidden` and `action` let an HTTP adapter tell a refusal from any other
failure (`app/dashboard_refusal.py`). `refusal_for_host_config` returns the same
shape. The steps are:

1. If enforcement is disabled, return `None` (allow) — and log exactly one
   `WARNING` at first use, so an operator who set the kill switch sees it in the
   log without a line per call.
2. Resolve the principal from the bound actor context (`current_principal()`)
   and the resource from `resource_for`. `current_principal()` yields
   `LOCAL_OPERATOR` when the process itself is the caller — the local default —
   and `ANONYMOUS` only when a transport bound it for an unidentified caller
   (the dashboard's chat-stream `k-` pass-through), which is then denied.
3. Call `is_allowed(default_authorizer(), principal, action, resource)`.
4. On denial, record it and return the refusal.

**`resource_for(manifest, plan_name, story_key) -> Resource`** builds the ADR
0009 scope chain: `team` from the manifest's `team` field, `repo` from
`repo_root`, `plan` from the plan name, `story` from the story key (empty string
when there is none). Missing fields become empty strings rather than raising.

**`DEFAULT_TEAM = "local"`** is the team a plan belongs to when its manifest
declares none. One implicit team keeps local mode a single-operator deployment;
the real tenant chain is only populated once a shared control plane exists.

**Fail-closed rules.** Three paths could quietly become an allow, and none
does:

- **A policy that raises is a denial.** `is_allowed` is the fail-closed wrapper
  around the `Authorizer`; an exception from the policy lands as `False`, not as
  `True`.
- **An unreadable manifest never becomes an allow.** When the manifest cannot be
  read, `_load_manifest` returns `{}`, so `resource_for` falls back to
  `DEFAULT_TEAM` for `team` and to `""` for `repo` (it never raises). The
  failure can never short-circuit to "allowed because we could not check": the
  policy is consulted and returns its normal answer.
- **Recording the denial is best effort.** If appending the sidecar record
  fails, the denial is still returned; the failure only logs a warning.

The denial is appended as an `authorization_denied` record on the plan's
notification sidecar (`_notify_user(..., event="authorization_denied")`),
carrying the action and the principal subject, so an operator sees who was
turned away and from what. The same denial is also appended to the plan's audit
stream (§2, "Audit stream").

**`record_allowed=False`** keeps the decision but writes no `allowed` record;
denials are recorded in full either way. It exists for operations that run
continuously rather than when a human acts — `checkpoint` is the case it
serves, reached by every agent tick and every `checkpoint_story` call — where an
`allowed` record per call would bury the audit chain without adding a fact
worth having.

**What is enforced today.** Every action in the `Action` enum:

| Action | Status |
| --- | --- |
| `story:merge` | enforced (`pipeline/merge.py`) |
| `story:mark_done` | enforced (`pipeline/ci.py`) |
| `plan:approve` | enforced (`pipeline/plan_approval.py`) |
| `plan:reject` | enforced (`pipeline/plan_approval.py`) |
| `plan:pause` | enforced for both pause and resume (`pipeline/merge.py`) |
| `plan:ingest` | enforced (`pipeline/service.py`, `ingest_plan`) |
| `plan:dispatch` | enforced (`pipeline/dispatch.py`, and `pipeline/service.py`, `mark_story_in_progress` — claiming a story is the first step of dispatch) |
| `story:patch` | enforced (`pipeline/service.py`, `patch_story`, `set_story_status` and `checkpoint`; `checkpoint` records denials only) |
| `story:interrupt` | enforced (`pipeline/service.py`, `interrupt_story`) |
| `decision:answer` | enforced on the dashboard decision route (`app/dashboard.py`) |
| `config:write` | enforced for `set_plan_role_config` and `patch_plan` (`pipeline/service.py`), and for `set_role_default` through `refusal_for_host_config`, which decides against an empty resource and records in `<PLAN_DIR>/.audit.host.jsonl` because the model registry belongs to the host, not to a plan |

`LocalPolicy` allows the local operator every action. A plan's `owner` and its
`delegates` are additionally allowed `plan:approve`, `plan:reject` and
`story:merge` on that plan; ownership grants nothing else, and a plan with no
owner grants nothing to anyone but the local operator.

**Dashboard responses.** A dashboard route whose service call is refused answers
`403` with the detail `not authorized: <action>` (the action only, never the
principal) through `app/dashboard_refusal.py`. Every other failure keeps the
route's existing status, and an MCP caller still receives the full refusal dict.

**Audit stream.** Every decision this module makes is also appended to the plan's
audit stream (`<plan>.audit.jsonl` in `PLAN_DIR`; see
[REFERENCE.md](../../REFERENCE.md)) — except an allowed decision whose caller
passed `record_allowed=False`, which `checkpoint` does; denials are always
appended, whatever the caller asked for. `action` is the enforced label
from the table above, `outcome` is `allowed` or `denied`, `target` carries the plan and story, and
`detail` carries the principal subject. Records are written **only when
enforcement is on** — with `PIPELINE_POLICY_ENFORCE` disabled the function returns
before it consults the policy — so an `allowed` record always corresponds to a real
check of that action for that principal.

Alongside the per-plan streams there is one host-scoped stream,
`PLAN_DIR/.audit.host.jsonl`, for events that have no plan to belong to (a host
config write, for example). It is written by `append_host_audit`, hash-chained the
same way, and verified by the same walk: `verify_audit` reports the first altered
`seq` for the host chain and for every plan chain in the directory, so the whole
directory can be checked in one read-only call. What no verifier here can detect is
truncation of a chain's tail — without a recorded head, a shortened chain still
verifies intact.

The approval and merge transitions carry their own records rather than relying on
the authorization record: the decision is written as `plan.approved` or
`plan.changes_requested` (outcome `allowed`, or `refused` when the canonical digest
no longer matches), and a completed merge as `story.merge`. With enforcement off,
where `authorize` writes nothing, those transition records are the only trace of
the action.

## 3. The invariant that keeps local mode unchanged

With **no identity provider configured**, the pipeline behaves as it did
before this plan:

- `SharedSecretProvider` is only constructed inside `app/dashboard.py`, and it
  authenticates the same shared secret the middleware already required — it
  adds no new gate and rejects no request the old check accepted.
- `LocalPolicy` is consulted, through `default_authorizer()`, only by
  `pipeline/authorization.py` (§2, "Enforcement"). With nothing bound the
  principal is `LOCAL_OPERATOR`, which it allows for every action, so the
  enforced call sites pass exactly as they did before enforcement existed.
- The enforced actions are the only ones that can now refuse; §4 lists the
  mutations that still cannot.
- Enforcement can be turned off wholesale with `PIPELINE_POLICY_ENFORCE=0`, and
  when it is off every action is allowed and local behavior is identical to
  before this plan.
- No event, journal, notification or API response shape changes. The manifest
  gains one additive field, `ingested_by` (§2), which no existing reader
  requires.
- `pipeline/plan_approval.py` records the deciding principal's subject as
  `decided_by` and its most privileged role as `role`; with nothing bound that
  principal is `LOCAL_OPERATOR`, so local mode records the same actor it always
  did.

This is ADR 0009 section 7 ("local mode is unchanged") and the first part of
ADR 0008 staging step 1 ("verified caller identity at the gateway, RBAC and
ownership records, authorization enforced in `PipelineService`"). The
identity-at-the-gateway piece, service-layer enforcement for every action in
the `Action` enum and the plan ownership records have landed; RBAC roles and
the mutating tools outside the enum (§4) are the follow-up in §6. Enforcement was added together with a
`LOCAL_OPERATOR`-allows-everything policy, so it changes no local behavior while
the seam is adopted incrementally.

## 4. What it does NOT do

Do not mistake any row of this table for a control that exists.

| Not implemented | Consequence today |
| --- | --- |
| **Enforcement outside the `Action` enum** | Every enum action is enforced (§2, "Enforcement"). Mutating MCP tools whose service path never calls `authorize()` — `save_plan`, `record_security_audit`, `review_story`, `advance_pipeline`, `advance_all_plans`, `decompose_plan` and `request_decision` — run for any principal. The dashboard's `set_role_default` route is not one of them: it requires `config:write`, decided against an empty resource because `model_registry.json` is host-scoped rather than owned by a plan, and the decision is recorded in `<PLAN_DIR>/.audit.host.jsonl`. |
| **Plan owner / author / delegate records** | A plan's manifest now records an `owner` and `delegates`, but `ROLE_OWNER`, `ROLE_AUTHOR` and `ROLE_DELEGATE` are defined without being assigned to any principal, and `request_decision` has no owner to route to. Ownership is chosen at ingest, not authored: on a first ingest (no prior manifest) only the local operator may set an `owner` that differs from the caller, defaulting to the ingester, and `delegates`, defaulting to none; any other first-ingest caller owns the plan they created, has its delegates dropped, and its ignored naming is audited as `plan:ownership_change` with outcome `ignored`. On every later ingest — including a wholesale-replace (`overwrite=True`) re-ingest — the manifest's existing ownership is the starting point; the plan's `owner` replaces it only when the plan names one and the plan's `delegates` replace them only when that key is present, and nothing is recorded when the result is unchanged. Any such change is accepted only when the ingester is the local operator or the plan's current non-empty owner — a delegate cannot change ownership, and anyone else is refused with no manifest written. A manifest written before ownership existed stays owner-less on re-ingest unless the local operator names an owner. Every attempted change, allowed or denied, is recorded as `plan:ownership_change` in the plan's audit stream with the from/to owner and whether the delegates changed. |
| **System principals** | `system:scheduler` and `system:worker` do not exist. The scheduler and worker act with no principal at all, and `LocalPolicy` would deny them if they did. |
| **OIDC in the browser** | The `OidcProvider` itself has landed (§2, "OIDC provider"): JWT/JWKS verification, the issuer/audience/expiry checks and the group-to-role mapping all exist. What does not is a login flow: the bundled dashboard mints no token, so under `PIPELINE_IDENTITY_PROVIDER=oidc` its pages load but every `/api/` call is a `401` unless the client sends a bearer token. OIDC authenticates API clients, not the UI. |
| **Remote MCP transport hardening** | The transport itself has landed (§2, "Remote MCP transport"): an OIDC-gated HTTP endpoint that verifies a token and binds a `Principal`. What is missing is everything an internet-facing endpoint wants — **no rate limiting at the gate** (the only outbound bound is the JWKS refetch limit), **no TLS** (put a reverse proxy in front), no tenant scoping on the allowlist that does exist — remote principals are limited to an explicit tool allowlist (`pipeline/remote_tool_policy.py`): only the ten enforced tools consult `authorize`, the six allowed read tools are not tenant-scoped, and every other tool is refused before its handler runs, while a local caller still reaches the unchecked tools in the row above. A proxied public host name is accepted through `PIPELINE_MCP_ALLOWED_HOSTS`, and the HTTP transports refuse to start when `PIPELINE_POLICY_ENFORCE` disables enforcement. Authentication attempts are recorded in the host-scoped hash-chained stream (`<PLAN_DIR>/.audit.host.jsonl`), which has no verifier yet. |
| **Audit stream durability** | The stream itself exists (§2, "Audit stream"): ADR 0009 section 8's `{actor, action, target, outcome, correlation_id, at}` records are appended, hash-chained, per plan. What is missing is an external anchor — the head hash is never shipped off-box — so truncation of the tail is undetectable and a record cannot be proven against a third-party witness. |
| **Actor on the remaining mutations** | Ingest (`ingested_by`), plan approval (`decided_by`/`role`) and dashboard decisions (`actor`) record an actor; nothing else does. Dispatch, story status changes, pause/resume and decision answers record no actor at all. `Principal.roles` is recorded but never checked — enforcement reads the subject and the resource, not the roles. |

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

The seam is established and enforced for every action in the `Action` enum;
the rest is still prep.
The remaining work, in dependency order:

1. **Enforcement on the remaining mutations** (ADR 0009 section 2). Landed for
   every action in the `Action` enum through `pipeline/authorization.py` (§2,
   "Enforcement"). What is left is the mutating MCP tools outside the enum
   listed in §4's first row; until each calls `authorize()`, that row stands.
2. **Ownership records.** Landed in part: a plan's manifest records an `owner`
   and `delegates`, and they may approve, reject and merge that plan's work
   (§2, "Enforcement"). What remains is giving `request_decision` the owner as
   its routing target (ADR 0006).
3. **System principals.** Add `system:scheduler` and `system:worker` as
   distinct, narrowly scoped principals that are audited like a human and
   cannot approve a plan (ADR 0009 section 6).
4. **The audit stream.** Landed: `pipeline/audit.py` appends the ADR 0009
   section 8 record for every decision the policy makes (§2, "Audit stream"),
   except an allowed decision taken with `record_allowed=False` (`checkpoint`);
   denials are appended either way. What remains open is the external anchor —
   shipping the head hash off-box, so that truncation of the tail becomes
   detectable.
5. **The OIDC adapter and the remote MCP transport.** Landed: `OidcProvider`
   (§2, "OIDC provider") verifies a JWT against a JWKS with the issuer,
   audience and expiry checks and maps its claims to a `Principal`, and the
   remote MCP transport (§2, "Remote MCP transport") verifies the same token
   and yields the same `Principal` for the shared server — naming an HTTP
   `PIPELINE_MCP_TRANSPORT` and `PIPELINE_IDENTITY_PROVIDER=oidc` makes a
   remote client authenticated. Authentication attempts are recorded in the
   host-scoped hash-chained stream (`<PLAN_DIR>/.audit.host.jsonl`). What
   remains open is a verifier for that stream and the hardening in §4's
   remote-transport row.

Steps 1–3 are the multi-tenant implementation ADR 0008 calls "not prep"; step 4
has landed as far as the local stream goes; step 5 is the provider swap the seam
was built to accept.
