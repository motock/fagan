# Mode 20: "Correct-but-rejected" — reviewer blocks on the agent's own buggy test, rework destroys correct code

**PLAN CLOSED (2026-07-30).** Fixes 1/3/7 shipped and live-verified; Fixes
2/5/6 are explicitly deferred with reasons in "Explicitly out of scope" below
(each needs its own design/validation cycle against shared prompts/control-flow
every story depends on). No further action needed unless a later run shows
Fix 1 doesn't fully close the gap. Closed per `MATURITY_AND_UNIQUENESS_PLANS.md`
A1's "decide the remaining plan docs' fate" item.

Status: **VERIFIED LIVE END-TO-END, 2026-07-17.** Fixes 1, 3, 7 done (Fix 7
required no code change — see below); Fixes 2, 5, 6 deliberately deferred (see
"Explicitly out of scope"). Direct-implemented, no pipeline story — this fixes
the pipeline's own review/rework path, which is self-blocking exactly like PR
#28-#56's direct-branch precedents in `~/.claude/mcp-servers/pipeline` memory.

**Live re-run verification** (`mode20_fix_verify_20260717_082402`, identical
config to the failing run: token_bucket/mlx/t0, same production settings,
same qwen2.5-coder-14b weights): **`final_status: done`, `review_verdict:
APPROVE`, `rework_attempts: 0`, ground truth 15/15 against merged master.**
940s, single review cycle, first try. The agent again wrote its own weak
`test_zero_tokens` (same latent bug present, `test_changed: true`) — but
`review.log` confirms the reviewer ran `pytest test_acceptance.py` scoped
(10/10), never touched the agent's own test file, never issued
`REQUEST_CHANGES`, so no rework ever fired and there was nothing to regress.
Same model, same task, same config; only variable was the fix. Prior to the
live run, also replayed the fixed `_run_reviewer` offline against the exact
worktree that produced the original failure and confirmed the constructed
command was `pytest .../test_acceptance.py` only. Full suite 1096 passed,
ruff clean throughout.

## Finding (2026-07-17, verified by replay)

`role_registry_prod_verify5_20260717_073857` (token_bucket/mlx/t0, qwen2.5-coder-14b
implementer) scored 0/1. Replaying the implementer's **first** attempt (extracted
from `.agent_transcript.json`) against the acceptance oracle + held-out ground
truth: **25/25 passed.** The model solved the task correctly on attempt 1. The
pipeline rejected it and the resulting rework introduced the exact bug ground
truth caught.

Sequence:
1. Agent's own `test_zero_tokens` (in `test_rate_limiter.py`, NOT the harness's
   `test_acceptance.py`) asserts something unsatisfiable given the rest of the spec.
2. Attempt 1's `rate_limiter.py` is correct (verified by replay). The agent
   re-emits it byte-identical because there is no move that satisfies its own
   broken test without violating the spec — repetition guard parks it (guard
   behaved correctly given its inputs).
3. **The reviewer ran bare `pytest`** (a free-form bash choice), hit the agent's
   broken test, issued `REQUEST_CHANGES` — while `test_acceptance.py` was 10/10
   green in that same run.
4. Rework, seeded with that feedback, rewrote the whole file and regressed the
   exact backward-jump bug the planner had explicitly warned against.
5. Ground truth failed on backward-jump. Scorecard: 0/1.

Full writeup: `~/.claude/projects/-Users-jessecarroll--claude-mcp-servers-pipeline/memory/project_dispatch_failure_modes.md`,
Mode 20.

## Root causes

1. **FM-A resurrected in the review path.** The FM-A fix (`pipeline_mcp_server.py`
   `_reverify_acceptance` / `scope_to_acceptance`) scoped the *harness test gate*
   to the acceptance oracle. `_run_reviewer` never got the same treatment — it
   hands the reviewer a full-suite `pytest` command via `detect_test_command`,
   so the reviewer can rediscover and block on the agent's own test bugs.
2. **No escape hatch when the agent's own test is unsatisfiable.** Steering says
   "never touch test files, the bug is always in the impl" — a deadlock by
   construction when the test itself is wrong.
3. **Whole-file `create_file` rework has no diff discipline** — a rework can
   silently drop an already-correct line. The repetition guard exempts
   `str_replace` as a "legitimate fix-build cycle" while steering forbids it.
4. **Parking discards a green state** downstream in scorekeeping even though the
   pipeline itself preserves the commit (existing Mode 13/14 behavior) — the
   *benchmark scorecard* has no way to represent "was correct at some point."

## Fixes being implemented directly (this repo, TDD, no pipeline story)

- [x] **Fix 1 — scope the reviewer's OWN test command to the acceptance oracle.**
  `_run_reviewer` now takes the story's `acceptance` block and, when the detected
  test command is pytest, scopes it to the acceptance paths exactly like
  `_reverify_acceptance` does — plus explicit prompt language that a failure in
  an agent-authored test the acceptance oracle doesn't require is not sufficient
  grounds for `REQUEST_CHANGES` on its own. This is the change that flips the
  verdict-shepherd's evidence to the oracle by construction, mirroring FM-A.
- [x] **Fix 3 (lightweight) — don't let rework fly blind into a regression.**
  When `review_story` is about to send a story to rework, and the story carries
  an `acceptance` block, it now independently re-runs the acceptance oracle
  against the CURRENT worktree state before dispatching rework. If the oracle
  currently passes, that fact is prepended to `review_feedback` so the
  redispatched agent is explicitly told "the acceptance oracle is green right
  now — do not regress functional correctness while addressing this feedback."
  Non-blocking (does not change verdict/control flow), so it's safe to ship
  without a bigger redesign.
- [ ] **Fix 2 — contradiction detector / escalation authority.** Deferred: needs
  a design decision for autonomous mode (who is the "user approval" authority
  from CLAUDE.md Step 4 when there's no human present). Fix 1 removes the
  proximate cause for this specific failure signature; revisit if Fix 1 doesn't
  fully close the gap in later runs.
- [ ] **Fix 4 — route "parked but oracle-green" to review instead of failed.**
  The pipeline already preserves the commit on park (Mode 13/14); the gap is
  purely in the *benchmark scorecard's* bucketing, not pipeline state. Folded
  into Fix 7 below instead of a pipeline-side change.
- [ ] **Fix 5 — resolve the `str_replace`/`create_file` steering conflict.**
  Deferred: this is a change to the guided-decomposition steering prompt
  (`GUIDED_DECOMPOSITION_PLAN.md` territory) that affects every story, not just
  oracle-bearing ones — needs its own validation pass, not a same-session change.
- [ ] **Fix 6 — planner emits literal assertions for state invariants, not just
  prose worked examples.** Deferred: a prompt-engineering change to the
  decompose/planner persona; needs A/B evidence across more than one cell before
  changing a prompt every plan depends on.
- [x] **Fix 7 — already exists, no change needed.** Before writing this,
  checked `harness.py`'s result-building code (~line 995-1045): it already
  runs `run_groundtruth` against the surviving worktree (not just merged
  master) regardless of `final_status`, and records `groundtruth_passed` +
  `groundtruth_where: "worktree"` for exactly this case. The "final state was
  actually fine" signal I was about to add as a new `oracle_pass_at_end`
  field is already there under a different name — this is how Mode 20 was
  detected in the first place (verify5's `groundtruth_passed: false` was
  legible and led to the transcript-replay investigation). What's genuinely
  missing is automating the transcript-replay half (checking whether an
  EARLIER attempt, since overwritten, was better than the final one) — that
  remains a manual forensic step per the Mode 20 memory's "how to apply";
  automating it is future work, not done here.

## Explicitly out of scope for this pass

Fixes 2, 5, 6 above, and full transcript-replay automation for Fix 7. Each
needs its own design/validation cycle and touches shared prompts/control-flow
used by every story, not just this failure signature — bundling them into one
direct-repair session would be exactly the kind of large, multi-concern change
CLAUDE.md's PR-size/one-concern guidance warns against.
