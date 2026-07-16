# Plan — In-Story Guided Decomposition ("tech-lead → jr executor")

**Status:** Implemented (phases 1-3; production code + harness plumbing, both TDD'd,
955 unit tests + benchmark self-tests green) 2026-07-15. First live experiment run
same day — see Results below. Written 2026-07-15.

**One-line thesis:** A weak local implementer (gpt-oss / qwen3-coder — "jr level")
succeeds more often when a strong "tech-lead" planner first breaks a coarse story
into an ordered sub-step checklist that the local model executes *inside a single
worktree/transcript*, with a persistent scratchpad carrying state across sub-steps
and reworks. This is distinct from — and specifically designed to avoid the failure
mode of — splitting a story into multiple pipeline stories.

---

## 1. Why this plan exists (and what it is NOT)

The intuitive fix — "the plan fed to local models is too high-level; add a middle
step that breaks stories into smaller stories" — has **already been built and
tested** in this repo:

- The mechanism is the product-analyst **"Story Sizing"** guidance (bias toward
  smaller, single-concern, dependency-chained stories).
- The test is `tests/benchmark/PRODUCT_ANALYST_VALIDATION_PLAN.md` (Condition D =
  product-analyst's 3-story decomposition vs. Condition M = one bundled story,
  same requirement, same hidden groundtruth, `gptoss_temp03`, dispatch pinned
  `local`).
- **Result: decomposition did NOT help — it hurt.** Post-fix rerun: M = 100%
  correct-and-merged, D = 33%. The hard sub-problem (fractional-refill math)
  dominated regardless of framing; splitting *relocated* the difficulty into its
  own cold-start story rather than resolving it, and cold dispatch per story
  *fragmented context* (repetition-guard trips; one merged-but-wrong where the
  downstream story never saw the upstream build).

**Two lessons that shape this plan:**

1. **Difficulty is reasoning-density, not scope.** Listing steps cannot, by itself,
   make a hard reasoning nugget easier. The experiment must therefore isolate
   whether *guidance density* (a principal telling the jr exactly how to approach
   each step) moves the needle — not merely *step count*.
2. **Fragmenting context is a real cost.** Splitting at the *pipeline-story* level
   throws away the shared transcript. Decomposition must happen **within one
   dispatch/worktree**, so the executor keeps full context across sub-steps.

**What already exists and is NOT re-litigated here:**

- Intra-story transcript persistence + rework resume: `.agent_transcript.json` in
  the worktree, resumed via `LOCAL_AGENT_RESUME_TRANSCRIPT_PATH`, reviewer feedback
  appended mid-transcript (`backend.py` ~L875–887; validated in production, see
  `project_rework_resume_validation`). Cross-*rework* memory is solved. This plan
  adds cross-*sub-step* structure, not a new persistence engine — it reuses the
  same atomic-write pattern (`PersistingList` / `_persist_messages`).

---

## 2. The refined hypothesis (what we actually test)

> **H1 (guidance):** For a coarse story handed to a constrained local model, a
> strong-planner-authored ordered checklist (sub-steps + per-step done-criteria),
> executed within a *single* worktree/transcript, reaches correct-and-merged more
> often than the same coarse story with no checklist.

> **H2 (planner strength):** The benefit of H1 depends on planner strength — a
> cloud/principal planner produces checklists that help; the same weak local model
> planning for itself does not (or helps much less). If H2 is false (local planner
> helps just as much), the cheaper local-planner variant wins on cost.

> **H3 (memory):** A persistent sub-step scratchpad (state carried forward as the
> executor completes each step) contributes independently of the checklist — i.e.
> checklist+scratchpad beats checklist-only.

These are deliberately separable so a positive result tells us *which lever* to
ship, and a negative result on H1 kills the whole line cheaply.

---

## 3. Design of the decomposition step

### 3.1 Where it injects (in-story, not pipeline-story)

A new **pre-implementation phase inside `Backend.dispatch()`** (`backend.py`),
gated behind a flag (`PIPELINE_DECOMPOSE=off|local|cloud`, default `off` — secure/
neutral default, opt-in per Core Principles):

```
dispatch(story):
    if PIPELINE_DECOMPOSE != off and no existing .agent_plan.md in worktree:
        plan = run_planner(story.agent_instructions, repo_context)   # NEW
        write .agent_plan.md   (ordered checklist + per-step done-criteria)
    run local_agent.py  (executor)  # existing path, prompt augmented with the plan
```

- The **planner** is a single, bounded LLM call (not an agent loop) — cheap. It
  reads `agent_instructions` + a lightweight repo snapshot (file tree + the target
  file(s) named in the brief) and emits a checklist. Model selected by the flag:
  `cloud` → the configured escalation model (principal); `local` → the same local
  model (for the H2 ablation).
- The **executor** is the existing `scripts/local_agent.py` subprocess, unchanged
  except its system/task prompt is augmented: "Here is your step plan. Work it in
  order. After finishing each step, update `.agent_scratchpad.md` with what you did
  and what's left. Do not skip ahead." No new agent framework.
- **Persistence:** two new artifacts in the worktree `cwd`, written with the same
  atomic pattern as `.agent_transcript.json` so reworks (which reuse the worktree)
  find them:
  - `.agent_plan.md` — the checklist. Written once by the planner; the executor
    checks off steps.
  - `.agent_scratchpad.md` — running state the executor appends after each sub-step
    (H3 lever; can be disabled independently to test its contribution).
  Both added to `.git/info/exclude` alongside the existing agent artifacts
  (`backend.py` ~L476) so they never dirty the tree and block a merge rebase
  (Mode 17 guard).

### 3.2 Why this dodges the PA-validation failure

| PA-validation (Condition D, disproven) | This plan |
|---|---|
| Splits into N *pipeline stories* | Splits into N *sub-steps in one story* |
| Each sub-step is a **cold dispatch** | One warm transcript across all sub-steps |
| Context fragmented (repetition guard, merged-but-wrong) | Context whole; scratchpad carries state |
| Splitter = product-analyst (plan-time, no repo view) | Planner = tech-lead (dispatch-time, sees the actual repo/target file) |
| Tested step *count* | Tests guidance *density* + planner *strength* |

### 3.3 What the planner must and must not do

- **Must:** produce 3–7 concrete, verifiable sub-steps for the *given* story;
  order them; give each a one-line done-criterion; keep TDD ordering (failing test
  before impl) explicit per the project's Step 3.
- **Must not:** invent scope beyond `agent_instructions` (Core Principle: minimal
  footprint), touch the acceptance/oracle fixture, or emit new pipeline
  stories/dependencies. It is a *within-story* brief, full stop.
- Failure handling: if the planner call errors or returns an unparseable/empty
  plan, **fall back to the current no-plan path** (fail-open to *existing behavior*,
  never fail-closed into a broken dispatch — the executor already works without a
  plan today).

---

## 4. Proving it — harness expansion

Reuse the compound-harness discipline already built for PA-validation
(`tests/benchmark/compound_harness.py`, `harness.py::drive_plan` /
`build_plan_from_stories`, hidden `acceptance.py` + independent `groundtruth.py`).
Grade only on the independent groundtruth the model never sees.

### 4.1 New conditions (per task, same requirement, same groundtruth)

- **M — monolithic control:** coarse story, no plan. (Existing baseline.)
- **G-cloud — guided, cloud planner:** coarse story + `.agent_plan.md` from the
  principal model + scratchpad. (Primary H1 + H2 arm.)
- **G-local — guided, local planner:** same, but the local model authors the plan.
  (H2 ablation — is planner strength the active ingredient?)
- **G-cloud-noscratch — checklist only:** cloud plan, scratchpad disabled.
  (H3 ablation — does the persistent memory matter, or just the checklist?)
- **D — story-split** (optional, reference only): the already-tested product-analyst
  decomposition, to keep the disproven baseline visible in the same table.

### 4.2 Task selection — deliberately fix the prior experiment's blind spot

The PA-validation task (`ratelimiter_inspect`) had a single hard reasoning nugget
that dominated the result. To fairly test *guidance*, include tasks whose
difficulty is **breadth / state-tracking across several independent easy-ish
concerns** — where a checklist has real work to do — while *keeping the hard task
as a control* to confirm we don't regress and to reconfirm lesson (1).

- **Reuse:** `ratelimiter_inspect` (known-hard, reasoning-dense) — control.
- **Author 1–2 new compound tasks** whose concerns are independent and
  context-heavy rather than reasoning-heavy, e.g. a small module requiring several
  wired-together methods + validation + a metrics accessor, each easy alone but
  easy to *lose track of* in one pass. Each needs the standard pair (`acceptance.py`
  hidden oracle + independent `groundtruth.py`, both verified to agree on a correct
  reference impl and on a deliberately-broken "gamer" impl, per the existing
  "Adding a task" checklist) plus a `_MOCK_IMPLS` entry for the offline self-test.

### 4.3 Metrics

- **Correct-AND-merged rate** — the one that matters (groundtruth pass on the final
  merged repo). Everything else is diagnostic.
- **Steps consumed** (executor) and **wall time**.
- **Cost:** planner tokens (once) + executor tokens — so the principal-planning /
  jr-execution economics are explicit.
- **Where it stopped / which sub-step** — diagnostic for concentrated vs. spread
  difficulty.
- **Plan adherence** — did the executor actually follow `.agent_plan.md` order, or
  ignore it? (A null result could be non-compliance, not a bad hypothesis.)

### 4.4 Config discipline (carry over hard-won controls)

- Run on a **post-fix config** where the two confounds PA-validation surfaced are
  controlled: the "correct code fails to land" reviewer-nudge issue (`63e37cc`) and
  the merged-but-wrong gate gap. Otherwise results can't be attributed to
  decomposition. Verify the acceptance-oracle gate + a CI-equivalent re-check are
  active before trusting any merge in the run.
- `PIPELINE_BACKEND_REVIEW=local`, dispatch pinned `local` (no escalation) so we
  measure the *executor*, not Claude rescuing it — **except** the cloud planner call
  itself, which is the intervention under test.
- Rework cap 3 per story; cache one planner output per (task, condition) so we test
  "this plan" not planner variance (resampling planner quality is a separate,
  later question — same caching rationale as PA-validation).
- **Small n first (~5/condition), one or two tasks, stop on directional signal.**
  Do not loop a full matrix for hours (`feedback_experiment_scope_and_polling`).

### 4.5 Kill / ship criteria (decide before running)

- **Kill** if G-cloud does not beat M on correct-and-merged by a clear margin
  (e.g. ≥ +2 of 5) on **at least the breadth-heavy task** at n=5. If guidance can't
  help even where difficulty is breadth (not a single hard nugget), the line is dead.
- **Ship narrow** if the win localizes: e.g. if G-cloud-noscratch ≈ G-cloud, ship
  the checklist only (drop the scratchpad); if G-local ≈ G-cloud, ship the cheaper
  local planner.
- **Confounded / redo** if plan-adherence is low — fix the executor prompt to
  actually consume the plan before drawing a conclusion.

---

## 5. Implementation phases

Each phase is a pipeline story (`ingest_plan`), TDD, one concern per PR (< ~400 LOC),
gated through `review_story` → `approve_merge` per the Agent Workflow.

1. **Harness: guided conditions** (test-only, no production change).
   - Add G-cloud / G-local / G-cloud-noscratch condition plumbing to
     `compound_harness.py` (inject a pre-built `.agent_plan.md` + toggle scratchpad).
   - `_MOCK_IMPLS`-backed offline self-test first, before any real model time.
2. **New compound task(s)** (breadth-heavy) — `acceptance.py` + `groundtruth.py` +
   reference + gamer impl + `_MOCK_IMPLS`, verified to agree per the checklist.
3. **Planner step in `backend.py`** behind `PIPELINE_DECOMPOSE=off` (default).
   - `run_planner()` bounded call; `.agent_plan.md` write (atomic, git-excluded);
     executor prompt augmentation; scratchpad artifact; fail-open to current path.
   - Unit tests: plan written/parsed; empty/error → no-plan fallback; artifacts
     git-excluded; rework reuses existing plan (no re-plan on redispatch).
4. **Run the small experiment** (§4), record results in this file's Results section
   (mirror the PA-validation write-up style: scorecard table + honest reading +
   caveats). **Do not expand to a full matrix until directional signal is positive.**
5. **Ship-or-kill decision** per §4.5, recorded here.

---

## 6. Risks & open questions

- **Reasoning-density ceiling (biggest risk).** Per PA-validation lesson (1), if a
  story's difficulty is one hard reasoning step, no checklist helps. Mitigation:
  the breadth-heavy task selection in §4.2 targets where guidance *can* help; the
  hard control confirms where it can't. A negative on the hard task is expected,
  not a failure of the design.
- **Planner cost vs. escalation cost.** If the cloud planner call is a large
  fraction of just escalating the whole story to cloud, the economics collapse.
  Track planner tokens explicitly (§4.3); the pitch only holds if planning is cheap
  relative to execution.
- **Plan non-adherence.** Weak models may ignore the checklist. This is why
  plan-adherence is a first-class metric, not an afterthought.
- **Context-budget pressure.** The plan + scratchpad consume context window. On a
  24GB host, doubling ctx 8192→16384 already moved GT-correct 75%→100%
  (`project_provider_dispatch_s3`); adding plan/scratchpad text must not blow the
  budget. Keep artifacts terse; measure ctx headroom in the run.
- **Overlap with escalation.** This is orthogonal to cloud escalation: guided
  decomposition is a *cheaper local* lever tried *before* escalating. If it lifts
  local solo rate from ~50%, it reduces escalation frequency (the real cost win).
- **Open:** does the planner need repo context beyond the named target file(s)?
  Start minimal (file tree + target files); expand only if plans are visibly
  uninformed.

---

## 7. Relationship to prior work in this repo

- Builds directly on `tests/benchmark/PRODUCT_ANALYST_VALIDATION_PLAN.md` — treat
  its Results as the baseline this plan must beat, and its config controls as
  mandatory carry-overs.
- Reuses transcript persistence / rework-resume (`project_rework_resume_validation`).
- Complements, does not replace, cloud escalation and the acceptance-oracle gate.

---

## Results — first live run (2026-07-15, `tests/benchmark/_runs/guided_decomp_first_test/`)

Real `gptoss_temp03` (gpt-oss:20b, temp=0.3, num_ctx=32768) on the known reasoning-
dense control task `ratelimiter_inspect` (not yet the breadth-heavy task §4.2 calls
for — that's still unwritten). `PIPELINE_BACKEND_REVIEW=local` (gpt-oss reviews its
own work, matching PA-validation's post-fix config) to conserve Claude usage; the
G-cloud arm's *planner* call is the only Claude usage spent. n=3 per arm except
where noted. Graded on correct-AND-merged (groundtruth pass + actually landed).

### H1/H2 scorecard

| Condition | Trial | Final status | Merged | Groundtruth | Note |
|---|---|---|---|---|---|
| M (`--decompose off`) | 0 | parked | no | passed | review_verdict=UNKNOWN x2 (local self-review parse failure) |
| M | 2 | failed | no | passed (worktree) | APPROVE'd, but merge-gate CI caught 3 wrong self-authored test assertions (fractional refill/non-mutation/exact-drain) |
| M | 4 | done | **yes** | passed | — |
| **M total** | | | **1/3 (33%) correct-and-merged** | 3/3 groundtruth-correct | both failures were pipeline/self-review issues, not bad code |
| G-cloud (`--decompose cloud`) | 1 | done | **yes** | passed | APPROVE clean first try, 640s |
| G-cloud | 3 | done | **yes** | passed | 962s |
| G-cloud | 6 | done | **yes** | passed | 297s (retry of a killed t5, excluded) |
| **G-cloud total** | | | **3/3 (100%) correct-and-merged** | 3/3 | — |
| G-local (`--decompose local`, H2) | 7 | done | **yes** | passed | 810s |
| G-local | 8 | done | **yes** | passed | 468s |
| G-local | 10 | failed | no | **failed** (real bug) | rework cycle triggered, 2nd attempt landed a genuine refill/capacity bug groundtruth caught (retry of a killed t9, excluded) |
| **G-local total** | | | **2/3 (67%) correct-and-merged** | 2/3 groundtruth-correct | one GENUINE incorrect implementation, not a pipeline artifact |

One trial per of G-cloud (t5) and G-local (t9) was killed by the execution
environment mid-run (t5 before any real work; t9 at step 22, after 21 steps of
real progress, on a transient Ollama 500) and excluded — not counted as a failure
of either arm, retried under a new trial number instead.

### Reading it

**H1 supported, cleanly, on the first task tried.** G-cloud (3/3) clearly beats M
(1/3) on correct-and-merged — and notably, this is the known reasoning-dense
*control* task from PA-validation, not even the breadth-heavy task the plan
expected to favor the hypothesis. Both of M's failures were pipeline-landing
problems on CORRECT code (self-review parse failure; a self-authored-test bug that
CI caught pre-merge, exactly the defense-in-depth the merge gate exists for) —
G-cloud hit neither failure mode in 3/3 tries. Real qualitative confirmation the
mechanism works as designed: `.agent_plan.md` in every G-cloud trial held a
genuinely well-ordered, task-specific TDD checklist, and `agent.log` showed the
executor following it step-for-step.

**H2 (planner strength) is NOT clearly resolved — G-local trends weaker but the
one failure is a different KIND of failure, not just fewer of the same kind.**
G-local's two successes look qualitatively as good as G-cloud's (its self-authored
checklists were, if anything, more granular — explicit skeleton-then-implement
staging with a pytest check at every micro-step). But its one failure (t10) is
the first case in this whole run where the GROUNDTRUTH itself failed — a real
refill/capacity bug survived a rework cycle and reached the merge gate as "failed"
only because the merge-CI check caught it, not because the code was secretly
fine. That's a capability-level miss, not a landing-mechanics miss like M's
failures. Consistent with (not proof of) the hypothesis that planner *strength*
matters, not just having *a* checklist — but n=3 with one non-matching failure
mode is far too thin to call this either way. Needs a larger n and/or the
breadth-heavy task before trusting a direction here.

### Honest caveats

- n=3 per arm (n=2 effectively for H2 once the real-bug trial is treated as a
  distinct failure mode) is a strong directional read, not proof.
- Single task, single model (gpt-oss:20b) — matches PA-validation's own caveat
  verbatim: "not enough to generalize... only 'it didn't help here' [or here, 'it
  helped here']."
- This is still the reasoning-dense control task (§4.2), not the breadth-heavy
  task the plan's kill/ship criteria (§4.5) are actually keyed to. A clean win
  here is a good sign but doesn't yet satisfy the plan's own bar.
- Environment killed 2 of 11 real-model background runs outright (not a pipeline
  or model issue — no panics, no orphaned processes, Ollama healthy both times)
  — an operational annoyance for long unattended runs, unrelated to decompose.
- H3 (scratchpad's independent contribution) still untested.

## Results — MLX qwen replication (2026-07-15, `tests/benchmark/_runs/guided_decomp_mlx_test/`)

Same task (`ratelimiter_inspect`), same `PIPELINE_BACKEND_REVIEW=local` config, but
`--model mlx` — dispatch (and, confirmed by inspecting `_local_provider()`'s env,
review too, since `PIPELINE_LOCAL_PROVIDER`/`PIPELINE_LOCAL_ENDPOINT` are set
process-wide) both run against `Qwen2.5-Coder-14B-Instruct-4bit` served locally via
`mlx_lm.server` — the right-sized, stability-validated MLX model from
`MLX_DEFAULT_PROVIDER_PLAN.md`, previously measured 0/2 correct on this exact task.
n=1 per arm (small — see caveats) after the M baseline alone burned a full hour.

| Condition | Trial | Final status | Merged | Groundtruth | Elapsed | Note |
|---|---|---|---|---|---|---|
| M (`--decompose off`) | 1 | **interrupted/timeout** | no | **failed (16/16)** | 3601s (full budget) | 7+ failed dispatch attempts, stuck in a view/edit loop, never once ran pytest |
| G-cloud (`--decompose cloud`) | 2 | done | **yes** | **failed (1/16)** | **188s** | clean self-review APPROVE on a real bug: the exact speculative-future-peek mutation-safety case the oracle exists to catch |

MLX server itself: **stable throughout** (0 panics, confirmed via `mlx-server-wrapper.log`/`mlx-server.log` and process liveness checks across both trials) — the earlier `MLX_DEFAULT_PROVIDER_PLAN.md` stability fixes (right-sizing to 14B, wrapper lock removal) hold up under this workload.

### Reading it

**A different, and in a way more decisive, kind of result than the gpt-oss run: the checklist changed WHICH failure mode occurred, not whether one occurred.**
Unaided, this weaker model couldn't even produce a testable implementation — it
thrashed on `view_file`/`str_replace` cycles across 7+ dispatch attempts without
ever running its own tests, and simply ran out the full hour. With the checklist,
it converged fast (188s, its fastest result of the whole session) and got most of
the behavior right (15/16 groundtruth tests) — but landed the one genuinely subtle
requirement wrong (mutation-safety on `available_tokens()`'s speculative peek), and
its own self-review — running on the identical weak model — approved it anyway.

This is a clean empirical instance of the plan's own §6 risk ("reasoning-density
ceiling... if a story's difficulty is one hard reasoning step, no checklist
helps") and simultaneously a real, practical win: going from "never even reaches a
gradeable state" to "reaches done, mostly correct, in 3 minutes" is a large
improvement in usefulness even though it doesn't clear the correct-and-merged bar.
It also reinforces a point the gpt-oss H2 result already hinted at: **the review
step is a load-bearing, independent variable** — a merge gate that routes review to
a *different, stronger* model (the way the gpt-oss runs implicitly did, since
`PIPELINE_BACKEND_REVIEW=local` resolved to Ollama/gpt-oss reviewing Ollama/gpt-oss
work, still a capable model) would very plausibly have caught this bug before
merge; self-review by an equally-weak model is the actual point of failure here,
not the checklist mechanism.

### Honest caveats

- n=1 per arm — a single trial each, chosen deliberately after the M baseline's
  own single trial cost a full hour; treat this as a vivid illustrative case, not
  a rate.
- The comparison isn't perfectly clean: M never reached a state groundtruth could
  meaningfully grade as "wrong code" (it graded the never-finished worktree, 16/16
  fail, which mixes "didn't finish" with "was wrong") vs G-cloud's "finished, one
  specific behavioral bug." Both are real failures, but of different characters.
- Self-review-by-equally-weak-model is a confound worth isolating: an obvious
  follow-up is the same G-cloud trial with review routed to a stronger model
  (Claude, or gpt-oss) to see whether the checklist's gain survives a review gate
  that can actually catch the bug.
- 3 background-task terminations occurred across this session's real-model runs
  (unrelated to any model or to decompose — no panics, no orphaned processes,
  healthy retries each time); noted as an operational nuisance for long unattended
  runs, not a finding about the pipeline or the models.

## Results — self-review confound isolated (2026-07-15, trial `t3`)

Added `PIPELINE_DECOMPOSE_CLOUD_MODEL` (env override, defaults to the existing
overlord/opus tier when unset; 1 new TDD test, `test_run_planner_cloud_mode_
honors_model_override`, 956 total tests green) so the cloud planner's tier is
independently controllable from the reviewer's. Re-ran the identical MLX-qwen
G-cloud config (`--decompose cloud`, same task/dispatch) but with
`PIPELINE_DECOMPOSE_CLOUD_MODEL=sonnet` (planner) and `PIPELINE_BACKEND_REVIEW=
claude` (reviewer resolves to Sonnet via the code-reviewer persona's declared
tier) instead of local self-review.

| Condition | Trial | Final status | Merged | Groundtruth | Review verdict | Note |
|---|---|---|---|---|---|---|
| G-cloud, self-review (local) | 2 | done | **yes** | failed (1/16) | APPROVE | speculative-peek mutation bug missed |
| G-cloud, Sonnet plans + Sonnet reviews | 3 | **parked** | **no** | failed (1/16, different bug) | **REQUEST_CHANGES** | Sonnet found 2 real bugs directly (not just from the given tests) on its **first and only** review pass |

### Reading it

**Confirmed: the merged-but-wrong result was a self-review failure, not a
decomposition failure.** With an independent, stronger reviewer in the loop, the
identical weak-model dispatch config never reaches a false merge — Sonnet's
review feedback explicitly states tests were green but *"green tests here don't
prove correctness — the acceptance oracle's scenarios happen to mask two real
bugs I found by exercising the module directly,"* the same kind of reasoning
`MLX_DEFAULT_PROVIDER_PLAN.md` separately documented as "an exemplary review."
The checklist mechanism and the review-quality question are cleanly separable
variables, and this run separates them: guided decomposition helped this weak
model reach a gradeable state fast (both trials); an equally-weak self-review
is what let a bug through, and a competent independent reviewer is sufficient
to stop it — parking (a safe, correct outcome) rather than merging wrong code.

This also reframes what "MLX qwen + guided decomposition" is actually worth in
practice: not "correct code," but "a fast, mostly-right draft that a competent
reviewer can catch problems in" — which is a real, usable position in the
pipeline (assuming review is never also weak), just not the same claim as
correct-and-merged on the first try.

**Correction (2026-07-15, caught during the H4 write-up below):** trial `t3`
parked after exactly **one** review pass, not "3 rework cycles" as an earlier
draft of this section claimed. Oracle-graded stories use
`REWORK_MAX_ATTEMPTS_ORACLE` (default **1**, never overridden in today's runs
- unlike the earlier PA-validation script, which explicitly set it to 3), so
`attempts (1) >= rework_cap (1)` parks the story on the FIRST `REQUEST_CHANGES`
- it never transitions to `changes_requested`, so no rework redispatch, and no
resume-via-transcript, ever occurs. The verdict/reasoning reported above (Sonnet
catching 2 real bugs and declining to approve) is accurate; only the "how many
cycles" detail was wrong. This also means every REQUEST_CHANGES outcome
anywhere in today's session (t2 gpt-oss trials, t3, t4 below) reflects a single
review pass, not a multi-cycle rework loop, unless stated otherwise.

### Session wrap-up (2026-07-15)

Across ~3 hours of real local-model wall-clock this session: **H1 supported**
cleanly on gpt-oss (3/3 vs 1/3, both M failures were landing-mechanics not code
bugs); **H2 inconclusive** (G-local 2/3, one qualitatively different failure);
**MLX replication** showed a starker contrast (total non-convergence vs a fast
mostly-correct draft) that also surfaced the self-review confound; **isolating
that confound** confirmed a stronger independent reviewer is sufficient defense
even when both decomposition and dispatch run on a weak model. No panics, no
merged-but-wrong slipped past a competent reviewer, all findings written up
with honest caveats (n is small throughout — directional, not proof).

## H4 — rework-feedback decomposition (added + tested 2026-07-15, inconclusive)

Extended the same tech-lead-decomposition logic to the rework step: a reviewer's
prose feedback is itself a coarse brief for a weak executor, so
`_run_rework_planner()` (new, mirrors `_run_planner()`) translates
`review_feedback` into an ordered fix-checklist before it's appended to the
rework redispatch prompt, gated behind the same `PIPELINE_DECOMPOSE` flag.
Added `PIPELINE_DECOMPOSE_CLOUD_MODEL` env override alongside it so the cloud
planner's tier is independently controllable from the reviewer's (defaults to
the existing overlord/opus tier when unset). 962 tests green (7 new TDD tests:
cloud/local mode resolution, fail-open, integration with a mocked rework
planner, and a byte-for-byte regression guard that `PIPELINE_DECOMPOSE=off`
leaves the existing raw-feedback rework path completely unchanged).

**Correction caught mid-session:** an earlier trial (`t3`) was mis-reported as
"3 rework cycles" — see the correction inline above. Oracle-graded stories
default `REWORK_MAX_ATTEMPTS_ORACLE` to **1**, so a story parks on its FIRST
`REQUEST_CHANGES` and never redispatches at all unless this is explicitly
raised. This is why `t3`/`t4` never could have exercised H4 regardless of the
new code's correctness — the mechanism requires a rework redispatch to ever
fire, and the default config never grants one.

**Testing attempts (`PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE=3`, MLX qwen 14B
implementer, Sonnet plans + reviews):**

| Trial | Outcome | Rework fired? |
|---|---|---|
| t5 | Dispatch hung mid-attempt (see wedge diagnosis in `MLX_DEFAULT_PROVIDER_PLAN.md`); killed after 13+ min stuck. Worktree code was actually correct (16/16 GT) but never reached review. | No — never got past attempt 1 |
| t6 (guarded re-run, session supervisor active) | done, merged, groundtruth passed (16/16), 408s. Manifest: `review_verdict: APPROVE`, no `rework_attempts` field at all. | **No — approved clean on the first pass** |

**H4 remains genuinely untested after 6 real MLX trials today (t2-t6 plus the
original t3/t4 misconfiguration).** Every trial either converged and got
APPROVE on the first attempt (t2, t6) or never reached a completed review at
all (t3/t4 via the cap=1 bug, t5 via the hang). None has yet produced the one
condition H4 needs to say anything: a REQUEST_CHANGES verdict with the rework
cap actually open. This isn't evidence against the hypothesis — it's a
sampling gap. The fastest path to a real answer is deliberately forcing a
REQUEST_CHANGES on attempt 1 (e.g. a task/config combination more likely to
trip Sonnet's review, or simply more trials at cap=3) rather than hoping for
one to occur naturally.

### Next

- H4 needs a trial that actually reaches a rework redispatch — not yet
  achieved; worth deliberately engineering rather than continuing to sample.
- H3 (scratchpad's independent contribution) still untested on any model.
- The breadth-heavy task from §4.2 is still unwritten — the one task type most
  likely to show decomposition's benefit cleanly on either model family, and
  the plan's actual kill/ship criteria (§4.5) are keyed to it, not to
  `ratelimiter_inspect`.
- Worth a larger-n pass on gpt-oss H1 given how clean the n=3 signal was.
- Operational: MLX server wedges (see `MLX_DEFAULT_PROVIDER_PLAN.md`) are real
  and recur under load; any future MLX run should have the supervisor running
  periodically first (persistent launchd job, not yet installed — session
  loop only was used today) or the wedge will silently eat a trial's timeout
  budget.

## H4 continued — 6-task diversity sweep (2026-07-15, still zero H4 trials)

Same config as the H4 attempts above (MLX qwen 14B implementer, Sonnet plans +
reviews, `PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE=3`), but across 6 *fresh* tasks
(`harness.py`'s single-story path, which reports `review_verdict`/
`rework_attempts` directly in `result.json` — no manifest digging needed) the
model had not seen today, instead of re-running `ratelimiter_inspect` a 7th
time. Session-scoped supervisor loop guarded the whole sweep (180s probe
timeout); caught one real wedge mid-run (`interval_merge`, `restarted_wedged`
at 17:05:44) and recovered it within one tick — no manual intervention needed
this time, unlike `t5`.

| Task | Final status | Review verdict | Rework attempts | Groundtruth |
|---|---|---|---|---|
| `cron_field` | failed | **null (never reviewed)** | 0 | 15/16 — step-range parsing bug |
| `interval_merge` | failed | **null (never reviewed)** | 0 | 0/12 — left as `NotImplementedError` stub |
| `lru_cache` | failed | **null (never reviewed)** | 0 | didn't run — impl file never created |
| `ratelimiter_bugfix` | interrupted (3600s timeout) | **null (never reviewed)** | 0 | 5/7 — refill-double-count bug remained |
| `retry_backoff` | **done, merged** | APPROVE | 0 | 28/28 |
| `token_bucket` | failed | **null (never reviewed)** | 0 | 14/15 — the same forward-reject double-count refill bug class the oracle gap fix (`MLX_DEFAULT_PROVIDER_PLAN.md`) was built to catch |

**Zero of 6 reached a REQUEST_CHANGES + rework redispatch. H4 remains
completely untested — now across 12 real MLX trials today** (the 6 earlier
`ratelimiter_inspect` repeats plus these 6). One dispatch_attempts/
rework_attempts field consistently reads 0 whether the story succeeded or
failed, confirming none of them ever redispatched.

**Why: on a fresh (not-yet-practiced) task, this weak model is strongly
bimodal, not "close but fixable."** 5/6 never even reached a state worth
reviewing — they failed inside their OWN dispatch loop (never got their own
tests to pass, or ran out the wall-clock/step budget first) — real capability
misses on subtle correctness (refill double-counting appeared independently in
2 of the 3 rate-limiter-shaped tasks), not near-misses a reviewer's feedback
could plausibly patch. The 1/6 that succeeded did so cleanly, first try, no
review friction at all. This is a materially different picture from earlier
today's `ratelimiter_inspect` reruns (where the model had effectively
practiced the same task 6+ times and *did* occasionally reach a reviewable-but-
wrong state) — task novelty, not just raw difficulty, appears to matter for
whether this model ever produces something worth sending back for rework.

**Implication for H4 testing strategy:** continuing to sample fresh tasks and
hoping for a REQUEST_CHANGES is unlikely to be efficient — the natural rate
observed today is roughly 0/12 exactly-in-the-window trials (getting a
completed-but-flawed review) out of 18 total real trials. Testing H4 for real
probably needs to be engineered rather than sampled: e.g. seed a deliberately
near-miss starting point, lower the model's own step budget so it's forced to
submit something incomplete-but-testable for review sooner, or specifically
target `ratelimiter_inspect`/similar tasks where a first-pass REQUEST_CHANGES
has already been observed to occur reliably with Sonnet reviewing.

## Checklist quality upgrade: worked examples close the gap (2026-07-15)

User's hypothesis after the 6-task sweep: if the checklist mechanism exists to
help a weaker model, and a strong cloud tech lead genuinely knows the executor
is weaker, the checklist should reflect that the way real engineering
management does — flagging exactly which parts need extra care, not just
listing steps. Upgraded `_PLANNER_SYSTEM` and `_REWORK_PLANNER_SYSTEM`
accordingly: for any step involving timing/state-mutation/an edge case that's
easy to get subtly wrong, the tech lead must now include **a concrete worked
numeric example of the correct result and name the specific mistake a less
careful implementation would make there** (not just "handle edge cases").
Also relaxed the step-count guidance (3-10, split rather than bundle tricky
reasoning) instead of a flat 3-7. Both prompts changed only (no code-path
change); 962 tests green (they reference `p._PLANNER_SYSTEM`/
`p._REWORK_PLANNER_SYSTEM` symbolically, not by literal text).

**Test: re-ran `ratelimiter_bugfix` (MLX qwen 14B, Sonnet plans+reviews) —
the exact task/config that failed a few hours earlier under the old prompt.**

| | Old prompt (`t0`, earlier today) | New prompt (`t1`) |
|---|---|---|
| Final status | interrupted (full 3600s timeout) | **done, merged** |
| Groundtruth | 5/7 | **7/7** |
| Review | never reached | **APPROVE** |
| Elapsed | 3600s | **569s** |

**Clean, decisive result.** Under the new prompt, Sonnet's checklist didn't
just list more steps — it diagnosed the actual root cause itself: traced
`self.last_time` to being written only on the rejection branch, worked
through the exact wrong arithmetic (`elapsed = 0.5 - last_time(0.0) = 0.5`
instead of the correct `0.5 - 0.5 = 0`), explicitly ruled out a plausible
wrong diagnosis ("this is *not* a refill-rate math error"), specified the
one-line fix, and pre-empted a specific wrong fix (special-casing "the second
call") with a third-call verification scenario designed to catch exactly that
shortcut. **Verified the merged `ratelimiter.py` implements that exact
prescribed fix line-for-line** - the model followed the diagnosis, it didn't
independently re-derive a different correct solution.

**This refines the earlier "reasoning-density ceiling" framing from today.**
It's not that this class of bug is unconditionally beyond the model - it's
that a checklist which only names *what* to build (matching real
engineering-management practice for how much detail to hand a mid+ engineer)
leaves the hard reasoning step to the weak model, and that's where it failed.
A checklist that does the diagnostic reasoning up front and hands over a
worked example shrinks the model's job to "faithfully transcribe this
specific, fully-specified fix" - which it's clearly capable of. That's a
materially different (and stronger) mechanism than "more decomposition
steps," and it's much closer to how a good tech lead actually manages a
junior engineer on a subtle bug in practice.

**Caveats:** n=1 (one task, one re-run) - a strong directional result, not
proof it generalizes. The comparison is clean (same task, same model, same
config, only the prompt changed) but doesn't yet tell us whether this level
of detail is necessary for ALL of today's failure types (e.g. `interval_merge`'s
stub/`NotImplementedError` and `lru_cache`'s missing-impl-file look more like
attention/completion failures than reasoning failures, and may not respond the
same way to worked examples). Worth re-testing the other 3 fresh-task failures
from the 6-task sweep under this same upgraded prompt before generalizing
further.

## Final re-test results + two production bugs found (2026-07-15)

Re-ran the remaining 2 fresh-task failures under the worked-example prompt.

| Task | Old (t0) | Enhanced prompt, 1st clean attempt | Root cause found | After fix, retest |
|---|---|---|---|---|
| `interval_merge` | failed, 0/12, stub | **done, merged, 12/12, APPROVE** | n/a - worked first try | n/a |
| `ratelimiter_bugfix` | interrupted/timeout, 5/7 | **done, merged, 7/7, APPROVE** | n/a - worked first try | n/a |
| `cron_field` | failed, 15/16, never-reviewed | failed, 0/16, `SyntaxError` (t1, wedge-confounded) → failed, 0/16, `SyntaxError` (t2, clean) | **Bug #1**: `ast.parse()` doesn't catch `return`/`yield` outside function | t3 (fix live): still failed - different failure mode (never wrote the impl, absorbed by its own 9+-case test-writing requirement) |
| `lru_cache` | failed, never-reviewed, no impl file | failed, no impl file (t1) | **Bug #2**: `create_file`'s own membership in `MUTATING_TOOLS` cleared its repetition count on every call | t2 (fix live): nudge fired (proof the fix works live), impl file created, 3/8 GT passing (`get` correct, `put` left as `NotImplementedError`) - genuine partial progress, still fails |

**Score: 2/4 fully fixed by the prompt alone; the other 2 had real infrastructure
bugs underneath, both found and fixed with TDD, both independently verified to
engage in live trials — but neither bug fix alone was sufficient to flip
`cron_field`/`lru_cache` to success.** These two tasks may simply be harder for
this 14B model regardless of guidance quality (see per-task notes below).

### Bug #1 — syntax guard used `ast.parse()`, which misses semantic-only SyntaxErrors

`scripts/local_agent.py` and `scripts/local_agent_oracle.py`'s
`_python_syntax_error()` used `ast.parse(content)` to validate submitted code
before writing it to disk. `ast.parse()` only validates grammar (balanced
parens, legal indentation structure) — it does **not** catch `return`/`yield`
outside a function, `break`/`continue` outside a loop, or similar
semantic-only errors, which are real `SyntaxError`s but only surface at
`compile()` time. A dedented `for` loop landed `return` at module scope in a
live `cron_field` trial; `ast.parse()` let it through, the file reached the
merge/groundtruth gate as an import-breaking `SyntaxError` (`test_groundtruth.py`
couldn't even be collected). Verified directly: `ast.parse()` on the broken
file raised nothing; `compile(content, path, "exec")` raised `SyntaxError:
'return' outside function` at the correct line. Fix: swap `ast.parse()` for
`compile(content, path_str, "exec")` in both files (identical guard contract,
same error-message formatting). 2 new TDD tests (one per file, mirrored
naming convention), 966 tests green.

### Bug #2 — the repetition guard cleared its own signature's count on every mutating call

The per-target repetition guard (`seen[sig] >= 3` → nudge → park) is meant to
catch a model stuck repeating the same failing action. Its "clear on mutation"
logic (`if fn in MUTATING_TOOLS: seen.clear()`) exists to invalidate *stale
read-counts* after a real edit (see the 2026-07-04 gpt-oss false-positive-park
fix already in this codebase) — but `create_file` is itself one of
`MUTATING_TOOLS`, so this same line ran on every `create_file` call
**including consecutive identical ones**, wiping out that signature's own
count before it could ever reach the threshold. A model resubmitting
`create_file` against an already-existing path (rejected every time by the
non-destructive-editor guard, which explicitly suggests `str_replace` instead)
could repeat this **forever** with the guard permanently inert — observed
live: 34 consecutive rejected `create_file` calls burned an entire trial's
step budget with zero nudges. Verified the bug by hand-tracing the exact
`seen.clear()` ordering, then reproduced it in a unit test (6 `create_file`
calls to a pre-seeded existing path → 0 nudges, pre-fix). Fix: increment the
current signature's count *before* clearing, and when clearing, preserve
*only* the current signature's just-incremented value instead of zeroing
everything unconditionally. 2 new TDD tests (one per file), 966 tests green,
confirmed the pre-fix version of both new tests fails for the predicted
reason before the fix and passes after.

**Known residual gap (not fixed today):** the fix closes *consecutive*
repetition but not repetition *interleaved* with genuine progress. Since
`str_replace` is also in `MUTATING_TOOLS`, a `str_replace` call between two
`create_file` attempts on the same path still clears `create_file`'s
accumulated count (my fix only preserves the *current* call's own signature,
not signatures from other recent mutating calls) — observed live in the
`lru_cache` t2 retest: `create_file: lru_cache.py` recurred roughly every 5
steps, each time reset to count 1 by intervening `str_replace` calls, so the
guard's nudge fired only once for the whole run rather than catching each
individual wasted attempt. A cleaner fix would gate the clear on the tool
call's actual *outcome* (only a genuinely successful write should invalidate
prior tracking, not merely being a "mutating" tool name) rather than on tool
identity alone — deferred as a follow-up given today's TDD evidence that the
current fix already closes the dominant, most damaging case (pure consecutive
resubmission) and materially improved the live retest (impl file went from
never-created to created-and-partially-correct).

### Reading the whole day

The checklist quality upgrade (worked examples + naming the specific mistake)
is real and validated - 2 of 4 previously-failing fresh tasks flipped to clean
first-try success, no reasoning-density argument needed once the tech lead did
the diagnostic work up front. But today's deepest, most durable value turned
out to be two genuine, previously-unknown production bugs in the local-agent
harness itself, found only by refusing to accept "the model is just bad at
this" at face value and tracing failures to their literal root cause instead.
Both bugs are general-purpose fixes that benefit every future dispatch on any
model, independent of the guided-decomposition question this plan set out to
answer.

## Two more targeted fixes from the lru_cache deep-dive (2026-07-15)

Digging into the ACTUAL lru_cache t2 transcript (not just the summary)
revealed the real story was worse than reported: `get()`/`put()` were BOTH
still `raise NotImplementedError` — the earlier "3/8 passing, get() correct"
read was wrong (the 3 passes were just constructor-validation/empty-size
cases). ~90 steps across two dispatches produced almost nothing. The
transcript showed a precise, repeating 4-step deadlock: `create_file`
(rejected: exists) -> `str_replace` (rejected: old_str occurs in the file
hundreds of times / not found) -> `str_replace` (rejected again) -> `pytest`
(1 failure) -> repeat. The model never got past *editing mechanics* to
*logic* — the checklist's worked examples were irrelevant because it never
reached them.

**Fix #3 — gate the repetition-guard clear on actual SUCCESS, not tool
identity.** The residual gap flagged in the prior write-up was worse than
described: `if fn in MUTATING_TOOLS: seen.clear()` ran on every mutating call
REGARDLESS of whether it succeeded, so (a) create_file's own repeated
FAILURES kept wiping their own count (the original bug), AND (b) a FAILED
str_replace interleaved between failed create_file attempts also wiped
create_file's count — meaning the guard was inert not just for pure
consecutive repetition but for the *actual* observed pattern (create_file /
str_replace / str_replace / bash, repeated). Fixed by moving the clear to run
*after* the tool call, gated on the result not starting with `"ERROR"`.
Critically, this only applies to the mutating-tool clear — the read-tool
(`view_file`/`bash`) per-target counting is untouched, since treating every
successful *read* as "progress" would defeat the read-repetition guard
entirely (repeatedly viewing an existing file always "succeeds"). 2 new TDD
tests per file (`test_*_interleaved_failed_mutations_still_trip_repetition_
guard`) reproduce the exact observed pattern; pre-fix they fail identically
to the live incident (zero nudges across 8 scripted calls); post-fix both
pass.

**Fix #4 — let `create_file` overwrite a file the agent created earlier in
the same run.** The non-destructive-editor guard exists to protect
pre-existing repo/seed files from being clobbered by a confused model — it
was never meant to also block the agent from overwriting its *own* file. A
weak model that can't construct a correct `str_replace old_str` (as seen
directly in the transcript: "old_str occurs 377 times", "old_str not found")
has "rewrite the whole small file" as its only realistic recovery strategy;
forcing a surgical-edit protocol on a model that can't reliably produce one
just deadlocks it — this is the literal mechanism behind the observed
failure. Added `_CREATED_THIS_RUN: set[str]`, populated on every successful
`create_file`; the "already exists" rejection is now skipped for paths in
that set. Scoped to the current process's lifetime (module-level, naturally
resets on every fresh dispatch/rework subprocess), so a REWORK's *inherited*
file — which usually needs a surgical fix, not a wholesale rewrite, per
today's successful `ratelimiter_bugfix` checklist — stays protected until the
agent creates it again itself in the new process. 2 new TDD tests per file
(overwrite-allowed + pre-existing-file-still-protected regression), plus a
real test-isolation bug caught and fixed along the way (`_CREATED_THIS_RUN`
is module-level shared state across pytest's process, so tests needed
explicit `monkeypatch.setattr(..., "_CREATED_THIS_RUN", set())` resets,
matching the existing `_SYNTAX_REJECT_COUNTS` convention).

972 tests green after both fixes (up from 966).

**Next: re-validate `lru_cache` live** with both fixes in place, to see
whether closing the actual deadlock mechanism (not just symptoms) lets the
model reach real LRU logic this time.

### Live validation (`lru_cache` t3, 2026-07-16) — fixes didn't move this
particular trial, and the transcript shows why

Re-ran the identical config (MLX qwen2.5-coder-14B implementer, Sonnet
tech-lead + reviewer, `PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE=3`) with Fix #3 and
Fix #4 in place. Result: `final_status: failed`, parked after a single
repetition nudge, `lru_cache.py` never landed on disk. On its face this looks
like a repeat of the same deadlock — it is not. Pulling the actual
`.agent_transcript.json` (not just the tick log) shows a different, more
fundamental mechanism:

- The model called `create_file(lru_cache.py, ...)` four times. All four
  submissions were **byte-for-byte identical** (verified via direct string
  comparison) — the same `@property` line followed by a `def size(self):`
  dedented back to column 0, tripping the syntax guard's "unexpected
  unindent" check every time.
- Attempt 3 triggered the repetition nudge ("You have repeated the same
  action 3 times... regenerate the ENTIRE file from scratch"). The model's
  *next* action was to run `pytest`, then `view_file` (which correctly
  reported the file doesn't exist, since every create_file had failed), then
  submit — the exact same broken content a fourth time. That fourth attempt
  hit "parked: repeated action after nudge" (`local_agent.py:792`), which
  parks on the *first* repeat after a nudge, not after another 3.
- Neither fix had a chance to engage. Fix #3 only helps when a mutation
  interleaved between failures *succeeds* (clearing a false-positive count) —
  here every single create_file call failed, so the success-gated clear
  branch never ran; the counter climbed 1/2/3(nudge)/4(park) exactly as it
  would have pre-fix. Fix #4 only helps when the guard blocks overwriting a
  file the agent already wrote — here the file was never successfully
  written even once, so there was nothing to overwrite; the rejection every
  time was the syntax guard, not the non-destructive-editor guard.

**Honest read:** this trial exposed a third, distinct failure mode neither
fix targets — the model reproducing an identical syntax mistake verbatim
across multiple attempts, including immediately after being told exactly
what was wrong and instructed to regenerate from scratch. That is not an
editing-mechanics deadlock (t2's failure mode, which Fix #3/#4 do address);
it is the model failing to incorporate corrective feedback into a
regeneration at all. A finer-grained checklist or a friendlier editing
protocol can't fix a case where the model emits the same tokens regardless
of what it's told. The one lever this trial suggests and we haven't tried:
echoing the *exact rejected line* back in the syntax-guard error (already
partially done — the error message does show line 23 and the offending
line) — apparently isn't enough signal; a next step worth testing is having
the nudge include a *corrected, ready-to-submit* replacement line rather
than asking the model to regenerate the whole file from memory.

Fix #3 and Fix #4 remain correct, verified, narrowly-scoped fixes for the
bugs they were built for (confirmed by unit tests and, for the simpler
version of Fix #3's mechanism, by t2's real nudge-fires-and-makes-progress
result). They are not a general solution to weak-model syntax stubbornness,
which is now confirmed as a separate, unresolved failure mode.

### The "can't incorporate feedback" diagnosis was wrong — it was greedy
decoding (lru_cache t4, 2026-07-16, temperature A/B)

A re-read of the t3 transcript + a config trace changed the diagnosis of
the open problem. t3 ran at **temperature 0.3** (the `mlx` registry entry in
`tests/benchmark/models.py` sets no temperature override, unlike `gptoss`'s
explicit `1.0`; it inherits the default `PIPELINE_LOCAL_TEMPERATURE=0.3`;
`backend.py:_tuned_temperature` honors that env var over everything; the
harness merges the model's env via `os.environ.update(model_cfg["env"])`).
At temp 0.3 the decoder is near-greedy. **Byte-for-byte identical
reproduction of a 400-token file four times is the expected signature of
near-greedy decoding, not a model that "heard the feedback and chose to
ignore it."** The corrective nudge changes the model's input but, at low
temp, barely shifts the sampled tokens — the file content is dominated by
the (fixed) task prompt, so the argmax path re-derives the same broken
file. The "can't incorporate feedback" framing I gave in the prior
section was a misdiagnosis of a decoding artifact.

To confirm, ran **lru_cache t4 = t3 with ONLY temperature changed 0.3 →
1.0** (same 14B model — `MLXProvider.chat` omits the `model` field from
the request body by design, so the server always generates with its
loaded 14B regardless of the recorded `dispatched_model` tag; I set
`BENCH_MLX_TAG` truthfully this run so the bookkeeping field is no longer
the misleading 1.5B default).

**Result: the determinism lock broke — but the task still failed, for a
DIFFERENT reason.** Head-to-head:

| | t3 (temp 0.3) | t4 (temp 1.0) |
|---|---|---|
| `lru_cache.py` lands? | No — 4× byte-identical broken `create_file`, syntax guard rejected each | Yes — valid Python on the first `create_file` attempt |
| Determinism lock? | YES (byte-identical ×4) | NO — output varied, lock escaped |
| What landed | (nothing) | a valid-but-incomplete stub: only `__init__`, no `get`/`put`/`size` |
| Steps used | 8 (parked on repetition guard) | 60 (hit the 900s dispatch wall-clock timeout) |
| Wall time | 203s | 944s |
| `final_status` | failed | failed |

So the experiment cleanly separates the open problem into **two distinct
components**:

1. **The determinism lock (temp 0.3)** — REAL, and fixable by raising
   temperature. At temp 1.0 the model did not reproduce identical content;
   it produced a syntactically valid file on the first try. The
   byte-identical t3 failure was a decoding artifact, not a capability
   ceiling. **Confirmed.**

2. **A genuine completion/steering floor** — even with the lock broken,
   the model cannot drive the file to a complete, correct implementation.
   t4 landed an `__init__`-only stub and then spent **56 consecutive
   `str_replace` calls editing the WRONG file** — `test_lru_cache.py`
   (its own test file), not `lru_cache.py` (the implementation) — until
   the 900s dispatch timeout killed it. Higher temperature made this
   *worse* in wall-time (944s vs 203s, 4.6×): because `str_replace` is
   excluded from the repetition guard (each edit changes the file, so it
   is treated as legitimate iteration), an unbounded edit-loop on the
   wrong target is never parked. This is exactly the "higher temp → more
   variety, not more correctness → more flailing" downside that the
   `gpt-oss` A/B note in `backend.py:_LOCAL_MODEL_TUNING` warned about
   (temp 1.0 → 3 cells where the impl file never landed vs 1 at temp 0.3),
   now reproduced on qwen-coder-14B.

**Honest read of the three pre-registered outcomes:** this is a MIX of
(a) and (b/c), not a clean win for either side. The determinism lock was
real and is broken by temperature (outcome a, confirmed). But raising
temperature did NOT solve the task — it exchanged a fast determinism-lock
failure for a slow flailing-on-the-wrong-file failure (outcome c-ish), and
the model never reached real `get`/`put`/`size` logic at either
temperature (a genuine capability/steering floor, outcome b). **Neither
0.3 nor 1.0 is a good operating point for this model on this task:** 0.3
locks onto a broken file, 1.0 lands a stub and flails on the test file.

**What this refines about the open problem:** the lever is NOT "inject a
corrected line into the nudge" (that was predicated on the wrong
diagnosis — the model wasn't ignoring the nudge, greedy decoding was
re-selecting the same tokens). The real open problem is *steering the
model toward completing the implementation in the correct file*, not
*breaking a reproduction loop*. Two concrete next directions this
suggests, neither yet tested: (1) a checklist/tech-lead instruction that
explicitly forbids editing the test file and directs all edits at the
implementation file — t4's 56-step test-file edit loop is a steering
failure a stronger checklist could plausibly prevent; (2) a middle
temperature (~0.6–0.7) that breaks the determinism lock without the full
1.0 flailing — the A/B data only tested the two extremes (0.3 and 1.0),
both bad here; the optimum for qwen-coder may lie between them.

### Steering checklist validation (lru_cache t5, 2026-07-16) — steering
WORKED, but the run was dominated by a recurring str_replace-mechanics
failure, not a clean capability test

Implemented the steering directive in `_PLANNER_SYSTEM` (and
`_REWORK_PLANNER_SYSTEM`): "all implementation work goes in the ONE
implementation file named by the task; NEVER edit/rename/weaken/delete the
test files; if a test fails the bug is in the implementation," and the
planner is told to make this the checklist's FIRST line. New regression
guard `test_planner_system_steers_away_from_editing_test_files`; 973
tests green. Ran t5 = t4's exact config (temp 1.0, 14B, Sonnet
planner+reviewer, rework cap 3) + the steering checklist.

**Three things worked, confirmed in the artifacts:**

1. **The steering reached the agent and changed its behavior.** Sonnet's
   generated `.agent_plan.md` leads with the steering line verbatim ("All
   implementation goes in `lru_cache.py` — never edit, rename, weaken, or
   delete `test_lru_cache.py`..."). t4 spent 56 `str_replace` calls on
   `test_lru_cache.py` (the test); **t5 spent all 8 of its `str_replace`
   calls on `lru_cache.py` (the implementation)** — the file-targeting
   flipped. The steering hypothesis is confirmed: the checklist steers the
   weak model to the right file.

2. **Fix #4 validated live.** `create_file(lru_cache.py)` at step 3 and
   `create_file(test_lru_cache.py)` at step 5 both succeeded as overwrites
   of files the model had created earlier this run — no "already exists"
   rejection, no deadlock. Fix #4 works in a real run (previously only
   t2 had partially exercised it).

3. **The generated checklist itself was excellent** — 9 fine-grained TDD
   steps with three worked numeric examples (update-in-place, get-miss-
   no-reorder, eviction-before-insert). The tech-lead decomposition quality
   is not the bottleneck.

**But the run still failed (final_status: failed, 214.6s, parked at step
19), and the transcript shows it was dominated by a MECHANICS failure,
not a capability floor:**

- All 8 `str_replace(lru_cache.py)` calls were **rejected** by the editor
  guard, alternating between `old_str occurs 2 times in lru_cache.py` and
  `old_str not found in lru_cache.py`. This is the **identical
  str_replace-matching failure mode as t2** — the model cannot construct a
  valid surgical `old_str`. It never successfully edited the
  implementation past the stub state.
- The final `lru_cache.py` has `get`/`put` as `raise NotImplementedError`
  (only `__init__` and a trivial `size = len(self._cache)` are real). This
  is partly the **checklist's own TDD ordering trapping the model**: step 2
  explicitly says "leave get/put/size as stubs," so the model's step-3
  `create_file` rewrite correctly wrote stubs per step 2 — then it could
  never str_replace them into real code (all 8 attempts rejected).
- It parked at step 19 on the **bash/pytest repetition guard** (repeated
  `pytest` invocations). Ironic: steering produced the *correct* TDD
  behavior (edit impl -> run tests -> repeat), but that legitimate loop
  tripped the guard (3 identical `pytest` calls = "stuck") and parked the
  run *faster* than t4's pathological test-file-editing, which never tripped
  any guard and ran to the 900s timeout (t5 214s vs t4 944s).

**Honest, corrected read:** t5 did **NOT cleanly test the capability
floor.** It was dominated by a recurrence of the t2 str_replace-mechanics
failure. The model never got a clean chance to write real `get`/`put` —
its one whole-file `create_file` (step 3) was bound by the checklist's
"leave stubs" instruction, and every surgical attempt to add the logic
afterward failed on `old_str` matching. So we still do not know whether
this model can produce real LRU logic when the str_replace mechanics are
removed from the equation.

This isolates the next experiment cleanly: a checklist that says **"write
the COMPLETE implementation in ONE `create_file` — all methods implemented
per the recipe, no stubs, no `str_replace`"** removes the str_replace
mechanics entirely and tests the pure capability question — can the model
write working `get`/`put`/`size` in a single whole-file write, given the
OrderedDict recipe? That separates "can't construct surgical edits"
(t2/t5, a mechanics/harness issue) from "can't write the logic" (a true
capability floor). The current TDD step-2-stubs-then-step-4-implement
ordering creates the stubs-then-str_replace trap; a single-shot
whole-file checklist sidesteps it.

### Whole-file capability test (lru_cache t6, 2026-07-16) — SUCCESS.
The blocker was str_replace MECHANICS, not a coding capability floor.

Strengthened `_PLANNER_SYSTEM` (and `_REWORK_PLANNER_SYSTEM`) with an
EDITING MECHANICS directive: the weak model reliably fails surgical
str_replace (t2/t5: every str_replace rejected as "old_str occurs N
times" / "not found"), so the checklist now directs the executor to write
COMPLETE files via `create_file` in one shot — no `NotImplementedError`
stubs, rewrite the whole file to fix rather than str_replace. Regression
guard updated; 992 tests green. Ran t6 = t5's config (temp 1.0, 14B,
Sonnet planner+reviewer, steering) + the whole-file directive.

**Result: PASSED — the first fully-merged, groundtruth-passing lru_cache
trial of the entire session.** `final_status: done`, `merged: true`,
`review_verdict: APPROVE`, `rework_attempts: 0`, `groundtruth_passed:
true`, 234s. The merged `lru_cache.py` contains real, correct logic:

```python
from collections import OrderedDict
class LRUCache:
    def __init__(self, capacity):
        if capacity < 1: raise ValueError(...)
        self._capacity = capacity
        self._data = OrderedDict()
    def get(self, key):
        if key not in self._data: return None
        self._data.move_to_end(key)        # hit marks MRU
        return self._data[key]
    def put(self, key, value):
        if key in self._data:
            self._data[key] = value
            self._data.move_to_end(key)     # update marks MRU, no evict
        else:
            self._data[key] = value
            if len(self._data) > self._capacity:
                self._data.popitem(last=False)   # evict LRU on new-key overflow
    @property
    def size(self): return len(self._data)
```

Triple-verified: in-repo suite 12 passed; the **independent hidden
groundtruth** (`_gt/test_groundtruth.py`, 8 tests the model never saw)
**8 passed** against the merged impl.

**This is outcome (a) — the decisive, positive result.** With the
str_replace mechanics removed from the equation, the model wrote correct,
complete `get`/`put`/`size` on a whole-file `create_file`, passed the
hidden groundtruth, got APPROVE, and merged. The blocker across t2/t3/t5
was **not a coding capability floor** — it was the **str_replace
mechanics** (the model cannot construct a unique matching `old_str`, so a
stubs-then-surgically-edit loop never makes progress). Give the same model
the same recipe via a "write the whole file in one shot" checklist and it
succeeds.

**Caveat (honest):** the worktree artifacts (agent.log,
.agent_plan.md, .agent_transcript.json) were cleaned up post-merge, so the
exact step sequence can't be re-inspected to confirm no str_replace was
attempted. But the proof point is unambiguous: the merged file has real
logic, not stubs, and it passed the hidden groundtruth. Whether the model
tried (and the checklist steered it away from) str_replace is unknown from
artifacts alone — only the outcome is certain.

**What this means for the open problem (refined across t2→t6):**

The "weak model can't complete coding tasks" framing this session started
from was, in hindsight, *mostly a harness/mechanics problem, not a model
problem* — but it took four distinct fixes, each isolating one mechanism,
to surface that:

1. **temp 0.3 determinism lock** (t3) — fixable by raising temperature
   (proven t4: the byte-identical reproduction was greedy decoding).
2. **test-file-editing flailing** (t4) — fixable by the steering checklist
   (proven t5: the model targeted the impl file, not the test).
3. **str_replace old_str-matching mechanics** (t2, t5) — fixable by the
   whole-file create_file directive (proven t6: real logic, merged).
4. **repetition guard on the legitimate TDD edit-test loop** (t5 parked
   on repeated pytest) — NOT yet fixed; t6 succeeded fast enough (234s)
   that it didn't bite here, but it remains a latent false-positive for a
   model doing correct edit->test->edit->test TDD that doesn't converge
   immediately.

The remaining genuinely-open question is narrower than where we started:
**does the whole-file + steering + temp-1.0 combination succeed on the
OTHER tasks that failed this session (cron_field, interval_merge), or is
lru_cache a lucky/representative case?** t6 validates the mechanism on one
task; generalization needs a re-run of the failed tasks under the now-
complete prompt (steering + whole-file + temp 1.0) to claim the lever
generalizes. None of that has been run yet.

### Generalization check (cron_field t7 + interval_merge t7, 2026-07-16)
— the lever generalizes; all three previously-failed tasks now pass

Re-ran the two other tasks that failed the 6-task sweep under the now-
complete prompt (steering + whole-file create_file + worked examples) at
temp 1.0, MLX 14B, Sonnet planner+reviewer, rework cap 3.

| task | final_status | merged | review | groundtruth | elapsed |
|---|---|---|---|---|---|
| lru_cache t6 | done | true | APPROVE | passed | 234s |
| cron_field t7 | done | true | APPROVE | passed | 237.7s |
| interval_merge t7 | done | true | APPROVE | passed | **2309.8s** |

**All three previously-failed tasks now merge with groundtruth pass.** The
combined lever (steering + whole-file mechanics + worked examples + temp
1.0) generalizes — the failures were not task-specific luck. The merged
`intervals.py` is correct (independent hidden groundtruth `_gt/
test_groundtruth.py`: 12 passed).

**Honest caveat — interval_merge is 10x slower and did NOT follow the
whole-file directive.** lru_cache t6 and cron_field t7 finished in ~235s
each (clean, fast). interval_merge t7 took **2309.8s (~38 min)**, grinding
through a long `str_replace`->pytest loop (captured in the inflight
snapshot before merge cleanup wiped the final `agent.log`). It ignored
the "write the whole file via create_file, not str_replace" mechanics
directive — BUT unlike lru_cache (where str_replace failed on non-unique
`old_str`), str_replace *worked* here because `intervals.py`'s anchors
were unique. So str_replace was a viable (if slow) path on this task, and
the model eventually cracked the last edge case (validation) and passed.

**What this confirms about the open problem (final, refined):**

1. The earlier "weak model can't complete coding tasks" framing is
   **largely debunked** for these tasks — with the right prompt
   (steering + whole-file + worked examples) and temp 1.0, the 14B model
   implements correct logic on all three tasks it previously failed.
   The blockers were mechanics/decoding/steering, not capability.
2. The whole-file `create_file` directive is **necessary but not always
   followed**: when the model ignores it and falls back to str_replace,
   the outcome depends on the file's anchor structure — str_replace
   succeeds on files with unique code (interval_merge) and deadlocks on
   files with repeated stubs (lru_cache). The directive removes the
   deadlock case but can't force the efficient path.
3. The repetition-guard false-positive on the legitimate edit->test TDD
   loop (mechanics #4 from the t5 write-up) did NOT bite here only because
   interval_merge varied its pytest invocations enough to avoid the
   3-identical-call threshold; it remains a latent trap.

**Remaining genuinely-open items (not blocking; recorded for later):**
- ~~A real rework trial (H4) is still unexercised~~ **RESOLVED (t9,
  2026-07-16, see below)** — t9's forced interruption produced a genuine
  REQUEST_CHANGES -> `_run_rework_planner` fix-checklist -> rework
  redispatch, unintentionally exercising exactly this path. The fix-checklist
  arrived in the transcript verbatim and the rework succeeded (APPROVE,
  merged, groundtruth passed).
- H3 (scratchpad's independent contribution) was never tested. **Still open**
  — no trial this session isolated `PIPELINE_DECOMPOSE_SCRATCHPAD=on` vs
  `off` on a matched task.
- The whole-file directive could be strengthened or enforced: **(a) make the
  harness prefer/permit create_file-overwrite more aggressively — DONE**, see
  the informed-overwrite fix + t8/t9 live validation below; **(b) gate
  str_replace behind a nudge — explicitly scoped OUT** (user chose
  informed-overwrite only, not the nudge, when the fix was approved). (b)
  remains a real gap for a task where str_replace's anchors are unique
  enough that it never deadlocks and the model is never steered toward
  create_file at all — worth revisiting if a future task grinds through
  repeated str_replace cycles despite the whole-file directive.
- The repetition-guard TDD false-positive (mechanism 3 above) remains
  **exactly as open as before** — t9's guard trip was a genuine stuck-loop
  (3 identical `pytest` calls with no intervening progress), not the
  false-positive case (progress between reads) the plan worried about, so
  it neither confirms nor refutes the latent trap.

**Preserved artifacts:** `tests/benchmark/_runs/guided_decomp_mlx_6tasks/
_preserve_for_analysis/` holds snapshots of all three generalize trials
(inflight + final where captured) for later analysis.

---

## Whole-file directive: root cause found + fixed (2026-07-16)

Followed up the "whole-file directive is necessary but not always followed"
item above by reading the **preserved interval_merge t7 artifacts** rather
than theorising. The 10x slowdown was NOT the executor ignoring the directive.

**What the transcript actually shows** (`_preserve_for_analysis/
interval_merge__mlx__t7__inflight_snapshot/worktrees/INTERVAL-MERGE/
.agent_transcript.json`):

1. The whole-file directive **was delivered in full**. The planner paraphrases
   the `_PLANNER_SYSTEM` steering into the checklist's own prose; the delivered
   resume prompt (msg[1], the last ~700 chars) reads verbatim: *"Write each
   file completely via create_file in one shot (no stubs-then-surgically-edit;
   rewrite the whole file when fixing)."* (An earlier pass keyword-searched for
   the *system-prompt* wording — `CRITICAL STEERING`, `NEVER edit`,
   `NotImplementedError` — and wrongly concluded "not delivered." Retracted.)

2. The executor **tried to comply** — it opened with `create_file` on both
   `test_intervals.py` and `intervals.py` (steps 0 and 2). But this was a
   **step-cap RESUME**: both files already existed on disk from the interrupted
   run, so they were not in `_CREATED_THIS_RUN` (a per-process set, empty in the
   fresh resume process). `create_file` refused with *"already exists and is
   non-empty. Use str_replace to edit it"* — **the guard's own error message
   funnelled the model onto str_replace**, directly defeating the directive it
   had just been given. Result: 28+ rejected surgical-edit cycles, ~2310s.

**Root cause:** the non-destructive `create_file` guard (added to protect
pre-existing repo/seed files from blind clobber) had no exception for the
resume/rework case, and its error text actively recommended the exact tool
(str_replace) the weak model cannot drive.

**Fix (informed overwrite):** a pre-existing file the model has read via
`view_file` THIS run is now eligible for a whole-file `create_file` overwrite
(tracked in a new `_VIEWED_THIS_RUN` set, companion to `_CREATED_THIS_RUN`),
and the refusal message now steers to `view_file` -> `create_file`, never
str_replace. The blind-clobber protection is preserved exactly: a file the
model has neither authored nor read this run is still refused. Applied in
lockstep to `scripts/local_agent.py` and `scripts/local_agent_oracle.py`
(verbatim mirror), plus the `create_file` tool description in both.

TDD: modified the pinning test `..._still_rejects_overwrite_of_pre_existing_file`
-> `..._rejects_overwrite_of_unseen_pre_existing_file` (with explicit user
approval per CLAUDE.md Step 4; it now asserts the view_file-steering message
and the narrowed guard) and added `..._overwrites_a_pre_existing_file_after_
view_file`, mirrored in the oracle test. All four failed for the right reason
(no `_VIEWED_THIS_RUN`) before the impl, green after; full local_agent +
oracle suites 176 passed. End-to-end replay of the interval_merge resume
sequence confirms: blind overwrite refused -> view_file -> whole-file
overwrite succeeds, and an unseen sibling file stays protected.

**Not yet validated live** against a fresh interval_merge resume run — the
unit + end-to-end replay cover the mechanism, but a real dispatched resume is
the remaining confirmation.

### Live re-run after the fix (interval_merge t8, 2026-07-16)

Re-ran interval_merge under identical conditions to t7 (steering + whole-file
+ worked examples, temp 1.0, MLX 14B, Sonnet planner+reviewer, rework cap 3;
`PIPELINE_DECOMPOSE=cloud PIPELINE_DECOMPOSE_CLOUD_MODEL=sonnet
PIPELINE_BACKEND_REVIEW=claude PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE=3
PIPELINE_LOCAL_TEMPERATURE=1.0`), trial t8, after the informed-overwrite fix
above landed.

| task | final_status | merged | review | groundtruth | elapsed |
|---|---|---|---|---|---|
| interval_merge t7 (pre-fix) | done | true | APPROVE | passed | 2309.8s |
| interval_merge t8 (post-fix) | done | true | APPROVE | passed | **222.0s** |

**~10.4x speedup**, landing in line with the fast lru_cache/cron_field
baselines (~235s). 14 ticks, 0 rework attempts, 0 dispatch attempts — a clean
first-try run, no step-cap resume needed to reach it. `groundtruth_ran: true`,
`groundtruth_passed: true`, `impl_changed: true`, `test_changed: true`
(correct code, proper TDD).

**Honest caveat:** this run did not hit a step-cap resume, so it doesn't
*directly* observe the `view_file` -> `create_file` informed-overwrite path
firing live — it demonstrates the system is fast and correct post-fix, not
that this specific run exercised the fixed code path. The resume-funnel
mechanism itself is directly exercised by the unit tests and the end-to-end
replay in the section above. A run that deliberately forces a step-cap
resume (e.g. a low step budget) would be needed to observe the fix's exact
mechanism fire in a live dispatch, and remains undone.

### Forced-interruption live verification (interval_merge t9, 2026-07-16)

The t8 caveat above ("remains undone") was closed by deliberately forcing an
interruption mid-run and observing the resume live, rather than waiting for
one to occur naturally.

**Setup:** relaunched interval_merge with `PIPELINE_LOCAL_MAX_STEPS=6` (t8
had landed `intervals.py` via `create_file` at step 4, so a tight cap should
interrupt shortly after the impl file exists on disk but before the task
finishes). Same conditions otherwise (steering + whole-file + worked
examples, temp 1.0, MLX 14B, Sonnet planner+reviewer, rework cap 3).

**What actually triggered the interruption:** not the step cap directly —
three identical `pytest -q test_intervals.py` bash calls tripped the
per-target repetition guard first (bash is not excluded from that guard,
unlike `str_replace`), which nudged once, then parked on the next repeat.
The park led to a reviewer REQUEST_CHANGES and a fix-checklist redispatch in
a **fresh process** with `test_intervals.py`/`intervals.py` already on disk —
the identical precondition a step-cap resume produces (`_CREATED_THIS_RUN`
empty, files pre-existing). A different trigger than intended, but the same
code path under test, so it's a valid observation of the mechanism.

**Transcript evidence the fix fired** (`.agent_transcript.json`, msgs 17-21):

1. msg[17]: rework redispatch delivers the reviewer's fix-checklist in the
   fresh process.
2. msg[18]: `view_file: test_intervals.py` — the model reads the file
   before touching it (not a blind overwrite attempt).
3. msg[19]: tool result returns the actual file contents.
4. msg[20]: `create_file` with the corrected full contents — an informed
   whole-file rewrite of a pre-existing file.
5. msg[21]: tool result is `created test_intervals.py` — **accepted**. Pre-
   fix, this exact call would have returned `"ERROR: test_intervals.py
   already exists and is non-empty. Use str_replace to edit it."` and
   funnelled the model into the old str_replace deadlock.

The very next agent.log line is `ORACLE GREEN — acceptance tests pass;
committed & done`.

**Outcome:** `final_status: done`, `merged: true`, `review_verdict: APPROVE`,
`groundtruth_passed: true`, `groundtruth_ran: true`, `impl_changed: true`,
`test_changed: true`, `elapsed_s: 504.5`, `ticks: 21`. Slower than t8's clean
first-try 222.0s (this run did genuine rework: nudge, park, redispatch,
fix-checklist, informed overwrite) but still far under t7's pre-fix 2309.8s,
and — unlike t7 — it never fell into a str_replace grind; the fix resolved
the file in one `view_file` + one `create_file` cycle.

**This closes the "not yet validated live" gap noted above.** The informed-
overwrite mechanism is now confirmed by: (1) unit tests, (2) an offline
end-to-end replay of the interval_merge resume sequence, and (3) this live
dispatched run, where the exact `view_file` -> `create_file` sequence was
observed firing in the real transcript on a genuine fresh-process resume.

### H3 scratchpad ablation — first attempt, INCONCLUSIVE (lru_cache t7/t8, 2026-07-16)

Ran the first matched H3 pair on lru_cache (the task with the best-
characterised failure history), identical config to the t6/t8/t9 runs
otherwise (steering + whole-file + worked examples, temp 1.0, MLX 14B,
Sonnet planner+reviewer, rework cap 3), varying only
`PIPELINE_DECOMPOSE_SCRATCHPAD`:

| trial | scratchpad | final_status | merged | groundtruth | elapsed |
|---|---|---|---|---|---|
| lru_cache t7 | **off** | failed (parked) | no | not run | 178.4s |
| lru_cache t8 | **on** | failed (parked) | no | not run | 245.7s |

**Both failed — but the comparison does NOT isolate H3, because the
scratchpad was never consumed.** Verified from the t8 transcript:

- The scratchpad instruction WAS delivered (msg[1] ends verbatim with "After
  finishing each step, keep .agent_scratchpad.md up to date with a short
  running summary..."), and t7's prompt correctly omitted it (ablation
  plumbing works).
- But the t8 model never once called `create_file`/`str_replace` on
  `.agent_scratchpad.md` — zero scratchpad tool activity across the whole
  run, and no `.agent_scratchpad.md` file exists in the worktree afterward.
  The "memory" lever was available but never pulled, so t8's behaviour can't
  be attributed to the scratchpad's presence. Single pair, inconclusive on
  H3 by construction.

**What the pair DID surface (both independent of the scratchpad):**

1. **A confirmed, reproducible model defect.** This 14B writes `@property`
   immediately followed by a wrongly-indented `def size(self):` (dedented to
   column 0), producing `SyntaxError: unexpected unindent`. It then
   reproduces that exact broken content near-verbatim across retries — the
   same determinism-lock mechanism first seen at t3, now observed a 2nd and
   3rd time and specifically localised to this `@property` + `size` pattern.
   Both t7 and t8 hit it; neither escaped it before parking.
2. **A new steering violation (t8 only).** Under repetition-nudge pressure,
   t8 abandoned the impl file and `str_replace`'d the TEST file
   (`test_lru_cache.py` — changing `from lru_cache import LRUCache` to a
   broken relative `from .lru_cache import LRUCache`), which the steering
   directive explicitly forbids ("NEVER edit the test files"). That
   introduced an ImportError it then looped on until parking. t7 did not do
   this. Not attributable to the scratchpad, but a real directive-adherence
   gap under pressure worth its own follow-up.

**Open question this raises (to investigate next):** why did the executor
ignore an explicit, delivered instruction to maintain `.agent_scratchpad.md`?
Candidate causes: the instruction sits at the very end of a 7.7k-char prompt
(recency/primacy burial); it's phrased as an aside after "Work through these
steps in order" rather than as a numbered step with its own done-criterion;
the model may deprioritise a non-functional bookkeeping action when it's
already struggling with the functional task; or the tool-selection pressure
(every turn spent on scratchpad is a turn not spent on the failing tests).
Needs a deeper look at prompt structure + a re-run with the scratchpad step
promoted to a first-class checklist item before H3 can be fairly tested.

### Why the scratchpad is (almost) never consumed — root cause (2026-07-16)

Dug into the t8 "instruction delivered but ignored" finding by measuring
scratchpad consumption across the whole `_runs` corpus, not just t8.

**Corpus measurement:** of 22 guided runs (those with a generated
`.agent_plan.md`) across the mlx tree, only **2 ever touched
`.agent_scratchpad.md` — a 9% consumption rate.** And those 2 were the two
LONGEST runs in the entire tree:

| run | steps | scratchpad writes | status | elapsed |
|---|---|---|---|---|
| ratelimiter_bugfix mlx t0 | 407 | 7 | interrupted | 3602s |
| token_bucket mlx t0 | 90 | 4 | failed | 1526s |
| (every other guided run, 8-112 steps) | — | 0 | failed | — |

**Three structural causes, all evidence-backed:**

1. **The scratchpad is orphaned from the checklist the model actually
   follows (primary cause).** The executor works the *planner-generated*
   checklist in `.agent_plan.md` step by step. That checklist NEVER mentions
   the scratchpad — `_PLANNER_SYSTEM` doesn't tell the planner to include it,
   and t8's actual `.agent_plan.md` has zero references. The scratchpad ask
   lives in a separate hardcoded sentence (`pipeline_mcp_server.py` ~L2828)
   appended AFTER the checklist, tacked onto "Work through these steps in
   order." So the model sees N numbered steps each with a done-criterion,
   plus one trailing bookkeeping aside that belongs to no step and has no
   done-criterion. Under any pressure the numbered steps win.
2. **It's a non-functional action competing for turns against the functional
   task.** The only runs that used it were long, sprawling ones where
   tracking state paid off. In short parked runs (t7/t8, ~10 steps) the model
   was fighting a syntax deadlock from step 2 on and never spent a turn on
   optional bookkeeping.
3. **Not weak-model-specific — it's prompt structure.** 9% consumption
   regardless of run length points at instruction design, not capability.

**Implication for H3:** even the 2 runs that DID write the scratchpad both
still FAILED. Across the entire corpus there is not one example of the
scratchpad being written AND the run succeeding — so H3 (does persistent
cross-step memory help?) is currently untestable, not because a pair was
unlucky, but because the affordance is almost never exercised. You can't
measure the benefit of a lever nobody pulls.

**Fix to make H3 testable:** promote the scratchpad from a trailing aside to
a first-class checklist step — have the planner weave "update
`.agent_scratchpad.md` with a one-line status after each step" into the
generated checklist as an explicit instruction the executor treats as part
of the work, rather than bolting it on after the fact.

**Implemented (2026-07-16).** Added `_PLANNER_SCRATCHPAD_CLAUSE` +
`_planner_system(include_scratchpad=...)` in `pipeline_mcp_server.py`, and
threaded an `include_scratchpad` flag through `_run_planner`. When the H3
scratchpad is on, the clause is appended to the planner's system prompt,
directing it to (a) make the FIRST numbered step create
`.agent_scratchpad.md`, and (b) end each subsequent step with "then update
.agent_scratchpad.md ... (rewrite the whole file via create_file)" as part of
that step's done-criterion — so the scratchpad lives INSIDE the checklist the
executor actually follows, not in a trailing sentence it skips. Gated on the
same `PIPELINE_DECOMPOSE_SCRATCHPAD` env var, so the ablation "off" arm sends
the byte-identical base `_PLANNER_SYSTEM` (verified). The old trailing-
sentence instruction is kept as a backstop for resumed dispatches whose stored
`.agent_plan.md` predates the clause. TDD: 3 new tests (planner-augments-
system, omits-by-default, dispatch-passes-flag) + a mechanical `**kwargs`
signature fix to one existing planner mock; full server suite 531 pass, ruff
clean. **Not yet validated live** — a fresh scratchpad-on run must confirm the
generated checklist now contains the scratchpad steps AND that the executor
follows them (that live re-run is the next step before H3 can be scored).
