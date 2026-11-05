# 0013. Operator commands wait a bounded time and compare-and-set the manifest revision, under a (namespace, plan) lock

- **Status:** Proposed
- **Date:** 2026-11-04
- **Related:** [0008](0008-shared-control-plane-per-repo-workers.md),
  [0009](0009-tenancy-and-authorization-model.md),
  [0011](0011-scheduler-scale-out-leased-plan-advancement.md),
  [0012](0012-store-contract-v2.md)

## Context

[ADR 0011](0011-scheduler-scale-out-leased-plan-advancement.md) makes plan
advancement leased and event-driven. It does not cover the operator-facing
commands. `patch_story`, `set_story_status`, `approve_merge` and `approve_plan`
still take the same per-plan flock the advancer takes, and they take it
**fail-fast**: if the advancer is mid-pass, the command returns "busy" and the
operator is expected to try again.

That was acceptable when one operator drove one host with a handful of plans.
It is not acceptable for the stated target, one control plane serving many
teams (ADR 0008) fronted by MCP: a multi-tenant control plane cannot ask an
operator to poll. "Busy, try again" is a race the user loses, and it gets worse
exactly when the platform is under load, because the advancer holds the plan
lock across the expensive work 0011 catalogues - the merge-gate re-verify, the
dispatch baseline snapshot, reviewer model calls - for minutes at a time. A
retry loop from a client is not a fix: it burns the client's time, it is
unbounded, and under concurrency it turns contention into a thundering herd.

Two further forces make the naive fixes wrong:

- The lock is keyed on the plan name alone. ADR 0012 establishes that plan
  names are not global once the store has a namespace. A lock keyed on the bare
  plan name either collides across tenants or silently serializes unrelated
  tenants against each other.
- The commands themselves are cheap. They mutate a few fields of one record.
  The expensive work they sit behind is not theirs.

The evidence for how long the lock is actually held is thin. **No lock-hold
measurements exist yet as of 2026-11-04; when the lock-hold log is available,
record p50/p99 hold times and the log path here.** Structured hold logging
exists (`test_plan_lock_hold_logging`), so the numbers are obtainable; they are
simply not yet collected.

## Decision

**We make an operator command a short, bounded, conflict-detecting write on the
manifest, independent of how long the advancer's heavy work takes.**

1. **Commands wait a bounded time, then report `retry_after`.** A command
   acquires or waits on the plan lock for a bounded interval rather than
   failing fast. If the interval expires it returns a structured
   `retry_after` value so a client knows when to come back instead of guessing
   or polling at a fixed interval. The wait and the `retry_after` shape are the
   subject of this plan's previous story; this record fixes the contract that
   the deadline is bounded and the answer is explicit.

2. **A command is a compare-and-set on a manifest revision.** Read revision N,
   apply the change, write only if the record is still at revision N; if it is
   not, re-read and retry on the new revision. Long work - baseline, review,
   merge, CI - never runs inside the command. This is what makes the bounded
   wait sufficient rather than merely longer: contention is resolved by
   detecting the conflict, not by holding the lock for the duration of the
   work.

3. **Claim, work, record.** The advancer takes the lease, does the heavy work
   outside any lock, and records the result with a revision check bound to the
   commit SHA it worked on (ADR 0011 decision 4). A result computed against a
   revision or a SHA that is no longer current is discarded rather than
   written. Heavy work therefore never appears in a lock-hold measurement, and
   an operator command never waits behind it.

4. **The lock key becomes (namespace, plan).** The plan lock is keyed on the
   pair, so two tenants' plans named the same do not collide and do not
   serialize against each other. Core carries only the opaque namespace
   `"default"`, exactly as ADR 0012 specifies: the namespace is a seam, not a
   feature, and core never derives meaning from it. Per-namespace fairness and
   quotas live in the enterprise overlay, not in core.

5. **The end state is a single writer per (namespace, plan) over the event
   stream**, which is what removes the flock entirely. It is reached only after
   ADR 0011 stage S2, when leases and the advance queue live in the store and
   are written in the same transaction as the event outbox. Do not build it
   before S2: until the lease is a store row, a single-writer design is a
   second, weaker locking scheme layered on the flock.

6. **Interim mitigation is the queue mode.** Until S2, the supported
   mitigation for contention is `PIPELINE_ADVANCE_MODE=queue` with
   `PIPELINE_ADVANCE_WORKERS` above 1, so advancement runs off the request path
   and operator commands meet a lock holder far less often. No lock-hold
   measurements exist yet as of 2026-11-04; when the lock-hold log is
   available, record p50/p99 hold times and the log path here.

## Consequences

- **The manifest gains a revision field.** That is a schema addition to the one
  record that ADR 0012 makes authoritative for a plan, so it must be versioned
  and migrated, not slipped in. See
  [ADR 0012](0012-store-contract-v2.md): the manifest is the plan, and a record
  exists in the store and nowhere else.
- **`approve_merge` outlasts the 300 s MCP idle timeout.** It triggers work
  that cannot finish inside a synchronous MCP call, so the bounded-wait
  contract is not enough for it on its own. It should become an asynchronous
  job that returns a handle the client can poll or resume, with the decision
  recorded separately.
- **Callers must handle `retry_after`.** Every client of these commands,
  including the MCP tools and the dashboard, needs a path for "not yet" that is
  not an error dialog.
- **Two lock keys exist for a while.** Until S2 the flock is keyed on
  (namespace, plan) while leases are still host-local; the two must agree, or
  a plan can be advanced and commanded under different keys.
- **Tempting fixes to avoid.** Each of these looks like a smaller change and
  each makes the system worse:
  - **Lengthening the wait.** It converts a fast failure into a slow one and
    leaves the command holding a lock while it does nothing; with many tenants
    the queue of waiters is itself the outage.
  - **A blocking acquire in the scheduler.** It reintroduces the head-of-line
    blocking ADR 0011 removed: one slow plan stalls the tick for every plan.
  - **A second scheduler before S2.** Without leases in the store, two
    schedulers advance the same plan concurrently and corrupt the manifest;
    "exactly one consumer" (ADR 0011 decision 7) is a consequence of S2, not a
    prerequisite that can be skipped.
- **No measured baseline yet.** The claim that commands are cheap and holds are
  dominated by heavy work is an argument from code structure, not a
  measurement. When the lock-hold log yields numbers, record them here.
