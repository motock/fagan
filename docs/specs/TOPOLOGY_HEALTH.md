# Resolved Topology in the Health Endpoint

- **Status:** Implemented (dashboard half); scheduler half deferred
- **Story:** ENT-PREP-03
- **Audience:** Operators and on-call engineers reading `/api/health`, and
  contributors adding health fields.

## 0. Summary

When two pipeline processes disagree about *where they are*, every other
health signal is misleading: a scheduler writing fingerprints into one plan
dir while the dashboard reads another looks healthy from both sides. The
dashboard's `/api/health` therefore reports the process's **resolved
topology** — the plan dir, target repo root, and branch/worktree namespaces
this process is actually operating on.

The change is **additive and behavior-preserving**: no existing health key
changes meaning, and the new data appears in two additive observational blocks,
`topology` and `runtime`.

## 1. The four fields

`pipeline.topology_identity.resolved_topology()` returns a dict with exactly
these four keys:

| Key | Source | Type |
| --- | --- | --- |
| `plan_dir` | `str(pipeline.paths.PLAN_DIR)` | `str` |
| `repo_root` | `str(pipeline.server.REPO_ROOT)` — the active target repo root | `str` |
| `branch_namespace` | `os.environ.get(pipeline.branch_names.NAMESPACE_ENV)` | `str \| None` |
| `worktree_namespace` | `os.environ.get("PIPELINE_WORKTREE_NAMESPACE")` | `str \| None` |

Rules:

- **Read at call time.** `pipeline.paths.PLAN_DIR` and
  `pipeline.server.REPO_ROOT` are read as module attributes on every call,
  never snapshotted at import. Long-lived processes and tests re-patch these
  bindings; a snapshot would report a stale topology.
- **Lazy import.** `pipeline.server.REPO_ROOT` is imported *inside* the
  function. `pipeline.server` has a broad dependency graph and importing it
  at module scope from a small identity module would create an import cycle
  for callers such as `app/dashboard.py`.
- **Empty means unset.** An empty-string env value reports `None`, matching
  the pipeline's own "no namespace configured" default of `""`. Reporting
  `""` would make "unset" indistinguishable from "set to nothing".

## 2. The `/api/health` surface

`GET /api/health` gains two additive, observational top-level keys:
`topology` (this story) and `runtime` (the cumulative block of runtime
selections). No pre-existing key changes meaning.

```json
{
  "ok": true,
  "plan_dir": "~/pipeline/plans",
  "scheduler": {"plan_dir": "~/pipeline/plans", "worktree_root": "~/pipeline/worktrees"},
  "config_mismatch": [],
  "topology": {
    "plan_dir": "~/pipeline/plans",
    "repo_root": "~/src/target-repo",
    "branch_namespace": "team-a",
    "worktree_namespace": null
  },
  "runtime": {
    "event_bus": "inprocess"
  }
}
```

- `topology` — the four fields of §1, read live per request by
  `pipeline.topology_identity.resolved_topology()`.
- `runtime` — returned exactly as produced by
  `pipeline.topology_identity.resolved_runtime()`, so it is **cumulative**:
  each runtime selection (event bus, store, broker status, …) adds its own key
  there, and `health()` never rebuilds the literal, which would silently drop
  keys added by other work.
  - `event_bus` — the bus `PIPELINE_EVENT_BUS` selects (`inprocess` or
    `jsonl`), or the marker `invalid` when the variable holds an unsupported
    value. The rejected value is never echoed: it may be a path or a secret.

Unchanged:

- `ok` — always `true` on a 200.
- `plan_dir` — the resolved canonical plan dir, same meaning as before.
- `scheduler` — the config fingerprint from
  `<plan_dir>/.scheduler_health.json`, or `null`.
- `config_mismatch` — dashboard-vs-fingerprint field diffs, or `[]`.

The fail-soft fingerprint logic is untouched: any problem reading or parsing
`.scheduler_health.json` still degrades to `scheduler=null` /
`config_mismatch=[]` with HTTP 200. `topology` is computed independently of
the fingerprint and cannot turn a health check into a 500.

`runtime` is fail-soft for the same reason: reporting an unsupported
`PIPELINE_EVENT_BUS` as the marker `invalid` (via
`pipeline.event_wiring.reported_event_bus_name()`) keeps `/api/health` at HTTP
200, because a health check must never 500 — `scripts/standalone-setup.sh`
polls it. **Failing closed still applies where it matters:** `build_bus()`
raises `ValueError` for the same unsupported value, so the pipeline refuses to
run with a selection it cannot honour. Only the observation degrades; the
rejected value is never echoed.

## 3. Values are observational, not validated

The namespace fields are reported **raw**. `resolved_topology()` does not
apply the branch-name validator, does not strip whitespace, and does not
normalise case. A value the validator would reject is still reported
verbatim, because the purpose is to show what the process sees — not to gate
it. Validation belongs at the boundary where a namespace is *used* (branch
naming), not in an observational health read.

## 4. Deferred: the scheduler health-file half

The original story also surfaced the resolved topology in the scheduler's
health file (`.scheduler_health.json`), so the fingerprint would carry the
scheduler's own topology. **That half is deferred to a follow-up story.**

Reason: `pipeline/scheduler_daemon.py` is at the hard 999-line gate enforced
by `scripts/check_line_limit.py` (see `tests/unit/test_check_line_limit.py`,
`tests/unit/test_dashboard_split.py`, and
`tests/unit/test_scheduler_timeouts_extraction.py`). Adding even one line
there fails the gate. The scheduler half lands after `scheduler_daemon.py` is
split or `config_fingerprint` moves to its own module.

Consequently there is no test pinning the scheduler fingerprint's topology;
the dashboard half is verified on its own.

## 5. Tests

- `tests/unit/test_topology_identity.py` — the four fields, call-time reads,
  unset/empty/set namespaces, non-validation, and the `/api/health`
  `topology` key alongside the existing keys.
- `tests/unit/test_event_bus_selector.py` — `PIPELINE_EVENT_BUS` selection
  (`inprocess`, `jsonl`, unset, unsupported value), the jsonl bus persisting
  *and* dispatching to its subscribers, and the `/api/health` `runtime`
  `event_bus` report including the `invalid` marker.
- `tests/unit/test_dashboard_health_plan_dir.py` and
  `tests/unit/test_dashboard_health_config_mismatch.py` — unchanged; they
  assert membership of the pre-existing keys and must keep passing.
