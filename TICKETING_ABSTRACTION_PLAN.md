# Plan: Abstract the ticketing layer (Plane default, pluggable, optional)

> Status: **Implemented 2026-07-09** (S1-S5, executed directly, not through the
> pipeline). `LogicalState`, `TicketProvider`, `NullTicketProvider`,
> `PlaneTicketProvider`, `JiraTicketProvider` (stub), and `get_ticket_provider()`
> now live in `pipeline_mcp_server.py`. All 756 existing + 14 new tests pass
> unchanged. Follow-up: a real `JiraTicketProvider` implementation (see S5/bottom).
>
> **2026-07-11:** "optional" now also covers Plane *configured but temporarily
> unreachable* (connection refused/timeout/any `httpx` transport error), not just
> unconfigured — `PlaneTicketProvider.create_epic`/`create_story` catch any
> failure and return `None` so `ingest_plan` falls back to local/synthetic story
> keys instead of raising, with a `Warning:` print surfacing the failure. See the
> "PlaneTicketProvider.create_epic/create_story must never raise on a Plane
> connection failure" story (3ff4018d-3f82-46ac-97df-30672dccc82a).

## Goal
Replace the Plane-specific calls scattered through `pipeline_mcp_server.py` with a small
`TicketProvider` interface so the pipeline can target Plane (default), Jira, or nothing —
and make "no ticketing backend" a first-class, supported mode rather than an accident of
unset env vars.

## Current state (assessment)
Plane is already isolated and already optional, more than expected:

- **Config** is 5 env vars: `PLANE_BASE` / `PLANE_API_KEY` / `PLANE_WORKSPACE` /
  `PLANE_PROJECT` (`pipeline_mcp_server.py:45-48`) and `PIPELINE_PLANE_MAX_ATTEMPTS` (`:218`).
- **`_plane_enabled()`** (`:240`) already gates *every* Plane call. When off, "the manifest
  is the sole source of truth" — the pipeline already runs fully without a ticketing backend.
- The **logical surface** the pipeline needs is tiny — four operations:
  1. create an epic → id (optional; Plane epics already degrade to `None`)
  2. create a story → id (+ initial state + label)
  3. transition a story's state (backlog → started → completed)
  4. resolve a human key (`PIPE-7`) to a backend id

Wrinkles to fix during the refactor:
- Transition logic is funneled through `_plane_set_state()` (`:1773`) **except two call sites
  that PATCH Plane directly** — `mark_story_in_progress` (`:2647`) and `mark_story_done`
  (`:2712`). Those bypass the retry/never-raise safety net and *will* raise on a Plane outage.
- Plane-specific concepts (state *groups*, label UUIDs, `X-API-Key`, work-item UUID lookup,
  the `POST /epics/{id}/issues/` link call) are smeared across `_get_state` (`:351`),
  `_resolve_issue_uuid` (`:368`), `_get_or_create_label` (`:393`), and `ingest_plan`
  (`:1992-2028`).

## Design

**Selection.** One new env var `PIPELINE_TICKET_PROVIDER` ∈ `{auto, none, plane, jira}`,
default `auto`:
- `auto` → `plane` if `PLANE_API_KEY && PLANE_WORKSPACE && PLANE_PROJECT` are set, else
  `none`. (Preserves today's behavior exactly.)
- `none` → Null provider; manifest is the sole source of truth.
- `plane` / `jira` → force that backend; error clearly if its required vars are missing.

**Interface** (new module `ticketing.py`, or a dedicated section in the server):
```python
class LogicalState(Enum): BACKLOG; IN_PROGRESS; DONE

class TicketProvider(Protocol):
    enabled: bool
    def create_epic(summary: str) -> str | None
    def create_story(summary, description, epic_id, label) -> str
    def set_state(story_key, state: LogicalState, plan_name=None) -> bool   # best-effort, never raises
    def resolve_key(story_key) -> str

def get_ticket_provider() -> TicketProvider   # factory, cached per-process
```

**Invariants the interface must preserve** (current behavior, not new):
- `set_state` **never raises** — retries up to `PIPELINE_PLANE_MAX_ATTEMPTS` (generalize to
  `PIPELINE_TICKET_MAX_ATTEMPTS`, keep the old name as a fallback alias), then records the
  drop via `_notify_user`/print and returns `False`.
- `create_epic` may return `None` (backend without epics); the story is still created ungrouped.
- When `enabled=False`, every method is a no-op that reports success and the manifest drives
  everything.

## Stories (TDD, in order)

**S1 — Interface + Null provider + factory.** Add `TicketProvider`, `LogicalState`,
`NullTicketProvider`, `get_ticket_provider()`. No call sites changed yet.
- Tests: `auto` + no Plane config → Null, `enabled is False`; `PIPELINE_TICKET_PROVIDER=none`
  forces Null even when Plane vars are present; every Null op returns success/no-op;
  `plane`/`jira` forced with missing vars raises a clear config error.

**S2 — `PlaneTicketProvider`.** Move `plane_request`, `_get_state`, `_resolve_issue_uuid`,
`_get_or_create_label`, `_plane_set_state`, `_mark_plane_done`, and the state/label caches
behind it. Map `LogicalState` → Plane state groups
(`BACKLOG→backlog`, `IN_PROGRESS→started`, `DONE→completed`).
- **Also fold in the two direct-PATCH call sites** (`mark_story_in_progress:2647`,
  `mark_story_done:2712`) so *all* transitions go through `provider.set_state`, eliminating
  the bypass.
- Tests: existing Plane-path tests pass unchanged (golden regression); the two former direct
  call sites now honor the retry/never-raise contract; state-group and UUID resolution identical.

**S3 — Repoint orchestration.** Replace inline `_plane_enabled()` + `plane_request` blocks in
`ingest_plan` (`:1992-2028`), `dispatch_story` (`:2219`), and the merge/done paths
(`:3599`, `:3695`) with `provider.*` calls.
- Tests: Plane-on and Plane-off (`none`) `ingest_plan` produce identical manifests to today;
  dispatch/merge transitions fire once through the provider.

**S4 — First-class "optional/off" + docs.** Document `PIPELINE_TICKET_PROVIDER` (and the
`TICKET_MAX_ATTEMPTS` rename+alias). Update the module header env block (`:9-12`), the README
env-var table, and CLAUDE.md so running with no ticketing backend is a named, supported mode.
- Tests: doc/lint only; assert the alias fallback (`PIPELINE_PLANE_MAX_ATTEMPTS` still honored).

**S5 — Jira as a documented extension point (stub, no live impl).** Add `JiraTicketProvider`
raising `NotImplementedError` with a clear message, registered in the factory, plus a comment
spelling out the three things a real impl must handle: **transition IDs**
(`POST /issue/{key}/transitions`, not a `state` PATCH), **auth**
(`Authorization: Basic email:token` or Bearer, not `X-API-Key`), and **ADF description format**.
Leave the contract tests scaffolded and skipped.
- Tests: `PIPELINE_TICKET_PROVIDER=jira` selects the stub; calling it raises the documented
  `NotImplementedError`; the skipped contract-test file exists for the follow-up.

## Back-compat & risk
- Every current Plane env var and existing manifest keeps working untouched; `auto` reproduces
  today's behavior bit-for-bit.
- The "optional" ask already works when Plane is unconfigured — this makes it explicit and
  testable, and removes the two transition call sites that currently bypass the
  retry/never-raise safety net (a latent bug on a Plane outage).

## Follow-up (separate plan)
Implement `JiraTicketProvider` for real once a Jira instance + API token is available to
validate against (contract tests per the API-contract-testing standard).
