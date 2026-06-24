# 🧠 Porting the Orchestrator: From Claude MCP to Local LLM

The current codebase is not a simple script; it is a **highly sophisticated Orchestration Layer**. Our goal in porting to a local LLM stack (e.g., Ollama, vLLM) is not to rewrite the brain, but to replace the bindings. We preserve the decision logic and use a new Executor to drive the LLM calls.

---

## 🎯 The Conceptual Goal: Decoupling the System
The migration is about separating **WHY** (the Orchestration, Gating, and Decision Logic) from **HOW** (the specific LLM API calls). Your existing code owns the "Why"—it is the Intellectual Property of this architecture.

### 🔄 Role Mapping: From Claude to Local LLM
The existing components become conceptual roles that remain relevant regardless of the backend LLM.

| Current Claude MCP Component | Role / Function | Local LLM Alternative / Action Required |
| :--- | :--- | :--- |
| `claude -p` (Headless Agents) | The execution of the LLM thought process; generating code/text. | **Local LLM API Call:** The Orchestrator sends the prompt to your local endpoint (`http://localhost:port`). |
| `persona` + `model` | The system prompt and model selection. | **System Prompt Engineering:** This is translated into a detailed, static `system_prompt` passed with the API call. The model choice maps to the local LLM you select for optimal performance (e.g., `sonnet` $\rightarrow$ "Mixtral 8x7B"). |
| `_build_dispatch_command` | Translating story data into a runnable agent prompt. | **Prompt Templating Engine:** This function becomes the engine that injects structured context (journal entries, instructions) into the prompt template. |
| `subprocess` Calls (Git/GH) | The physical execution of code and SCM operations. | **Dedicated Shell Executor:** This remains a `subprocess` call, executed by the Orchestrator. The LLM should only *suggest* the change; the script executes it. |
| `Usage Gate` | Tracking cost/usage via Claude's internal usage metrics. | **Local Resource Monitor:** This tracks local compute resources (VRAM, CPU cycles) or token usage to manage your own financial/hardware cost. |

---

## 🛠️ Implementation Roadmap: The Migration Action Plan

The migration should follow a three-step approach to minimize risk and maximize reuse of your existing controls.

### STEP 1: Abstract the LLM Interaction (The Refactoring)
*   **Action:** Introduce a new `LLM_Executor` class/module.
*   **Focus:** This layer completely wraps the proprietary vendor calls (`claude -p`). It becomes a black box to the Orchestrator, responding only via simple commands like `generate_response(prompt)` or `adjudicate(question)`.
*   **Goal:** Achieve perfect functional parity between the old and new execution backends.

### STEP 2: Reinforce the Orchestrator (The Preservation)
*   **Do Not Touch:** The core orchestration functions (`advance_pipeline`, `interrupt_story`, `_merge_decision`) are correct and should be preserved. They manage the *state machine* of the project, which is independent of whether the brain runs on Claude or Llama.
*   **Focus:** Ensure your manifests, journals, and persistence logic (the `manifest.json` and `journal.json`) remain the single source of truth across both versions.

### STEP 3: Localizing the "Brain" (The Engineering)
*   **Challenge:** The local LLM is a constraint. It may not have the vast, seamless context of an Opus-class model.
*   **Solution:** Use highly efficient **Prompt Templating**. Your prompts must be surgically precise, requiring the LLM to structure its output using tags (`RULING:`, `TIER:`) so that the Orchestrator can reliably parse it.

---

## 🚀 Summary of Code Transition Targets

The following actions summarize the engineering work required during the porting process:

| Current Code Dependency | Local LLM Translation / Action | Why This Matters |
| :--- | :--- | :--- |
| `claude -p` | $\rightarrow$ **Local LLM API Call** | Replaces the vendor overhead with a direct endpoint call. |
| `_persona_body` | $\rightarrow$ **Static System Prompt** | Moves the persona instructions into the system context, making them hyper-optimized for the local model. |
| `_build_dispatch_command` | $\rightarrow$ **Prompt Templating Layer** | This function adapts the execution commands to fit the local LLM's token limits and context window. |
| `check_usage()` | $\rightarrow$ **Local Resource Monitor** | Shifts the focus from billing usage to compute resource management (VRAM, CPU time). |

By following this blueprint, you successfully transition from being a *vendor-specific solution* to possessing a truly **hardware-agnostic, self-contained autonomous control system.**

