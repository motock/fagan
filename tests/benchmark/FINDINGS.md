# Benchmark Run Findings — 2026-06-30

Full matrix run: `tests/benchmark/_runs/full_20260630/`  
Run time: 8017s (~2.25 hours). 45 cells: 5 tasks × 3 models × 3 trials.

**tl;dr:** The run was severely contaminated by Claude credit exhaustion mid-run.
All sonnet cells (13/15) failed at dispatch due to rate limits; all minimax "harder task"
cells (9/15) were parked because the REVIEW backend also hit the same rate limit and
returned the rate-limit message as an UNKNOWN verdict. The only fully clean data is
devstral (15/15 uncontaminated) and minimax on the first two tasks (6/15).

---

## Corrected scorecard (excluding rate-limited cells)

The printed scorecard is misleading. Here is the actual picture:

| Task | devstral (clean) | minimax (clean) | sonnet (clean) |
|------|:---:|:---:|:---:|
| cron_field | 0/3 (0%) | **3/3 (100%)** | 1/1 (100%) |
| interval_merge | 0/3 (0%) | **3/3 (100%)** | 1/1 (100%) |
| lru_cache | 0/3 (0%) | ⚠️ tainted | ❌ no data |
| retry_backoff | 0/3 (0%) | ⚠️ tainted | ❌ no data |
| token_bucket | 0/3 (0%) | ⚠️ tainted | ❌ no data |

**minimax "tainted" cells:** the implementation was correct (groundtruth=True for 8/9
cells) but the reviewer hit the rate limit and returned a non-verdict response, which
the pipeline counted against the rework budget until the story was parked.

**sonnet "no data":** credits exhausted after the first 2 cells; 13/15 cells instant-
failed at dispatch with `"overageDisabledReason":"out_of_credits"`.

---

## Per-model analysis

### devstral — 0/15 success, 8/15 groundtruth-correct (DATA: CLEAN)

devstral is the only model with fully clean, interpretable data. It never reached the
review stage, so the rate limit on the reviewer never affected it.

**Two distinct failure patterns:**

#### Pattern A — Wrong implementation (5 cells)

| Cell | Error |
|------|-------|
| cron_field t0/t1/t2 | `*/step` syntax: tries to `int("*")` for the range start; crashes with `ValueError` |
| interval_merge t0 | Implementation returns `None` everywhere (likely forgot `return`) — 11/12 gt tests fail |
| interval_merge t2 | `SyntaxError` in generated file — code was not valid Python |
| token_bucket t0 | `rate_limiter.py` was never written to the worktree at all |

cron_field is a consistent devstral blind spot: across 3 independent trials it always
mishandled `*/step` syntax (the `*` wildcard-step form). Two trials crashed when
parsing `*` as an integer; one trial raised a false ValueError rejecting `*/step`
entirely as "malformed". Same conceptual bug every time.

interval_merge t1 nearly passed (only 1 gt failure: missing ValueError for reversed
range). t0 and t2 were write-level failures.

#### Pattern B — Correct implementation, blocked by own buggy test assertions (5 cells)

| Cell | gt result | Why pipeline failed |
|------|-----------|---------------------|
| lru_cache t0/t1 | **gt=True** (parked) | Step-cap hit; story parked before tests_passed |
| lru_cache t2 | **gt=True** (failed) | Own test assertions failed whole-suite gate |
| token_bucket t1/t2 | **gt=True** | Own test assertions failed whole-suite gate (same as trial 0 diagnosed previously) |
| retry_backoff t0/t2 | **gt=True** (parked) | Step-cap hit before tests_passed |
| retry_backoff t1 | **gt=True** (failed) | Own test assertions failed whole-suite gate |

This is the documented "graded on own buggy tests" failure mode (Fix #1). devstral
writes correct implementations but writes wrong test assertions alongside them.
`check_story_status` runs pytest over the entire worktree, so the model's own bad
assertions block stories that would pass the harness-owned acceptance oracle.

**Step cap:** devstral's lru_cache and retry_backoff trials that ended as `parked`
hit the 40-step cap before the agent finished (step cap routes to `interrupted`, which
the harness records as `parked`). The implementations were already correct at that
point — groundtruth confirmed it.

**Summary for devstral:**
- When the implementation is wrong, it's wrong consistently (same bug across trials)
- When the implementation is right, the pipeline still rejects it (own buggy tests or step cap)
- Devstral's actual coding ability > what the pipeline success rate suggests

---

### minimax — 6/15 success, 14/15 groundtruth-correct (DATA: MIXED)

**Clean cells (6/15): cron_field and interval_merge**

minimax excelled here — fast, correct, clean merges:
- Average per-cell time: 83–454s (vs devstral's 391–914s)
- Average ticks to completion: 4–10
- All 6 reached `done` with gt=True, no reviewer friction

**Rate-limit-tainted cells (9/15): lru_cache, retry_backoff, token_bucket**

All 9 cells have gt=True (8/9) or gt=False for 1 specific bug (see below).
The implementations were correct. The pipeline parked them because:

1. Story reaches `tests_passed`
2. Review is attempted; reviewer (Claude) returns rate-limit message:
   `"You've hit your session limit · resets 8:20pm (America/Chicago)"`
3. `_parse_verdict` regex finds no `VERDICT: APPROVE|REQUEST_CHANGES` →
   returns `UNKNOWN`; pipeline treats this as REQUEST_CHANGES for rework-budget purposes
4. After 3 cycles: `parked_reason: "rework budget exhausted after 3 review cycles"`

Evidence from `retry_backoff__minimax__t0/plans/bench_retry_backoff_minimax_t0.manifest.json`:
```json
"review_verdict": "UNKNOWN",
"review_feedback": "You've hit your session limit · resets 8:20pm (America/Chicago)",
"parked_reason": "rework budget exhausted after 3 review cycles"
```

Some cells also show `"Review backend gated (Claude usage gate tripped): deferring review"`
in the notifications log before eventually parking — the usage gate deferral and the
reviewer-process rate-limit are two different code paths, both failing.

**One real minimax bug (retry_backoff t1, gt=False):**

Spec says: raise ValueError when `cap < base`. minimax also raised for `cap == base`
(equal case). Groundtruth test `test_cap_equal_to_base` caught this:
```
ValueError: cap must be > base, got base=5, cap=5
```
This is a genuine misreading of the spec — `cap < base` is the exclusive condition,
`cap == base` should be valid. This is the only minimax cell with a real code bug.

---

### sonnet — 2/15 success (DATA: ALMOST ALL INVALID)

Only 2 cells were real runs (no rate-limit hit at dispatch):
- `cron_field__sonnet__t1`: **done, gt=True** (425s)
- `interval_merge__sonnet__t0`: **done, gt=True** (320s)

Both real runs succeeded. The 13 remaining cells failed in 10–191s with
`"overageDisabledReason":"out_of_credits"` in the agent log. The credits were
consumed by the sonnet dispatch earlier in the same run (cron_field/t0 and
interval_merge/t0 and t1 also rate-limited at dispatch but ran longer before failing).

---

## Pipeline failure modes surfaced by this run

### Failure Mode A — Whole-suite pytest gate (devstral)

`check_story_status` runs pytest over the entire worktree including model-authored
test files. When the model writes a correct implementation but buggy test assertions,
the gate fails and the story goes to `failed`. The harness-owned acceptance oracle
passes (the model's code is correct) but the gate doesn't know to prefer it.

**Fix direction (Fix #1, previously identified):** When a story has an `acceptance`
block, `check_story_status` should run only the acceptance oracle tests to gate the
story, not the model's self-written tests. The model's tests can remain in the
worktree for the reviewer to read but should not be authoritative for the gate.

**File:** `pipeline_mcp_server.py`, function `check_story_status` / `_gate_tests`.

---

### Failure Mode B — Reviewer rate-limit treated as UNKNOWN verdict (NEW)

When the reviewer Claude process hits the usage rate limit, it returns a plain English
rate-limit message. `_parse_verdict` (regex `r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)"`)
finds no match → returns `UNKNOWN`. The pipeline's rework-budget logic counts UNKNOWN
against the budget (same as REQUEST_CHANGES). After 3 cycles, the story is parked with
"rework budget exhausted."

This means rate-limit events at the review stage silently park correct implementations,
burning the rework budget on non-review events.

**Fix direction:** Detect the rate-limit condition in `_parse_verdict` or in the
reviewer-call wrapper. If the response looks like a rate-limit message (contains "hit
your session limit" or "out_of_credits"), do NOT count it as a review cycle; instead,
defer and retry (like the dispatch-side usage gate does). A rate-limit should park the
review, not the story.

**File:** `pipeline_mcp_server.py`, `_parse_verdict` and/or `review_story` /
`_run_reviewer`.

---

### Failure Mode C — Shared Claude credits between dispatch and review (operational)

This run used a single Claude account for both sonnet dispatch calls and the reviewer.
The sonnet dispatch cells ran first and consumed enough credits that the reviewer
started hitting the rate limit partway through. This contaminated minimax results for
the latter half of the task list.

**Fix direction (operational):** Either:
1. Run the benchmark in multiple sessions separated by the rate-limit reset window, OR
2. Use `--models devstral minimax` first (no sonnet dispatch credits consumed), run
   `--models sonnet` in a separate session after a fresh credit window, OR
3. Configure a dedicated API key for the reviewer separate from the dispatch key

---

### Failure Mode D — Step cap too tight for devstral on complex tasks (operational)

`PIPELINE_LOCAL_MAX_STEPS=40` (set in `models.py`) is reached by devstral on
lru_cache and retry_backoff before the implementation is complete. The step cap routes
to `interrupted` (parked in harness), which is correct behavior (it's resumable), but
the harness doesn't resume — the cell just records `parked`.

Some of devstral's lru_cache cells had gt=True at park time — the implementation was
complete, but the agent hadn't yet run the final "confirm tests pass" loop and submit.

**Fix direction:** Raise `PIPELINE_LOCAL_MAX_STEPS` to 60 or 80 for benchmark runs
to give devstral more room. The token_bucket trial 0 from the prior session showed
devstral using ~36 steps; complex tasks may need more. Alternatively, the harness
could re-dispatch parked cells (resume them) up to a configurable limit.

---

## Per-task difficulty ranking (from clean devstral data)

| Rank | Task | Why |
|------|------|-----|
| Easiest | cron_field, interval_merge | minimax 3/3 each, sonnet 1/1 each when credits available |
| Medium | retry_backoff | devstral writes correct impl; minimax too (8/9 cells) |
| Harder | lru_cache | devstral writes correct impl but step-cap parks it; minimax too |
| Harder | token_bucket | devstral correct on 2/3 trials; t0 no file written at all |

cron_field is hardest for **devstral specifically** (wrong code, not just wrong tests)
but easiest for minimax and sonnet. This suggests a model-specific weakness in
devstral's handling of cron/step-syntax parsing logic.

---

## What to run next

### Priority 1 — Re-run with clean credits

Run `--models devstral minimax sonnet` AFTER a fresh rate-limit window, using
`--resume --workdir _runs/full_20260630` to skip the 6 clean minimax cells and only
re-run the 9 tainted minimax cells plus all 15 sonnet cells.

Before running sonnet cells, run only `--models devstral minimax` first to avoid
consuming reviewer credits during dispatch. Start `--models sonnet` afterward.

```bash
# Step 1: re-run tainted minimax cells (no sonnet dispatch, reviewer still used)
python matrix.py --models minimax --workdir _runs/full_20260630_clean --trials 3

# Step 2: after credits reset, run sonnet
python matrix.py --models sonnet --workdir _runs/full_20260630_clean --trials 3
```

### Priority 2 — Fix #1 (whole-suite gate → oracle-gated)

Implement in `pipeline_mcp_server.py`: when a story's `acceptance` block is set,
run only the acceptance fixture tests for the gate rather than the whole worktree
suite. This would unblock devstral's 8 correct-but-failed implementations.

Story to file: "When story has acceptance fixtures, gate check_story_status on
acceptance oracle only, not model-authored tests."

### Priority 3 — Fix #2 (reviewer rate-limit → defer, not park)

Implement in `_parse_verdict` or the reviewer wrapper: detect rate-limit responses
(string match on "session limit" / "out_of_credits" / HTTP 429) and raise a retriable
exception rather than returning UNKNOWN. The rework-budget should only decrement on
genuine REQUEST_CHANGES verdicts, not on infrastructure errors.

Story to file: "Reviewer rate-limit should defer (like dispatch gate) not consume
rework budget."

---

## Raw data pointers

| Artifact | Path |
|----------|------|
| All cell results | `tests/benchmark/_runs/full_20260630/` |
| Aggregate JSON | `tests/benchmark/_runs/full_20260630/results.json` |
| Printed scorecard (misleading) | `tests/benchmark/_runs/full_20260630/scorecard.md` |
| Agent log (per cell) | `<cell>/worktrees/<STORY-KEY>/agent.log` |
| Plan manifest (review verdicts) | `<cell>/plans/bench_*.manifest.json` |
| Notification log (gate events) | `<cell>/plans/bench_*.notifications.log` |
| minimax retry_backoff t0 manifest | `_runs/full_20260630/retry_backoff__minimax__t0/plans/bench_retry_backoff_minimax_t0.manifest.json` |

The devstral groundtruth failure text is in `result.json["groundtruth_tail"]` for each
cell — concrete pytest output showing exactly which tests fail and why.
