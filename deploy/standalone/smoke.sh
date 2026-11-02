#!/usr/bin/env bash
#
# End-to-end smoke for the STANDALONE install path (deploy/install.sh +
# deploy/standalone/compose.yaml). deploy/smoke/smoke.sh grades the dev smoke
# stack; this grades the artifact an operator actually runs, and it is the only
# place in CI where `install.sh --non-interactive` is executed for real.
#
# What it proves, in order:
#
#   1. `deploy/install.sh --non-interactive` brings the standalone stack up in a
#      throwaway state directory, with hostname `localhost` and a scripted model
#      backend standing in for Ollama (CI has no GPU and no model host).
#   2. A token minted by deploy/standalone/mcp-token for the installer-created
#      user is accepted by the MCP transport over HTTPS through Caddy, verified
#      against Caddy's internal CA (never `curl -k`).
#   3. `GET /.well-known/oauth-protected-resource` answers the RFC 9728 metadata.
#   4. A plan whose `repo_url` is a `file://` bare repository - CI has no git
#      host - is saved, ingested, approved and dispatched.
#   5. `check_story_status` reaches a terminal state within 10 minutes.
#
# Two design notes, because both are easy to get wrong:
#
# * TEARDOWN FIRST. `trap cleanup EXIT` is registered immediately after the temp
#   directory exists, before install.sh runs. A failure at t=40s (approve_plan,
#   say) then still runs `down -v`, so the next run starts from a clean compose
#   project. Without it the leftover `pgdata`-style volumes and published port
#   make run #2 fail with "port already allocated", which looks like a product
#   bug and is not one.
#
# * ONE DEADLINE. The poll loop computes DEADLINE once, before the loop. Recomputed
#   inside it, a stuck run would push the deadline forward on every iteration and
#   burn the job's 30-minute timeout instead of reporting "timed out after 600s".
#
# THE COMPOSE OVERRIDE. Nothing may be added next to deploy/standalone/compose.yaml
# (that is the operator's file, and a stray compose.override.yaml would change a
# real install), and install.sh invokes compose itself:
#
#     compose() { (cd -- "$REPO_ROOT" && docker compose -f "$COMPOSE_REL" "$@"); }
#
# An explicit `-f` makes Docker Compose ignore `COMPOSE_FILE`, and install.sh has
# no "extra file" flag, so the override is injected with a PATH shim: $TMP/bin/docker
# appends `-f $TMP/override.yaml` to any `compose` invocation that names the
# standalone file. The shim lives in the temp dir and dies with it; the repository
# is never modified.
#
# Environment (all optional; defaults suit CI):
#   STANDALONE_SMOKE_POLL_TIMEOUT_S   terminal-state deadline           (600)
#   STANDALONE_SMOKE_POLL_INTERVAL_S  seconds between polls               (5)
#   STANDALONE_SMOKE_KEEP             "1" leaves the stack *and* the temp dir
#                                     in place, for inspection            (unset)
#
# Usage:
#   bash deploy/standalone/smoke.sh
#
# Requires: bash, Docker with the compose plugin, git, curl, python3, openssl.
set -eu
set -o pipefail

STEP="startup"
log() { echo "standalone-smoke: $*"; }
fail() {
    echo "FAILED: ${STEP}: $*" >&2
    exit 1
}
# The line number, never $BASH_COMMAND: a failing command may carry the bearer
# token in its arguments, and this output goes to the job log.
trap 'echo "FAILED: ${STEP} (line ${LINENO})" >&2; exit 1' ERR

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BASE_COMPOSE="$REPO_ROOT/deploy/standalone/compose.yaml"
INSTALL="$REPO_ROOT/deploy/install.sh"
MCP_TOKEN="$REPO_ROOT/deploy/standalone/mcp-token"
SCRIPTED_SERVER="$REPO_ROOT/tests/smoke/scripted_model_server.py"

POLL_TIMEOUT_S="${STANDALONE_SMOKE_POLL_TIMEOUT_S:-600}"
POLL_INTERVAL_S="${STANDALONE_SMOKE_POLL_INTERVAL_S:-5}"
KEEP_TMP="${STANDALONE_SMOKE_KEEP:-0}"

PUBLIC_HOSTNAME="localhost"
PUBLIC_PORT="8443"
MCP_URL="https://${PUBLIC_HOSTNAME}:${PUBLIC_PORT}/mcp"
METADATA_URL="https://${PUBLIC_HOSTNAME}:${PUBLIC_PORT}/.well-known/oauth-protected-resource"
PLAN_NAME="standalone-smoke"
STORY_KEY="SMOKE-1"
# Path the bare repository is mounted at inside the pipeline container. The
# file:// URL must end in <owner>/<repo>.git (pipeline/repo_clone._file_segments).
CONTAINER_REPO_DIR="/git-origin"
CONTAINER_REPO_URL="file://${CONTAINER_REPO_DIR}/repo.git"

MODEL_PID=""
MODEL_PORT=""
TOKEN=""
# Empty for the root case; see the escalation note above seed_credentials().
SUDO=""

# ---------------------------------------------------------------------------
# Temp dir and teardown - registered before anything else can fail
# ---------------------------------------------------------------------------

TMP="$(mktemp -d "${TMPDIR:-/tmp}/standalone-smoke.XXXXXX")"
# The pipeline container runs as uid 10001 and must read the bare repository
# through a bind mount, so the temp tree cannot be 0700.
chmod 755 "$TMP"
TMP="$(cd "$TMP" && pwd -P)"
OVERRIDE="$TMP/override.yaml"
STATE_DIR="$TMP/state"
export HOME="$TMP/home"
export XDG_CONFIG_HOME="$TMP/home/.config"
CA_FILE="$XDG_CONFIG_HOME/pipeline/ca.crt"
CLIENT_CONFIG="$XDG_CONFIG_HOME/pipeline/mcp-client.json"
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$STATE_DIR"

# shellcheck disable=SC2317  # invoked by the EXIT trap
cleanup() {
    local status=$?
    trap - EXIT
    if [ -n "$MODEL_PID" ]; then
        kill "$MODEL_PID" 2>/dev/null || true
        wait "$MODEL_PID" 2>/dev/null || true
    fi
    # `down -v` (not plain `down`): the named volumes are the thing that makes a
    # second run fail, and they belong to this run only.
    #
    # The variables are exported *here* rather than left over from run_installer:
    # deploy/standalone/compose.yaml declares several of them with `${VAR:?}`, so
    # compose refuses to even build the model without them, `down` would fail, and
    # the volumes would survive - the exact leftover that breaks the next run.
    # The values only have to satisfy interpolation, not match the install: the
    # project name is fixed in the compose file (`pipeline-standalone`).
    if [ -f "$ENV_FILE" ]; then
        # shellcheck disable=SC1090  # generated at run time
        set -a && . "$ENV_FILE" && set +a
    fi
    PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME:-localhost}"
    PUBLIC_PORT="${PUBLIC_PORT:-8443}"
    KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-unused}"
    export PUBLIC_HOSTNAME PUBLIC_PORT
    export CADDY_TLS="${CADDY_TLS:-internal}"
    export KEYCLOAK_ADMIN_PASSWORD
    export PIPELINE_CLI_CALLBACK_PORT="${PIPELINE_CLI_CALLBACK_PORT:-33418}"
    export PIPELINE_DASHBOARD_CLIENT_SECRET="${PIPELINE_DASHBOARD_CLIENT_SECRET:-unused}"
    export PIPELINE_MCP_CLIENT_SECRET="${PIPELINE_MCP_CLIENT_SECRET:-unused}"
    if [ "${STANDALONE_SMOKE_KEEP:-0}" = "1" ]; then
        log "STANDALONE_SMOKE_KEEP=1: leaving the stack up. Inspect with: docker compose -f \"$BASE_COMPOSE\" -f \"$OVERRIDE\" ps"
    else
        docker compose -f "$BASE_COMPOSE" -f "$OVERRIDE" down -v >/dev/null 2>&1 || true
    fi
    if [ -n "$SUDO" ] && [ -d "$TMP" ]; then
        # The escalated install leaves root-owned files (the state dir, the CA,
        # the client credentials) behind.
        $SUDO chown -R "$(id -u):$(id -g)" "$TMP" >/dev/null 2>&1 || true
    fi
    if [ "$KEEP_TMP" = "1" ] || [ "$status" -ne 0 ]; then
        log "leaving $TMP in place for inspection"
    else
        rm -rf "$TMP"
    fi
    exit "$status"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# The override: scripted model, file:// repos, throwaway secrets
# ---------------------------------------------------------------------------

# Absolute paths, because everything declared in an override file resolves
# against the project directory (deploy/standalone), not against $TMP.
write_override() {
    STEP="override"
    local name
    cat >"$OVERRIDE" <<YAML
# Generated by deploy/standalone/smoke.sh. Never committed: it exists to point a
# throwaway install at a scripted model server and a file:// git repository.
services:
  pipeline:
    environment:
      # Every role in the plan below resolves to the scripted backend, which is
      # the provider the smoke stack also uses (deploy/compose.yaml).
      PIPELINE_LOCAL_ENDPOINT_LMSTUDIO: http://host.docker.internal:${MODEL_PORT}
      # TEST-ONLY escape hatch (pipeline/repo_clone.py): CI has no git host, so
      # the plan's repo_url is file:///git-origin/repo.git.
      PIPELINE_REPO_URL_ALLOW_FILE: "1"
      # The base file points this at ./model_registry.json, which is written by
      # the installer; this run supplies its own registry with the scripted
      # models so a plan can select them.
      PIPELINE_MODEL_REGISTRY_PATH: /app/model_registry.local.json
    volumes:
      # Same target as the base file, so compose merges the two by target and
      # this one wins. The generated registry replaces the installer's copy.
      - ${TMP}/model_registry.scripted.json:/app/model_registry.local.json:ro
      # The bare repository the plan clones from. Read-write: a dispatched story
      # pushes its branch back to origin.
      - ${TMP}/repo.git:${CONTAINER_REPO_DIR}/repo.git
secrets:
  # The installer writes these under the throwaway state dir; the base file
  # names ./secrets/<name> relative to deploy/standalone, so an install with a
  # separate --state-dir has to re-point them.
YAML
    for name in oidc_client_secret anthropic_api_key claude_code_oauth_token gh_token ollama_api_key; do
        printf '  %s:\n    file: %s\n' "$name" "$STATE_DIR/secrets/$name" >>"$OVERRIDE"
    done
    log "wrote the compose override"
}

# The registry the plan's models have to resolve against (the pipeline validates
# a role's model as a registry fact, not an environment-only switch).
write_scripted_registry() {
    STEP="registry"
    python3 - "$TMP/model_registry.scripted.json" <<'PY'
import json
import sys

roles = ["dispatch", "review", "overlord", "decompose"]
registry = {
    "providers": {
        "lmstudio": {
            "models": {
                f"scripted-{role}": {"tag": f"scripted-{role}"} for role in roles
            }
        }
    }
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(registry, handle, indent=2)
    handle.write("\n")
PY
    log "wrote the scripted model registry"
}

# The plan is deploy/smoke/plan.json with `repo_url` in place of `repo_root`:
# pipeline/remote_scope.resolve_repo_url refuses a plan that names both.
write_plan() {
    STEP="plan"
    python3 - "$REPO_ROOT/deploy/smoke/plan.json" "$TMP/plan.json" "$CONTAINER_REPO_URL" <<'PY'
import json
import sys

source, target, repo_url = sys.argv[1:4]
plan = json.loads(open(source, encoding="utf-8").read())
plan.pop("repo_root", None)
plan["repo_url"] = repo_url
with open(target, "w", encoding="utf-8") as handle:
    json.dump(plan, handle, indent=2)
    handle.write("\n")
PY
    log "wrote the repo_url plan ($CONTAINER_REPO_URL)"
}

# A bare repository with one commit on `main`, which is all a clone needs.
seed_repository() {
    STEP="seed-repo"
    git init --bare -q --initial-branch=main "$TMP/repo.git"
    git init -q -b main "$TMP/seed"
    git -C "$TMP/seed" -c user.name='standalone smoke' \
        -c user.email='smoke@example.invalid' commit -q --allow-empty -m 'chore: seed the smoke repository'
    git -C "$TMP/seed" remote add origin "$TMP/repo.git"
    git -C "$TMP/seed" push -q origin HEAD:main
    # uid 10001 inside the container reads (and pushes) through the bind mount.
    chmod -R a+rwX "$TMP/repo.git"
    log "seeded the bare repository"
}

# ---------------------------------------------------------------------------
# The docker shim (see the header: install.sh hardcodes its own -f)
# ---------------------------------------------------------------------------

install_shim() {
    STEP="shim"
    mkdir -p "$TMP/bin"
    cat >"$TMP/shim.py" <<'PY'
"""Insert the smoke override into `compose -f deploy/standalone/compose.yaml`."""

import os
import sys

VALUE_FLAGS = {
    "-f", "--file", "-p", "--project-name", "--env-file", "--profile",
    "--project-directory", "-H", "--host", "--context", "--log-level",
    "--ansi", "--progress", "--parallel",
}


def rewrite(args, override):
    if not args or args[0] != "compose":
        return args
    files = []
    index = 1
    while index < len(args):
        arg = args[index]
        if arg in ("-f", "--file"):
            if index + 1 < len(args):
                files.append(args[index + 1])
            index += 2
        elif arg.startswith("--file="):
            files.append(arg.split("=", 1)[1])
            index += 1
        elif arg.startswith("-f") and len(arg) > 2:
            files.append(arg[2:])
            index += 1
        elif arg in VALUE_FLAGS:
            index += 2
        elif arg.startswith("-"):
            index += 1
        else:
            break  # the subcommand
    base = ("deploy/standalone/compose.yaml",)
    if override in files or not any(f.endswith(base) for f in files):
        return args
    return args[:index] + ["-f", override] + args[index:]


for value in rewrite(sys.argv[2:], sys.argv[1]):
    sys.stdout.write(value.replace("\0", "") + "\0")
PY
    cat >"$TMP/bin/docker" <<'SHIM'
#!/usr/bin/env bash
# PATH shim: hands every docker invocation to the real client, with the smoke
# override appended to `compose` calls that name the standalone compose file.
# The variables are literal here because sudo's secure_path does not keep the
# caller's PATH, so install.sh reaches this file by absolute path instead.
set -eu
mapfile -d '' -t args < <(python3 "$TMPDIR_PLACEHOLDER/shim.py" \
    "$TMPDIR_PLACEHOLDER/override.yaml" "$@")
exec "$TMPDIR_PLACEHOLDER/real-docker" "${args[@]}"
SHIM
    printf '#!/usr/bin/env bash\nexec %q "$@"\n' "$(command -v docker)" >"$TMP/real-docker"
    chmod +x "$TMP/real-docker"
    # Rewrite the placeholder rather than interpolating a heredoc: the temp path
    # is not under our control, but it never contains a quote.
    python3 - "$TMP/bin/docker" "$TMP" <<'PY'
import sys

path, tmp = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
open(path, "w", encoding="utf-8").write(text.replace("$TMPDIR_PLACEHOLDER", tmp))
PY
    chmod 755 "$TMP/bin/docker"
    export PATH="$TMP/bin:$PATH"
    # Docker client config home as well: sudo -H would send the root shell at
    # /root/.docker, and the daemon connection lives in the caller's config.
    if [ -n "${DOCKER_CONFIG:-}" ]; then
        printf 'DOCKER_CONFIG=%s\n' "$DOCKER_CONFIG" >>"$ENV_FILE"
    fi
    log "installed the compose shim"
}

# ---------------------------------------------------------------------------
# The scripted model server
# ---------------------------------------------------------------------------

start_model_server() {
    STEP="scripted-model"
    local out="$TMP/model-server.log" deadline
    : >"$out"
    # `--port 0` lets the kernel pick a free port and the server prints the one
    # it got, which is the only race-free way to learn it.
    python3 "$SCRIPTED_SERVER" --host 0.0.0.0 --port 0 >"$out" 2>&1 &
    MODEL_PID=$!
    deadline=$((SECONDS + 30))
    MODEL_PORT=""
    while [ "$SECONDS" -lt "$deadline" ]; do
        MODEL_PORT="$(sed -n 's|.*:\([0-9][0-9]*\)/v1/chat/completions.*|\1|p' "$out" | tail -n 1)"
        if [ -n "$MODEL_PORT" ] && curl -fsS --max-time 2 -o /dev/null \
            "http://127.0.0.1:${MODEL_PORT}/v1/models" 2>/dev/null; then
            log "scripted model backend is on 127.0.0.1:${MODEL_PORT}"
            return 0
        fi
        kill -0 "$MODEL_PID" 2>/dev/null || fail "the scripted model server exited; see $out"
        sleep 1
    done
    fail "the scripted model server did not answer within 30s; see $out"
}

# ---------------------------------------------------------------------------
# install.sh
# ---------------------------------------------------------------------------

# The installer writes $STATE_DIR/.env, but compose reads `.env` from the
# project directory (deploy/standalone) and an explicit `-f` is all install.sh
# passes, so the values it interpolates have to be in the environment too. The
# generated credentials are seeded first so install.sh reuses these exact values
# instead of inventing different ones for the secret files.
#
# WHY SUDO. deploy/lib/operator_env.sh refuses a secret file that is not owned by
# the container's uid (10001) and has no bypass, and docker creates a fresh named
# volume owned by that uid. install.sh hands SECRETS_DIR to 10001 only when it
# runs as root; otherwise it warns and the pipeline's entrypoint dies on the first
# file ("secret file is not owned by this user"). A throwaway state dir is owned
# by the runner, so the install has to escalate - the same `sudo chown -R
# 10001:10001` install.sh tells a non-root operator to run. macOS is the exception:
# Docker Desktop maps the bind mount to the container's uid, so root ownership is
# neither created nor needed, and an unattended local run must not prompt for sudo.
if [ "$(id -u)" -ne 0 ] && [ "$(uname -s)" != "Darwin" ]; then
    command -v sudo >/dev/null 2>&1 ||
        fail "sudo is required: deploy/lib/operator_env.sh accepts secret files owned only by uid 10001"
    SUDO="sudo"
elif [ "$(uname -s)" = "Darwin" ]; then
    log "macOS: Docker Desktop maps bind mounts to the container uid; no sudo needed"
fi

# Values the installer and compose need, written to a 0600 file and sourced by
# the escalated shell. A file keeps them out of argv (`ps` is world-readable) and
# survives sudo's env_reset, which would otherwise drop every exported variable.
ENV_FILE="$TMP/stack.env"

seed_credentials() {
    STEP="credentials"
    local callback_port=33418
    {
        printf 'KEYCLOAK_ADMIN_PASSWORD=%s\n' "$(openssl rand -hex 32)"
        printf 'PIPELINE_DASHBOARD_CLIENT_SECRET=%s\n' "$(openssl rand -hex 32)"
        printf 'PIPELINE_MCP_CLIENT_SECRET=%s\n' "$(openssl rand -hex 32)"
        printf 'PIPELINE_CLI_CALLBACK_PORT=%s\n' "$callback_port"
        printf 'PUBLIC_HOSTNAME=%s\n' "$PUBLIC_HOSTNAME"
        printf 'PUBLIC_PORT=%s\n' "$PUBLIC_PORT"
        printf 'CADDY_TLS=internal\n'
        printf 'KEYCLOAK_ADMIN=smoke-admin\n'
        printf 'OLLAMA_ENDPOINT=http://host.docker.internal:%s\n' "$MODEL_PORT"
        printf 'PIPELINE_REPO_URL_HOSTS=localhost\n'
        printf 'PIPELINE_STATE_DIR=%s\n' "$STATE_DIR"
        printf 'HOME=%s\n' "$HOME"
        printf 'XDG_CONFIG_HOME=%s\n' "$XDG_CONFIG_HOME"
    } >"$ENV_FILE"
    chmod 600 "$ENV_FILE"
    # install.sh reuses values it finds in $STATE_DIR/.env instead of generating
    # different ones for the secret files, so seed the same three secrets there.
    grep -E '^(KEYCLOAK_ADMIN|KEYCLOAK_ADMIN_PASSWORD|PIPELINE_DASHBOARD_CLIENT_SECRET|PIPELINE_MCP_CLIENT_SECRET|PIPELINE_CLI_CALLBACK_PORT)=' \
        "$ENV_FILE" >"$STATE_DIR/.env"
    chmod 600 "$STATE_DIR/.env"
    log "seeded throwaway stack credentials"
}

run_installer() {
    STEP="install"
    local password_file="$TMP/first-user-password"
    openssl rand -hex 16 >"$password_file"
    chmod 600 "$password_file"
    # The installer prints no secret; its output is the operator-facing summary.
    # Hostname `localhost` keeps Caddy's internal certificate matching the URL
    # this script calls, and the state dir is thrown away by the EXIT trap.
    # shellcheck disable=SC2086  # $SUDO is empty or the word `sudo`
    $SUDO bash -c '
        set -eu
        set -a
        . "$1"
        set +a
        shift
        exec bash "$@"
    ' _ "$ENV_FILE" "$INSTALL" --non-interactive \
        --state-dir "$STATE_DIR" \
        --hostname "$PUBLIC_HOSTNAME" \
        --port "$PUBLIC_PORT" \
        --first-user smoke \
        --first-user-password-file "$password_file"
    # The installer ran escalated, so the CA and the per-user client credentials
    # it wrote under XDG_CONFIG_HOME are root-owned (mode 0600, inside a 0700
    # directory). Hand them back before minting: mcp-token refuses a file it does
    # not own, and this script talks to the API as the invoking user.
    [ -z "$SUDO" ] || $SUDO chown -R "$(id -u):$(id -g)" "$XDG_CONFIG_HOME" ||
        fail "could not take ownership of $XDG_CONFIG_HOME"
}

# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

mint_token() {
    STEP="mint-token"
    [ -f "$CLIENT_CONFIG" ] || fail "the installer wrote no client credentials at $CLIENT_CONFIG"
    [ -f "$CA_FILE" ] || fail "the installer left no Caddy CA at $CA_FILE"
    # Kept in a shell variable: never echoed, never on a command line we print.
    TOKEN="$(python3 "$MCP_TOKEN" --config "$CLIENT_CONFIG" --cacert "$CA_FILE" --raw)" ||
        fail "mcp-token could not mint a token"
    [ -n "$TOKEN" ] || fail "mcp-token returned an empty token"
    echo "token minted"
}

SESSION_ID=""

json_payload() {
    python3 -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    raise SystemExit("empty MCP response")
if raw.startswith("event:") or raw.startswith("data:"):
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[len("data:"):].strip()
            break
print(json.dumps(json.loads(raw)))
'
}

tool_result() {
    python3 -c '
import json, sys
payload = json.load(sys.stdin)
if "error" in payload:
    print(json.dumps({"ok": False, "transport_error": payload["error"]}))
    raise SystemExit(0)
result = payload.get("result") or {}
structured = result.get("structuredContent")
if structured is not None:
    print(json.dumps(structured))
    raise SystemExit(0)
for item in result.get("content") or []:
    if item.get("type") == "text":
        print(item.get("text", ""))
        raise SystemExit(0)
print(json.dumps(result))
'
}

mcp_post() { # $1 body -> prints the response body
    local body="$1" headers response session
    local args=(-sS --fail-with-body -X POST "$MCP_URL"
        --cacert "$CA_FILE"
        -H "Authorization: Bearer ${TOKEN}"
        -H "Content-Type: application/json"
        -H "Accept: application/json, text/event-stream")
    [ -n "$SESSION_ID" ] && args+=(-H "Mcp-Session-Id: ${SESSION_ID}")
    headers="$TMP/headers"
    if ! response="$(curl "${args[@]}" -D "$headers" -d "$body" 2>&1)"; then
        fail "the MCP endpoint refused a request"
    fi
    session="$(grep -i '^mcp-session-id:' "$headers" 2>/dev/null | tr -d '\r' | awk '{print $2}' | tail -1 || true)"
    [ -n "$session" ] && SESSION_ID="$session"
    printf '%s' "$response"
}

mcp_initialize() {
    STEP="mcp-initialize"
    SESSION_ID=""
    mcp_post '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"standalone-smoke","version":"1"}}}' >/dev/null
    [ -n "$SESSION_ID" ] || fail "the MCP transport returned no session id"
    mcp_post '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null || true
}

mcp_tool() { # $1 tool, $2 json args -> prints the tool result JSON
    local call
    call="$(python3 -c '
import json, sys
print(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": sys.argv[1], "arguments": json.loads(sys.argv[2])}}))
' "$1" "$2")"
    mcp_post "$call" | json_payload | tool_result
}

require_ok() { # $1 tool, $2 result JSON
    local ok
    ok="$(printf '%s' "$2" | python3 -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except ValueError:
    print("unparseable")
    raise SystemExit(0)
print("ok" if payload.get("ok", True) else "refused")
')"
    [ "$ok" = "ok" ] || fail "$1 refused: $(printf '%s' "$2" | tr -d '\n' | cut -c1-300)"
}

check_metadata() {
    STEP="metadata"
    local body
    body="$TMP/metadata.json"
    curl -sS --fail-with-body --cacert "$CA_FILE" -o "$body" "$METADATA_URL" ||
        fail "GET $METADATA_URL was refused"
    python3 - "$body" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
resource = document.get("resource")
if not resource:
    raise SystemExit("protected-resource metadata names no resource")
if not document.get("authorization_servers"):
    raise SystemExit("protected-resource metadata names no authorization server")
print(f"metadata names resource {resource}")
PY
    log "oauth-protected-resource metadata is served"
}

call_tools() {
    STEP="save-plan"
    local result
    result="$(mcp_tool save_plan "$(python3 -c '
import json, sys
print(json.dumps({"plan_name": sys.argv[1],
                  "plan_json": open(sys.argv[2], encoding="utf-8").read()}))
' "$PLAN_NAME" "$TMP/plan.json")")"
    require_ok save_plan "$result"
    log "save_plan accepted the repo_url plan"

    STEP="ingest-plan"
    result="$(mcp_tool ingest_plan "$(python3 -c '
import json, sys
print(json.dumps({"plan_name": sys.argv[1], "overwrite": True}))
' "$PLAN_NAME")")"
    require_ok ingest_plan "$result"
    log "ingest_plan cloned the file:// repository"

    STEP="approve-plan"
    result="$(mcp_tool approve_plan "$(python3 -c '
import json, sys
print(json.dumps({"plan_name": sys.argv[1]}))
' "$PLAN_NAME")")"
    require_ok approve_plan "$result"
    log "approve_plan succeeded"

    STEP="dispatch-story"
    result="$(mcp_tool dispatch_story "$(python3 -c '
import json, sys
print(json.dumps({"plan_name": sys.argv[1], "story_key": sys.argv[2]}))
' "$PLAN_NAME" "$STORY_KEY")")"
    require_ok dispatch_story "$result"
    log "dispatch_story started $STORY_KEY"
}

poll_for_terminal_state() {
    STEP="poll-story-status"
    # Computed once, outside the loop: recomputed inside it, a stuck run would
    # push the deadline forward forever and eat the job's 30-minute timeout.
    local DEADLINE=$((SECONDS + POLL_TIMEOUT_S))
    local state="unknown" seen=""
    while [ "$SECONDS" -lt "$DEADLINE" ]; do
        state="$(mcp_tool check_story_status "$(python3 -c '
import json, sys
print(json.dumps({"plan_name": sys.argv[1], "story_key": sys.argv[2]}))
' "$PLAN_NAME" "$STORY_KEY")" | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
try:
    payload = json.loads(raw)
except ValueError:
    print("unknown")
    raise SystemExit(0)
if not isinstance(payload, dict):
    print("unknown")
    raise SystemExit(0)
print(payload.get("status") or "unknown")
')"
        [ "$state" = "$seen" ] || log "story $STORY_KEY is ${state}"
        seen="$state"
        case "$state" in
            done | merged | reviewed | completed | success)
                log "story $STORY_KEY reached ${state}"
                return 0
                ;;
            failed | error | interrupted | blocked)
                fail "story $STORY_KEY ended in '${state}'"
                ;;
        esac
        sleep "$POLL_INTERVAL_S"
    done
    fail "timed out after ${POLL_TIMEOUT_S}s waiting for $STORY_KEY to reach a terminal state (last: ${state})"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

STEP="preflight"
command -v docker >/dev/null 2>&1 || fail "docker is not installed or not on PATH"
docker compose version >/dev/null 2>&1 || fail "the Docker Compose v2 plugin is missing"
log "temp dir: $TMP"

seed_repository
write_plan
write_scripted_registry
start_model_server
write_override
seed_credentials
install_shim
# PATH goes into the same 0600 file, but only now: install_shim prepends $TMP/bin,
# and the escalated `$SUDO bash -c` sources this file (env_reset drops an exported
# PATH), so a write before the shim install would send install.sh at the real
# docker and the compose override would never be applied. Appended, never
# rewritten: seed_credentials truncated the file above.
printf 'PATH=%s\n' "$PATH" >>"$ENV_FILE"
run_installer

STEP="mcp-initialize"
mint_token
mcp_initialize
check_metadata
call_tools
poll_for_terminal_state

log "PASSED: the standalone install ran a repo_url plan to a terminal state"
