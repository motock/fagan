#!/usr/bin/env bash
#
# scripts/standalone-setup.sh — provision and verify a standalone,
# dashboard-only pipeline instance with ONE supported command:
#
#   scripts/standalone-setup.sh up | down | status
#       [--repo-root DIR] [--data-dir DIR] [--target-repo DIR]
#       [--port PORT] [--autonomy MODE] [--force]
#
# `up` provisions the scratch data dir, writes the shared operator env file
# with ABSOLUTE paths, starts the dashboard and the scheduler through their
# existing helper scripts, and then REFUSES to report success unless
# GET /api/health answers with the intended plan_dir and an empty
# config_mismatch.  `down` stops both processes and leaves the scratch data
# in place.  `status` prints the resolved paths and both processes' state.
#
# Standalone means standalone: this script never registers an MCP server.
set -euo pipefail

# Internal state. REPO_ROOT is initialised empty on purpose so an ambient
# REPO_ROOT variable inherited from the caller's environment can never be
# mistaken for an explicit --repo-root override.
REPO_ROOT=""
VENV_PY=""
ENV_FILE=""

# --------------------------------------------------------------------------- #
# defaults (overridable via the options below)
# --------------------------------------------------------------------------- #
PORT="${PORT:-8001}"
DATA_DIR="${DATA_DIR:-$HOME/pipeline-standalone}"
TARGET_REPO="${TARGET_REPO:-}"
AUTONOMY="${AUTONOMY:-dry-run}"
FORCE=0

usage() {
  cat <<EOF
Usage: scripts/standalone-setup.sh <up|down|status> [options]

  up      provision the data dir, write the shared env file, start the
          dashboard and the scheduler, then verify the health endpoint
          agrees with the written configuration before reporting success
  down    stop the dashboard and the scheduler started by \`up\`
          (scratch data under the data dir is left in place, not deleted)
  status  print the resolved paths and both processes' state

Options:
  --repo-root DIR     repo to operate on (default: this checkout)
  --data-dir DIR      scratch data dir (default: ~/pipeline-standalone)
  --target-repo DIR   existing git repo to serve as the target
                      (default: a scratch repo under the data dir)
  --port PORT         dashboard port (default: 8001)
  --autonomy MODE     pipeline autonomy mode (default: dry-run)
  --force             overwrite an existing env file without backing it up
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

# --------------------------------------------------------------------------- #
# option parsing (before any subcommand work)
# --------------------------------------------------------------------------- #
parse_options() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --repo-root)
        [ "$#" -ge 2 ] || die "--repo-root requires a value"
        REPO_ROOT="$2"
        shift 2
        ;;
      --data-dir)
        [ "$#" -ge 2 ] || die "--data-dir requires a value"
        DATA_DIR="$2"
        shift 2
        ;;
      --target-repo)
        [ "$#" -ge 2 ] || die "--target-repo requires a value"
        TARGET_REPO="$2"
        shift 2
        ;;
      --port)
        [ "$#" -ge 2 ] || die "--port requires a value"
        PORT="$2"
        shift 2
        ;;
      --autonomy)
        [ "$#" -ge 2 ] || die "--autonomy requires a value"
        AUTONOMY="$2"
        shift 2
        ;;
      --force)
        FORCE=1
        shift
        ;;
      -h|--help|help)
        usage
        exit 0
        ;;
      *)
        echo "ERROR: unknown option: $1" >&2
        usage
        exit 2
        ;;
    esac
  done
}

# --------------------------------------------------------------------------- #
# shared resolution helpers
# --------------------------------------------------------------------------- #
resolve_repo_root() {
  if [ -n "$REPO_ROOT" ]; then
    if ! REPO_ROOT="$(cd "$REPO_ROOT" 2>/dev/null && pwd)"; then
      die "--repo-root: not a directory: $REPO_ROOT"
    fi
  else
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  fi
}

expand_data_dir() {
  # The env file is sourced with `set -a` and gets NO tilde expansion, so a
  # leading ~ is expanded here; the values written below must be absolute.
  case "$DATA_DIR" in
    "~"*) DATA_DIR="${DATA_DIR/#\~/$HOME}" ;;
  esac
}

# Absolutise DATA_DIR for the read-only subcommands (down/status), exactly the
# way `up` resolves it — but WITHOUT mkdir: a relative --data-dir re-resolves
# against each invocation's cwd, so state written by `up` would otherwise
# become invisible to `down`/`status` run from another directory, and `down`
# would report success while signalling nothing.
resolve_data_dir_readonly() {
  expand_data_dir
  if ! DATA_DIR="$(cd "$DATA_DIR" 2>/dev/null && pwd)"; then
    die "data dir not found: $DATA_DIR"
  fi
}

resolve_data_dir() {
  expand_data_dir
  mkdir -p "$DATA_DIR"
  DATA_DIR="$(cd "$DATA_DIR" && pwd)"
}

resolve_venv_python() {
  VENV_PY="$REPO_ROOT/.venv/bin/python"
  if [ ! -x "$VENV_PY" ]; then
    echo "ERROR: no .venv at $REPO_ROOT/.venv — run scripts/install.sh first" >&2
    exit 1
  fi
}

# --------------------------------------------------------------------------- #
# `up`: provision + launch + verify
# --------------------------------------------------------------------------- #
cmd_up() {
  resolve_repo_root
  resolve_venv_python
  resolve_data_dir

  echo "==> repo root: $REPO_ROOT"
  echo "==> data dir:  $DATA_DIR"

  # Scratch layout: plans + worktrees always; a scratch git repo only when
  # the caller did not supply their own target repo.
  mkdir -p "$DATA_DIR/plans" "$DATA_DIR/worktrees" "$DATA_DIR/agents"
  if [ -z "$TARGET_REPO" ]; then
    TARGET_REPO="$DATA_DIR/repo"
    git init -q "$DATA_DIR/repo"
  fi
  echo "==> target repo: $TARGET_REPO"

  AGENTS_DIR="$DATA_DIR/agents"

  # Provision the standalone instance's OWN persona set from the repo's
  # bundled agents/*.md, non-destructively (-n), so a fresh install never
  # depends on the operator's global ~/.claude/agents/ already being
  # populated. That directory's name is historical - persona files are
  # plain system-prompt templates read by _persona_body(), unrelated to
  # which dispatch backend actually executes a story; a pure-ollama setup
  # needs these exactly as much as a Claude-backed one. -n means a re-run
  # of `up` never clobbers an already-customized persona in $AGENTS_DIR.
  if [ -d "$REPO_ROOT/agents" ]; then
    cp -n "$REPO_ROOT"/agents/*.md "$AGENTS_DIR/" 2>/dev/null || true
  fi
  echo "==> provisioned personas: $AGENTS_DIR"

  PLAN_DIR="$DATA_DIR/plans"
  WORKTREE_ROOT="$DATA_DIR/worktrees"

  ENV_FILE="$REPO_ROOT/.pipeline.env"
  if [ -f "$ENV_FILE" ] && [ "$FORCE" -ne 1 ]; then
    BACKUP="$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
    cp "$ENV_FILE" "$BACKUP"
    echo "==> backed up existing env file to $BACKUP"
  fi
  {
    # Values are quoted: this file is sourced with `set -a`, and an unquoted
    # value containing spaces (e.g. --data-dir "/tmp/dash state") would be
    # parsed as an assignment prefix plus a bogus command name, silently
    # truncating the path downstream.
    echo "PLAN_DIR=\"$PLAN_DIR\""
    echo "WORKTREE_ROOT=\"$WORKTREE_ROOT\""
    echo "PIPELINE_AUTONOMY=\"$AUTONOMY\""
    echo "AGENTS_DIR=\"$AGENTS_DIR\""
  } >"$ENV_FILE"
  echo "==> wrote $ENV_FILE"

  # Both helper scripts source scripts/pipeline-env.sh, which sources the env
  # file written above with `set -a`, so both pick up PLAN_DIR /
  # WORKTREE_ROOT / PIPELINE_AUTONOMY.  The dashboard port is passed through
  # the DASHBOARD_PORT env var.
  echo "==> starting dashboard on port $PORT"
  DASHBOARD_PORT="$PORT" "$REPO_ROOT/scripts/dashboard.sh" start
  echo "==> starting scheduler"
  "$REPO_ROOT/scripts/scheduler.sh" start

  # Record our own pidfiles under the data dir so `down` only ever signals
  # processes THIS script started (never an operator's unrelated dashboard).
  DASH_PID="$(cat "$REPO_ROOT/.dashboard.${PORT}.pid" 2>/dev/null || true)"
  SCHED_PID="$(cat "$REPO_ROOT/.scheduler.pid" 2>/dev/null || true)"
  if [ -n "$DASH_PID" ]; then
    echo "$DASH_PID" >"$DATA_DIR/dashboard.pid"
  fi
  if [ -n "$SCHED_PID" ]; then
    echo "$SCHED_PID" >"$DATA_DIR/scheduler.pid"
  fi

  verify_health

  echo "==> standalone up: dashboard ready on http://127.0.0.1:$PORT (plan dir: $PLAN_DIR)"
}

# --------------------------------------------------------------------------- #
# verification: the dashboard must agree with the intended configuration
# --------------------------------------------------------------------------- #
verify_health() {
  local health_url="http://127.0.0.1:${PORT}/api/health"
  local key
  # The venv does NOT install the `app` package (install.sh only pip-installs
  # requirements*.txt; pyproject's pythonpath=["."] is pytest-only), so a bare
  # `"$VENV_PY" -c 'from app.auth import ...'` resolves `app` via the CALLER's
  # cwd: from anywhere but the repo root, `up` dies with ModuleNotFoundError
  # after both processes are already running — or silently reads a foreign
  # checkout's key.  Force cwd to $REPO_ROOT; a PYTHONPATH prefix alone would
  # not suffice, because for `python -c` the cwd (sys.path[0]) precedes
  # PYTHONPATH, so a foreign checkout's app/ would still shadow $REPO_ROOT.
  key="$(cd "$REPO_ROOT" && "$VENV_PY" -c 'from app.auth import get_or_create_api_key; print(get_or_create_api_key())')"

  local resp=""
  local attempts=60
  local i=1
  while [ "$i" -le "$attempts" ]; do
    if resp="$(curl -fsS -H "x-pipeline-api-key: $key" "$health_url" 2>/dev/null)"; then
      break
    fi
    sleep 1
    i=$((i + 1))
  done

  if [ -z "$resp" ]; then
    echo "ERROR: dashboard did not become healthy at $health_url after ${attempts}s" >&2
    exit 1
  fi

  # Parse the payload with the venv python (no jq dependency).
  local plan_dir mismatch
  plan_dir="$("$VENV_PY" -c 'import json, sys; print(json.load(sys.stdin).get("plan_dir", ""))' <<<"$resp" 2>/dev/null || true)"
  mismatch="$("$VENV_PY" -c 'import json, sys; print(",".join(json.load(sys.stdin).get("config_mismatch") or []))' <<<"$resp" 2>/dev/null || true)"

  if [ -n "$mismatch" ]; then
    echo "ERROR: configuration mismatch: $mismatch (dashboard reports plan_dir=$plan_dir, intended PLAN_DIR=$PLAN_DIR)" >&2
    exit 1
  fi
  if [ "$plan_dir" != "$PLAN_DIR" ]; then
    echo "ERROR: configuration mismatch: plan_dir (dashboard reports plan_dir=$plan_dir, intended PLAN_DIR=$PLAN_DIR)" >&2
    exit 1
  fi
}

# --------------------------------------------------------------------------- #
# `down`: stop both processes, keep the scratch data
# --------------------------------------------------------------------------- #
stop_recorded_pid() {
  # Only ever signal a pid recorded in OUR pidfile under the data dir — never
  # a pattern match, which could hit an operator's unrelated dashboard.
  local pidfile="$1"
  [ -f "$pidfile" ] || return 0
  local pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [ -n "$pid" ] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    # Give the process a moment to exit, then escalate to SIGKILL.
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 5 ]; do
      sleep 1
      waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
}

cmd_down() {
  resolve_repo_root
  resolve_data_dir_readonly
  # `down` must never report success while signalling nothing: if `up` never
  # recorded a pid here (wrong data dir, or the dir was never provisioned),
  # fail loudly instead of printing the success banner.
  if [ ! -f "$DATA_DIR/dashboard.pid" ] && [ ! -f "$DATA_DIR/scheduler.pid" ]; then
    die "no pid files under $DATA_DIR — \`up\` never recorded a dashboard or scheduler there; nothing to stop"
  fi
  echo "stopping dashboard"
  stop_recorded_pid "$DATA_DIR/dashboard.pid"
  echo "stopping scheduler"
  stop_recorded_pid "$DATA_DIR/scheduler.pid"
  echo "standalone down; scratch data kept in place at $DATA_DIR (not deleted)"
}

# --------------------------------------------------------------------------- #
# `status`: resolved paths + both processes' state (read-only, no side effects)
# --------------------------------------------------------------------------- #
cmd_status() {
  resolve_repo_root
  resolve_data_dir_readonly
  echo "repo root:     $REPO_ROOT"
  echo "data dir:      $DATA_DIR"
  echo "plan dir:      $DATA_DIR/plans"
  echo "worktrees:     $DATA_DIR/worktrees"
  echo "target repo:   ${TARGET_REPO:-$DATA_DIR/repo}"
  echo "port:          $PORT"
  echo "autonomy:      $AUTONOMY"

  local dash_pid sched_pid
  dash_pid="$(cat "$DATA_DIR/dashboard.pid" 2>/dev/null || true)"
  sched_pid="$(cat "$DATA_DIR/scheduler.pid" 2>/dev/null || true)"
  if [ -n "$dash_pid" ] && kill -0 "$dash_pid" 2>/dev/null; then
    echo "dashboard:     running (pid $dash_pid)"
  else
    echo "dashboard:     stopped"
  fi
  if [ -n "$sched_pid" ] && kill -0 "$sched_pid" 2>/dev/null; then
    echo "scheduler:     running (pid $sched_pid)"
  else
    echo "scheduler:     stopped"
  fi
}

# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
main() {
  if [ "$#" -lt 1 ]; then
    usage
    exit 2
  fi
  local cmd="$1"
  shift
  parse_options "$@"

  # Fail fast on invalid input, before anything is started or written.
  case "$PORT" in
    ''|*[!0-9]*) die "--port must be numeric: $PORT" ;;
  esac
  if [ -n "$TARGET_REPO" ] && [ ! -d "$TARGET_REPO/.git" ]; then
    die "--target-repo must be an existing git repo: $TARGET_REPO"
  fi

  case "$cmd" in
    up) cmd_up ;;
    down) cmd_down ;;
    status) cmd_status ;;
    usage|-h|--help) usage ;;
    *)
      echo "ERROR: unknown subcommand: $cmd" >&2
      usage
      exit 2
      ;;
  esac
}

main "$@"
