# 0016. Repository code runs through one command-wrap seam

- **Status:** Proposed
- **Date:** 2026-11-21

## Context

The pipeline runs repository-controlled code: agent harnesses, reviews, tests,
builds, setup, lint and validation commands. Today those run as plain
subprocesses beside the control plane's own credentials, in the same
environment and under the same user. An operator who needs a container,
a sandbox or a tenant boundary around that code has no single place to put
one: each spawn site builds its own argv, and only the docker sandbox
(`PIPELINE_SANDBOX=docker`) rewrites argv today, and only for harness spawns.

[ADR 0008](0008-shared-control-plane-per-repo-workers.md) moves repository
execution onto per-repo workers later. Until then, the control plane must
still be able to contain repository code on hosts that run it locally.

## Decision

We add one neutral argv-rewrite seam, `pipeline/tenant_exec.py`, selected by
`PIPELINE_COMMAND_WRAP_PLUGIN` through the plugin allowlist. Its contract is
`wrap(argv, cwd, kind, namespace) -> list[str]`, consulted before every command
that runs repository-controlled code.

The seam is off by default: with the variable unset, `wrap` returns the same
argv object unchanged and imports nothing. It fails closed: a plugin that
cannot be loaded, raises, or returns anything other than a non-empty list of
strings refuses the command, and the unwrapped argv is never used as a
fallback. Because a wrapper decides what repository code may touch, the
plugin loads only through the module allowlist (`PIPELINE_PLUGIN_MODULES`).

Core ships no wrapper of its own. The seam is a hook for the operator's
isolation; the pipeline does not claim that any particular wrapper is a
sandbox.

## Consequences

- Every new site that runs repository code must call the seam. A spawn that
  bypasses it is a gap in the containment this seam provides, and reviewers
  should reject it.
- The seam cannot be combined with ssh execution (`PIPELINE_EXEC_<ROLE>=ssh`)
  or with `PIPELINE_SANDBOX=docker`. Each combination raises before anything
  is spawned, because each would either skip the wrapper or wrap twice.
- A wrapper sees only the argv, the working directory, the command kind and
  the tenancy namespace. It never receives the environment, so it cannot read
  pipeline secrets through this seam, and it cannot rely on them being passed
  to it.
- A wrapper that is unavailable blocks every wrapped command. That is the
  intended cost of failing closed; an operator who wants the commands to run
  without the wrapper must unset the variable, not expect a fallback.
- The plugin object is built once per process. A change to the wrapper takes
  effect after a restart of the control plane.
