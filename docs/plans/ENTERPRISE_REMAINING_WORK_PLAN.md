# Enterprise readiness — remaining work roadmap

- **Status:** drafted 2026-10-28 on `855cc1e`; every plan preflighted the same
  day on `master` `cd09a83`. Eleven plan files, none ingested. A dry-run ingest
  of all eleven (dispatch provider resolved to a non-Claude tier, so the
  preflight gate was active) accepts every story.
- **Inputs:** [`ENTERPRISE_PLATFORM_READINESS.md`](ENTERPRISE_PLATFORM_READINESS.md)
  (the gap list), [`docs/specs/IDENTITY_AND_POLICY.md`](../specs/IDENTITY_AND_POLICY.md)
  §4 and §6 (what is not implemented, in dependency order),
  [ADR 0006](../adr/0006-decision-routing-on-a-shared-server.md),
  [ADR 0008](../adr/0008-shared-control-plane-per-repo-workers.md) staging, and
  [ADR 0009](../adr/0009-tenancy-and-authorization-model.md).

## Dispatch tier

Every story targets the **cloud open-source** tier (Claude-class sizing, with
every mechanically checkable requirement graded in the brief). No story pins
`backend` or `model`: dispatch resolves them from the live model registry's
`roles.dispatch` entry. The checked-in `model_registry.json` has no `roles`
block, so the registry at `PIPELINE_MODEL_REGISTRY_PATH` on the dispatch host
must route `dispatch` to the cloud model. Otherwise ingest resolves the
provider to `claude`, which skips the preflight gate.

## The plans, in order

| # | File | Plan name to ingest as | Stories | Preflight | Blocked by |
|---|---|---|---|---|---|
| 1 | [`ENTERPRISE_ENFORCEMENT_CLOSEOUT_PLAN.json`](ENTERPRISE_ENFORCEMENT_CLOSEOUT_PLAN.json) | `enterprise-enforcement-closeout` | ENF-00..03 | Done; 1 conflict, pre-authorized | — |
| 2 | [`DECISION_ROUTING_PLAN.json`](DECISION_ROUTING_PLAN.json) | `decision-routing` | RT-01..03 | Done, stacked on plan 1; 2 pre-authorized | ENF-03 |
| 3 | [`SYSTEM_PRINCIPALS_PLAN.json`](SYSTEM_PRINCIPALS_PLAN.json) | `system-principals` | SYS-01..02 | Done, stacked on plan 1; 2 re-scoped | ENF-03 |
| 4 | [`AUDIT_ANCHOR_PLAN.json`](AUDIT_ANCHOR_PLAN.json) | `audit-anchor` | AA-01..02 | Done; 1 pre-authorized, 1 re-scoped | — |
| 5 | [`REMOTE_MCP_HARDENING_PLAN.json`](REMOTE_MCP_HARDENING_PLAN.json) | `remote-mcp-hardening-2` (the name `remote-mcp-hardening` is taken by a completed plan) | RMH-01..02 | Done; 0 conflicts | — |
| 6 | [`DASHBOARD_OIDC_LOGIN_PLAN.json`](DASHBOARD_OIDC_LOGIN_PLAN.json) | `dashboard-oidc-login` | LG-01..03 | Done; 0 conflicts | — |
| 7 | [`OBSERVABILITY_OTEL_PLAN.json`](OBSERVABILITY_OTEL_PLAN.json) | `observability-otel` | OT-01..02 | Done; 0 conflicts | — |
| 8 | [`EVENTING_BROKER_PREP_PLAN.json`](EVENTING_BROKER_PREP_PLAN.json) | `eventing-broker-prep` | EV-01..02 | Done; 2 re-scoped | — |
| 8b | [`EVENTING_BROKER_ADAPTER_PLAN.json`](EVENTING_BROKER_ADAPTER_PLAN.json) | `eventing-broker-adapter` | BR-01..05 | Done, stacked on plan 8; 0 conflicts | EV-02; implements [ADR 0010](../adr/0010-event-delivery-contract-and-nats-jetstream.md) (Proposed) |
| 9 | [`CONTROL_PLANE_STORE_PLAN.json`](CONTROL_PLANE_STORE_PLAN.json) | `control-plane-store` | ST-01..03 | Done; 0 conflicts | ADR 0008 accepted 2026-10-28; ST-01 waits on ENF-03 |
| 10 | [`SMOKE_STACK_PLAN.json`](SMOKE_STACK_PLAN.json) | `smoke-stack` | DK-01..04 | Done; 0 conflicts (no Docker daemon here) | AA-01, RMH-02, LG-02, BR-04 (on DK-03) |

`blocked_by` entries use the plan names in the third column, so ingest each
plan under exactly that name. Plans 1–3 finish ADR 0008 stage 1. Plans 4–6
harden what Phase 6 shipped. Plans 7, 8 and 8b are observability, broker prep and the NATS JetStream adapter.
Plan 9 is ADR 0008 stage 2a (single-host database `Store`; 2b is Postgres
with distributed locks, adopted with stage 3). Plan 10 is the deployable smoke stack. Within a plan, the
stories are chained by `dependencies` because most of them edit the same files.

Every story is `risk: high` (security-engineer pass, held for human review)
except the behavior-preserving or non-auth ones, which are marked `low` or
`medium` in the JSON.

### Preflight results (2026-10-28, `master` `cd09a83`)

Each plan's stand-in (a rough version of all its stories) ran against the full
suite, `ruff check .` and `scripts/check_line_limit.py` in a scratch worktree.
Plans 2–3 ran stacked on plan 1's stand-in, and plan 8b on plan 8's, because
they build on those changes. Every conflict was resolved one of two ways:
- **Pre-authorized:** the brief names the file, the test and the literal edit.
- **Re-scoped:** the story was changed so it no longer collides.

| Conflict | Story | Resolution |
|---|---|---|
| `test_service_active_workspace::test_save_plan_unchanged` (sha256 pin of `save_plan`) | ENF-02 | Pre-authorized: delete the pin and its baseline entry |
| `test_lifecycle_tool_registration::test_tools_tuple_has_nine_entries_in_original_order` (exact `TOOLS` list) | AA-01 | Pre-authorized: append `verify_audit` (the precedent is the plan-approval tools) |
| `test_readme_reference_split` (byte-compared MCP tools reference) | AA-01 | Re-scoped: document `verify_audit` in REFERENCE.md's Audit stream section |
| `test_topology_identity` ×2 (exact topology keys) | EV-02 (and ST-01, BR-05) | Re-scoped: a separate, cumulative `runtime` block in `/api/health` |
| `test_notification_routing` (pins `match.owner` as unsupported) | RT-03 | Pre-authorized: drop that one case; keep the "not supported yet" wording |
| `test_oa2_request_decision_failopen::test_success_path_return_and_record_unchanged` | RT-02 | Pre-authorized: add `requested_by` and `routed_to` to the expected record |
| `test_scheduler_daemon` (`reconcile_fn is advance_all_plans`), `test_scheduler_daemon_scan_watchdog` (stdlib/`pipeline`-only imports) | SYS-02 | Re-scoped: bind the principal inside the watchdog worker thread; import it through `pipeline.actor` |

The runs also changed several briefs:
- **`master` moved during drafting** (refusals gained `forbidden`/`action`,
  plus `app/dashboard_refusal.py`). Plan 1 was re-run on the new base, and its
  briefs now add `raise_if_forbidden` to the six dashboard routes they gate.
- **Rate-limit keys (RMH-01):** limits key through an explicit trusted-proxy
  list. Behind a reverse proxy the raw peer is the proxy, so one client's failed
  logins would otherwise lock out everyone.
- **Read scoping (RMH-02):** an invisible plan is hidden exactly like a missing
  one, so a caller can't probe which plans exist.
- **Scheduler grants (SYS-01):** a static pass found what the tick reaches
  (`plan:dispatch`, `story:merge`, `story:mark_done`, `story:interrupt`). The
  scheduler is granted only those.
- **Small cleanups:** ENF-00 removes an import that becomes unused, ST-01
  declares `pipeline/unpark.py`, and SqliteStore delegates its
  filesystem-native reads to `FileStore`.

**What these runs do not prove:**
- The OpenTelemetry packages, `nats-py` and `nats-server` were not installed.
  The planning container has the Docker client but no daemon.
- Those paths are graded only by the stories' own tests: the NATS integration
  tests, which CI is required to run, and DK-04's CI smoke job.

The planning container also fails four tests on untouched `master` for
environmental reasons:
- `test_aider_harness_availability` ×2: the proxy injects `GH_TOKEN`.
- `test_docs_have_no_personal_home_path`: the container `HOME` is the root user's home, which the guard bans by value.
- `test_remote_sync`: there is no `ssh` binary.

They pass on a normal host and are not conflicts.

**If `master` moves before a plan is ingested,** re-run its impact check:
`.claude/rules/local-dispatch-preflight.md` §1, then update the plan's
`Preflight:` lines. A result is only as current as the commit it names.

## Decisions made while drafting (override any of them before ingest)

1. **No new `Action` values.** ADR 0009 §5 maps actions one-to-one onto the
   existing enum, so the remaining tools map onto it:
   - `story:patch`: `set_story_status`, `checkpoint`, `request_decision`.
   - `plan:dispatch`: `mark_story_in_progress`, `review_story`,
     `advance_pipeline`.
   - `plan:ingest`: `save_plan`.
   - host-scoped `config:write`: `record_security_audit`.
2. **`decompose_plan` stays ungated.** It writes no state, and two structural
   tests pin its body. Its model-spend exposure is a rate-limiting concern
   (plan 5), and the remote transport already refuses it.
3. **`authorize(..., record_allowed=False)`** for the two high-frequency
   actions, `checkpoint` and the per-tick `advance_pipeline`. Their denials are
   always recorded, but their allowed decisions are not, because otherwise the
   advance tick alone would write about 1,440 records per plan per day. This
   narrows ADR 0009 §8 ("every allow and deny records") for those two actions.
   If you would rather keep the ADR literal, the alternative is a deduplicated
   allowed record (one per principal, action and plan per hour).
4. **The `save_plan` digest pin is deleted, not re-pinned.** A new hash would
   break the next legitimate edit the same way (the cumulative-artifact rule).
5. **System principals never get ownership grants.** A manifest naming
   `system:scheduler` as owner or delegate still denies it approve and reject.
6. **SQLite comes before Postgres** for stage 2. It is enough for the one-repo
   server mode. Postgres is needed only when workers run on separate hosts
   (stage 3).
7. **The newly gated tools stay off the remote MCP allowlist.** Widening the
   remote surface is a separate decision.
8. **NATS JetStream is the default broker (ADR 0010, Proposed).** Delivery is
   at-least-once with idempotent handlers, bounded redelivery and a dead-letter
   subject; there is no ordering guarantee. The scheduler daemon is the only
   consumer, and the in-process bus stays the default and stays best-effort.
   Kafka was considered; it fits a high-volume event-streaming estate, not this
   pipeline's thousands of events per day.

## Open questions with no plan yet

- **Worker identity (would be SYS-03).** How does a dispatched agent's MCP
  process prove it is the worker for story X, without trusting an environment
  variable the agent could change? Until this is designed, workers act as the
  local operator.
- **ADR 0006 item 4 (unanswered decisions park the story after a bounded
  time).** First diagnose what today's code does with an overlord ruling that
  escalates to a human. `_request_decision_impl` parks only on the fail-open
  path.
- **ADR 0008 stages 3–4:** a work queue, worker registration and heartbeats,
  generic workers, and federation. Not drafted, because they depend on stage 2
  and on the worker-identity design.
- **Per-tenant naming:** plan and story keys per owner or tenant (ADR 0009 §4),
  and namespaced branches and worktrees by default on a shared server.
- **Moving the sidecars into the database:** notifications, audit, journals and
  decisions are still files under `PLAN_DIR` even with `SqliteStore`.
- **A Kafka adapter.** ADR 0010 makes NATS JetStream the default and keeps the
  contract broker-neutral, so Kafka can be a second implementation that passes
  the same contract suite. It is not drafted until someone needs to publish
  into an existing Kafka estate.
- **An HTTP/S3 object-lock audit anchor sink, and a shared rate limiter for
  more than one replica.** Plans 4 and 5 ship single-host versions.

## Is a docker-compose smoke stack the right next step?

Yes, as the capstone. It is the right way to let anyone stand the system up and
test it end to end, and building it in CI and publishing the image as an
artifact is the correct home for it. Plan 10 drafts exactly that. But several
pieces must exist first, or the containers would show nothing:

| Proposed container | Is there code for it to talk to today? | What closes the gap |
|---|---|---|
| Pipeline image | Partly. The root `Dockerfile` is the stdio MCP-directory image, with no dashboard, no HTTP transport and a root user. | DK-01 adds `deploy/Dockerfile`. |
| IdP (Keycloak) | API and MCP clients: yes (OIDC bearer). Browser: **no**, the dashboard has no login flow, so every `/api/` call from the UI is a 401. | Plan 6 (LG-01..03). |
| TLS | No. The remote MCP transport has no TLS of its own. | A Caddy proxy in DK-03. |
| Lightweight DB | **No.** `FileStore` is the only `Store`. A database container would sit unused. | Plan 9 (SQLite needs no container; Postgres waits on stage 3). |
| Message broker | **No.** Only the in-process and JSONL buses exist, and no broker adapter. | Plans 8 and 8b (NATS JetStream, ADR 0010); DK-03 adds a `nats` service and asserts delivery survives a restart. |
| OTel collector and Grafana | **No** traces or metrics exist. JSON logs (`PIPELINE_LOG_FORMAT=json`) could already feed Loki. | Plan 7. |
| A model to run against | CI cannot call Claude or a local model. | DK-02, a scripted OpenAI-compatible stub. |
| A forge for PRs and merges | `pipeline/forge.py` knows only GitHub and GitLab. | The smoke run stops at "reviewed, merge parked". A real forge, a GitLab container or a new adapter is a separate decision. |
| Authorization worth smoke-testing | Mostly. Some write tools are unchecked, reads are not tenant-scoped, and there is no rate limiting. | Plans 1, 5. |

So the order is plans 1–9 (including 8b), then plan 10. Adding the database, broker and
Grafana containers only once their adapters exist also follows ADR 0004's
warning: a container nothing uses gives a smoke test that passes while the
feature is absent.
