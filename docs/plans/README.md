# Plans

This directory is the project's own planning archive: the design/fix plans
that were written, ingested into the pipeline, and (mostly) executed by it.
They're kept as historical record and design rationale, not as current specs
— for what actually shipped and what broke along the way, see
[`retros/`](../../retros/INDEX.md) and the root
[`docs/failure_modes.json`](../failure_modes.json) catalog. Several of these
plans predate conventions described elsewhere in the repo and may reference
personal paths or superseded designs; read them as history, not instruction.

Rough groupings, newest thinking first:

**Provider / backend abstraction** — how the pipeline decoupled from any one
model provider: `Local_LLM_Port_Plan.md`, `Port_to_local_LLM.md`,
`MODEL_PROVIDER_ABSTRACTION_PLAN.md`, `CLAUDE_BACKEND_PROVIDER_ISOLATION_PLAN.md`,
`MLX_DEFAULT_PROVIDER_PLAN.md`, `MLX_TOOL_FORMAT_SUPPORT_PLAN.md`,
`TICKETING_ABSTRACTION_PLAN.md`, `PLATFORM_DECOUPLING_AND_SCALE_PLAN.md`.

**Dispatch quality, TDD, and reliability** — the gates that make cheap local
dispatch trustworthy: `ALWAYS_ON_CHECKLIST_AND_TDD_SPLIT_PLAN.md`,
`TDD_SPLIT_PRODUCTION_PLAN.md`, `GUIDED_DECOMPOSITION_PLAN.md`,
`RELIABILITY_PLAN.md`, `MODE_20_CORRECT_BUT_REJECTED_PLAN.md`,
`REVIEWER_ESCALATION_PLAN.md`, `OVERLORD_FAILURE_TRIAGE_PLAN.md`,
`OVERLORD_PARKED_STORY_AUTONOMY_PLAN.md`,
`SCHEDULER_RECONCILE_RESILIENCE_PLAN.md`, `MERGE_CI_REWORK_PLAN.md`,
`AUTONOMY_GAP_CLOSURE_PLAN.md`.

**Dashboard / workspace** — `DASHBOARD_STORY_PROGRESS_PLAN.md`,
`workspace-selection.json`.

**Architecture / decomposition of the pipeline itself** —
`PIPELINE_MCP_DECOMPOSITION_PLAN.md`, `EVENT_DRIVEN_PHASE2_PLAN.md`,
`server-app-file-split.json`.

**Cost / performance** — `TOKEN_CONTEXT_OPTIMIZATION_PLAN.md`.

**Process** — `PLAN_RETROSPECTIVE_PROCESS_PLAN.md` (the retro process
`retros/INDEX.md` is part of).

**Analysis / retrospective summaries** — `Production_System_analysis.md`,
`MATURITY_AND_UNIQUENESS_PLANS.md`, `REAL_REPO_INTEGRATION_TEST_PLAN.md`.
