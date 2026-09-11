#!/usr/bin/env bash
# scripts/pipeline-env.sh — shared operator env chain for long-running
# pipeline processes (dashboard, scheduler).
#
# Sourceable helper: sources "$ROOT/.pipeline.env" if present, then
# "$ROOT/.dashboard.env" if present, both under `set -a` allexport so the
# sourced values reach detached child processes.  .dashboard.env is sourced
# SECOND so existing installs keep their current last-write precedence.
#
# The repo root is taken from the caller's ROOT variable when set
# (dashboard.sh and scheduler.sh resolve ROOT before sourcing this file).
#
# Allexport hygiene: this helper is SOURCED, so it runs inside the caller's
# shell — `set -a` must never outlive it, and the caller's pre-existing
# allexport mode must survive it too.  Save the caller's state first, do the
# sourcing, restore conditionally (a bare `set +a` here would strip a
# caller's pre-existing allexport — a leak in the other direction), then
# unset the temp var so nothing leaks.  The probe is guarded by `if`, never
# a bare `[[`, so it is safe under the caller's `set -euo pipefail`.

ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"

# Save the caller's allexport state (safe under the caller's `set -euo pipefail`).
__pipeline_env_allexport_was_on=0
if [[ $- == *a* ]]; then __pipeline_env_allexport_was_on=1; fi

# .pipeline.env first: the pipeline-wide operator overrides.
set -a
if [ -f "$ROOT/.pipeline.env" ]; then source "$ROOT/.pipeline.env"; fi

# .dashboard.env second: legacy dashboard-only overrides keep last-write
# precedence, so installs that already have one see no behaviour change.
if [ -f "$ROOT/.dashboard.env" ]; then source "$ROOT/.dashboard.env"; fi

# Restore the caller's allexport mode exactly as we found it, then drop the
# temp var so nothing leaks into the caller's shell.
if [[ $__pipeline_env_allexport_was_on == 1 ]]; then set -a; else set +a; fi
unset __pipeline_env_allexport_was_on
