# ST-01-repo-issue — make the dispatch worktree resolve `main` so the new-commits check runs

Follow-up repo-issue filed by triage for story **ST-01**. Triage ruled that ST-01
failed for an environmental reason, not on its own scope: its implementation was
coherent, fully green and ruff clean but could not be *verified*. This story repairs
that repository condition through the normal TDD / review / CI path. **This file is
the authoritative record for the fixer.** The manifest entry for `ST-01-repo-issue`
persists only `key`, `summary`, `status`, `agent_instructions`, `persona`, `risk` and
`backend` — it drops `description`, `files` and `acceptance`. Refresh the manifest
record from this file before dispatch rather than treating a thinner manifest record
as a different story.

## Hold on the parent

`ST-01` stays parked for a human with the reason:

> verification could not be evaluated — base branch `main` is unresolved in the
> dispatch worktree, so the new-commits check was INCONCLUSIVE; implementation on
> `agent/st-01` reports 18162 passed / ruff clean but is unmerged.

Do **not** mark `ST-01` done. Do **not** merge `agent/st-01`. There is no `gh`
evidence of a MERGED PR, and new commits on `agent/st-01` (e159cfe) never
corroborate on their own. Triage must not reset `ST-01`'s `triage_attempts`.

## Preserved work (verified, not re-dispatched)

- branch `agent/st-01` at `e159cfe` — committed, pushed, unmerged
- the branch's own reported state: 18162 passed, `ruff` clean
- **Do not implement ST-01's own deliverable.** This story fixes the pipeline
  condition that blocked ST-01's verification, nothing else.

## Defect — the base branch `main` cannot be resolved in the dispatch worktree

Evidence recorded by triage (not invented state):

- `git rev-parse --verify main` → `fatal: Needed a single revision`, `exit=128`
- `git rev-parse --verify origin/main` → `fatal: Needed a single revision`, `exit=128`
- `pipeline.server._default_branch()` → `main`
- the verification step rendered, verbatim:

```
GIT STATE: HEAD 33bdd0ab6b5065e7502ac9784bc069c633e1e9a7
BASE BRANCH main UNRESOLVED in this worktree; new-commits check INCONCLUSIVE
CHANGED FILES vs main:
(unavailable)
```

Where it is produced: `pipeline/triage.py` (`_current_git_state_impl`) renders the
`INCONCLUSIVE` line when `pipeline.git_ops._worktree_new_commits` answers
`("unknown", "")`, which happens when
`pipeline.git_ops._resolve_base_branch` cannot resolve the base in the worktree's
own repo. The dispatcher resolves the default branch (`main`) while the worktree was
created from a repo whose default is `master`, so `main` exists only — if at all — as
`origin/main`, and nothing materialises a local `main` ref.

## Required behaviour for the fix

1. `pipeline.git_ops.resolve_base(worktree, base_branch)` returns the base **commit
   sha**. When local `<base>` is absent but `origin/<base>` = `a1b2c3d`, it returns
   `a1b2c3d` — never `HEAD` — and afterward `git rev-parse <base>` in that worktree
   also returns `a1b2c3d`.
2. Materialisation happens **once**. After three commits land on the agent branch
   (HEAD = `f0a1b2c`), a second call still returns `a1b2c3d` and
   `git rev-list --count a1b2c3d..HEAD` is `3`, not `0`. Implementations that run
   `git branch -f <base> HEAD` / `git checkout -B <base>` on every call silently move
   the base to the tip, empty the diff and re-fail the story as "no new commits".
3. When **neither** local `<base>` nor `origin/<base>` resolves, the result is the
   explicit unresolved signal `None` (the caller renders `INCONCLUSIVE`), never a
   `HEAD` fallback and never a cached/memoised fallback: after a later
   `git fetch origin <base>` makes `origin/<base>` = `a1b2c3d`, the next call returns
   `a1b2c3d` and the count is `3`.
4. Invalid base names at the boundary (empty, blank, rev expressions, leading `-`) are
   unresolved, not resolved-by-accident.
5. `_resolve_base_branch` keeps its existing contract (branch **name**, `""` for
   unknown) so every current caller and test is unaffected.

Acceptance criteria from the ruling:

- the dispatch worktree resolves `main` (a local ref exists after the resolver runs),
- the new-commits check goes from `INCONCLUSIVE` to evaluable — it reports
  `BRANCH HAS NEW COMMITS vs main: yes` with a non-zero count for a branch that has
  commits beyond base,
- `ST-01` stays parked and **unmarked-DONE**, with the parked reason quoting
  "verification could not be evaluated — base branch `main` is unresolved",
- no ST-01 deliverable code is added by this story.

## Files

- implementation (single pinned file): `pipeline/git_ops.py` — adds `resolve_base`,
  `_rev_sha`, `_BASE_NAME_RE`, and routes `_resolve_base_branch` through it
- tests: `tests/unit/test_git_ops_base_branch_resolves_main.py` (additive)
- this record: `stories/ST-01-repo-issue.md`

`stories/OT-03-repo-issue.md` already exists but records the **detached grade** defect
(`pipeline/detached_grade.py`, rc=1 with a green suite) and does not cover base-branch
resolution, so it is not extended here and no duplicate of this story is filed.
