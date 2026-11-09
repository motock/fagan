#!/bin/sh
# Container entrypoint: dashboard, remote MCP transport and scheduler daemon in
# one process tree.
#
# This script does not own those processes: it runs its start-up assertions and
# then `exec`s pipeline.supervise, which becomes PID 1 and supervises the three
# children (restart with backoff, bounded shutdown). That matters for
# orchestration: a container whose entrypoint has returned looks healthy to a
# supervisor while half of the service is gone.
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

# Refuse the example file's placeholder credentials before anything starts. The
# example ships them empty on purpose so compose's `:?` guard fires first; this
# catches a value that arrived some other way (a hand-edited .env, a shared env
# file, a template). A `change-me-*` client secret is a published credential.
assert_no_placeholder_credentials

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

# From here on this script owns nothing: it hands the process tree to
# `pipeline.supervise` with `exec`, so the supervisor *is* PID 1. That is what
# makes a stop signal reliable - the orchestrator's SIGTERM reaches the process
# that holds the children, not a shell that is busy reaping them.
#
# The supervisor starts the dashboard exactly the way scripts/dashboard.sh does
# (`python -m uvicorn app.dashboard:app` on DASHBOARD_HOST/DASHBOARD_PORT), the
# MCP transport through its own module entrypoint - which refuses to open a
# socket unless the transport has an OIDC identity provider and policy
# enforcement is on, so a misconfigured deployment still dies here instead of
# exposing an unauthenticated endpoint - and the scheduler daemon, the only
# durable event-bus consumer (ADR 0010), when PIPELINE_SCHEDULER_DAEMON=1.
#
# A child that exits is logged and restarted after a bounded backoff instead of
# taking the container down half-dead: the line to look for is
# "<name> exited with status <status> after <uptime>s; restarting in <delay>s",
# so a dead daemon reads "scheduler daemon exited with status 3 ...". On
# SIGTERM/SIGINT the supervisor forwards the signal, gives the children
# SHUTDOWN_GRACE_SECONDS to exit, SIGKILLs any straggler, reaps everything and
# exits 0.
# The supervisor lives in the shipped pipeline package (the image copies
# ./pipeline wholesale), so it needs no separate COPY in the Dockerfile. Run it
# by path, not as a module: an absolute path needs no importable cwd.
_exec_dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd) \
    || die "cannot resolve this script's directory"
supervise="$_exec_dir/../pipeline/supervise.py"
[ -r "$supervise" ] \
    || die "$supervise is missing; the image must ship pipeline/supervise.py"
unset _exec_dir

log "starting dashboard on ${DASHBOARD_HOST}:${DASHBOARD_PORT}"
log "starting MCP transport ${PIPELINE_MCP_TRANSPORT} on ${PIPELINE_MCP_HOST}:${PIPELINE_MCP_PORT}"

set -- \
    --child "dashboard=python -m uvicorn app.dashboard:app --host $DASHBOARD_HOST --port $DASHBOARD_PORT" \
    --child "MCP transport=python app/pipeline_mcp_server.py"
if [ "$PIPELINE_SCHEDULER_DAEMON" = "1" ]; then
    log "starting scheduler daemon"
    set -- "$@" --child "scheduler daemon=python -m pipeline.scheduler_daemon"
fi

exec python "$supervise" "$@"
