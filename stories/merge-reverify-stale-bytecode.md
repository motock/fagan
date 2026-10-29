# Repo issue: the merge gate can grade stale bytecode and report a false pass

Filed 2026-10-29. Found while merging `TP-01` (`agent/tp-01`), whose diff does
not touch any of this: its macOS job failed in
`tests/integration/test_merge_reverify_real.py::test_real_job_reports_pass_then_fail`,
a test added by `TP-04` (`cec53ea`).

## Symptom

`test_real_job_reports_pass_then_fail` runs two merge-reverify jobs against the
same worktree. Round 1 grades a green sample suite and expects `pass`; the test
then rewrites the sample to a failing assertion, commits, starts a second job,
and expects `fail`.

The second job intermittently reports **`state: "pass"`** — about **2 runs in 6**
locally. CI caught it on `Test (Python 3.14, macos-latest)` while the other five
jobs passed on the same commit.

## What it is not

The reported `sha` equals the worktree's new `HEAD` in every failing run, so the
job is **not** reading the previous job's result file. An earlier guess — a
stale `result.json` in the `story_key`-keyed job directory
(`pipeline/merge_reverify.py:100-110`) — is wrong; `start_merge_reverify`
already unlinks it, and the SHA proves the child graded the new commit.

## Root cause — stale `__pycache__`

The two versions of the sample are the *same byte length* and are written within
the same wall-clock second:

```
def test_one_plus_one():
    assert 1 + 1 == 2      # round 1
def test_one_plus_one():
    assert 1 + 1 == 3      # round 2, one character differs, same length
```

CPython validates a `.pyc` against the source's **mtime truncated to whole
seconds** plus its **size**. Same second, same size, so the existing
`__pycache__/test_sample.cpython-*.pyc` is accepted and the child runs round 1's
compiled test — `1 passed in 0.00s` — against round 2's tree.

Measured, in the TP-01 worktree, six runs per variant:

| variant | result |
|---|---|
| the test as written (same length, `__pycache__` present) | **2 of 6 wrong** |
| same edit, `__pycache__` removed before the second job | 0 of 6 wrong |
| same edit, source made a different length (extra comment line) | 0 of 6 wrong |

Both controls eliminate it, and only the pycache-preserving variant reproduces
it. That is the mechanism, not a correlation.

## Why it is a product defect, not just a flaky test

`pipeline/merge_reverify.py` grades a worktree that the merge gate **rebases in
place** and reuses across commits. A rebase that changes a test to the same byte
length inside one second can therefore be graded from stale bytecode and report
`state: "pass"` for a suite that is actually red. The gate's whole guarantee is
"no merge without a passing re-verify of the exact pushed SHA"; here it can pass
the wrong bytes while naming the right SHA. That is fail-open, which is the
direction the repo's rules say a gate must never fail in.

The test is the *detector*, not the defect. Fixing only the test would hide it.

## Scope for the fix

Prefer the product fix, then make the test reproduce it rather than sleep:

1. Isolate the grading child's bytecode cache per job, so a worktree's existing
   `__pycache__` can never be consulted — e.g. give the child its own
   `PYTHONPYCACHEPREFIX` under the job directory (removed with it), alongside
   whatever it already sets in its environment.
2. Keep the existing test's shape: it is the only thing that reproduces this,
   and it must be observed failing *before* the fix, then passing **repeatedly**
   (the failure rate is ~1 in 3, so one green run proves nothing — run it in a
   loop, e.g. 10 iterations).
3. Add a regression assertion that does not depend on timing: after the second
   job, assert the result's evidence (the executed test's own output) is the
   round-2 suite, so a future stale-cache regression fails deterministically
   rather than 2 times in 6.

Do not fix this by making the sample a different length. That removes the
detector and leaves the gate able to grade stale bytecode.

## Relationship to the other harness defects

Separate from, and additional to:

- `stories/OT-03-repo-issue.md` — the detached grade exiting rc=1 with
  "detached grade exited without writing a result", and the fixed set of 20
  nodes that go red only while another full suite runs concurrently.

Three distinct harness defects now, all of which present as "the gate says red
(or green) despite the suite" — which is why `pipeline/story_status.py`'s
collapsing them into one recorded verdict is itself worth fixing.

## Not in scope

- `TP-01`'s own diff (`pipeline/advance_one.py`, `pipeline/advance_pool.py`,
  `pipeline/telemetry.py`). It is unrelated; its CI failure was this defect.
- The `.`/`..` traversal hardening in `start_merge_reverify`, which `TP-04`'s
  final rework explicitly deferred to a separate story that does not yet exist.
  It should be filed alongside this one.
