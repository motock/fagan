#!/usr/bin/env bash
set -euo pipefail

# Determine the script's own repository root (the directory containing this script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$SCRIPT_DIR"
OUT_DIR=""

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
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# --out-dir defaults to the RESOLVED --repo-root's systemd/ dir (not this
# script's own SCRIPT_DIR) - must be computed after flag parsing, otherwise a
# caller passing --repo-root without --out-dir would silently write into
# this repo's own committed systemd/ directory instead of the target repo's.
if [[ -z "$OUT_DIR" ]]; then
    OUT_DIR="${REPO_ROOT}/systemd"
fi

# Ensure output directory exists
mkdir -p "$OUT_DIR"

TEMPLATE_DIR="${SCRIPT_DIR}/systemd"

# Parallel list (not `declare -A`) on purpose, matching
# scripts/generate_launchd_plists.sh's own reasoning: macOS ships bash 3.2 as
# /bin/bash and `declare -A` requires bash 4+. This generator does not need
# to run on macOS itself, but keeping the same style avoids a needless
# divergence between the two sibling generator scripts.
UNITS="com.fagan.pipeline.advance-scheduler.service com.fagan.pipeline.usage-poller.service com.fagan.pipeline.usage-poller.timer"

for unit in $UNITS; do
    src="${TEMPLATE_DIR}/${unit}.template"
    dst="${OUT_DIR}/${unit}"
    sed -e "s|{{REPO_ROOT}}|${REPO_ROOT}|g" \
        -e "s|{{HOME}}|${HOME}|g" \
        "$src" > "$dst"
done

# The logrotate config is templated the same way as the units and rendered
# with the SAME {{REPO_ROOT}} substitution as the loop above - it carries no
# {{HOME}} token.
LOGROTATE_SRC="${TEMPLATE_DIR}/pipeline-logs.logrotate.template"
LOGROTATE_DST="${OUT_DIR}/pipeline-logs.logrotate.conf"
sed -e "s|{{REPO_ROOT}}|${REPO_ROOT}|g" "$LOGROTATE_SRC" > "$LOGROTATE_DST"

chmod +x "$0"
