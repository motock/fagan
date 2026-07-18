# Plan — Production TDD-Split (tech-lead writes tests, weak local model implements)

**Status:** Scoped, not started. Written 2026-07-18, following the isolated-cell
experiment in `tests/benchmark/tdd_split_experiment.py` (see
`memory/project_tdd_split_experiment_result.md` for the raw result this plan is
based on).

**One-line thesis:** Splitting test-authorship from implementation into two
dispatches on the SAME worktree/branch — a strong model writes the test suite
first, a weak local model implements against it — beats both the weak model
authoring its own tests (same-model split: harmful) and today's monolithic
TDD-in-one-dispatch baseline, **provided** the test-author is meaningfully
stronger than the implementer.

---

## 1. Why this plan exists (and what it's NOT)

### 1.1 The experiment this is based on

`tests/benchmark/tdd_split_experiment.py`, isolated 3-way on `ratelimiter_inspect`
(n=1/arm, no reviewer/rework loop — pure single-dispatch capability):

| cell | impl groundtruth | phase-1 test quality |
|---|---|---|
| baseline (gpt-oss monolithic, no reviewer) | PASS (282s) | 11 tests, **valid=False** (3 wrong-expected), catchesFMG=True |
| A — gpt-oss writes tests → gpt-oss implements | **PARKED, no impl** (24s, read-loop) | 11 tests, valid=False |
| B — glm writes tests → gpt-oss implements | PASS (153s) | **31 tests, valid=True, catchesFMG=True** |

Three findings drive this plan:

1. **The 8/9 production run's `ratelimiter_inspect` park was reviewer-induced,
   not a gpt-oss capability limit** — the monolithic baseline here (no
   reviewer) succeeds solo. This plan is orthogonal to that finding; it does
   not fix the reviewer-park issue, it targets a different lever (test
   quality feeding the implementer).
2. **Same-model split (A) is actively harmful** — gpt-oss implementing against
   its OWN authored tests read-loop-parked in 24s without ever writing the
   impl file. Do not ship a same-model split.
3. **Stronger-author split (B) wins on both axes** — 31 valid, portable tests
   (vs 11 invalid ones) and a clean implementer pass. The lever is
   **test-author quality**, not the split mechanism itself.

### 1.2 What already exists in production that this reuses

`GUIDED_DECOMPOSITION_PLAN.md` (shipped, `PIPELINE_DECOMPOSE=off|cloud|local`)
already established the "strong tech-lead helps a weak jr executor" pattern in
this codebase, including:

- A pre-executor phase inside `dispatch_story` (`pipeline_mcp_server.py` ~3185)
  that runs before the main agent dispatch, in the same worktree/branch.
- Role-scoped backend/model resolution (`_resolve_planner_backend`,
  `role_registry.py`) — `PIPELINE_BACKEND_PLANNER` env / plan `role_config` /
  `model_registry.json`'s `roles` map, falling back to dispatch's own
  backend/model when unconfigured.
- Fail-open contract: a broken/slow/rate-limited planner call returns `None`
  and the story proceeds on the existing no-plan path — never a gate.
- The exact steering clause this plan's phase-2 executor needs already exists
  verbatim in `_REWORK_PLANNER_SYSTEM`: *"NEVER edit, rename, weaken, or
  delete the test files (anything matching test_*.py) to make a test pass -
  if a test fails, the bug is in the implementation, so fix it there."*

**What's structurally different and missing:** `_run_planner` is one bounded
`complete()` call that returns prose — it never touches the worktree or runs
tests. Test-authoring is agentic work (write file, run pytest, iterate) and
needs a **full agent-loop dispatch** (the same `backend.dispatch()` shape the
main executor already uses), not a single completion. That dispatch-phase
primitive does not exist yet and is this plan's core deliverable.

### 1.3 What this is NOT

- **Not** a replacement for `GUIDED_DECOMPOSITION_PLAN.md`'s checklist
  mechanism — orthogonal, can compose (a guided-decomposition checklist could
  still run inside either phase).
- **Not** validated against the reviewer/rework loop yet — the experiment
  deliberately omitted it (see §5, the standing verification gap).
- **Not** a story-splitting mechanism — this stays within ONE pipeline story,
  ONE worktree, ONE branch, two sequential dispatches. (The disproven
  product-analyst story-split experiment, `PRODUCT_ANALYST_VALIDATION_PLAN.md`,
  is the cautionary precedent for why NOT to split at the pipeline-story
  level — see `GUIDED_DECOMPOSITION_PLAN.md` §1 for the full writeup.)

---

## 2. Design

### 2.1 Where it injects

A new pre-executor phase inside `dispatch_story` (`pipeline_mcp_server.py`),
gated by a new flag `PIPELINE_TDD_SPLIT=off|on` (default `off` — secure/neutral
default, opt-in per Core Principles), inserted after worktree setup and before
the main executor dispatch (i.e. alongside, not replacing, the existing
`PIPELINE_DECOMPOSE` planner-checklist step — both can be active):

```
dispatch_story(story):
    ... existing worktree setup ...
    if PIPELINE_TDD_SPLIT == "on" and story allows split (see §2.4) and no
       existing test-author commit marker in the worktree:
        run test_author_dispatch(story.agent_instructions, cwd=worktree)  # NEW
        # blocks until exit, same poll-and-reap pattern backend.py's
        # OllamaDriver/ClaudeCliDriver dispatch already uses
    ... existing PIPELINE_DECOMPOSE planner-checklist step (unchanged) ...
    ... existing executor dispatch, prompt augmented per §2.3 ...
```

- **Test-author dispatch** is a full `backend.get_backend("dispatch",
  name=test_author_backend).dispatch(...)` call — the same primitive the main
  executor uses, not a bounded `complete()` — because writing a good test
  suite requires running pytest and iterating, exactly like the experiment's
  phase-1 cells did.
- Runs in the **same worktree** the executor will use, so its test file and
  commit are already present when the executor starts (mirrors the
  experiment's `seed_from_prev` worktree-seeding, but in-place rather than a
  separate worktree since production only ever has one worktree per story).
- **Idempotent on redispatch/rework**: if a rework redispatch reuses an
  existing worktree that already has a test-author commit, skip re-running
  the test-author phase — reworks act on the SAME tests, they don't get a
  fresh test-authoring pass. (Mirrors `GUIDED_DECOMPOSITION_PLAN.md`'s "no
  existing `.agent_plan.md` in worktree" reuse-check, same rationale.)

### 2.2 Backend/model resolution for the test-author role

New role name `"test_author"`, added the same way `"planner"` was — no schema
change needed (`model_registry.json`'s `roles` map and `role_registry.py`'s
`resolve_role` already accept arbitrary role-name keys; nothing is hardcoded
to a fixed role list). Mirror `_resolve_planner_backend` exactly:

```python
def _resolve_test_author_backend(
    dispatch_backend: str, local_model: str,
    plan_role_config: dict | None = None,
) -> tuple[str, str]:
    plan_cfg = (plan_role_config or {}).get("test_author", {})
    registry = role_registry.load_registry()
    provider_override = (
        plan_cfg.get("provider")
        or os.environ.get("PIPELINE_BACKEND_TEST_AUTHOR")
        or registry.get("roles", {}).get("test_author", {}).get("provider")
    )
    if not provider_override:
        # Unlike the planner (which defaults to mirroring dispatch), the
        # test-author's whole value proposition is being a DIFFERENT,
        # stronger model than the implementer -- so an unconfigured
        # install should NOT silently mirror dispatch (that reproduces the
        # experiment's harmful same-model variant A). Fail open to "off"
        # instead: no test-author dispatch runs, story proceeds exactly as
        # it does today. This makes PIPELINE_TDD_SPLIT=on a no-op until the
        # operator explicitly configures a test-author role/model, which is
        # the correct secure/neutral default (Core Principles) given A's
        # proven harm.
        return None, None
    resolution = role_registry.resolve_role(
        "test_author", plan_role_config=plan_role_config, registry=registry,
        model_fallback=lambda: None,
    )
    return resolution.provider, resolution.model
```

This is the single most important design decision in this plan: **same-model
fallback is explicitly refused**, not just discouraged. If `resolve_role`
returns the identical backend+model as `dispatch_backend`/`local_model`, the
phase must also skip (log a warning, proceed monolithic) — belt-and-suspenders
against reproducing variant A by misconfiguration (e.g. an operator sets
`PIPELINE_BACKEND_TEST_AUTHOR=local` pointing at the same Ollama endpoint/model
dispatch already uses).

### 2.3 Prompt content

- **Test-author prompt**: built from `story["agent_instructions"]` plus a
  fixed suffix instructing test-only scope — reuse the experiment's
  `phase1_prompt()` shape (write ONLY the test file, do not create the impl,
  confirm red state via pytest, cover negative/boundary cases per CLAUDE.md's
  testing standard). Generalize from the experiment's hardcoded
  `ratelimiter_inspect`-specific spec text to the story's own
  `agent_instructions`, since production stories vary per task (the
  experiment could hardcode `SPEC_RULES`; production cannot).
- **Executor prompt augmentation**: append the exact steering line already
  proven in `_REWORK_PLANNER_SYSTEM` — "fixes/implementation go in the file(s)
  named by the task; NEVER edit, rename, weaken, or delete the test files
  (anything matching test_*.py) — implement against them as given." No new
  prompt design needed; reuse verbatim.
- **Scope guard**: prompt-only steering ("do NOT create the impl file") is
  what the experiment used and it held for both gpt-oss and glm as
  test-authors. A harness-level guard (reject `create_file`/`str_replace` on
  the story's declared impl target until phase 2) would be more robust but is
  explicitly deferred — start with prompt steering (matches this repo's
  general pattern of trying the cheap fix first and hardening only if
  violated live, per `GUIDED_DECOMPOSITION_PLAN.md`'s own iteration history).

### 2.4 Which stories are eligible

Not every story is TDD-shaped (bugfixes with a single existing test file to
extend, refactors with no new tests, etc.). Gate on the same signal
`ingest_plan`/`dispatch_story` already have available: only run the
test-author phase when the story's `agent_instructions` calls for **new**
test file(s) for **new** functionality (heuristic: no acceptance fixture
present, or an explicit story-level opt-in flag — see §4 open question).
Default to **not** running the split unless a story explicitly opts in
(`story["tdd_split"] = true` or similar), rather than trying to infer
eligibility from prose — inferring wrongly and running an unwanted
test-authoring dispatch on a bugfix story is a worse failure mode than an
operator forgetting to opt in.

### 2.5 Fail-open semantics

Unlike `_run_planner` (failure → `None` → skip straight to dispatch, already
proven safe), a test-author dispatch is a full subprocess with its own
possible timeout/crash/rate-limit. On any failure (non-zero exit, timeout, no
new commit produced): **log it, do not retry, fall back to today's monolithic
executor dispatch** (the original TDD-mandated `agent_instructions`, no
pre-seeded tests) — must be an explicit, tested fallback path, not implied by
absence of code. This is the single most safety-critical property of the
whole feature: a broken test-author must never leave a story permanently
blocked or silently degrade to "no tests at all."

### 2.6 Cost

Not cheap like the planner call (one bounded `complete()`) — this is a full
second agent-loop dispatch, roughly the cost class of escalating the story to
Claude once. Only economically justified if it measurably reduces
implementer-side rework/escalation downstream. Track test-author dispatch
cost/tokens explicitly (mirrors how `GUIDED_DECOMPOSITION_PLAN.md` tracked
planner-token cost before trusting the mechanism) — this is a required metric
in the validation run (§4.3), not optional telemetry.

---

## 3. What's proven vs. what's NOT (read before shipping)

**Proven (isolated-cell, n=1, no reviewer):**
- Same-model split is harmful (variant A).
- Stronger-author split produces valid, portable tests and a clean
  implementer pass (variant B).

**NOT proven — the standing gap this plan must close before global rollout:**
- **The full `dispatch_story` → `review_story` → `approve_merge` path.** The
  experiment omitted the reviewer/rework loop entirely (no acceptance oracle,
  no rework redispatch). It is untested whether:
  - A rework redispatch (reviewer sends the implementer back with feedback)
    respects "don't touch the tests" under real pressure — the same class of
    directive-adherence question `GUIDED_DECOMPOSITION_PLAN.md`'s lru_cache t8
    trial surfaced (a stressed executor edited the forbidden test file when
    stuck). This is a real, previously-observed failure mode on a structurally
    similar steering directive, not a hypothetical.
  - The reviewer correctly evaluates code written against tests it didn't see
    authored (does it need the test-author's rationale/diff for context, or
    is the test file self-explanatory enough for review as-is?).
  - Merge-gate CI behaves normally when the "test-author" commit and the
    "implementer" commit are separate commits on the same branch (should be
    fine — `_worktree_has_new_commits`/rebase logic is commit-count-agnostic
    — but unverified in this exact two-actor-one-branch shape).
- **n=1 per arm, one task.** Directional, not statistical. No breadth-heavy
  task tried (same blind spot `GUIDED_DECOMPOSITION_PLAN.md` §4.2 called out
  for its own PA-validation precedent — a single reasoning-dense task can
  make either mechanism look better or worse than it generalizes to).
- **B's test-author was glm-5.2:cloud, not a differently-architected stronger
  model.** The production conservation config already routes Claude→glm, so
  this is likely the real deployed shape anyway, but if true Claude-authored
  tests are ever wanted as a comparison, that run is still unperformed.

---

## 4. Implementation phases

Each phase is a pipeline story (`ingest_plan`), TDD, one concern per PR
(< ~400 LOC), gated through `review_story` → `approve_merge` per the Agent
Workflow. See the accompanying `TDD_SPLIT_PRODUCTION_PLAN.json` for the
ingest-ready story definitions (`ingest_plan`'s actual schema — `summary` +
`agent_instructions` + `dependencies`, not prose criteria).

1. **`_resolve_test_author_backend` + role wiring** (no dispatch change yet).
   Unit-testable in isolation: role_config → env var → registry → same-model
   refusal → `(None, None)` when unconfigured. This is the safety-critical
   "never silently reproduce variant A" guard — test it exhaustively before
   anything else.
2. **Test-author dispatch phase in `dispatch_story`**, gated
   `PIPELINE_TDD_SPLIT=off` (default). Fail-open to monolithic on any error
   (§2.5). Idempotent on rework/redispatch (§2.1). Unit tests: phase runs only
   when flag on AND role resolves to a genuinely different backend/model;
   skipped and logged when role unconfigured or resolves to the same
   backend/model as dispatch; failure/timeout falls back to monolithic
   dispatch with the ORIGINAL agent_instructions unchanged; rework redispatch
   with an existing test-author commit in the worktree does not re-run the
   phase.
3. **Story-level eligibility gate** (§2.4) — explicit opt-in field, not
   inference. `ingest_plan` accepts and validates it; absent/false means
   today's unchanged behavior.
4. **Executor prompt augmentation** — splice the proven "don't touch tests"
   steering line (reused verbatim from `_REWORK_PLANNER_SYSTEM`) into the
   executor's prompt whenever a test-author phase actually ran this
   dispatch.
5. **Validation run** — the standing gap from §3. Minimum bar before this
   ships as a real default anywhere:
   - One story, real config, real reviewer (glm or Claude) in the loop,
     rework cap ≥ 2 so a REQUEST_CHANGES can actually redispatch.
   - Confirm: implementer respects "don't touch tests" through at least one
     real rework cycle; reviewer produces a sane verdict; merge-gate CI and
     the rebase/merge path work normally with the two-commit-per-story shape.
   - Record cost (test-author dispatch tokens/wall-time) alongside outcome,
     per §2.6.
   - Small n, stop on directional signal (`feedback_experiment_scope_and_polling`
     — do not expand to a full matrix before this).
6. **Ship-or-kill decision**, recorded in this file's Results section (same
   style as `GUIDED_DECOMPOSITION_PLAN.md`): does it clear rework/escalation
   reduction net of test-author dispatch cost? Does directive-adherence hold
   under real reviewer pressure?

---

## 5. Risks & open questions

- **Directive-adherence under rework pressure** (the single biggest risk) —
  `GUIDED_DECOMPOSITION_PLAN.md`'s lru_cache t8 trial already showed a
  stressed executor violating an analogous "never edit the test files"
  steering line when stuck. This plan's whole safety property rests on that
  line holding; it must be checked explicitly in the validation run (§4.5),
  not assumed from the rework planner's existing (different-context) success.
- **Same-model misconfiguration risk** — an operator could set
  `PIPELINE_BACKEND_TEST_AUTHOR` to something that resolves to the same
  concrete model as dispatch without intending to (e.g. same Ollama endpoint,
  different env var, same underlying tag). The same-model refusal (§2.2) must
  compare RESOLVED backend+model, not just the config source, to catch this.
- **Cost vs. benefit** — untested whether the rework/escalation reduction
  (if any) outweighs a full second dispatch's cost on every eligible story.
  This is the plan's own kill criterion if the validation run doesn't show a
  clear net win.
- **Eligibility inference** — §2.4 punts to explicit opt-in rather than
  solving "does this story need new tests" generally; a future refinement
  could infer it from `agent_instructions`/acceptance-fixture absence, but
  that's a separate, harder problem deliberately deferred.
- **Interaction with `PIPELINE_DECOMPOSE`** — both flags can be on
  simultaneously (test-author phase, then guided-decomposition checklist,
  then executor). Untested whether the checklist planner should be told a
  test-author phase already ran (so its checklist doesn't redundantly include
  "write tests" as its own step). Worth a quick check in phase 4, not a
  blocker for shipping either flag alone.

---

## 6. Relationship to prior work in this repo

- Builds directly on `GUIDED_DECOMPOSITION_PLAN.md` — reuses its dispatch-time
  pre-executor-phase pattern, its role-resolution shape, its fail-open
  contract, and its exact "don't touch the tests" steering line.
- The isolated experiment this plan formalizes:
  `tests/benchmark/tdd_split_experiment.py` /
  `memory/project_tdd_split_experiment_result.md`.
- Explicitly does NOT re-litigate pipeline-story-level splitting — see
  `PRODUCT_ANALYST_VALIDATION_PLAN.md` (referenced via
  `GUIDED_DECOMPOSITION_PLAN.md` §1) for why that already failed.

---

## Results

*(Not yet run — this section is filled in during phase 5.)*
