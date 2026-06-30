#!/usr/bin/env bash
#
# Start / stop / status for the read-only monitoring dashboard (dashboard.py).
#
#   scripts/dashboard.sh start    # run uvicorn in the background
#   scripts/dashboard.sh stop     # SIGTERM the recorded pid
#   scripts/dashboard.sh restart  # stop then start
#   scripts/dashboard.sh status   # is it up? on what port?
#
# Env (all optional):
#   DASHBOARD_HOST   default 127.0.0.1
#   DASHBOARD_PORT   default 8000
#   DASHBOARD_RELOAD if set to 1, also pass --reload to uvicorn (dev only;
#                    the script always starts uvicorn in its own session
#                    via `os.setsid()`, so `stop` kills the whole process
#                    group — reloader master and worker both).
#
# Artifacts in repo root:
#   .dashboard.pid   pid of the running uvicorn process (master when reload)
#   dashboard.log    combined stdout+stderr from uvicorn
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYBIN="$ROOT/.venv/bin/python3"
[ -x "$PYBIN" ] || PYBIN="$ROOT/.venv/bin/python"
PID_FILE="$ROOT/.dashboard.pid"
LOG_FILE="$ROOT/dashboard.log"

DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"

usage() {
  cat <<EOF
Usage: scripts/dashboard.sh <start|stop|restart|status>

Start, stop, restart, or report on the monitoring dashboard (dashboard.py)
running via uvicorn.

Env:
  DASHBOARD_HOST   (default 127.0.0.1)
  DASHBOARD_PORT   (default 8000)
  DASHBOARD_RELOAD 1 to pass --reload to uvicorn (dev only)

Pid is written to .dashboard.pid and logs to dashboard.log in the repo root.
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

ensure_python() {
  if [ ! -x "$PYBIN" ]; then
    echo "ERROR: $PYBIN not found. Run scripts/install.sh first." >&2
    exit 1
  fi
}

cmd_start() {
  ensure_python

  if is_running; then
    local pid
    pid="$(read_pid)"
    echo "already running, pid $pid, http://$DASHBOARD_HOST:$DASHBOARD_PORT"
    exit 0
  fi

  # Stale pid file from a previous crash? Drop it before we start.
  if [ -f "$PID_FILE" ]; then
    echo "==> removing stale pid file $(basename "$PID_FILE")"
    rm -f "$PID_FILE"
  fi

  local reload_flag=""
  if [ "${DASHBOARD_RELOAD:-}" = "1" ]; then
    reload_flag="--reload"
  fi

  echo "==> starting dashboard on http://$DASHBOARD_HOST:$DASHBOARD_PORT"
  # Spawn detached via Python so we get a fresh session / process group on
  # both Linux and macOS without relying on `setsid(1)` being installed.
  # The Python helper exec()s into uvicorn; the recorded pid is therefore
  # the uvicorn master itself (so kill -TERM $pid works in stop).
  # The helper script is passed via -c (no heredoc) and stdin is /dev/null
  # so a test harness that controls the parent's pipe can't hang the start.
  "$PYBIN" -c '
import os, sys
host, port, log_file, reload_flag = sys.argv[1:5]
log_fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.setsid()
os.dup2(log_fd, 1)
os.dup2(log_fd, 2)
devnull = os.open(os.devnull, os.O_RDONLY)
os.dup2(devnull, 0)
args = [sys.executable, "-m", "uvicorn", "dashboard:app",
        "--host", host, "--port", port]
if reload_flag:
    args.append("--reload")
os.execvp(sys.executable, args)
' "$DASHBOARD_HOST" "$DASHBOARD_PORT" "$LOG_FILE" "$reload_flag" \
    < /dev/null >>"$LOG_FILE" 2>&1 &
  local pid=$!

  echo "$pid" >"$PID_FILE"
  # Brief settle so the master pid is observable. If uvicorn died
  # immediately (port in use, bad config) the pid file is still a useful
  # breadcrumb — stop will treat it as stale and clean it up.
  sleep 0.2
  if pid_alive "$pid"; then
    echo "started, pid $pid, http://$DASHBOARD_HOST:$DASHBOARD_PORT"
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

  echo "==> stopping dashboard (pid $pid)"
  # uvicorn is started in its own session/process group (see cmd_start), so
  # killing -$pid sends the signal to the whole group: master + any worker
  # spawned by --reload. Try SIGTERM first, escalate to SIGKILL if needed.
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
    echo "running, pid $pid, http://$DASHBOARD_HOST:$DASHBOARD_PORT"
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
