# Plans

This directory is the project's own planning archive: the design and fix plans
that were written, ingested into the pipeline, and (mostly) executed by it.
Finished plans are kept as history and design rationale, not as current specs.
For what actually shipped and what broke along the way, see
[`retros/`](../../retros/INDEX.md) and the
[`docs/failure_modes.json`](../failure_modes.json) catalog. Older plans may
reference superseded designs or file layouts; read them as history, not
instruction.

**Status below was verified 2026-10-29** against the pipeline manifests and the
code. A plan's own header can lag behind; where it did, a dated
`Status (verified ...)` banner now sits under its title. When you finish a plan,
update this index in the same change.

## Open backlog, in priority order

| # | Plan | State | Why this position | Next step |
|---|---|---|---|---|
| 1 | [`JIRA_TICKET_PROVIDER_PLAN.md`](JIRA_TICKET_PROVIDER_PLAN.md) | Drafted, blocked | Replaces the `JiraTicketProvider` stub. Its Definition of Done needs a live Jira Cloud instance and token. | Get a test Jira instance. |
| 2 | Smoke stack to green (no plan yet) | Open | The compose stack in `deploy/` builds and every container comes up healthy on a Linux engine, but `deploy/smoke/smoke.sh` has never passed: the CI job is gated off (`ENABLE_SMOKE_CI`), and a local run on 2026-10-29 got through `ingest`, `approve` and `dispatch` and then stalled before `reviewed`. The defects are listed in [`ENTERPRISE_REMAINING_WORK_PLAN.md`](ENTERPRISE_REMAINING_WORK_PLAN.md#outcome-verified-2026-10-29). | Plan and fix them, then set `ENABLE_SMOKE_CI=true`. |
| 3 | [`../specs/PIP_INSTALLABILITY_DESIGN.md`](../specs/PIP_INSTALLABILITY_DESIGN.md) | Decision pending | `pyproject.toml` still has no `[project]` table. `scripts/remote-install.sh` covers the one-line install; pip/uvx matters only for directories that require it. | Decide whether any target listing needs pip/uvx. |

**Enterprise platform readiness (assessed 2026-10-25):**
[`ENTERPRISE_PLATFORM_READINESS.md`](ENTERPRISE_PLATFORM_READINESS.md) records
the eventing, observability, auditability, and authn/authz findings. Three plans
came out of it and all shipped 2026-10-26: the behavior-preserving
`enterprise-prep-tier1` (3 stories), `enterprise-plan-approval` (7 stories, ADR
0007), and `tenancy-seams` (5 stories — the ADR 0009 identity/authorization
seam only, spec `docs/specs/IDENTITY_AND_POLICY.md`). The findings are now being
worked in dependency order: actor identity, request correlation, structured
logs and policy enforcement shipped 2026-10-26/27, and the tamper-evident audit
stream shipped 2026-10-27, and Phase 6 (OIDC verification and the remote MCP
transport) shipped 2026-10-27. The rest of ADR 0008 stage 1, Phase 6
hardening, observability, the NATS broker, stage 2a (`SqliteStore`), the
compose smoke stack and ADR 0011 stages S0–S1 shipped 2026-10-28/29 as the
eleven plans indexed in
[`ENTERPRISE_REMAINING_WORK_PLAN.md`](ENTERPRISE_REMAINING_WORK_PLAN.md) plus
[`SCHEDULER_EVENT_DRIVEN_PLAN.json`](SCHEDULER_EVENT_DRIVEN_PLAN.json). The
smoke run itself is not green yet (backlog item 2).

**Decision needed (not a build item):**
[`SHARED_SERVICE_TOPOLOGY_PLAN.md`](SHARED_SERVICE_TOPOLOGY_PLAN.md) recommends
option B (several sessions on one shared host) now and defers T3 (hosted,
multi-tenant) until someone can say who answers a decision on a plan they did
not create. Option B's prerequisites (the latent hazards it names) shipped in
`TOPOLOGY_COLLISION_GUARDS_PLAN.md`, so accepting B needs no further build.
**Update 2026-10-25:** the *direction* for T3 is now recorded in
[ADR 0008](../adr/0008-shared-control-plane-per-repo-workers.md) (Accepted 2026-10-28): a
shared control plane with per-repo workers, federated per trust boundary. What
remains is the tenancy/identity work, not the topology choice.

**Parked (revisit only if the trigger happens):**

- `DASHBOARD_STORY_PROGRESS_PLAN.md` Tier 2: step-budget badge for stories
  without a checklist. Nice to have.
- `MLX_TOOL_FORMAT_SUPPORT_PLAN.md`: Mistral/Qwen3-Coder tool-call plumbing on
  MLX. Matters only if MLX dispatch is revived; dispatch is on Ollama and Claude.
- W4 / T3 multi-tenancy: the remaining workstream of
  `PLATFORM_DECOUPLING_AND_SCALE_PLAN.md` and `MATURITY_AND_UNIQUENESS_PLANS.md`
  B3 (fleet, RBAC, real work queue). Blocked on the topology decision above.

**Ongoing goal, not a plan:** `LOCAL_DISPATCH_90_PLAN.md` targets ≥90%
first-pass-clean on non-Claude tiers. Its stories shipped (`local-dispatch-90-t1`,
`ld90-closeout`), but the target is unmet: the 2026-10-18 window retro measured
cloud open-weight at 88.4% and the on-device tier at 0/9. Track it through the
window retros.

## Finished plans (history)

Each of these is fully merged. The pipeline plan name is given where it differs
from the file name.

**Enterprise readiness (2026-10-28/29)**: `ENTERPRISE_REMAINING_WORK_PLAN.md`
(the index of the eleven plans below), `ENTERPRISE_ENFORCEMENT_CLOSEOUT_PLAN.json`,
`DECISION_ROUTING_PLAN.json`, `SYSTEM_PRINCIPALS_PLAN.json`,
`AUDIT_ANCHOR_PLAN.json`, `REMOTE_MCP_HARDENING_PLAN.json`
(`remote-mcp-hardening-2`), `DASHBOARD_OIDC_LOGIN_PLAN.json`,
`OBSERVABILITY_OTEL_PLAN.json`, `EVENTING_BROKER_PREP_PLAN.json`,
`EVENTING_BROKER_ADAPTER_PLAN.json`, `CONTROL_PLANE_STORE_PLAN.json`,
`SMOKE_STACK_PLAN.json` (merged, but its smoke run is not green: backlog item
2), `SCHEDULER_THROUGHPUT_PLAN.json` (ADR 0011 S0) and
`SCHEDULER_EVENT_DRIVEN_PLAN.json` (ADR 0011 S1).

**Installation and onboarding**: `GUIDED_INSTALL_PLAN.md` (`guided-install`),
`WAVE1_RETROSPECTIVE.md` (measured retro for auto-merge-off, sidebar settings
and guided install).

**Provider and backend abstraction**: `Port_to_local_LLM.md`,
`Local_LLM_Port_Plan.md`, `MODEL_PROVIDER_ABSTRACTION_PLAN.md`,
`CLAUDE_BACKEND_PROVIDER_ISOLATION_PLAN.md`, `MLX_DEFAULT_PROVIDER_PLAN.md`
(MLX was validated, then superseded as the default by Ollama),
`TICKETING_ABSTRACTION_PLAN.md` (its Jira follow-up is backlog item 6),
`PLATFORM_DECOUPLING_AND_SCALE_PLAN.md` (W1-W3 done; W4 parked).

**Dispatch quality, TDD and reliability**:
`ALWAYS_ON_CHECKLIST_AND_TDD_SPLIT_PLAN.md`, `TDD_SPLIT_PRODUCTION_PLAN.md`
and `.json`, `GUIDED_DECOMPOSITION_PLAN.md`, `RELIABILITY_PLAN.md`,
`MODE_20_CORRECT_BUT_REJECTED_PLAN.md`, `REVIEWER_ESCALATION_PLAN.md`
(`reviewer-escalation-l2l3`), `MERGE_CI_REWORK_PLAN.md`,
`AUTONOMY_GAP_CLOSURE_PLAN.md`, `LOCAL_DISPATCH_90_PLAN.md`,
`LD90_CLOSEOUT_PLAN.json`, `POST_LD90_PRIORITIES_PLAN.json`,
`POST_LD90_ITEMS_5_6_PLAN.md`, `POST_RLF_HARDENING_PLAN.json`,
`DETACHED_HEAD_GUARD_PLAN.json`, `PR_TITLE_SYNC_PLAN.json`.

**Overlord and scheduler**: `OVERLORD_FAILURE_TRIAGE_PLAN.md`,
`OVERLORD_PARKED_STORY_AUTONOMY_PLAN.md`,
`SCHEDULER_RECONCILE_RESILIENCE_PLAN.md`, `AUTO_MERGE_OFF_SWITCH_PLAN.md`
(`pipeline-auto-merge-off`), `NOTIFICATION_CHANNELS_PLAN.md`
(`notification-channels`; pluggable channel types routed per team).

**Dashboard and workspace**: `DASHBOARD_STORY_PROGRESS_PLAN.md` (Tiers 0-1;
Tier 2 parked), `DASHBOARD_SIDEBAR_SETTINGS_PLAN.md`,
`DASHBOARD_WORKTREE_DIFF_PLAN.md`, `workspace-selection.json`,
`chat-claude-md-leak.json`, `WAP_PARKED_PR_REVIEW.md` (PRs #760/#761 since
merged; follow-ups in `wap-review-followups`).

**Architecture of the pipeline itself**: `PIPELINE_MCP_DECOMPOSITION_PLAN.md`,
`EVENT_DRIVEN_PHASE2_PLAN.md` (`event-driven-pipeline-phase2` and `-phase3`),
`server-app-file-split.json`, `OVERSIZED_MODULE_SPLIT_PLAN.json`,
`PIPELINE_COLD_IMPORTS_PLAN.json`, `PIPELINE_COLD_IMPORTS_PHASE2_PLAN.json`,
`TOPOLOGY_COLLISION_GUARDS_PLAN.md` (story-key uniqueness at ingest and
dispatch, checked merge fetch), `CHECK_STORY_STATUS_FIX_PLAN.md`
(`check_story_status` returns a result for finished stories and unknown plans),
`PARKED_STORY_RECOVERY_PLAN.md` (`parked-story-recovery`: triage
`amend_scope`, the rebase-conflict feedback brief, scope-aware re-review),
`HARNESS_RESILIENCE_PLAN.md` (`harness-resilience`: usage-gate announcements,
the Claude dispatch cooldown, capacity waits, and the stall scan),
`LOCAL_FLEET_BRANCH_SAFETY_PLAN.md` (branch safety for several agents on one
host; ADR 0006).

**Enterprise readiness**: `enterprise-prep-tier1` (repo-root allowlist,
worktree namespace, topology in health; all opt-in and default-preserving),
`enterprise-plan-approval` (the ADR 0007 gate — state and content digest at
ingest, `approve_plan`/`reject_plan`, reset on authored change, enforcement in
the tick and direct dispatch, spec `docs/specs/PLAN_APPROVAL.md`), and
`tenancy-seams` (the ADR 0009 `Principal` / `IdentityProvider` /
`SharedSecretProvider` / deny-by-default `Authorizer` seam, attached to
`request.state.principal` by the dashboard middleware but enforcing nothing yet
— spec `docs/specs/IDENTITY_AND_POLICY.md`).

The readiness phases that followed, in dependency order: `actor-identity`
(request-scoped `Principal` propagation, a `team` on the manifest, and a guard
test that no MCP tool may take an actor parameter), `request-correlation` (one
id per dashboard request, minted or preserved, returned on every response
including 401/403 and stamped on decision and approval records),
`structured-logs` (opt-in `PIPELINE_LOG_FORMAT=json` rendering the standard
fields plus every `extra=` key), `policy-enforcement` (deny-by-default
enforcement for `story:merge`, `story:mark_done`, `plan:approve`,
`plan:reject` and `plan:pause`, on by default with
`PIPELINE_POLICY_ENFORCE` as the kill switch), and `audit-stream` (the
append-only hash-chained `<plan>.audit.jsonl`, its `verify_audit_chain` check,
and eight call sites from authorization through merge and pause/resume).
`dispatch-stale-base` and `stale-base-probe` belong to the same push: a skipped
fetch is recorded as `stale_base_dispatch`, and a base proven stale by
`git ls-remote` now refuses the dispatch instead of branching from it.
`stale-probe-robustness` followed once CI caught the probe crashing on a
recorder-stubbed `subprocess.run`.

Phase 6 shipped in two parts. `oidc-identity` adds JWKS-backed token
verification (`app/oidc.py`): a fixed RS256/ES256 allowlist, required
`exp`/`iss`/`aud`/`sub`, configured issuer and audience, a cached and
address-checked JWKS fetch, and an issuer-qualified `<issuer>#<sub>` subject;
`PIPELINE_IDENTITY_PROVIDER` selects it and raises rather than falling back,
`PIPELINE_OIDC_ALLOW_PRIVATE_JWKS` is the opt-in for a self-hosted IdP, and the
dashboard authenticates bearer tokens only when that provider is OIDC.
`remote-mcp` adds the network transport: a pure-ASGI bearer gate and transport
selection that refuses to start an HTTP transport without OIDC. Both carry the
same documented boundary — OIDC authenticates API clients, not the bundled
browser UI.

**Code host and CI**: `FORGE_ABSTRACTION_PLAN.md` (`forge-abstraction`; a
`Forge` protocol with GitHub and GitLab adapters behind `PIPELINE_FORGE`, so
the PR/CI/merge loop no longer assumes GitHub).

**Context sources**: `EXTERNAL_CONTEXT_SOURCES_PLAN.md`
(`external-context-sources-phase1` and `-phase2`; governed reference material
pinned by digest, ADR 0005).

**Reporting and metrics**: `POST_LD90_REPORTING_FIX_PLAN.json`,
`REPORTING_LAYER_FOLLOWUPS_PLAN.json`.

**Cost and performance**: `TOKEN_CONTEXT_OPTIMIZATION_PLAN.md` (closed
2026-08-31; prompt caching turned out to be automatic already).

**Process**: `PLAN_RETROSPECTIVE_PROCESS_PLAN.md` (the retro process that
`retros/INDEX.md` and `retros/PENDING.md` belong to).

**Testing infrastructure**: `REAL_REPO_INTEGRATION_TEST_PLAN.md`.

**Analysis and strategy**: `Production_System_analysis.md`,
`MATURITY_AND_UNIQUENESS_PLANS.md` (everything shipped except B3's fleet, RBAC
and queueing items, which are parked with W4).
