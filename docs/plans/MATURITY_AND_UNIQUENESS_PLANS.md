# Maturity & Uniqueness Plans

A self-assessment TODO list, not a specification. Two halves:

- **Plan A — Tighten maturity.** The gaps holding the project under 8/10.
- **Plan B — Close the uniqueness gaps.** Make it more scalable and feature-rich
  relative to the 2026 harness landscape.

Each item is a TODO with a *why* and a rough priority — no design detail yet.

> **See also:** `PLATFORM_DECOUPLING_AND_SCALE_PLAN.md` (2026-08-05; updated
> 2026-08-29) carries the design detail for A2, B1's remote-exec, B3, and B4,
> and adds a service-extraction workstream (W1) this doc has no item for.
> Overlaps, disagreements, and a since-resolved ordering conflict are mapped in
> that doc's "Relationship to MATURITY_AND_UNIQUENESS_PLANS.md" section.

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
- [x] **Finish the MLX validate re-run.** (2026-08-05) Stopped at 5/9
      (`full_matrix_mlx_validate_20260717_115340`, model
      `qwen2.5_coder_14b_manual` — the manually-downloaded/served
      `Qwen2.5-Coder-14B-Instruct-4bit`, the only MLX tier validated stable
      on this 24GB host per `project_mlx_24gb_footprint_ceiling` memory);
      `lru_cache_rs` had groundtruth pass + reviewer APPROVE but the merge
      gate failed 3x on a real Rust compile error (`no method named size
      found for struct LruCache`) — a genuine gt=True-not-merged gap, not a
      grading bug. **Correction (2026-08-05): the "3 fixes" this bullet
      referred to already landed** — commit `dc02b38` (2026-07-17,
      "fix(grading): acceptance scoping, decorator-dedent, review-on-fail")
      shipped the same day as this run and its own message says it was
      validated by it; nothing is uncommitted or parked on a branch. What's
      actually still open: a follow-up re-verification the next day
      (`full_matrix_mlx_validate_20260718_085820` /
      `full_matrix_mlx_rework_20260718_092326`) silently fell back to the
      harness's tiny default MLX tag (`Qwen2.5-1.5B-Instruct-4bit`,
      documented as known-weak in `tests/benchmark/models.py`'s own
      docstring) instead of the 14B config the 07-17 run used, and both runs
      were then killed mid-flight (`Terminated: 15`, exit 143 — cause not
      confirmed, do not assume memory pressure without re-checking). Under
      the weaker model, `lru_cache_rs` regressed to a different failure
      (`.remove(&key)` type mismatch) and two previously-passing cells
      (`cron_field`, `lru_cache`) also flipped to failed. **Done 2026-08-05:**
      re-ran the full 9-cell matrix with the 14B model explicitly configured
      (`BENCH_MLX_TAG=qwen2.5_coder_14b_manual`) to completion (~78 min, exit
      0). `lru_cache_rs` converged — gt-passed and done in 136s; `lru_cache`
      also gt-passed. Overall 2/9 success (22%), 4/9 merged, 2
      merged-but-wrong. Two cells (`ratelimiter_bugfix`,
      `review_story_lock_guard`) `harness_error` at 0s — distinct harness
      bugs in `load_task`, not model failures: (1) it `read_text()`s
      `__pycache__/*.pyc` binaries in `seed/` → `UnicodeDecodeError`; (2) it
      unconditionally reads `acceptance.{ext}` but `review_story_lock_guard`
      has none (its groundtruth declares `"acceptance": []`). File a
      harness-fix story for both before counting those two cells.
- [x] **Implement the reviewer-escalation plan L1** (2026-07-22/23, Mode 40
      series — PRs #166/#168/#171/#172). L1 shipped: `_ci_status` now
      populates the failing-check names, the merge gate synthesizes
      CI-fail-specific rework feedback, `detect_lint_command` is wired into
      `check_story_status`'s test gate, and the merge-gate CI-fail rework
      helper has an integration test. The agent's done-criterion is no longer
      purely oracle-green on CI-fail rework. **L2/L3 (reviewer-escalation
      tiers above L1) remain unimplemented** — see
      `REVIEWER_ESCALATION_PLAN.md`.
- [x] **Implement token-context optimization** (2026-08-06, plan `token-context-a1`,
      PRs #243/#244/#245). Step 0 measurement was answered from existing
      production data (reviewer sidecar logs already showed heavy cache hits,
      hundreds-of-thousands of `cache_read_input_tokens` vs. 10-58 fresh —
      no probe call needed), which deprioritized the original Step 1
      (marking `cache_control` breakpoints). Landed: `role` passthrough on
      `Backend.complete()` (#243); `cell_dir`/`role` wired into the
      planner/rework-planner calls so they now get the same cache-hit
      sidecar data the reviewer role already had (#245); dead
      `PIPELINE_REVIEW_MAX_TOKENS`/`PIPELINE_SECURITY_REVIEW_MAX_TOKENS`
      knobs retired (#244). Measured close-out (2026-08-31, plan CLOSED):
      planner cache hits confirmed from sidecar data (planner sonnet n=83,
      ~70% with cache hits, median ~16,669; rework_planner sonnet n=157,
      ~72%, median ~58,613 — the zero-hit records are all ollama-family
      models that don't report Anthropic-style cache fields), so prompt
      caching is NOT full-price on the Claude backend and Step 1 is
      deprioritized permanently; the Claude reviewer input-context cap
      (Step 4) is closed as infeasible-as-scoped (no mechanism to bound the
      real `claude` CLI's internal tool use).
      See `TOKEN_CONTEXT_OPTIMIZATION_PLAN.md`'s status footer.
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

- [x] **Make `model_registry.json` the single source of truth for role/model
      selection** — already done, found on re-check 2026-08-05.
      `app/role_registry.py::resolve_role()` fully implements the plan → env
      → registry → default priority chain for the ~9 role/model vars
      (`PIPELINE_BACKEND_*`, `PIPELINE_LOCAL_MODEL_*`, `PIPELINE_LOCAL_PROVIDER`,
      `PIPELINE_DEFAULT_MODEL`). **Correction to this bullet's original framing:**
      of the ~80 `PIPELINE_*` env vars in the repo, only those ~9 are
      role/model-selection vars; the rest are unrelated operational knobs
      (timeouts, thresholds, feature flags, paths) that this item was never
      meant to touch and shouldn't be — "demote ~50 vars" overstated the
      actual scope, which is already centralized.
- [x] **Deprecate the `LOCAL_AGENT_MAX_STEPS`/`NUM_CTX`/`TEMPERATURE` alias
      traps** — found 2026-08-05 to be ~90% already shipped via
      `TRANSPORT-ALIAS-SETTER`/`-READERS`/`-CLEANUP` (PRs #194/#200/#205):
      `scripts/local_agent.py` and `local_agent_oracle.py` read only
      `PIPELINE_TRANSPORT_*` now; nothing reads the legacy `LOCAL_AGENT_*`
      trio anymore. The one remaining gap — `backend.py` still *wrote* the
      three dead `LOCAL_AGENT_*` keys into the dispatch env every call, plus a
      stale `REFERENCE.md` description — is the `config-surface-a2-cleanup`
      plan filed 2026-08-05 (dispatched to local ollama/gpt-oss-20b-high).
      **Correction:** this bullet previously conflated two unrelated things.
      The `"local"` back-compat *driver-name* alias (`backend.py`, selecting
      the Ollama driver) is explicitly commented **"permanent, must never be
      removed"** (dispatched-story manifests persist `"backend": "local"`) —
      out of scope for any deprecation, unlike the `LOCAL_AGENT_*` transport
      vars above which were always meant to go away once nothing read them.
- [x] **Split the 74 KB README** into a quickstart + a reference doc
      (2026-07-27, `5f7d811`). README.md is now a 126-line quickstart;
      REFERENCE.md (919 lines) holds the moved reference material.
- [x] **W3a — effective-config + provenance view** (2026-08-09, plan
      `w3a-effective-config-provenance`, PRs #247-#258). Makes the effective
      value of every role and operational `PIPELINE_*` env var visible, plus
      which of process env / launchd plist / `~/.claude.json` MCP env /
      `model_registry.json` / plan `role_config` supplied it, whether sources
      conflict, and dead-var flags. Read-only: `pipeline/config_provenance.py`
      (catalog + layered resolution for both env vars and the real 8-role
      list), `get_effective_config` MCP tool, `/api/config` dashboard
      endpoint. Write path is W3b, later, after the W1 `PipelineService`
      extraction. See `PLATFORM_DECOUPLING_AND_SCALE_PLAN.md`'s dependency
      order — this was step 1 of 7; **next up is W1a (extract
      `PipelineService`)**.

### A3. Stabilize the active bug surface

- [ ] **Bound the failure-mode discovery rate — REFRAMED 2026-08-06. The
      original metric is the wrong instrument, and is not measurable today.**
      History: 19 modes at this doc's 2026-07-17 baseline, 32 by 2026-07-22,
      **50 named as of 2026-08-06**. The original prescription — "add a 'no
      new modes for N benchmark runs' gate" — should be retired, for three
      reasons that are now evidenced rather than argued:
      1. **It measures the wrong thing.** A raw discovered-mode count rises
         with dispatch volume and with honest logging, so "the number went
         up" cannot distinguish a degrading system from a well-instrumented
         one. Held as a target, it penalizes recording a mode at all.
      2. **The proposed instrument would not have fired.** Modes 42
         (2026-07-24), 43 (2026-07-29) and 50 (2026-08-05) were each found by
         dispatching real production stories, not benchmark cells. A
         benchmark-run-scoped gate stays green through all three.
      3. **These are not one population.** They fall into at least three
         classes with unrelated fixes: **harness defects** (Modes 44/46/50 —
         real bugs in dispatch/grading code, each fixable and regression-
         testable); **plan-authoring defects** (born-broken oracle,
         isolation-only fixture, lint-dirty fixture, an acceptance assertion
         on a gitignored path — operator error, addressed by pre-ingest gates
         like `pipeline/oracle_gate.py` and `_isolation_only_acceptance_warning`,
         not by harness fixes); and **model capability limits** (Modes
         22/31/32/42 — no code change closes these; they belong in the
         local-dispatch splitting rules as known-unsafe task shapes, not in a
         bug backlog). The class label must stay *revisable*: Mode 49 was
         reframed from model weakness to unwinnable-task once the
         born-broken-oracle cause was found, and Mode 34 from bug to
         not-a-bug. Class is an attribute of an entry, never a separate
         counter to defend.
- [x] **Prerequisite — make the failure-mode log a dataset instead of prose.**
      Verified 2026-08-06: none of the above was measurable, because the log
      was not machine-readable. Of the 50 modes named in
      `project_dispatch_failure_modes.md`, only **37 had a body section** —
      the other 13 (29-32, 38-43, 47-49) existed *only* inside a single
      48,591-character frontmatter `description` blob. Status vocabulary was
      free text and inconsistent (`FIXED`, `NOT fixed`, `MITIGATED`,
      `PARTIALLY fixed`, `operational, not a code bug`, and one
      "observability gap FIXED … CORRECTION — not actually a bug"). Of the 17
      `test_*.py` filenames the log named, 3 were benchmark *cell fixtures*
      rather than regression guards, and 1
      (`test_transport_alias_readers_regression.py`) does not exist in the
      repo at all — legitimately, it was reverted with PR #197 and superseded
      by `test_transport_alias_cleanup_spec.py`, but nothing in the log said
      so. **Done same day**: added a `Mode | Date | Class | Status | Fix ref`
      table to the top of `project_dispatch_failure_modes.md` (51 rows —
      Modes 1-50 plus the historical 16b/16-recurrence sub-entries), each
      status drawn from that mode's *most recent* write-up rather than its
      original header (several modes were reframed after first landing, e.g.
      34 bug→not-a-bug, 49 model-weakness→unwinnable-task). Class split:
      **42 harness-bug, 5 model-capability, 3 operational, 1 plan-authoring,
      1 not-a-bug** — most of the raw count growth that looked alarming
      under the old framing is harness bugs being found-and-fixed, not
      capability debt accumulating. The regression-guard-path column was
      deliberately NOT backfilled (the 3-cell-fixture / 1-nonexistent-file
      spot-check above means a careless mapping would just add fabricated
      data); that per-mode verification pass is the next actionable step.
- [x] **Guard-path backfill.** Done 2026-08-06, same day as the dataset
      prerequisite. Read all 50 modes' full write-ups (not just the
      frontmatter blob) and, for each cited test name/symbol, grepped the
      live repo to confirm the file exists under `tests/unit/` and actually
      covers that mode — rather than trusting the log's own prose, which the
      earlier audit had already shown drifts (3 benchmark-fixture mislabels,
      1 nonexistent file among 17 prior citations). Result: **25 of 51
      entries have a confirmed guard**; the rest are honestly marked "none
      identified" — either genuinely un-fixed/operational (no guard
      expected) or a plausible-sounding file that couldn't be tied to the
      specific mode with a symbol match. Guard-liveness (does the cited test
      still exist and get collected) is now checkable by re-running the same
      grep pattern periodically; recurrence detection is unblocked by the
      Mode/Status columns from the prior step.
- [ ] **Replace the count with two signals that are actually actionable.**
      - **Recurrence, not discovery.** A *new* mode is the system working as
        intended; a *fixed* mode reappearing is the real failure. Precedent
        for guards silently disarming exists: Mode 46 shipped a regression
        file CI never collected, and `tests/unit/test_conftest_env_isolation.py`
        was written precisely because deleting the load-bearing conftest lines
        would not otherwise break CI.
      - **Guard liveness.** Data now exists (25/51 confirmed paths above);
        turning the periodic re-check into an actual automated gate (vs. a
        one-off manual pass) is the remaining step.
      - **Cost per merged story** (wasted dispatches, rework cycles, direct
        repairs) is the metric that would actually show maturity improving,
        and it is blocked on B4's structured-logs + correlation-ID item. The
        two should be sequenced together, not tracked as independent work.
- [x] **Latent, found while auditing the above (2026-08-06): the `testpaths`
      allowlist is badly stale.** `pyproject.toml` pins **23** files while
      `tests/unit/` holds **104** — a bare `pytest` collects ~22% of the
      suite. The gates themselves were already safe: CI
      (`.github/workflows/ci.yml`) and `pipeline/build_detect.py` both apply
      `--override-ini=testpaths=. --ignore=tests/benchmark
      --ignore=tests/experiments`, and the two have matched exactly since
      Mode 50's fix. But this was the same shape as Mode 46 with a far larger
      gap than when Mode 46 was found, and it silently misled any human or
      agent who ran bare `pytest`. **Fixed same day, PR #246** (dispatched
      through the pipeline to local `gpt-oss-20b-high`, one production file,
      merged autonomously by the scheduler): dropped the stale file list and
      replaced it with `addopts = "--ignore=tests/benchmark
      --ignore=tests/experiments"`, mirroring the override flags the real
      gates already used instead of a hand-maintained allowlist that rots.
      Regression-tested in `tests/unit/test_pytest_collection_allowlist.py`
      (asserts a previously-unlisted file is now collected by bare `pytest`,
      while `tests/experiments`/`tests/benchmark` stay excluded).
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
- [x] **Mode 31 (2026-07-22, FIXED 2026-08-05, PR #234/#235) — confident
      off-task drift.** A correctly-scoped, narrowly-instructed dispatch
      (verified via its own transcript) abandoned the assigned task and
      invented an unrelated one instead — 20+ steps of real, coherent-looking
      tool calls (greps, file reads, a genuine `create_file`, a real `pytest`
      run) on a completely different subject. Distinct from a read-loop park:
      it looks productive to any "did it call tools / did it write files"
      health check, so only a content/on-topic diff catches it. Fixed via the
      `mode31-off-task-drift-guard` plan's two stories:
      `_expected_task_paths`/`_is_off_task_path` helpers (PR #234) extract the
      file paths a task brief names and flag a mutating tool call's target as
      off-task when it matches none of them (failing open when the brief
      names no files), then wired into `scripts/local_agent.py`'s `main()`
      mutating-tool handling (PR #235) with an acceptance fixture that
      exercises the wiring itself, not just the unit — avoiding the
      isolation-only-fixture trap this same doc's story-schema rule warns
      about.
- [~] **Mode 32 (2026-07-22, PARTIALLY fixed 2026-08-05, PR #232) — local-model
      content corruption + stall past the configured timeout.** `gemma4:12b-mlx`
      (a custom MLX-imported Ollama model) produced a truncated file ending in a
      literal `# ... (rest of file remains same)` artifact and deleted
      still-imported functions, then later hung 15+ minutes in a live
      `sock_recv`/`poll` with zero progress — well past
      `READ_SILENCE_SECONDS=180`'s supposed bound — while Ollama itself
      stayed responsive to other requests. The original gemma4:12b-mlx
      incident's exact root cause is still not sanity-checked in isolation
      (still worth doing before drawing a capability conclusion — `/api/ps`
      shows no `family`/`quantization_level`, consistent with a serving/
      plumbing gap rather than a weights problem). What IS now fixed: the
      general class of "some LLM call path isn't covered by the streaming/
      silence-timeout protection" this mode named — `app/backend.py`'s
      `OllamaDriver._chat` (the review/planner/overlord/decompose path, now
      the DEFAULT path for review since `test_author`/`review` route to
      ollama/glm) was a single blocking, non-streaming httpx call with a flat
      timeout and ZERO retry, unlike `scripts/local_agent.py`'s dispatch path
      which streams + retries. `_chat` now retries transient httpx failures
      (`TransportError`, 5xx) up to `PIPELINE_LOCAL_CHAT_MAX_ATTEMPTS` with
      linear backoff; a 4xx or `RateLimitedError` still propagates
      immediately, unchanged. Landing this took ~2 hours of dispatch churn
      unrelated to the fix itself — see memory `project_dispatch_failure_modes`
      Mode 50 for the harness-bug tangent this surfaced (a `pipeline/
      build_detect.py` pytest-collection bug, unrelated to Mode 32, that made
      local rework verification structurally impossible after this repo's own
      reorg; fixed separately, PR #233).
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
- [x] **P0 (same retro) — stabilize the flaky-under-load read-heavy/
      repetition-guard tests.** 11 tests pass isolated but fail under
      full-suite load, misleading local-model workers into chasing red
      herrings and tripping the per-target repetition guard. Fix the
      shared-state/order-dependence or mark them non-blocking.
      `test_local_agent.py`, `test_pipeline_mcp_server.py`.
      **Resolved 2026-08-03 (verified, not re-fixed):** the flakiness was the
      `LOCAL_AGENT_*` env-leak from the scheduler's launchd plist into the
      agent's pytest subprocess — module-level constants in
      `scripts/local_agent.py` (`READ_HEAVY_WINDOW`, `PARK_ENABLED`) read the
      env once at import time, so a leaked `LOCAL_AGENT_PARK_ENABLED=0` /
      non-default `LOCAL_AGENT_READ_HEAVY_WINDOW` made every default-asserting
      guard test fail under load but never in normal CI. The fix —
      `tests/unit/conftest.py` clearing every `PIPELINE_*`/`LOCAL_AGENT_*` var
      at conftest's own import time, before test modules import `local_agent`
      — landed 2026-07-22 (one day after this retro flagged it); the checkbox
      was simply never updated. Verified empirically: 5 full-suite runs green,
      and `LOCAL_AGENT_READ_HEAVY_WINDOW=99 LOCAL_AGENT_PARK_ENABLED=0` leaked
      into the suite env still yields 2163 passed (conftest neutralizes it).
      Added `tests/unit/test_conftest_env_isolation.py` as a subprocess
      regression guard that catches removal of those load-bearing conftest
      lines (normal CI has no leaked vars, so silently deleting them wouldn't
      otherwise break CI — only the in-agent subprocess scenario).

**2026-08-03 harness fixes (same session, recorded here as a non-checkbox
note — full detail in memory `project_oracle_gate_harness_fixes_2026_08_03`):
two harness-side grading fixes for the failure-mode class that kept the Mode
count climbing (45–49 were almost all harness/oracle bugs, not model bugs).
(a) Born-broken-by-prior-gate oracle detection (Mode 49, 5 wasted
dispatches): `pipeline/oracle_gate.py` now blocks dispatch when an acceptance
oracle's own test edits trip the already-merged `confirm_removals` deletion
gate — detected via the gate's block signature in the baseline failure output
when the oracle's source does not itself reference `confirm_removals`.
(b) Pure-append tamper allowance (s4, PR #222): `pipeline/ci.py`
`_acceptance_tampered` now treats the original oracle source surviving as a
byte-exact prefix of the worktree fixture as NOT tampered for non-TDD-split
stories (TDD-split stays strictly read-only). Tests in
`test_acceptance_oracle_gate_prior_gate.py` and
`test_acceptance_oracle_tamper_append.py`.**

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
      `claude` CLI (not launchd-supervised, unlike the scheduler/usage-poller
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
- [x] **Fix the test-isolation leak** (2026-07-18, PR #131) —
      `load_oracle_module_with_env`/`load_module_with_env` mutating
      `os.environ` without cleanup; fixed with an `autouse` environ-snapshot
      fixture in both test files, plus a regression test pair.
- [x] **Mode 52 (found 2026-08-12, FIXED 2026-08-13, PR #301, plan
      `harness-guard-hardening-mode52-55`) — `create_file`'s content-loss
      guard had no check for dropped module-level constants/dict literals,
      only def/class.** `_dropped_top_level_defs`
      (`scripts/local_agent.py`, mirrored in `local_agent_oracle.py`) walked
      only `FunctionDef`/`AsyncFunctionDef`/`ClassDef`; `_newly_undefined_module_vars`
      (Mode 43's fix) covered dropped constants but only when the name was
      still referenced elsewhere in the same file — the exact precondition
      Mode 44's def-guard was built to NOT require. A `create_file` rewrite
      that dropped a constant/table nothing else in that file referenced
      (e.g. an `ENV_VAR_CATALOG`-shaped export) passed both guards silently.
      Extended the content-loss guard to cover dropped module-level
      constants/dict literals unconditionally, not just def/class. See
      memory `project_dispatch_failure_modes` Mode 52 for detail.
- [x] **Mode 53 (found 2026-08-12, FIXED 2026-08-13, PR #303, plan
      `harness-guard-hardening-mode52-55`) — no check for a stale worktree
      base before a resumed/redispatched story starts working, only at merge
      time.** `_rebase_onto_master` was only called from `approve_merge`/the
      scheduler's merge tick, never from the dispatch/resume path — a
      worktree branched before an unrelated fix landed on `origin/master`
      carried no signal of that until merge time. Fixed: a resumed story's
      worktree base is now flagged when it has fallen behind origin's
      default branch, before dispatch starts.
- [x] **Modes 54/55 (found 2026-08-12, FIXED 2026-08-13, PR #305, plan
      `harness-guard-hardening-mode52-55`) — the adjacent-content-corruption
      class recurred after `edit-guard-enforcement` landed.** token-context-a1's
      corrupted stories (PRs #243/#244) merged 2026-08-06, 3 days after the
      guards (landed 2026-08-03) — resolving that retro's own open "predate
      or postdate" question in favor of postdate, i.e. a live gap, not a
      historical artifact. w3a's story 6 rework (`b5577e47`, PR #254)
      clobbered ~190 unrelated lines with the same guards live and was the
      more severe sibling. **Root cause pinned 2026-08-12**: the damage came
      via `replace_lines`, not `create_file`; the orphan-name guard
      (`_newly_undefined_names`) only flagged a dropped top-level def/class/var
      when something *else in the same file* still referenced it, and the 4
      deleted symbols (`resolve_env_var`, `effective_env_config`,
      `ENV_VAR_CATALOG`, `ignored_env_vars_present`) were consumed by
      `pipeline/server.py`/the dashboard, not `config_provenance.py` itself —
      the same cross-file blind spot as Mode 52, confirmed on `replace_lines`
      too. Fixed: the unconditional top-level-symbol-loss check is now wired
      into `replace_lines`/`str_replace`, naming lost symbols explicitly in
      the removal report, closing the gap the old unscoped
      `confirm_removals=true` escape hatch left. See memory
      `project_dispatch_failure_modes` Modes 54/55 for full detail.
- [ ] **Get CI to an enforced green baseline and tag a real release.** 703
      commits, no real release tags (only a `w1c02-backup` checkpoint tag) —
      adoption starts with "what version."

### A4. Make it usable by someone who isn't the author

- [ ] **One-command install story** for a fresh, non-author clone (the
      `.venv-mlx`, launchd plists, and absolute-path assumptions are
      personal-machine baked-in).
- [ ] **Externalize per-machine assumptions** (24 GB Mac, Ollama/MLX
      coexistence, launchd) into documented requirements with graceful
      degradation when absent.
- [ ] **A getting-started end-to-end smoke** a stranger can run and see a
      story merge, on the all-Claude path (no local model required).
- [ ] **Re-enable the macOS CI leg before the repo goes public.** Commit
      7958a23 disabled the macOS runner "until repo is public" (repo is
      still private), and `test_a_job_that_runs_pytest_also_runs_on_macos`
      is currently `@pytest.mark.skip` for the same period (PR #355). Before
      switching the repo to public: re-enable the macOS matrix leg in
      `.github/workflows/ci.yml` AND remove the skip so macOS-only
      dependencies (the original `plutil` motivation) are caught before a
      PR opens again. This is a release gate, not optional polish.

---

## Plan B — Close the uniqueness gaps

### B1. Execution isolation & portability (biggest feature gaps vs OpenHands/AWF)

- [ ] **Docker sandbox per worktree.** Agents currently run with full host
      access. Table stakes for any multi-user or untrusted-input story.
- [ ] **Remote execution backend** (a server mode, not just local subprocess).
      Lets dispatch/review run on a GPU box while the orchestrator stays local.
- [x] **Abstract the *inference provider*.** Done ahead of this doc: the
      `Backend` protocol in `app/backend.py` already sits between orchestration
      and execution, with `ClaudeCliDriver` and `OllamaDriver` (Ollama/LM Studio/
      MLX via `inference_providers.py`) behind it, selected per role through
      `app/role_registry.py`. Originally bundled into the bullet below; split
      out 2026-08-05 because the two axes are different seams and only one is
      still open.
- [ ] **Abstract the *agent harness*.** The still-open half. `claude -p` and the
      hand-rolled local tool-calling loop are two bespoke harnesses, not one
      interface — there is no seam at which Codex / Aider / Goose could be
      dropped in. This is the axis that would give the multi-harness breadth
      Agent Orchestrator has, and it is independent of which model serves the
      tokens.

### B2. Model breadth

- [ ] **LiteLLM (or equivalent) adapter** for 100+ LLMs instead of
      per-provider drivers. The per-role registry is already the right shape;
      swap the driver layer behind it.
- [ ] **Multi-LLM routing** (cheap model for reads/orientation, strong model
      for edits/review). `auto` escalation is the seed; generalize it to a
      routing policy, not just local→Claude.

### B3. Scale & multi-tenancy

> **Prerequisite (added 2026-08-05; SATISFIED 2026-08-17):** every item below
> needed a service seam — the `@mcp.tool()` entrypoints and the state machine
> were the same `pipeline/server.py` (then 4,132 lines; now split into nine
> modules under 1,000 lines by `server-app-file-split`, PRs #435-#457), with no
> `PipelineService` a non-MCP caller could drive and no `Store` abstraction
> over the `PLAN_DIR` JSON files. **That seam now exists:** W1a extracted
> `PipelineService` (2026-08-12) and W1b added the `Store` protocol + `FileStore`
> (2026-08-15). So B3 is no longer *refactor-then-feature* — the refactor is
> done; what remains under B3 is the genuine multi-tenant work (W4 in
> `PLATFORM_DECOUPLING_AND_SCALE_PLAN.md`). Design detail and sequencing there.
>
> Also note the single-host assumption is load-bearing in ~6 places, not one:
> `fcntl.flock` plan locking, `os.kill(pid, 0)` slot accounting, the single
> non-elected 60s launchd scheduler, local worktrees under `WORKTREE_ROOT`,
> read-modify-write on the shared decision/journal JSON, and the local GPU. The
> binding throughput limit today is the last of these — `MAX_CONCURRENT_AGENTS=1`
> is a 24 GB memory ceiling, not caution, so orchestration-side scaling work
> changes nothing until the inference tier is separated (see B1's remote
> execution backend).

- [ ] **Multi-repo fleet as a first-class concept.** `advance_all_plans`
      exists but is plan-per-repo glued. A fleet manager with per-repo
      backends, quotas, and isolation is the scale story.
- [ ] **RBAC / multi-user.** Overlord park-and-ping is the kernel of an
      authorization model; extend it to actual users/roles for shared
      deployments.
- [ ] **Concurrency & queueing beyond `MAX_CONCURRENT_AGENTS`.** A real work
      queue with priorities, fair sharing across plans, and back-pressure.

### B4. Observability & control surfaces

> **Prerequisite (added 2026-08-05; SATISFIED 2026-08-24):** the writable
> dashboard was a refactor-plus-UI task, not a UI task. `app/dashboard.py` was
> deliberately import-decoupled from the orchestrator but re-parsed the
> `PLAN_DIR` file layout itself — so the manifest's on-disk shape was a de
> facto public API with two independent parsers. Adding writes on top of that
> second parser would have entrenched it. **This is now done:** W3b rerouted
> the dashboard GET handlers through `Store`/`PipelineService`, retired the
> local parse helpers, and added the config-write surface through the same
> service API every other client uses (PRs #421-#432). The second parser is
> gone; the on-disk layout is private again. See
> `PLATFORM_DECOUPLING_AND_SCALE_PLAN.md` (W1, W3b).

- [~] **Structured logs + a correlation ID carried across the whole story
      lifecycle** (dispatch → review → rework → merge, including into the agent
      subprocess so `agent.log` joins up with orchestrator lines). Today: text
      files with no join key (`dashboard.log` 9.5 MB, `mlx-server.log` 7 MB),
      so every retro is hand-reconstructed from five separate logs. This is
      also the tooling A3's "bound the failure-mode discovery rate" needs to
      become measurable rather than archaeological — and `CLAUDE.md` already
      mandates it, so the orchestrator currently fails its own standard.
      **Partial progress (2026-08-14): `event-driven-pipeline-phase3` shipped
      complete, 12/12 stories, PRs #318-#346.** `_notify_user` now emits
      structured events (severity, dedup key) onto a process-wide bus with a
      file-log sink and a JSONL sidecar, replacing ad hoc inline file writes;
      the dashboard serves/collapses/filters these as structured records and
      renders them per-story in the story detail modal. This gives
      notification-level events a real schema and a queryable sidecar, but it
      does **not** close this bullet: there is still no correlation ID joining
      a story's dispatch/review/rework/merge log lines together, and
      `agent.log` (the dispatched agent subprocess's own transcript) is not on
      the bus — only orchestrator-side notifications are. The remaining gap is
      narrower than before this landed, not gone.
- [x] **Writable dashboard / control plane.** Current dashboard is read-only
      by design (good safety instinct). Add an explicit, audit-logged action
      surface (pause/resume/approve/reroute) so overlord decisions are
      reviewable and overridable from the UI.
      **DONE 2026-08-24** via W3b (plan `w3b-dashboard-config-ui`, 11/11
      stories, PRs #421-#432): dashboard GET handlers reroute through
      `Store`/`PipelineService` and the local `PLAN_DIR` parse helpers are
      retired; config is editable in the UI (global role defaults, per-plan
      `role_config`, per-story `backend`) with effective-value + provenance
      display and a security-engineer gate (W3b-B5). The action surface
      (pause/resume/approve/reroute) flows through the same gated service
      methods every other client uses. The chat entry point (W2, PRs
      #391-#398 + #406) is the conversational control surface on the same API.
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

~~A1 → A3 (fix-isolation) → A2 → B1 (sandbox) → B5 (export the moat) → B2 →
B4 → B6.~~ **Superseded 2026-08-06 — see resolution below.**

> **Ordering conflict — RESOLVED 2026-08-06.** This doc's original ordering put
> B4 second-to-last and omitted B3 entirely — defensible as "land what's
> half-done, export the distinctive ideas before building breadth," but it
> deferred the "UI is the entry point, not Claude Code" goal in
> `PLATFORM_DECOUPLING_AND_SCALE_PLAN.md` behind six other workstreams, while
> that plan's own sequence front-loads the service extraction because B3/B4
> both queue behind it. **Decision: `PLATFORM_DECOUPLING_AND_SCALE_PLAN.md`'s
> sequencing now governs post-A3 work.** With A1/A2 closed, the path is: ~~W3a
> (effective-config+provenance view, no prerequisites, serves A2)~~ **DONE
> 2026-08-09, PRs #247-#258** → ~~W1a (extract `PipelineService` — the
> keystone both B3 and B4 depend on)~~ **DONE 2026-08-12, 22 stories, PRs
> #263-#286** → ~~W1b (`Store` protocol — `FileStore` as the only
> implementation)~~ **DONE 2026-08-15 — 20/20 stories, PRs #315-#349** →
> ~~W1c (HTTP adapter + SSE event stream)~~ **DONE 2026-08-17 — 9/9
> stories, PRs #350, #360-#366** → ~~W2 (chat entry point)~~ **DONE
> 2026-08-20 — 9/9 stories, PRs #391-#398 + #406** → ~~W3b (writable
> dashboard, closes B4)~~ **DONE 2026-08-24 — 11/11 stories, PRs
> #421-#432** → ~~`server-app-file-split` (split the then-5,466-line
> `pipeline/server.py` + 2,730-line `static/app.js` into modules under
> 1,000 lines)~~ **DONE 2026-08-25 — 22 stories, PRs #435-#457** →
> ~~`workspace-selection` (the chat entry point's new-user path: workspace
> create/pick + recents + server-side repo_root validation + chat tools +
> picker UI; with it, a new user runs entirely in the browser)~~ **DONE
> 2026-08-29 — 13 stories, PRs #475-#497 (+ `workspace-security-followups`
> #489/#492)** →
> **re-split `scripts/local_agent.py`/`local_agent_oracle.py` (same
> file-size concern recurring at 1,723/1,599 lines) — PARTIAL 2026-08-27,
> direct, no plan: config constants + tool schemas extracted to
> 1,530/1,436 lines; `run_tool`/transport/`_main_impl` remain, needing the
> same `_ServerRef`-proxy rigor `server-app-file-split` used, applied
> per-function — scoped follow-up, not force-completed same-session** →
> W4 (multi-tenant, closes B3). B1
> (sandbox) and B5 (export the moat) are picked up once the service seam
> exists, not before — the seam now exists (W1a/W1b landed), so B1/B5 are
> unblocked whenever prioritized ahead of W4. See that doc's own "Ordering
> conflict" section for the full rationale.

Land what's half-done before building new; then extract the service seam
that everything else — sandboxing included — is cheaper to build behind.