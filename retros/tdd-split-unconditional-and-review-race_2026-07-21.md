# Harness retro — TDD-split made truly unconditional + review/merge race (2026-07-21)

A same-day follow-up to `tdd-split-always-on_2026-07-21.md`. That retro covered
Story 2 of `always-on-checklist-and-tdd-split` shipping incomplete. This one
covers three things discovered later the same day: (1) a real gap in that
story's own design — the per-story `tdd_split` opt-in field survived the
toggle removal, defeating the point of "always-on" — and its fix, validated
live; (2) a new bug found while merging the fix's own follow-up story, where
a stale duplicate review pass corrupted an already-merged story's status; and
(3) a manual-git-surgery-vs-scheduler race that actually corrupted a source
file on disk while reconciling (1) and (2)'s aftermath.

**Not a committed plan doc — a retro.** Read alongside
`MATURITY_AND_UNIQUENESS_PLANS.md` (A3: failure-mode rate), Mode 28
(`tdd-split-always-on_2026-07-21.md`), and `project_dispatch_failure_modes`.

---

## 1. Timeline

1. User asked for a recurring plan-retro process; a story
   (`a4a06203-1c43-4dcb-8b1d-3cdabbf6f363`, "mark_story_done detects plan
   completion") was filed and dispatched to add a `plan_completed` signal.
2. User noticed the dispatch never engaged the test-author (glm) phase and
   asked why — surfacing that the per-story `tdd_split: true` opt-in field
   (kept intentionally per the original `ALWAYS_ON_CHECKLIST_AND_TDD_SPLIT_PLAN.md`
   §3.1 design) was still gating the phase. The user overrode that design
   decision: TDD-split should be unconditional for local-family dispatch,
   mirroring how the guided-decomposition planner became unconditional in
   Story 1 — "that was the point of removing the toggle."
3. Plan paused, story interrupted (no commits lost — 8 read-only steps in).
   Fixed test-first: `pipeline/server.py`'s TDD-split gate changed from
   `story.get("tdd_split") and not resuming and not marker.exists()` to
   `dispatch_backend in _LOCAL_BACKEND_NAMES and not resuming and not
   marker.exists()` — same shape as the planner's existing gate. 5 unrelated
   tests that incidentally triggered the now-unconditional phase were
   neutralized with `_run_test_author_phase` mocks; 1 test asserting the
   now-superseded opt-in-required behavior was flipped. Full suite green,
   ruff clean. Committed directly to master (`091301b`, `f8ad55d`) per user's
   explicit choice of "direct to master, 2 commits" over branch+PR.
4. Stale worktree from the interrupted attempt removed, story reset to
   `todo`, redispatched fresh. Verified end-to-end: `.tdd_split_test_author_done`
   marker present, glm's test-author commit landed *before* the executor's
   own boot line in `agent.log` — the fix works live, for the first time,
   exactly as designed.
5. Dispatched story ran cleanly: glm wrote 4 correct tests
   (`c4c95fe`), gpt-oss:20b implemented the minimal `all_done` check
   (`e34f2f7`) — diff matched the filed spec exactly, no scope creep.
6. Executor then chased a **pre-existing, order-dependent flaky test**
   (`test_local_agent_read_heavy_loop_nudges_once_then_parks` — passes
   standalone, intermittently fails under full-suite load, same family as
   Mode-28's flaky-under-load list) for ~20 steps, gave up ("I can't
   complete this task"), but still correctly reported `DONE` because its
   actual code change was already committed and correct. Full independent
   verification: 7/7 targeted tests pass, 1252/1252 full suite passes.
7. Plan was unpaused (from step 3's resume), so the background scheduler's
   own `advance_all_plans` tick picked up the finished story autonomously:
   reviewed (`APPROVE`), opened PR #156, all CI green, then auto-merged via
   its own `approve_merge` path (commit `2f82285` on master) — all before
   any manual `review_story`/`approve_merge` call from this session
   completed. A manual `review_story` call was correctly rejected by the
   user mid-flight as redundant once this was discovered.
8. A manual `approve_merge` call (issued for safety, unaware the scheduler
   had already merged) returned
   `{"ok": false, "error": "Story is changes_requested, not parked/pr_open"}`.
   Root cause: **a second, redundant `review_story` tick ran after the
   merge and post-merge worktree cleanup already happened**, found the now
   nonexistent worktree/branch, and reported `REQUEST_CHANGES` ("worktree is
   broken, no reviewable change") — flipping the manifest's `status` for an
   already-merged, already-done story back to `changes_requested`.
9. Verified against GitHub directly (`gh pr view`, `git log origin/master`)
   that the merge was real and the fix was genuinely in master. Manually
   corrected the manifest via `mark_story_done`, which set `status: done`.
10. Restarted the long-lived pipeline MCP server subprocess (a direct stdio
    child of the Claude Code CLI, PPID = `claude`, not launchd-supervised)
    so it would stop running the pre-merge, in-memory copy of
    `pipeline/server.py` — the very code this story just shipped. Claude
    Code did **not** auto-respawn the connection immediately after the
    kill; the `mcp__pipeline__*` tools disappeared from the session for
    roughly a minute until it reconnected on its own.
11. Even after reconnecting, `mark_story_done` still returned bare
    `{"ok": true}` for a single-story, now-`done` plan. Root cause: the
    earlier "direct to master, 2 commits" push for the TDD-split fix
    (`f8ad55d`) had only ever landed on *local* master — it was never
    pushed to `origin`. The scheduler's automated merge for PR #156
    therefore rebased onto `origin/master` at `f605628` (unaware of
    `f8ad55d`) and produced `2f82285` as a **sibling** commit, not a
    descendant. Net effect: `origin/master` was missing the TDD-split fix,
    and local `master` was missing the plan-completion merge — a genuine
    two-way divergence on the trunk branch, not just a lag.
12. Reconciled with `git rebase origin/master` (after stashing in-progress
    retro-doc edits). Git reported both local commits' patches as "already
    upstream" and dropped them — the dispatched story's worktree had
    branched from local master *after* `f8ad55d` landed, so the PR's own
    rebase-merge had already carried that diff's content into `2f82285`
    even though `f8ad55d` itself was never an ancestor. Local HEAD now
    matched `origin/master` exactly (`2f82285`), confirmed clean by
    `git diff`.
13. Restored the stashed doc edits, then restarted the MCP server again to
    load the reconciled file — and hit a **second, more serious** problem:
    the on-disk `pipeline/server.py` had picked up a corrupted, duplicated
    `mark_story_done` body (undefined-name `NameError` on call) that did
    **not** match the clean, committed `2f82285` tree. Root cause: the
    background `advance-scheduler` launchd job (`WorkingDirectory` = this
    same repo, `StartInterval` = 60s) runs its own `git checkout`/
    fast-forward operations **directly against the shared main-repo
    working tree** on every tick (confirmed in `advance-scheduler.log` —
    repeated `HEAD is now at ...` / `Fast-forward` lines with no locking
    visible from the human-driven side). My manual `git rebase` almost
    certainly executed concurrently with one of these ticks, and the two
    processes' writes to the same working-tree file interleaved into
    corrupted, duplicated source. Fixed by `git checkout HEAD --
    pipeline/server.py` (discarding the corrupt uncommitted diff — the
    committed tree was always clean) and re-verified with an AST parse +
    clean import before restarting the server a second time.
14. Also found: the false `REQUEST_CHANGES` from step 8 had left the story
    dispatch-eligible, so the scheduler had already auto-redispatched a
    **new rework worker** (a second `local_agent.py`, separate PID) into a
    freshly recreated worktree at the same path — one that never got
    registered in `git worktree list` (a `git worktree add` skipped or
    raced by the same scheduler/manual-git contention as step 13). That
    worker was still running against an already-merged, already-`done`
    story when discovered; killed and the stray worktree directory removed
    (safe — manifest status was already `done`, i.e. not dispatch-eligible,
    before the removal, per the established worktree-repair ordering rule).
15. Final verification after the second server restart: `mark_story_done`
    correctly returned
    `{"ok": true, "plan_completed": true, "stories": [...]}` live.

---

## 2. Learnings

**L1 — "remove the toggle" and "always-on" are not the same fix; a
per-story opt-in field is just the toggle moved one level down.** Story 2 of
the earlier plan removed the *global* `PIPELINE_TDD_SPLIT` env var but left a
per-story field with the same effect, so the feature was still off by default
for every story that didn't explicitly ask for it. The planner's equivalent
story got this right the first time (gate keyed purely on
`dispatch_backend`); TDD-split's story didn't, and it took a user
double-check after the fact to catch. **When "always-on" is the explicit
goal, the gate should have zero opt-in surface — not global, not per-story.**

**L2 — an executor giving up mid-diagnosis on unrelated noise, but still
landing on a correct final state, is fragile, not something to rely on.**
gpt-oss:20b spent a third of its step budget grepping for an env-var leak
explaining a flaky test that had nothing to do with its story, explicitly
said it couldn't complete the task, and only reported `DONE` because its
actual (already-committed) fix happened to be correct and complete before it
went down that path. Had the flaky investigation happened *before* the real
implementation instead of after, this run would likely have step-capped with
nothing shipped. This is the same flaky-under-load test family flagged as a
P0 in the prior retro (Mode 28) — it is now confirmed to cost real executor
step budget on a second, unrelated story, not just cause misleading review
findings.

**L3 (new failure mode — "Mode 29") — `review_story`/the scheduler does not
check whether a story is already merged/done before dispatching another
review pass against it.** Sequence: auto-review → APPROVE → PR opened → CI
green → auto-merge → worktree/branch cleaned up (all correct) → **a second,
redundant review tick fires anyway**, finds the (correctly) nonexistent
worktree, and reports `REQUEST_CHANGES`, overwriting the manifest's `status`
for a story that is actually `done` and merged. Nothing about the merge
itself was at risk — GitHub's state was always correct — but the manifest
briefly lied, a subsequent `approve_merge` call failed on a stale
precondition, and diagnosing it required cross-checking `gh pr view` /
`git log origin/master` by hand to prove the manifest wrong. **Fix: before
dispatching any review pass, check `manifest["stories"][key]["status"] in
("done",)` (or equivalently, that the PR is already merged) and skip/no-op
instead of reviewing.**

**L4 — the pipeline MCP server is a long-lived subprocess that does not
pick up its own newly-merged code, and killing it does not auto-reconnect
immediately.** This story shipped a change to `pipeline/server.py` itself;
the running in-session MCP server (a direct stdio child of the `claude` CLI
process, not launchd-supervised — distinct from the `advance-scheduler` and
`usage-poller` launchd jobs, which *do* get a fresh process per tick and so
don't have this problem) kept executing the pre-merge code until manually
killed. After killing it, Claude Code did not respawn the connection
instantly — the `mcp__pipeline__*` tools disappeared from the session for
roughly a minute before reconnecting on their own. Any story that changes
`pipeline_mcp_server.py`/`pipeline/server.py` behavior needs an explicit
"restart the MCP server and wait for reconnect" step before its effect can
be observed live in the current session — this is not automatic or instant.

**L5 (new failure mode — "Mode 30") — the background scheduler runs raw git
operations directly against the shared main-repo working tree, unsafe to run
concurrently with any manual git command a session performs in that same
directory.** `advance-scheduler`'s launchd job has `WorkingDirectory` set to
the main repo and ticks every 60s; its log shows it repeatedly doing
`git checkout <sha>` / fast-forward pulls in place, with no visible locking
against external writers. A plain `git rebase origin/master` run by hand in
this session almost certainly executed concurrently with one of those ticks
and the two processes' writes interleaved into a **corrupted, duplicated
function body** in `pipeline/server.py` — a real on-disk source-code
corruption, not a manifest/state bug like Mode 29. It was caught only because
directly invoking the function threw a `NameError` on an undefined name;
had the duplication instead produced *syntactically valid but semantically
wrong* code, it could have silently shipped. Recovered with
`git checkout HEAD -- <file>` once diagnosed, but the diagnosis required
comparing the working tree against the committed tree by hand — nothing in
the harness flagged the mismatch on its own. **This is a two-way hazard**:
any interactive session doing manual git surgery (rebase, merge, checkout,
reset) directly in a pipeline-managed repo while its scheduler is active is
at risk, independent of anything a dispatched story does.

---

## 3. Concrete improvements, prioritized

- [ ] **P0 — guard `review_story` (and the scheduler's dispatch of it)
      against already-`done`/merged stories.** (Mode 29, L3.) Cheapest fix:
      one status check before doing any worktree/git inspection. This is a
      correctness bug in the pipeline's own state machine, not a review-
      quality issue — higher urgency than the polish-routing item below.
- [ ] **P0 (carried over from Mode 28) — stabilize the flaky-under-load
      read-heavy/repetition-guard tests.** Now confirmed to cost real
      executor step budget (this story) in addition to misleading reviewers
      (prior story) — two independent incidents on the same root cause
      raises this from "annoying" to "recurring tax."
- [ ] **P1 — when a story/feature changes `pipeline/server.py` or
      `pipeline_mcp_server.py` itself, make "restart + reconnect the MCP
      server" an explicit, checked step** (either in the Definition of Done
      for pipeline-self-modifying stories, or as an automated post-merge
      hook) rather than something discovered by manually testing the new
      behavior and getting the old result.
- [ ] **P2 — audit for other "always-on" stories with the same L1 shape**
      (global toggle removed, but a per-story/per-plan opt-in field quietly
      left as the real gate). The planner conversion got this right; confirm
      no other always-on conversion has the same latent gap TDD-split had.
- [ ] **P0 — stop the scheduler from mutating the main repo's shared working
      tree in place, or pause it before any manual git surgery there.**
      (Mode 30, L5.) Two options, not mutually exclusive: (a) have
      `advance-scheduler` do its own git bookkeeping in a dedicated bare
      clone/worktree instead of the main working directory a human might
      also be using; (b) document/enforce "pause the scheduler
      (`launchctl unload` or an explicit pause flag) before running any
      manual `git rebase`/`merge`/`reset`/`checkout` directly in this repo."
      This is the highest-severity item in this retro — it produced actual
      source-code corruption, not just a stale-state bug — but is scoped as
      P0-not-P0-highest because it requires touching the scheduler's own git
      invocation path, more invasive than Mode 29's single status check.

---

## 4. What went well (don't regress these)

- The user's ordered directive ("pause → interrupt → implement → resume")
  was followed exactly, and the redispatch was independently verified
  end-to-end (marker file, commit ordering in `agent.log`) rather than
  assumed to work from the code change alone.
- TDD was followed for the gate fix itself: tests written and confirmed
  failing (via `git stash`) before the implementation, matching CLAUDE.md's
  Step 3.
- The test-author/executor split worked exactly as designed on the very
  first real dispatch after the fix: glm wrote tests first and stopped
  ("implementation to follow in a separate dispatch"), gpt-oss implemented
  against them without touching the tests.
- The manifest's incorrect `changes_requested` status was not taken at face
  value — it was independently cross-checked against `gh pr view` and
  `git log origin/master` before any corrective action, which is what
  surfaced Mode 29 as a real bug instead of quietly re-running a merge that
  had already happened.
