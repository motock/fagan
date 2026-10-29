# DK-03-repo-issue — fix the worktree's base-branch resolution so new-commits checks stop being INCONCLUSIVE

Status: todo
Key: DK-03-repo-issue
Parent story: DK-03 (parked)
Persona: devops-release-engineer
Risk: high — `pipeline/git_ops.py` is on the verification path for every dispatched story
Branch convention: `agent/dk-03-repo-issue`
Files: `pipeline/git_ops.py` (resolver only), `tests/unit/test_git_ops_base_branch_resolves_main.py` (additive)

Follow-up repo-issue filed by triage for story **DK-03**. Triage ruled that DK-03
failed for an *environmental* reason, not on its own scope, and this file is the
authoritative record for the fixer. The plan manifest
(`~/.claude/plans/smoke-stack.manifest.json`, key `DK-03-repo-issue`) is refreshed
into `stories/DK-03-repo-issue.md`'s shape — the manifest persists only `key`,
`summary`, `status`, `agent_instructions`, `persona`, `risk` and `backend`, and drops
`description`, `files` and `acceptance`. Treat a thinner manifest record as a filing
defect to correct before dispatch, not as a different story.

## Scope — the defect this story fixes

In this checkout the base branch cannot be resolved, so the triage git-state block
renders the unresolved signal instead of a fact:

```
BASE BRANCH main UNRESOLVED in this worktree; new-commits check INCONCLUSIVE
```

Every new-commits / ancestry check is therefore **INCONCLUSIVE** in this checkout: a
green, committed, pushed branch corroborates nothing, so `mark_done` fails closed and
the story parks. The defect is in base-branch resolution — not in DK-03's scope, and
not in triage's rendering (which correctly reports an unresolvable base rather than
guessing `HEAD`).

Root cause observed here: the repository's default branch is `master`
(`git symbolic-ref refs/remotes/origin/HEAD` -> `refs/remotes/origin/master`) while
the dispatcher asks for `main`. There is no local `main` **and** no `origin/main` to
materialise a local ref from, so `pipeline.git_ops.resolve_base()` has nothing to
return and must answer `None`. Where `origin/<base>` *does* exist, `resolve_base`
must create `refs/heads/<base>` exactly once at that sha and never `HEAD`.

## Evidence — reproduced in the DK-03-repo-issue worktree

Verbatim output of the reproduction (base branch `main`, repository default
`master`):

```
$ git rev-parse --verify main
fatal: Needed a single revision

$ git rev-parse --verify origin/main
fatal: Needed a single revision

$ git symbolic-ref refs/remotes/origin/HEAD
refs/remotes/origin/master

$ python -c 'from pathlib import Path; from pipeline import git_ops; print(git_ops.resolve_base(Path("."), "main"))'
None

# pipeline/triage.py renders, for that None:
BASE BRANCH main UNRESOLVED in this worktree; new-commits check INCONCLUSIVE
```

Supporting state at filing time:

- DK-03's branch `agent/dk-03` is at `614942d`, pushed, **unmerged** (PR #1380 open).
- The branch's own grade is green: 18542 passed / 43 skipped / 0 failed, `ruff` clean.
- `git grep -l 'BASE BRANCH'` locates the rendering in `pipeline/triage.py` and the
  resolver in `pipeline/git_ops.py`; `tests/unit/test_git_ops_base_branch_resolves_main.py`
  pins the ST-01 contract for the `origin/<base>` case.

## Hold on the parent — DK-03 stays parked

`DK-03` is parked for a human with the reason, character for character:

> green branch awaiting human review/merge; triage evidence blocked by unresolved
> base branch (see `DK-03-repo-issue`)

Do **not** re-dispatch `DK-03`, do **not** mark it done, do **not** merge
`agent/dk-03`, and do **not** reset its `triage_attempts`. A human reviews and merges
PR #1380 on its merits; there is no `gh` evidence that it is MERGED, and new commits
on `agent/dk-03` never corroborate on their own under the squash-merge rule.

## This story must NOT implement DK-03's deliverable

Do not add or change `deploy/compose.yaml`, `deploy/keycloak/realm.json`,
`deploy/smoke/smoke.sh`, `deploy/smoke/plan.json` or `deploy/README.md` — that is
DK-03's scope, it is already written, green and awaiting review on `agent/dk-03`, and
duplicating it here would fork the same work across two branches. This story changes
**base-branch resolution only**.

## Required behaviour for the fix

1. `pipeline.git_ops.resolve_base(worktree, base_branch)` returns the base **commit
   sha**. When local `<base>` is absent but `origin/<base>` = `a1b2c3d`, it returns
   `a1b2c3d` — never `HEAD` — and afterwards `git rev-parse <base>` in that worktree
   also returns `a1b2c3d`.
2. Materialisation happens **once**. After three commits land on the agent branch,
   a second call still returns `a1b2c3d` and `git rev-list --count a1b2c3d..HEAD` is
   `3`, not `0`. `git branch -f <base> HEAD` / `git checkout -B <base>` on every call
   silently moves the base to the tip, empties the diff and re-fails the story as
   "no new commits".
3. When **neither** local `<base>` nor `origin/<base>` resolves — the condition in
   this checkout — the result is the explicit unresolved signal `None`, rendered as
   `BASE BRANCH main UNRESOLVED ... new-commits check INCONCLUSIVE`. Never a `HEAD`
   fallback, never a memoised answer: a later `git fetch origin <base>` must make the
   very next call resolvable.
4. Invalid base names at the boundary (empty, blank, rev expressions, leading `-`)
   are unresolved, not resolved-by-accident.
5. `_resolve_base_branch` keeps its existing contract (branch **name**, `""` for
   unknown) so every current caller and test is unaffected.

For a repo whose default branch is genuinely `master`, the durable fix is to ask the
repository which base it has (origin's default branch) instead of assuming `main`;
whatever is chosen must make the new-commits check **evaluable** in a checkout like
this one and must not weaken case 3.

## Acceptance criteria

- In a checkout shaped like this one, the git-state block reports a resolved base and
  a real new-commits count rather than `INCONCLUSIVE` — or, where no base exists at
  all, the INCONCLUSIVE output stays as the honest signal and the human-facing reason
  names the missing ref.
- `resolve_base` is idempotent (cases 1–2 above) and returns `None` for unresolvable
  and invalid bases (cases 3–4).
- `tests/unit/test_git_ops_base_branch_resolves_main.py` and
  `tests/unit/test_triage_new_commits_evidence.py` keep passing unchanged; new tests
  are additive, in new files or appended cases.
- `DK-03` remains `parked` with its reason, `pr_url` (PR #1380), branch `agent/dk-03`
  and head `614942d` intact — a human must still be able to find the PR.
- No DK-03 deliverable file is touched (see the section above).

## Operational notes for the fixer

- Single local setup: `python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.
- Single test run: `.venv/bin/python -m pytest -q` (mirrors CI; `addopts` already
  carries `--ignore=tests/benchmark --ignore=tests/experiments -n auto`).
- Lint: `.venv/bin/ruff check .`.
- This story needs no credentials. Merging PR #1380 does — a human with write access
  on `motock/fagan` must review and merge it; no agent step can substitute for that.
