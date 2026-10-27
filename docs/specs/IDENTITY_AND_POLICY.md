# Identity and Policy Seams

- **Status:** Seam plus partial enforcement — types, a local provider and a
  deny-by-default policy exist, and the service layer enforces them for five
  actions (§3, `pipeline/authorization.py`); everything else in §4 is still
  unenforced. Written 2026-10-26.
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
actions that matter most — `story:merge`, `story:mark_done`, `plan:approve`,
`plan:reject` and `plan:pause` — through `pipeline/authorization.py` (§2,
"Enforcement"). Enforcement is **on by default** and
`PIPELINE_POLICY_ENFORCE` is the operator's kill switch.

It is still a partial boundary. Every other action in §4 is unenforced: a
principal denied by `LocalPolicy` for `plan:ingest`, `plan:dispatch`,
`story:patch`, `story:interrupt`, `decision:answer` or `config:write` changes
nothing, and the mutation still runs. With no identity provider configured the
bound principal is `LOCAL_OPERATOR`, which `LocalPolicy` allows, so local mode
behaves as it did before this plan.

Read this document as a map of the wiring points, not as evidence of a
complete security boundary. §4 lists, in one table, everything the seam does
**not** do.

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
recording sites in §2 read it through `actor_id()`. Whether that principal may
perform an action is a separate question, answered in the service layer by the
section below.

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

**`authorize(action, *, plan_name, story_key=None, manifest=None) -> dict | None`**
is the single entry point the service calls. It returns `None` when the action
is allowed and a refusal dict `{"ok": False, "error": ...}` when it is denied —
the same shape every mutating service operation already returns, so a refusal
needs no special handling at the call site. The steps are:

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
turned away and from what before the audit stream of §4 exists.

**What is enforced today.** Five actions, four of them plus pause/resume:

| Action | Status |
| --- | --- |
| `story:merge` | enforced (`pipeline/merge.py`) |
| `story:mark_done` | enforced (`pipeline/ci.py`) |
| `plan:approve` | enforced (`pipeline/plan_approval.py`) |
| `plan:reject` | enforced (`pipeline/plan_approval.py`) |
| `plan:pause` | enforced for both pause and resume (`pipeline/merge.py`) |

**What is still unenforced.** These actions exist in the `Action` enum but no
service path asks `authorize` for them, so `LocalPolicy` cannot block them
today: `plan:ingest`, `plan:dispatch`, `story:patch`, `story:interrupt`,
`decision:answer`, `config:write`.

**Audit stream.** Every decision this module makes is also appended to the plan's
audit stream (`<plan>.audit.jsonl` in `PLAN_DIR`; see
[REFERENCE.md](../../REFERENCE.md)): `action` is the enforced label
(`story:merge`, `story:mark_done`, `plan:approve`, `plan:reject`, `plan:pause`),
`outcome` is `allowed` or `denied`, `target` carries the plan and story, and
`detail` carries the principal subject. Records are written **only when
enforcement is on** — with `PIPELINE_POLICY_ENFORCE` disabled the function returns
before it consults the policy — so an `allowed` record always corresponds to a real
check of that action for that principal.

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
- The four (plus pause/resume) enforced actions are the only ones that can now
  refuse; §4 lists the actions that still cannot.
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
identity-at-the-gateway piece and service-layer enforcement for the actions in
§2 have landed; the RBAC/ownership records and enforcement for the remaining
actions in §4 are the follow-up in §6. Enforcement was added together with a
`LOCAL_OPERATOR`-allows-everything policy, so it changes no local behavior while
the seam is adopted incrementally.

## 4. What it does NOT do

Do not mistake any row of this table for a control that exists.

| Not implemented | Consequence today |
| --- | --- |
| **Enforcement on the remaining actions** | Enforcement exists for `story:merge`, `story:mark_done`, `plan:approve`, `plan:reject` and `plan:pause` (§2, "Enforcement"). `plan:ingest`, `plan:dispatch`, `story:patch`, `story:interrupt`, `decision:answer` and `config:write` do not call `authorize()`, so a principal denied by `LocalPolicy` for those changes nothing: the mutation still runs. |
| **Plan owner / author / delegate records** | Plans carry no ownership. `ROLE_OWNER`, `ROLE_AUTHOR` and `ROLE_DELEGATE` are defined but nothing assigns or reads them, and `request_decision` has no owner to route to. |
| **System principals** | `system:scheduler` and `system:worker` do not exist. The scheduler and worker act with no principal at all, and `LocalPolicy` would deny them if they did. |
| **OIDC provider** | There is no `OidcProvider`. `SharedSecretProvider` is the only `IdentityProvider`, so there is no JWT/JWKS verification, issuer/audience/expiry check, or group-to-role mapping. |
| **Remote MCP transport** | MCP is stdio only. There is no HTTP transport layer to verify a token and yield a `Principal`. |
| **Audit stream** | No allow/deny record is emitted. ADR 0009 section 8's `{actor, action, target, outcome, correlation_id, at}` stream does not exist. |
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

The seam is established and enforced for five actions; the rest is still prep.
The remaining work, in dependency order:

1. **Enforcement on the remaining actions** (ADR 0009 section 2). Landed for
   `story:merge`, `story:mark_done`, `plan:approve`, `plan:reject` and
   `plan:pause` through `pipeline/authorization.py` (§2, "Enforcement"). What is
   left is the other six in §4: each mutating path calls `authorize()` so a
   denied principal blocks it. Until then, §4's first row stands.
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
