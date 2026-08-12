# Plan retrospective process

**Status:** partially implemented. The `mark_story_done` completion signal
(§2, transient `plan_completed: true` return value) shipped 2026-07-21
(#156) and works, but the loop it was meant to feed went dormant
immediately: it depends on an interactive session catching that return
value mid-conversation, and across 100+ plans completed since then, that
happened exactly zero times outside the day it shipped. Confirmed
2026-08-12 — see `retros/PENDING.md` for the backlog this gap left behind
and §2.1 below for the durable-marker fix filed to close it
(`retro-pending-marker` plan, story `b25dc05a-...`, pending pipeline
dispatch as of this writing).
**Scope:** institutionalize what has so far happened ad hoc three times
(`HARNESS_RETRO_TDDSPLIT_2026_07_21.md` and its two unnamed predecessors baked
into `MATURITY_AND_UNIQUENESS_PLANS.md`'s Modes list) — a retro written after a
plan finishes, capturing what went well/poorly and turning it into prioritized
harness-improvement items. This is a **process + light-hook** plan, not a new
subsystem: no sandboxing, no new agent role, no scheduler changes.

---

## 1. Goal

Every plan run through the pipeline (`ingest_plan` → stories → `mark_story_done`
on all stories) should end with a short, structured retro that:

1. Records the timeline of what actually happened (including the messy parts —
   rework loops, stalls, false-passes), not just the outcome.
2. Names concrete learnings, each tied to evidence (a commit SHA, a test name,
   a log line), not vague impressions.
3. Produces prioritized (P0–P3) harness-improvement items — the same shape
   already used in `HARNESS_RETRO_TDDSPLIT_2026_07_21.md` §3.
4. Is **persisted** somewhere durable and **discoverable** by a future session
   without the user having to remember it exists.
5. Feeds back into the one place that already tracks the harness's failure
   surface over time (`MATURITY_AND_UNIQUENESS_PLANS.md` A3), so retros
   accumulate into a trend, not a pile of unread documents.

## 2. Where this fits

Three ad hoc precedents already exist — `HARNESS_RETRO_TDDSPLIT_2026_07_21.md`,
the Mode 22–24 write-up folded into `project_dispatch_failure_modes.md`, and
`MODE_20_CORRECT_BUT_REJECTED_PLAN.md`. All three:

- live as a **plain markdown file at the repo root**, matching every other
  `*_PLAN.md` doc in this repo (no new file-format or tool was invented for
  them);
- get a **pointer memory** written afterward (e.g.
  `project_harness_retro_tddsplit_2026_07_21.md` in auto-memory) so a future
  session recalls the retro exists;
- get their P0/P3 items **manually copied** into `MATURITY_AND_UNIQUENESS_PLANS.md`
  (this step has been inconsistent — A3's Mode count was updated but the
  specific P0/P1 fixes from this retro are not yet reflected there).

This plan keeps that shape and only fixes the inconsistent last step, plus adds
one small trigger so writing the retro isn't purely a "remembered to do it"
event. **No new subsystem, no new markdown convention, no new agent persona.**

Concretely:

- **Where retros live:** a new `retros/` directory (not the repo root — root
  already has 30+ `*_PLAN.md` files and retros are a distinct, growing
  category with their own lifecycle). One file per plan:
  `retros/<plan-slug>_<YYYY-MM-DD>.md`. `HARNESS_RETRO_TDDSPLIT_2026_07_21.md`
  moves there as the first entry (`retros/tdd-split-always-on_2026-07-21.md`),
  with a redirect line left at the old path pointing to the new one (or a
  plain `git mv`, since nothing outside auto-memory prose references the old
  path by exact filename).
- **Index:** `retros/INDEX.md` — one line per retro (plan name, date, link,
  one-sentence outcome), newest first. This is the discoverability fix: a
  future session (or the user) can scan one short file instead of grepping
  the root for `*RETRO*`.
- **Feedback into A3:** every retro's P0/P1 items get a corresponding bullet
  under a new `MATURITY_AND_UNIQUENESS_PLANS.md` subsection (§3 below) so the
  maturity backlog and the retros stay in sync instead of the backlog only
  recording the Mode *count*.
- **Trigger:** `mark_story_done` (`pipeline/server.py:1603`) already flips the
  last per-story piece of state; it has no concept of "the plan is now fully
  done" today (verified — no `plan status` / `all stories done` check exists
  anywhere in `server.py`). Add one: after writing `"status": "done"`, check
  whether every story in the manifest is now `"done"`. If so, return
  `{"ok": True, "plan_completed": True, "retro_pending": True, "stories": [...]}`
  in addition to today's `{"ok": True}`. This is the one code change in this
  plan — everything else is process + file moves.
- **Who writes it:** the interactive Claude Code session that called
  `mark_story_done` and saw `plan_completed: true` — not a dispatched pipeline
  story, and not a new autonomous agent. Retro-writing needs judgment
  (deciding what's a real learning vs. noise, prioritizing P0 vs P3) that the
  existing dispatch/review loop isn't built to grade against an oracle. This
  mirrors how the three precedent retros were actually produced: written
  in-session by Claude, not dispatched.

## 2.1. Durable marker (fixing the dormancy)

The original design in §2 relied on the interactive session noticing
`plan_completed: true` in a single tool-call return value. In practice that
signal is easy to miss mid-conversation, and it was missed on every one of
the 58 pipeline-repo plans that went fully done between 2026-07-21 and
2026-08-12 (only the 2 plans that completed on the shipping day itself got
a retro). A transient return value with no persisted state is not
"discoverable by a future session" per §1 goal 4 — it disappears the
instant the turn that received it ends.

The fix (filed as the `retro-pending-marker` plan): when `mark_story_done`
detects a plan's last story going done (§2's existing check), it now also
appends an idempotent line to `retros/PENDING.md` — but **only when the
manifest's `repo_root` equals this pipeline repo's own root**. That scoping
rule is deliberate, not an oversight: `repo_root` already distinguishes
pipeline self-improvement plans from the far larger set of external
game/app plans dispatched *through* this pipeline (checked against real
data — every plan that has ever received a retro has `repo_root` pointing
at this repo; zero of the ~70 external game-dev plans do). Retros are about
harness improvement, not grading every dispatched feature; widening the net
to all plans would flood `PENDING.md` with noise the retro process was
never meant to carry (see "Log signal, not noise" in this repo's
CLAUDE.md).

`retros/PENDING.md` is the new discoverability surface `§1` goal 4 asked
for: a plain file, in the same directory as `INDEX.md`, that any future
session sees just by looking at `retros/`. Once a retro is written for an
entry, remove that entry's line from `PENDING.md` by hand — the same manual
curation `INDEX.md` already gets (§2's existing precedent), not automated
(§6 still applies: retro *authorship* stays out of scope for automation,
only the "don't forget it needs one" signal is now durable).

## 3. Retro template (content requirements)

Every retro must have these sections, in this order — `HARNESS_RETRO_TDDSPLIT_2026_07_21.md`
is the reference example for all five:

1. **Timeline** — numbered, factual, one line per event, each tied to a commit
   SHA / PR number / test name / log excerpt where one exists. No editorializing
   here; save judgment for §2.
2. **Learnings** — each learning names the mechanism, not just the symptom
   ("the reviewer can APPROVE a near-identical diff with its own prior findings
   unaddressed" — not "review sometimes flakes"). Cross-reference an existing
   Mode number in `project_dispatch_failure_modes.md` if the learning is an
   instance of a known mode; mint a new one if it's genuinely novel.
3. **Harness improvement areas, prioritized P0–P3** — P0 = actively causing
   merged-but-wrong or blocked work; P1 = degrades reliability but has a
   workaround; P2 = quality-of-life; P3 = cosmetic/hygiene. Each item names the
   file/function it would touch, so it can be filed as a story directly.
4. **What worked** — do not skip this. A retro that only lists failures drifts
   the harness away from approaches already validated (mirrors the
   `[[feedback_verify_dont_assume]]`-style discipline already applied to user
   feedback; the same discipline applies to the harness's own retros).
5. **Status** — which Modes this retro fixed/confirmed/left open, and the
   current trend on `MATURITY_AND_UNIQUENESS_PLANS.md`'s A3 mode-count metric.

## 4. Rollout

This is small enough to implement directly rather than filing a multi-story
plan, but the one code change (the `mark_story_done` completion check) still
goes through the normal CLAUDE.md workflow — TDD, `review_story`, the usual
gates — since it touches `pipeline/server.py`. Sequence:

1. `git mv HARNESS_RETRO_TDDSPLIT_2026_07_21.md retros/tdd-split-always-on_2026-07-21.md`;
   write `retros/INDEX.md` with that one entry.
2. Add the P0/P1 items from that retro to `MATURITY_AND_UNIQUENESS_PLANS.md`
   as a new subsection (see §5 below for the actual content).
3. File one pipeline story for the `mark_story_done` completion-detection
   change: red test first (`mark_story_done` on the *last* remaining `todo`→`done`
   transition returns `plan_completed: true`; on a non-last transition it does
   not; on a plan with zero stories it does not crash), then the ~5-line
   implementation.
4. Update this doc's Status line once shipped.

## 5. Harness improvements sourced from `HARNESS_RETRO_TDDSPLIT_2026_07_21.md`

Carrying the retro's own prioritization forward (do not re-derive — the retro
already did the analysis):

- **P0 — track prior findings' target paths; refuse silent re-approval**
  (fixes Mode 28 and Mode 24). Highest leverage: this is the one gap that let
  an incomplete story merge. Recommend picking this up **next**, before
  another plan ships through the always-on TDD-split/planner path and hits
  the same failure again.
- **P0 — stabilize the flaky-under-load read-heavy/repetition-guard tests.**
  Independent of P0 above; recommend running this in parallel since it's a
  test-file-local fix with no gate-logic risk.
- **P1 — route rework to a stronger model when remaining findings are
  polish-only**, and **P1 — bound the polish-stall pattern in `check_story_status`**
  (depends on the P0 finding-target storage, so sequence after it).
- **P2/P3** (faster merge-abort, gate-side findings check, commit-message
  hygiene) — lower urgency per the retro's own ranking; leave in the backlog.

This plan does not re-argue those priorities; it exists so they don't stay
stranded in a single retro file that the next session has to remember to
re-read.

## 6. Out of scope

- Automating retro *authorship* (an LLM-graded retro-writer story). Retros are
  judgment calls about what mattered; dispatching them to the same local-model
  pipeline being critiqued is circular, and no oracle exists to grade "is this
  retro's prioritization correct."
- A dashboard view for retros. `retros/INDEX.md` is sufficient until there are
  enough entries to make a flat file unwieldy (revisit past ~15–20 entries).
- Retiring the per-Mode entries in `project_dispatch_failure_modes.md`. Retros
  and the Mode catalog are complementary: the catalog is the flat failure-mode
  registry across all sessions, retros are the narrative per-plan account that
  produced some of those Mode entries.
- Changing anything about per-story review/merge gating. This plan is about
  what happens *after* a plan's stories are all done, not the per-story gates
  (those are `MERGE_CI_REWORK_PLAN.md` / the retro's own P0–P2 items above).
