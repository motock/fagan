#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

TASKS=(cron_field interval_merge lru_cache retry_backoff token_bucket ratelimiter_bugfix inventory_pagination)
MAIN_ARMS=(sonnet glm_claude_review gptoss_claude_review_s60)
STEP_ARM=(gptoss_claude_review_s120)
TRIALS=3

usage() {
  cat >&2 <<EOF
usage: run_published_matrix.sh --workdir DIR [--dry-run]
EOF
}

WORKDIR=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --workdir)
      [ $# -ge 2 ] || { usage; exit 2; }
      WORKDIR="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) usage; exit 2 ;;
  esac
done
if [ -z "$WORKDIR" ]; then
  usage
  exit 2
fi

REG_PATH="${PIPELINE_MODEL_REGISTRY_PATH:-}"
if [ -z "$REG_PATH" ]; then
  REG_PATH="$REPO/model_registry.json"
fi

mkdir -p "$WORKDIR"
REG_PATH="$REG_PATH" python3 -c '
import datetime, hashlib, json, os, subprocess, sys
reg = os.environ["REG_PATH"]
sha = None
if os.path.isfile(reg):
    sha = hashlib.sha256(open(reg, "rb").read()).hexdigest()
dirty = bool(subprocess.run(
    ["git", "status", "--porcelain"], capture_output=True, text=True,
    cwd=os.environ["REPO"],
).stdout.strip())
meta = {
    "repo_sha": subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
        cwd=os.environ["REPO"],
    ).stdout.strip(),
    "repo_dirty": dirty,
    "started_utc": datetime.datetime.now(datetime.timezone.utc)
        .isoformat().replace("+00:00", "Z"),
    "tasks": sys.argv[1:8],
    "main_arms": sys.argv[8:11],
    "step_arm": sys.argv[11:12],
    "trials": 3,
    "registry": {"path": reg, "sha256": sha},
}
with open(sys.argv[12], "w") as fh:
    fh.write(json.dumps(meta, indent=2) + "\n")
' "${TASKS[@]}" "${MAIN_ARMS[@]}" "${STEP_ARM[@]}" "$WORKDIR/run_meta.json"

PY="${PY:-$REPO/.venv/bin/python}"
if [ ! -f "$PY" ]; then
  PY="python3"
fi

run_phase() {
  local workdir="$1"; shift
  local cmd=("$PY" matrix.py --tasks "${TASKS[@]}" --models "$@" \
    --trials "$TRIALS" --workdir "$workdir" --resume)
  if [ "$DRY_RUN" = "1" ]; then
    printf 'DRY-RUN: %s\n' "${cmd[*]}"
  else
    (cd "$SCRIPT_DIR" && "${cmd[@]}")
  fi
}

run_phase "$WORKDIR/main" "${MAIN_ARMS[@]}"
run_phase "$WORKDIR/steps" "${STEP_ARM[@]}"