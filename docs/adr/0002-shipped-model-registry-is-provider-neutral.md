# 0002. The shipped model registry is provider-neutral

- **Status:** Accepted
- **Date:** 2026-09-11

## Context

`model_registry.json` declares two things: which models exist per provider, and
which provider/model each role defaults to. A registry shipped carrying the
maintainer's own routing would point a fresh clone at providers the user has not
installed, authorized, or agreed to pay for - and would do so silently, since
role resolution has no user-facing confirmation step.

## Decision

We ship `model_registry.json` with `providers` declared but `roles` and
`routing` absent. Operator-specific routing lives in `model_registry.local.json`
- gitignored - and is selected with `PIPELINE_MODEL_REGISTRY_PATH`.

## Consequences

A fresh clone routes nothing by default: provider selection is an explicit setup
step, documented in the README, not a default that happens to work on the
maintainer's machine.

`PIPELINE_MODEL_REGISTRY_PATH` **replaces** the registry file wholesale rather
than merging with it, so the local file must contain the complete `providers`
block, not just a `roles` fragment. `load_registry` fails closed: a `roles` entry
naming a provider absent from `providers` raises rather than silently resolving
elsewhere.

Cost: the maintainer runs with a configuration nobody else has, so
"works on my machine" is the default state rather than the exception. The
gitignored local file exists specifically so personal routing cannot be
committed by accident - a real near-miss, since editing the tracked file
directly is the obvious thing to do and leaves the change staged for the next
`git add -A`.