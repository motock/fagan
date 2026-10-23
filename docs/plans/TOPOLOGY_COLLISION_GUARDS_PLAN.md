# Plan — topology collision guards (story-key reuse and the unchecked merge fetch)

**Status (verified 2026-10-23): DONE.** All four stories merged (PRs
#1205-#1208, CI green). Measured after merge against the live plan store
(401 plans): 0 would-be ingest rejections and 0 dispatchable stories refused,
as expected.

**Original status:** Proposed, saved as pipeline plan `topology-collision-guards`, not yet
ingested (2026-10-22). Claude-class dispatch: the live registry routes `dispatch`
to claude/sonnet and `PIPELINE_BACKEND_DISPATCH` is unset, so the local-dispatch
preflight is not mandatory. An impact run was still done (below).

**Source:** latent hazards 1-3 in
[`SHARED_SERVICE_TOPOLOGY_PLAN.md`](SHARED_SERVICE_TOPOLOGY_PLAN.md). This plan
fixes them for topologies T1 and T2 without the branch/worktree namespacing
migration, which stays deferred as a T3 prerequisite.

## 1. Diagnosis

### Hazards 1 and 2: a story key is a global identifier, but nothing enforces it

A story's branch is `agent/{story_key.lower()}` (six modules, e.g.
`pipeline/dispatch.py:129`), and its worktree is `WORKTREE_ROOT / story_key`
(`pipeline/dispatch.py:130`), where `WORKTREE_ROOT` defaults to the user-global
`~/.claude/worktrees`. Neither includes the plan or the repository. Auto-minted
keys are UUIDs and never collide. Keys written by a plan author can collide:

- **Same repo, two plans:** both stories push to one branch on `origin`.
- **Any repo, any two plans:** both stories want one worktree directory. Worse,
  `resuming` is true whenever that directory exists
  (`pipeline/dispatch.py:131-134`), so the second story silently resumes the
  first one's worktree, branch and commits.

**Measured, not hypothetical.** The live plan store has 916 author-written keys,
and 5 are already reused across plans: `dsg-1` (fagan and anagram, so two repos
share one worktree path), `gate-1`, `pipe-205`, `reg-1` and `srr-1`. Each pair
ran at different times, which is the only reason nothing collided.

### Hazard 3: the merge gate's fetch is unchecked

`_rebase_onto_master` (`pipeline/rebase.py:208`) runs `git fetch origin` and
discards the result. Every caller goes through it: the merge gate
(`pipeline/merge.py:75`), `approve_merge` (`pipeline/merge.py:569`) and the
dispatch resume rebase (`pipeline/dispatch.py:202`). A failed fetch lets the
rebase target a stale `origin/<default>`, so the gate force-pushes and polls CI
for a branch built on an old base.

Reproduced 2026-10-22: 6 concurrent `git fetch origin` runs into one repository,
15 rounds, gave 75 non-zero exits out of 90, each
`cannot lock ref 'refs/remotes/origin/master'`. Most losers are harmless (the
winner already moved the ref), so the concrete defect is that *any* fetch
failure (lock race, network, auth) is invisible, not that races always go
stale. The topology record's framing ("not under the git lock") is secondary:
git's own ref lock keeps the repository consistent, and the missing piece is
checking and retrying the fetch.

## 2. Approach

Guard rather than migrate. Namespacing branches by plan would break every
in-flight branch and the resume path. A uniqueness rule on author-written keys
removes the collision at its source with no migration:

> An explicit story key may not match, case-insensitively, a story in another
> plan that is not `done`/`merged` or whose worktree directory still exists.

A `done` story's worktree and branch are deleted at merge, so its key is safe to
reuse. That is why this rule blocks 0 of the 5 shared keys today.

Enforce it twice (defense in depth): at ingest, where it fails loudly before any
manifest is written, and at dispatch, which also covers plans ingested before
the rule existed.

For hazard 3, check the fetch's exit code, retry a bounded number of times, and
report a persistent failure as a non-conflict rebase error. Every caller already
handles that shape safely: the merge gate counts it as an ordinary
`merge_attempts` retry (never rework, `pipeline/advance_merge.py:367-369`),
`approve_merge` returns the error to the caller, and the dispatch resume path
fails open (`pipeline/dispatch.py:262`).

**Not in scope:** cross-machine collisions (two engineers on separate machines
sharing only `origin`, topology T1). Only the remote can see those. The
recommendation there stays "use UUID keys, or namespace later as part of T3".

## 3. Stories

| Key | Summary | Production files | Depends on |
|---|---|---|---|
| TOPO-1 | Fail the rebase helper on a failed `git fetch origin`, with bounded retry | `pipeline/rebase.py` | — |
| TOPO-2 | Add `cross_plan.key_conflicts` | `pipeline/cross_plan.py` | — |
| TOPO-3 | Reject an ingest whose explicit story key collides with another plan | `pipeline/ingest.py`, `.claude/rules/pipeline-story-schema.md` | TOPO-2 |
| TOPO-4 | Refuse to dispatch a story whose key collides with another plan | `pipeline/dispatch.py` | TOPO-2 |

The full briefs are in the saved plan (`~/.claude/plans/topology-collision-guards.json`).

## 4. Preflight

**Impact run, 2026-10-22, on `2a8c182`:** rough stand-ins of all four changes in
a scratch worktree gave 15,961 passed, 0 failed, and ruff was clean. No
pre-existing test conflicts, so no test edits are pre-authorized.

**Real-environment check of the withholding gates** (per
`.claude/rules/testing-config-gates.md`): against the live plan store, the key
rule blocks **0** stories (5 shared keys, all `done`, worktrees gone). TOPO-3 and
TOPO-4 must repeat this measurement after merge and record it in the PR. TOPO-1
withholds only when the fetch fails 3 times in a row; when the network is down
the push straight after it fails today anyway, so it adds no new blocking.

## 5. Follow-ups this plan does not do

- Branch/worktree namespacing (`agent/{plan}/{key}`): the T3 prerequisite, a
  migration with its own resume-path design.
- Per-repo `WORKTREE_ROOT`: would remove the cross-repo half of hazard 2 by
  construction, but changes every worktree path and the cleanup sweep.
- `_scoped_repo_root` swaps the `REPO_ROOT` module global
  (`pipeline/server.py:523`), which is not thread-safe if two threads advance
  different plans at once. Noticed while tracing hazard 3; not verified as a
  live race, so it needs its own diagnosis first.
