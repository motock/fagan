#!/usr/bin/env bash
set -euo pipefail

# Determine the script's own repository root (the directory containing this script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$SCRIPT_DIR"
OUT_DIR=""
MLX_MODEL_PATH="${MLX_MODEL_PATH:-}"

# Parse optional flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo-root)
            REPO_ROOT="$2"
            shift 2
            ;;
        --out-dir)
            OUT_DIR="$2"
            shift 2
            ;;
        --mlx-model-path)
            MLX_MODEL_PATH="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# Fail closed if no mlx model path provided and environment variable unset/empty
if [[ -z "$MLX_MODEL_PATH" ]]; then
    echo "error: --mlx-model-path not given and MLX_MODEL_PATH env var is unset or empty" >&2
    exit 1
fi

# --out-dir defaults to the RESOLVED --repo-root's launchd/ dir (not this
# script's own SCRIPT_DIR) - must be computed after flag parsing, otherwise a
# caller passing --repo-root without --out-dir would silently write into
# this repo's own committed launchd/ directory instead of the target repo's.
if [[ -z "$OUT_DIR" ]]; then
    OUT_DIR="${REPO_ROOT}/launchd"
fi

# Ensure output directory exists
mkdir -p "$OUT_DIR"

TEMPLATE_DIR="${SCRIPT_DIR}/launchd"

declare -A KINDS=(
    [advance-scheduler]=com.claude.pipeline.advance-scheduler.plist
    [usage-poller]=com.claude.pipeline.usage-poller.plist
    [mlx-supervisor]=com.claude.pipeline.mlx-supervisor.plist
)

for kind in "${!KINDS[@]}"; do
    src="${TEMPLATE_DIR}/com.claude.pipeline.${kind}.plist.template"
    dst="${OUT_DIR}/${KINDS[$kind]}"
    sed -e "s|{{REPO_ROOT}}|${REPO_ROOT}|g" \
        -e "s|{{HOME}}|${HOME}|g" \
        ${MLX_MODEL_PATH:+-e "s|{{MLX_MODEL_PATH}}|${MLX_MODEL_PATH}|g"} \
        "$src" > "$dst"
done

chmod +x "$0"
