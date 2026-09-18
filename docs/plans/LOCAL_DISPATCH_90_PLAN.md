# Local-dispatch 90% plan

**Goal:** ≥90% **first-pass-clean** for stories dispatched to non-Claude tiers
(on-device gpt-oss-20b and cloud open-weight models like glm/deepseek), measured over a
rolling window of the last 30 such stories. That is the initial target.

**First-pass-clean** means the story merged with no escalation, no park, no
triage, no model fallback, and no human rewrite of its brief. A story that
merges only after a stronger model or a human steps in counts as a miss. That
kind of success has never been the point of this pipeline.

Written 2026-09-18, after `gptoss-num-ctx-ceiling` escalated 4 of 4 stories.

---

## 1. Baseline (measured 2026-09-18)

These figures cover 419 stories dispatched on local-family backends across every
plan with a notifications sidecar:

| Outcome | Share |
|---|---|
| First-pass-clean | **65%** (Aug 66%, Sep 65%, so no trend) |
| Merged only after escalation or park | 28% |
| Merged only after a human patched the brief | 7% |

Top escalation and park reasons: rework budget exhausted after N review cycles (40),
no new commit after N rework redispatches (32 escalations plus 17 parks), off-task
drift parks (15), review inconclusive (19).

There were 324 REQUEST_CHANGES comments on 500 PRs since 2026-08-01. Categories
were matched by keyword, so treat the shares as approximate:

| Class | Share of change requests | Owner of the defect |
|---|---|---|
| Gate-synthesized test failure (suite red) | 46% | see split below |
| ↳ failures in **pre-existing test files the story never touched** | **48% of gate failures** (72/149) | **plan author** |
| Docs or comments stale or wrong | ~48% | **plan structure plus reviewer blindness** |
| Correctness bug | ~28% | executor |
| Missing negative or boundary tests | ~22% | test-author / executor |
| Wiring missing or dead code | 16% | plan (ungraded integration) |
| Scope creep / unrelated edits | 12% | harness (no scope gate) |

**Why the rate stayed flat:** `docs/failure_modes.json` records 57 modes, 44 of
them harness bugs, and each was fixed one at a time. Four things that dominate
the misses were never fixed:

1. **Plans conflict with the existing test suite.** A story that must change
   something a pre-existing test pins can't be won by an executor that is told
   "never touch tests". Today's rule told authors to grep. The grep in the
   2026-09-18 plan returned 22 files and missed the one that mattered.
2. **The reviewer never sees the brief.** `pipeline/review.py::_run_reviewer`
   passes the diff and general standards, but not `agent_instructions` or
   sibling stories. It grades every PR as a complete change, so it demands
   work that was deliberately given to a sibling story. The executor complies
   and goes out of scope.
3. **No mechanical scope boundary.** Nothing stops a merge that touches files
   outside the story. On 2026-09-18 an unrelated `scripts/pipeline-env.sh`
   edit merged, and a stray `httpx/` stub package broke 358 tests before it
   was caught.
4. **Infrastructure failures are charged to the story.** Only a 429 is
   deferred. 502s, refused connections and 180s timeouts on the reviewer use
   up `REVIEW_INCONCLUSIVE_MAX` and trigger escalation. Duplicate stories from
   a keyless re-ingest and `OLLAMA_NUM_PARALLEL=1` with 4 concurrent agents
   are warned about but never stopped.

---

## 2. Already done (2026-09-18, direct edits, uncommitted)

- New `.claude/rules/local-dispatch-preflight.md` is a mandatory per-story
  checklist. It covers the impact run (run the suite against a rough stand-in
  change instead of grepping), keeping code and docs in one story, comments
  that stay true after later stories, test sizing for doc/config stories, the
  weak-tier file cap including `.md`, rewriting briefs instead of stacking
  amendments, operational preconditions, and a recorded `Preflight:` line.
- `CLAUDE.md` Step 1.2 now requires that checklist for every non-Claude story.
- `pipeline-story-schema.md`: the existing-test-conflict rule now covers every
  story, not just API shape changes, and replaces grepping with the impact run.
- `agent-dispatch-story-sizing.md`: Markdown files count toward the ~1000-line
  cap, and the ingest warning does not check them.
- `agents/product-analyst.md` and the installed copy: the tier rules now take
  priority over vertical-slice advice. Added code and docs in one story,
  `tdd_split:false` for doc/config stories, the impact run, explicit keys, and
  `Preflight: NOT RUN` when the analyst can't run commands.
- Memory: the mission and this baseline.

These fix plan **authoring** when the author follows them. The workstreams
below make the **pipeline** enforce them, so success doesn't depend on
discipline during an incident.

---

## 3. Workstreams (in priority order)

The estimated gains below are rough attributions from the table above. They
overlap and are not additive. W0 exists to replace them with measurements.

### W0: Measure it (do first; everything else is judged against this)
- `pipeline/escalation.py`'s `escalating to …` notification (the most common
  escalation path) emits no `event=`, so `story_metrics` can't count it. Add
  `event="escalated"` there and on every park, and `event="brief_patched"`
  in `patch_story` whenever `agent_instructions` changes after the first
  dispatch.
- Add `first_pass_clean` to `compute_story_metrics`/`compute_plan_rollup`.
  Add a `scripts/local_success_report.py` that prints the rolling-30 rate by
  tier plus the reason breakdown, using the same definition as §1.
- **Done when:** the report reproduces §1's 65% from existing sidecars within
  ±3 points, and it runs at plan completion (see W7).

### W1: Stop dispatching stories that conflict with existing tests (est. +10–15 pts)
- **W1a ingest gate.** For non-Claude backends, `ingest_plan` rejects a story
  whose `agent_instructions` lacks a `Preflight:` line or starts with
  `Preflight: NOT RUN`. The error names the preflight rule. Allow an explicit
  override flag in the plan JSON for emergencies, and log each use.
- **W1b runtime classifier.** When the done-bar or gate suite fails **only**
  in test files that existed at the story's base SHA and that the branch did
  not modify, classify it as a *plan conflict*, not a rework cycle. Stop the
  weak-executor loop. Send the failure to the strong planner/overlord role
  with the failing tests and the story brief. Its ruling is either a
  pre-authorized test edit written into the brief (exact before/after plus a
  replacement assertion for the deliverable), or park-for-human with the
  conflict named. It never tells the executor to revert its deliverable.
  Charge nothing to the rework budget.
- **W1c steering fix.** `_NEVER_TOUCH_TESTS_STEERING`
  (`pipeline/test_author.py`) says "if a test fails, the bug is in the
  implementation". That line is what drove the plist story to re-add the key
  it was meant to delete. Reword: if a test **you did not write** fails and
  your change is what the brief asks for, stop and report the conflict. Never
  undo the brief's required change to make a test pass.
- **Done when:** a fixture story that deletes a survivor-listed key is
  rejected at ingest without a preflight line, and with one it reaches the
  planner ruling instead of a rework loop. The pre-existing-file share of gate
  failures (§1, 48%) drops below 10% over the next 30 stories.

### W2: Give the reviewer the brief (est. +8–12 pts)
- Pass the story's `agent_instructions`, the summaries of its sibling stories
  and their status, and any `Deferred to <sibling> — not a finding:` lines
  into `_run_reviewer`'s prompt. Instruct: work the brief explicitly assigns
  to another story is a Suggestion, not Blocking. Doc drift *caused by this
  diff* stays Blocking.
- Fix the garbled documentation criterion in `pipeline/review.py`. Item (4)
  was spliced into the middle of the documentation sentence, so the prompt
  reads "…does not prove delegation is real.that EXISTING callers/users
  already depend on…". The rule that decides whether missing docs are
  Blocking has been unreadable to every reviewer. Add a test that the
  rendered prompt contains the whole documentation sentence.
- **Done when:** a reviewer given a brief with a Deferred line doesn't
  REQUEST_CHANGES for that item (benchmark cell), and the docs/comments
  change-request share falls by half.

### W3: Machine-readable scope with a merge-time scope gate (est. +4–6 pts)
- Add an optional story field `files`: the production paths the story may
  change, excluding tests. Document it in `pipeline-story-schema.md`, because
  that file's "don't invent fields" rule makes the field meaningless until it
  is documented.
- Ingest: use `files` in place of the backtick-path regex in
  `_story_sizing_warning`. Count `.md` and repo-root files, and **auto-route**
  (set `backend` to the cloud-OSS tier) any story whose files exceed the
  weak-tier caps instead of only warning.
- Gate: before review, a branch diff that touches a non-test path outside
  `files` (or creates a new top-level package such as `httpx/`) fails with a
  specific message listing the paths. The executor gets that list, not a
  generic rework.
- **Done when:** the 2026-09-18 `pipeline-env.sh` and `httpx/` cases are
  blocked by the gate in regression tests.

### W4: Right-size the test-author phase (est. +3–5 pts)
- Skip the test-author phase automatically when every path in `files` is
  doc or config (`.md`, `.plist`, `.template`, `.json`, `.toml`, `.yaml`). One
  small test belongs in the brief.
- After the phase, if authored test lines exceed max(150, 5× the brief's
  estimated change), fail the phase back to the test-author with "cut to the
  assertions that grade the deliverable". Don't hand a 661-line oracle to a
  20B executor.
- Scope the test-author prompt's "EVERY requirement… docstring/comment
  updates count" push to structural assertions (the key exists, the row has 4
  cells, the name is present). Prose wording assertions are what the executor
  later drifts into editing.

### W5: Stop charging infrastructure to the story (est. +3–5 pts)
- Reviewer transport failures (502/503, connection refused, read timeout)
  defer like a 429 and never increment `review_inconclusive_count`. Keep the
  notification. After K consecutive deferrals, pause the plan instead of
  escalating the story.
- Gate dispatch on Ollama serving parallelism: when the runner's `-np` is
  below the number of in-flight local dispatches, don't start another. This
  is currently a warning (273 occurrences).
- Make re-ingesting an already-ingested plan with any keyless story a hard
  error unless `overwrite=True`, since it creates duplicates that dispatch
  concurrently.
- Quarantine known-flaky tests (`test_pipeline_env_sharing.py` allexport on
  3.14/ubuntu) with a tracking issue. An executor must never be sent to "fix"
  a flake. On 2026-09-18 one merged a no-op edit to `scripts/pipeline-env.sh`
  while doing so.
- Investigate: `22b78cd3` was pinned to `model: deepseek-v4.1-flash`, but its
  18:03 CI-rework redispatch recorded `dispatched_model=gpt-oss-20b-high:latest`.
  Find which path dropped the pin before trusting per-story pins.

### W6: Rework that converges (est. +3–5 pts)
- "No new commit after N rework redispatches" is the second-largest reason
  (49 events). Before the 2nd redispatch, the strong rebrief role must state a
  root cause (CLAUDE.md Step 9) and **replace** the rework brief rather than
  append to it. Follow the retro rule: restart from a fresh context at the
  last good commit, not a resumed cluttered transcript.
- Give `patch_story` a `replace_rework_brief` mode that keeps the original
  brief and swaps one clearly delimited rework block, so humans stop
  producing amendment stacks.

### W7: Close the learning loop
- `retros/PENDING.md` has about 170 plans waiting and 5 retros have ever been
  written, so the loop that should feed these lessons back is stalled.
  Replace the manual retro with an automatic per-plan report at plan
  completion: the W0 metrics, every escalation, park and patch event, and
  the categorized REQUEST_CHANGES findings from the plan's PRs. Write it to
  `retros/`.
- Clear the 2026-08-12 backfill in `PENDING.md` without writing those retros;
  the W0 report covers them in aggregate.
- Once a week (or every 30 local stories), review the rolling report. Any
  class above 5% of misses gets a rule or harness change, not a one-off fix.

---

## 4. Sequencing and how to run it

1. **W0**, so there is a real number to move.
2. **W1c + W2's prompt fix + W5's reviewer-transport deferral.** These are
   small, isolated changes that should pay off quickly.
3. **W1a/W1b, W2 (brief to reviewer), W3.** The structural changes.
4. **W4, W6, W7.**
5. Re-measure after each step, using the rolling-30 rate from W0.

**Execution tier.** Most of this edits `pipeline/dispatch.py` (1368 lines),
`pipeline/advance.py` (1232) and `pipeline/story_status.py` (1036). By this
repo's own sizing rule, those are not weak-tier files. Run these stories on the
cloud-OSS tier, each one preflighted per the new checklist. Stories that only
append to smaller files (`pipeline/review.py`, `pipeline/escalation.py`,
`pipeline/story_metrics.py`, `pipeline/test_author.py`) are good on-device
candidates and serve as the first test of the new rules.

**Stop criterion.** If the rolling rate isn't above 80% after W1–W3 land, the
remaining gap is executor capability, not process. At that point, re-benchmark
the on-device tier (`tests/benchmark/`) against glm/deepseek before adding more
harness guards.
