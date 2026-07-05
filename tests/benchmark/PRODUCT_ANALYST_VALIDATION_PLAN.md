# Test Plan — Validating the product-analyst schema fix + Story Sizing guidance

Written 2026-07-04, after killing the `gptoss_temp03` / 6-task / 3-trial matrix run
(`_matrixrun_gptoss_only_norework.log`, task `bylnip2wr`) once it became clear that
run couldn't test what we actually changed.

**Results (2026-07-04): see "Results" section at the bottom.** tl;dr: n=3 per
condition, single task (`ratelimiter_inspect`), single model (`gptoss_temp03`).
The hypothesis was NOT supported — Condition D (product-analyst's 3-story
decomposition) completed 0/3 vs Condition M (monolithic) 1/3. The bottleneck
was concentrated in one specific sub-task (fractional-refill `allow()` logic)
that gpt-oss struggled with regardless of whether it was isolated into its own
story or bundled — splitting relocated the difficulty rather than resolving
it. A separate, unrelated finding: Condition M parked 2/3 times despite the
groundtruth confirming the code was correct all 3/3 times, pointing at a
pipeline-level review/rework issue independent of story sizing.

## Why the existing harness can't validate this change

The two `~/.claude/agents/product-analyst.md` edits under test are:

1. **Schema fix** — stop emitting the dead `acceptance_criteria` field; fold testable
   success criteria into `agent_instructions` instead (the only field `ingest_plan`
   actually forwards to the implementer).
2. **Story Sizing** — bias product-analyst toward smaller, single-concern,
   single-file stories, splitting bundled asks into dependency-chained stories.

Both only matter when **product-analyst itself decomposes a request**. The six tasks
in `tests/benchmark/tasks/` are hand-authored fixtures — checked, per the earlier
audit, to already use the correct fields and already be single-concern/single-file.
Running the existing matrix against them (any model, any config) re-measures
dispatch/review mechanics we've already validated; it cannot move if product-analyst's
prompt changes, because product-analyst never touches those tasks. Confirmed gap in
the harness itself:

- `harness.py:build_plan()` always emits exactly **one** epic with **one** story.
- `harness.py:drive()` polls a single hard-coded `story_key` until terminal.

Neither supports a multi-story, dependency-chained plan — which is the only shape
of output the Story Sizing guidance can produce. To test the change for real, the
harness needs to drive a *plan*, not a single story.

## Hypothesis

A compound, multi-concern feature request that product-analyst decomposes into
several small single-concern stories (per the new guidance) reaches `done` with
correct code, under a constrained local-only model, **more often** than the same
requirement handed to the pipeline as one bundled story.

## Design: paired experiment, same requirement, two decomposition conditions

For each compound task below, run two conditions through the identical pipeline
config, and compare:

- **Condition D (decomposed)** — the plain-language brief goes to the real
  product-analyst agent; ingest whatever plan it returns, as-is.
- **Condition M (monolithic control)** — the same requirements, hand-bundled into
  one `agent_instructions` on a single story, no split.

Both conditions are graded by the **same independent `groundtruth.py`**, which
tests the combined end-state behavior and doesn't care how many stories produced
it — so it's a fair comparison and reuses the harness's existing "hidden oracle
the model never sees" discipline.

### Candidate compound tasks (new; not yet written)

Pick requests that are naturally splittable along a concern boundary, so the
sizing guidance has something to do, while staying close enough to the existing
katas that authoring a trustworthy `groundtruth.py` is low-risk:

1. **rate limiter + inspection** — a token-bucket-style limiter, plus a read-only
   "how many tokens remain" accessor that must not mutate state, plus input
   validation on both. Natural split: (a) core bucket, (b) read-only inspector,
   (c) validation — 2-3 stories.
2. **LRU cache + eviction metrics** — the existing cache mechanics, plus a
   hit-rate/eviction-count tracker exposed via a method. Natural split: (a) cache
   mechanics, (b) metrics tracking that depends on (a).

Each needs the usual pair (`acceptance.py` hidden oracle, `groundtruth.py`
independent suite, both verified to agree on a correct reference impl, both
covering no-mutation and negative/boundary cases per the existing "Adding a task"
checklist) plus a `_MOCK_IMPLS` entry so the new harness plumbing gets an offline
self-test before spending real gpt-oss time on it.

## Metrics

Same shape as the existing scorecard, extended:

- **Completion rate** — for Condition D, "done" means *every* story in the plan
  reached `done`/merged, not just one.
- **Groundtruth pass rate** — run once against the final merged repo state,
  regardless of story count.
- **Story count per plan** (D only) — did product-analyst actually split it, and
  into how many pieces?
- **Rework cycles consumed per story**, and **which sub-story parked**, if any —
  diagnostic for whether a failure is concentrated in one concern or spread out.

## Execution config (reuses the config from the killed run)

- `--models gptoss_temp03` only — no devstral, no minimax.
- `PIPELINE_BACKEND_REVIEW=local` — gpt-oss reviews its own work; matches "gpt for
  implementation and review only."
- Dispatch stays pinned to `local` (never `auto`) — no escalation to Claude.
- Rework cap: 3 attempts before park (the acceptance-oracle path defaults
  `PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE` to 1; override to 3 as done for the
  killed run) — for Condition D this cap applies **per story**, so a 3-story
  plan gets up to 3 rework cycles on each of its stories independently.
- Start with a small trial count (e.g. 3) per the usual practice of scoping
  fix-validation runs small and looking for directional signal before committing
  to a full matrix.

## Open variable to decide before implementing: cache or resample product-analyst?

Product-analyst is itself a nondeterministic agent. Two options for Condition D:

- **Cache one decomposition per compound task** (run product-analyst once, save
  the resulting plan JSON alongside the task, reuse it for all trials) — isolates
  the variable under test to "does a fixed small-story plan complete more
  reliably," matching how Condition M is also a fixed, hand-written plan.
- **Resample product-analyst every trial** — also measures decomposition
  variance itself, but conflates two things being tested at once (does
  product-analyst reliably produce good splits, and do good splits complete more
  reliably) and needs more trials to separate them.

Recommend caching (first option) for this validation pass; decomposition
consistency is a separate, later question.

## Implementation work required before this can run (not yet done)

1. Generalize `harness.py`'s `build_plan()` to accept either the existing
   single-story task shape (unchanged, so the 6 current tasks keep working) or a
   pre-built multi-story plan JSON.
2. Generalize `drive()` to poll *all* story keys in a plan until every one is
   terminal (or the deadline lapses), instead of one hard-coded `story_key`.
3. Add the 2 new compound tasks (`acceptance.py` + `groundtruth.py` + seed
   reference impl in `_MOCK_IMPLS`), verified against a correct reference
   implementation per the existing task-authoring checklist.
4. A small driver script (or a `matrix.py` mode) that: invokes product-analyst
   once per compound task to produce and cache Condition D's plan, constructs
   Condition M's monolithic plan by hand, then runs both through the
   generalized harness at the config above.

## Next step

This is a design document, not yet code. Confirm scope (both candidate tasks, or
start with just one?) before I generalize the harness and author the new
task fixtures.

---

## Results (2026-07-04)

Built: `harness.py`'s `drive_plan()`/`build_plan_from_stories()`, the
`ratelimiter_inspect` compound task (`acceptance.py`/`groundtruth.py`, verified
against a correct reference impl and a deliberately mutating-inspector "gamer"
impl), a real product-analyst decomposition (cached at
`tasks/ratelimiter_inspect/decomposed_plan.stories.json`: 3 stories, RLI-1 →
RLI-2 → RLI-3, matching the Story Sizing guidance), and `compound_harness.py`
to drive both conditions. Config: `gptoss_temp03`, `PIPELINE_BACKEND_REVIEW=
local`, `PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE=3`, dispatch pinned to `local`
(no escalation), 3 trials per condition.

### Scorecard

| Condition | Trial | Final status | Merged | Groundtruth ran | Groundtruth passed | Where it stopped |
|---|---|---|---|---|---|---|
| M (monolithic) | 0 | done | yes | yes | **yes** | — |
| M | 1 | parked | no | yes | **yes** | reviewer never landed it despite correct code |
| M | 2 | parked | no | yes | **yes** | same |
| D (3-story) | 0 | incomplete | no | no | — | RLI-2 (repetition guard) |
| D | 1 | failed | no | yes | **no** | RLI-3 reached, but wrong (real bug caught) |
| D | 2 | incomplete | no | no | — | RLI-2 (repetition guard) |

**Completion rate:** M 1/3 (33%) vs D 0/3 (0%).
**Groundtruth pass rate, of trials where it ran:** M 3/3 (100%) vs D 0/1 (0%).

### Reading it

The hypothesis (product-analyst's smaller, single-concern stories complete
more reliably under a constrained local model) is **not supported** by this
run — n=3 per condition, one task, one model, so treat this as a directional
signal, not a proof, but the direction is the opposite of what we were testing
for.

The Condition D bottleneck was concentrated in one specific place: RLI-2 (the
fractional-time-refill `allow()` logic) tripped gpt-oss's per-target
repetition guard in 2 of 3 trials — the exact same underlying reasoning task
that, in Condition M, is just one part of a larger bundled story. Isolating it
into its own story didn't make it easier; gpt-oss got stuck on it either way.
The one D trial that got past RLI-2 then failed at RLI-3 (the read-only peek
method) with the independent groundtruth catching a real bug — a genuine
validation that the harness's oracle design works as intended (see the
speculative-future-peek test in `tasks/ratelimiter_inspect/groundtruth.py`),
but not a point in decomposition's favor either.

A separate, unrelated finding worth flagging: Condition M parked in 2 of 3
trials despite the groundtruth confirming the code was correct all 3 times.
That's a pipeline-level review/rework issue (correct code failing to land),
independent of story granularity, and probably worth its own investigation
before drawing further conclusions from any model-comparison run at this
config.

### Caveats / what this doesn't tell us

- Single task, single model, n=3 — not enough to generalize "decomposition
  doesn't help," only "it didn't help here."
- The specific compound task (a token-bucket rate limiter) may just have an
  unusually hard sub-problem (fractional-time refill math) that dominates the
  result regardless of framing; a task with more independent, less
  interdependent concerns might split more favorably.
- Condition D used one cached product-analyst decomposition, not resampled —
  we're testing "this particular split," not product-analyst's average
  decomposition quality (see the caching-vs-resampling tradeoff above).
- Bugs fixed mid-run (`MockBackend` test/diff gaps, `drive_plan`'s
  direct-then-transitive dependency-blocking detection) only affected the
  offline self-test and the harness's own wall-clock efficiency, not the
  live-run results themselves.

---

## Results — post-fix rerun (2026-07-04, same config, `_runs/rli_pa_validation_postfix/`)

Digging into *why* the above run failed led to two production fixes, committed
separately from this experiment: the per-target repetition guard now resets on
a real edit instead of counting reads cumulatively for the whole run
(`852c6ad`), and the local reviewer's "wrap up" nudge now repeats every step
near the cap instead of firing once (`63e37cc`). Re-ran the identical 6-cell
comparison to see whether either fix moved the numbers.

| Condition | Trial | Final status | Merged | Groundtruth passed | Note |
|---|---|---|---|---|---|
| M | 0 | done | yes | **yes** | — |
| M | 1 | done | yes | **yes** | — |
| M | 2 | done | yes | **yes** | — |
| D | 0 | done | yes | **no** | merged-but-wrong (see below) |
| D | 1 | done | yes | **yes** | — |
| D | 2 | incomplete | no | — | RLI-1 parked after 3 REQUEST_CHANGES rework cycles |

**Completion rate (merged):** M 3/3 (100%, up from 1/3) vs D 2/3 (67%, up from 0/3).
**Correct AND merged (the metric that actually matters):** M 3/3 (100%) vs D 1/3 (33%).

### Reading it

The reviewer-nudge fix fully resolved Condition M's problem: all 3 trials now
merge, and all 3 remain groundtruth-correct — matching the pre-fix run, where
the code was already correct 3/3 times but only landed once. That's a clean,
unambiguous win, independent of the Story Sizing question.

Condition D also improved — no repetition-guard trips this time; both
trials that reached RLI-2 got past it — but the Story Sizing hypothesis
**still isn't supported**. D/trial 0 is the important result: all three
stories reached `done` and the pipeline merged the work, but the independent
groundtruth failed. Inspecting the merged repo directly: `rate_limiter.py` was
never touched by RLI-3's dispatch at all — the only diff was a one-line
comment in the test file (`# Additional tests for available_tokens method`)
and the materialized `test_acceptance.py` fixture itself. No implementation,
no real test, yet the story reached `tests_passed` and got `APPROVE`.

This is a genuine merged-but-wrong regression, and specifically the failure
mode the acceptance-oracle gate (`pipeline_mcp_server.py`'s FM-A scoping,
`backend.py`'s `oracle_result()`) exists to prevent — `test_acceptance.py`
directly calls `available_tokens()`, which doesn't exist in the merged code,
so a pytest run against it should have failed with an `AttributeError` and
kept the story out of `tests_passed`. Both the server-side gate and the
agent's own client-side `oracle_result()` check look correct on inspection;
neither the worktree nor `agent.log` survived past the successful merge
(cleaned up on success), so the exact mechanism is **not yet root-caused** —
only the outcome is confirmed. Worth a follow-up investigation on its own,
independent of the Story Sizing question, and probably higher priority: a gate
that can be bypassed is a bigger risk than a model that's merely slow.

D/trial 2 parked cleanly this time (no runaway spin, no repetition-guard
false-positive) — three genuine `REQUEST_CHANGES` cycles exhausted the
rework budget. A normal, working outcome of the pipeline's own safety net.

### Updated bottom line

Both production fixes were real, valuable improvements — Condition M went
from 33% completion (despite always-correct code) to 100%. But
they didn't change the answer to the original question: product-analyst's
3-story decomposition still underperforms the monolithic story on this task
(33% correct-and-merged vs 100%), and now surfaced a more serious problem
(a gate that let wrong code merge) that deserves investigation before this
config is trusted for anything beyond benchmarking.

## Investigating the merged-but-wrong RLI-3 incident (2026-07-04)

### Ruled out by code inspection

- **Pre-merge rebase (`_rebase_onto_master`)**: aborts and reports a conflict
  rather than silently applying a partial merge. Not the mechanism — and moot
  here anyway, since RLI-3 had no sibling stories to drift against (it
  dispatches strictly after RLI-2 already merged, so its branch base was
  already current master).
- **`bash`-tool bypass of the acceptance-file guard**: `is_oracle_path()` in
  `local_agent_oracle.py` only blocks `create_file`/`str_replace` on the
  oracle path, not `bash` (a model could `rm`/overwrite it via shell and
  dodge the guard) — a real gap, confirmed by reading the code, but not what
  happened here: the merged `test_acceptance.py` still has all 10 original
  tests unmodified, so it wasn't tampered with.
- **Squash-merge conflict silently landing a partial diff**: `harness.py`'s
  merge stub runs `git merge --squash` then `git commit` with `check=True`
  (`_sh`'s default) — a real conflict would raise `CalledProcessError` and
  crash the cell, not complete cleanly. Our cells completed cleanly.

### Live repro: 2 attempts, 0 reproductions

Built `tests/benchmark/_repro/repro_rli3.py`: seeds a fresh repo with the
*actual* RLI-1+RLI-2 code from the incident (copied from
`_runs/rli_pa_validation_postfix/.../t0/repo` at the commit right after
RLI-2's merge), then dispatches only RLI-3 against it, with the merge stub
patched to keep the worktree on success so logs survive regardless of
outcome.

- **Attempt 1**: gpt-oss correctly implemented `available_tokens()` (with a
  shared `_tokens_after_refill` helper, exactly per instructions), reached
  `done`/`APPROVE`, groundtruth passed.
- **Attempt 2**: the agent genuinely attempted the work (5 `str_replace`
  calls to `rate_limiter.py` across the run, visible in the preserved
  `agent.log`) but hit the repetition guard and parked — `status: failed`,
  never reached review, `rate_limiter.py` correctly left untouched on master.
  Side note (not a new bug, just a design observation): `nudged_repeat` is a
  single flag for the whole run, not per-signature, so an early nudge on one
  repeated target (`test_rate_limiter.py`) consumed the agent's only warning,
  and a later, unrelated repetition (`rate_limiter.py`) went straight to
  park with no second chance.

Neither attempt reproduced the actual incident (reaching `done`/`APPROVE`
with zero real implementation). At this sample size that's consistent with a
rare, non-deterministic model-behavior tail event rather than a deterministic
pipeline defect — but it also means **the exact mechanism is still not
confirmed**, only the outcome (from the original run's artifacts) and what
it isn't (the three ruled-out mechanisms above).

### A structural gap this surfaced regardless of root cause

The benchmark harness's CI stub (`harness.py`'s `_ci_status_stub`)
unconditionally returns `{"state": "pass"}` — it never actually runs
anything. In a real deployment, `_ci_status` polls genuine CI, and a repo
with a pytest CI job would very likely have caught this before merge as a
second, independent check. The hermetic benchmark has no equivalent: once
whatever let `tests_passed` get set incorrectly happened, there was no
second layer to catch it. That's a **defense-in-depth gap in the test
harness itself**, independent of whatever the original root cause turns out
to be, and cheap to close: make the stub actually run the acceptance oracle
(or the full suite) before reporting pass, mirroring what real CI would do.

### Recommended follow-ups (not yet done)

1. **Close the benchmark's CI-stub gap** (low-risk, harness-only): have
   `_ci_status_stub` actually run the story's acceptance oracle against the
   branch and report `fail` if it doesn't pass, instead of always reporting
   `pass`. This alone would have caught the RLI-3 incident before merge.
2. **Consider an independent pre-merge re-verification in the real pipeline**
   (broader, production-level): don't rely solely on external CI configuration
   (which may not exist for a given repo) — have the merge gate itself
   re-run a story's acceptance oracle right before merging, as a second,
   self-contained check independent of review. This is the same
   defense-in-depth principle CLAUDE.md already asks for elsewhere in this
   codebase.
3. Keep an eye out for a natural recurrence with logs preserved (don't clean
   up a worktree on a *first* occurrence of an anomaly like this if one is
   spotted again), since live reproduction is expensive and low-yield.
