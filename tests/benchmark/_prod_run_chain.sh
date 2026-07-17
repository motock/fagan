#!/bin/bash
# Runs the production-mimicking benchmark chain: MLX implementer leg, then
# (after tearing down mlx-server) the Ollama gpt-oss implementer leg. Both
# legs under PIPELINE_DECOMPOSE=cloud, scratchpad on, 3-attempt rework,
# 5400s per-cell timeout, Claude reviewer fixed across both.
#
# Hardening (2026-07-16, after the concurrent mlx+gpt-oss memory crisis):
#   * set -e           — a dying leg ABORTS the chain instead of silently
#                        falling through to the next leg (which would load a
#                        second ~14GB model on top of the first). Previously
#                        `set -uo pipefail` let a killed mlx matrix.py fall
#                        straight into the gptoss leg.
#   * preflight guard  — refuses to start if any benchmark/model-server
#                        process is already running, so a half-dead previous
#                        run can't silently overlap with a new one.
#   * trap on TERM/INT — killing THIS script cascades to all its children
#                        (matrix.py/harness.py + model servers) via
#                        stop_benchmark.sh, instead of orphaning them to init.
set -euo pipefail
cd "$(dirname "$0")"

# Preflight: block on stale benchmark DRIVERS (matrix.py/harness.py, any
# model) and on an already-loaded Ollama model (llama-server), which would
# dual-load with the mlx leg. mlx_server_wrapper is EXPECTED to already be up
# (the supervisor starts it; this chain does not), so it is deliberately NOT
# blocked here — blocking on it would make the chain unstartable.
PATTERNS="matrix\.py|harness\.py|llama-server"

preflight() {
    local found
    found=$(ps -axo pid,command | grep -E "$PATTERNS" | grep -v grep || true)
    if [ -n "$found" ]; then
        echo "ERROR: stale benchmark processes or Ollama model already loaded — refusing to start:" >&2
        echo "$found" >&2
        echo "Run ./stop_benchmark.sh to clean them up, then retry." >&2
        exit 1
    fi
}

# Killing this chain (SIGTERM/SIGINT) must tear down its children too, not
# orphan them. stop_benchmark.sh pattern-kills everything including orphans.
trap 'echo "=== chain interrupted, tearing down all benchmark procs $(date) ===" >&2; ./stop_benchmark.sh || true' TERM INT

preflight

export PIPELINE_DECOMPOSE=cloud
export PIPELINE_DECOMPOSE_SCRATCHPAD=on
export PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE=3
export PIPELINE_REWORK_MAX_ATTEMPTS=3
export PIPELINE_REWORK_MAX_ATTEMPTS_ESCALATED=3

echo "=== [1/2] mlx production run starting $(date) ==="
python3 matrix.py --models mlx --workdir _runs/prod_mlx_20260716 --timeout 5400 --resume
echo "=== [1/2] mlx production run finished $(date) ==="

echo "=== stopping mlx-server before starting the gptoss run (frees ~14GB before Ollama loads gpt-oss:20b) $(date) ==="
../../scripts/stop_mlx_server.sh

echo "=== [2/2] gptoss production run starting $(date) ==="
python3 matrix.py --models gptoss --workdir _runs/prod_gptoss_20260716 --timeout 5400 --resume
echo "=== [2/2] gptoss production run finished $(date) ==="

echo "=== CHAIN COMPLETE $(date) ==="