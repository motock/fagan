# 0004. Safety gates default on and fail closed

- **Status:** Accepted
- **Date:** 2026-09-11

## Context

This pipeline can merge code with no human present. Every gate it has - the CI
check, the merge gate, the overlord's risk tiers, the sandbox - is also an
obstacle when something upstream breaks, so each needs an escape hatch. Escape
hatches are where safety systems go to die: the outage ends, the override stays.

## Decision

Every gate defaults to enabled when its setting is unset, and any unexpected
error inside a gate lands in the denied or closed state rather than the
permissive one. Disabling a gate requires an explicit opt-out value.

## Consequences

Concretely: `PIPELINE_MERGE_CI_GATE` is read as
`os.environ.get(..., "1") != "0"`, so absent configuration means gated.
`PIPELINE_SANDBOX` ships `'none'`, but when Docker sandboxing is configured and
the binary is missing, dispatch is refused rather than silently falling back to
unsandboxed execution. The overlord's park-and-ping tier stops for a human on
irreversible, security, money and production-config decisions regardless of
autonomy level.

Learned the hard way on 2026-09-11: defaulting on is necessary but not
sufficient, because an opt-out that reports its skipped check as passing is
invisible. `PIPELINE_MERGE_CI_GATE=0` was set as a deliberate workaround while
GitHub Actions was billing-blocked, and survived roughly five weeks - through
the repo going public and CI becoming usable again - because `_ci_status`
returned `{"state": "pass"}` when the gate was off and nothing anywhere warned.
Every merge in that window skipped CI while appearing to pass it.

So the rule has a second half: opting out must be loud. A disabled gate is now
surfaced as a warn-level preflight check and logged at each skip site. Cost: an
operator in a genuine outage sees warnings they cannot immediately act on, which
is the correct trade - a gate that is impossible to miss, not impossible to
disable.