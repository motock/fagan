# OT-03-repo-issue — repair the detached grade that exits rc=1 while the suite is green

Follow-up repo-issue filed by triage for story **OT-03**. `OT-03` failed for an
environmental reason (the detached grade step), not on its own scope. This story
repairs the grading harness through the normal TDD / review / CI path. **This file
is the authoritative record for the fixer.** The manifest entry
(`~/.claude/plans/observability-otel.manifest.json`, key `OT-03-repo-issue`) persists
only `key`, `summary`, `status`, `agent_instructions`, `persona`, `risk` and
`backend` — it drops `description`, `files` and `acceptance`. Refresh the manifest
record from this file before dispatch rather than treating a thinner manifest
record as a different story.

## Hold on the parent

`OT-03` stays parked for a human. Do **not** re-dispatch `OT-03`, do **not** mark
`OT-03` done, do **not** merge `agent/ot-03`. A human re-runs the detached grade on
the preserved commit and merges only if that grade is green.

## Preserved work (verified, not re-dispatched)

- commit `46b4ab5` — `docs(telemetry): document the OTel endpoint and metric names`
- branch `origin/agent/ot-03` (`git rev-parse origin/agent/ot-03` =
  `46b4ab530841824af20c0b4b5c7467b4e38413a1`)
- `REFERENCE.md +46/-0` — docs-only, no production code touched

## Defect — the detached grade exits rc=1 with no result while the unit suite is green at HEAD

Recorded evidence (from `OT-03`'s manifest `last_test_check`, not invented state):

- `returncode` = 1
- `stderr_tail` = `detached grade exited without writing a result`
- `cwd` = the `OT-03` worktree, whose HEAD is `46b4ab5`
- the same worktree's unit/docs guards pass at that HEAD (61 passed), and `ruff`
  is clean — so the red check is a harness failure, not a test failure

Where the message is produced: `pipeline/detached_grade.py:131` and
`pipeline/detached_grade.py:294` both emit `"stderr": "detached grade exited
without writing a result"`. The grade child exits without writing its result
payload, and the caller synthesises an rc=1 failure with that stderr. The
downstream consequence is the real bug: `pipeline/story_status.py` records the
run as a failing test check, so an environmental grade failure becomes
indistinguishable from a genuine red suite and the story is parked as if its
code failed.

Required behaviour for the fix:

1. Keep the existing fail-closed behaviour for genuine child crashes (the
   `FAIL_CLOSED_STDERR` guards in `tests/unit/test_detached_grade.py`,
   `tests/unit/test_detached_grade_retry_once.py` and
   `tests/unit/test_dead_pid_detached_grading.py` must keep passing).
2. But distinguish *no result written* from *the suite failed*: the recorded
   reason must name the harness defect (an infra/needs-human outcome), not a
   test failure, and must not park a story whose suite is green at HEAD.
3. Cover both the positive path (a result payload is written and graded) and
   the negative/boundary paths (missing payload, empty payload, child killed,
   retry-once) with tests in the normal `tests/unit/` suite.

## Explicit non-goal

This story **does not implement OT-03's deliverable**. The `REFERENCE.md`
OpenTelemetry endpoint/metric documentation already exists at `46b4ab5`; do not
rewrite it, do not touch `REFERENCE.md`, do not re-run or re-dispatch `OT-03`.

## Story record

```json
{
  "key": "OT-03-repo-issue",
  "summary": "OT-03-repo-issue: repair the detached grade step that exits rc=1 with 'detached grade exited without writing a result' while the unit suite is green at HEAD",
  "description": "Follow-up repo-issue filed by triage for story OT-03 (branch origin/agent/ot-03, commit 46b4ab5, REFERENCE.md +46/-0). Triage ruled OT-03 failed environmentally: the detached grade step exits rc=1 with the message 'detached grade exited without writing a result' while the unit suite is green at HEAD. OT-03 stays parked for a human and is not re-dispatched. This story repairs the grading harness through normal TDD/review/CI and does not implement OT-03's deliverable.",
  "persona": "software-engineer",
  "model": "sonnet",
  "risk": "medium",
  "files": [
    "pipeline/detached_grade.py",
    "pipeline/story_status.py",
    "tests/unit/test_detached_grade.py",
    "tests/unit/test_detached_grade_retry_once.py"
  ],
  "agent_instructions": "Follow-up to OT-03 (branch origin/agent/ot-03, commit 46b4ab5, REFERENCE.md +46/-0). HOLD CONDITIONS: OT-03 stays parked for a human - do NOT re-dispatch OT-03, do NOT mark OT-03 done, do NOT merge agent/ot-03, do NOT touch or revert REFERENCE.md. A human re-runs the detached grade on 46b4ab5 and merges only if green. This story does not implement OT-03's deliverable.\n\nDEFECT: the detached grade step exits rc=1 with the exact message 'detached grade exited without writing a result' while the unit suite is green at HEAD. Recorded evidence is OT-03's manifest last_test_check: returncode 1, stderr_tail starting with that message, cwd the OT-03 worktree at 46b4ab5, where the docs guards pass (61 passed) and ruff is clean. The message is emitted at pipeline/detached_grade.py:131 and pipeline/detached_grade.py:294 as the stderr of a synthesised failure when the grade child exits without writing its result payload.\n\nWHY IT MATTERS: pipeline/story_status.py records the run as a failing test check, so a harness failure is indistinguishable from a genuine red suite. That is what parked OT-03 despite green tests at HEAD.\n\nSCOPE: repair the grading harness through the normal TDD/review/CI path. Preserve distinct outcomes: a missing/empty result payload, a killed child, and a child that reports a failing suite must not all collapse to the same recorded verdict. Keep fail-closed behaviour for real crashes (the FAIL_CLOSED_STDERR guards in tests/unit/test_detached_grade.py, tests/unit/test_detached_grade_retry_once.py and tests/unit/test_dead_pid_detached_grading.py must keep passing), but record the no-result case as an infra/needs-human reason that cannot park a story whose suite is green at HEAD. Cover positive, negative and boundary cases: result written and graded, missing payload, empty payload, child killed, retry-once path. Do not weaken or delete existing tests; add regression tests that fail for the bug before the fix.\n\nNON-GOAL: do not implement OT-03's deliverable and do not edit REFERENCE.md.",
  "acceptance": [],
  "dependencies": [],
  "blocked_by": [],
  "status": "todo"
}
```
