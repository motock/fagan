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
  content. Only this state is activatable when the gate is on.
- **`changes_requested`** — a reviewer asked for changes; the record carries
  the reviewer's `reason`. The plan is not activatable.

`changes_requested` is not terminal: a later `approve_plan` moves the plan to
`approved`, and a re-ingest after an authored change resets it to
`pending_approval`.

## 3. The digest

Every approval record carries a `digest` of the plan's authored content,
computed by `canonical_digest(manifest)`:

- It hashes **only authored content**: the plan-level `repo_root`,
  `role_config` and `context`, plus each story's authored fields (`summary`,
  `agent_instructions`, `dependencies`, `persona`, `model`, `acceptance`,
  `risk`, `backend`, `tdd_split`, `files`, `blocked_by`).
- It **excludes** the `approval` block itself and all runtime story state
  (`status`, `attempts`, `sizing_auto_routed`, …). Recording or changing a
  decision therefore never invalidates the digest it was made against, and a
  story moving from `todo` to `done` does not revoke an approval.
- The payload is JSON-serialised with sorted keys and no whitespace, then
  hashed with SHA-256. The manifest is not mutated.

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

Both paths are **no-ops while the gate is off**, so the default-off path stays
byte-identical to before the feature existed.

## 5. `approve_plan` and `reject_plan`

`pipeline.plan_approval.PlanApprovalMixin` is mixed into `PipelineService`;
`pipeline/server.py` keeps one-line `@mcp.tool()` delegations.

- **`approve_plan(plan_name)`** — `pending_approval` (or
  `changes_requested`) → `approved`.
- **`reject_plan(plan_name, reason="")`** — `pending_approval` → 
  `changes_requested`, recording `reason` when given.

Both delegate to `_decide_plan`, which:

- validates the plan name, then returns
  `{"ok": True, "skipped": "approval_disabled"}` **without reading or writing
  the manifest** while the gate is off;
- returns `{"ok": True, "skipped": "locked"}` when the plan's transaction lock
  is held;
- returns `{"ok": False, "error": ...}` when the plan does not exist;
- **refuses** with `{"ok": False, "error": "plan content changed; re-ingest
  before approving"}` when the recomputed digest differs from the digest on
  the record — the operator would otherwise be approving content they never
  saw;
- otherwise writes the decision onto the manifest, appends it to the
  decisions log, and returns `{"ok": True, ...record}`.

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
