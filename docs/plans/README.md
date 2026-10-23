# Plans

This directory is the project's own planning archive: the design and fix plans
that were written, ingested into the pipeline, and (mostly) executed by it.
Finished plans are kept as history and design rationale, not as current specs.
For what actually shipped and what broke along the way, see
[`retros/`](../../retros/INDEX.md) and the
[`docs/failure_modes.json`](../failure_modes.json) catalog. Older plans may
reference superseded designs or file layouts; read them as history, not
instruction.

**Status below was verified 2026-10-22** against the pipeline manifests and the
code. A plan's own header can lag behind; where it did, a dated
`Status (verified ...)` banner now sits under its title. When you finish a plan,
update this index in the same change.

## Open backlog, in priority order

| # | Plan | State | Why this position | Next step |
|---|---|---|---|---|
| 1 | [`EXTERNAL_CONTEXT_SOURCES_PLAN.md`](EXTERNAL_CONTEXT_SOURCES_PLAN.md) | Drafted, held | Largest unstarted feature, and the only Wave 1 hold still waiting. | Finish the preflight impact runs for CL-14/15/21/22/23/24 against current master, then ingest. |
| 2 | [`NOTIFICATION_CHANNELS_PLAN.md`](NOTIFICATION_CHANNELS_PLAN.md) | Drafted, not decomposed | Slack/Teams delivery. The outbox and injected-sender seams already exist, so this is mostly new modules with low blast radius. | Settle the open questions (one generic webhook sender vs. one per channel), then decompose. |
| 3 | [`FORGE_ABSTRACTION_PLAN.md`](FORGE_ABSTRACTION_PLAN.md) | Drafted, not started | GitLab/Bitbucket support for the PR/CI/merge loop. Large and touches the merge gate; worth it only once a non-GitHub user exists. | Re-verify file references, then decompose. |
| 4 | [`JIRA_TICKET_PROVIDER_PLAN.md`](JIRA_TICKET_PROVIDER_PLAN.md) | Drafted, blocked | Replaces the `JiraTicketProvider` stub. Its Definition of Done needs a live Jira Cloud instance and token. | Get a test Jira instance. |
| 5 | [`../specs/PIP_INSTALLABILITY_DESIGN.md`](../specs/PIP_INSTALLABILITY_DESIGN.md) | Decision pending | `pyproject.toml` still has no `[project]` table. `scripts/remote-install.sh` covers the one-line install; pip/uvx matters only for directories that require it. | Decide whether any target listing needs pip/uvx. |

**Decision needed (not a build item):**
[`SHARED_SERVICE_TOPOLOGY_PLAN.md`](SHARED_SERVICE_TOPOLOGY_PLAN.md) recommends
option B (several sessions on one shared host) now and defers T3 (hosted,
multi-tenant) until someone can say who answers a decision on a plan they did
not create. Option B's prerequisites (the latent hazards it names) shipped in
`TOPOLOGY_COLLISION_GUARDS_PLAN.md`, so accepting B needs no further build.

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
(`pipeline-auto-merge-off`).

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
(`check_story_status` returns a result for finished stories and unknown plans).

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
