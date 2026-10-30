# Scope-gate follow-ups

- **Status:** authored 2026-10-30, **not ingested and not preflighted**. Every
  brief still owes the `local-dispatch-preflight.md` impact run before
  `ingest_plan`, so ingest will (correctly) reject them as written. The plan
  file is [`SCOPE_GATE_FOLLOWUPS_PLAN.json`](SCOPE_GATE_FOLLOWUPS_PLAN.json).
- **Why now, and why not yet:** three independent fixes to the failure class
  that consumed the first `standalone-server` dispatch round. They are held
  until that plan finishes so they do not compete with it for the two agent
  slots, and so the impact run is not run against a host already saturated by
  two concurrent test suites (the contention that made the first round look
  like a hang).
- **Dispatch tier:** the default, `ollama / deepseek-v4.1-flash:cloud` via
  `roles.dispatch`. No `backend` pin.

## The failure class

A story whose branch changes a production path outside its declared `files`
is sent back by `pipeline/scope_gate.py` before any LLM review. That is
correct for drift, but when the *plan author* under-declared the scope it is
unwinnable: `check_scope_amendment` refuses any path whose directory is not
already declared, so no agent action can ever widen it. On `standalone-server`
five stories burned 3-5 rework cycles each this way, and the resulting park
reason ("triage attempts at cap") reads as an agent failure.

## Stories

| Key | Summary | Files | Risk |
|---|---|---|---|
| SF-01 | Park a scope-gate miss as a plan defect instead of spending the rework budget | `pipeline/scope_gate_park.py` (new), `pipeline/review_orchestrator.py` | high |
| SF-02 | Warn at ingest when a brief names a production path the scope omits | `pipeline/scope_headroom.py`, `pipeline/ingest.py` | low |
| SF-04 | Gate dispatch on the backend the stories will actually use, not on Claude's usage | `pipeline/usage.py`, `pipeline/advance.py`, `pipeline/stall_scan.py` | high |

No dependencies between them; they touch disjoint files. There was a fourth
story, SF-03, for the `PIPELINE_ANTHROPIC_API_KEY` entry in
`pipeline/execution.py`. It is **withdrawn**: on 2026-10-30 the operator chose
to close that exposure inside `standalone-server`'s SA-09 instead, so the
deploying story never merges while the runtime still mounts credentials it
does not strip. SA-09 now owns both halves — the child-env scrubbing in
`app/backend_claude.py` and the `_AGENT_SECRET_ENV` entries — and lists
`pipeline/execution.py` in its `files`.

SF-04 was added the same day, after the standalone plan froze for 40 minutes
with three dispatch-eligible stories: `_role_resource_ok("dispatch")` defaults
to the `claude` backend, so a tripped Claude usage gate set
`advance.py`'s plan-wide `dispatch_ok=False`, which skips the whole dispatch
loop and interrupts in-progress stories — while every story in that plan routes
to `ollama` and `ollama`'s own gate was green. The operator workaround was
pinning `PIPELINE_BACKEND_DISPATCH=ollama` and restarting the daemon; the story
removes the need for that.

SF-01 parks immediately when the amendment guardrail refuses every path, and
on the second identical violation otherwise, so a legitimate one-off drift
edit still gets its single cheap revert. SF-02 is advisory only and must never
fail an ingest.

## Preflight still owed (before ingest)

1. **Impact run per story** (`local-dispatch-preflight.md` §1): apply a rough
   stand-in in a scratch worktree and run the full suite plus `ruff check .`.
   Highest-risk shapes to watch: any test that pins `review_story`'s
   `scope_gate_failed` notification text or the exact `SCOPE GATE:` feedback
   string (SF-01 keeps both character-identical by design), a test that pins
   `scope_headroom_warning`'s call site in `ingest.py` (SF-02), and a test that
   asserts `_AGENT_SECRET_ENV` by exact equality or count (SF-03).
2. **Line-budget check:** `review_orchestrator.py` is at exactly 1000 lines and
   `ingest.py` at 998, so both briefs require a net-negative or net-+1 change
   there. If the impact run shows either file cannot stay under 1000, split
   the move into its own prerequisite story rather than accepting the overage.
3. **Fixture lint:** these stories ship no `acceptance` fixtures (the tests are
   prescribed in the briefs), so there is nothing to lint — but confirm the
   test-author phase writes them as described and re-run lint on the result.
4. Then add the one-line `Preflight:` summary to each brief and ingest.
