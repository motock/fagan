# Shared-service topology — decision record (deferred)

Status: **draft, not decided.** Written 2026-10-22 to capture the evidence for a
later decision. No story in this document is scheduled; nothing here is
ingested.

## Why this exists

The pipeline has so far been used by one planner ingesting plans one at a time
per repository. A team of 5–10 engineers working in a shared repository has
overlapping changes by default, so the question is which deployment topology
that team should run, and what the harness must grow to support it.

The answer is not "one toggle". Three topologies are in play, and they need
different work.

## The three topologies

| | What is shared | Where conflict surfaces | Runnable today |
|---|---|---|---|
| **T1 · Fully separate** | Only `origin` | Push / PR / rebase-onto-default at the merge gate | Yes — today's design center |
| **T2 · Same machine/user, several sessions** | `PLAN_DIR` (`~/.claude/plans`), `WORKTREE_ROOT`, `REPO_ROOT`, one scheduler daemon, one `.pipeline.env` | Everything in T1, plus dispatch races, shared worktrees, shared manifests | Yes — the existing locks exist for it |
| **T3 · One shared server, many clients** | All of T2, plus identity and tenancy | All of T2, plus authentication, authorization, ownership, decision routing | **No** |

T2 is easy to miss because it looks like a single-user setup. It is already
"many-to-one" on one machine: `pipeline/server.py` documents that `_plan_lock`
exists specifically because two Claude sessions, each with its own MCP server
process, could dispatch the same story and spawn a second agent into the same
worktree (the 2026-06-27 zero-output-death incident). Concurrency across plans
is therefore already partially handled — capacity accounting in
`pipeline/concurrency.py` scans every manifest and reaps dead-pid stories.

## Why T3 is not a configuration change

`pipeline/server.py` constructs `MCPServer("pipeline")` and calls `mcp.run()`
with no transport argument, which is **stdio**: one client per process. A
shared server needs all of the following, none of which exists:

1. **A network transport.** HTTP/streamable HTTP, a bind address, TLS, and a
   supervision story for restart and backpressure.
2. **Authentication.** Today the only credential-shaped surface is the
   dashboard's API key middleware. There is no notion of a caller identity in
   the MCP path at all.
3. **Authorization and ownership.** Neither a plan nor a story carries an
   owner. Nothing today can answer "may this engineer interrupt that story",
   or "whose decision request is this".
4. **Decision routing.** `request_decision` escalates to the `overlord` role and
   appends to the plan's decision log. On a shared server the human who should
   answer is not necessarily the one who started the plan; there is no routing
   or notification target per owner.
5. **Per-operator configuration.** `.pipeline.env` is a single file at the repo
   root and, by design, **wins over inherited environment variables**. Two
   operators with different model pins, notification settings, or concurrency
   caps cannot coexist in one process tree without changing that precedence —
   which the file's own header warns about.

## Latent hazards that already exist today

These are not hypothetical; each was verified in the source while drafting this
record.

### 1. Branch names collide across independent pipelines (T1)

Story branches are `agent/{story_key.lower()}` in at least six modules
(`pipeline/pr.py`, `dispatch.py`, `git_ops.py`, `merge.py`,
`review_orchestrator.py`, `escalation.py`). Keys minted automatically are
UUIDs, so they do not collide. Keys written by a plan author do: two engineers
running plans that both define a `SETUP-01` push to the **same branch** on the
shared `origin`, and each overwrites the other.

Options: namespace by plan name (`agent/{plan}/{key}`), or require UUID keys in
any plan intended for shared use. Namespacing breaks existing branches and the
resume path, so it is a migration, not a one-liner.

### 2. Worktree paths collide across repositories (T2)

`pipeline/dispatch.py` builds the worktree path as `WORKTREE_ROOT / story_key`,
and `WORKTREE_ROOT` defaults to the user-global `~/.claude/worktrees` — not a
per-repo directory. A plan in repo A and a plan in repo B that both use
`SETUP-01` want the same directory. Because the resume path infers "this story
is resumable" partly from worktree existence, a collision is not merely an
inconvenience; it can make one story resume another's directory.

### 3. The merge rebase and push are not under the git lock

`pipeline/merge.py::_try_acquire_git_lock` guards worktree creation
(`dispatch_worktree.py`) and the dispatch rebase (`dispatch.py`). The merge
gate's `_rebase_and_push_for_merge` rebases and force-pushes without it. Two
merges in the same second fetch the same default-branch SHA, rebase
independently, and the second one lands against a base that has moved. The
existing retry loop (`PIPELINE_MERGE_MAX_ATTEMPTS`) absorbs this, so the
observed effect is a latency cliff and occasional parking — not corruption. On
a shared server the cliff is on the critical path for everyone.

### 4. A story in one plan cannot see another plan's files

`files` is a scope guard at review time (`check_branch_scope`) and a sizing
input, never a reservation. Nothing asks whether another in-flight story is
editing the same path. This is the gap Plan 2 (file-overlap advisory and
cross-plan `blocked_by`) closes for T2, and would close for T3 once T3 exists.

## Decision options

| Option | What it means | Cost | Cost of being wrong |
|---|---|---|---|
| **A. Stay T1** | Each engineer runs their own pipeline against the shared remote; conflicts surface only at push/PR. | Zero. | Duplicate work; the pin class still bites (see Plan 1); no cross-engineer visibility. |
| **B. Standardize on T2** | One box (or one per team), several sessions sharing `PLAN_DIR`/`WORKTREE_ROOT`. | Small: Plan 2, plus fixing latent hazards 1–3. | Acceptable — it is the topology the locks already assume. |
| **C. Build T3** | A hosted server with identity. | Large: transport, auth, tenancy, routing, per-operator config. | Expensive to build before knowing the ownership model. |

## Recommendation

**Choose B now, and treat C as a separate product decision.** B is the only
option that is both runnable today and materially better than A, and its
prerequisites are already scoped: Plan 2 supplies cross-plan awareness, and the
three latent hazards above are each small, independent stories.

Do not start C until someone can answer, in one sentence, *who answers a
decision on a plan they did not create*. That answer determines the shape of
the tenancy model, and building the transport before it will produce the wrong
one.

## Prerequisites for C, in order

1. Own the decision-routing question (above); write it down as an ADR.
2. Fix latent hazards 1 and 2 (namespacing), since both become multi-tenant
   correctness bugs once several engineers share a server.
3. Add an `owner` to a plan (and optionally to a story) and enforce it in the
   mutating tool surface.
4. Only then add a transport, authentication, and per-operator configuration
   precedence.

## Open questions for the decision

- How many engineers, on how many machines, and do they share a filesystem?
- Is a plan owned by one engineer, by a team, or unowned?
- Does the shared server need to survive a reboot unattended?
- Is the pipeline the system of record for plan state, or is the repository?
- Would a simpler answer suffice — one pipeline instance per engineer (T1),
  with Plan 1 and Plan 2's advisory making cross-plan collisions visible
  anyway?

## Related

> **Update 2026-10-22:** Plan 1 shipped as `dashboard-pin-guard-and-split` and
> Plan 2 as `cross-plan-awareness`, which closes latent hazard 4 (in-flight file
> overlap is now advised on, and `blocked_by` gates ready stories). Hazards 1-3
> are still open in the code: branch names are `agent/{story_key.lower()}`, the
> worktree is `WORKTREE_ROOT / story_key`, and `_rebase_and_push_for_merge` runs
> without the git lock.

- Plan 1 (frozen-pin lint; dashboard split) — removes a conflict class that
  behaves identically in all three topologies.
- Plan 2 (cross-plan file-overlap advisory and `blocked_by`) — the T2
  coordination layer.
- `docs/plans/server-app-file-split.json` — the historical split chain and the
  monkeypatch hazard it documents.
