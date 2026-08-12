# Harness retro — token-context-a1 (2026-08-06)

Backfilled 2026-08-12 as part of clearing the retro-process backlog (see
`docs/plans/PLAN_RETROSPECTIVE_PROCESS_PLAN.md` §2.1). Written from git
history (commit messages, PR numbers) plus a detailed in-session memory
note captured across the plan's full lifecycle, including a multi-week
gap between the original suspicion and the plan actually landing.

Scope: `docs/plans/TOKEN_CONTEXT_OPTIMIZATION_PLAN.md`'s Step 0/A1 slice —
instrument the planner role's token/cache-hit logging to match what the
reviewer role already had, and retire a dead config knob
(`PIPELINE_REVIEW_MAX_TOKENS`). 3 stories, dependency-chained where noted,
dispatched local ~20B-class (`ollama/gpt-oss-20b-high`). PRs #243, #244,
#245, all merged 2026-08-06.

The most valuable part of this plan happened *before* any code was
written: a measurement step that overturned the plan's own founding
assumption.

---

## 1. Timeline

1. 2026-07-17 — Plan doc written from a user suspicion ("full context" is
   being sent to the planner/reviewer, wasting tokens). Initial code read
   found the suspicion **not confirmed** on the user-prompt side (planner
   already scoped to one story's `agent_instructions`; reviewer preloads no
   diff/files up front) — the actual waste candidates identified instead
   were duplicated system prompts with no prompt caching, a dead
   `PIPELINE_REVIEW_MAX_TOKENS` knob, and no input-context cap on the
   Claude reviewer. Plan explicitly gated everything else behind a Step 0
   measurement before committing to any fix.
2. 2026-08-03 — Step 0 probe attempted directly (two identical `claude -p`
   calls, inspect `cache_read_input_tokens` on the second) rather than
   dispatching a story for it. **Invalidated by the operator's own shell
   environment**: `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` silently
   redirected the probe calls to `glm-5.2:cloud` instead of Anthropic
   Sonnet — `--model sonnet` was silently ignored, and glm's `0`
   cache-field readings proved nothing about Anthropic prompt caching. The
   pipeline's own Claude driver already avoids this
   (`_first_party_claude_env()` strips the redirect vars), but an ad hoc
   shell probe outside that code path does not get that protection for
   free. Also discovered: a single trivial "Reply OK" call loaded 45,388
   input tokens from the CLI's own base system prompt and cost ~$0.23 —
   probe calls are not cheap, so this was paused rather than swept.
3. 2026-08-06 — Step 0 answered **without spending a single probe call**:
   `pipeline/review.py`'s existing reviewer instrumentation was already
   writing `cache_read_input_tokens`/`cache_creation_input_tokens` to
   `~/.claude/worktrees/review_token_costs.jsonl` on every live call. Real
   production records from 2026-08-04 showed `cache_read_input_tokens` in
   the hundreds of thousands to ~1.8M per call against `input_tokens`
   (fresh) of only 10–58 — caching was already overwhelmingly dominant for
   the reviewer role, in production, with zero new instrumentation needed.
   This directly killed the plan's original Step 1 (manually marking cache
   breakpoints) as unnecessary, at least for the reviewer role — the win
   it was chasing already existed.
4. Same measurement pass found a real, different gap: the **planner** role
   had zero equivalent data. `_run_planner`/`_run_rework_planner` never
   passed `cell_dir` into `complete()`, even though `worktree_path` was
   already in scope at both call sites. A second, independent bug was
   found alongside it: `complete()` hardcodes the literal `role="complete"`
   internally regardless of caller, so even once `cell_dir` is wired,
   planner and reviewer records still couldn't be told apart in the log.
   This — not the original caching hypothesis — became the actual scope of
   `token-context-a1`.
5. Plan ingested 2026-08-06, split into 3 stories to fit the local-dispatch
   ≤2-file cap: (1) `app/backend.py` only — add a `role` passthrough
   parameter to `complete()` on all 3 signatures; (2) `pipeline/planner.py`
   + `pipeline/server.py` — wire `cell_dir`/`role` into the planner-family
   calls, depends on (1); (3) `pipeline/review.py` + `REFERENCE.md` —
   retire (not truncate-and-wire) the dead `PIPELINE_REVIEW_MAX_TOKENS` /
   `PIPELINE_SECURITY_REVIEW_MAX_TOKENS` knobs, independent, no deps.
6. Stories 1 and 3 merged same day, PR #243 and #244 — **both required a
   direct-repair pass first**: the local executor's resumed run introduced
   unrequested adjacent changes not asked for by either story (a
   fabricated copyright header, a dropped `from __future__ import
   annotations`, a reformatted logging call, a duplicated logger line) that
   the reviewer's own APPROVE did not catch. Same corruption class already
   seen on `mcp-self-mod-notice`. Fixing story 1's regression required a
   companion test fix: 3 of the agent's own new tests asserted
   `inspect.signature(fn).parameters["role"].annotation is str`, which only
   holds when `from __future__ import annotations` is **absent** (it
   stringifies annotations) — restoring the accidentally-dropped import
   broke those tests until switched to
   `inspect.signature(fn, eval_str=True)`.
7. Story 2 merged same day, PR #245 — the scheduler picked it up
   automatically after `resume_plan` and it ran clean end-to-end with **no**
   direct repair needed, unlike 1 and 3: correct `cell_dir`/`role` wiring
   into both planner call sites, matching `review.py`'s existing pattern
   exactly. It did hit 2 failed `str_replace` matches on `server.py` (stale
   anchors after a resume-trim) but self-recovered by re-reading the file
   with `nl -ba | sed` before retrying — no rework cycle, no operator
   intervention needed.
8. Plan marked done: `TOKEN_CONTEXT_OPTIMIZATION_PLAN.md` updated with a
   status footer, `MATURITY_AND_UNIQUENESS_PLANS.md` A1 checkbox closed.
   Deferred items (prompt-caching the static system prompts, a Claude
   reviewer input-context cap) left un-ingested as candidates for a future
   plan, explicitly not silently dropped.
9. Both merges of stories 1/3 (#243/#244) went through manual merge, not
   `approve_merge` — GitHub Actions was billing-blocked at the time (see
   the separate `project_gha_billing_block_ci_gate` finding), under the
   standing local-CI-and-manual-merge authorization.

---

## 2. Learnings

**L1 — the plan's own founding assumption was wrong, and the fix was to
measure before building anything, not to trust the original suspicion.**
Both the informal 2026-07-17 code read *and* the formal Step 0 gate existed
specifically so a plausible-sounding hypothesis ("we're sending full
context, wasting tokens") wouldn't turn directly into implementation work.
It didn't: the actual dominant cost (system-prompt duplication without
caching) turned out to already be a non-issue in production, and the real
gap (planner has no observability at all) was different from and smaller
than the original hypothesis. **This is the plan's single most valuable
outcome and it happened before any story was dispatched.**

**L2 — an ad hoc verification probe run outside the pipeline's own
code paths does not inherit the pipeline's own environment protections.**
The pipeline's Claude driver strips provider-redirect env vars
(`_first_party_claude_env()`) specifically because a user's shell can
silently reroute `claude -p` calls to a different backend. A quick manual
probe run directly in the shell has no such protection and produced a
result (`cache_read_input_tokens: 0`) that looked like a real negative
finding but was actually measuring the wrong model entirely. **When
verifying a pipeline-internal behavior manually, replicate the
pipeline's own environment-sanitization, don't assume a bare shell call is
equivalent.**

**L3 (recurrence) — the same adjacent-content-corruption class from
`mcp-self-mod-notice` recurred here, on 2 of 3 stories, weeks later and on
a completely different kind of change** (a parameter-passthrough addition
and a config-retirement, not a wiring story). Concretely: a fabricated
copyright header, a dropped `__future__` import, an unrelated
reformatting, a duplicated line — none requested, none caught by review's
APPROVE. This confirms the class is general to local-dispatch resumed
runs, not specific to the wiring-heavy shape of the earlier plan. Whether
this predates or postdates the `edit-guard-enforcement` hard-block guards
landing (2026-08-03) needs checking against exact dispatch/resume
timestamps — if these two stories' resumed runs happened after the guards
shipped, this is the same open question as w3a-effective-config-provenance's
L3 (a guard gap that survived the hard-block plan); if before, it's
independent evidence that motivated it.

**L4 — a regression fix cascaded into an unrelated companion test fix for
a subtle reason (stringified annotations under `from __future__ import
annotations`), not a mechanical one.** Restoring an accidentally-dropped
import changed the *runtime type* of `inspect.signature(...).annotation`
from the string `str` to nothing evaluable without `eval_str=True` — a
correct repair still required understanding *why* the tests broke, not
just re-adding the missing line.

---

## 3. Concrete harness improvements, prioritized

- [ ] **P1 — determine whether L3's two corrupted stories predate or
      postdate the edit-guard-enforcement hard-block guards, and if they
      postdate them, treat this the same as w3a's open P0** (an
      unidentified gap in guards that were supposed to already cover this
      class). If they predate the guards, downgrade to confirmation-only.
- [ ] **P2 — a lightweight, reusable "replicate pipeline env sanitization"
      helper or documented recipe for ad hoc manual verification probes**,
      so a future one-off check doesn't silently measure the wrong backend
      the way the 2026-08-03 probe did. Low cost, directly reusable next
      time someone wants a quick manual sanity check outside the pipeline's
      own code paths.
- [ ] **P3 — the plan's deferred items (prompt-caching static system
      prompts, Claude-reviewer input-context cap) are still un-ingested.**
      Not urgent — Step 0's finding suggests the caching item specifically
      may no longer be worth pursuing at all now that production data shows
      it's largely already happening — but worth an explicit decision
      (drop vs. defer-with-reason) rather than leaving it implicitly open
      in a doc nobody is tracking.

---

## 4. What worked

- The plan's own Step 0 gate did exactly its job: it stopped 3 of the 4
  originally-suspected fixes from ever being built, because the
  measurement showed they weren't needed. This is the clearest example in
  any of the three retros written this session of "measure before
  assuming" (`feedback_verify_dont_assume`) paying off directly, not just
  as a general principle.
- Finding and using **existing** production instrumentation
  (`review_token_costs.jsonl`) instead of building new measurement
  infrastructure or spending probe calls saved real cost and answered the
  question faster than the original probe plan would have.
- The local-dispatch file-count cap correctly shaped the split (3 stories,
  one dependency edge) rather than being treated as a soft suggestion.
- Story 2's self-recovery from stale `str_replace` anchors (re-reading via
  `nl -ba | sed` instead of stalling or escalating) is a concrete example
  of local-dispatch resilience working as intended, worth noting alongside
  the two failure cases in the same plan.
- Deferred scope (Step 1, Step 4) was explicitly recorded as deferred with
  a reason, not silently dropped when the plan closed.

## 5. Status

Modes referenced: L3 is the same recurring adjacent-content-corruption
class as `mcp-self-mod-notice`'s L1 — not yet resolved whether this
instance is pre- or post-guard, see P1 above. No other Mode numbers
apply. `MATURITY_AND_UNIQUENESS_PLANS.md` A1 checkbox is closed per the
plan's own final step; this retro's P1/P2/P3 are not yet reflected there —
follow-up.
