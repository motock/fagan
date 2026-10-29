#!/bin/bash
# Container entrypoint: seed the sample origin, then run the pipeline services.
#
# Two properties matter for a container that is restarted mid-run:
#
#   1. The origin repository the pipeline pushes story branches to lives on a
#      volume, and seeding it happens only when it is absent. Re-initialising it
#      on every start (``rm -rf`` the volume, or ``git clone --bare`` each time)
#      resets the refs to the single seed branch, so a story branch and the work
#      it carries look lost after ``docker compose restart``. That failure looks
#      exactly like a lost event notification unless the seeding itself is
#      idempotent, which is why the guard sits *before* the first ``git init``.
#   2. The process the container execs is the one whose exit is the container's
#      exit. ``exec`` replaces this shell, so signals reach the server directly
#      and its status becomes the container's status.
#
# Processes:
#
#   * MCP remote transport (``streamable-http``) - port 8080, OIDC bearer auth,
#     exec'd as the container's foreground process;
#   * dashboard (uvicorn ``app.dashboard:app``) - read-only web UI, port 8000;
#   * scheduler daemon (``python -m pipeline.scheduler_daemon``) - owns
#     ``advance_all_plans`` and is the *only* durable consumer of the event bus
#     (ADR 0010: MCP servers and the dashboard only publish).
#
# Both background children are supervised: if either exits, the container stops
# rather than serving half a pipeline.
#
# No credential is passed as an environment value. The OIDC client secret is
# read from the file named by PIPELINE_OIDC_CLIENT_SECRET_FILE, mounted by the
# operator; NATS credentials arrive through PIPELINE_NATS_CREDS_FILE.
set -euo pipefail

DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"
PIPELINE_MCP_HOST="${PIPELINE_MCP_HOST:-0.0.0.0}"
PIPELINE_MCP_PORT="${PIPELINE_MCP_PORT:-8080}"
PIPELINE_MCP_TRANSPORT="${PIPELINE_MCP_TRANSPORT:-streamable-http}"
PIPELINE_IDENTITY_PROVIDER="${PIPELINE_IDENTITY_PROVIDER:-oidc}"
PIPELINE_OIDC_CLIENT_SECRET_FILE="${PIPELINE_OIDC_CLIENT_SECRET_FILE:-/run/secrets/oidc_client_secret}"
PIPELINE_POLICY_ENFORCE="${PIPELINE_POLICY_ENFORCE:-1}"
PIPELINE_SCHEDULER_DAEMON="${PIPELINE_SCHEDULER_DAEMON:-1}"
PIPELINE_SCRIPTED_MODEL="${PIPELINE_SCRIPTED_MODEL:-0}"
GIT_ORIGIN_DIR="${GIT_ORIGIN_DIR:-/git-origin}"
SAMPLE_REPO_DIR="${SAMPLE_REPO_DIR:-/sample-repo}"
PLAN_DIR="${PLAN_DIR:-/var/lib/pipeline/plans}"
WORKTREE_ROOT="${WORKTREE_ROOT:-/var/lib/pipeline/worktrees}"

export DASHBOARD_HOST DASHBOARD_PORT
export PIPELINE_MCP_HOST PIPELINE_MCP_PORT PIPELINE_MCP_TRANSPORT
export PIPELINE_IDENTITY_PROVIDER PIPELINE_POLICY_ENFORCE
export PIPELINE_OIDC_CLIENT_SECRET_FILE
export PLAN_DIR WORKTREE_ROOT

log() {
    # Action + subject only: no paths, tokens, payloads or exception text
    # (Secure by Design).
    echo "entrypoint: $*" >&2
}

die() {
    log "fatal: $1"
    exit 1
}

# ---------------------------------------------------------------------------
# Seed the sample origin exactly once.
#
# Layout (all on the git-origin volume, so it survives container replacement):
#
#   $GIT_ORIGIN_DIR/sample.git   bare origin the pipeline pushes story branches to
#   $GIT_ORIGIN_DIR/work         working clone whose origin is that bare repo;
#                                the smoke plan's repo_root points here
#
# First start: sample.git has no HEAD, so it is initialised and the read-only
# sample tree mounted at $SAMPLE_REPO_DIR is committed once and pushed (one ref,
# main). Later starts: HEAD is present, nothing is touched, so the refs pushed
# by the story under test are still there and still point where they did.
# ---------------------------------------------------------------------------
seed_git_origin() {
    if [ ! -d "$SAMPLE_REPO_DIR" ]; then
        log "no sample repository mounted; skipping origin seed"
        return 0
    fi
    if [ ! -e "$GIT_ORIGIN_DIR/sample.git/HEAD" ]; then
        log "seeding the sample origin (first start)"
        install -d -m 0750 "$GIT_ORIGIN_DIR"
        git init --bare "$GIT_ORIGIN_DIR/sample.git" >/dev/null
        git init -b main "$GIT_ORIGIN_DIR/work" >/dev/null
        cp -a "$SAMPLE_REPO_DIR/." "$GIT_ORIGIN_DIR/work/"
        git -C "$GIT_ORIGIN_DIR/work" add -A
        git -C "$GIT_ORIGIN_DIR/work" \
            -c user.name="pipeline smoke" \
            -c user.email="smoke@example.invalid" \
            commit -m "chore: seed the sample repository" >/dev/null
        git -C "$GIT_ORIGIN_DIR/work" remote add origin "$GIT_ORIGIN_DIR/sample.git"
        git -C "$GIT_ORIGIN_DIR/work" push --set-upstream origin main >/dev/null
        log "sample origin seeded"
    else
        log "sample origin already seeded; keeping its refs"
    fi
}

# ---------------------------------------------------------------------------
# Optional: point every model role at the scripted model server.
#
# The pipeline resolves a role's provider/model from the plan's role_config, the
# model registry, or the caller's own default, and it *validates* a registry
# model name against providers.<provider>.models. "Use the scripted server" is
# therefore not expressible as a bare environment variable: a registry that
# declares the scripted models is required. This writes one, derived from the
# registry already in the image, and exports PIPELINE_MODEL_REGISTRY_PATH at it.
# Off unless PIPELINE_SCRIPTED_MODEL is 1, so a normal deployment keeps the
# provider its operator pinned.
# ---------------------------------------------------------------------------
write_scripted_registry() {
    [ "$PIPELINE_SCRIPTED_MODEL" = "1" ] || return 0
    target="${PIPELINE_MODEL_REGISTRY_PATH:-${PLAN_DIR%/plans}/model_registry.scripted.json}"
    python - "$target" <<'PY'
import json
import sys

from app import role_registry

# role -> the model name the scripted server recognises on the wire. The server
# picks its reply section by finding a role name in the request's ``model``, so
# a planner-role request has to be labelled after the server's "decompose"
# section, and a security review after its "review" section.
SCRIPTED = {
    "dispatch": "scripted-dispatch",
    "review": "scripted-review",
    "security_review": "scripted-review",
    "overlord": "scripted-overlord",
    "planner": "scripted-decompose",
}

target = sys.argv[1]
registry = role_registry.load_registry()
providers = registry.setdefault("providers", {})
models = providers.setdefault("lmstudio", {}).setdefault("models", {})
roles = registry.setdefault("roles", {})

for role, model in SCRIPTED.items():
    models[model] = {"tag": model}
    roles[role] = {"provider": "lmstudio", "model": model}

with open(target, "w", encoding="utf-8") as handle:
    json.dump(registry, handle, indent=2, sort_keys=True)
print(target)
PY
    export PIPELINE_MODEL_REGISTRY_PATH="$target"
    log "scripted model registry written; model roles point at the scripted server"
}

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

# Mutable state must be on the volume, or plans and worktrees vanish with the
# container. Refuse to start on a read-only or missing state directory.
for dir in "$PLAN_DIR" "$WORKTREE_ROOT"; do
    [ -d "$dir" ] || die "state directory is missing; mount the /var/lib/pipeline volume"
    [ -w "$dir" ] || die "state directory is not writable by this user; check volume ownership"
done

seed_git_origin
write_scripted_registry

# Stop the container when a background child dies: a supervisor that keeps
# serving with half the pipeline up reports healthy while work is not advancing.
supervise() {
    local pid="$1" name="$2"
    (
        while kill -0 "$pid" 2>/dev/null; do sleep 2; done
        log "$name exited; stopping the container"
        kill -TERM 1 2>/dev/null || true
    ) &
}

if [ "$PIPELINE_SCHEDULER_DAEMON" = "1" ]; then
    log "starting the scheduler daemon (the only durable event-bus consumer)"
    python -m pipeline.scheduler_daemon &
    supervise "$!" "scheduler daemon"
fi

log "starting dashboard on ${DASHBOARD_HOST}:${DASHBOARD_PORT}"
python -m uvicorn app.dashboard:app \
    --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" &
supervise "$!" "dashboard"

log "starting MCP transport ${PIPELINE_MCP_TRANSPORT} on ${PIPELINE_MCP_HOST}:${PIPELINE_MCP_PORT}"
exec python app/pipeline_mcp_server.py
