# Plan: Route merge-gate CI failures into the rework loop

**Status:** IMPLEMENTED directly (2026-07-17, at user's explicit request to bypass
the pipeline for this one change) and **corrected after live validation**.
`PIPELINE_REWORK_ON_CI_FAIL` ships in `pipeline_mcp_server.py`'s merge gate;
7 tests in `test_pipeline_mcp_server.py` (6 original + 1 regression for the
bug below); README documented. Full repo suite green (1120 passed). Not yet
committed — awaiting user review.

**Bug found + fixed during live validation (2026-07-17, token_bucket cell):**
the first implementation bounded the rework loop with `rework_attempts` — but
the review-APPROVE path (`pipeline_mcp_server.py` ~line 4189) pops
`rework_attempts` on every pass (the acceptance-scoped reviewer APPROVEs
whenever the oracle is green), so the counter reset to 0 each cycle and the
loop never exhausted: four identical `routed to rework (1/3)` notifications
~90s apart, same broken `3.0` vs `9.0` assertion every round. **Fix:** bound
the loop with `merge_attempts` (the merge gate's own counter, NOT reset by
review) instead — `MERGE_MAX_ATTEMPTS` rework rounds, then the existing
terminal-fail fall-through. The new regression test
(`test_advance_pipeline_ci_fail_rework_counter_survives_review_approve`)
simulates the full rework→review-APPROVE-reset→merge cycle across 4 ticks and
asserts the counter advances 1→2→3 then terminal-fails (the test that should
have caught it the first time; the original 6 tested only single mocked
ticks). NOTE: the live run also showed gpt-oss did not fix the specific broken
assertion across rounds (it edited the test file but not the `3.0` expectation)
— a model-behavior gap, separate from the harness bound. The fix guarantees the
loop is BOUNDED; whether a given model converges within the budget is on the
model.

**Origin:** 2026-07-17 gpt-oss production validation matrix. `retry_backoff` and
`token_bucket` both produced ground-truth-correct implementations that the
acceptance oracle passed and the reviewer APPROVE'd, yet ended terminal
`failed` — because each agent also committed a self-contradictory test file,
and the merge-gate CI check (which correctly runs the full worktree suite)
caught it, burned all 3 `merge_attempts` on identical re-runs, then gave up
with no feedback to the implementer.

**This is not an FM-A "grade against the agent's own tests" bug.** The CI gate
is *supposed* to run the full suite — it exists to catch merged-but-wrong
(RLI-3, 2026-07-04), and un-scoping the reviewer to acceptance while the CI
gate runs the full suite is the intended, defensible design. The real gap is
narrower: **a definitive CI failure has no rework path.** The agent is never
told "your test file is broken, fix it," even when one rework round would
obviously resolve it (in `retry_backoff` the correct answer was sitting in the
agent's own comment).

---

## Root cause (verified)

`pipeline_mcp_server.py`, `advance_pipeline` merge gate, ~lines 4673–4716:

```
if not gate_error:
    ci = _ci_status(branch)              # runs full CI / gh pr checks
    ...
    if ci["state"] in ("fail", "cancelled"):
        gate_error = f"ci fail: {ci['error']}"
...
if gate_error:
    attempts = story.get("merge_attempts", 0) + 1
    story["merge_attempts"] = attempts
    if attempts >= MERGE_MAX_ATTEMPTS:
        story["status"] = "failed"       # terminal, no rework, no feedback
        ...
    else:
        # leave pr_open; the next tick retries within budget   <-- identical re-run
```

A CI `fail` and a transient push/network error are funnelled through the *same*
`gate_error` path: N identical retries, then terminal fail. Nothing ever
re-dispatches the implementer with the failure as feedback. Contrast
`PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL`, which *does* route an acceptance-failing
dispatch into review + rework.

---

## Story (ingest_plan schema)

```json
{
  "repo_root": "/Users/jessecarroll/.claude/mcp-servers/pipeline",
  "epics": [
    {
      "summary": "Merge-gate resilience",
      "stories": [
        {
          "summary": "Route merge-gate CI failures into the rework loop instead of terminal-failing",
          "description": "When the merge gate's CI check returns a definitive test failure, hand the branch back to the implementer as rework feedback (opt-in), instead of burning identical merge retries and terminal-failing. Closes the gap where a correct implementation plus a broken agent-written test file is abandoned with no chance to fix the test. Observed on gpt-oss retry_backoff + token_bucket, 2026-07-17.",
          "agent_instructions": "CONTEXT / ROOT CAUSE: In pipeline_mcp_server.py, advance_pipeline's merge gate (~lines 4673-4716), a CI-gate failure (_ci_status returns state=='fail') is treated identically to a transient push error: it increments story['merge_attempts'] and, at MERGE_MAX_ATTEMPTS, sets story['status']='failed' terminally. The branch is never re-dispatched to the implementer, so a CI failure caused by the agent's OWN committed test file (a broken/self-contradictory test the acceptance-scoped reviewer never saw) is retried identically N times then abandoned -- even when the implementation is correct and one rework round would fix the test.\n\nWHAT TO BUILD (opt-in via a new env flag PIPELINE_REWORK_ON_CI_FAIL, default OFF -- mirror the existing PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL opt-in exactly): when the merge-gate CI check returns state=='fail' (a definitive test failure -- NOT 'pending', NOT 'cancelled', NOT a push/network error), AND the story has remaining rework budget, route the story back into the rework loop with the CI failure output (ci['error']) as the reviewer-style feedback, instead of incrementing merge_attempts toward terminal fail. The implementer is re-dispatched, fixes the failing test/code, re-pushes, and the merge gate re-runs on the next tick.\n\nHARD CONSTRAINTS:\n1. Do NOT scope the CI gate to acceptance. The full-suite CI check is intentional -- it catches merged-but-wrong (see the _ci_status_stub docstring / RLI-3, 2026-07-04). Only change what happens ON failure, never what the gate checks.\n2. Transient/non-definitive failures must NOT consume rework budget and must NOT route to rework: push/force-with-lease errors, ci state 'pending', and ci state 'cancelled' (which already has its own one-auto-rerun path) all keep their existing behavior untouched.\n3. Bound the loop with the existing rework caps (PIPELINE_REWORK_MAX_ATTEMPTS and its _ORACLE / _ESCALATED variants -- reuse the SAME budget/counter the reviewer rework loop uses; do not invent a parallel counter). Once rework budget is exhausted, fall through to the EXISTING terminal-fail path -- no infinite loop.\n4. With the flag OFF, behavior must be byte-for-byte identical to today.\n\nTDD (write tests FIRST, in test_pipeline_mcp_server.py; DO NOT modify any existing test -- adding new test functions is fine): confirm they fail for the right reason before implementing. Cover at minimum:\n  (a) CI 'fail' + flag ON + rework budget remaining -> story routed to rework (status set to the same state the acceptance-fail->review path uses; ci error carried as feedback; rework counter incremented; merge_attempts NOT pushed to terminal 'failed').\n  (b) CI 'fail' + flag OFF -> existing behavior exactly (merge_attempts increments; terminal 'failed' at MERGE_MAX_ATTEMPTS).\n  (c) CI 'fail' + flag ON + rework budget already exhausted -> terminal 'failed' (proves no infinite loop).\n  (d) transient push error + flag ON -> NOT routed to rework, rework counter untouched.\n  (e) ci 'pending' + flag ON -> NOT routed to rework.\n  (f) ci 'cancelled' + flag ON -> existing single-auto-rerun path unchanged, not routed to rework on the first cancel.\n\nTESTABLE SUCCESS CRITERIA: `pytest test_pipeline_mcp_server.py` passes; the six new tests fail before the change and pass after; the full existing server suite (586+ tests) stays green; `git grep PIPELINE_REWORK_ON_CI_FAIL` shows the flag read once at the merge gate and documented in the README env-var section.",
          "persona": "software-engineer",
          "model": "sonnet",
          "risk": "high"
        }
      ]
    }
  ]
}
```

**Why `risk: high`** — this edits the production merge path (`advance_pipeline`),
the same region Mode 9 hardened. It drives the overlord's merge gating and
warrants a careful review gate even though the change itself is small.

**Why no `acceptance` fixture** — the behavior is heavily-mocked orchestration
internal to `advance_pipeline`; there is no clean read-only file fixture that
grades it. The agent writes the six unit tests per `agent_instructions`, and
the "tests pass" bar plus the reviewer gate suffice.

---

## Explicitly rejected alternatives

- **Scope the merge-CI gate to acceptance** (the "5th FM-A site" framing from
  the first-pass triage). Rejected: it would make the pipeline merge stories
  whose own committed test suite is broken — the exact merged-but-wrong class
  the gate exists to stop — and it cannot apply in production, where the gate
  is `gh pr checks` running the repo's real CI, which we do not control.
- **Un-scope the reviewer to the full suite.** Rejected: the reviewer being
  acceptance-scoped is correct; making it noisier is the wrong lever. The fix
  belongs at rework routing.

---

## Documentation gate

Update the README env-var section to document `PIPELINE_REWORK_ON_CI_FAIL`
alongside `PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL` (same opt-in family).
