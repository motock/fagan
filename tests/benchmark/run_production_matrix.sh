#!/bin/bash
# Runs the benchmark matrix under settings that mimic a real production
# pipeline run, instead of the harness's fast/cheap defaults:
#
#   PIPELINE_DECOMPOSE=cloud                  a Claude planner writes a
#                                              per-story checklist before the
#                                              local model implements (the
#                                              production-intended path -
#                                              "local" mode is an ablation arm)
#   PIPELINE_DECOMPOSE_SCRATCHPAD=on           persistent cross-step scratchpad
#   PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE=3      benchmark tasks all carry an
#   PIPELINE_REWORK_MAX_ATTEMPTS=3             acceptance oracle, which caps
#   PIPELINE_REWORK_MAX_ATTEMPTS_ESCALATED=3   rework at just 1 attempt by
#                                              default (a deliberate fast-
#                                              converge default, see
#                                              pipeline_mcp_server.py's
#                                              REWORK_MAX_ATTEMPTS_ORACLE) -
#                                              override all three so the run
#                                              gets the full 3-attempt
#                                              implementer<->reviewer budget
#                                              production actually allows.
#   --timeout 5400                            longer per-cell wall-clock
#                                              budget than matrix.py's 3600s
#                                              default, to leave room for the
#                                              extra planning step + up to 3
#                                              rework round-trips.
#
# Reviewer is left at the harness default (Claude) for both arms of any
# comparison - only the implementer model should vary between runs.
#
# Hardening (2026-07-16): set -e, a preflight guard (refuses to start if any
# benchmark/model-server process is already running), and a TERM/INT trap that
# tears down children via stop_benchmark.sh instead of orphaning them. See
# _prod_run_chain.sh for the rationale.
#
# Usage:
#   ./run_production_matrix.sh <model> [workdir] [-- extra matrix.py args]
#   ./run_production_matrix.sh mlx
#   ./run_production_matrix.sh gptoss _runs/prod_gptoss_custom
#   ./run_production_matrix.sh mlx _runs/prod_mlx_smoke -- --tasks cron_field --trials 1
#
# To chain multiple models sequentially (e.g. proving out model A, then B,
# under identical production settings), just call this script back-to-back:
#   ./run_production_matrix.sh mlx && ./run_production_matrix.sh gptoss
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${1:-}" ]; then
    echo "usage: $0 <model> [workdir] [-- extra matrix.py args]" >&2
    exit 2
fi

MODEL="$1"; shift
WORKDIR="${1:-_runs/prod_${MODEL}_$(date +%Y%m%d_%H%M%S)}"
if [ "${1:-}" ]; then shift; fi
if [ "${1:-}" = "--" ]; then shift; fi

# Stale benchmark DRIVERS always block (a half-dead previous run, any model).
DRIVER_PATTERNS="matrix\.py|harness\.py"
# The model server that would DUAL-LOAD with THIS leg: the *other* model, not
# this leg's own server (which is expected to already be up — e.g. mlx-server
# for an mlx run — the harness assumes it's listening before the run starts).
# Blocking on this leg's own server would make the run unstartable.
case "$MODEL" in
    mlx*)  OPPONENT_PATTERNS="llama-server" ;;            # Ollama model already loaded -> conflicts with mlx
    *)     OPPONENT_PATTERNS="mlx_server_wrapper|mlx_lm\.server" ;;  # mlx already loaded -> conflicts with Ollama
esac

preflight() {
    local drivers opponents
    drivers=$(ps -axo pid,command | grep -E "$DRIVER_PATTERNS" | grep -v grep || true)
    opponents=$(ps -axo pid,command | grep -E "$OPPONENT_PATTERNS" | grep -v grep || true)
    if [ -n "$drivers" ] || [ -n "$opponents" ]; then
        echo "ERROR: stale benchmark processes or opposing model server already running — refusing to start:" >&2
        [ -n "$drivers" ]   && { echo "  stale drivers:";   echo "$drivers"   | sed 's/^/    /'; }
        [ -n "$opponents" ] && { echo "  opposing model server (would dual-load with $MODEL):"; echo "$opponents" | sed 's/^/    /'; }
        echo "  Run ./stop_benchmark.sh to clean them up, then retry." >&2
        exit 1
    fi
}

trap 'echo "=== run interrupted, tearing down all benchmark procs $(date) ===" >&2; ./stop_benchmark.sh || true' TERM INT

preflight

export PIPELINE_DECOMPOSE=cloud
export PIPELINE_DECOMPOSE_SCRATCHPAD=on
export PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE=3
export PIPELINE_REWORK_MAX_ATTEMPTS=3
export PIPELINE_REWORK_MAX_ATTEMPTS_ESCALATED=3
# Route an acceptance-failing dispatch that produced real work to review
# instead of straight to "failed", so the reviewer evaluates the failing
# submission and the rework loop above (REWORK_MAX_ATTEMPTS=3) retries the
# model with the reviewer's feedback. Without this, every acceptance-failing
# cell parks at "failed" before reaching review, so the rework budget and the
# GLM reviewer below never run (observed: 0/9 mlx cells reached review, zero
# reviewer usage). Opt-in here so the production-mimicking run exercises the
# full review+rework safety net; the merge gate still blocks any
# APPROVEd-but-failing merge.
export PIPELINE_REVIEW_ON_ACCEPTANCE_FAIL=1

# Reviewer: glm-5.2:cloud (cloud-routed via Ollama — `ollama list` shows SIZE
# "-", i.e. NOT a local model file), so it consumes NO local memory and is NOT
# gated by the Claude usage gate. Used as the Claude-usage-conscious reviewer
# so a production comparison can run when Claude review quota is exhausted
# (2026-07-16: the mlx leg stalled 40+ min on "Review backend gated (Claude
# usage gate tripped): deferring review" — FM-H, persistent reviewer
# rate-limit). Because it's cloud-routed it does NOT spawn a local
# llama-server, so it cannot dual-load with the local implementer (mlx-server
# on the mlx leg, gpt-oss:20b on the gptoss leg). Both legs use this SAME
# reviewer, so the comparison stays apples-to-apples — only the implementer
# varies. To revert to the Claude reviewer, unset these two vars.
export PIPELINE_BACKEND_REVIEW=local
export PIPELINE_LOCAL_REVIEW_MODEL=glm-5.2:cloud

echo "=== production run: model=$MODEL workdir=$WORKDIR starting $(date) ===" >&2
# `cmd && status=0 || status=$?` captures the exit code WITHOUT letting set -e
# abort before the finishing log line, so the user always sees the end banner.
python3 matrix.py --models "$MODEL" --workdir "$WORKDIR" --timeout 5400 "$@" && status=0 || status=$?
echo "=== production run: model=$MODEL workdir=$WORKDIR finished $(date), exit=$status ===" >&2
exit $status