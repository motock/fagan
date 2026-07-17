#!/bin/bash
# Stops the running mlx_lm.server (via its wrapper) and unloads the launchd
# supervisor so it doesn't immediately relaunch it.
#
# Needed before starting any other memory-heavy local model (e.g. an Ollama
# model) on this 24GB host: MLX's own ~14GB soft memory limit plus another
# ~13GB local model can exceed physical RAM even though neither alone would -
# see the project_mlx_24gb_footprint_ceiling / project_production_mimicking_
# benchmark_recipe memory notes on the 2026-07-16 kernel panic this host hit
# under exactly that kind of memory pressure.
set -uo pipefail

PLIST=~/Library/LaunchAgents/com.claude.pipeline.mlx-supervisor.plist
if launchctl list 2>/dev/null | grep -q com.claude.pipeline.mlx-supervisor; then
    launchctl unload "$PLIST" 2>/dev/null
    echo "unloaded mlx-supervisor launchd job"
fi

PIDS=$(pgrep -f "scripts/mlx_server_wrapper.py" || true)
if [ -n "$PIDS" ]; then
    kill $PIDS
    echo "stopped mlx-server (pid(s): $PIDS)"
else
    echo "mlx-server was not running"
fi
