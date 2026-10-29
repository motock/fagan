# 0011. The scheduler advances plans as leased work items and runs heavy work as detached jobs

- **Status:** Accepted
- **Date:** 2026-10-28
- **Supersedes:** rule 6 of [ADR 0010](0010-event-delivery-contract-and-nats-jetstream.md)
  ("exactly one consuming process per deployment").

## Context

Operators running several plans in parallel see stories move slowly through the
lifecycle. Nothing deadlocks; the scheduler does all of its work for every plan
in one serial loop:

- `SchedulerDaemon.run_once` runs scan, then reconcile, then the outbox drain.
  Reconcile is `PipelineService.advance_all_plans`, a `for` loop that calls
  `advance_pipeline` for every manifest in turn. The scan phase is not cheap
  either: each `agent_done` it publishes reaches `wake_handler` on the
  in-process bus, which runs a full `advance_pipeline` before the scan moves on.
- Inside one plan's tick, the merge gate (`advance_merge._adjudicate_merges`)
  runs `_reverify_acceptance` (the full test suite when a story has no
  acceptance fixture) and `_reverify_build` inline, with no timeout, while
  holding the plan lock. A fresh local-family dispatch runs
  `_run_baseline_test_snapshot` (the full suite again, bounded at 900 s)
  before the agent starts. Review runs the reviewer model calls (each bounded
  at 180 s by the scheduler's clamp). On this repository the suite takes
  133–143 s on 4 cores.
- `PIPELINE_MAX_DISPATCH_PER_TICK` defaults to 1 per plan per tick, so a plan
  with four ready stories needs four passes, and a pass can take many minutes.
- A pass that outruns the 900 s reconcile deadline is abandoned. Its thread
  keeps the plan lock, and 300 s later the daemon exits so launchd restarts
  it. The restart kills any suite or review in flight, and the next pass
  starts that work again.
- `_scoped_repo_root` swaps the module-global `REPO_ROOT` for each plan
  (`pipeline/server.py`), and about 100 call sites across 25 modules read that
  global. Two plans can therefore not be advanced on two threads of one
  process.

That design serves one host with a handful of plans. The stated target is one
control plane serving many teams (ADR 0008). With hundreds of plans, a faster
loop is not enough:

- Running every suite on the control-plane host saturates its CPU however the
  loop is parallelized.
- Sweeping every plan each pass costs work in proportion to the number of
  plans, not to the changes that happened. `scan_all_plans` also re-reads
  every manifest about once a second.
- ADR 0010 rule 6 and the daemon's single-instance lock allow exactly one
  scheduler process, which is both a ceiling and a single point of failure.
- One `gh` CI poll per pending story per pass meets GitHub's API rate limits.
- Concurrency is capped per process (`MAX_CONCURRENT_AGENTS`), not shared
  fairly across tenants and provider quotas.

## Decision

**We treat "advance plan X" as a unit of work that an advancer runs under a
time-bounded lease on that plan, triggered by events, and the control plane
never runs a test suite or build inline.**

1. **Leased per-plan advancement.** Exactly one advancer works on a plan at a
   time, and it holds a lease on the plan that expires on its own. A crashed
   advancer's plans pass to another advancer when the lease expires; recovery
   never depends on restarting the whole scheduler.
2. **Isolation per plan.** No per-plan state may live in a process global
   while two plans are advanced concurrently in the same process.
   Until `REPO_ROOT` is request-scoped (ADR 0008's "Request-scoped
   repo/config"), each concurrent advance runs in its own process.
3. **Event-triggered, not swept.** A state change (agent done, grade
   finished, review verdict, CI result, operator action) enqueues "advance
   plan X". Requests for the same plan coalesce while one is queued. The
   periodic reconcile remains as a slow safety net that enqueues plans whose
   last advance is older than a staleness bound; it no longer advances every
   plan itself.
4. **Heavy work is a detached job.** Grading, merge-gate re-verify, the
   dispatch baseline and builds run as jobs whose results a later advance
   collects, following the existing detached-grade pattern
   (`pipeline/detached_grade.py`). In ADR 0008 stage 3 those jobs run on
   per-repo workers and report back as events. A result is bound to the
   commit SHA it graded; a result for a SHA that is no longer the branch head
   is discarded.
5. **Fair admission.** Advancement order rotates so no plan is always last.
   Dispatch admission draws from a global budget per provider and model,
   divided across tenants and plans by weight, instead of a per-process cap.
6. **Push before poll.** CI status arrives as forge events (GitHub check-suite
   webhooks) where the forge supports them; polling is the fallback.
7. **Consumer groups replace the single consumer** (supersedes ADR 0010 rule
   6). Every durable event is consumed by exactly one member of a consumer
   group. Any number of scheduler processes may be members, and work for one
   plan is serialized by its lease, not by having one consumer.

### Staging

| Stage | What changes | Where the lease lives | Depends on |
|---|---|---|---|
| S0 | `PIPELINE_ADVANCE_WORKERS` runs each plan's advance in a child process from a bounded pool with a pass deadline and rotation. The merge-gate re-verify becomes a detached job. The dispatch baseline is computed once per commit. (`scheduler-throughput` plan.) | The plan flock, held by the child; process death releases it. | `system-principals` |
| S1 | `wake_handler` enqueues instead of advancing inline; long-lived advancers drain an in-process queue; reconcile becomes a staleness sweep. | The plan flock. | S0 |
| S2 | Leases and the advance queue move into the control-plane store, written in the same transaction as the event outbox. More than one scheduler process may run. The single-instance lock goes away. | A lease row with an expiry. | ADR 0008 stage 2b, ADR 0010 broker |
| S3 | Heavy jobs run on workers; CI arrives by webhook; fair-share admission. | A lease row. | ADR 0008 stage 3 |

### Progress

S0 has shipped: the `PIPELINE_ADVANCE_WORKERS` pool, the detached merge re-verify and the per-commit dispatch baseline. The throughput work is tracked in `docs/plans/SCHEDULER_THROUGHPUT_PLAN.json`.

S1 is specified in `docs/plans/SCHEDULER_EVENT_DRIVEN_PLAN.json` and runs behind `PIPELINE_ADVANCE_MODE=queue`. It adds two items beyond the S1 row in the staging table above:

- Wake signals for detached-job results, `blocked_by` completions and operator actions.
- Per-repository batched CI status, pulled forward from S3: the per-PR status poll meets GitHub's rate limit at roughly 50-80 open pull requests.

## Consequences

- **S0 buys tens of plans, not hundreds.** A child process costs about 1 s of
  CPU to import the pipeline (measured 0.9–1.4 s). At four workers and a 60 s
  pass that is acceptable; at hundreds of plans it is not, which is why S1
  moves to long-lived advancers and S2 removes the sweep.
- **Killing a child is now the recovery.** A timed-out child is terminated,
  which releases its plan flock. That replaces the abandoned-thread and
  process-exit path for reconcile. A child killed between claiming a dispatch
  lease and launching the agent leaves the lease to expire
  (`PIPELINE_DISPATCH_LEASE_TTL_SECONDS`), the same exposure as a crash today.
  Grandchildren it started (a `git` or test run) are not killed and finish
  orphaned. An agent the child launched keeps running after the child
  exits; agent liveness is checked by pid, so nothing depends on the child
  surviving.
- **Children run as `system:scheduler`.** Only the scheduler's own reconcile
  uses the pool. The `advance_all_plans` MCP tool keeps advancing in-process
  under the caller's identity: handing a remote caller's request to a child
  that runs as the scheduler would escalate its privileges.
- **Short-lived children export telemetry as separate instances.** Each child
  flushes its spans and counters before exiting, under its own
  `service.instance.id`. That costs series churn at S0 and disappears with
  long-lived advancers at S1.
- **The merge gate spans ticks.** `master` can move between the re-verify and
  the merge. That is the same exposure the merge gate already has while CI is
  pending, and the existing conflict and retry paths handle a merge that no
  longer applies. A re-verify result is discarded if the branch head moved.
- **No ordering across plans or events** (ADR 0010 rule 3). A handler that
  depends on the order in which plans are advanced is wrong. Read the
  manifest.
- **Tempting "fixes" to avoid:** do not parallelize plans on threads while
  `REPO_ROOT` is a process global; do not raise the reconcile deadline to hide
  a slow pass instead of moving its heavy work into a job; do not add a second
  scheduler process before leases live in the store.

Related: [0004](0004-safety-gates-default-on-and-fail-closed.md),
[0008](0008-shared-control-plane-per-repo-workers.md),
[0009](0009-tenancy-and-authorization-model.md),
[0010](0010-event-delivery-contract-and-nats-jetstream.md),
`docs/plans/SCHEDULER_THROUGHPUT_PLAN.json`.
