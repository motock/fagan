# The Acceptance-Oracle Grading Pattern

- **Status:** Published pattern (stable)
- **Audience:** Builders of autonomous code-generation harnesses — any system
  that dispatches an AI executor at a story and must afterwards produce a
  trustworthy verdict on whether the delivered code satisfies the requirement.
- **Provenance:** Distilled from production incidents in an autonomous
  dispatch pipeline (Plan B5, insight FM-A). Every rule is backed by at least
  one live failure; the pattern is stated generically so another harness can
  adopt it without importing this repository's code. How this repository
  implements it is confined to the Appendix.

## 0. Summary

An executor that grades itself on its own tests converges to the minimum edit
that turns those tests green. That convergence is not a model defect to be
prompted away; it is the rational response to the incentive the harness
created. The fix is structural: separate *who authors the grade* from *who
writes the code*. The plan author authors a small set of acceptance fixtures
before the executor is launched; the harness materializes them read-only,
digest-verifies them, and grades the delivered branch on those fixtures
alone. The executor's own tests are never the grade.

This document specifies five normative rules (§2), the outcome classification
that detects unusable and born-broken oracles before they burn an executor's
budget (§3), and an adoption checklist (§4). The key words MUST, SHOULD, and
MAY are to be interpreted as described in RFC 2119.

## 1. Problem statement

An executor that grades itself on its own tests fails in three recurring
ways. Any grading scheme MUST be designed against all three:

1. **Minimum-edit convergence.** An executor graded on its own tests stops at
   the smallest edit that turns them green. Anything no assertion covers is
   liable to be left half-done (a rename applied in one place but not
   another, a doc comment never updated, a second call site never migrated).
   The suite is green; the requirement is not met. No amount of "be thorough"
   in the prompt beats a grade that cannot see the gap.
2. **Self-consistent bugs.** A test that exists and passes is necessary but
   not sufficient. An executor that misreads a boundary condition — an
   off-by-one, an inclusive/exclusive edge — can write a fully green test
   suite that encodes the *same* mistake as its implementation. The test then
   confirms the bug instead of catching it: self-grading is circular
   precisely when it matters most. Only a grader whose expectations were
   fixed *before* the implementation existed can catch an error the
   implementer does not know it is making.
3. **Half-done wiring under a green fixture.** A green suite proves the
   branch logic the assertions cover, not that the deliverable is complete.
   An acceptance fixture that calls the changed unit in isolation — never
   touching the call site, registration path, or wiring the story actually
   asks for — goes green the moment the unit works alone, so the ungraded
   wiring step is skipped and the story ships dead code. This is not
   hypothetical: in one documented incident, a fixture asserted a helper
   returned the right string, the brief said "wire this at the call site,"
   and the executor never touched the call site — because nothing graded it.
   The full-suite bar did not catch it either, since the suite did not
   exercise the call site any more than the fixture did.

## 2. The pattern's five rules

### Rule (a): grade on plan-authored fixtures, not agent-authored tests

The grading inputs MUST be authored by the plan author, at plan-authoring
time, before any executor exists, and MUST be carried in the plan itself as
explicit acceptance entries (a file path plus the authoritative file source).
The executor's own tests — pre-existing or written during the story — MUST
NOT be the grade. The executor MAY still write its own tests; they are
development aids, not the oracle. Rationale: an executor can edit its own
tests (class 1), can share its own misconceptions with its own tests
(class 2), and can satisfy its own tests without doing the graded wiring
(class 3). A plan-authored fixture is fixed before the executor exists, so
none of the three is available to it.

### Rule (b): fixtures are materialized read-only and digest-verified

The harness MUST materialize the plan's authoritative fixture source into the
worktree *before* the executor launches, overwriting any inherited file at the
same path — a stale, already-satisfied copy silently grades nothing and
produces a false green with zero implementation. At materialization time the
harness MUST record a digest (e.g. SHA-256) of each fixture's authoritative
*manifest source* — never of the file on disk, which may already have been
rewritten.

Before any grading run, the harness MUST compare each fixture's worktree bytes
against its pinned digest. On mismatch it MUST refuse WITHOUT running
anything: a rewritten grader is not a trustworthy gate, and running it is
self-grading again — the exact failure the pattern exists to prevent. The
refusal is a tamper refusal, not a red fixture and not a partial run.

A refusal MUST NOT alter the pinned digest. The pin is immutable manifest
content; the only legitimate way to change fixture content is a plan-author
correction that re-pins from the authoritative source. The failure mode a
"helpful" re-pin creates is permanent: suppose the plan pins a fixture at
`sha256:3f9a1c…` and the worktree copy hashes to `sha256:b7e42d…`. The
correct behavior is to refuse and leave the pin at `3f9a1c…`, so that once
the fixture is restored the next run hashes to `3f9a1c…`, matches, and runs
normally. A refusal that re-pinned to `b7e42d…` would make the next
comparison `b7e42d…` against `b7e42d…` — "match" — and the tampered fixture
would run, and pass, forever.

### Rule (c): fixture lint hygiene — the plan author lints before embedding

Acceptance fixtures bypass every quality role in the harness: they are not
written by the executor's test-authoring phase and no reviewer persona sees
them before dispatch. A fixture with a lint violation therefore becomes a
read-only oracle the executor is forbidden to touch, and a repo-wide lint
gate fails every rework attempt with no way for the agent to ever fix it — a
born-broken oracle caused by the plan author rather than by a prior merged
gate. (Live case: four of seven CI lint errors traced to the plan author's
own fixture — unused unpacked variables — burned a full rework cycle on a
file the executor could not edit.)

The plan author MUST, before embedding a fixture as read-only oracle
content: materialize the fixture source into a scratch copy of the target
repository and run BOTH the project's test command AND its lint command
against it. A dry run of the test command alone is not enough — the live
incident above caught the functional bugs and missed four lint errors in the
very same fixtures. The author MUST also confirm the pre-dispatch outcome is
a *useful* failure (§3): a fixture that errors out, collects nothing, or
already passes with zero implementation is an unusable oracle and MUST be
fixed before ingestion, because the executor cannot fix it later. If a
fixture needs correcting after dispatch, the correction MUST go through the
plan's authoritative source (which re-pins the digest) and the live worktree
copy MUST be hand-fixed to match before the run resumes.

### Rule (d): fixtures must traverse the real integration seam

A fixture MUST exercise the path the requirement actually asks for. State the
rule at the right strength: the requirement is "traverse the real integration
seam" — not "never import the unit" (over-broad: a unit-level assertion is
fine as long as the graded path also crosses the seam the story exists to
wire) and not "mock the neighbors for speed" (which defeats the purpose —
mocking the integration point is exactly how fixtures pass with the wiring
half-done, class 3).

The operative test the plan author MUST apply before embedding a fixture:
*would this fixture still pass if the required wiring were left half-done?*
If yes, the fixture MUST be rewritten to drive the real entrypoint (the
main function, the CLI, the route handler) with mocks only at true external
boundaries, or to assert against the production source/registry that the
wiring actually landed (e.g. that the entrypoint is registered, not merely
that a bare attribute is callable — late name binding lets an unregistered
attribute and the registered object diverge invisibly). A harness MAY run a
non-blocking heuristic at plan ingestion that flags fixtures which appear to
exercise the unit in isolation only, and surface it as a prompt to re-check
the fixture rather than as a hard gate.

### Rule (e): a separate regression backstop covers what fixtures don't

Acceptance fixtures are deliberately narrow; they do not make a full-suite
regression check redundant. The harness MUST provide a backstop that is
*independent* of both the executor and the fixtures: a reviewer who diffs the
delivered change against the actual requirement (not against "did the tests
pass"), plus a full-suite run of the delivered branch. The backstop MUST be
outside the executor's control — a full-suite run performed by the executor
is still self-grading. The two layers have different coverage and neither
substitutes for the other: the fixtures grade the requirement's core
behavior; the reviewer catches spec-completeness gaps no assertion covers;
the full suite catches regressions in behavior the fixtures never touch. A
merge-time re-verification of the fixtures against the just-rebased branch —
independent of both review and CI — SHOULD close the residual gap for repos
without CI and for slips between "tests passed" and merge.

## 3. Outcome classification and born-broken detection

### 3.1 Outcome states

Grading a fixture set against a checkout yields exactly one of four states.
The harness MUST classify before it reacts, because the states demand
opposite actions:

- **`passes`** — the fixture is already satisfied with no implementation. The
  oracle tests nothing missing; it cannot distinguish work from no work. This
  is a broken pre-condition, not a success: the harness MUST refuse to
  dispatch on it (a false green with zero implementation is worse than no
  signal, because it looks like success).
- **`empty`** — the runner collected no tests. The oracle graded nothing;
  refuse to dispatch.
- **`errors`** — the oracle itself is broken: a syntax error in a helper, a
  parser rejecting fixture content, a malformed CLI invocation, an
  infrastructure failure. The grader never reached a real assertion, so the
  run produces no usable signal. Refuse to dispatch and report the fixture as
  broken; this is the most expensive failure mode to discover late, because
  it consumes an executor's entire budget while producing nothing.
- **`fails_correctly`** — the fixture ran cleanly and correctly reports that
  the deliverable is missing. This is the CORRECT pre-dispatch state and the
  only state that should proceed to implementation. Note that a
  not-yet-written module may surface as an import/collection error in the
  runner's output; the classification MUST treat an expected missing-module
  failure as `fails_correctly`, not as a broken oracle, or every
  before-implementation check will misfire.

### 3.2 The prior-gate rule (born-broken detection)

A fixture that fails at a clean baseline — before the executor has touched
anything — was broken by a prior gate's merge, not by the executor. The
harness MUST detect this case and MUST NOT charge it to the executor,
because no implementation can satisfy it: the correct action is to refuse to
dispatch and route the failure to whoever owns the broken baseline.

The discriminator MUST be evidence-based: the failure is attributable to a
prior gate when the failure output carries the signature of an enforcement
mechanism that an already-merged change introduced (a deletion-gate block
message, a newly enforced lint rule, a renamed API) AND the fixture does not
itself intend to test that mechanism. A fixture whose own assertions
reference the gate's mechanism is a legitimate test of an unimplemented
feature and stays a useful failure. A fixture that cannot prove its intent
MUST be treated as born-broken — the safe direction, since a born-broken
oracle wastes whole dispatches while a false "born-broken" verdict merely
delays one.

### 3.3 Worked example: the recorded baseline must stay truthful

Let F be an acceptance fixture and c1…c4 successive commits.

- **c1 (baseline, before the executor exists):** F fails — a prior gate's
  merge broke the behavior F checks. The recorded baseline result for F is
  *fail, prior-gate trip*.
- **c2 (executor branch):** F fails the same way. Correct classification:
  prior-gate trip, NOT an executor failure. The mistake to avoid: failing the
  executor because F is red on their branch. The corrupt-state variant to
  avoid: overwriting the recorded baseline result to green without the prior
  gate's fix — c2's verdict then *looks* handled, but the record is now a
  lie.
- **c3:** the prior gate's fix lands; F passes at baseline. The recorded
  baseline result is *pass*.
- **c4 (a later executor change):** a genuine regression breaks F. Because
  the recorded baseline is truthful (fail-then-pass across c1→c3), c4's
  failure correctly classifies as an executor failure.

The follow-up is the point: getting c2's classification right is not enough.
The recorded baseline MUST still be truthful when the next call reads it. A
c2 verdict produced by overwriting the baseline to green — rather than by
classifying the failure as a prior-gate trip — masks the c4 regression
against a false-green baseline, and the harness silently merges a regression
it was built to catch. Classification and record-keeping are the same
mechanism: the verdict at c2 is only as good as the baseline state it leaves
behind for c4.

## 4. Adoption checklist

A harness adopting this pattern MUST provide the following four provisions.
Each is stated with the minimum viable form; refinements are optional.

1. **A fixture store.** Acceptance entries live in the plan/manifest, not in
   the executor's reach: per entry, a worktree-relative path and the
   authoritative source text. Plan-authored only; the executor MUST NOT be
   able to author, edit, or review them. Provide a plan-authoring lint step
   (rule (c)) and an isolation-only heuristic (rule (d)) at ingestion time,
   before the fixture is ever embedded.
2. **Digest pinning.** At dispatch, record a digest of each fixture's
   authoritative source text. Pins MUST be immutable for the life of the
   story; the ONLY way to change pinned content is a plan-author correction
   that re-pins from the authoritative source. A tamper refusal MUST leave
   the pin untouched (§2, rule (b)).
3. **Materialization.** Before the executor launches, write the
   authoritative source into the worktree, overwriting inherited stale
   copies; skip the overwrite only on a resumed run, where mid-run fixture
   evolution may be committed work in progress. Re-record digests on every
   dispatch. Before grading, verify worktree bytes against pins and refuse
   without running on mismatch.
4. **Outcome classification.** Implement the four states of §3.1 and the
   prior-gate rule of §3.3, including: refuse-to-dispatch on `passes`,
   `empty`, and `errors`; proceed only on `fails_correctly`; re-verify the
   fixtures at merge time, independent of review and CI; and keep recorded
   baseline results truthful across runs. A harness SHOULD additionally run
   a pre-dispatch validation pass against a clean checkout, scope the
   merge-time re-verification to the fixture paths when the runner supports
   scoping (falling back to the full suite when it does not), and provide a
   reviewer role whose verdict is independent of the fixture verdict.

## Appendix: How this repository implements it

This repository implements the pattern as follows. Function names are
listed for navigation only; the normative content of this spec is the
sections above.

- `pipeline/oracle_gate.py` — the oracle module.
  `classify_oracle_outcome` implements the four states of §3.1 (`passes`,
  `empty`, `errors` via error markers, `fails_correctly`).
  `validate_acceptance_fixtures` runs a story's fixtures against a checkout
  (never raises; an uninvokable runner is reported as `errors`) and
  reclassifies born-broken oracles to `errors` before dispatch.
  `acceptance_digests` pins a sha256 of each fixture's authoritative
  manifest `source` — never the on-disk file. `_oracle_trips_prior_gate`
  implements the prior-gate discriminator: the merged deletion gate's block
  signature appears in the failure output AND no fixture source references
  the gate's `confirm_removals` mechanism (a fixture with no `source` cannot
  prove intent and is treated as born-broken).
- `pipeline/ci.py` — `_reverify_acceptance` re-runs the acceptance oracle at
  merge time against the rebased branch and refuses tampered fixtures
  WITHOUT running them (`_acceptance_tampered` compares worktree bytes to
  the dispatch-time pins); `_scope_test_cmd_to_acceptance` (defined in
  `pipeline/build_detect.py`, used here and by the oracle gate) scopes the
  detected test command to the fixture paths only.
- `pipeline/build_detect.py` — `_isolation_only_acceptance_warning` is the
  non-blocking isolation-only heuristic run at plan ingestion (rule (d)).
- `pipeline/test_author.py` — the executor-side test-authoring phase; it
  authors the agent's own unit tests and, by design, never touches or
  reviews acceptance fixtures (rule (a)'s separation of authorship).
- The dispatch path in `pipeline/dispatch.py` materializes the
  authoritative fixture source into the worktree pre-launch and records
  `acceptance_digests` on every dispatch (rule (b)).