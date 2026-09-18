# Local-dispatch plan preflight (mandatory before `save_plan`/`ingest_plan`)

> Added 2026-09-18 after the `gptoss-num-ctx-ceiling` plan escalated 4 of 4
> stories, and a cross-plan audit showed the first-pass-clean rate for
> non-Claude dispatch flat at ~65% from August to September. Consolidates
> the pre-ingest steps that `pipeline-story-schema.md` and
> `agent-dispatch-story-sizing.md` describe as advice into one checklist
> with a pass/fail answer per story. Those two files stay the reference
> for *why*; this file is the *gate*.

**The point of this pipeline is local and open-weight models.** A plan that
only succeeds on Claude/frontier models has failed its purpose. Every story
dispatched to a non-Claude tier must pass all of the checks below, and the
plan author (not the executor, not the reviewer) owns them. A weak executor
cannot recover from a plan defect; it can only burn its rework budget on it.

## Why a checklist and not more prose

The rules that would have prevented today's failures already existed. They
were not skipped out of neglect — the author searched, but the checks were
advisory and phrased too narrowly to fire:

- The author grepped `tests/` for `PIPELINE_LOCAL_NUM_CTX`, got **22 hits**,
  opened the two with obvious names, and missed the one survivor-list test
  that made the story unwinnable. A keyword grep answers "which tests mention
  this?"; the question that matters is "which tests FAIL if I make this
  change?" — and only running the suite answers that.
- The existing-test-conflict rule was worded for API shapes ("return value,
  request/response body… strict `==`"). A config-key deletion and a doc
  insertion did not pattern-match it.
- A follow-up story written mid-incident got no test search at all.

Across 500 PRs since 2026-08-01, **72 of 149 gate-synthesized test failures
(48%) were in pre-existing test files the story never touched.** That is the
single largest avoidable failure class, and it is entirely a plan-authoring
defect: no executor, however strong, may fix a test it was told never to touch.

## The checklist (every non-Claude story, every time — including follow-ups written mid-incident)

### 1. Impact run — run the suite, don't grep it
For each story, in a scratch worktree off current `master`
(`git worktree add /tmp/preflight-<story> master`):
1. Apply a rough stand-in of the story's production change — sloppy is fine
   (delete the key, insert placeholder text in the target doc section, stub
   the new function). The goal is to trip every pre-existing test that pins
   what the story touches, not to implement the story.
2. Run the full suite with the repo's venv (`.venv/bin/python -m pytest -q`)
   and `ruff check .`.
3. Every failure in a test file the story does **not** create is a conflict.
   For each one, do exactly one of:
   - **pre-authorize** it in `agent_instructions`: name the file, the test, and
     the literal before/after text, plus the commit-message justification
     (CLAUDE.md Step 4) — and add a replacement assertion that grades the
     story's real deliverable if the edit removes one (see the plist story's
     EDIT 2 pattern: dropping a survivor entry must be paired with an absence
     assertion, or the story can pass as a no-op);
   - **re-scope** the story so it doesn't collide (e.g. insert new doc text
     outside a verbatim-pinned section);
   - **split** the reconciling test edit into its own prerequisite story.
4. Also note any failure that reproduces on untouched `master` — that is a
   broken baseline; fix or quarantine it before dispatching anything.
5. Remove the scratch worktree.

High-risk test shapes to look for when reading the failures — each one has
cost a full story on this repo:
- exact equality on a dict/list/string (`==` on a request body, `__all__`, a prompt)
- **survivor lists / negative controls** ("these keys must still be present")
- **verbatim / byte-for-byte section checks** (`test_readme_reference_split.py`
  pins REFERENCE.md's moved sections against the pre-split README — any
  insertion *inside* those sections fails)
- SHA-256 / hash pins of a file or region
- exact total counts (row counts, tool counts, line counts)

### 2. The reviewer cannot see the plan — design for that
The reviewer is shown only the diff and general standards, not the story's
brief or its siblings. It grades every PR as if it were a complete change.
- **Keep a code change and its documentation in the same story.** If
  REFERENCE.md/README must change because the code changed, that edit
  belongs in the code story. Splitting them guarantees a "docs not updated"
  REQUEST_CHANGES that tells the executor to do the sibling's work.
  (If the doc file is too large for the tier, route the whole story up a
  tier rather than splitting the doc off.)
- **Write comments that stay true after the next story merges.** Never
  "inert until the sibling story removes X" or "today the env var is set to
  16384" — write the invariant ("an explicit PIPELINE_LOCAL_NUM_CTX overrides
  this table entry"). A time-relative comment becomes a Blocking finding
  against whichever later story makes it false.
- If a deliberate deferral is unavoidable, the brief must say so in a
  `Deferred to <exact sibling summary> — not a finding:` line, and the PR
  description must carry it verbatim.

### 3. Size the tests to the change
- **Doc-only or config-only stories:** set `"tdd_split": false` and
  prescribe at most one small test (locate the row/key by unique text and
  assert its cells/value). The always-on test-author phase otherwise writes
  hundreds of lines of prose assertions (661 lines for a 3-row table edit on
  2026-09-18) that the executor then drifts into editing.
- **Any tier:** if the prescribed tests would be more than ~5x the
  production change, the requirement list is over-specified — cut it to the
  assertions that grade the deliverable.

### 4. Size the story to the tier
Apply `agent-dispatch-story-sizing.md` in full. Additionally, for the
weakest (on-device ~20B) tier:
- `.md` files count toward the ~1000-line file-size cap. REFERENCE.md
  (~1330 lines) and README.md are **not** weak-tier files — route stories
  that edit them to cloud open-source or Claude. The ingest-time sizing
  warning does not check `.md` or repo-root files, so check by hand.
- One production concern per story; ≤2-3 new functions; no deletion without
  a survivor list.

### 5. One brief, rewritten — never an amendment stack
When a story needs rework instructions, **rewrite** `agent_instructions` as a
single coherent brief that states the current branch state and the remaining
work. Do not append `REWORK SCOPE` / `AMENDMENT` / `SUPERSEDED` /
`CORRECTION` layers: a 20B model resuming mid-transcript cannot reliably
resolve which of three contradictory layers wins (the plist story reached
~10.7k characters of layered instructions, including "do NOT edit
REFERENCE.md" followed by "run `git checkout master -- REFERENCE.md`").

### 6. Operational preconditions (check once per plan, before the first dispatch)
- Every story in the plan JSON has an explicit `key` (re-ingest without keys
  mints duplicates that dispatch concurrently — see memory
  `project-ingest-duplicate-stories`).
- `OLLAMA_NUM_PARALLEL` on the running Ollama ≥ `PIPELINE_MAX_CONCURRENT_AGENTS`
  from the **installed** LaunchAgent (not the repo template), or set concurrency
  to 1. The "serving parallelism below MAX_CONCURRENT_AGENTS" notification
  is a stop sign, not noise.
- A per-story `model` pin resolves to a tag declared in the live registry
  (`PIPELINE_MODEL_REGISTRY_PATH`, not the checked-in `model_registry.json`).

## Record the result
Put a one-line `Preflight:` summary at the top of each non-Claude story's
`agent_instructions` — e.g. `Preflight: impact run 2026-09-18 on 7ac2a04 —
0 pre-existing failures` or `— 1 conflict (test_x::test_y), pre-authorized
below`. A story without that line has not been preflighted.
