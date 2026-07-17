# Plan: Escalating, enforceable rework feedback

**Status:** NOT YET IMPLEMENTED. Plan only, 2026-07-17. Awaiting user review.
**Origin:** 2026-07-17 gpt-oss `token_bucket` live validation of
`PIPELINE_REWORK_ON_CI_FAIL` (commit 159f4d0). The merge-CI→rework loop is now
*bounded* (advances 1/3→2/3→3/3→terminal-fail) and the CI detail (file:line,
actual-vs-expected) IS surfaced to the implementer every round — yet gpt-oss
never fixed the broken `test_time_backwards_no_refill` `3.0` assertion across
3 rounds. Investigation answered *why*: it isn't a model-capability refusal,
it's a harness done-criterion gap (see Root cause). The fix is therefore
enforceable at the harness, not a request of the model.

---

## Root cause (verified)

`tests/benchmark/local_agent_oracle.py:874` — the benchmark agent's loop
terminates the instant the **acceptance oracle** passes:

```python
def finish_if_green(step) -> bool:
    ok, _ = oracle_result()
    if ok:
        auto_commit("feat: implement task (acceptance oracle green)")
        print(f"[step {step}] ORACLE GREEN — acceptance tests pass; committed & done.")
        return True
    return False
```

The acceptance oracle is acceptance-**scoped** — it does not include the
agent's own committed test file. So on every rework round the agent poked
`test_rate_limiter.py`, the oracle stayed green, the loop auto-committed and
terminated, and the branch sailed through review (reviewer is also
acceptance-scoped, lines 1310-1328) only to fail the merge-gate full-suite CI
on the same `3.0` assertion — repeat, 3×, then bounded terminal-fail.

**The agent never ran its own full test suite**, so it never saw that
`test_time_backwards_no_refill` still failed. The rework feedback said "fix
your test," but the done-bar was "oracle green," and those are independent —
the agent satisfied the done-bar without satisfying the feedback. This is a
sibling of the FM-A family (oracle vs full-suite grading) at the *agent
done-criterion* rather than the merge gate. The merge gate correctly runs the
full suite; the agent's own loop does not.

Production (`scripts/local_agent.py`) has no oracle-auto-finish — the model
calls `done` itself, with commit-enforcement (done rejected while worktree
dirty, lines 914-925) but **no full-suite gate**. So a production agent can
likewise call `done` without ever running its own tests.

---

## The fix has three layers (in priority order)

### Layer 1 — Harness-enforced full-suite done-bar on CI-fail rework (the robust half)

Do not rely on the model to self-verify; make the harness require it. On a
rework round triggered by a merge-gate CI failure (the defect is the agent's
own test), raise the done-bar from "oracle green" to "full worktree suite
green."

- **Benchmark** (`local_agent_oracle.py`): on a rework round, `finish_if_green`
  (or a rework-scoped variant) runs the **full** worktree suite — the same
  pytest the merge gate's `_ci_status_stub` runs (`tests/benchmark/harness.py:626`)
  — and terminates only when it is green. On a failure, feed the failing-test
  excerpt back into the loop as another tool result so the agent must fix the
  specific failing assertion before it can stop (or hit the step cap).
  Reuse the existing full-suite runner; do not write a second one.
- **Production** (`scripts/local_agent.py`): extend the existing dirty-worktree
  `done`-rejection (914-925) so that on a CI-fail-rework round, `done` is also
  rejected when the full suite isn't green, with the failing test fed back.
  This is the production analog of the benchmark enforcement.

This forces convergence regardless of model diligence — gpt-oss cannot declare
done while its own test still fails, so it keeps working the test until fixed
or the step cap binds. **This is the layer that rescues gpt-oss; the prose
layers below are framing on top.**

### Layer 2 — Directive, round-escalating feedback (the framing half)

Today both rework paths give identical, general feedback every round (verified:
`_run_reviewer` prompt is stateless across rounds, `pipeline_mcp_server.py:1232-1364`;
the merge-CI feedback string is static, lines 4746-4757). Escalate directness by
round, and thread the round number + prior feedback into both ends:

- **Round 1** — high-level (today's guidance): "a test assertion you wrote is
  wrong; re-examine your own tests against the spec; do not call done until the
  full suite passes."
- **Round ≥2** — explicit directive naming the exact change, parsed from the CI
  excerpt: *"PREVIOUS REWORK DID NOT FIX THIS. On `test_rate_limiter.py:62`,
  `assert approx_equal(bucket.tokens, 3.0)` fails — the implementation
  produces `9.0`; change the expected `3.0` → `9.0`. Do not call done until
  `pytest` passes in full."*

Mechanism:
- Pass `rework_attempts` (round) + prior `review_feedback` into `_run_reviewer`
  (signature 1232, prompt 1337-1364) and into the implementer dispatch
  (`_build_dispatch_command` 873-878; resume append 3248-3259). Both have
  `story["rework_attempts"]` / `merge_attempts` in scope.
- The merge-CI feedback (4746-4757) becomes round-conditional: `attempts >= 2`
  prepends "PREVIOUS REWORK ATTEMPT {n} DID NOT FIX THIS. " and surfaces the
  parsed file:line + actual/expected from `gate_error`.

### Layer 3 — Production `_ci_status` enrichment (prerequisite for production Layer 2)

Production `_ci_status` (`pipeline_mcp_server.py:1911-1962`) polls
`gh pr checks --json bucket` and returns `error=""` on fail. So in production
`gate_error` is the literal `"ci fail: "` — **no pytest detail, no file:line,
no actual-vs-expected.** Layer 2's "name the exact change" is impossible in
production without this. Enrich `_ci_status` to additionally run
`gh run view <id> --log-failed` (or `--log`) on a fail verdict and capture the
failing-test excerpt into `error`, bounded to ~500-1000 chars (mirroring the
benchmark stub's `[-500:]`). The benchmark already surfaces this via the stub;
only production is blind.

Note: Layer 1 (full-suite done-bar) does NOT depend on Layer 3 — the benchmark
enforcement runs the suite locally and feeds the live failure back regardless
of what `gate_error` carried. Layer 3 is only needed for production Layer 2
prose.

---

## Story (ingest_plan schema)

```json
{
  "repo_root": "/Users/jessecarroll/.claude/mcp-servers/pipeline",
  "epics": [
    {
      "summary": "Rework feedback that converges",
      "stories": [
        {
          "summary": "Enforce a full-suite done-bar on CI-fail rework rounds so the agent cannot declare done while its own test still fails",
          "description": "Layer 1 of the reviewer-escalation plan. The benchmark agent's done-criterion is oracle-green (acceptance-scoped), so on a CI-fail rework round it stops and commits without ever running its own broken test — the merge gate then re-fails on the same assertion every round. Raise the done-bar to full-suite-green on CI-fail rework rounds, feeding the failing-test excerpt back into the loop. Production analog: reject the done tool call when the full suite isn't green on a CI-fail-rework round, parallel to the existing dirty-worktree rejection.",
          "agent_instructions": "CONTEXT / ROOT CAUSE: tests/benchmark/local_agent_oracle.py:874 finish_if_green terminates the agent loop the instant the acceptance oracle passes. The oracle is acceptance-scoped and excludes the agent's own committed test file, so on a CI-fail rework round (where the defect IS the agent's own test) the agent stops, auto-commits, and goes to review→APPROVE→merge-gate→CI-fail on the same assertion, looping until bounded terminal-fail. The agent never runs its own full suite. See REVIEWER_ESCALATION_PLAN.md.\n\nWHAT TO BUILD (Layer 1 — harness enforcement, the robust half):\n  (1) Benchmark: in local_agent_oracle.py, on a rework round (signal via env, e.g. LOCAL_AGENT_REWORK_FULL_SUITE=1 set by dispatch_story when the rework was triggered by a merge-gate CI fail), make finish_if_green ALSO run the full worktree suite (reuse the same test invocation the merge gate's _ci_status_stub uses — detect_test_command + run, capture last ~500 chars) and terminate only when BOTH the oracle AND the full suite are green. On a full-suite failure, do NOT terminate; feed the failing-test excerpt back as a user/tool message and continue the loop so the agent must fix the specific failing test. Keep the cold-start (non-rework) path byte-for-byte identical — oracle-green remains the bar there.\n  (2) Production: in scripts/local_agent.py, extend the done-rejection (lines ~914-925, which today rejects done while the worktree is dirty) so that on a CI-fail-rework round it ALSO rejects done when the full suite isn't green, feeding the failing-test excerpt back. Mirror the existing rejection's structure.\n\nHARD CONSTRAINTS:\n1. Cold-start (non-rework) behavior must be unchanged — oracle-green stays the done-bar for a fresh dispatch. Only CI-fail-rework rounds get the full-suite bar.\n2. Reuse the existing full-suite runner / detect_test_command. Do not write a second test runner.\n3. Bound the fed-back excerpt (~500 chars) so a noisy failure can't blow out the agent's context.\n4. The full-suite run must not itself trip the heavy-executable / read-heavy guards in a way that double-counts — check how _ci_status_stub avoids that.\n\nTDD (write tests FIRST; do not modify existing tests — adding new ones is fine): confirm they fail for the right reason before implementing. Cover at minimum:\n  (a) Benchmark rework round: oracle green but agent's own test fails -> loop does NOT terminate; the failing excerpt is fed back; loop terminates once the agent fixes the test and the full suite goes green.\n  (b) Benchmark cold start: oracle green, agent's own test fails -> loop STILL terminates (oracle-green bar unchanged) — proves the rework gate is scoped, not global.\n  (c) Benchmark rework round that never fixes the test -> step cap binds (no infinite loop).\n  (d) Production done-rejection: on a CI-fail-rework round, done is rejected with the failing excerpt when the suite isn't green; on a non-rework round, done is accepted on a clean worktree as today.\n\nTESTABLE SUCCESS CRITERIA: pytest passes; the new tests fail before the change and pass after; the existing harness/local-agent suites stay green; re-running the token_bucket cell with the enforcement on converges (the agent fixes line 62 and merges) OR bounded-terminal-fails after actually attempting the right line — NOT the prior 'pokes the file, oracle-green, done' loop.",
          "acceptance": [],
          "persona": "software-engineer",
          "model": "sonnet",
          "risk": "high",
          "dependencies": []
        },
        {
          "summary": "Make rework feedback directive and round-escalating (name the exact file:line + change on round >=2)",
          "description": "Layer 2 of the reviewer-escalation plan. Today both rework paths (review REQUEST_CHANGES and merge-CI-fail) give identical general feedback every round — the reviewer is stateless across rounds and the merge-CI string is static. Thread the round number + prior feedback into the reviewer and implementer prompts, and escalate directness: round 1 high-level, round >=2 an explicit directive naming the exact file:line + actual/expected + the change to make, plus 'do not call done until the full suite passes.'",
          "agent_instructions": "CONTEXT: pipeline_mcp_server.py _run_reviewer (1232-1364) builds a stateless prompt each call — no rework_attempts, no prior review_feedback, no round signal. review_story (4024-4031) doesn't pass round info. The merge-CI feedback (4746-4757, commit 159f4d0) is a static string; only gate_error varies; attempts is used only for the cap check. The implementer gets reviewer prose + static boilerplate with no round number (873-878, 3248-3259). See REVIEWER_ESCALATION_PLAN.md.\n\nWHAT TO BUILD (Layer 2 — directive escalation, the framing half):\n  (1) Thread rework_attempts (round) + prior review_feedback into _run_reviewer (add params; pass from review_story 4024-4031 via story.get('rework_attempts'), story.get('review_feedback')). Inject a round-conditional preamble: round 1 = high-level; round >=2 = 'This is your round-{N} review; the implementer has already failed to address: {prior_feedback}. Be MORE DIRECT: name the exact file:line and the exact change required.'\n  (2) Thread the round into the implementer dispatch: _build_dispatch_command (873-878) and the resume append (3248-3259) accept rework_round and prefix 'This is rework round {N} of {cap}. ' on round >=2.\n  (3) Merge-CI feedback (4746-4757): make the string round-conditional on attempts (in scope at 4746). attempts >= 2 prepends 'PREVIOUS REWORK ATTEMPT {attempts-1} DID NOT FIX THIS. ' and, when gate_error carries a parseable pytest excerpt (file:line + actual/expected), surfaces it explicitly as 'On {file}:{line}, {assertion} fails — actual {actual}; change expected {wrong} -> {actual}. Do not call done until the full suite passes.' Add a small helper to parse the pytest excerpt from gate_error (best-effort; fall back to verbatim if it doesn't parse).\n\nHARD CONSTRAINTS:\n1. Round 1 guidance must stay close to today's wording (high-level) — only round >=2 escalates. Do not make round 1 more verbose than today.\n2. Parsing the CI excerpt is best-effort; never fabricate a file:line or change. If the excerpt doesn't parse, fall back to verbatim gate_error + the static directive.\n3. Do not change verdict/control flow — the reviewer still APPROVE/REQUEST_CHANGES; the merge-CI path still routes to changes_requested. This layer only changes feedback TEXT.\n\nTDD: cover (a) round 1 feedback ≈ today's wording; (b) round >=2 feedback contains the prior-feedback reference + 'file:line' directive (with a parseable excerpt); (c) unparseable excerpt falls back to verbatim + directive, no fabricated line; (d) _run_reviewer receives rework_attempts + prior_feedback and the prompt reflects them.\n\nTESTABLE SUCCESS CRITERIA: pytest passes; new tests fail before / pass after; existing suite green; git grep shows rework_attempts threaded into _run_reviewer and both dispatch injection sites.",
          "acceptance": [],
          "persona": "software-engineer",
          "model": "sonnet",
          "risk": "medium",
          "dependencies": ["Enforce a full-suite done-bar on CI-fail rework rounds so the agent cannot declare done while its own test still fails"]
        },
        {
          "summary": "Enrich production _ci_status to capture the failing-test pytest excerpt so merge-CI rework feedback can name the broken assertion",
          "description": "Layer 3 / prerequisite for production Layer 2. Production _ci_status (pipeline_mcp_server.py:1911-1962) returns error='' on a fail verdict (it polls gh pr checks --json bucket, a classification only). So production gate_error is 'ci fail: ' with no pytest detail — Layer 2's 'name the exact change' is impossible in production. Enrich _ci_status to run `gh run view <id> --log-failed` on a fail and capture the failing-test excerpt into error, bounded ~500-1000 chars (mirroring the benchmark stub's [-500:]).",
          "agent_instructions": "CONTEXT: pipeline_mcp_server.py:1911-1962 _ci_status polls `gh pr checks <branch> --json bucket` and on a fail bucket returns {'state':'fail','error':''} — empty. The benchmark stub (tests/benchmark/harness.py:626) returns (stdout+stderr)[-500:] and so DOES surface file:line + actual/expected. Production is blind. See REVIEWER_ESCALATION_PLAN.md Layer 3.\n\nWHAT TO BUILD: on a fail bucket, additionally resolve the failed run id and run `gh run view <id> --log-failed` (fall back to `gh run view <id> --log` if --log-failed is unavailable / empty), capture the failing-test excerpt (the FAILED test summary + the assertion + file:line + actual/expected), truncate to ~500-1000 chars (tail, like the stub), and return it in error. Be robust to gh CLI output shape changes (parse failures fall back to '' — never crash the merge gate; an empty error preserves today's behavior).\n\nHARD CONSTRAINTS:\n1. Never raise — a gh parse failure must fall back to error='' (today's behavior), not break the merge gate.\n2. Bound the excerpt; a multi-page log must not be returned wholesale.\n3. Do not change the state classification (fail/cancelled/pending) — only enrich error on fail.\n4. Add a short timeout on the gh run view subprocess so a stuck gh can't hang the merge gate.\n\nTDD: mock gh pr checks + gh run view; cover (a) fail bucket -> error contains the pytest excerpt (file:line + actual/expected); (b) gh run view empty/missing -> error='' (graceful fallback); (c) cancelled/pending states unchanged; (d) timeout on gh run view -> error='' (no hang).\n\nTESTABLE SUCCESS CRITERIA: pytest passes; new tests fail before / pass after; existing _ci_status tests stay green; production merge-CI rework feedback now carries the failing assertion (verifiable via a fixtures-only unit test, no real CI).",
          "acceptance": [],
          "persona": "software-engineer",
          "model": "sonnet",
          "risk": "medium",
          "dependencies": ["Make rework feedback directive and round-escalating (name the exact file:line + change on round >=2)"]
        }
      ]
    }
  ]
}
```

---

## Explicitly rejected / deferred

- **Make the reviewer un-scoped to the full suite so it catches the agent's
  broken test itself.** Rejected: the reviewer being acceptance-scoped is
  correct and intentional (it's the defensible design — see MERGE_CI_REWORK_PLAN.md).
  The fix belongs at the done-bar (Layer 1) and the CI feedback path, not at
  re-scoping the reviewer.
- **Trust the model to run its own tests.** Rejected as the primary mechanism:
  the live run proved gpt-oss stops on oracle-green without self-verifying.
  Layer 1 enforces it at the harness; Layer 2's "do not call done until pytest
  passes" prose is a complement, not a substitute.
- **Per-round escalation of the reviewer verdict (e.g. lower the APPROVE bar
  on later rounds).** Deferred — out of scope; risks the reviewer blocking
  correct work. This plan only escalates feedback TEXT and the done-bar.

---

## Validation

Re-run the `token_bucket` cell (and `retry_backoff`) with Layer 1 + Layer 2
enabled. Expected: the agent, unable to declare done while its own test fails,
fixes line 62 (`3.0`→`9.0`) and the full suite goes green → merge. If the
model still can't fix it, it bounded-terminal-fails after *actually attempting
the right line* (the fed-back excerpt), not the prior "pokes the file, oracle-
green, done" loop. Compare convergence rate vs the 159f4d0 baseline (0/1 for
token_bucket). A/B on a stronger implementer (Claude) to confirm the
enforcement, not just gpt-oss diligence, is doing the work.