# Code Review

> Migrated from CLAUDE.md's "Code Review" section (not one of the template's
> keep-as-is invariant sections) to keep CLAUDE.md under its size limit.
> Referenced from the Agent Workflow (Steps 4 and 7) — this is the checklist
> and standard the review gate enforces.

Code review is the last quality gate before code enters the shared codebase. Its purpose is to catch what tests cannot: design problems, unclear intent, missing edge cases, security concerns, and drift from team standards.

## What a reviewer is signing off on

Approving a pull request is a statement that the reviewer has verified all of the following:

- The change does what it claims to do
- Tests cover the new behavior, including negative and boundary cases
- No obvious security issues — injection, unvalidated input, exposed secrets, excessive permissions
- The implementation is consistent with the architecture and style of the surrounding code
- The commit message accurately describes the change
- If the change alters externally visible behavior (API contracts, configuration, CLI flags, user-facing functionality), documentation is updated — request changes and name the specific doc if it's missing, rather than approving with the gap unaddressed
- If the diff modifies or removes an existing test (see CLAUDE.md's Agent Workflow Step 4), the author's stated justification holds up: the new assertion reflects a real, requested behavior change — not a loosened or deleted check made just to pass. No justification present is itself a **Blocking** finding

An approver who has not checked these items should not approve.

## Blocking vs. non-blocking feedback

Be explicit about the weight of your feedback so the author can prioritize:

| Label | Meaning |
|---|---|
| **Blocking** | Must be addressed before merge. The change is incorrect, unsafe, or violates a standard. |
| **Suggestion** | Worth considering but will not block merge. Author decides. |
| **Nit** | Minor style or wording preference. Author may ignore. |

Default to `Suggestion` when in doubt. Reserve `Blocking` for genuine problems — overusing it trains authors to discount all feedback.

## Pull request size

Small, focused pull requests are easier to review, less risky to merge, and produce more useful feedback.

- **Aim for PRs under 400 lines of changed code.** This is a guideline, not a hard limit — a well-scoped 600-line change is better than five artificial splits — but consistently large PRs indicate a scoping problem.
- **One concern per PR.** A PR that fixes a bug *and* refactors a module *and* updates dependencies is three PRs. Mixing concerns makes review harder and rollback nearly impossible.
- **Separate mechanical changes from behavioral ones.** Refactors, formatting fixes, and dependency updates should not be bundled with feature work. When something breaks, you need to identify which change caused it.
- If a task genuinely requires a large change, break it into a sequence of reviewable steps merged incrementally to `main`.

## Reviewing AI-generated code

AI-generated code requires the same scrutiny as human-written code. Additionally:

- Verify the AI did not add unrequested features, quietly remove behavior, or silently refactor adjacent code
- Check that tests were written before the implementation (per the Agent Workflow), not retrofitted after
- Be skeptical of plausible-looking code that has not been exercised against the actual system — AI can generate syntactically correct code that is logically wrong
- Security-sensitive changes (auth, payments, data access) require human review regardless of source or apparent quality

## Merge-gate and AI-review lessons from production incidents

Four specific, non-obvious lessons distilled from real incidents in this
project's autonomous-dispatch history — general enough to apply to any
review/CI pipeline gating a merge, AI-driven or not:

- **A green test suite proves the diff's branch logic, not spec-completeness.**
  An AI executor (and a rushed human) reliably converges to the *minimum*
  edit that turns its own tests green, then stops — anything not covered by
  an assertion is liable to be left half-done (a rename applied in one
  place but not another, a doc comment never updated, a second call site
  never migrated). Only a reviewer who diffs against the actual requirement
  — not just "did the tests pass" — catches the gap. When re-reviewing a
  rework round, require each prior Blocking finding to be discharged
  individually against the new diff; do not treat "the suite is green now"
  as evidence a specific named finding was actually fixed.
- **An AI-authored test can be self-consistently wrong.** A test existing
  and passing is necessary but not sufficient: an executor that misreads a
  boundary condition (an off-by-one, an inclusive/exclusive edge) can write
  a fully green test suite that encodes the *same* mistake as the
  implementation, so the test confirms the bug instead of catching it.
  Review sensitive numeric/boundary logic by re-deriving the correct
  behavior yourself, not by confirming a test exists and is green.
- **Pin the exact version of any tool whose exit code gates a merge**
  (linters, formatters, type checkers) — a floor constraint (`>=`) lets an
  upstream release silently change what "clean" means mid-flight, and a
  version mismatch between a local environment and CI then gets
  misdiagnosed as a code or capability problem when it's actually an
  environment drift problem. Keep the pinned version identical everywhere
  the check runs.
- **A stuck agent's own resumed context can be the actual blocker**, not a
  capability ceiling. If a dispatched agent churns or regresses across
  repeated rework attempts, try once with a fresh context (a clean restart
  from the last-known-good state, briefed only on what's left to do) before
  concluding the model or approach can't do the task — a long, cluttered
  transcript full of its own prior confusion can itself be what's
  producing more confusion.