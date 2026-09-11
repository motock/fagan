# 0001. Review is the primary quality gate, not tests

- **Status:** Accepted
- **Date:** 2026-09-11

## Context

An autonomous coding agent converges on the minimum diff that turns its own
tests green, then stops. Worse, a test written by the same agent that wrote the
implementation can encode the same misunderstanding in both, so the suite
confirms the bug rather than catching it. A green suite is therefore evidence
that the diff's branch logic is self-consistent, not that the change is correct
or complete.

Separately, frontier-model tokens are metered and local-model inference is not.
That asymmetry is the project's central economic constraint.

## Decision

We put the expensive model where judgment happens - decomposition, planning,
review, adjudication - and the cheap model where keystrokes happen. We treat the
review gate, not the test suite, as the primary determinant of whether a change
is correct.

## Consequences

The empirical basis is Michael Fagan's 1976 IBM study of formal inspection,
which found inspection caught 82% of the defects in the released product - 38
per KLOC, against 8 per KLOC for unit testing. If quality lives in the gate
rather than the author, then a weaker implementer behind a strong gate is a
sound trade, which is what makes the cost split viable at all.

Follows from this:

- Review may run on a capable model even when dispatch does not.
- A green suite is explicitly insufficient for merge; the merge gate re-runs the
  suite against the rebased branch and polls real CI.
- An existing-test modification without stated justification is a Blocking
  review finding by default (`.claude/rules/code-review.md`), because loosening
  an inconvenient assertion is the cheapest way to fake a pass.

Costs: total spend scales with review passes rather than lines written, so a
story that needs several rework cycles can cost more than writing it by hand.
Review quality becomes the system's ceiling - a weak reviewer silently
re-admits every failure mode this decision exists to catch.