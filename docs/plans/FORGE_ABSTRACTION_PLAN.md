# Plan: Abstract the forge layer (GitHub default, GitLab and Bitbucket pluggable)

> **Status: DRAFT — not started (2026-10-22).** This document is persisted for
> later execution and has not been decomposed or ingested into the pipeline.
> Nothing described here is implemented. When work begins, re-verify the file
> and line references below against the tree at that time — the assessment is
> accurate as of 2026-10-22 but the surrounding modules move.

> **Status (verified 2026-10-25):** still not started. Every referenced module
> exists and the proposed `pipeline/forge*.py` modules do not, as expected. The
> `ci.py` refs (`:65`, `:100`, `:144`, `:280`, `:396`) and
> `triage_story_actions.py:130` still land on their symbols; several others have
> drifted — prefer the symbol names over the numbers: `_sync_pr_title`
> `pr.py:161`, `_open_pr` `:245` (called from
> `review_orchestrator.py:733`), `_merge_pr` `:290`, `_post_pr_comment` `:349`,
> `_AGENT_SECRET_ENV` `execution.py:214`, `_resolve_base_branch`
> `git_ops.py:194`, `_default_branch` `server.py:453`.
> One claim has changed in substance: "both merge paths refuse a story with no
> `pr_url`" now describes a single path — `advance_merge.py:147`;
> `pipeline/merge.py` no longer references `pr_url`. Re-derive the
> "PR is load-bearing" section before decomposing.

## Goal

Make "which forge hosts this repository" a resolved, swappable dependency
instead of an implicit GitHub assumption, so a project hosted on GitLab or
Bitbucket can run the full review → PR → CI → squash-merge loop.

Hard requirement: `auto` reproduces today's GitHub behavior exactly. `gh`
stays the default, and an existing GitHub install needs no new configuration.

## Non-goals

- **A no-forge / local-merge mode.** Today a story reaches `done` only through
  a PR (`advance_merge` requires `pr_url`; `_pr_is_merged` corroborates a
  `MERGED` PR). A forge-less mode would need a local squash-merge path plus a
  decision about what replaces PR corroboration. That is a separate design.
- **Rewriting dispatch, review, or the local test/build gates.** Those are
  already host-neutral and must not change.
- **Forge features beyond the operations listed below** (issues, wikis,
  pipeline config generation, branch protection management, etc.).
- **Ticketing.** Plane/Jira selection is an independent abstraction
  (`pipeline/ticketing.py`, `PIPELINE_TICKET_PROVIDER`).

## Current state (verified 2026-10-22)

### Already host-neutral

Everything below is plain `git` against a remote named `origin`, or entirely
local, and works unchanged on any host:

- worktrees, branch creation, `fetch origin`, rebase onto `origin/HEAD`,
  `push --force-with-lease`, `branch -D`, `push origin --delete`
  (`pipeline/dispatch.py`, `pipeline/dispatch_worktree.py`,
  `pipeline/rebase.py`, `pipeline/merge.py:94,572`)
- default-branch detection from `git symbolic-ref refs/remotes/origin/HEAD`,
  so `main` vs `master` already works (`pipeline/server.py:453`,
  `pipeline/git_ops.py:204`)
- dispatch, agent execution, review personas, overlord, scheduler
- local test/lint/build detection and re-verification
  (`pipeline/ecosystem_detect.py`, `pipeline/build_detect.py`,
  `pipeline/ci.py:430+`): the merge gate's full-suite, acceptance, and build
  re-runs are all local subprocesses
- branch naming, PR-title derivation from commits, story-branch alias
  resolution (`pipeline/pr.py:88`, `:138`, `:220` — all `git`-only)
- the ticketing layer

The only off-host operations in the pipeline are **PR lifecycle** and **CI
status**. Everything else runs locally.

### The coupling, precisely

| Operation | Location | GitHub mechanism |
|---|---|---|
| open PR (idempotent; recovers an "already exists" PR) | `pipeline/pr.py:241` (`_open_pr`) | `gh pr create`, `gh pr view --json url` |
| refresh PR title | `pipeline/pr.py:157` (`_sync_pr_title`) | `gh pr edit --title` |
| read PR title (for the squash subject) | `pipeline/pr.py:286` (`_merge_pr`), `:220` | `gh pr view --json title` |
| comment on PR | `pipeline/pr.py:345` (`_post_pr_comment`) | `gh pr comment --body` |
| squash-merge PR | `pipeline/pr.py:286` (`_merge_pr`) | `gh pr merge --squash --subject` |
| merged-PR corroboration | `pipeline/triage_story_actions.py:130` (`_pr_is_merged`) | `gh pr view <url> --json state` == `MERGED` |
| CI status (single poll) | `pipeline/ci.py:280` (`_ci_status_once`) | `gh pr checks --json name,bucket`; `gh api .../commits/{sha}/check-runs` |
| CI status (blocking poll) | `pipeline/ci.py:144` (`_ci_status`) | same two queries, looped until terminal/timeout |
| failing-log excerpt | `pipeline/ci.py:100` (`_fetch_ci_failure_excerpt`) | `gh run list`, `gh run view --log-failed` |
| CI re-run after cancellation | `pipeline/ci.py:396` (`_ci_rerun`) | `gh api .../actions/runs?head_sha=`, `gh run rerun --failed` |
| "is CI configured?" | `pipeline/ci.py:65` (`_repo_has_ci_configured`) | hardcoded `.github/workflows/` |
| installer prerequisite | `scripts/install_checks.py:58-62,103` | `gh` required, `gh auth status` probed |
| agent secret scrubbing | `pipeline/execution.py:211` (`_AGENT_SECRET_ENV`) | strips `GH_TOKEN`/`GITHUB_TOKEN` |

Orchestration around these is already forge-agnostic:
`pipeline/merge.py`, `pipeline/advance_merge.py`,
`pipeline/review_orchestrator.py`, and `pipeline/server.py` call the functions
above through re-exported names (`_open_pr`, `_merge_pr`, `_ci_status_once`,
`_ci_rerun`, …). Keeping those names and signatures is what limits the blast
radius of this work.

### The PR is load-bearing

`review_story` promotes an APPROVE'd story to `pr_open` by calling `_open_pr`
(`pipeline/review_orchestrator.py:732`). Both merge paths refuse a story with
no `pr_url` (`pipeline/advance_merge.py:120`, `pipeline/merge.py:484`). So on a
host with no adapter, the pipeline still dispatches, reviews, and runs local
tests — it simply cannot land anything.

### Test surface

19 test files contain `"gh"`; 13 assert a literal `["gh", …]` argv slice:

- `tests/unit/test_ci_fail_error_detail.py`
- `tests/unit/test_ci_fail_log_excerpt.py`
- `tests/unit/test_ci_sha_scoping.py`
- `tests/unit/test_ci_status_gh_cwd.py`
- `tests/unit/test_install_checks_authorization.py`
- `tests/unit/test_pipeline_mcp_server_mark_done_and_patch.py`
- `tests/unit/test_pipeline_mcp_server_repo_root_and_usage.py`
- `tests/unit/test_pipeline_mcp_server_ticketing.py`
- `tests/unit/test_pr_comment.py`
- `tests/unit/test_pr_title_diff_check.py`
- `tests/unit/test_pr_title_sync.py`
- `tests/unit/test_pr_title.py`
- `tests/unit/test_security_verdict_pr_comment.py`

Most of the suite mocks `_open_pr` / `_merge_pr` / `_ci_status_once` at the
function boundary and is unaffected. The 13 files above must be retargeted:
their subject is still valid (the GitHub adapter runs the same argv), but the
patch target moves from `pipeline.pr.subprocess.run` / `pipeline.ci.subprocess.run`
to the GitHub adapter module.

## Design

### Selection

One new env var, following the `PIPELINE_TICKET_PROVIDER` precedent:

```
PIPELINE_FORGE = auto | github | gitlab | bitbucket      (default: auto)
```

- `auto` resolves per repo from `git -C <repo_root> remote get-url origin`:
  - hostname contains `github` → `github`
  - hostname contains `gitlab` → `gitlab`
  - hostname contains `bitbucket` → `bitbucket`
  - otherwise → `github` with a one-line WARNING naming the host (preserves
    today's behavior; self-hosted GitHub Enterprise is the common case)
- An explicit value forces one adapter for every repo in the process.
- The provider is cached **per repo root**, mirroring `_default_branch`
  (`pipeline/server.py:453`), because one process serves multiple plans and
  repos via `_scoped_repo_root`.
- Unknown values raise a clear `ValueError` at resolution time, as
  `get_ticket_provider()` does.

Do **not** add a `none` value in this plan — see Non-goals.

### Module layout

Follow the `pipeline/ticketing.py` precedent (one module, Protocol +
implementations + factory), but split by adapter to respect the 1,000-line
file guideline:

```
pipeline/forge.py            Protocol, canonical states, detection, factory, cache
pipeline/forge_github.py     current gh-backed behavior, moved verbatim
pipeline/forge_gitlab.py     glab-backed
pipeline/forge_bitbucket.py  REST v2 via httpx (already a runtime dependency)
```

`pipeline/pr.py` and `pipeline/ci.py` keep their public functions as thin
delegating shims. `_pr_title`, `_branch_commit_subject`,
`_resolve_story_branch`, and the CI polling loop stay where they are —
they are host-neutral.

### Interface

```python
class ForgeProvider(Protocol):
    name: str

    # PR lifecycle (open_pr is idempotent: an existing PR returns its URL)
    def open_pr(self, worktree: str, branch: str, title: str, body: str) -> str: ...
    def update_pr_title(self, worktree: str, branch: str, title: str) -> None: ...
    def comment_pr(self, worktree: str, body: str) -> None: ...
    def merge_pr(self, worktree: str, branch: str, subject: str) -> str: ...
    def pr_title(self, worktree: str, branch: str) -> str: ...
    def pr_state(self, pr_url: str) -> str: ...          # "MERGED" | "OPEN" | "CLOSED" | "UNKNOWN"

    # CI
    def checks_status(self, branch: str, sha: str) -> dict[str, str]: ...
    def failure_excerpt(self, branch: str) -> str | None: ...
    def rerun_failed(self, sha: str) -> bool: ...
    def has_ci_config(self, repo_root: str) -> bool: ...
```

`checks_status` performs exactly one snapshot; the sleep/poll loop and the
`PIPELINE_MERGE_CI_GATE` short-circuit stay above the adapter in `ci.py`, so
they remain host-neutral and identically testable.

### Canonical CI state contract

Every adapter returns exactly these states with these meanings. This is the
contract the merge gate already consumes (`pipeline/merge.py:596-627`,
`pipeline/advance_merge.py:278-306`) and must not drift:

| State | GitHub | GitLab | Bitbucket |
|---|---|---|---|
| `pass` | all check-runs `success`/`neutral`/`skipped` | pipeline `success`, or all statuses `success`/`skipped`/`manual` | all statuses `SUCCESSFUL` |
| `fail` | any `failure`/`timed_out`/`action_required` | pipeline `failed`, or any status `failed` | any `FAILED` |
| `pending` | any run not `completed`, or checks configured but none registered yet | `created`/`pending`/`running` | `INPROGRESS`/`PENDING`, or none registered yet |
| `cancelled` | any `cancelled` | `canceled` | `STOPPED` |
| `unreadable` | CLI/API error **and** CI is configured | same | same |
| `none` | no `.github/workflows/` | no `.gitlab-ci.yml` | no `bitbucket-pipelines.yml` |

Two invariants carried over unchanged:

1. **`unreadable` must never collapse into `none`.** `none` is permission to
   merge without checks; `unreadable` refuses. Each adapter implements
   `has_ci_config` so the distinction is preserved on every host.
2. `PIPELINE_MERGE_CI_GATE=0` still returns `pass` before the adapter is
   consulted.

### Auth and secrets

| Forge | CLI / API | Credentials |
|---|---|---|
| GitHub | `gh` | `GH_TOKEN` / `GITHUB_TOKEN`, or `gh auth login` |
| GitLab | `glab` (self-hosted via `GITLAB_HOST`) | `GITLAB_TOKEN` / `GLAB_TOKEN`, or `glab auth login` |
| Bitbucket | REST v2 via `httpx` | `BITBUCKET_USERNAME` + `BITBUCKET_APP_PASSWORD` (Basic), or `BITBUCKET_TOKEN` (Bearer) |

`_AGENT_SECRET_ENV` (`pipeline/execution.py:211`) must grow to strip every new
token from agent subprocesses: `GITLAB_TOKEN`, `GLAB_TOKEN`,
`BITBUCKET_USERNAME`, `BITBUCKET_APP_PASSWORD`, `BITBUCKET_TOKEN`,
`CI_JOB_TOKEN`.

`scripts/install_checks.py` must stop requiring `gh` unconditionally: report
the resolved forge's CLI as required (or, for Bitbucket, probe the API
credential pair), and keep `gh` required only when the resolved forge is
GitHub.

## Stories

Ordered so each lands green and independently reviewable. Each story keeps its
code change and the documentation for that change together (the reviewer sees
only the diff). Tier guidance is included because these stories are mostly
refactors across large files and should **not** run on the on-device tier.

### S1 — Protocol, detection, factory, and the GitHub adapter (pure refactor)

- **Files:** new `pipeline/forge.py`, new `pipeline/forge_github.py`,
  `pipeline/pr.py` (shims only).
- **Scope:** define the Protocol and canonical states; implement
  `get_forge_provider(repo_root)` with per-repo caching and the `auto`
  detection rules; move every `gh` argv in `pipeline/pr.py` verbatim into
  `GitHubForge`. `_open_pr`, `_merge_pr`, `_post_pr_comment`, and
  `_sync_pr_title` become one-line delegations; `_pr_title`,
  `_branch_commit_subject`, and `_resolve_story_branch` stay put.
- **Behavior:** byte-identical argv and return values. `PIPELINE_FORGE` unset
  ⇒ same behavior as before, including the "already exists" recovery path.
- **Tests (new):** factory selection for explicit values; unknown value
  raises; `auto` maps a fake `git remote get-url` host to each adapter and
  falls back to GitHub for an unrecognized host; per-repo cache isolation;
  GitHub adapter unit tests for every PR method, including — `open_pr`
  returns the URL on the create path, recovers the URL and refreshes the
  title on "already exists", propagates a non-"already exists" failure, and
  makes no `gh pr edit` call on the create path; `update_pr_title` swallows
  CLI failure; `comment_pr` propagates CLI failure; `merge_pr` passes the
  subject and does not delete the branch.
- **Tests (modified — pre-authorize these as an explicit list in
  `agent_instructions`):** retarget the 13 files above from
  `pipeline.pr.subprocess.run` to the adapter's runner seam. No assertion's
  substance changes. This is the one story where test files are the point of
  the diff, so the reviewer must be told.
- **Tier:** Claude-class or cloud open-source. Many new functions; not a
  weak-tier story.

### S2 — Extract the CI adapter (pure refactor)

- **Files:** `pipeline/forge.py`, `pipeline/forge_github.py`,
  `pipeline/ci.py`.
- **Scope:** move `_repo_has_ci_configured`, `_fetch_ci_failure_excerpt`, and
  `_ci_rerun` into `GitHubForge`; split `_ci_status` / `_ci_status_once` into
  (a) a host-neutral poll loop and state machine in `ci.py` and (b) a
  GitHub-specific `checks_status(branch, sha)` snapshot classifier. Keep the
  six canonical states and the `_unreadable_or_none` fail-closed behavior.
- **Behavior:** identical states and errors; `ci.py`'s local re-verification
  functions (`_reverify_acceptance`, `_reverify_build`, tamper checks) are
  untouched.
- **Tests:** all six states through the GitHub fake, including the GitHub
  `bucket` vocabulary and the `check-runs` vocabulary; `unreadable` vs `none`
  with and without `.github/workflows/`; empty-checks-before-registration
  stays `pending`; timeout yields `pending`; `PIPELINE_MERGE_CI_GATE=0`
  short-circuits to `pass`; cancelled triggers exactly one rerun.
  Retarget the CI test files listed above.
- **Tier:** Claude-class or cloud open-source — this is an anchored
  extraction from a 951-line file and the most delicate story in the plan.

### S3 — Wire forged checks into triage, installer, and docs

- **Files:** `pipeline/triage_story_actions.py`, `scripts/install_checks.py`,
  `README.md`, `REFERENCE.md` (and `pipeline/execution.py` for the secret
  list).
- **Scope:** `_pr_is_merged` becomes `provider.pr_state(url) == "MERGED"`;
  the installer resolves the active forge and checks the right prerequisite;
  the secret scrub list grows; document `PIPELINE_FORGE` beside
  `PIPELINE_TICKET_PROVIDER` in REFERENCE.md and the README prerequisites
  table.
- **Behavior (user-visible):** a new supported env var; `gh` is no longer
  required when the resolved forge is not GitHub.
- **Tests:** merged check accepts only `MERGED` (not `OPEN`/`CLOSED`/
  `UNKNOWN`/empty URL); installer reports GitHub/GitLab/Bitbucket
  prerequisites correctly and still fails loudly when the required one is
  missing; doc assertions for the new variable.
- **Tier:** cloud open-source or better (three production files).

### S4 — GitLab adapter

- **Files:** new `pipeline/forge_gitlab.py`, small edits to
  `pipeline/forge.py` (registration), docs.
- **Scope:** implement all ten operations via `glab`:
  `glab mr create --source-branch <branch> --title … --description …`,
  `glab mr view <branch|url> --output json`, `glab mr update --title`,
  `glab mr note --message`, `glab mr merge --squash --yes`; CI via
  `glab api projects/:id/pipelines?sha=<sha>` (or commit statuses) and
  `POST …/pipelines/:id/retry`; `has_ci_config` = `.gitlab-ci.yml`; log
  excerpt from the failed job's `trace`.
- **Asymmetries to resolve explicitly in the brief:** GitLab has no
  "already exists" error string — `open_pr` must look up an existing MR for
  the source branch before creating; GitLab calls them merge requests, but the
  interface keeps the PR vocabulary; `manual` statuses map to `pass` (as
  GitHub's `skipped` does).
- **Tests:** a fake `glab` runner returning captured JSON fixtures for every
  operation and every CI state; `open_pr` is idempotent when an MR already
  exists; a missing `glab` produces `unreadable` only when `.gitlab-ci.yml`
  exists. Add a live-gated integration test (skipped unless
  `FAGAN_FORGE_INTEGRATION=1`).
- **Tier:** Claude-class or cloud open-source.

### S5 — Bitbucket adapter

- **Files:** new `pipeline/forge_bitbucket.py`, small edits to
  `pipeline/forge.py`, docs.
- **Scope:** REST v2 via `httpx` (already a dependency). Create PR, view,
  update title, comment, merge with `"merge_strategy": "squash"`, read
  `state`, read commit statuses, trigger a new pipeline, read pipeline step
  logs, detect `bitbucket-pipelines.yml`. Parse workspace/repo-slug from the
  `origin` URL.
- **Asymmetries to resolve explicitly:** there is no `gh`/`glab`-equivalent
  CLI, so this adapter is pure REST and owns its own auth; Bitbucket has no
  "rerun the failed jobs" operation — `rerun_failed` triggers a new pipeline
  for the same commit (documented behavior change, not a silent no-op); the
  log excerpt may return `None` in v1, falling back to the
  classification-only error.
- **Tests:** `httpx` mocked at the transport boundary; Basic and Bearer auth
  selection; URL parsing for both `bitbucket.org` and a self-hosted host;
  all six CI states; a 401 produces `unreadable` only when
  `bitbucket-pipelines.yml` exists; `pr_state` maps
  `MERGED`/`OPEN`/`DECLINED` and treats everything else as not merged.
- **Tier:** Claude-class.

## Back-compat and risk

- `auto` + an unset `PIPELINE_FORGE` is bit-for-bit today's behavior. Existing
  manifests, stories in flight, worktrees, and `pr_url` values need no
  migration.
- The public function names and signatures (`_open_pr`, `_merge_pr`,
  `_post_pr_comment`, `_ci_status`, `_ci_status_once`, `_ci_rerun`,
  `_repo_has_ci_configured`, `_pr_is_merged`) are preserved, so every
  orchestration call site and every test that mocks at that boundary is
  unaffected.
- The risky direction is the CI gate: a wrong mapping that turns `unreadable`
  into `none` lets the merge gate proceed without checks. Every adapter's
  contract tests must assert both sides of that distinction.
- `_merge_pr`'s `--subject` behavior exists because a squash merge otherwise
  inherits the branch's last commit message (PR #821). Each adapter must pass
  the PR title as the squash subject explicitly; a host that silently ignores
  it is a bug.

## Open questions

1. **Per-repo override.** `PIPELINE_FORGE` is process-wide, but `auto`
   detection is per repo, which covers mixed-repo installs. Is an explicit
   `.fagan.json` `forge` key worth adding in S3, or is the env var enough?
2. **Unknown-host fallback.** Falling back to GitHub preserves behavior but
   can misroute a self-hosted GitLab hostname that doesn't contain "gitlab".
   Should `auto` instead fail loudly, or accept a `PIPELINE_FORGE_HOSTS`
   mapping?
3. **Bitbucket rerun semantics.** Triggering a new pipeline is not the same as
   rerunning failed jobs. Confirm that is acceptable for the `cancelled` retry
   path, or make `rerun_failed` return `False` there and let
   `PIPELINE_MERGE_MAX_ATTEMPTS` handle it.
4. **GitLab CI source of truth.** Pipelines vs commit statuses: commit
   statuses include external statuses and may be closer to GitHub
   check-runs, but pipelines expose jobs for the log excerpt. Pick one in S4
   and document why.

## Registration (next step, not done here)

When the work is scheduled, decompose this plan with the product-analyst
(`decompose_plan` / `ingest_plan`), confirming the implementer tier first.
Each story's `agent_instructions` must then carry:

- a `Preflight:` line backed by a real impact run
  (`.claude/rules/local-dispatch-preflight.md`),
- the explicit list of retargeted test files and the literal before/after
  patch-target text (see "Test surface"),
- a `files:` declaration covering the production files named above,
- the no-time-relative-comments rule: write invariants, not "today only
  GitHub is supported".
