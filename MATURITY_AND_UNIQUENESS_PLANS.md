# Maturity & Uniqueness Plans

A self-assessment TODO list, not a specification. Two halves:

- **Plan A — Tighten maturity.** The gaps holding the project under 8/10.
- **Plan B — Close the uniqueness gaps.** Make it more scalable and feature-rich
  relative to the 2026 harness landscape.

Each item is a TODO with a *why* and a rough priority — no design detail yet.

---

## Baseline assessment (static review, 2026-07-17)

- **Maturity: ~7/10.** 1,120 tests (~2.5:1 test-to-source), 263 commits, real
  safety/audit/resumability, and an unusually honest failure-mode/ablation
  culture. Held back by config sprawl (~50 `PIPELINE_*` env vars, 74 KB
  README), several half-validated/uncommitted features still in flight, an
  active bug surface (Modes 18–20 found July 2026), and a single-user/personal
  machine bias. Not validated by a live run — static review + auto-memory only.
- **Uniqueness.** The *shape* (worktrees → review → merge, multi-backend,
  local LLM) is a 2026 commodity (Agent Orchestrator, Grove, VNX, SwarmWeaver,
  Agent Workspace Fabric, OpenHands, SWE-agent all do it). The distinctive
  thesis is the *combination*: MCP-native self-orchestration + a policy-driven
  overlord + Claude-spend-aware usage gating + acceptance-oracle grading +
  ablation honesty. Lags the field on sandboxing/remote-exec, model breadth,
  web UI, and adoption.

Reference comparables:
- Agent Orchestrator (8.3k★, Go/Electron, 23 worker harnesses)
- Grove (Rust, PRD/designer/builder/reviewer/judge, SQLite DAG)
- VNX Orchestration (Python, governance ledger, dual-LLM review, Ollama)
- SwarmWeaver, Agent Workspace Fabric (worktree + Docker + PR-monitor)
- OpenHands (16-feature enterprise SDK, LiteLLM, Docker, remote exec)
- SWE-agent (minimal ACI, academic)

---

## Plan A — Tighten maturity

### A1. Land the half-validated work (highest priority — biggest maturity tax)

- [x] **Finish the `pipeline_mcp_server.py` decomposition** (2026-07-19,
      `PIPELINE_MCP_DECOMPOSITION_PLAN.md`, PR #133). All extracted modules
      consolidated into a `pipeline/` package; `pipeline_mcp_server.py` is now
      a thin backward-compat shim. Also fixed a real bug found in the process:
      `ingest_plan` silently dropped a plan's `role_config` onto the manifest
      (PR #132), plus a `pipeline/__init__.py` name collision shadowing the
      `pipeline.checkpoint` submodule.
- [x] **Ship or kill guided decomposition** (2026-07-19/20, shipped narrow).
      H1 confirmed across ~18 benchmark trials plus n=4 real production
      stories (PR #132/134/135/136/137, decompose+TDD-split+gpt-oss:20b,
      tech-lead=Claude Sonnet), all eventually `APPROVE`+merged. **Caveat: not
      yet safe for unattended runs** — every one of the n=4 needed manual
      intervention for failure modes found in the same session (see
      `project_dispatch_failure_modes.md` Modes 22-24: `create_file`
      catastrophic forgetting, watchdog-checkpointed file deletion, reviewer
      re-approving an unaddressed REQUEST_CHANGES). The formal §4.5
      breadth-heavy-task criterion in `GUIDED_DECOMPOSITION_PLAN.md` was never
      written/run — this is a pragmatic ship call, not that plan's original
      bar. Modes 22-24 are the real next work, not further guided-decomposition
      validation.
- [ ] **Finish the MLX validate re-run.** Stopped at 5/9; `lru_cache_rs`
      gt=True-not-merged (memory pressure) still open. Re-verify, then commit
      the 3 fixes or record why they're parked.
- [ ] **Implement the reviewer-escalation plan** (L1 harness-enforced
      full-suite done-bar on CI-fail rework). `MERGE_CI_REWORK` is committed
      but the agent still ignores CI detail because its done-criterion is
      oracle-green — the rework path is half-wired.
- [ ] **Implement token-context optimization**, measuring cache hits first
      (per the plan's own caveat). Dead `PIPELINE_REVIEW_MAX_TOKENS`,
      duplicated uncached system prompts.
- [ ] **Decide the remaining plan docs' fate**
      (`MODEL_PROVIDER_ABSTRACTION` S3, `TICKETING_ABSTRACTION`,
      `CLAUDE_BACKEND_PROVIDER_ISOLATION` residual, `MODE_20_CORRECT_BUT_REJECTED`).
      Each is a committed-then-superseded retro or an open task — close the loop.

### A2. Shrink the config surface

- [ ] **Make `model_registry.json` the single source of truth**; demote the
      ~50 `PIPELINE_*` env vars to overrides-only. The priority chain
      (plan → env → registry → hardcoded) is documented but sprawling.
- [ ] **Deprecate/rename the alias traps** the memory already flags:
      `LOCAL_AGENT_MAX_STEPS` (transport-only no-op) vs
      `PIPELINE_LOCAL_MAX_STEPS` (real knob); `local` as permanent back-compat
      alias; per-provider tier overrides. Rename with a deprecation warning or
      document the trap loudly in one place.
- [ ] **Split the 74 KB README** into a quickstart + a reference doc. A new
      contributor currently reads 1,000 lines to do anything.

### A3. Stabilize the active bug surface

- [ ] **Bound the failure-mode discovery rate — trending the wrong way.**
      19 modes at this doc's 2026-07-17 baseline; now **24** (Modes 22-24
      found 2026-07-19/20, three new modes surfaced in a single session
      dispatching only 4 stories). Add a "no new modes for N benchmark runs"
      gate as a stability signal — not done, and the discovery rate argues
      this is more urgent than when first written.
- [x] **Fix the test-isolation leak** (2026-07-18, PR #131) —
      `load_oracle_module_with_env`/`load_module_with_env` mutating
      `os.environ` without cleanup; fixed with an `autouse` environ-snapshot
      fixture in both test files, plus a regression test pair.
- [ ] **Get CI to an enforced green baseline and tag a real release.** 263
      commits, no release tags — adoption starts with "what version."

### A4. Make it usable by someone who isn't the author

- [ ] **One-command install story** for a fresh, non-author clone (the
      `.venv-mlx`, launchd plists, and absolute-path assumptions are
      personal-machine baked-in).
- [ ] **Externalize per-machine assumptions** (24 GB Mac, Ollama/MLX
      coexistence, launchd) into documented requirements with graceful
      degradation when absent.
- [ ] **A getting-started end-to-end smoke** a stranger can run and see a
      story merge, on the all-Claude path (no local model required).

---

## Plan B — Close the uniqueness gaps

### B1. Execution isolation & portability (biggest feature gaps vs OpenHands/AWF)

- [ ] **Docker sandbox per worktree.** Agents currently run with full host
      access. Table stakes for any multi-user or untrusted-input story.
- [ ] **Remote execution backend** (a server mode, not just local subprocess).
      Lets dispatch/review run on a GPU box while the orchestrator stays local.
- [ ] **Abstract the agent runner.** Currently hardcoded to `claude -p` + a
      hand-rolled Ollama/MLX/LM Studio loop. A runner interface (Claude Code /
      Codex / Aider / Goose) enables the multi-harness breadth Agent
      Orchestrator has.

### B2. Model breadth

- [ ] **LiteLLM (or equivalent) adapter** for 100+ LLMs instead of
      per-provider drivers. The per-role registry is already the right shape;
      swap the driver layer behind it.
- [ ] **Multi-LLM routing** (cheap model for reads/orientation, strong model
      for edits/review). `auto` escalation is the seed; generalize it to a
      routing policy, not just local→Claude.

### B3. Scale & multi-tenancy

- [ ] **Multi-repo fleet as a first-class concept.** `advance_all_plans`
      exists but is plan-per-repo glued. A fleet manager with per-repo
      backends, quotas, and isolation is the scale story.
- [ ] **RBAC / multi-user.** Overlord park-and-ping is the kernel of an
      authorization model; extend it to actual users/roles for shared
      deployments.
- [ ] **Concurrency & queueing beyond `MAX_CONCURRENT_AGENTS`.** A real work
      queue with priorities, fair sharing across plans, and back-pressure.

### B4. Observability & control surfaces

- [ ] **Writable dashboard / control plane.** Current dashboard is read-only
      by design (good safety instinct). Add an explicit, audit-logged action
      surface (pause/resume/approve/reroute) so overlord decisions are
      reviewable and overridable from the UI.
- [ ] **Replay UI for failed runs.** The checkpoint journal + `review.log`
      already capture enough to reconstruct a run; surface it.
- [ ] **Stuck-agent / wedge detection** as a first-class health signal
      (OpenHands has this; staleness badges exist but no automated recovery).

### B5. Generalize & export the distinctive ideas (the real moat)

- [ ] **Package the overlord policy as a reusable spec** (3-tier risk + audit)
      so other harnesses can adopt it — the most portable idea, currently
      locked inside `overlord-policy.md`.
- [ ] **Publish the acceptance-oracle grading pattern** (don't grade the model
      on its own tests; scope gate to fixtures; reviewer as regression
      backstop) as a documented standalone module. FM-A is a real insight,
      currently buried in dispatch internals.
- [ ] **Standard benchmark integration** (SWE-bench / SWE-bench-Live)
      alongside the bespoke matrix. Enables comparison against OpenHands /
      SWE-agent numbers, not only internal cells.
- [ ] **MCP discoverability** — list on the MCP registry; ship a companion
      server exposing *only* the overlord + acceptance-oracle so other
      harnesses can adopt them piecemeal.

### B6. Ecosystem & community

- [ ] **Tag releases + changelog.** Conventional Commits already enables this
      (`release-please` / `cz`).
- [ ] **Contributor docs + the ADR pattern** VNX uses (19 ADRs). Plan docs are
      rich but internal; ADRs make decisions navigable to outsiders.
- [ ] **A public demo / writeup of the MCP-native inversion** (Claude Code
      driving its own pipeline) — the angle most likely to draw interest, and
      nobody else leads with it.

---

## Suggested ordering

A1 → A3 (fix-isolation) → A2 → B1 (sandbox) → B5 (export the moat) → B2 →
B4 → B6.

Land what's half-done before building new; export the distinctive ideas
before they get further buried; sandbox before any multi-user push.