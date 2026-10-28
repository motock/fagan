#!/bin/sh
# Container entrypoint: dashboard + remote MCP transport in one process tree.
#
# Both processes are children of this script, and the script runs until one of
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
set -eu

DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"
PIPELINE_MCP_HOST="${PIPELINE_MCP_HOST:-0.0.0.0}"
PIPELINE_MCP_PORT="${PIPELINE_MCP_PORT:-8080}"
PIPELINE_MCP_TRANSPORT="${PIPELINE_MCP_TRANSPORT:-streamable-http}"
PIPELINE_IDENTITY_PROVIDER="${PIPELINE_IDENTITY_PROVIDER:-oidc}"
PIPELINE_OIDC_CLIENT_SECRET_FILE="${PIPELINE_OIDC_CLIENT_SECRET_FILE:-/run/secrets/oidc_client_secret}"
PIPELINE_POLICY_ENFORCE="${PIPELINE_POLICY_ENFORCE:-1}"

export DASHBOARD_HOST DASHBOARD_PORT
export PIPELINE_MCP_HOST PIPELINE_MCP_PORT PIPELINE_MCP_TRANSPORT
export PIPELINE_IDENTITY_PROVIDER PIPELINE_POLICY_ENFORCE
export PIPELINE_OIDC_CLIENT_SECRET_FILE

log() {
    # Action + subject only: no paths, tokens or payloads (Secure by Design).
    echo "entrypoint: $*" >&2
}

die() {
    log "fatal: $1"
    exit 1
}

# The remote transport is authenticated; without the mounted secret the
# identity provider cannot be constructed, and starting anyway would mean
# serving an endpoint nobody can authenticate to.
if [ "$PIPELINE_IDENTITY_PROVIDER" = "oidc" ]; then
    [ -r "$PIPELINE_OIDC_CLIENT_SECRET_FILE" ] \
        || die "PIPELINE_OIDC_CLIENT_SECRET_FILE is not readable; mount the secret file"
fi

[ "$PIPELINE_MCP_TRANSPORT" = "streamable-http" ] \
    || die "PIPELINE_MCP_TRANSPORT must be streamable-http in this image"

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

shutdown() {
    log "shutting down"
    kill -TERM "$dashboard_pid" "$mcp_pid" 2>/dev/null || true
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
    sleep 1
done

# Reap the sibling so no orphan keeps the port.
kill -TERM "$dashboard_pid" "$mcp_pid" 2>/dev/null || true
wait "$dashboard_pid" 2>/dev/null || true
wait "$mcp_pid" 2>/dev/null || true
exit "$status"
