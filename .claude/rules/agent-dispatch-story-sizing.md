# Story sizing for agent dispatch

> Extends `.claude/rules/pipeline-story-schema.md`'s "Local (non-Claude)
> dispatch — hard-won rules" section. That section's ≤2-production-file cap
> is necessary but not sufficient — every failure below satisfied the file
> cap and still overwhelmed the executor. Read both together when authoring
> a story for local or weaker-than-Claude dispatch; each rule here is a
> distinct axis the file-count cap does not capture.

## Function count, not file count, is the real budget

Cap a single dispatch story at ~2-3 **new functions**, not just ≤2
production files. Function count is the actual step/context budget the
executor is working against — the file cap doesn't bound it.

**Not hypothetical:** a story touched exactly ONE file, so the ≤2-file cap
held, but its brief prescribed 6 distinct functions (a render helper, a
second render helper, an event-wiring function, a fetch call, plus exports).
The local executor burned three multi-hour watchdog timeouts thrashing
across the six subtasks before the story had to escalate to a stronger
model — one file hid a lot of work.

**How to apply:** when authoring the brief, count the new functions/handlers
the story creates. If more than ~3, split: separate pure/render logic from
side-effecting wiring logic into a dependent follow-up story rather than
asking one dispatch to produce both.

## File size gates the dispatch tier, independent of model tier

A story that edits a large existing file (roughly 1000+ lines) should not
run on the weakest dispatch tier, regardless of how capable that tier's
model otherwise is.

**Not hypothetical:** across two separate plans, every story limited to
markup/config files landed in a single local-model shot with no struggle,
while every story touching one specific ~1500-line file hit a step cap,
a watchdog timeout, or required escalation — with no exceptions, on both a
local and a stronger cloud open-source model. The mechanism: the file-view
tool truncates a large file, context eviction makes an earlier view stale,
and the model falls back to line-number-based edits against now-stale line
numbers — corrupting the file. Prescribing anchored (find-this-exact-text)
edits instead of line-number edits helps but does not fully prevent this;
models revert to line-number edits once their context has been evicted.

**How to apply:** before dispatch, check the line count of every production
file a story touches. If any file exceeds ~1000 lines, route that story to
a stronger executor tier — do not leave it on the default/weakest tier.
Better still, split the story so no single dispatch has to edit deep inside
a large file's interior; a story that only appends a new method at the end,
or only reads the file for context, is fine on a weaker tier.

## Some edits are hard independent of file count

Two edit *shapes* overwhelm a weak executor even when the file-count cap is
satisfied, and each deserves its own story (or its own step in a dependency
chain) rather than sharing a budget with an easier sub-task:

1. **An anchored multi-line insertion into a large existing function**
   (roughly 500+ lines), guarded by several conditions. This is distinct
   from the "re-indent an entire function" failure mode (which a
   rename-and-delegate refactor solves) — it's a precise
   insert-between-two-anchors edit, and it is comparably hard for a weak
   executor to get exactly right without corrupting the surrounding code.
2. **A test that needs a real subprocess/external-state fixture** (spinning
   up a real repo, committing to it, asserting several guard permutations)
   is qualitatively harder to author correctly than a clean input/output
   unit test, even at a similar line count.

**Not hypothetical:** a story with exactly two production files (satisfying
the file cap) combined an anchored insertion into a ~500-line function with
a 380-line subprocess-fixture test covering five guard permutations. The
worktree accumulated five separate stuck/checkpoint states before
converging. Meanwhile the *other* half of the same story — an isolated
helper function with a clean unit test — landed with no struggle at all.

**How to apply:** when authoring a local/weak-tier story, check for these
two shapes in addition to the file-count cap. If either is present, split:
the clean/isolated work as one story, the anchored-insertion-into-a-large-
function or fixture-heavy-test work as a separate dependent story with its
own rework budget. Don't let an easy sub-task and a hard sub-task share one
budget — the hard one will burn it and leave nothing for genuine rework of
its own mistakes.

## Deletion stories need an explicit survivor list

A story whose task is "remove X" needs an explicit "do NOT remove or
modify" list naming the adjacent code that must survive — the inverse of
pre-authorizing exact edits.

**Not hypothetical:** a story instructed to remove one specific UI control
did so, but also deleted three unrelated event listeners and reset an
unrelated default setting — all adjacent code that merely *looked* related
to the removed feature. A review cycle was needed just to restore what
should never have been touched.

**How to apply:** for any deletion story, after stating what to remove, add
a section naming the specific adjacent symbols/blocks that must survive.
"Never modify an existing test without approval" only protects test files —
it provides no guard against production-code over-deletion, so the survivor
list is the only thing that does.

## A "done" story is not proof its title's scope was fully delivered

A story marked done with a merged PR is not proof its full scope landed.
The review/done-bar grades the story's *own* tests, not whether the
title-level claim ("migrate all the X", "remove the Y") was actually
completed in full.

**Not hypothetical:** a story titled "migrate every loader onto the shared
helper" merged and was marked done, but migrated only 6 of 14 targets — the
other 8 silently kept using the old path. Because "done" satisfied the
dependency graph, a dependent story was dispatched on top of it and
regressed roughly 160 tests when it assumed the migration was complete.

**How to apply:**
- Before relying on a "done" prerequisite for a dependent story, verify its
  actual deliverable against its title's claim (e.g. diff the PR's changed
  files against the full list the title implies) — don't trust the status
  field alone.
- If a dependent story regresses against a "done" prerequisite, suspect the
  prerequisite's incomplete delivery first, and file a completion story
  against it rather than immediately blaming the dependent.
- At authoring time, a story titled "migrate/remove ALL the X" is high risk
  for partial delivery on a weak executor. Either enumerate the exact
  targets in the brief with a mechanically-checkable done-criterion (e.g. a
  grep that must return no matches), or split it one story per target so a
  partial attempt shows up as an unclaimed story instead of silent debt.
