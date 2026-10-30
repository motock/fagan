#!/bin/sh
# Container entrypoint: dashboard, remote MCP transport and scheduler daemon in
# one process tree.
#
# All processes are children of this script, and the script runs until one of
# them exits. That matters for orchestration: a container whose entrypoint has
# returned looks healthy to a supervisor while a half of the service is gone.
#
# The dashboard is launched exactly the way scripts/dashboard.sh launches it -
# `python -m uvicorn app.dashboard:app` bound to DASHBOARD_HOST/DASHBOARD_PORT -
# but in the foreground of its own pid rather than detached, so signals reach
# it.
#
# The MCP server is started through its own module entrypoint, which delegates
# to pipeline.remote_transport.serve(). That module refuses to open a socket
# unless the transport has an OIDC identity provider and policy enforcement is
# on, so a misconfigured deployment dies here instead of exposing an
# unauthenticated endpoint.
#
# The OIDC client secret is never an environment value: it is read from the
# file named by PIPELINE_OIDC_CLIENT_SECRET_FILE, which the operator mounts
# (for example from a Docker/Kubernetes secret at /run/secrets/...).
#
# The operator's forge and model credentials are mounted the same way and
# loaded by deploy/lib/operator_env.sh, which this script sources before it
# starts anything (see deploy/README.md, "Credentials").
set -eu

DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"
PIPELINE_MCP_HOST="${PIPELINE_MCP_HOST:-0.0.0.0}"
PIPELINE_MCP_PORT="${PIPELINE_MCP_PORT:-8080}"
PIPELINE_MCP_TRANSPORT="${PIPELINE_MCP_TRANSPORT:-streamable-http}"
PIPELINE_IDENTITY_PROVIDER="${PIPELINE_IDENTITY_PROVIDER:-oidc}"
PIPELINE_OIDC_CLIENT_SECRET_FILE="${PIPELINE_OIDC_CLIENT_SECRET_FILE:-/run/secrets/oidc_client_secret}"
PIPELINE_POLICY_ENFORCE="${PIPELINE_POLICY_ENFORCE:-1}"
PIPELINE_SCHEDULER_DAEMON="${PIPELINE_SCHEDULER_DAEMON:-1}"

export DASHBOARD_HOST DASHBOARD_PORT
export PIPELINE_MCP_HOST PIPELINE_MCP_PORT PIPELINE_MCP_TRANSPORT
export PIPELINE_IDENTITY_PROVIDER PIPELINE_POLICY_ENFORCE
export PIPELINE_OIDC_CLIENT_SECRET_FILE

# The credential loader and the repository roots live in a sourced library, not
# in this file. That is a security property, not tidiness: this script must have
# no "stop before the process start" branch for a stray variable, a rename or a
# wrapper to reach, because under dash - /bin/sh in this image - an executed
# script that returns early exits *0* and leaves a container that looks healthy
# with the dashboard, the MCP transport and the scheduler all absent. Keeping
# the shared code in a library gives a test nothing to bypass.
#
# log, die, stat_mode, stat_uid, load_secret and the start-up helpers all come
# from there; this script sources it before its first message.
_entrypoint_dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd) \
    || { echo "entrypoint: fatal: cannot resolve this script's directory" >&2; exit 1; }
_operator_env="$_entrypoint_dir/lib/operator_env.sh"
[ -r "$_operator_env" ] \
    || { echo "entrypoint: fatal: $_operator_env is missing; the image must ship deploy/lib" >&2; exit 1; }
. "$_operator_env"
unset _entrypoint_dir _operator_env

# Operator credentials from the mounted secret files, then the repo roots the
# clone path is confined to (SA-05 relies on both).
load_operator_credentials
ensure_repos_root

# Fail fast when the deployment forgot to mount the OIDC client credential.
# Bearer-token verification itself is JWKS-only (pipeline/remote_transport.py
# never reads this file), so the check is a configuration assertion, not a
# dependency of the verification path: it turns a missing mount into a start
# failure instead of a failure on the first token exchange.
if [ "$PIPELINE_IDENTITY_PROVIDER" = "oidc" ]; then
    [ -r "$PIPELINE_OIDC_CLIENT_SECRET_FILE" ] \
        || die "PIPELINE_OIDC_CLIENT_SECRET_FILE is not readable; mount the secret file"
fi

[ "$PIPELINE_MCP_TRANSPORT" = "streamable-http" ] \
    || die "PIPELINE_MCP_TRANSPORT must be streamable-http in this image"

# Fail closed before any child starts: an unrecognised value would silently
# pick one of the two behaviours, and "the scheduler is not running" is not a
# state a deployment should discover later.
case "$PIPELINE_SCHEDULER_DAEMON" in
    0|1) ;;
    *) die "PIPELINE_SCHEDULER_DAEMON must be 0 or 1" ;;
esac

# Mutable state must be on the volume, or plans and worktrees vanish with the
# container. Refuse to start on a read-only or missing PLAN_DIR.
for dir in "${PLAN_DIR:-/var/lib/pipeline/plans}" "${WORKTREE_ROOT:-/var/lib/pipeline/worktrees}"; do
    [ -d "$dir" ] || die "state directory is missing; mount the /var/lib/pipeline volume"
    [ -w "$dir" ] || die "state directory is not writable by this user; check volume ownership"
done

log "starting dashboard on ${DASHBOARD_HOST}:${DASHBOARD_PORT}"
python -m uvicorn app.dashboard:app \
    --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" &
dashboard_pid=$!

log "starting MCP transport ${PIPELINE_MCP_TRANSPORT} on ${PIPELINE_MCP_HOST}:${PIPELINE_MCP_PORT}"
python app/pipeline_mcp_server.py &
mcp_pid=$!

# The scheduler daemon is the only durable event-bus consumer (ADR 0010), so it
# runs here rather than in an orchestration shim: a container supervisor sees
# one process tree, and a daemon that dies takes the container down with it.
# PIPELINE_SCHEDULER_DAEMON=0 runs it outside the image instead.
scheduler_pid=
if [ "$PIPELINE_SCHEDULER_DAEMON" = "1" ]; then
    log "starting scheduler daemon"
    python -m pipeline.scheduler_daemon &
    scheduler_pid=$!
fi

shutdown() {
    log "shutting down"
    # shellcheck disable=SC2086  # unquoted on purpose: an empty pid must vanish
    kill -TERM "$dashboard_pid" "$mcp_pid" $scheduler_pid 2>/dev/null || true
}
trap shutdown INT TERM

# Wait for either child; propagate its status so the container exits non-zero
# when a half of the service dies. `wait -n` is bash-only, and this image runs
# under /bin/sh, so poll the two pids instead.
status=0
while :; do
    if ! kill -0 "$dashboard_pid" 2>/dev/null; then
        log "dashboard exited"
        wait "$dashboard_pid" || status=$?
        break
    fi
    if ! kill -0 "$mcp_pid" 2>/dev/null; then
        log "MCP transport exited"
        wait "$mcp_pid" || status=$?
        break
    fi
    if [ -n "$scheduler_pid" ] && ! kill -0 "$scheduler_pid" 2>/dev/null; then
        log "scheduler daemon exited"
        wait "$scheduler_pid" || status=$?
        break
    fi
    sleep 1
done

# Reap the siblings so no orphan keeps the port or the daemon lock.
# shellcheck disable=SC2086  # unquoted on purpose: an empty pid must vanish
kill -TERM "$dashboard_pid" "$mcp_pid" $scheduler_pid 2>/dev/null || true
wait "$dashboard_pid" 2>/dev/null || true
wait "$mcp_pid" 2>/dev/null || true
if [ -n "$scheduler_pid" ]; then
    wait "$scheduler_pid" 2>/dev/null || true
fi
exit "$status"
