#!/bin/bash
# Stops ALL benchmark-related processes on this host and frees their memory:
# any matrix.py / harness.py, and the model servers they spun up
# (mlx_server_wrapper / mlx_lm.server, and Ollama's llama-server).
#
# Why this exists (not just `kill <chain-pid>`): killing a chain's parent
# process does NOT kill its already-exec'd children — they reparent to init
# (PPID 1) and keep running, still holding model servers in memory. On this
# 24GB Mac that orphan-residency is exactly how mlx-server (~14GB) ended up
# resident at the same time as Ollama's gpt-oss:20b (~14GB) on 2026-07-16,
# driving free RAM toward zero. A pattern-kill catches those orphans
# regardless of which chain (if any) is still their parent — which a
# process-group kill would miss once the group leader is already dead.
#
# Use this instead of `kill <chain-pid>` whenever you need to stop a run early,
# and re-run it (or `pgrep -f "matrix.py|harness.py|llama-server"`) to confirm
# nothing remains before starting a new run. It intentionally leaves the
# Ollama *app daemon* (`ollama serve`) alive — only the loaded model child
# (`llama-server`) is killed.
set -uo pipefail

PATTERNS="matrix\.py|harness\.py|mlx_server_wrapper|mlx_lm\.server|llama-server"

# Unload the mlx-supervisor watchdog first so it doesn't relaunch mlx-server
# the instant we kill it.
PLIST=~/Library/LaunchAgents/com.fagan.pipeline.mlx-supervisor.plist
if launchctl list 2>/dev/null | grep -q com.fagan.pipeline.mlx-supervisor; then
    launchctl unload "$PLIST" 2>/dev/null
    echo "unloaded mlx-supervisor launchd watchdog"
fi

list_pids() {
    ps -axo pid,command | grep -E "$PATTERNS" | grep -v grep | awk '{print $1}' | sort -u
}

PIDS=$(list_pids)
if [ -z "$PIDS" ]; then
    echo "no benchmark/model-server processes found"
    exit 0
fi

echo "found benchmark/model-server processes:"
ps -axo pid,ppid,etime,rss,command | grep -E "$PATTERNS" | grep -v grep

# SIGTERM first so processes can clean up / so Ollama stops getting requests
# (the matrix.py/harness.py requesters die, then llama-server can exit
# instead of being respawned by a still-requesting client).
kill $PIDS 2>/dev/null
echo "sent SIGTERM to: $(echo $PIDS | tr '\n' ' ')"

sleep 2
REMAIN=$(list_pids)
if [ -n "$REMAIN" ]; then
    kill -9 $REMAIN 2>/dev/null
    echo "sent SIGKILL to stragglers: $(echo $REMAIN | tr '\n' ' ')"
fi

# Final confirmation.
FINAL=$(list_pids)
if [ -n "$FINAL" ]; then
    echo "WARNING: still running after SIGKILL:" >&2
    ps -axo pid,ppid,command | grep -E "$PATTERNS" | grep -v grep >&2
    exit 1
fi
echo "all benchmark/model-server processes stopped"