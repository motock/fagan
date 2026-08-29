# Testing Configuration-Driven Logic and Resource Gates

> Migrated from CLAUDE.md's additive section of the same name to keep
> CLAUDE.md under its size limit. Two specific testing failure modes,
> generalized from production incidents, that the generic Testing section
> in CLAUDE.md doesn't call out by name. Extends CLAUDE.md; follows the
> same "prefer adding sections over editing invariant ones" rule.

## Test the resolution logic, not today's configured values

- When a test exercises code that resolves a setting from a config file,
  registry, or environment (e.g. "which provider/model does role X route
  to"), stub the config source with a synthetic fixture and assert against
  that fixture — never assert against whatever the real config currently
  contains.
- **Why:** an assertion against a live value ties the test to today's
  configuration. The next legitimate config change breaks every test that
  asserted the old value, for no defect in the code — and reverting the
  config later requires reverting the tests too. A registry, flag store, or
  settings file is meant to be freely reconfigurable; a test suite that
  breaks every time it's reconfigured has the coupling backwards.
- The one legitimate exception is a test asserting a *hardcoded fallback
  invariant* — e.g. "when the registry has no entry for role X, default to
  the safe built-in provider." Stub the registry as empty for that case and
  assert the fallback fires; that's a code-level guarantee independent of
  what's currently configured, not a live-value assertion.

## Validate any gate that can withhold work against the real environment

- For any check that can refuse or block work — a resource threshold, a
  rate limit, a quota check, a capacity gate — a green test suite proves
  the branch logic is correct, not that the threshold is survivable in
  production. If every test mocks the gate's input to a convenient value,
  nothing has ever evaluated the gate against a real reading.
- **Why:** this failure mode is silent by construction — nothing throws,
  nothing errors, the gate just returns "not ok" forever and the blocked
  work quietly stops happening. It can pass hundreds or thousands of green,
  fully-mocked tests while paralyzing the exact thing it was meant to
  protect, because no test ever fed it a realistic number.
- Before calling a new withholding gate done, run it against the real host
  or environment at least once and print the actual measured value next to
  the threshold. Prefer a stable signal over an instantaneous one (e.g. a
  resource ceiling that doesn't flap with unrelated concurrent load), and
  bias the gate toward never blocking something already known to work — if
  a genuinely-unservable case slips through, a downstream bounded retry or
  escalation path can catch it at the cost of one cycle; a gate that's
  wrong in the blocking direction costs everything behind it.