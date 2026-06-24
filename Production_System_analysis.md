# ⚙️ Autonomous SDLC Agent Pipeline: Architectural Review Summary

This document provides a comprehensive review of the conceptual and implemented architecture for the Autonomous SDLC Agent Pipeline. The system is an advanced, highly resilient orchestration layer designed to safely and efficiently bridge the gap between goal definition and software delivery using probabilistic LLM agents.

---

> ### 📝 Currency review — 2026-06-24
>
> This analysis was re-validated against the current codebase. Every function it
> names (`_scoped_repo_root`, `_plan_lock`, `_count_in_progress_agents`,
> `_merge_pr`, `checkpoint`, `request_decision`, the usage-gate hysteresis) still
> exists, and the operational flow it describes is faithful. Two qualifications:
>
> 1. **It predates the multi-backend work.** The doc frames resource control as a
>    Claude-only "Usage Gate." The pipeline now routes dispatch / review / overlord
>    independently to a `claude` or local **Ollama** backend, and the gate is
>    **per-backend** (`_role_resource_ok`): a local role has no usage limit and keeps
>    running while Claude's window is maxed. The "Usage Gate" strength below is now a
>    special case of a broader, more robust design — the doc *understates* current resilience.
> 2. **Suggested Next Step #3 (error budgeting) has since been implemented** for the
>    merge step — see the annotation in that section.

---

## 🎯 High-Level Goal & Functionality

The pipeline transforms a high-level software goal into executable tasks, manages the lifecycle of those tasks across isolated environments (git worktrees), and uses a multi-layered gating system to ensure human supervision is only required when the risk profile dictates it.

### Key Operational Flow
**Plan $\rightarrow$ Ingest (Plane) $\xrightarrow{\text{Dependencies}}$ Ready Story $\xrightarrow{\text{Dispatch}}$ Agent Worktree $\xrightarrow{\text{Checkpoint/Tasking}} \text{Tests Pass} \rightarrow \text{Review} \xrightarrow{\text{Overlord/Risk Threshold}} \text{Merge/Park}$**

---

## ✅ Architectural Strengths (The Successes)

The design excels in its mitigation of common autonomous system risks: cost overrun, uncontrolled execution, and lack of auditability.

### 🛡️ Safety & Governance
*   **Risk-Based Gating:** The combination of the **Overlord Policy**, `PIPELINE_RISK_THRESHOLD`, and decision tiers (`Routine`/`Notify-async`/`Park-and-ping`) ensures that the system is rarely in an unsupervised high-risk state. High-risk actions are always blocked by design.
*   **Immutable Audit Trail:** Every critical decision (`request_decision`) is logged in a tamper-proof audit file, satisfying compliance and reproducibility requirements.
*   **Mainline Protection:** The architecture mandates a strict separation between the agent's work (isolated git worktrees) and the target repository, with the `code-reviewer` acting as the final quality gatekeeper.

### 🧠 Resilience & Efficiency
*   **Transactional Checkpointing:** The most sophisticated element. By requiring agents to explicitly call `checkpoint()` after every meaningful step, the system treats agent work as a series of small, durable commits. This allows for safe interruption (due to the Usage Gate) and seamless resumption, virtually eliminating wasted compute time.
*   **Fault Tolerance in Tick:** The `advance_pipeline` orchestrator is designed to be idempotent. If it crashes or overlaps (`_plan_lock`), it correctly determines the next state without racing agents into identical, broken attempts.
*   **Isolation (`_scoped_repo_root`):** The implementation of `REPO_ROOT` scoping via `_scoped_repo_root` is a masterful solution to the physical reality of how a single running process manages multiple, unrelated git repositories. It ensures that global environment variables don't accidentally point to the wrong repository context during a tick.

### 💰 Resource Management
*   **Usage Gate:** This external control loop is a crucial business feature. By introducing hysteresis (`PAUSE_THRESHOLD`/`RESUME_THRESHOLD`) and strictly isolating the cost-monitoring from the action logic, you prevent transient usage bursts from leading to a hard freeze.
*   **Concurrency Control:** The `MAX_CONCURRENT_AGENTS` cap and the `_count_in_progress_agents` function are critical race condition mitigators. They prevent the scheduler from overwhelming GitHub/API limits or incurring unnecessary cost by dispatching too many agents simultaneously.

---

## ⚠️ Technical Nuances & Areas of Focus (The Complexity)

These points are not flaws, but rather areas where the system's complexity intersects with its operational limits.

| Area | Technical Detail | Implication / Risk Profile |
| :--- | :--- | :--- |
| **LLM Boundary** | Agents commit code/steps, but the system relies on the agent correctly calling `checkpoint()` and accurately labeling the work. | **The reliability of the checkpointing flow depends entirely on prompt adherence from the agent.** |
| **External Dependency** | The pipeline has complex fallbacks (e.g., `_repo_root` fallback, branch detection). | **These fallbacks add fragility.** While safety-net worthy, they should be logged as potential sources of unpredictable behavior in production. |
| **Process Lifetime** | `check_story_status` correctly distinguishes between a truly "failed" commit and an agent that simply didn't finish (and is thus `interrupted`). | This rigorous distinction adds complexity to the state management but is necessary for optimal recovery. |
| **Scaling** | The `advance_all_plans()` feature is powerful, allowing mass parallelization across different repos. | It introduces global concurrency management challenges (`_plan_lock` only governs a single plan per call). This is highly robust but requires careful orchestration. |

---

## ⏭️ Suggested Next Steps (Moving from Prototype to Product)

Given the exceptional level of detail, the next steps should focus on tuning behavior based on operational metrics rather than fixing structural bugs.

1.  **Operational Telemetry:** Define clear logging standards around *why* a decision was reached (`Why was it parked?`, `Why did the usage gate trip?`).
    - *2026-06-24 status: **partially done.*** Park reasons are structured
      (`_merge_decision` returns a `reason`), gate trips carry a `reason` string, and
      `_notify_user` durably logs to `<plan>.notifications.log`. Still open: structured
      *decision rationale* telemetry distinct from human-facing notifications.
2.  **Agent-Prompt Tightening:** Since the checkpoints rely on prompt instructions, focus efforts on making the `checkpoint()` instruction absolutely inviolable in the prompt template.
    - *2026-06-24 status: **still relevant, now broader.*** The local dispatch loop
      (`scripts/local_agent.py`) has its own commit-enforcement / loop-guard prompt
      surface, and weaker local models make adherence harder than with Claude — so this
      matters more than when first written.
3.  **Error Budgeting:** Define acceptable tolerance thresholds for transient errors (e.g., how many times can `_merge_pr` fail before it becomes a critical failure that requires human intervention).
    - *2026-06-24 status: **done across the merge, dispatch, and Plane boundaries.***
      - **Merge:** a failing `_merge_pr` no longer crashes the tick; the story stays
        `pr_open` and is retried up to `PIPELINE_MERGE_MAX_ATTEMPTS` (default 3), then
        marked `failed` for human intervention. `approve_merge` returns a structured
        error instead of raising.
      - **Dispatch:** a launch that keeps failing — a raising `dispatch_story` or an agent
        that produces no output — bumps a per-story `dispatch_attempts` counter and is
        retried up to `PIPELINE_DISPATCH_MAX_ATTEMPTS` before becoming a terminal `failed`,
        rather than looping forever as a redispatch-eligible `interrupted`. Legitimate
        usage-gate interrupts never count against it.
      - **Plane:** `_plane_set_state` retries a transient transition up to
        `PIPELINE_PLANE_MAX_ATTEMPTS` inline (Plane sync is a side effect of work already
        committed in git), then records the drop durably via `_notify_user` instead of a
        silent `print`.

