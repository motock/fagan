#!/usr/bin/env bash
#
# Start / stop / status for the pipeline scheduler daemon
# (pipeline.scheduler_daemon).
#
#   scripts/scheduler.sh start    # run the daemon in the background
#   scripts/scheduler.sh stop     # SIGTERM the recorded pid
#   scripts/scheduler.sh restart  # stop then start
#   scripts/scheduler.sh status   # is it up?
#
# Operator-local overrides (.pipeline.env, gitignored — see
# .pipeline.env.example): sourced with allexport via the shared
# scripts/pipeline-env.sh helper before dispatch, so PLAN_DIR /
# WORKTREE_ROOT / PIPELINE_AUTONOMY / provider routing vars survive a
# restart from a bare shell and reach the detached daemon's environment.
# Sourcing overwrites caller-exported env — the file is durable operator
# intent.
#
# Artifacts in repo root:
#   .scheduler.pid   pid of the running scheduler daemon
#   scheduler.log    combined stdout+stderr from the daemon
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Shared operator env chain: .pipeline.env then .dashboard.env, both under
# set -a so sourced values are exported and inherited by the detached
# daemon spawned below.
. "$ROOT/scripts/pipeline-env.sh"

# Prefer the project venv (what scripts/install.sh creates); fall back to a
# python on PATH so the script also works in CI runners and any env without
# a local .venv (the scheduler module still has to be importable by
# whichever python wins).
if   [ -x "$ROOT/.venv/bin/python3" ]; then PYBIN="$ROOT/.venv/bin/python3"
elif [ -x "$ROOT/.venv/bin/python"  ]; then PYBIN="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1;  then PYBIN=python3
elif command -v python  >/dev/null 2>&1;  then PYBIN=python
else PYBIN=""; fi

# The scheduler is a single daemon with no port, so one fixed pid file in
# the repo root is enough (unlike the dashboard's per-port pid files).
PID_FILE="$ROOT/.scheduler.pid"
LOG_FILE="$ROOT/scheduler.log"

usage() {
  cat <<EOF
Usage: scripts/scheduler.sh <start|stop|restart|status>

Start, stop, restart, or report on the pipeline scheduler daemon
(pipeline.scheduler_daemon).

Env (via .pipeline.env, gitignored — see .pipeline.env.example):
  PLAN_DIR             where the pipeline reads/writes its plan
  WORKTREE_ROOT        root of the worktree the scheduler operates on
  PIPELINE_AUTONOMY    autonomy level for the scheduler loop

Operator-local overrides are sourced at start when present and override
caller-exported env, so scheduler config survives restarts from any shell.

Pid is written to .scheduler.pid and logs to scheduler.log in the repo root.
EOF
}

pid_alive() {
  # kill -0 succeeds if the pid exists and we can signal it. The redirect
  # suppresses the "No such process" stderr; an empty exit code is the
  # signal we care about.
  kill -0 "$1" 2>/dev/null
}

read_pid() {
  if [ -f "$PID_FILE" ]; then
    cat "$PID_FILE"
  else
    echo ""
  fi
}

is_running() {
  local pid
  pid="$(read_pid)"
  [ -n "$pid" ] && pid_alive "$pid"
}

cleanup_pid_file() {
  # Only remove the pid file if it still points at a live process; if not,
  # treat it as stale and drop it.
  local pid
  pid="$(read_pid)"
  if [ -z "$pid" ] || ! pid_alive "$pid"; then
    rm -f "$PID_FILE"
    return 0
  fi
  return 1
}

ensure_python () {
  if [ -n "$PYBIN" ]; then
    return 0
  fi
  echo "ERROR: no python found (.venv/bin/python or python3 on PATH). Run scripts/install.sh first." >&2
  exit 1
}

cmd_start() {
  ensure_python

  if is_running; then
    local pid
    pid="$(read_pid)"
    echo "already running, pid $pid"
    exit 0
  fi

  # Stale pid file from a previous crash? Drop it before we start.
  if [ -f "$PID_FILE" ]; then
    echo "==> removing stale pid file $(basename "$PID_FILE")"
    rm -f "$PID_FILE"
  fi

  echo "==> starting scheduler daemon"
  # Spawn detached via Python so we get a fresh session / process group on
  # both Linux and macOS without relying on `setsid(1)` being installed.
  # The Python helper exec()s into the daemon; the recorded pid is therefore
  # the daemon process itself (so kill -TERM $pid works in stop).
  # The helper script is passed via -c (no heredoc) and stdin is /dev/null
  # so a test harness that controls the parent's pipe can't hang the start.
  "$PYBIN" -c '
import os, sys
log_file = sys.argv[1]
log_fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.setsid()
os.dup2(log_fd, 1)
os.dup2(log_fd, 2)
devnull = os.open(os.devnull, os.O_RDONLY)
os.dup2(devnull, 0)
args = [sys.executable, "-m", "pipeline.scheduler_daemon"]
os.execvp(sys.executable, args)
' "$LOG_FILE" \
    < /dev/null >>"$LOG_FILE" 2>&1 &
  local pid=$!

  echo "$pid" >"$PID_FILE"
  # Brief settle so the master pid is observable. If the daemon died
  # immediately (bad config, import error) the pid file is still a useful
  # breadcrumb — stop will treat it as stale and clean it up.
  sleep 0.2
  if pid_alive "$pid"; then
    echo "started, pid $pid"
  else
    echo "started, pid $pid (process exited early — see $LOG_FILE)"
  fi
}

cmd_stop() {
  local pid
  pid="$(read_pid)"

  if [ -z "$pid" ] || ! pid_alive "$pid"; then
    # No live process — clean up any stale pid file and report no-op.
    [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
    echo "not running"
    return 0
  fi

  echo "==> stopping scheduler (pid $pid)"
  # The daemon is started in its own session/process group (see cmd_start),
  # so killing -$pid sends the signal to the whole group: master + any
  # children. Try SIGTERM first, escalate to SIGKILL if needed.
  local signal=TERM
  while :; do
    kill -"$signal" -- "-$pid" 2>/dev/null || kill -"$signal" "$pid" 2>/dev/null || true

    local waited=0
    while pid_alive "$pid" && [ "$waited" -lt 5 ]; do
      sleep 1
      waited=$((waited + 1))
    done

    if ! pid_alive "$pid"; then
      break
    fi
    if [ "$signal" = "KILL" ]; then
      # Already escalated; nothing more we can do politely.
      break
    fi
    echo "==> pid $pid ignored SIGTERM; sending SIGKILL"
    signal=KILL
  done

  rm -f "$PID_FILE"
  echo "stopped, pid $pid"
}

cmd_status() {
  local pid
  pid="$(read_pid)"
  if [ -n "$pid" ] && pid_alive "$pid"; then
    echo "running, pid $pid"
    return 0
  fi
  # Stale pid file → tidy up and report down.
  [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
  echo "not running"
  return 1
}

cmd_restart() {
  cmd_stop
  cmd_start
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  status)  cmd_status ;;
  restart) cmd_restart ;;
  usage|-h|--help) usage ;;
  "")
    usage
    exit 1
    ;;
  *)
    echo "unknown subcommand: $1" >&2
    usage
    exit 2
    ;;
esac
