# 0008. Enterprise topology is a shared control plane with per-repo workers

- **Status:** Accepted (direction agreed 2026-10-25; accepted 2026-10-28)
- **Date:** 2026-10-25
- **Implementation status (2026-10-28):** stage 0, stage 1 except for the
  tools that three in-flight plans still gate
  (`enterprise-enforcement-closeout`, `decision-routing`,
  `system-principals`), and every Tier 1 prep item are implemented. Stage 2a
  is drafted as the `control-plane-store` plan. Stages 2b–4 are not started.

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
   against `PipelineService` and `Store` (ADR 0007). Landed.
1. Verified caller identity at the gateway, RBAC and ownership records,
   authorization enforced in `PipelineService`, per-principal notifications.
   Substantially landed: OIDC verification, the `Action` enum, ownership
   records and the audit chain are in; the three enforcement plans close the
   remaining tools.
2. Server-mode persistence, in two parts:
   - **2a. A single-host database `Store`.** `PIPELINE_STORE` selects it,
     defaulting to `file` and failing closed on an unknown value, plus the
     Tier 1 namespacing already shipped. It keeps the existing
     single-writer `transaction()` contract, and the process globals listed
     above stay valid — which is what the one-repo server mode needs.
   - **2b. A database `Store` with distributed locks** (Postgres), adopted
     together with stage 3's remote workers, when more than one process
     writes the same repository's state.
3. A work queue and worker registration: the control plane decides readiness,
   workers report capacity and heartbeats.
4. Generic or ephemeral workers, and federation per trust boundary.

**System of record.** The split is by kind of state, not by convenience:

- The **repository** is the system of record for code — commits, branches,
  worktrees and merges live in git and are read from git.
- The **store** is the system of record for plan and control state —
  manifests, story lifecycle and status, decisions, approvals, plan owner and
  delegates, per-plan configuration, and the authorization and audit records.

A value is authoritative on exactly one side; neither is written through to
the other. In stage 2a that means manifests and decisions move into the
database while the journal, notification and audit sidecars stay authoritative
on disk, and there is no migration from `FileStore`: a repository switching
`PIPELINE_STORE` starts from the plans it already has on disk through normal
ingest.

## Prep list: low-hanging fruit that preserves local behavior

The topology above is a fork, not a toggle. Three modes are worth naming
separately, because only the third crosses the fork:

1. **local** — today: one operator, `FileStore`, stdio MCP, no auth, plans
   auto-approved, one repo per process.
2. **one-repo server** — a shared identity, registry, approval, and audit
   layer, but each control-plane process still owns one repo. The existing
   process globals remain valid. This is the cheapest enterprise step and
   likely the one most teams need first.
3. **multi-tenant server** — one process serving many repos concurrently
   and/or distributed workers. This is the fork: `REPO_ROOT` and the other
   globals must become request-scoped, the store must be a database, and the
   scheduler must become a queue with heartbeats.

Every prep item below must satisfy two tests: (a) its default reproduces
today's behavior exactly, and (b) it pays for itself today, ideally by closing
an existing hazard. Anything that fails either test is the server
implementation, not prep.

### Tier 1 — behavior-preserving, also closes a current hazard (shipped)

All three shipped before this record was accepted.

- **Optional repo-root allowlist (shipped).** `validate_workspace`
  (`pipeline/workspace.py`) applies a deny list but no allow list; only
  `create_workspace` does. Any existing git repository outside the deny list is
  a valid plan target. `PIPELINE_ALLOWED_REPO_ROOTS` now applies an allow list;
  unset means today's behavior, set means fail closed. It closes a
  prompt-injection path-targeting hole and is the boundary stage 3 needs —
  see the allowlist note under Consequences.
- **Worktree path namespacing (shipped).** Branch namespacing shipped as
  opt-in (`pipeline/branch_names.py`, `PIPELINE_BRANCH_NAMESPACE`, default
  `agent/<key>`); worktrees had no equivalent. `PIPELINE_WORKTREE_NAMESPACE`
  now has the same default-preserving shape.
- **Surface effective topology in health (shipped).** `plan_dir`, `repo_root`,
  `branch_namespace`, and `worktree_namespace` are reported in the scheduler
  health file and `/api/health`. Pure additive.

These three are behavior-preserving and may be registered independently of
this record; none of them implements T3.

### Tier 2 — cheap seams, but they touch a public shape

- **Plan metadata at save time:** `saved_at`, `saved_by` (default
  `local-operator`), and a content digest, stored inside the plan JSON (not a
  sidecar, which would add `list_plans` noise). Ingest ignores unknown
  top-level keys. Preps ADR 0007 without implementing the gate.
- **Store factory:** `PIPELINE_STORE=file`, with an unknown value failing
  closed, replacing `_store = FileStore()` in `pipeline/server.py`. A one-line
  seam with no behavior change. Stage 2a builds on it.

### Not prep — these cross the fork

Request-scoped repo/config, replacing `_scoped_repo_root`; a database `Store`
with distributed locks (stage 2b); a queue, worker registration, and heartbeats
replacing pid liveness; AuthN/AuthZ and HTTP transport; plan-name scoping per
owner/tenant and row-level repo authorization.

Do not add a `PIPELINE_MODE` flag until at least one gate consumes it. A mode
switch nothing enforces is cosmetic, and ADR 0004 records the cost of a gate
that reports success while it is off.

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
- **The repo-root allowlist becomes load-bearing.** It is optional today
  (unset means no allow list). It must become mandatory as soon as a shared
  server accepts plan writes, because from then on `repo_root` is
  caller-supplied and a deny list is not a boundary.
- Not decided: how many trust boundaries or control planes; generic versus
  repo-pinned workers; the control plane's HA and failure domain.
- Do not build the network transport before identity and ownership exist
  (ADR 0006 and this record), and do not implement the approval gate as
  filesystem-local logic. Both would be rebuilt.

Related: [0006](0006-decision-routing-on-a-shared-server.md),
[0007](0007-plan-approval-at-activation.md),
[`SHARED_SERVICE_TOPOLOGY_PLAN.md`](../plans/SHARED_SERVICE_TOPOLOGY_PLAN.md).
