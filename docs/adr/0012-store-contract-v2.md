# 0012. The Store holds namespaced records, and the file layout is one implementation of it

- **Status:** Proposed (draft 2026-11-04, not yet reviewed)
- **Date:** 2026-11-04
- **Amended:** 2026-11-06 (multi-organization deployments are an enterprise concern; see 'What is out of scope')
- **Builds on:** [0008](0008-shared-control-plane-per-repo-workers.md) (store is
  the system of record for plan and control state; no write-through),
  [0009](0009-tenancy-and-authorization-model.md) (service-layer policy over a
  verified principal), [0011](0011-scheduler-scale-out-leased-plan-advancement.md)
  (leases and the advance queue live in the store)

## Context

ADR 0008 made the store the system of record for plan and control state and
staged a database store (2a single host, 2b Postgres with distributed locks).
Stage 2a shipped as `SqliteStore`, and it shows why the `Store` contract, not the
database, is the obstacle:

- **The database holds one record kind.** `SqliteStore` has one table,
  `story_manifests`. Its own module comment says everything "natively a
  filesystem object is delegated to an internal file store". Decisions,
  journals, notifications, the plan source, the audit chain and the event log
  remain files, so a deployment on SQLite has two homes for control-plane state.
- **The Protocol leaks paths.** `manifest_path()`, `get_worktree_file()` and
  `get_story_log()` return or read files. A store with no filesystem cannot
  implement them.
- **Most of the code bypasses the Protocol.** About 50 modules under `pipeline/`
  and `app/` read or write `PLAN_DIR` directly, and about 316 test files
  reference it. The Store is the contract only on paper.
- **The plan is stored twice.** `save_plan` writes `<plan>.json`; `ingest_plan`
  derives `<plan>.manifest.json` from it and copies each story's brief into the
  manifest. Both are then authoritative for different questions.
- **Names are global.** Every `Store` method takes a bare `plan_name`. Two teams
  naming a plan the same collide, and a scoping bug is a cross-namespace leak
  (ADR 0008, "row-level authorization is now the product").

Leases (ADR 0011 S2), a Postgres store (0008 stage 2b) and scoped access (0009)
all need the same prerequisite: a store contract made of records, with a namespace
key, that a database can implement without a filesystem.

### What is out of scope

Core gives the namespace **no meaning**. It is an opaque string that core always
sets to `"default"`. What a tenant is, how one is resolved from an identity
provider's claims, how it maps to teams and roles, how isolation is enforced and
audited, and any hosted multi-customer operation (per-namespace encryption
keys, quotas, scheduler fair-share, billing) belong to the proprietary
enterprise distribution and are described in its own documentation. The
core distribution assumes nothing about how many organizations a deployment
serves: it holds one opaque namespace and always passes "default". The hosted
enterprise distribution serves many organizations, each with teams and one
namespace per team, as decided in the enterprise tenancy ADR (enterprise
distribution, ADR 0001, Accepted 2026-11-06). It realises that through a store
plugin that supplies real namespaces; the Store signatures do not change.
(Amended 2026-11-06; this replaces the earlier one-organization assumption
decided 2026-11-04.)

## Decision

1. **Records, not paths.** `Store` exposes typed operations per record kind.
   No control-plane method accepts or returns a filesystem path. Reads of
   worktree files and story logs are not store records (they come from git and
   the host); they move to a separate `WorkspaceReader` seam.

2. **Every call carries a namespace.** The first argument of every `Store`
   operation is `namespace: str`. Core always passes `"default"` and attaches no
   meaning to the value; it is a seam, not a feature. A store implementation
   partitions by it and offers no operation that spans namespaces. Whatever
   resolves the namespace for a request is outside core: core's service passes
   the constant, and an operator-supplied component (see the plugin loader) may
   supply another. The namespace is never taken from a request parameter, a
   plan name or a repo path. Authorization (team, role, owner) stays in the
   service under ADR 0009; the namespace argument makes a cross-namespace read
   structurally impossible for an implementation that honours it. Teams remain
   an authorization attribute on a plan, not part of any key.

3. **Record kinds.** Control-plane records, namespaced:

   | Record | Today | Notes |
   |---|---|---|
   | Manifest (plan and story state, including each story's live brief) | `<plan>.manifest.json`, `story_manifests` | the single record for a plan once ingested (decision 5) |
   | Plan draft (transient) | `<plan>.json` | staged by `save_plan`, consumed by `ingest_plan`; not a record afterward |
   | Decisions | `<plan>.decisions.json` | |
   | Journal (per story) | `<plan>.<key>.journal.json` | |
   | Notification outbox (per channel) and notification history | `.notifications.jsonl`, `.log`, outbox suffixes | the only durable event-derived record (decision 9) |
   | Usage (one record per model call) | none yet: `review_token_costs.jsonl` is written only for benchmark cells | append-only and namespaced; written in the same transaction family as the notification outbox; fields in `docs/TELEMETRY_CONTRACT.md` |
   | Audit chain | `<plan>.audit.jsonl` | append-only table, hash-chained, one chain per plan plus a host chain per namespace (decision 10) |
   | Context snapshot (content-addressed, immutable) | `context/<digest>/` | org-owned, namespaced blob; see decision 11 |
   | Plan completion marker and report | `.plan_completed`, `.report.md` | |
   | Leases and the advance queue | none yet | ADR 0011 S2 |

4. **Host-local state stays out of the store.** Locks, the scheduler pid file and
   health, `heavy.lock`, recent and active workspace, dashboard UI state, and
   git clone working copies under `context/git/` describe one host and can be
   rebuilt. They live behind a `HostState` interface, are never replicated, and
   are not namespaced data.

5. **One authoritative home per value; the manifest is the plan.** ADR 0008's
   no-write-through rule is extended: a record exists in the store and nowhere
   else. After `ingest_plan` the manifest is the only record of a plan and holds
   each story's live brief. `<plan>.json` stops being stored: `save_plan` stages
   a transient draft that `ingest_plan` consumes, and the document is rendered
   from the manifest on demand when an export is wanted. A file under `PLAN_DIR`
   is the `FileStore`'s own representation of a record or an explicit export,
   never a second source. The approval digest must therefore be computed from
   manifest content (see Consequences).

6. **Transactions and leases are store primitives.** `transaction(namespace, plan)`
   stays non-blocking. The store adds lease operations (acquire, renew, release,
   with a fencing token) so ADR 0011 S2 needs no new mechanism.

7. **Implementations.** `FileStore` keeps the JSON layout, under
   `PLAN_DIR/<namespace>/`. `SqliteStore` stores every record kind in tables and no
   longer delegates to a file store. A Postgres store, or any other, is a
   plugin loaded through the plugin loader (plan `plugin-seams`) and must pass
   the same store contract suite.

8. **Migration is strangler-style and behavior-preserving at every step.**

   | Stage | Change | Guard |
   |---|---|---|
   | A | Add the record operations and `namespace` argument beside the existing methods; `FileStore` implements both | contract suite runs against `FileStore` and `SqliteStore` |
   | B | Move one module family at a time off direct `PLAN_DIR` use (manifest and decisions, then journal, then notifications and outbox, then audit and events, then plan source) | a test lists the modules still allowed to touch `PLAN_DIR`; the list only shrinks |
   | C | Remove the path-returning methods; make `SqliteStore` self-contained | the allowlist is empty |
   | D | Move the flat layout into `PLAN_DIR/default/` with a one-time, reversible migration | upgrade and rollback test |

9. **Events are transport; the outbox is the durable record.** The store keeps
   no event log. The message broker (NATS JetStream or any plugin bus) owns
   persistence of events when a deployment needs it. The JSONL bus remains an
   `EventBus` implementation for single-host use, writing under `HostState`, not
   a store record. Scheduler wake events are derived state: the reconcile sweep
   (ADR 0011) recovers any that are lost, so no store record is needed for them.
   The notification outbox is the one event-derived record that must survive a
   crash, and it is written in the same transaction as the state change that
   caused it.

   Amended 2026-11-07: 'Same transaction' means atomic on stores that declare atomic transactions (SqliteStore, any database plugin); the file store is ordered, not atomic, and the loss window is a crash between the state write and the outbox append.

10. **Audit chain: an append-only table plus a pluggable anchor sink.** The hash
    chain is unchanged. Records are rows `(namespace, chain, seq, prev_hash,
    record, hash)` with `(namespace, chain, seq)` unique; the application's database
    role has INSERT and SELECT only, so the application cannot rewrite history.
    The chains are one per plan plus one host chain per namespace (the existing
    `host` chain). A database administrator can still rewrite a whole chain, so
    tamper-evidence comes from the anchor: a new `AnchorSink` Protocol
    (`publish(namespace, chain, seq, head_hash)`, `read_heads()`) replaces the
    hard-coded `file:` parser. `file:` stays the default sink; object-lock
    storage, syslog or a SIEM are plugins. A store other than the file store with
    no anchor configured is reported in `/api/health` as `anchor: unconfigured`.

11. **Context snapshots are namespaced data; clones are host cache.** The org-owned
    context lake is external; what Fagan holds are the digest-pinned snapshots
    that stories reference (ADR 0005). Those are immutable, content-addressed
    blobs keyed by `(namespace, digest)` with `put` and `get` only, so any worker can
    fetch what a story was pinned to. Git working copies under `context/git/` are
    rebuildable and stay in `HostState`.

12. **`WorkspaceReader` for worker-side artifacts.** The agent log, review log,
    scratchpad, plan checklist and diffs live in a story's worktree, on the host
    that ran it. In the single-host release a local reader serves them to the
    dashboard exactly as today, with the existing containment checks. Once workers
    are remote (ADR 0008 stage 3) the control plane cannot read a worker's disk:
    workers publish bounded tails and the progress checklist as journal records,
    and the dashboard reads those. Diffs come from git on the forge.

## Consequences

- **Cost: a wide migration.** About 50 modules and 316 test files touch
  `PLAN_DIR`. Many existing tests assert paths or file shapes and will need
  explicit approval to change; each stage's brief must name them. The allowlist
  guard keeps unfinished stages visible instead of silently partial.
- **Cost: the approval digest moves.** Plan approval binds to a content digest
  (ADR 0007). Today the digest is of the plan document; with the manifest as the
  only record it must be computed from the manifest's plan-defining fields
  (stories, briefs, files, dependencies, risk). Any `patch_story` still resets
  approval to `pending_approval`. The exact field set must be fixed by a test
  before stage B touches ingest.
- **Cost: re-ingest merges into the manifest.** Without a stored plan file,
  re-ingesting an edited plan merges by explicit story key (already required
  today to avoid duplicates).
- **Benefit: pluggable storage becomes real.** A Postgres store can be
  implemented without a filesystem, which is the precondition for stages 2b and
  3 of ADR 0008 and for leases in ADR 0011 S2.
- **Benefit: the namespace costs almost nothing now.** One constant value in
  core; an enterprise distribution can supply real ones later by changing values,
  not signatures, and core stays free of tenant behavior.
- **Risk: a contract suite that only `FileStore` passes proves nothing.** The
  suite must be written against the Protocol and run on every implementation,
  including the plugin one.

### Alternatives considered

- **Keep the paths and add a database mirror.** Rejected: it is the double
  storage this record removes, and ADR 0008 already forbids write-through.
- **A database per namespace.** Rejected for now: it gives hard isolation we do not
  need under one-org-per-deployment, at the cost of operating many databases.
- **Row-level policy inside the store.** Rejected: the store has no principal.
  Policy stays in the service (ADR 0009); the store only partitions.
- **No namespace in core; the enterprise store infers it from the current
  principal.** Rejected: the scheduler and system principals have no request
  principal and iterate across plans, leases and the queue need the key in their
  rows anyway, and a lookup bug would become a cross-namespace leak that an
  explicit argument prevents by construction.
- **Do the database store first and fix the contract later.** Rejected: that is
  how `SqliteStore` ended with one table and a file store behind it.

## Open questions

Resolved 2026-11-04 by the maintainer: the manifest is the single plan record
(decision 5); events are transport and the broker owns their persistence
(decision 9); the context lake is org-owned, so digest-pinned snapshots are
namespaced data and clones are host cache (decision 11).

1. **Audit anchor default (agreed 2026-11-04): warn-only.** A non-file store
   with no anchor configured is reported as `anchor: unconfigured` in
   `/api/health` and logged at WARNING; refusing to start is an opt-in setting,
   not the default, so a first deployment without a sink still starts.
2. **Remote-worker artifacts (approach agreed 2026-11-04).** Workers publish
   bounded log tails and the progress checklist as journal records. Still open:
   the size bound on a tail, and whether workers push on a timer or at
   checkpoints.
3. **Dashboard direct file reads.** `dashboard.py` reads `.scheduler_health.json`
   and `.dashboard_ui_state.json` (host-local, stay on `HostState`); the story
   routes read worktree files through `Store.get_worktree_file` (move to
   `WorkspaceReader`). Each route needs a per-route check in stage B.
