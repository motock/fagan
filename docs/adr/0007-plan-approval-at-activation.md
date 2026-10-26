# 0007. Plans are approved before activation, not before ingestion

- **Status:** Proposed (direction agreed 2026-10-25; revisit before implementation)
- **Date:** 2026-10-25

## Context

The enterprise workflow this project is trending toward gives every engineer the
ability to draft and ingest plans, and gives a team lead a review-and-approve
step before the work runs. That step does not exist today, and it cannot be
added as a boolean on a plan name.

The current lifecycle has no gate:

- `save_plan` writes `PLAN_DIR/{plan_name}.json` with no uniqueness check
  (`pipeline/service.py:646`).
- `ingest_plan` materializes `{plan_name}.manifest.json`.
- `advance_all_plans` iterates **every** manifest and dispatches ready stories
  on each tick (`pipeline/service.py:679`). The only brake is a whole-plan
  `paused` flag (`pipeline/advance.py:317`), which is unowned, unauthenticated,
  and pauses merges as well as dispatch.

So today **ingestion is activation**. There is no state in which a plan is
validated and visible but not dispatchable.

Four forces shape the answer:

1. **The gate should not block validation.** Ingest already runs schema,
   story-key conflict (`pipeline/ingest.py:697`), sizing, preflight, and
   acceptance-fixture checks, and optionally creates tickets. Reviewing a plan
   that might not ingest is reviewing the wrong artifact.
2. **Activation is the meaningful boundary.** Nothing executes until a story is
   dispatched, and `dispatch_story` can be called directly over MCP without
   going through the scheduler tick.
3. **The plan is mutable.** `save_plan` overwrites, `ingest_plan` refreshes
   every authored story field on re-ingest, and `patch_story` edits a brief
   after the fact. An `approved: true` boolean on a plan name is a
   time-of-check/time-of-use hole.
4. **Authorization is a hard prerequisite.** An approval without a verified
   principal is theater. Today the MCP path has no caller identity at all
   (`mcp.run()` is stdio, `pipeline/server.py:992`), and the dashboard is a
   single shared secret for everyone (`app/auth.py`).

This record decides the gate's shape. The identity and topology work it depends
on is recorded in ADR 0008.

## Decision

**A plan has an explicit lifecycle, and only an approved plan is
dispatchable.**

1. **Lifecycle:** `draft → pending_approval → approved | changes_requested`.
   In enterprise mode `ingest_plan` materializes a plan in `pending_approval`;
   approval is a separate transition (`approve_plan` / `reject_plan`).
2. **Enforcement point:** the check lives in `PipelineService` and is applied
   in both `_advance_pipeline_locked_impl` and `_dispatch_story_impl`. Gating
   only the tick would let a direct `dispatch_story` call bypass it; this is the
   same rule ADR 0006 sets for ownership — enforce in the mutating surface, not
   only in routing.
3. **Content-bound approval:** approval records a digest of the authored plan
   content. Any mutation that changes authored content — `save_plan`,
   a re-ingest, `patch_story`, or a plan-file edit — resets the plan to
   `pending_approval` and requires re-approval. `patch_story` is the easy path
   to miss.
4. **Approval scope is per plan** for now. Finer-grained per-story or
   per-story-set approval is deferred; because approval is per plan, a re-ingest
   that adds stories must invalidate the approval rather than inherit it.
5. **Roles:** `author` (drafts), `owner` (accountable, and the routing target
   for `request_decision` per ADR 0006), `approver` (may activate), and
   `delegate` (may answer decisions). The owner is the author. A team lead may
   hold the `approver` role, and may also hold `delegate`.
6. **Self-approval is permitted** when the lead is also the author. Because of
   this, the gate is a *visibility and process* control, not a
   separation-of-duties control. It makes approval attributable and reviewable;
   it does not make self-approval impossible. That distinction is deliberate and
   must not be described otherwise.
7. **Every approval records** `{plan, digest, decided_by, at, role,
   self_approved, warnings_seen}` in the plan's append-only decisions log.
   Attribution requires a verified principal; until authentication exists no
   approval is a security control.
8. **The review artifact is the validated plan plus warnings,** not raw JSON.
   The ingest path already computes most of what a lead needs:
   `_story_sizing_warning`, `_preflight_status`,
   `_isolation_only_acceptance_warning`, `_security_review_unenforced_warning`,
   declared `files`, per-story `model`/`backend`/`risk`, and `role_config`.
   `warnings_seen` pins what the approver was shown.
9. **Fail closed:** a missing, malformed, or unrecognized status is not
   approved. Any error while evaluating the gate denies activation.

## Consequences

- The workflow matches what a team expects: an engineer ingests, a lead reviews,
  nothing runs until it is approved, and every approval is in the audit log.
- The gate is a plan-level state machine in the service layer, so it survives
  the move from a per-repo instance to a shared control plane (ADR 0008). It
  must not be implemented against `PLAN_DIR` directly, or it will be rebuilt.
- Costs: new lifecycle state and digest machinery; every authored-content
  mutation path must reset approval; a one-line brief edit forces re-approval
  unless a cheaper re-approval flow is designed later; self-approval remains
  possible by policy.
- Prerequisites, none of which exist today: a verified caller identity, an
  authorization/ownership model, and enforcement in `PipelineService` so the
  dashboard and chat adapters cannot bypass it.
- Not decided here: which principals may hold `approver` for which repos or
  teams (the scope model), the re-approval UX for small edits, and whether
  low-risk plans may auto-approve.
- A future contributor must not "simplify" this into an `approved` boolean on a
  plan name. The digest and reset rule are what make the gate meaningful.

Related: [0006](0006-decision-routing-on-a-shared-server.md),
[0008](0008-shared-control-plane-per-repo-workers.md).
