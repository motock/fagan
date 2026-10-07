#!/usr/bin/env bash
# One-command driver for the published benchmark grid.
#
# Runs the fixed published task x arm grid in two phases - the main arms into
# DIR/main, then the step arm into DIR/steps - and records provenance
# (repo sha, dirty flag, start time, grid, registry path + sha256) in
# DIR/run_meta.json BEFORE anything runs. See README "Published run".
#
# Usage: run_published_matrix.sh --workdir DIR [--dry-run]
set -euo pipefail

# --- the published grid, defined once ---------------------------------------
TASKS=(cron_field interval_merge lru_cache retry_backoff token_bucket ratelimiter_bugfix inventory_pagination)
MAIN_ARMS=(sonnet glm_claude_review gptoss_claude_review_s60)
STEP_ARM=(gptoss_claude_review_s120)
TRIALS=3

# Resolve everything against this script's own directory, never the caller's
# cwd: matrix.py imports the app package and the repo's dependencies.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

usage() {
  cat >&2 <<'EOF'
usage: run_published_matrix.sh --workdir DIR [--dry-run]

Runs the published benchmark grid: the main arms over every published task
into DIR/main, then the step arm into DIR/steps. DIR/run_meta.json is written
before anything runs.

  --workdir DIR   output directory for the run (required)
  --dry-run       print the commands that would run, and run nothing
EOF
}

WORKDIR=""
DRY_RUN=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --workdir)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      WORKDIR="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

[ -n "$WORKDIR" ] || { usage; exit 2; }

# matrix.py needs the repo's venv (it imports the app package); the system
# python3 is only enough for the stdlib-only run_meta.json writer below.
VENV_PY="$REPO_ROOT/.venv/bin/python"
if [ -z "${PY:-}" ]; then
  if [ -f "$VENV_PY" ]; then
    PY="$VENV_PY"
  else
    PY="python3"
  fi
fi

# --- provenance, written before anything runs -------------------------------
# Absolutize DIR: matrix.py runs with cwd set to this script's directory, so a
# caller-relative --workdir would otherwise mean two different directories.
mkdir -p "$WORKDIR"
WORKDIR="$(cd "$WORKDIR" && pwd)"

REGISTRY_PATH="${PIPELINE_MODEL_REGISTRY_PATH:-}"
[ -n "$REGISTRY_PATH" ] || REGISTRY_PATH="$REPO_ROOT/model_registry.json"

REPO_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
  REPO_DIRTY=true
else
  REPO_DIRTY=false
fi

BPM_TASKS="$(printf '%s\n' "${TASKS[@]}")"
BPM_MAIN_ARMS="$(printf '%s\n' "${MAIN_ARMS[@]}")"
BPM_STEP_ARM="$(printf '%s\n' "${STEP_ARM[@]}")"
export BPM_TASKS BPM_MAIN_ARMS BPM_STEP_ARM
export BPM_TRIALS="$TRIALS"
export BPM_REPO_SHA="$REPO_SHA"
export BPM_REPO_DIRTY="$REPO_DIRTY"
export BPM_REGISTRY_PATH="$REGISTRY_PATH"
export BPM_META_PATH="$WORKDIR/run_meta.json"

python3 -c '
import hashlib
import json
import os
from datetime import datetime, timezone


def _lines(name):
    return [ln for ln in os.environ[name].splitlines() if ln]


registry = os.environ["BPM_REGISTRY_PATH"]
try:
    with open(registry, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
except OSError:
    digest = None

meta = {
    "repo_sha": os.environ["BPM_REPO_SHA"],
    "repo_dirty": os.environ["BPM_REPO_DIRTY"] == "true",
    "started_utc": datetime.now(timezone.utc).isoformat(),
    "tasks": _lines("BPM_TASKS"),
    "main_arms": _lines("BPM_MAIN_ARMS"),
    "step_arm": _lines("BPM_STEP_ARM"),
    "trials": int(os.environ["BPM_TRIALS"]),
    "registry": {"path": registry, "sha256": digest},
}

with open(os.environ["BPM_META_PATH"], "w") as fh:
    json.dump(meta, fh, indent=2)
    fh.write("\n")
'

# --- the two phases ---------------------------------------------------------
run_phase() {
  local workdir="$1"
  shift
  local -a cmd=(
    "$PY" matrix.py
    --tasks "${TASKS[@]}"
    --models "$@"
    --trials "$TRIALS"
    --workdir "$workdir"
    --resume
  )
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY-RUN: %s\n' "${cmd[*]}"
  else
    ( cd "$SCRIPT_DIR" && "${cmd[@]}" )
  fi
}

run_phase "$WORKDIR/main" "${MAIN_ARMS[@]}"
run_phase "$WORKDIR/steps" "${STEP_ARM[@]}"
