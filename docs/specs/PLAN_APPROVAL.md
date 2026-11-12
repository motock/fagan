# Plan Approval Lifecycle

- **Status:** Implemented (gate, digest, transitions, dashboard surfacing)
- **Stories:** ENT-LC-01 (state + digest), ENT-LC-02a (transitions),
  ENT-LC-05 (dashboard surfacing + this spec)
- **Audience:** Operators approving ingested plans, and contributors touching
  `pipeline/plan_approval.py`, ingest, the patch tools, or the dashboard plan
  summary.

## 0. Summary

Plan approval is an **opt-in** gate that ties a human/agent decision to the
*exact authored content* of a plan. It is **default off**: with
`PIPELINE_PLAN_APPROVAL` unset, every manifest is written exactly as it was
before this feature existed — no `approval` key is added, modified or removed,
and the dashboard plan summary is byte-identical to its pre-approval shape.

When the gate is on, ingest records an `approval` block on the manifest, the
plan moves through a small state machine, and the dashboard plan summary
surfaces the decision so an operator can see what was approved, by whom, and
against which content digest.

## 1. The `PIPELINE_PLAN_APPROVAL` variable

`pipeline.plan_approval.approval_enabled()` parses the environment variable on
**every call** (never cached), so a long-lived process honours a changed
environment.

| Value | Result |
| --- | --- |
| unset | `False` — **default off** |
| `off`, `0`, `false`, empty string | `False` |
| `on`, `1`, `true` | `True` |
| anything else | **raises** `ValueError` |

The accepted spellings are therefore `off` and `on` (plus the numeric/boolean
aliases above). An **unknown** value **raises** a `ValueError` naming both the
variable and the offending value: a typo must fail closed rather than silently
disabling — or silently enabling — the gate.

## 2. Lifecycle

```
draft ──ingest──▶ pending_approval ──approve_plan──▶ approved
                          │
                          └──reject_plan──▶ changes_requested
```

- **`draft`** — the plan exists but has not been ingested with the gate on, so
  it carries no `approval` record at all. The dashboard shows no approval
  state for it.
- **`pending_approval`** — ingest wrote an `approval` record; the authored
  content has not been approved (yet). This is also the state a previously
  `approved` plan falls back to when its authored content changes (see §4).
- **`approved`** — a decision was recorded against *this exact* authored
  content. `is_activatable()` returns true only for this state; wiring that
  check into dispatch is tracked separately (see §10), so until it lands an
  approval is advisory rather than enforced.
- **`changes_requested`** — a reviewer asked for changes; the record carries
  the reviewer's `reason`. The plan is not activatable once dispatch
  enforcement lands (see §10); today it still dispatches.

`changes_requested` is not terminal: a later `approve_plan` moves the plan to
`approved`, and a re-ingest after an authored change resets it to
`pending_approval`.

## 3. The digest

Every approval record carries a `digest` of the plan's authored content,
computed by `canonical_digest(manifest)`:

- It hashes **only authored content**: the plan-level `repo_root`,
  `role_config`, `context` and `team`, plus each story's authored fields
  (`summary`, `agent_instructions`, `dependencies`, `persona`, `model`,
  `acceptance`, `risk`, `backend`, `tdd_split`, `files`, `blocked_by`,
  `description`), and `test_scope` at plan and story level only when the key is
  present, so manifests without it keep their existing digest.
- It **excludes** the `approval` block itself and all runtime story state
  (`status`, `attempts`, `sizing_auto_routed`, …). Recording or changing a
  decision therefore never invalidates the digest it was made against, and a
  story moving from `todo` to `done` does not revoke an approval.
- The payload is JSON-serialised with sorted keys and no whitespace, then
  hashed with SHA-256. The manifest is not mutated.
- A story's `epic` and authored `plan_key`, and the plan's `plan_epics`, `preflight_override` and `repo_url`, are recorded on the manifest but are not plan-defining and do not reset approval.

### 3.1 Digest version

The digest binds an approval to content, but *what the digest covers* can
change between releases. So every approval record also carries a
`digest_version`: the value of `APPROVAL_DIGEST_VERSION` this build writes,
and the version the record's `digest` was computed under.

- A record **without** the field reads as the legacy version `1`: records
  written before the field existed were made under version 1. The current
  version is `2` (descriptions and the plan's team joined the digest), so a
  version-1 record is stale and fails closed until it is re-approved once.
- **Any non-current value fails closed** — a bool (`True` is an `int`
  subclass), a string, a float, `None`, a lower version and, importantly, a
  *higher* version: a rolled-back build must not honour a record written by a
  newer one.
- `on_ingest`, `reset_if_changed` and `_decide_plan` write the current version
  on every record they write, the decisions-log entry included.
- `on_ingest` keeps `approved` only when the digest matches **and** the
  previous record's version is current; `reset_if_changed` resets when the
  digest differs **or** the version is not current.
- `is_activatable` requires `approved` **and** a current version, so an
  out-of-version approval never dispatches. `advance` and `dispatch` add
  `reason: approval_digest_outdated` to their existing refusal dicts
  (`skipped: plan_not_approved` and `error: plan is not approved`
  respectively) so the operator learns *why* the plan stopped; a
  current-version record keeps exactly the dicts it returned before.
- `approve_plan` / `reject_plan` refuse an out-of-version record instead of
  upgrading it — approving in the same call would approve content the caller
  may never have been shown. The refusal writes no manifest, no decisions-log
  entry and no notification; it records exactly one `refused` audit event with
  `detail.reason` = `outdated_digest_version` and answers exactly:

  ```
  plan approval was recorded under an older digest version; run `python -m pipeline.tools.reset_outdated_approvals`, review the plan, then decide
  ```

  Recovering means resetting the record (a re-ingest or the reset tool), not
  deciding in place.

#### Re-approval after a digest version change

Bumping `APPROVAL_DIGEST_VERSION` invalidates every stored approval on
purpose: the digest those records carry was computed under a different
definition of "the content", so it cannot be compared with a digest computed
now, and neither `is_activatable` nor `_decide_plan` will honour it. Without a
repair path every approved plan would be stranded — unable to dispatch *and*
unable to be re-approved, because deciding on an out-of-version record is
refused.

The one-time repair is:

```
python -m pipeline.tools.reset_outdated_approvals [--dry-run]
```

It walks every plan whose manifest exists, recomputes the canonical digest of
the content on disk now, and, under each plan's lock, gives the record one of
these outcomes:

| Outcome | What happened |
| --- | --- |
| `reset_to_pending` | An `approved` record — or a malformed one, or one with an unrecognized status: fail closed — becomes `pending_approval` with the current `digest` and `digest_version`. `decided_by`, `at`, `decided_at`, `correlation_id`, `role`, `self_approved` and a `changes_requested` `reason` are cleared exactly as `reset_if_changed` clears them; `warnings_seen` is kept and `reset_reason: "digest_version_upgrade"` is recorded. One `plan_pending_approval` notice follows. |
| `restamped` | A `pending_approval` or `changes_requested` record keeps its status — and its `changes_requested` reason, which still stands — and is rebound to the current `digest` and `digest_version`. The plan stays non-activatable. |
| `current` | The record was already written under the current version. Untouched. |
| `no_record` | The plan has no `approval` dict (the gate-off shape). Untouched. |
| `skipped_locked` | The plan lock was held; nothing was read or written. |
| `skipped_unreadable` | The manifest could not be read; nothing was written. |

**It never approves a plan.** Every record the command writes carries a status
other than `approved`, so a plan approved under version 1 leaves the command as
`pending_approval`: the next `approve_plan` or dashboard approve is a
deliberate decision on content the reviewer is shown (the dashboard sends its
`expected_digest`, so a stale view is refused rather than approved).

Each write appends exactly one `plan.approval_reset` audit event, `outcome:
allowed`, with `detail: {from_version, to_version, previous_status, outcome}`
and no content or principals beyond the actor the audit module stamps. Output
is one JSON line per plan (`{plan, outcome}`) followed by a summary line
(`{reset_to_pending, restamped, current, no_record, skipped}`); the exit code is
`0` when nothing was skipped and `1` otherwise. A second run reports `current`
for everything — the command is idempotent, and a `skipped_locked` or
`skipped_unreadable` plan is simply picked up by the next run.
`--dry-run` reports the same outcomes and writes, audits and notifies nothing.

## 4. Reset rules

An approval must not outlive the content it approved. Two paths enforce that:

1. **At ingest** (`on_ingest`). The digest is recomputed and the previous
   record is kept as `approved` **only** when it was already `approved` *and*
   its stored digest still matches. Any authored change therefore **resets**
   the record to `pending_approval` while preserving the previous decision
   metadata (`decided_by`, `at`, `role`, `self_approved`) for auditability.
2. **Through the patch tools** (`reset_if_changed`), called by `patch_story`
   and `patch_plan`. When the recomputed digest differs from the stored one,
   the record is **reset** to `pending_approval`, the digest is updated, and
   the decision metadata — including a `changes_requested` `reason` — is
   cleared. A matching digest changes nothing.

`reset_if_changed` is not a blanket guard for every writer of authored fields:
`save_plan` is covered by the source-digest check at the next ingest, while
`set_plan_role_config` writes `role_config` directly and is not reset there —
an approval can outlive such a change until the next ingest.

A successful full ingest consumes the staged draft (the draft is deleted once
the manifest is written, ADR 0012 decision 5), so after it the manifest is the
plan record; an `only_epics` ingest, a refused ingest and a `skipped: locked`
ingest keep the draft. Consuming the draft does not move what an approval
binds to: the digest is still computed from the manifest's plan-defining
content, so re-staging an export and re-ingesting it unchanged keeps the
approval.

Changing a story's `description` or the plan's `team` and re-ingesting resets
the approval to `pending_approval`; `patch_story` cannot edit `description`.

Both paths are **no-ops while the gate is off**, so the default-off path stays
byte-identical to before the feature existed.

## 5. `approve_plan` and `reject_plan`

`pipeline.plan_approval.PlanApprovalMixin` is mixed into `PipelineService`;
`pipeline/server.py` keeps one-line `@mcp.tool()` delegations.

- **`approve_plan(plan_name, *, expected_digest=None)`** — `pending_approval`
  (or `changes_requested`) → `approved`.
- **`reject_plan(plan_name, reason="", *, expected_digest=None)`** —
  `pending_approval` → `changes_requested`, recording `reason` when given. The
  transition is **unguarded**: `_decide_plan` never checks the current status,
  so `approved` → `changes_requested` is accepted too (a reviewer may revoke an
  approval).

`expected_digest` is the digest the caller was shown. A provided value that
does not exactly equal the manifest's current canonical digest — including the
empty string — is refused with

```json
{"ok": false, "error": "plan changed since it was displayed; reload before deciding", "stale_digest": true}
```

and writes **nothing** except one audit record: action `plan.approved` (or
`plan.changes_requested`), outcome `refused`, target `{"plan": <name>}`, detail
`{"reason": "stale_digest"}`. No manifest save, no decisions-log entry, no
notification. `expected_digest=None` behaves exactly as before the keyword
existed; MCP callers (the `approve_plan` tool in
`pipeline/server_tools_lifecycle.py`) do not pass it.

Both delegate to `_decide_plan`, which:

- validates the plan name, then returns
  `{"ok": True, "skipped": "approval_disabled"}` **without reading or writing
  the manifest** while the gate is off;
- returns `{"ok": True, "skipped": "locked"}` when the plan's transaction lock
  is held;
- returns `{"ok": False, "error": ...}` when the plan does not exist;
- **refuses** with the `stale_digest` payload above, before the digest-on-record
  check, when a non-`None` `expected_digest` differs from the recomputed
  digest;
- **refuses** with `{"ok": False, "error": "plan content changed; re-ingest
  before approving"}` when the recomputed digest differs from the digest on
  the record — the operator would otherwise be approving content they never
  saw;
- otherwise writes the decision onto the manifest, appends it to the
  decisions log, records it in the audit stream, and returns
  `{"ok": True, ...record}`. A recorded decision then sends
  `plan_approved` (or `plan_changes_requested` when the status is not
  `approved`); every return path above sends nothing.

The decision record carries `plan`, `status`, `digest`, `decided_by`, `at`,
`role`, `self_approved` and `warnings_seen`, plus `reason` for a
`changes_requested` decision.

## 6. Self-approval

**Self-approval is permitted and recorded** — the term *self-approval* means
the decider is also the author. Until real identity exists
(ENT-LC-02b), every decision is attributed to the fixed actor
`local-operator`, and the record sets `self_approved: True`. The flag is
surfaced in the dashboard summary rather than suppressed, so an audit can tell
that the same actor authored and approved the content.

## 7. Local mode is unchanged

The gate is orthogonal to execution backend. **Local mode is unchanged**:
routing a role to a local model, running the local dispatch preflight, and
dispatching locally all behave exactly as before. With the gate off — the
default — nothing about local mode differs from the pre-approval pipeline. With
the gate on, a local-mode plan is subject to the same approval gate as any
other plan; the gate never rewrites `backend` or the routing configuration.

## 8. `warnings_seen`

`warnings_seen` is **reserved for the approval-time snapshot** of the
advisories that were visible when the decision was made. It is a list, always
present on a record written by ingest or by a decision, and it is preserved
across a reset (it is non-decision metadata, kept for auditability). Nothing
else may write it: consumers should treat it as a read-only historical
snapshot, not as a live view of current warnings.

## 9. Dashboard surfacing

### The deciding routes

Two `POST` routes decide a plan from the dashboard. Both are **digest-bound**:
the caller sends the 64-character lowercase-hex digest it was shown, and a
decision made against any other digest is refused. Neither route writes the
decision record, the audit record or the notification itself — the service
(`pipeline.plan_approval`) owns all three, so a decision is recorded exactly
once and a refused or skipped one records nothing.

| Route | Body |
| --- | --- |
| `POST /api/plans/{plan_name}/review/approve` | `{"digest": "<64 hex>"}` |
| `POST /api/plans/{plan_name}/review/request-changes` | `{"digest": "<64 hex>", "reason": "<1..2000 chars>"}` |

Both bodies forbid extra fields (`extra="forbid"`), and `digest` must match
`^[0-9a-f]{64}$` — lowercase hex only, and no trailing newline. `reason` is
required, is **stripped first** and must then be 1..2000 characters: `"   "` is
an empty reason (`422`), while a 2000-character reason the caller padded with
whitespace is accepted and stored stripped.

#### Check order (fail closed)

1. **A verified login.** `pipeline.actor.current_principal()` must carry
   `auth_method == "oidc"`. Otherwise `403 dashboard approval requires login`
   plus one refused audit record whose `detail.reason` is `unverified_principal`.
   Shared-key callers — including the local operator — keep using the MCP
   `approve_plan` tool: a shared secret authenticates a deployment, not the
   person a decision has to be attributed to.
2. **Enforcement on.** `pipeline.authorization.enforcement_enabled()` must be
   true. Otherwise `403 policy enforcement is disabled` plus a refused audit
   record whose `detail.reason` is `policy_enforcement_disabled`: with
   enforcement off every action is allowed, so an "approval" would record
   nothing.
3. **The plan name.** An invalid key is the same fixed `404 plan not found` the
   read route returns.
4. **The decision**, delegated to `approve_plan` / `reject_plan` with
   `expected_digest` set to the body's `digest`.

The order is part of the contract: steps 1 and 2 run before the plan name is
validated, so a refusal never reports whether a plan exists.

#### Status codes

| Status | Meaning |
| --- | --- |
| `200` | `{status, digest, decided_by, at, self_approved}` — exactly those five keys. The principal, a path and any exception text are never in the body. |
| `403` | `dashboard approval requires login` — the caller is not an OIDC principal. |
| `403` | `policy enforcement is disabled`. |
| `403` | `not authorized: plan:approve` (or `plan:reject`) — the policy refused this principal, for example an `approver`-role principal who neither owns the plan nor is one of its delegates. |
| `404` | `plan not found` — an invalid plan key, or no such plan. |
| `409` | `plan is busy; retry` — the store could not acquire the plan. Retryable: no decision record was written. |
| `409` | `plan changed since it was displayed; reload before deciding` — `digest` is stale against the plan as it is on disk now. Nothing is written, and repeating the same stale digest stays a `409`: the route never substitutes the current digest, because that would approve content the operator never saw. |
| `409` | `plan content changed; re-ingest before approving` — the stored approval digest no longer matches the content. |
| `409` | `plan approval was recorded under an older digest version; run python -m pipeline.tools.reset_outdated_approvals, review the plan, then decide` — the stored record's `digest_version` is not the current one (see §3.1). The refusal flag is `outdated_digest_version`; nothing is written, and this refusal never falls through to the `404 plan not found`. |
| `409` | `plan approval is not enabled` — `PIPELINE_PLAN_APPROVAL` is off. |
| `422` | The body is invalid: missing or malformed `digest`, an extra field, or a missing, blank or over-long `reason`. |

#### CSRF, and why a GET never decides

Both routes are `POST` with a JSON body and add **no** exemption to the
dashboard's same-origin proof. The `/api/` middleware authenticates the request
first; a session cookie is an ambient credential, so a `POST` that rides it must
present `Sec-Fetch-Site: same-origin` or an `Origin` equal to the configured
redirect URI's origin. A cross-site `POST` is refused with `403` by the
middleware before the handler runs, and therefore before anything is decided. A
bearer token is an explicit credential and needs no such proof.

**A GET never decides.** A `GET` on either path is refused with `405` by an
explicit route, so a link, a browser prefetch or an `<img>` tag can never
approve or ask for changes on a plan. (The route is explicit because the
dashboard's static mount would otherwise answer such a request with a bare
`404`, indistinguishable from a mistyped URL.)

### The review endpoint

`GET /api/plans/{plan_name}/review` (`app/dashboard_routes_approval.py`,
mounted under `/api` so the dashboard's existing `/api/` middleware
authenticates it) returns the payload an operator reviews a plan against: the
stored approval record (`status`, `digest`, `decided_by`, `at`,
`self_approved`, `reason`), `current_digest` — the digest of the authored
content as it is on disk right now — `digest_current` (whether the stored
digest still matches it, i.e. whether the approval is stale), `viewer_is_author`
(the viewer's subject compared with the manifest's `ingested_by`, the same
comparison `_decide_plan` records as `self_approved`), `owner`, `team` (the
plan's stored team, part of the approved digest), `role_config`, a
field-by-field projection of each story (including its `description` — the
story content the digest now binds, returned as JSON text, never rendered as
HTML), and `warnings` from `pipeline.plan_review.review_warnings`. The manifest
itself is never returned and the route never writes.

Visibility is `app.policy.can_view` over
`pipeline.authorization.resource_for(manifest, plan_name, None)`: the plan's
owner, one of its delegates, and the local operator see it; everyone else does
not. An invalid plan key, a missing manifest and a plan the caller may not see
are the **same** 404 with the **same** body, `{"detail": "plan not found"}` — no
existence oracle. Only for a visible plan are the two gate failures reported,
both `409`: `plan approval is not enabled` when the gate is off, and
`plan has no approval record; re-ingest it` when the manifest carries no
`approval` dict. Unauthenticated callers are refused by the middleware (401)
before the handler runs.

Core ships **no** review page: this is a JSON API. An operator supplies a review
UI through the dashboard extension mount (`app/dashboard_mount.py`, see
"Mounted dashboard extensions"), whose `ctx.router` routes are included under
`/api/ext` behind the same authentication.

`app/dashboard_helpers._plan_summary` adds an `"approval"` key **only** when
the manifest carries an approval record. The value is a six-field projection:

| Field | Meaning |
| --- | --- |
| `status` | `pending_approval`, `approved` or `changes_requested` |
| `digest` | the content digest the decision was made against |
| `decided_by` | the actor that decided |
| `at` | the decision timestamp (ISO-8601) |
| `self_approved` | whether the decider also authored the content |
| `warnings_seen` | the approval-time warning snapshot |

When no approval record exists, the key is **absent** — not `null`, not an
empty dict — and every other summary key keeps its name and value, so the
default-off response is byte-identical to today's.

## 10. Non-goals

- This spec does not change `REFERENCE.md`; the MCP tools section there is
  verbatim-pinned and is not the documentation surface for this feature.
- Enforcement of the gate at dispatch is tracked separately; this document
  describes the state, the transitions and the surfacing that exist today.

## 11. Approval notifications

Approval changes are announced through the ordinary notification channel, as
three events:

| Event | Meaning |
| --- | --- |
| `plan_pending_approval` | the plan's authored content is awaiting a decision |
| `plan_approved` | the plan was approved |
| `plan_changes_requested` | changes were requested on the plan |

A reset performed by `patch_story` or `patch_plan` also sends
`plan_pending_approval`, once the reset has been saved.

A recorded decision sends `plan_approved` when the plan was approved and
`plan_changes_requested` when changes were requested, once the decision has
been saved to the manifest, appended to the decisions log and recorded in the
audit stream. No other outcome notifies: a disabled gate, an authorization
refusal, a held lock, a missing plan and a digest mismatch all leave the
notification channel silent, because none of them changes the plan's state.
The change-request `reason` is never part of the notification — it is
reviewer text, so it stays in the manifest and the review UI, not on a channel
an operator may not control.

Ingest emits `plan_pending_approval` only while `PIPELINE_PLAN_APPROVAL` is on
and the plan is **newly** pending approval. A plan is newly pending when the
recomputed record is `pending_approval` and either there is no previous
`approval` record, or the previous status was not `pending_approval`, or the
previous digest differs from the new one. Re-ingesting unchanged content is
therefore silent, editing the authored content notifies again, and a plan that
is `approved` against the current digest stays silent across re-ingests. The
notice is sent after the manifest is written, and a delivery failure is logged
by exception type only: it never fails the ingest nor loses the manifest.

Message content is deliberately minimal. A notice carries identifiers (the plan
name and the event), counts (stories, risk summary, warning count, the approval
digest), and — when `PIPELINE_DASHBOARD_PUBLIC_URL` is configured — an optional
link to the dashboard filtered by plan name. It never carries brief text, file
paths, the reviewer's `reason` text, or secrets; the link never contains a
token, so a notice can never approve anything. A notice is one per plan and
digest, so retries do not repeat it.

These events are routed like any other: `notifications.json` entries select
them with the usual `match.plan` / `match.owner` globs, so an operator can send
approval traffic to a different channel without changing the pipeline.
