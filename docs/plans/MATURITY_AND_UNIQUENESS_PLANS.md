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
      (2026-07-27, `2cca309`). README.md is now a 126-line quickstart;
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
- [x] **Replace the count with two signals that are actually actionable —
      DONE 2026-09-05, plan `a3-maturity-metrics` (9 stories).**
      - **Recurrence, not discovery.** A *new* mode is the system working as
        intended; a *fixed* mode reappearing is the real failure. Precedent
        for guards silently disarming exists: Mode 46 shipped a regression
        file CI never collected, and `tests/unit/test_conftest_env_isolation.py`
        was written precisely because deleting the load-bearing conftest lines
        would not otherwise break CI. Shipped: `pipeline/guard_liveness.py`
        (pure guard-path parsing + a dataset/repo-tree checker, PRs #527/#552)
        and a CLI runner that re-collects each cited test under pytest and
        raises recurrence alerts (PR #557) — the "one-off manual grep" from
        the 2026-08-06 audit is now an automated, repeatable check.
      - **Guard liveness.** Now a live dashboard surface, not just a dataset:
        `GET` plan-metrics/guard-liveness endpoints (PR #572) and a maturity
        metrics + guard-liveness panel on the dashboard frontend (PR #577).
      - **Cost per merged story** (wasted dispatches, rework cycles, direct
        repairs) — unblocked by B4's correlation-ID item landing 2026-09-01
        (see below) and shipped in the same plan: per-story/per-plan metrics
        computed from the now-correlation-ID-bearing notification JSONL
        records (PR #559), surfaced through the same dashboard panel.
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
- [x] **Get CI to an enforced green baseline and tag a real release.** - DONE
      2026-09-11. The GitHub Actions billing cap that blocked this is
      RESOLVED: CI runs and goes green on `master` and on story branches as
      of 2026-09-11. `v0.1.0` is tagged and pushed (annotated "v0.1.0 -
      first public release", pointing at commit `81fc3a7`), with
      `CHANGELOG.md` landed alongside (#681). Three same-day cleanup plans
      got there: `ci-green`; `ci-unmasked-failures` (#676/#677, two
      macOS-only failures that only surfaced once real CI came back); and
      `ci-gate-visible` (#680, so a disabled `PIPELINE_MERGE_CI_GATE` can no
      longer report "pass" and silently outlive its cause). The manual
      local-suite + `gh pr merge` operating mode is RETIRED;
      `approve_merge` / `gh pr checks` is the gate again. **Residual
      cleaned 2026-09-14:** the stale `PIPELINE_MERGE_CI_GATE=0` was removed
      from the scheduler launchd plist and `~/.claude.json`; the repo's
      `.pipeline.env` (`=1`) is now the sole remaining source, so the gate
      no longer depends on an override to be correct.

### A4. Make it usable by someone who isn't the author

- [x] **One-command install story** for a fresh, non-author clone — DONE
      2026-09-03, plan `a4-non-author-usability` (9 stories). `scripts/
      install_checks.py` (stdlib-only prerequisite checks, A4-01) wired into
      `scripts/install.sh` with optional Docker detection (A4-02); README
      quickstart now explains what `install.sh` does and does not do (A4-03).
- [x] **Externalize per-machine assumptions** into documented requirements
      with graceful degradation — DONE, same plan. `pipeline/preflight.py`
      (A4-04) validates the runtime environment with actionable, non-leaking
      errors; a one-line preflight summary logs at dashboard startup (A4-05);
      REFERENCE.md gained a "Runtime preflight" subsection on preflight
      checks and graceful degradation.
- [x] **A getting-started end-to-end smoke** a stranger can run and see a
      story merge, on the all-Claude path — DONE, same plan.
      `scripts/smoke_getting_started.py` (A4-07) runs one story to merge on
      the claude backend against a scratch `PLAN_DIR`; README gained a
      getting-started walkthrough subsection (A4-08).
- [x] **Re-enable the macOS CI leg before the repo goes public.** - DONE
      2026-09-11 via plan `macos-ci-reenable`, PR #674. The macOS matrix leg
      is back in `.github/workflows/ci.yml` AND the `@pytest.mark.skip` is
      gone from `test_a_job_that_runs_pytest_also_runs_on_macos`. It paid
      for itself immediately: #676 and #677 were macOS-only failures
      invisible to single-platform local runs.

---

## Plan B — Close the uniqueness gaps

### B1. Execution isolation & portability (biggest feature gaps vs OpenHands/AWF)

- [x] **Docker sandbox per worktree — DONE 2026-09-05, plan
      `b1-sandbox-and-harness-seam` (9 stories).** `pipeline/sandbox.py`
      resolves `PIPELINE_SANDBOX` (ships `'none'` by default, fails closed —
      not silently unsandboxed — on an unrecognized value) and
      `build_docker_command`/`docker_binary_available` construct the `docker
      run` invocation; wired into `pipeline/execution.py`'s local spawn
      branch so dispatch runs inside the container when opted in. Documented
      in `docs/specs/DOCKER_SANDBOX.md`. **Caveat, read before relying on
      this as a security boundary**: the spec itself says the container
      shares the host's network namespace and can reach the host filesystem
      via the Docker daemon's default settings — "not a hard security
      boundary," in the doc's own words — and its "live-host validation"
      section documents the one-time checks to run against a real Docker
      install but the test suite only mocks the binary, so per this repo's
      own `.claude/rules/testing-config-gates.md` rule those checks are not
      yet confirmed executed against a live host.
- [x] **Remote execution backend — DONE 2026-09-05, plan
      `b1-remote-execution` (7 stories).** `pipeline/execution.py` gained a
      fail-closed `PIPELINE_EXEC_DISPATCH` gate in front of every spawn site;
      `pipeline/remote_sync.py` pushes/materializes the worktree onto a
      remote host and syncs commits back with divergence refusal;
      `pipeline/remote_exec.py` is the local SSH supervisor (spec file,
      `shlex`-quoted remote shell command, exit-code propagation that treats
      a sync-back failure as a story failure even when the remote agent
      exited 0); `ClaudeCliDriver`/`OllamaDriver` dispatch onto this seam.
      Documented in `docs/specs/REMOTE_EXECUTION.md` (GPU-box prerequisites,
      env vars, failure modes). **Caveat**: no record in memory or the retro
      log of an actual run against a live remote/GPU host — same
      not-yet-live-validated gap as the Docker sandbox above.
- [x] **Abstract the *inference provider*.** Done ahead of this doc: the
      `Backend` protocol in `app/backend.py` already sits between orchestration
      and execution, with `ClaudeCliDriver` and `OllamaDriver` (Ollama/LM Studio/
      MLX via `inference_providers.py`) behind it, selected per role through
      `app/role_registry.py`. Originally bundled into the bullet below; split
      out 2026-08-05 because the two axes are different seams and only one is
      still open.
- [x] **Abstract the *agent harness* — the seam now exists, DONE 2026-09-05,
      same `b1-sandbox-and-harness-seam` plan.** `app/harness.py` defines the
      `AgentHarness` protocol (`HarnessRequest`/`HarnessCommand` value types)
      and a fail-closed registry (`get_harness`/`register_harness`, starts
      empty, unknown names raise rather than silently defaulting); a
      `PIPELINE_AGENT_HARNESS` env var resolves the choice, and
      `ClaudeCliDriver`/`OllamaDriver` dispatch onto it via
      `ClaudeCliHarness`/`LocalAgentHarness`. This closes the actual gap this
      bullet named ("there is no seam at which a harness could be dropped
      in") — and the seam now has a second real harness behind it:
      `AiderHarness` (the third-party aider CLI, aider-chat 0.86.2) landed
      2026-09-08 in `app/harness.py` (PRs #593/#596/#597), registered as
      `"aider"`, with a fail-closed guard when the aider binary is missing
      from PATH or the harness is cross-selected, and its CLI contract
      documented in `docs/specs/AIDER_HARNESS.md`. **Caveat, narrowed
      2026-09-08**: the seam is no longer the untested part — but harness
      breadth is not a solved problem. Aider is one adapter against one
      third-party CLI, exercised by unit tests on the argv builder and the
      availability guard; there is still no record of an aider dispatch run
      against a real worktree, and Codex / Goose remain unplugged. The
      breadth question stays open on that evidence, not on the seam.

### B2. Model breadth

- [x] **LiteLLM (or equivalent) adapter** for 100+ LLMs instead of
      per-provider drivers — DONE 2026-09-08, plan
      `b2-litellm-routing-and-aider-harness` (11 stories). `LiteLLMProvider`
      in `app/inference_providers.py` (PR #592) with LiteLLM model strings
      passed through `_resolve_local_model` unchanged (PR #595) and `litellm`
      registered as a `Backend` driver name (PR #594); hosted-provider
      dispatch skips the on-device memory gates in `resource_status` (PR
      #598). Documented in `docs/specs/LITELLM_PROVIDER.md` (PR #602). The
      per-role registry was already the right shape; the driver layer behind
      it is what swapped.
- [x] **Multi-LLM routing** (cheap model for reads/orientation, strong model
      for edits/review) — DONE 2026-09-08, same plan. `app/role_registry.py`
      gained a routing-policy schema and `resolve_route()` (PR #599); auto
      dispatch routes through `resolve_route` in `_route_dispatch_backend`
      (PR #600), and the routing policy is honored only when no runtime
      ceiling is set (PR #605). Documented in `docs/specs/MULTI_LLM_ROUTING.md`.
      `auto` escalation was the seed; it is now a routing policy, not just
      local→Claude.

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

- [~] **Multi-repo fleet as a first-class concept.** `advance_all_plans`
      exists but is plan-per-repo glued. A fleet manager with per-repo
      backends, quotas, and isolation is the scale story.
      **First real step landed 2026-09-14, plan `repo-scoped-plan-visibility`
      (2 stories, PRs #732/#733):** plan summaries now expose `repo_root`,
      the plans API supports a repo filter, and the dashboard plan list
      filters to the active repository with an all-repos toggle. That is
      per-repo *visibility*, not yet a fleet — per-repo backends, quotas,
      and isolation remain open (and are W4 territory in
      `PLATFORM_DECOUPLING_AND_SCALE_PLAN.md`).
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

- [x] **Structured logs + a correlation ID carried across the whole story
      lifecycle — DONE 2026-09-01, plans `w4-logging-correlation` (5 stories)
      + `w4lcorr-lock-isolation` (1 story).** `event-driven-pipeline-phase3`
      (2026-08-14, 12/12 stories, PRs #318-#346) gave notification-level
      events a real schema and a queryable JSONL sidecar first, but left the
      actual join key missing. This closed it: the notification-record
      schema grew an optional `correlation_id` (plus attempt/role/provider/
      model context, W4L-01); dispatch mints one per story, persists it on
      the manifest, injects it into the agent subprocess env, and stamps its
      own events with it (W4L-02); the dispatched agent's own log records
      (`agent.log`/transcript) now carry `PIPELINE_CORRELATION_ID` too
      (W4L-03, `scripts/local_agent.py`); and review, rework, escalation, and
      merge events are all stamped with the story's correlation ID (W4L-04).
      A dispatch → review → rework → merge chain is now joinable by one ID
      across every log source, including the agent subprocess — closing the
      exact gap the 2026-08-14 partial update left open. This also directly
      unblocked A3's "cost per merged story" signal (see A3, DONE
      2026-09-05).
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
- [x] **Replay UI for failed runs — DONE 2026-09-05, plan
      `b4-control-surfaces` (8 stories).** A pure replay-event builder merges
      the checkpoint journal with worktree logs; `GET
      /api/plans/{plan}/stories/{story}/replay` exposes it; the dashboard
      story modal renders the replay timeline.
- [x] **Stuck-agent / wedge detection — DONE 2026-09-05, same plan** (plus
      earlier prep stories). `pipeline/wedge.py` has a pure wedged-story
      verdict function over configurable threshold env vars; wired into the
      `advance_pipeline` tick as a detection-only scan; per-story wedge state
      is exposed on the plan-detail payload and rendered as a wedged health
      badge on dashboard board cards. **Detection only, no automated
      recovery** — the OpenHands comparison this bullet drew was explicitly
      about detection; auto-recovery from a wedged story is still manual.

### B5. Generalize & export the distinctive ideas (the real moat)

- [x] **Package the overlord policy as a reusable spec — DONE 2026-09-01,
      plan `b5-export-the-moat` (3 stories, B5-01, PR #517).**
      `docs/specs/OVERLORD_POLICY_SPEC.md` is a harness-agnostic standalone
      spec of the 3-tier risk + audit decision policy, no longer locked
      inside internal docs. **Update 2026-09-14 — the live policy has since
      outgrown the exported spec.** Plan `overlord-parked-story-autonomy`
      (9 stories, PRs #736-#747) extended the *live* policy
      (`overlord-policy.md` at the repo root) with a parked-story decision
      matrix, an autonomy-mode ladder in which `PIPELINE_AUTONOMY=full`
      includes high-risk merge adjudication, executable `split_story`/
      `patch_acceptance`/`mark_done` rulings, and park-for-human replaced by
      delegate-then-review-post-hoc (the overlord resolves parked stories
      from live evidence; humans audit the decision log afterwards).
      `OVERLORD_POLICY_SPEC.md` itself was not touched by that plan —
      refreshing the exported spec to match is a small open follow-up, and
      the delta is now the most interesting part of the story (a pipeline
      that resolves its own stuck stories and invites post-hoc audit).
- [x] **Publish the acceptance-oracle grading pattern — DONE, same plan
      (B5-02, PR #519).** `docs/specs/ACCEPTANCE_ORACLE_PATTERN.md` documents
      the FM-A insight (grade on fixtures, not the model's own tests; scope
      gate; reviewer as regression backstop) as a standalone module spec.
- [ ] **Standard benchmark integration** (SWE-bench / SWE-bench-Live)
      alongside the bespoke matrix. Enables comparison against OpenHands /
      SWE-agent numbers, not only internal cells. Still open — not part of
      `b5-export-the-moat`'s 3 stories.
- [~] **MCP discoverability — PARTIAL, same plan (B5-03, PR #520).**
      `pipeline/companion_server.py` ships a companion MCP server exposing
      *only* the overlord + acceptance-oracle tools for piecemeal adoption —
      that half is done. Listing it on the MCP registry itself has not
      happened. **Unblocked in practice as of 2026-09-14: the repo is now
      public**, so the registry listing is now a small standalone task
      rather than something gated on a launch decision.

### B6. Ecosystem & community

- [x] **Tag releases + changelog.** - DONE 2026-09-11 via plan
      `release-docs`. `v0.1.0` tagged and pushed; `CHANGELOG.md` (#681)
      carries the 0.1.0 entry in Keep a Changelog format. Automated tooling
      (`release-please` / `cz`) remains optional — the practice is started,
      not automated. **Second release followed 2026-09-12:** `v0.2.0` is
      tagged and pushed, with the 0.2.0 entry written by plan
      `release-0.2.0` (3 stories, PRs #728-#730) and the release procedure
      documented in `docs/RELEASING.md` — the next release is a documented,
      repeatable process rather than git archaeology.
- [x] **Contributor docs + the ADR pattern** - DONE 2026-09-11, same plan,
      PR #682. `CONTRIBUTING.md` plus `docs/adr/` with an index
      (`README.md`), a `template.md`, and the first four decision records:
      0001 review-is-the-primary-quality-gate, 0002
      shipped-model-registry-is-provider-neutral, 0003
      dispatch-backend-resolves-from-environment, 0004
      safety-gates-default-on-and-fail-closed.
- [ ] **A public demo / writeup of the MCP-native inversion** (Claude Code
      driving its own pipeline) — the angle most likely to draw interest, and
      nobody else leads with it. **The repo is public as of 2026-09-14, so
      this is unblocked.** The 2026-09-12→14 work strengthens the material:
      the overlord now resolves its own parked stories in full-autonomy mode
      with a post-hoc-audit decision log (`overlord-parked-story-autonomy`),
      which is a sharper story than the 2026-09-11 snapshot of this doc had.

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
> file-size concern recurring at 1,723/1,599 lines)** — **DONE 2026-08-31,
> plan `local-agent-file-split` (9 stories)**: both scripts landed under
> 1,000 lines (999 / 947) via the `_ServerRef`-proxy pattern applied
> per-cluster — see `PLATFORM_DECOUPLING_AND_SCALE_PLAN.md`'s scaling
> concern #5 for the module list → **B5 (export the moat) — DONE
> 2026-09-01, plan `b5-export-the-moat` (3 stories, PRs #517/#519/#520)** →
> **B4's correlation-ID item — DONE 2026-09-01, plans
> `w4-logging-correlation` + `w4lcorr-lock-isolation` (6 stories)** → **A3's
> guard-liveness/recurrence/cost-per-story signals — DONE 2026-09-05, plan
> `a3-maturity-metrics` (9 stories), unblocked by the correlation-ID work
> above** → **A4's install/preflight/getting-started smoke — DONE
> 2026-09-03, plan `a4-non-author-usability` (9 stories)** → **B1 (Docker
> sandbox + remote execution + agent-harness seam) — DONE 2026-09-05, plans
> `b1-sandbox-and-harness-seam` (9 stories) + `b1-remote-execution` (7
> stories)**, both with a live-host-validation caveat noted under B1 above
> → **workspace picker wiring** — the `workspace-selection` plan's
> last-mile gaps (picker UI wired into `main.js`, active-workspace security
> hardening, chat/save_plan/decompose workspace threading; found 2026-09-02,
> see `project_workspace_picker_gap` memory) — **DONE 2026-09-07, plan
> `workspace-picker-wiring` (12 stories)** → **chat↔codebase parity — DONE
> 2026-09-08, plan `CHAT_CODEBASE_PARITY_PLAN` (9 stories, PRs #601/#603/
> #604/#606-#611)**: shared-secret API-key auth on the dashboard HTTP API
> (#601, with the dashboard frontend sending the key on every request,
> #606), a workspace-scoped path-resolution guard (#603), and workspace
> file-read / directory-list / code-search service+API endpoints (#604/
> #607/#609) each backed by a matching chat tool (read_file #608,
> list_directory #610, search_code #611). **What this plan does NOT close**:
> the "Open questions" entry this doc's sibling
> `PLATFORM_DECOUPLING_AND_SCALE_PLAN.md` resolved 2026-08-20 said chat must
> eventually cover *direct repair of a stuck worktree*, needing "worktree
> file-read + propose-patch + apply." Only the READ half shipped above; the
> write/apply half (propose-patch + apply against a worktree) is still
> untracked scope — see that doc's "Open questions" section, where it is now
> filed explicitly with its precondition intact: it is a prompt-reachable
> arbitrary-write path and needs a security-engineer gate before any
> implementation story is written. → **W4 (multi-tenant, closes
> B3)** — still open, no plan started. See that doc's own "Ordering
> conflict" section for the historical rationale on why B1/B5 were
> sequenced after the service seam.
>
> **What's left as of 2026-09-14:** W4 (multi-tenant — closes B3; deferred
> until a real second deployment exists), B5's remaining SWE-bench
> integration bullet and MCP-registry listing, B6's public demo/writeup,
> A3's reframed failure-mode-discovery-rate item, the worktree write/apply
> half of chat-driven repair (security-engineer review kicked off
> 2026-09-14), and a small follow-up surfaced by this refresh: the exported
> `OVERLORD_POLICY_SPEC.md` lags the live `overlord-policy.md` (see B5).
> Since the 2026-09-11 snapshot, `v0.2.0` shipped (streaming chat,
> per-backend usage reporting, plan-completion e-mail, canonical agent.log
> grammar), `overlord-parked-story-autonomy` landed (9 stories),
> `scheduler-lock-starvation` + `scheduler-reconcile-resilience` hardened
> the scheduler and added the dispatch-lease building block,
> `repo-scoped-plan-visibility` opened the B3 bullet, and the repo went
> public — all recorded in their sections above.

Land what's half-done before building new; then extract the service seam
that everything else — sandboxing included — is cheaper to build behind.
As of 2026-09-14 that sequence has run its course: A1/A2 are long closed
and A3/A4/B1/B2/B4 are now closed or landed-with-caveats; what remains
(W4/B3, plus B5/B6 adoption items) is genuinely new scope, not "land
what's half-done."