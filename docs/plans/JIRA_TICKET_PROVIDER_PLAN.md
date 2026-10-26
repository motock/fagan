# JIRA_TICKET_PROVIDER_PLAN

> Implement `JiraTicketProvider` for real, replacing the documented stub in
> `pipeline/ticketing.py:300`. This is the follow-up named in
> `TICKETING_ABSTRACTION_PLAN.md` ("Implement `JiraTicketProvider` for real
> once a Jira instance + API token is available to validate against").
>
> **Status (2026-10-21): persisted, NOT ingested.** At ingest time this goes
> through the normal authoring gate: resolve the implementer tier, apply
> `.claude/rules/local-dispatch-preflight.md` (impact run — see Pre-flight
> notes below), then `save_plan`/`ingest_plan`.
>
> **Status (verified 2026-10-25):** still blocked, still accurate. The stub is
> still at `pipeline/ticketing.py:300` (`class JiraTicketProvider`), and no plan
> in the pipeline has touched it.
> **Precondition that cannot be stubbed away:** an actual Jira Cloud instance
> + API token to validate against. The contract tests (S4) run HTTP-mocked,
> but the Definition of Done requires one live smoke against the real
> instance before the story chain closes (per the "validate any gate against
> the real environment" rule and the API-contract-testing standard — a green
> mocked suite proves branch logic, not that Atlassian accepts our bodies).

## API ground truth (verified 2026-10-21 against the live v3 OpenAPI spec)

Primary source: `developer.atlassian.com/cloud/jira/platform/swagger-v3.v3.json`
(queries against a local copy during research), plus the Atlassian changelog.
**Re-verify dated deprecations at implementation time** — Atlassian moves fast.

- **Auth** — `Authorization: Basic base64(email + ":" + api_token)`; v3
  strictly. Password basic auth long dead; tokens at
  `id.atlassian.com/manage/api-tokens`. New tokens default to 1-year expiry
  (2024-12-15 change). Scoped tokens announced 2025-04-15. Jira sends no
  auth challenge, so send the header proactively. OAuth 2.0 (3LO) is
  Atlassian's recommendation for real apps but is out of scope here.
- **Create issue** — `POST /rest/api/3/issue`, body `{"fields": {...}}`:
  `project` accepts `{"key": "PROJ"}` or `{"id": "…"}`; `issuetype` accepts
  `{"name": "Story"}` or `{"id": "…"}`. Guidance: accept names in config,
  resolve + log the resolved id once (names are project-scoped display
  strings). Response 201 `{"id", "key", "self"}`.
- **Description** — ADF strictly required on v3 (plain string → 400).
  Minimal valid ADF: `{"type": "doc", "version": 1, "content":
  [{"type": "paragraph", "content": [{"type": "text", "text": "…"}]}]}`.
  We ship a tiny text→ADF encoder (split on blank lines); no markdown.
- **Transitions** — workflow-specific; ids AND names are not stable across
  projects, only among transitions valid from the issue's current status.
  Correct pattern: `GET /rest/api/3/issue/{key}/transitions`, match by
  `name` (case-insensitive) or destination `to.statusCategory.key`
  (`new`/`indeterminate`/`done` — the one workflow-independent anchor),
  then `POST` `{"transition": {"id": "…"}}` → 204. **Never hard-code
  transition ids; treat names as config.**
- **Key resolution** — `GET /rest/api/3/issue/{key}` takes the human key
  (`PROJ-123`) directly, as does every issue-scoped endpoint. **Do NOT use
  search**: the classic `GET/POST /rest/api/3/search` was deprecated
  2025-05-01 and fully removed 2025-10 (410 Gone, CHANGE-2046). Replacement
  is `/rest/api/3/search/jql` (token pagination, `nextPageToken`; `startAt`
  and `total` gone) — only needed if we ever list epic children, which the
  `TicketProvider` interface does not require.
- **Epic linkage** — the `Epic Link`/`Parent Link` custom fields are fully
  removed (Epic Link removal final 2025-09-13). Story-inside-epic is just
  `"fields": {"parent": {"key": "PROJ-10"}}` on issue create.

## Design decisions (vs. the Plane provider's shape)

1. **Env-var block mirrors the Plane one**, fail-off by default:
   `JIRA_BASE` (e.g. `https://acme.atlassian.net`), `JIRA_EMAIL`,
   `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`. `_jira_enabled()` = all four set.
   `PIPELINE_TICKET_PROVIDER=jira` forced with any missing raises the same
   clear `ValueError` shape `plane` does today.
2. **`jira_request` helper** mirroring `plane_request` (thin httpx wrapper,
   30 s timeout, raise RuntimeError with status + body on non-2xx), plus:
   proactive Basic header; on 429 honor `Retry-After` then exponential
   backoff with jitter (cap ~30 s, ≤4 retries) inside the PLANE_MAX_ATTEMPTS
   budget. Parse `ErrorCollection` (`errorMessages` + `errors` map) into the
   RuntimeError message; defensive fallback to raw body.
3. **Never-raise contract preserved exactly.** Jira sync, like Plane sync, is
   a side effect of actions already done in git — `set_state` retries within
   budget, then records the drop via `_notify_user(plan_name)` / print and
   returns `False`; transport failure mirrors `_mark_plane_unreachable` with
   a `_jira_reachable` process-level silencer (a down Jira otherwise burns
   the retry budget on every scheduled tick). `create_epic`/`create_story`
   return `None` with a warning on any failure — ingest falls back to a
   synthetic local key, never blocked by a ticketing outage.
4. **Epic link in the create body, NOT a second call.** Plane needs two
   calls (epics are an optional module there); Jira's `parent` is core and
   settable at create, which is atomic and avoids Plane's orphan-on-link-
   failure hazard. The plan doc said "second call after create" for Plane —
   that rationale does not transfer.
5. **`resolve_key` is nearly free for Jira**: issue-scoped endpoints accept
   the human key in the path. Return `story_key` unchanged (validate the
   `PROJ-123` shape, pass through otherwise like the Plane fallback). No
   lookup call, no search endpoint, no cache.
6. **Transition names are config, not constants.** Map
   `LogicalState.BACKLOG|IN_PROGRESS|DONE` first by
   `to.statusCategory.key` (`new`/`indeterminate`/`done` — always present),
   then fall back to optional `JIRA_TRANSITION_*` name overrides
   (case-insensitive). If nothing matches, warn + return `False` — the
   manifest stays source of truth; never guess a transition id.
7. **Labels** — Jira labels are plain strings (`"labels": ["x"]`), no
   get-or-create. Simpler than Plane's label-UUID dance.

## Stories (Claude-class default; re-resolve tier at ingest)

Sizing notes up front (from `agent-dispatch-story-sizing.md`):
`pipeline/ticketing.py` is ~490 lines — under the 1000-line cap, mid-file
anchored edits OK on the weakest tier. `REFERENCE.md` is ~1330 lines — **S5
is not a weak-tier story**; route it to Claude/cloud open-source. S3 and S4
each carry fixture-heavy tests; keep them as separate stories as sequenced.

**S1 — Jira config + request client.**
Add the four `JIRA_*` env vars, `_jira_enabled()`, `jira_request()`
(Basic auth, ErrorCollection parsing, 429/`Retry-After` backoff), and the
`_jira_reachable` transport silencer mirroring `_mark_plane_unreachable`.
Tests (httpx-mocked `httpx.request`): header is sent proactively; successful
call returns parsed JSON and empty-on-204/empty-body; non-2xx raises
RuntimeError carrying status and ErrorCollection message; 429 honors
`Retry-After` and retries within budget then gives up; transport error
flips the silencer and subsequent calls short-circuit.

**S2 — ADF encoder.**
`_to_adf(text: str) -> dict`: blank-line-split paragraphs of plain text into
a version-1 doc. Tests: single paragraph; multi-paragraph; empty string
(empty `content` list is valid — no crash, no zero-paragraph doc node);
whitespace-only lines do not produce empty paragraph nodes.

**S3 — Real `JiraTicketProvider` (replaces the stub).**
Same four methods and contracts. `create_epic`: POST `/rest/api/3/issue`,
issuetype name `Epic`, returns the issue `key`. `create_story`: issuetype
name `Story` (issue-type names configurable via
`JIRA_ISSUETYPE_EPIC`/`JIRA_ISSUETYPE_STORY` with those defaults), ADF
description, `parent` set from `epic_id` when given, `labels: [label]`,
returns `key`. `set_state`: GET transitions → statusCategory match → name
override → POST transition; exact never-raise/retry/notify parity with
`_plane_set_state`. `resolve_key`: pass-through after `PROJECT-\d+` shape
check.
**Existing-test conflict, pre-authorized here** (CLAUDE.md Step 4 — the
behavior change this plan calls for): the two stub-pinning tests in
`tests/unit/test_pipeline_mcp_server_decisions_and_dispatch.py` —
`test_get_ticket_provider_jira_returns_stub` (line ~445) and
`test_jira_ticket_provider_every_op_raises_not_implemented` (line ~562) —
must be replaced: the first asserts `isinstance` only (keep, drop "stub"
language); the second is replaced by mocked happy-path tests per method.
Justification: the stub's raising behavior was temporary by design
(TICKETING_ABSTRACTION_PLAN S5), and the replacement must still grade the
deliverable — absence of `NotImplementedError` alone would pass as a no-op,
so each method gets a real asserted behavior test, not just a deletion.

**S4 — Contract tests against the documented API.**
Per the API-contract-testing standard, the spec is the source of truth,
not our implementation: create body has `fields.project.key` +
`fields.issuetype.name` + ADF description (400 on plain-string description
is asserted as the negative shape); transition POST body is exactly
`{"transition": {"id": …}}`; transition with zero matching transitions
returns `False` and does not POST; 400/422 from Jira surface status +
ErrorCollection text in the raised RuntimeError; per-issue write burst is
bounded by the retry config. Plus the real-environment gate once (live
instance): issue create + one round-trip transition, measured against the
documented responses — without it, the plan is not done.

**S5 — Docs.** REFERENCE.md provider table row (`jira`: stub → working),
env-var table gains the four `JIRA_*` rows (+ optional issuetype/transition
overrides), README env section if it lists PLANE_*. **Weak-tier excluded
(REFERENCE.md ≈1330 lines); Claude/cloud tier.** Comments in S1–S4 stay
timeless invariants (no "today the stub raises" text that rots after this
merges — see local-dispatch-preflight §2).

## Pre-flight notes (mandatory at ingest, before any dispatch)

- **Impact run first** (local-dispatch-preflight §1): apply a stand-in of
  S3 in a scratch worktree and run the full suite. Known conflicts are the
  two stub tests above (pre-authorized in S3), but run the suite — grep
  said only one file mentions Jira in tests; the run is what proves it.
- Stories S3+S4 are one PR-ish concern each; do not merge them — the
  interface change and its contract tests are separable (S3's own unit
  tests land in S3).
- Every story needs an explicit `key` at ingest (plan authoring trap —
  re-ingest without keys mints duplicates).
- `.pipeline.env` wins over inherited env on a dispatch; check whether the
  dispatch environment even sees `JIRA_*` before assuming a jira-enabled
  run is testable from a dispatched agent.

## Back-compat & risk

- `auto` and `plane` behavior is bit-identical; the only behavior change is
  what `jira` selects. Selecting `jira` previously raised on first call;
  after S3 it works — that IS the feature.
- Risk: Atlassian deprecations between this doc (2026-10-21) and
  implementation. Mitigate by re-checking CHANGE-2046-class notices and the
  transitions/ADF sections of the spec before S1; the plan's request shapes
  all came from the live spec, not the rendered docs.

## Done bar for the whole plan

Suite green; both pre-authorized test replacements in place and justified in
the commit message; `PIPELINE_TICKET_PROVIDER=jira` end-to-end smoke on a
real instance (create epic, create story in epic, backlog → in progress →
done) with `PLANE_*` unset; docs updated; each story through
`review_story`/`approve_merge`.
