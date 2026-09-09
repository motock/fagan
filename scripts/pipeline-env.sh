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

ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"

# .pipeline.env first: the pipeline-wide operator overrides.
set -a
if [ -f "$ROOT/.pipeline.env" ]; then source "$ROOT/.pipeline.env"; fi
set +a

# .dashboard.env second: legacy dashboard-only overrides keep last-write
# precedence, so installs that already have one see no behaviour change.
set -a
if [ -f "$ROOT/.dashboard.env" ]; then source "$ROOT/.dashboard.env"; fi
set +a
