# Harness retro — mcp-self-mod-notice (2026-07-31)

Backfilled 2026-08-12 as part of clearing the retro-process backlog (see
`docs/plans/PLAN_RETROSPECTIVE_PROCESS_PLAN.md` §2.1). Written from git
history (commit messages, PR numbers) plus a detailed in-session memory
note captured during the plan's own execution.

Scope: implement the open A3 item from `MATURITY_AND_UNIQUENESS_PLANS.md`
— when a merge lands changes to the MCP server's own live source
(`pipeline/server.py` / `app/pipeline_mcp_server.py`), notify the operator
that `/mcp reconnect` is needed before further dispatch/review, since the
long-lived server process doesn't hot-reload. 4 stories, serial
dependencies, all dispatched local ~20B-class (`gpt-oss`, confirmed
`PIPELINE_BACKEND_DISPATCH=local`), all merged the same day: PR #214
(`084ac9d`), #215 (`a1d759e`), #216 (`89835a2`), #217 (`1246d8d`).

This plan is the direct origin of the later `edit-guard-enforcement` plan
(retro pending separately) — its finding that local dispatch damages
*adjacent* content while writing *correct new* content is what motivated
building hard-block edit guards instead of the advisory-only echo that
existed before.

---

## 1. Timeline

1. Story 1 (`644d4475`, add `pipeline/self_modification.py` with
   `_mcp_self_source_touched`/`_mcp_restart_notice`) failed on its first
   `dispatch_story` call immediately — a pre-existing, unrelated harness
   break (Mode 48: `app/backend.py`'s local-dispatch paths broken by an
   earlier `app/` reorg, PR #211) blocked dispatch entirely, not this
   story's own logic. Fixed via a separate urgent plan
   (`fix-backend-venv-path-reorg`, PR #213) before this plan could proceed.
2. After `/mcp reconnect`, redispatch succeeded: gpt-oss wrote a genuinely
   correct `self_modification.py` (43 tests; the reviewer called it
   "genuinely thorough"). But the worktree had been resumed from before PR
   #213 landed, so the branch's diff spuriously reverted #213's fix and
   carried a repo-wide lint issue in the new files — the local model
   couldn't itself execute the git-rebase/`ruff --fix` remediation needed
   and parked twice (Mode 49). Direct-repaired: rebase + `ruff --fix`,
   minus an unsafe `__all__` reorder that would have broken an existing
   test (used an inline `# noqa: RUF022` instead, per the never-modify-
   existing-tests-without-approval rule). Fresh review APPROVE, merged
   (#214, `084ac9d`).
3. Story 2 (`2e86b60b`, wire the notify call into `approve_merge`) hit a
   genuine Ollama 5xx during escalating-trim on its first attempt — the
   trim-retry itself also 500'd and the process died. Resumed cleanly on
   redispatch (Ollama itself confirmed healthy via a direct probe).
4. The resumed run's `replace_lines` edit **deleted** the pre-existing
   `_mark_plane_done(story_key, plan_name)` call while adding the notify
   call alongside it, and separately corrupted an unrelated fallback
   default (`story.get("worktree", "+")`, should have stayed `""`). The
   story's own acceptance-oracle-only fixture did **not** catch either
   defect; the full test suite did (a pre-existing test and the story's own
   new wiring test both failed). Direct-repaired: a 2-line fix restoring
   the dropped call plus the corrupted default, full suite 2054/2054,
   fresh review APPROVE, merged (#215, `a1d759e`).
5. Because story 2 itself touched `pipeline/server.py`, the MCP server
   serving *this session* needed another `/mcp reconnect` before dispatching
   story 3 — same recurring gotcha, now confirmed live for the second time
   this plan.
6. Story 3 (`3f2390e9`, wire the notify call into `advance_pipeline`'s merge
   gate) landed clean on the first attempt. The detection call correctly
   runs before `_merge_pr` (which deletes the worktree/branch after
   squash-merge — the diff's own comment notes this explicitly), and unlike
   story 2 the edit was purely additive: `git diff a1d759e..HEAD --
   pipeline/server.py` showed only `+8` lines, no deletions of the
   pre-existing `_merge_pr`/`_mark_plane_done`/attempt-counter-clear calls.
   Independently verified (ruff clean, 2069/2069 full suite) before
   merging — the extra verification step had become standard practice for
   this plan by this point, not incidental. Merged (#216, `89835a2`).
7. Story 4 (`673065fd`, tick the A3 bullet in
   `MATURITY_AND_UNIQUENESS_PLANS.md`) — a "trivial" docs-only story — still
   corrupted adjacent content it wasn't asked to touch: a `replace_lines`
   edit moved a closing paren to the wrong line (breaking an existing
   parenthetical) **and** duplicated the entire "Shipped:" paragraph
   verbatim. The story's own acceptance fixture's docstring claimed to
   assert the pre-existing prose stays byte-identical, and the agent's own
   local `pytest` run (step 26, after both corrupting edits) still reported
   green before it committed as "done" — whether the fixture's assertion
   didn't actually catch what its docstring described, or the agent
   misread its own run output, was not resolved. Caught only by
   independent diff-read before pushing to review (same discipline applied
   to stories 2/3). Direct-repaired: 2-line-net fix, full suite 2094/2094,
   ruff clean, fresh review APPROVE, merged (#217, `1246d8d`).

---

## 2. Learnings

**L1 — 3 of 4 stories needed a human/session direct-repair after the local
model's own "done" claim, and the failure mode was consistent: correct
*new* content, damaged *adjacent existing* content.** Story 1 dropped an
unrelated fix during a stale-worktree rebase; story 2 deleted a pre-existing
function call while adding an unrelated one; story 4 corrupted prose next
to the sentence it was actually asked to edit — including on a "trivial"
docs story, which had no reason a priori to be riskier than the code
stories. This is not one bug; it's a class, and it held across three
different story shapes (new module, code wiring, doc edit). This finding
directly motivated the `edit-guard-enforcement` plan's later hard-block
guards on `replace_lines`/`create_file` — before that plan, only an
advisory echo existed, and it fired on both these failures without
blocking either.

**L2 — an acceptance-oracle-only fixture (green) is not sufficient signal;
the full test suite caught defects the story's own oracle missed, twice.**
Story 2's deleted `_mark_plane_done` call broke a *pre-existing* test, not
the new story's own test — a fixture scoped to just the new behavior has
no way to see that. Story 4's fixture literally claimed (per its own
docstring) to guard against exactly the corruption that occurred, and
still reported green. Oracle-only grading structurally cannot catch damage
to code/content the oracle wasn't written to watch.

**L3 — "stale worktree resumed from before an unrelated urgent fix landed"
recreated a fixed bug inside a brand-new story.** Story 1's redispatch
carried a real revert of PR #213 purely because the worktree had branched
before that fix merged — nothing about story 1's own logic was wrong. This
is a resume-hygiene gap distinct from the edit-damage class in L1: a
worktree that predates a landed fix needs rebasing onto the fix before
(or as part of) resuming, not just before merging.

**L4 — a story that itself changes `pipeline/server.py` requires an
`/mcp reconnect` before the *next* story in the same plan can be
dispatched, and this repeated within a single 4-story plan** (after story
1, and again after story 2). Already documented in
`project_stale_mcp_after_merge`, but this plan is a second live
confirmation that it recurs *within* one plan's own dependency chain, not
just across unrelated plans.

---

## 3. Concrete harness improvements, prioritized

- [x] **P0 — hard-block edit-damage instead of advisory-echo-only.** Fixed
      by the `edit-guard-enforcement` plan (PRs #218–#224, landed by
      2026-08-03): `confirm_removals` gate on true deletions,
      `duplicated_block_warning` for insertion-class damage,
      `verify_range_anchors` for stale-range detection. Directly traces to
      L1 of this retro.
- [ ] **P1 — an acceptance-oracle-only "green" is not a merge signal on its
      own; require the full suite to run and be independently read, not
      just the story's own fixture, before any story is pushed to
      review.** This was already informally standard practice by story 3
      of this plan (L2) — worth confirming it's enforced structurally
      (e.g. in `review_story`'s own gate) rather than remaining a
      discipline an operator has to remember to apply every time.
- [ ] **P2 — when a story is resumed/redispatched after being stale for any
      reason, check whether `master`/`origin` has moved since the
      worktree branched and flag it before the agent starts working**, not
      only at merge time. Would have caught L3 before the agent spent
      effort producing a diff that had to be rebased anyway.
- [ ] **P3 — self-modifying plans (any plan whose stories touch
      `MCP_SELF_SOURCE_FILES`) could batch their `/mcp reconnect`
      requirement to once per plan instead of once per touching story**, if
      dependency ordering allows deferring the reconnect until right before
      the next dispatch that actually needs the new code. Low urgency —
      the current per-story reconnect is correct, just occasionally
      redundant.

---

## 4. What worked

- Independent diff-read + full-suite verification before every merge
  (not just trusting a green oracle) was applied consistently from story 2
  onward and is what caught L1/L2's damage every time — none of the three
  corrupted merges reached master undetected.
- Direct repairs were kept minimal and targeted (2-line fixes, a rebase +
  lint pass) rather than turning into broader rewrites, even under time
  pressure from a blocked plan.
- Story 3 (the one clean run) is proof the underlying wiring pattern itself
  was sound — the plan's design wasn't the problem; local-dispatch edit
  reliability was.
- The plan correctly treated Mode 48 (dispatch entirely blocked) as an
  external blocker requiring its own urgent side-plan rather than working
  around it in place.

## 5. Status

Modes referenced: Mode 48 (pre-existing, blocked dispatch entirely — fixed
by `fix-backend-venv-path-reorg` before this plan proceeded), Mode 49
(local model can't execute git-rebase/lint remediation itself — recurred
here, still an open pattern to design around rather than a one-off).
L1's edit-damage class is **fixed** downstream (`edit-guard-enforcement`,
P0 above). L2–L4 are not yet reflected as their own line items in
`MATURITY_AND_UNIQUENESS_PLANS.md` A3 — follow-up.
