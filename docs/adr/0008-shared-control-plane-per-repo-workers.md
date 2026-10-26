# 0008. Enterprise topology is a shared control plane with per-repo workers

- **Status:** Proposed (direction agreed 2026-10-25; revisit before implementation)
- **Date:** 2026-10-25

## Context

The stated enterprise use case is one remote pipeline that many engineers reach
over MCP, with plans against a shared origin. The topology record
(`docs/plans/SHARED_SERVICE_TOPOLOGY_PLAN.md`) calls this T3 and lists what does
not exist: a network transport, authentication, authorization and ownership,
decision routing, and per-operator configuration.

The open question was whether a per-repo instance scales. The answer separates
into two planes that the current code bundles:

- **Per-repo control does not scale.** A large organization would run one plan
  registry, dashboard, identity configuration, scheduler clock, and config
  surface per repository: no cross-repo approval inbox for a lead, capacity
  stranded in idle instances, and upgrade and configuration drift across
  hundreds of deployments.
- **Per-repo execution does scale.** It is how CI works, and the code already
  supports it: `pipeline/execution.py` has a fail-closed
  `PIPELINE_EXEC_<ROLE>` gate and an ssh path (`_spawn_ssh` →
  `pipeline/remote_exec.py` → `pipeline/remote_sync.py`), with `app/backend.py`
  and `app/harness.py` abstracting the model and the harness. The remote-exec
  spec is explicit that it is "not a remote queue or a multi-host scheduler"
  (`docs/specs/REMOTE_EXECUTION.md` §1).

The current process would also be *unsafe* if it served all repositories
concurrently, because several load-bearing values are process globals:

| Assumption | Where | Why it breaks under one shared process |
|---|---|---|
| `REPO_ROOT` is a mutated global | `_scoped_repo_root`, `pipeline/server.py:523` | Concurrent requests for different repos race; one tenant's merge can run against another's path |
| `PLAN_DIR` / `WORKTREE_ROOT` are globals | `pipeline/paths.py:24-28` | Shared namespace; plan-name collisions overwrite each other |
| `.pipeline.env` beats inherited env | ADR 0006's noted gap | Per-operator model pins and concurrency caps cannot coexist |
| One heavy-build lock per `PLAN_DIR` | `_heavy_lock`, `pipeline/concurrency.py:443` | One repo's build serializes every other repo's build |
| Capacity scans manifests; liveness is `os.kill(pid, 0)` | `pipeline/concurrency.py:97` | Capacity is a host property and liveness is local; neither survives remote workers |
| One scheduler daemon per `PLAN_DIR` | `pipeline/scheduler_daemon.py:950` | Needs leader election, not a per-machine file |
| File store plus `flock` | `pipeline/store.py`, `pipeline/concurrency.py` | Single-host only |

What is already split, and therefore reusable: `PipelineService` and the
`Store`/`FileStore` seam, the execution-mode gate, remote sync, and the backend
and harness protocols. The work is concentrated in the process-global
assumptions above, not in the orchestration logic.

## Decision

**T3 is a shared control plane with per-repo (or generic) workers.** Not one
process that serves every tenant *and* executes every repository, and not one
control plane per repository.

**The control plane owns:** identity and RBAC, the plan registry and lifecycle
(including approval per ADR 0007), audit, scheduling decisions and the work
queue, cross-repo views such as one approval inbox, per-repo policy and
configuration, notifications, and the MCP/HTTP gateway.

**Workers own:** repository checkout and worktrees, git fetch/rebase/push,
build and test execution (`_heavy_lock` stays on the worker, where it is
correct), agent spawn and backend resource status, per-worker capacity, and
heartbeats.

Workers may begin as today's per-repo server process, driven by the control
plane and reporting status back through the shared store. They should trend
toward generic, ephemeral runners that materialize a repository on demand;
`remote_sync` already approximates the materialization step.

**Control planes are federated per trust boundary** (business unit, compliance
domain), not one global plane, unless repository segmentation is weak enough
that a single plane can safely hold all repositories' plans and briefs.

### Staging

0. Plan lifecycle and approval in the current single-instance code, implemented
   against `PipelineService` and `Store` (ADR 0007).
1. Verified caller identity at the gateway, RBAC and ownership records,
   authorization enforced in `PipelineService`, per-principal notifications.
2. A DB-backed `Store`; per-repo namespacing for plans, worktrees, and branches;
   a server-side `repo_root` allowlist (never trust the caller's path).
3. A work queue and worker registration: the control plane decides readiness,
   workers report capacity and heartbeats.
4. Generic or ephemeral workers, and federation per trust boundary.

## Consequences

- The control plane's operational surface is constant regardless of repository
  count, capacity is pooled, and a lead gets one approval inbox and one audit
  log across repositories. Execution scales independently and can stay
  per-repo where GPU, network, or venv constraints require it.
- The state machine, gating, review, merge, escalation, and dispatch logic are
  reused; the refactor is concentrated in the global-state and liveness
  assumptions listed above.
- **Cost — row-level authorization is now the product.** `principal → team →
  repository → plan → story` must be enforced in the service. The control-plane
  store holds every repository's plans and briefs, so a repository-scoping bug
  becomes a cross-tenant leak. Per-repo processes would contain that bug by
  construction; the shared plane trades isolation-by-construction for
  visibility and must earn it with authorization.
- **Cost — distributed operations.** Pid-based liveness becomes heartbeats and
  leases; the scheduler flock becomes leader election; `.pipeline.env`
  precedence becomes per-repo configuration from the control plane.
- Not decided: how many trust boundaries or control planes; generic versus
  repo-pinned workers; the control plane's HA and failure domain; whether the
  control plane or the repository is the system of record.
- Do not build the network transport before identity and ownership exist
  (ADR 0006 and this record), and do not implement the approval gate as
  filesystem-local logic. Both would be rebuilt.

Related: [0006](0006-decision-routing-on-a-shared-server.md),
[0007](0007-plan-approval-at-activation.md),
[`SHARED_SERVICE_TOPOLOGY_PLAN.md`](../plans/SHARED_SERVICE_TOPOLOGY_PLAN.md).
