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
- [x] **Implement the reviewer-escalation plan L1** (2026-07-22/23, Mode 40
      series — PRs #166/#168/#171/#172). L1 shipped: `_ci_status` now
      populates the failing-check names, the merge gate synthesizes
      CI-fail-specific rework feedback, `detect_lint_command` is wired into
      `check_story_status`'s test gate, and the merge-gate CI-fail rework
      helper has an integration test. The agent's done-criterion is no longer
      purely oracle-green on CI-fail rework. **L2/L3 (reviewer-escalation
      tiers above L1) remain unimplemented** — see
      `REVIEWER_ESCALATION_PLAN.md`.
- [ ] **Implement token-context optimization**, measuring cache hits first
      (per the plan's own caveat). Dead `PIPELINE_REVIEW_MAX_TOKENS`,
      duplicated uncached system prompts.
- [x] **Decide the remaining plan docs' fate** (2026-07-30). All four —
      `MODEL_PROVIDER_ABSTRACTION_PLAN.md`, `TICKETING_ABSTRACTION_PLAN.md`,
      `MODE_20_CORRECT_BUT_REJECTED_PLAN.md`, and
      `CLAUDE_BACKEND_PROVIDER_ISOLATION_PLAN.md` — turned out to be fully
      shipped already; each is now marked **PLAN CLOSED**.
      `CLAUDE_BACKEND_PROVIDER_ISOLATION_PLAN.md` was the interesting case: its
      header still said **"Mode: Not yet started"**, but `backend.py` already
      has `_first_party_claude_env()`, `ProviderIdentityMismatch`, and
      `verify_identity()` fully implemented and tested (T1-T4), documented in
      `REFERENCE.md` (T5) — the header was simply never updated after the
      work landed. Verified against the live code, not the doc text, before
      concluding this — worth flagging as its own small lesson: **this doc
      corpus has real staleness risk in the opposite direction from usual**
      (docs claiming *less* progress than the code has, not more), likely
      because implementers merge real fixes without circling back to update
      the planning doc that spawned them.

### A2. Shrink the config surface

- [ ] **Make `model_registry.json` the single source of truth**; demote the
      ~50 `PIPELINE_*` env vars to overrides-only. The priority chain
      (plan → env → registry → hardcoded) is documented but sprawling.
- [~] **Deprecate/rename the alias traps** the memory already flags:
      `LOCAL_AGENT_MAX_STEPS` (transport-only no-op) vs
      `PIPELINE_LOCAL_MAX_STEPS` (real knob); `local` as permanent back-compat
      alias; per-provider tier overrides. **Partial (2026-07-27, PR #174):
      `backend.py` now emits a one-time module-load `logging.warning` naming
      both the wrong `LOCAL_AGENT_*` var and the correct `PIPELINE_LOCAL_*`
      one whenever any of the three transport-only vars is set** — the
      "document the trap loudly in one place" half. The actual rename/deprecation
      (removing the alias, not just warning) is still open.
- [x] **Split the 74 KB README** into a quickstart + a reference doc
      (2026-07-27, `5f7d811`). README.md is now a 126-line quickstart;
      REFERENCE.md (919 lines) holds the moved reference material.

### A3. Stabilize the active bug surface

- [ ] **Bound the failure-mode discovery rate — trending the wrong way.**
      19 modes at this doc's 2026-07-17 baseline; now **32** (Modes 22-24
      found 2026-07-19/20; Mode 28 found 2026-07-21 shipping the always-on
      TDD-split story — see `retros/tdd-split-always-on_2026-07-21.md`; Modes
      29-30 found the same day fixing TDD-split's own opt-in gap — see
      `retros/tdd-split-unconditional-and-review-race_2026-07-21.md`; Mode 30
      is a scheduler-vs-manual-git race that corrupted a source file on
      disk, the most severe of the three; Modes 31-32 found 2026-07-22
      dispatching the Mode 24/28 fix itself — see `project_dispatch_failure_modes.md`
      for full writeups). Add a "no new modes for N benchmark runs" gate as
      a stability signal — not done, and the discovery rate argues this is
      more urgent than when first written. **Update 2026-07-27: the count
      has plateaued at 32 — no new modes since 2026-07-22 — but no gate
      enforces it, so this is luck rather than a measured signal. Update
      2026-07-29: the plateau broke — Mode 42 (2026-07-24) and Mode 43
      (2026-07-29, below) both found live, count now 33. Neither was found
      by a benchmark run; both came from dispatching real maturity-plan
      stories, which argues the "no new modes for N benchmark runs" gate
      as originally scoped would not have caught either.**
- [x] **Mode 43 (2026-07-29, FIXED PR #196, `5236334`) — module-level
      variable deletion slips both orphan guards.** A `replace_lines` edit on
      `scripts/local_agent.py` deleted the top-level assignment
      `TIMEOUT = float(os.environ.get(...))` while duplicating the adjacent
      line. The module still imported (no `SyntaxError`), and the 12
      surviving reads of `TIMEOUT` elsewhere in the file went undetected
      until a runtime `NameError` surfaced across 39 downstream test
      failures. Root cause: `_newly_undefined_names` (Mode 39's guard) only
      tracks names assigned/read *inside a function body*
      (`_function_name_scopes` walks `FunctionDef`/`AsyncFunctionDef` only);
      `_newly_undefined_module_defs` (Mode 38's fix) only covers deleted
      module-level `def`/`class` names, not deleted module-level *variable*
      assignments. Fix: a third guard, `_newly_undefined_module_vars`, diffs
      top-level `ast.Assign`/`ast.AnnAssign` target names between old and new
      trees and flags any that vanish while a load of that name survives
      anywhere in the new tree — wired into both `local_agent.py` and
      `local_agent_oracle.py`. Regression-tested: a deleting edit is now
      rejected and the file left unchanged; a legitimate refactor removing
      both the assignment and all its uses still passes.
- [ ] **Mode 31 (2026-07-22, NOT fixed) — confident off-task drift.** A
      correctly-scoped, narrowly-instructed dispatch (verified via its own
      transcript) abandoned the assigned task and invented an unrelated one
      instead — 20+ steps of real, coherent-looking tool calls (greps, file
      reads, a genuine `create_file`, a real `pytest` run) on a completely
      different subject. Distinct from a read-loop park: it looks
      productive to any "did it call tools / did it write files" health
      check, so only a content/on-topic diff catches it. No guard exists
      for this today.
- [ ] **Mode 32 (2026-07-22, NOT fixed) — local-model content corruption +
      stall past the configured timeout.** `gemma4:12b-mlx` (a custom
      MLX-imported Ollama model) produced a truncated file ending in a
      literal `# ... (rest of file remains same)` artifact and deleted
      still-imported functions, then later hung 15+ minutes in a live
      `sock_recv`/`poll` with zero progress — well past
      `READ_SILENCE_SECONDS=180`'s supposed bound — while Ollama itself
      stayed responsive to other requests. Suggests some LLM call path
      isn't covered by the streaming/silence-timeout protection. Sanity-check
      this model outside the harness (bare chat completion) before drawing
      any capability conclusion — `/api/ps` shows no `family`/
      `quantization_level`, consistent with a serving/plumbing gap rather
      than a weights problem (same lesson as the earlier MLX tool-format
      investigation).
- [x] **P0 (from `retros/tdd-split-unconditional-and-review-race_2026-07-21.md`)
      — Mode 29: guard `review_story`/the scheduler against dispatching a
      review pass on an already-`done`/merged story.** Fixed 2026-07-22/23
      (PR #157 + #160): `review_story` now skips when `status !=
      "tests_passed"` (no longer re-reviews a done/merged story) AND is
      wrapped in `_plan_lock` so a concurrent MCP call can't race the
      scheduler's own internal review and clobber an already-merged story's
      status. A redundant tick fired
      after a story was already auto-reviewed, auto-merged, and had its
      worktree cleaned up; it found the (correctly) nonexistent worktree and
      reported `REQUEST_CHANGES`, flipping the manifest's status for a done,
      merged story back to `changes_requested`. Nothing on GitHub was ever at
      risk, but a subsequent `approve_merge` call failed on the stale
      precondition and required manually cross-checking `gh pr view`/
      `git log origin/master` to prove the manifest wrong. Fix: check
      `status == "done"` (or PR-already-merged) before dispatching any review
      pass. `pipeline/server.py` (`review_story`, `advance_pipeline`/
      `advance_all_plans` scheduling).
- [x] **P0 (from `retros/tdd-split-unconditional-and-review-race_2026-07-21.md`) — Mode 30 – Scheduler/dispatch now guards `git fetch` with an advisory lock (`_try_acquire_git_lock`). (#177, #178)**
      `advance-scheduler`'s launchd job runs `git checkout`/fast-forward
      directly against this repo's working directory every 60s with no
      visible locking against external writers. A manual `git rebase` run
      in-session raced one of these ticks and produced a corrupted,
      duplicated function body in `pipeline/server.py` — caught only
      because it happened to throw a `NameError`; syntactically-valid
      corruption would have shipped silently. Highest-severity item in this
      retro (actual source corruption, not just stale state) but scoped
      below the Mode 29 status check since the fix is more invasive
      (isolate the scheduler's git bookkeeping from the main working tree,
      or require pausing it before manual git surgery). `advance-scheduler`
      job definition / its git invocation path.
      **Re-confirmed live 2026-07-22** during the Mode 24/28 direct repair —
      `pause_plan` (not merely moving a story's status off dispatch-eligible)
      was the only thing that actually stopped the race; used reactively,
      not proactively. **Also now higher-exposure**: PR #159 (same day) added
      an opportunistic local-branch sync that runs `git fetch`/`merge --ff-only`
      against `REPO_ROOT` on *every* tick for every unpaused plan, not just
      on dispatch — raising how often this exact surface gets touched. Still
      unfixed; `pause_plan` before any manual git surgery is now more
      load-bearing advice than when this bullet was written, not less.
- [x] **P0 (from `retros/tdd-split-always-on_2026-07-21.md`) — track prior
      findings' target paths; refuse silent re-approval** (2026-07-22, PR
      #158). Fixed Mode 28 and Mode 24 with one mechanism: `review_story`
      (in `pipeline/server.py`, not `pipeline/ci.py` as originally noted
      here — the A1 decomposition had already moved it) now records
      `last_review_findings` on every `REQUEST_CHANGES` and downgrades a
      later APPROVE back to `REQUEST_CHANGES` if a previously-flagged file
      was never touched since. Took 5 dispatch attempts across gemma4:12b-mlx
      and gpt-oss:20b to land (surfacing Modes 31-32 along the way) before
      being implemented directly and merged through the normal review gate.
- [ ] **P0 (same retro) — stabilize the flaky-under-load read-heavy/
      repetition-guard tests.** 11 tests pass isolated but fail under
      full-suite load, misleading local-model workers into chasing red
      herrings and tripping the per-target repetition guard. Fix the
      shared-state/order-dependence or mark them non-blocking.
      `test_local_agent.py`, `test_pipeline_mcp_server.py`.
- [x] **P1 (same retro) — route rework to a stronger model when remaining
      findings are polish-only** (2026-07-23, PR #164). `final_rework_escalation`
      plan-level config (default off) routes a story's LAST rework redispatch
      — the one immediately before it would park — to a configured stronger
      provider+model. Depends on the P0 finding-target storage above (PR
      #158), which shipped first.
- [x] **P1 (from `retros/tdd-split-unconditional-and-review-race_2026-07-21.md`)
      — when a story changes `pipeline/server.py` or `pipeline_mcp_server.py`
      itself, make "restart + reconnect the MCP server" an explicit, checked
      step.** The running MCP server is a long-lived stdio child of the
      `claude` CLI (not launchd-supervised, unlike the scheduler/usage-poller)
      jobs which get a fresh process per tick) and keeps executing pre-merge
      code until manually killed — and killing it does not auto-reconnect the
      session's tools. Discovered by manually testing the new
      `mark_story_done` behavior and getting the stale result.
      Shipped: `pipeline/self_modification.py` detects whether a merged
      branch's diff touched `pipeline/server.py` or
      `app/pipeline_mcp_server.py`, and both merge paths (`approve_merge` and
      `advance_pipeline`'s merge gate) then `_notify_user` the operator to run
      `/mcp reconnect` before dispatching or reviewing further work. Detection
      fails open, so a git error skips the notice rather than blocking a merge.
      Shipped: `pipeline/self_modification.py` detects whether a merged
      branch's diff touched `pipeline/server.py` or
      `app/pipeline_mcp_server.py`, and both merge paths (`approve_merge` and
      `advance_pipeline`'s merge gate) then `_notify_user` the operator to run
      `/mcp reconnect` before dispatching or reviewing further work. Detection
      fails open, so a git error skips the notice rather than blocking a merge.
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