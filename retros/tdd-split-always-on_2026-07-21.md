# Harness retro — always-on TDD-split ship (2026-07-21)

A look-back at shipping Story 2 (`6259f785`, "TDD-split always-on — remove the
`PIPELINE_TDD_SPLIT` toggle") of the `always-on-checklist-and-tdd-split` plan.
The gate logic shipped correct and tests pass, but the story reached master
**incomplete** and required a manual cleanup follow-up. This doc captures the
learnings and the concrete harness improvements surfaced, prioritized.

**Not a committed plan doc — a retro.** Read alongside
`MATURITY_AND_UNIQUENESS_PLANS.md` (A3: failure-mode rate) and
`project_dispatch_failure_modes` Mode 28.

---

## 1. Timeline (what happened)

1. Story 1 (`f343b436`, always-on planner) shipped cleanly — PR #153 (`b01a640`).
2. Story 2 dispatched to the production path: gpt-oss:20b implementer + sonnet
   reviewer, 3 rework attempts, the user chose "let it ride through rework."
3. Worker #1 wrote correct red tests (glm test-author phase) and the correct
   gate edit, but chased a pre-existing flaky-under-load test
   (`test_local_agent_read_heavy_loop_nudges_once_then_parks`) to step-cap —
   committed `d84accf` (WIP-named).
4. Reviewer REQUEST_CHANGES'd `d84accf` with **5 real findings**: no README
   section, stale `PIPELINE_TDD_SPLIT` comments (`server.py:970`,
   `planner.py:285`), a deleted safety-net test, a self-contradictory renamed
   test, WIP commit message. (11 other "failures" were pre-existing
   flaky-under-load read-heavy/repetition-guard tests — a separate
   test-stability issue, not Story 2's.)
5. Rework worker fixed only the one real test failure (`560829e`, planner.py
   +5/-1) then **stalled**: 4 consecutive no-tool-call steps ("we are stuck"),
   then an Ollama 500 killed it. It touched none of the 5 review findings.
   Committed `30e6b0e` ("WIP (llm error)") — a checkpoint whose only real
   change vs `d84accf` was the planner.py fix.
6. The gate marked `tests_passed` on `30e6b0e`. **This was NOT a false-pass** —
   tests genuinely pass there; the gate=tests contract was honored.
7. The reviewer re-ran on `30e6b0e` and **APPROVEd** — a near-identical diff
   with all its own prior findings still unaddressed (Mode 24 variant; slips
   the Mode 27 same-SHA guard because the SHA did change).
8. The scheduler's merge tick merged PR #154 (`466fa91`) as merged-but-
   incomplete. The abort was attempted but came too late (see §3).
9. Manual cleanup follow-up PR #155 (`f605628`) closed all 5 gaps, no behavior
   change, 1298 tests pass, code-reviewer APPROVE, CI green, merged.

---

## 2. Learnings

**L1 — gpt-oss:20b stalls on the doc/comment/test-coherence polish category.**
It does impl + test-fix work reliably, but consistently gives up (no-tool-call
stalls, "I cannot proceed due to constraints", then `done` with a failure
summary) on: writing a README section, cleaning stale comments, restoring a
deleted test, renaming a self-contradictory test, and writing a Conventional
Commits message. Two consecutive rework workers stalled on exactly these items.
This is the same shape as Mode 22 (`create_file` full-rewrite drops preserved
content) — a **work-shape** limit, not a capability ceiling on the logic.

**L2 — "let it ride through rework" is insufficient when the remaining work is
the polish category.** The rework loop assumes a fresh worker will make
incremental progress on the review's findings. When the findings are all
polish-category items the model stalls on, every rework attempt burns out on
the same items, the gate keeps advancing on passing tests, and the reviewer may
APPROVE the near-unchanged incomplete state. The loop cannot self-correct.

**L3 — the reviewer can APPROVE a near-identical diff with its own prior
findings unaddressed (Mode 28).** The Mode 27 same-SHA guard prevents
re-approval of an *unchanged* diff. It cannot detect "the SHA changed by a
trivial WIP checkpoint while the review's substantive findings were never
addressed" — that is a content/judgment gap, not a SHA-equality gap. The
reviewer is the last gate before merge and it failed open here.

**L4 — the gate is correct but narrow.** `tests_passed` only certifies the test
contract. Doc/comment/test-coherence/commit-message findings live entirely in
the reviewer's judgment. When the reviewer fails open (L3), nothing else
catches them. This is by design (gate=tests, review=the-rest), but it means the
reviewer's failure modes are merge-gating failure modes.

**L5 — abort-too-slow: a blocked intervention can lose to a running merge tick.**
When the wrong merge was identified, the fix was to kill the merge-tick PID. The
auto-mode classifier correctly denied that under the user's standing "let it
ride" directive (killing scheduler processes contradicts it), and the
AskUserQuestion round-trip to get explicit authorization took long enough that
PR #154 merged during it. The plan-pause attempt also lost the lock race three
times (`skipped:locked`) because an advance tick was holding the lock. By the
time the lock freed, the merge tick was already running its pre-merge pytest.

**L6 — pre-existing flaky-under-load tests are a real drain on local-model
workers.** 11 read-heavy/repetition-guard tests pass isolated on master but
fail under full-suite load. Two workers wasted ~18 steps each chasing one
(`test_local_agent_read_heavy_loop_nudges_once_then_parks`,
`test_dispatch_omits_resume_transcript_path_when_unset`). The per-target
repetition guard auto-parks the worker, burning the attempt. These flakes pre-
date Story 2 but actively mislead weak models.

---

## 3. Harness improvement areas (prioritized)

### P0 — Track prior findings' target paths; refuse silent re-approval (fixes Mode 28 AND Mode 24)

The highest-leverage fix. Today `review_story` guards on `last_reviewed_sha ==
HEAD` (Mode 27). Add: when a prior verdict was `REQUEST_CHANGES`, the next
review on a new SHA must check that the new HEAD's changed-paths set intersects
the prior findings' target files (at minimum the Blocking ones). If a Blocking
finding's target file was not touched since the last review, **downgrade to
`REQUEST_CHANGES`** (or escalate via `request_decision`) instead of allowing
APPROVE. This catches "trivial WIP checkpoint, findings unaddressed" (Mode 28)
and the original "identical diff, different verdict" (Mode 24) with one
mechanism. Requires the reviewer to emit structured findings with file targets
(it already produces prose; parse the `file:line` anchors it cites).

- **Where:** `pipeline/ci.py` `review_story` + the reviewer prompt/output
  parsing; store `last_review_findings` (list of `{file, severity}`) on the
  story alongside `last_reviewed_sha`.
- **Risk:** a reviewer that under-cites file targets could over-block. Mitigate
  by only applying the gate to findings it explicitly anchored to a file.

### P0 — Stabilize the flaky-under-load read-heavy/repetition-guard tests

11 tests pass isolated but fail under full-suite load. They actively mislead
local-model workers into chasing red herrings (L6) and trip the per-target
repetition guard. Either (a) fix the shared-state/order-dependence that makes
them flake, or (b) mark them `@pytest.mark.flaky` / move to a separate
non-blocking suite so a full-suite run is a trustworthy signal for the gate and
for agents.

- **Where:** `test_local_agent.py` (read-heavy loop nudge),
  `test_pipeline_mcp_server.py` (`test_dispatch_omits_resume_transcript_path_when_unset`),
  and the repetition-guard tests.
- **Why P0:** the gate and the workers both treat full-suite red as a real
  signal; flakes there are noise that directly causes wasted rework and
  false-confidence.

### P1 — Route rework to a stronger model when the remaining work is the polish category

When a rework cycle's open findings are all doc/comment/test-coherence items
(no logic/test failures remain), the gpt-oss:20b implementer will likely stall
(L1/L2). Options: (a) the planner/rework-planner detects the polish-only
finding shape and the rework dispatch upgrades the model (e.g. to Claude or a
larger local model) for that one cycle; (b) flag the story for a direct
(human-or-assistant) finish instead of burning rework attempts on a model that
stalls on the work shape.

- **Where:** `pipeline/server.py` rework gate + `_run_rework_planner`; needs a
  "finding-shape" classifier over the review feedback.
- **Cheaper partial:** surface in the dashboard when a story's rework findings
  are polish-only so a human can pre-empt the stall.

### P1 — Make "let it ride" safe for polish-category tails

Concretely: bound the *polish-stall* pattern. If a rework worker exits
(`done` with a failure summary / no-tool-call stall / step-cap) **without
touching any of the prior review's finding-target files**, do NOT advance to
`tests_passed` on the unchanged tip — route to `changes_requested` and
increment `rework_attempts` (mirror Mode 27's fix shape, but keyed on
"findings unaddressed" not "SHA unchanged"). This stops the loop from
feeding an incomplete tip to a reviewer that may APPROVE it.

- **Where:** `pipeline/ci.py` `check_story_status`, alongside the Mode 27
  HEAD-vs-`last_reviewed_sha` comparison.
- **Depends on:** the P0 finding-target storage.

### P2 — Faster merge-abort / pre-emptive pause

L5 showed the intervention path is too slow once a merge tick is running.
Options: (a) a one-shot "pause and do not merge" MCP call that queues ahead of
the merge step even mid-tick (cooperative, not a process kill); (b) separate
the merge adjudication into its own lock-free step so a pause between ticks is
guaranteed to land before the next merge; (c) when a story's last review was
`REQUEST_CHANGES` and the new tip's changed-paths don't intersect the findings
(P0), the merge step refuses on its own — making the abort unnecessary in the
common case.

- **Where:** `pipeline/server.py` `advance_pipeline` merge adjudication;
  `pause_plan` lock semantics.

### P2 — Gate should refuse `tests_passed`→review when findings are unaddressed

The gate currently advances to `tests_passed` whenever the tip's tests pass,
regardless of whether the rework addressed the prior review's findings. Add the
same "findings-target intersection" check (P0) at the gate: if the last verdict
was `REQUEST_CHANGES` and the new HEAD touches none of the finding targets,
keep the story in rework-eligible state rather than handing an incomplete tip
to the reviewer. Defense-in-depth with P0 (reviewer-side) and P1
(check_story_status-side).

- **Where:** `pipeline/ci.py` `check_story_status` test-gate path.

### P3 — Commit-message hygiene gate

A WIP-named commit (`"WIP (llm error)"`, `"WIP (parked on repetition)"`) reached
a reviewable tip. The checkpoint machinery writes these. Add a gate (or a
reviewer nudge) that a tip presented for review must have a non-WIP Conventional
Commits message — the checkpoint WIP commits are fine mid-flight, but the
*final* commit the agent offers for review should be rewritten before review.
(On Story 2 the squash-merge title saved us, but the WIP tip is what misled the
reviewer into treating a checkpoint as a finished attempt.)

- **Where:** `local_agent.py` done-path / `review_story` pre-check.

---

## 4. What worked

- The review cycle **did** correctly identify the 5 real findings on the first
  pass — the reviewer's analysis was sound; the failure was in *applying* it on
  the near-unchanged re-tip (L3), not in *producing* it.
- The same-model refusal, per-story opt-in, not-resuming, and marker safety
  nets all survived the toggle removal (the new `test_tdd_split_always_on.py`,
  9 tests, stayed green throughout).
- The glm test-author phase wrote high-quality portable red tests on the first
  try — consistent with the TDD-split experiment result (stronger-author split
  wins).
- The manual follow-up (PR #155) was cheap: 4 files, +82/-13, no behavior
  change, one code-reviewer pass. The cleanup itself is not the hard part; the
  hard part was that the pipeline couldn't do it autonomously.

---

## 5. Status

- Mode 28: **NOT fixed** (this doc's P0 is the fix).
- Modes 22, 24: still **NOT fixed** (P0's finding-target tracking covers 24 and
  the 28 variant; Mode 22's `create_file` work-shape steering is separate).
- A3 failure-mode rate: 24 → 28 discovered (still climbing, not bounded).
- Pre-existing flaky-under-load tests: **NOT fixed** (P0 stabilization).