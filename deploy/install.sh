#!/usr/bin/env bash
#
# Interactive installer for the standalone pipeline server (SA-16).
#
# Takes a clean machine with Docker from nothing to a running stack
# (deploy/standalone/compose.yaml) and prints how to point Claude Code and
# OpenCode at it.
#
#   bash deploy/install.sh                    # interactive
#   bash deploy/install.sh --non-interactive \
#       --anthropic-key-file ~/keys/anthropic --hostname pipeline.example.internal
#
# Flags:
#   --hostname NAME              public hostname clients use (default:
#                                pipeline.example.internal)
#   --port PORT                  public TLS port (default: 8443)
#   --admin-user NAME            Keycloak bootstrap admin (default: admin)
#   --first-user NAME            user to create in the `pipeline` realm
#   --repo-hosts HOST[,HOST...]  hosts the pipeline may clone from (default:
#                                github.com)
#   --ollama-endpoint URL        Ollama base URL (default:
#                                http://host.docker.internal:11434)
#   --cli-callback-port PORT     local OAuth callback port for CLI clients
#                                (default: 33418)
#   --anthropic-key-file PATH    file holding the Anthropic API key
#   --claude-oauth-token-file PATH  file holding a Claude Code OAuth token
#   --gh-token-file PATH         file holding a GitHub token
#   --ollama-key-file PATH       file holding an Ollama API key
#   --first-user-password-file PATH  file holding the first user's password
#   --state-dir DIR              where .env, secrets/ and import/realm.json are
#                                written (default: deploy/standalone)
#   --non-interactive            never prompt; take everything from flags/env and
#                                secrets only from files (required for CI)
#   --dry-run                    do steps 2-3 only: write the state directory and
#                                print the compose command instead of running it
#   -h, --help                   show this help and exit 0
#
# Render-only mode (no Docker, no sudo, no state directory):
#   bash deploy/install.sh --render-realm SRC DST
# Resolves every ${env.*} placeholder in SRC into DST (mode 0600) and exits.
# Missing or empty variables abort with the variable's name on stderr.
#
# Secrets are never accepted on the command line and never printed. Interactive
# mode reads them with `read -rs` (no echo); non-interactive mode reads them only
# from the `--*-file` flags. Generated secrets (Keycloak admin password, OIDC
# client secrets) are created once and then reused, so re-running the installer
# never silently rotates a credential.
#
# Requirements: bash, Docker with the compose plugin, curl, openssl (or
# /dev/urandom), and a checkout of this repository at its usual path (the
# printed Claude Code helper path is absolute).

set -euo pipefail

# Every file this script creates is private from the moment it exists.
umask 077

COMPOSE_REL="deploy/standalone/compose.yaml"
REALM="pipeline"
MCP_AUDIENCE="pipeline-mcp"
DEFAULT_HOSTNAME="pipeline.example.internal"
DEFAULT_PORT="8443"
DEFAULT_ADMIN_USER="admin"
DEFAULT_REPO_HOSTS="github.com"
DEFAULT_OLLAMA_ENDPOINT="http://host.docker.internal:11434"
DEFAULT_CALLBACK_PORT="33418"
DEFAULT_NOTIFY_EMAIL_PORT="587"
MIN_MEMORY_BYTES=6442450944 # 6 GiB
READY_TIMEOUT_SECONDS=180

# Secret files deploy/standalone/compose.yaml mounts. Every one must exist even
# when unused: `docker compose up` fails on a missing file and `config` does not
# check it. deploy/lib/operator_env.sh treats an empty file as absent.
COMPOSE_SECRETS="oidc_client_secret anthropic_api_key claude_code_oauth_token gh_token ollama_api_key slack_webhook_url notify_email_password"

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
COMPOSE_FILE="$REPO_ROOT/$COMPOSE_REL"

log() { printf '%s\n' "$*" >&2; }
fail() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Realm rendering (SA-G1).
#
# Keycloak's --import-realm substitutes ${env.*} only for variables that exist;
# anything unresolved is imported as literal text, which is how a redirect URI
# or a client secret ends up being the string "${env.PIPELINE_PUBLIC_URL}". So
# the template is rendered here, before the stack starts, and compose mounts the
# rendered copy (STANDALONE_REALM_FILE).
# ---------------------------------------------------------------------------

render_realm() { # $1 template, $2 rendered file
  local src="$1" dst="$2" tmp names placeholder name val remaining
  if [ ! -f "$src" ]; then
    printf 'render_realm: template not found: %s\n' "$src" >&2
    return 1
  fi
  # Rendering onto the template would replace the tracked file with resolved
  # client secrets and make every later run fail on "no placeholders found".
  if [ "$dst" -ef "$src" ]; then
    printf 'render_realm: refusing to overwrite the template: %s\n' "$dst" >&2
    return 1
  fi
  if ! mkdir -p -- "$(dirname -- "$dst")"; then
    printf 'render_realm: cannot create the directory for %s\n' "$dst" >&2
    return 1
  fi
  # `|| true` because grep exits 1 on no match and pipefail would end the
  # function before the friendly message below could be printed.
  names="$(grep -o '\${env\.[A-Za-z_][A-Za-z0-9_]*}' "$src" | sort -u || true)"
  if [ -z "$names" ]; then
    printf 'render_realm: no ${env.*} placeholders found in %s\n' "$src" >&2
    return 1
  fi
  # Render into a temp file and only publish it once everything resolved, so a
  # failed run leaves neither a partial realm.json nor a stale temp file behind.
  tmp="$(mktemp "${TMPDIR:-/tmp}/render_realm.XXXXXX")" || return 1
  if ! cp -- "$src" "$tmp"; then
    printf 'render_realm: cannot read %s\n' "$src" >&2
    rm -f -- "$tmp"
    return 1
  fi
  while IFS= read -r placeholder; do
    name="${placeholder#\$\{env.}"
    name="${name%\}}"
    eval "val=\${$name-}"
    if [ -z "$val" ]; then
      printf 'render_realm: required variable %s is unset or empty\n' "$name" >&2
      rm -f -- "$tmp" "$tmp.new"
      return 1
    fi
    # Portable sed: `sed -i` needs `-i ''` on macOS, so rewrite via a temp file.
    if ! sed "s|${placeholder}|${val}|g" "$tmp" >"$tmp.new"; then
      printf 'render_realm: failed to substitute %s\n' "$name" >&2
      rm -f -- "$tmp" "$tmp.new"
      return 1
    fi
    mv -- "$tmp.new" "$tmp"
  done <<<"$names"
  # A value containing `&` or a `|` can round-trip the placeholder back in; never
  # install a realm Keycloak would import with unresolved text.
  if grep -q '\${env\.' "$tmp"; then
    remaining="$(grep -c '\${env\.' "$tmp" || true)"
    printf 'render_realm: %s placeholders remain in %s\n' "$remaining" "$dst" >&2
    rm -f -- "$tmp" "$tmp.new"
    return 1
  fi
  chmod 0600 "$tmp"
  if ! mv -- "$tmp" "$dst"; then
    printf 'render_realm: cannot write %s\n' "$dst" >&2
    rm -f -- "$tmp" "$tmp.new"
    return 1
  fi
  chmod 0600 "$dst"
}

# `install.sh --render-realm SRC DST` only renders the template and stops: no
# docker, no compose, no sudo, no state directory. Handled before any other
# argument parsing so it cannot be reached after the stack has been touched.
if [ "${1-}" = "--render-realm" ]; then
  if [ "$#" -ne 3 ] || [ -z "${2-}" ] || [ -z "${3-}" ]; then
    printf 'install.sh: usage: bash deploy/install.sh --render-realm SRC DST\n' >&2
    exit 2
  fi
  render_status=0
  render_realm "$2" "$3" || render_status=$?
  exit "$render_status"
fi

warn() { printf 'install.sh: warning: %s\n' "$*" >&2; }

usage() {
  cat <<'USAGE'
Usage: bash deploy/install.sh [options]

Installs the standalone pipeline stack with Docker (deploy/standalone/compose.yaml)
and prints how to connect Claude Code and OpenCode.

Options:
  --hostname NAME              public hostname clients use (default: pipeline.example.internal)
  --port PORT                  public TLS port (default: 8443)
  --admin-user NAME            Keycloak bootstrap admin (default: admin)
  --first-user NAME            user to create in the pipeline realm
  --repo-hosts HOST[,HOST]     hosts the pipeline may clone from (default: github.com)
  --ollama-endpoint URL        Ollama base URL (default: http://host.docker.internal:11434)
  --cli-callback-port PORT     local OAuth callback port for CLI clients (default: 33418)
  --anthropic-key-file PATH    file holding the Anthropic API key
  --claude-oauth-token-file PATH  file holding a Claude Code OAuth token
  --gh-token-file PATH         file holding a GitHub token
  --ollama-key-file PATH       file holding an Ollama API key
  --first-user-password-file PATH  file holding the first user's password
  --notify-slack-webhook-file PATH  file holding a Slack incoming-webhook URL
  --notify-email-host HOST       enable email; SMTP relay host
  --notify-email-port PORT       SMTP port (default: 587)
  --notify-email-user USER       SMTP account name
  --notify-email-from ADDR       envelope From address
  --notify-email-to ADDR         recipient address
  --notify-email-password-file PATH  file holding the SMTP password
  --state-dir DIR              where .env and secrets/ are written (default: deploy/standalone)
  --non-interactive            never prompt; everything from flags/env, secrets only from files
  --dry-run                    write the state directory and print the compose command only
  -h, --help                   show this help and exit 0

Secrets are never taken from the command line and never printed. Interactive mode
reads them with `read -rs`; non-interactive mode reads them only from the
--*-file flags. Generated secrets are created once and reused on re-runs.
USAGE
}

NONINTERACTIVE=0
DRY_RUN=0
STATE_DIR="$SCRIPT_DIR/standalone"

HOSTNAME=""
PORT=""
ADMIN_USER=""
FIRST_USER=""
REPO_HOSTS=""
OLLAMA_ENDPOINT=""
CALLBACK_PORT=""
ANTHROPIC_KEY_FILE=""
CLAUDE_OAUTH_TOKEN_FILE=""
GH_TOKEN_FILE=""
OLLAMA_KEY_FILE=""
FIRST_USER_PASSWORD_FILE=""
NOTIFY_SLACK_WEBHOOK_FILE=""
NOTIFY_EMAIL_HOST=""
NOTIFY_EMAIL_PORT=""
NOTIFY_EMAIL_USER=""
NOTIFY_EMAIL_FROM=""
NOTIFY_EMAIL_TO=""
NOTIFY_EMAIL_PASSWORD_FILE=""

while [ "$#" -gt 0 ]; do
  arg="$1"
  case "$arg" in
    --*=*)
      key="${arg%%=*}"
      val="${arg#*=}"
      ;;
    *)
      key="$arg"
      val=""
      ;;
  esac

  case "$key" in
    -h | --help)
      usage
      exit 0
      ;;
    --non-interactive)
      NONINTERACTIVE=1
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    --state-dir | --hostname | --port | --admin-user | --first-user | --repo-hosts | \
      --ollama-endpoint | --cli-callback-port | --anthropic-key-file | \
      --claude-oauth-token-file | --gh-token-file | --ollama-key-file | \
      --first-user-password-file | --notify-slack-webhook-file | \
      --notify-email-host | --notify-email-port | --notify-email-user | \
      --notify-email-from | --notify-email-to | --notify-email-password-file)
      if [ -z "$val" ]; then
        [ "$#" -ge 2 ] || fail "option $key requires a value"
        val="$2"
        shift
      fi
      case "$key" in
        --state-dir) STATE_DIR="$val" ;;
        --hostname) HOSTNAME="$val" ;;
        --port) PORT="$val" ;;
        --admin-user) ADMIN_USER="$val" ;;
        --first-user) FIRST_USER="$val" ;;
        --repo-hosts) REPO_HOSTS="$val" ;;
        --ollama-endpoint) OLLAMA_ENDPOINT="$val" ;;
        --cli-callback-port) CALLBACK_PORT="$val" ;;
        --anthropic-key-file) ANTHROPIC_KEY_FILE="$val" ;;
        --claude-oauth-token-file) CLAUDE_OAUTH_TOKEN_FILE="$val" ;;
        --gh-token-file) GH_TOKEN_FILE="$val" ;;
        --ollama-key-file) OLLAMA_KEY_FILE="$val" ;;
        --first-user-password-file) FIRST_USER_PASSWORD_FILE="$val" ;;
        --notify-slack-webhook-file) NOTIFY_SLACK_WEBHOOK_FILE="$val" ;;
        --notify-email-host) NOTIFY_EMAIL_HOST="$val" ;;
        --notify-email-port) NOTIFY_EMAIL_PORT="$val" ;;
        --notify-email-user) NOTIFY_EMAIL_USER="$val" ;;
        --notify-email-from) NOTIFY_EMAIL_FROM="$val" ;;
        --notify-email-to) NOTIFY_EMAIL_TO="$val" ;;
        --notify-email-password-file) NOTIFY_EMAIL_PASSWORD_FILE="$val" ;;
      esac
      ;;
    # Inline secrets stay out of argv: they land in `ps`, shell history and CI
    # logs. Refuse without echoing the value.
    --anthropic-key | --claude-oauth-token | --gh-token | --ollama-key | \
      --first-user-password | --client-secret | --password)
      fail "$key is not accepted: pass a file with --anthropic-key-file, --claude-oauth-token-file, --gh-token-file, --ollama-key-file or --first-user-password-file"
      ;;
    *)
      fail "unknown option: $key (try --help)"
      ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Defaults, resolved in this order: flag > existing .env (re-run) > built-in.
# ---------------------------------------------------------------------------

STATE_DIR="${STATE_DIR%/}"
[ -n "$STATE_DIR" ] || fail "--state-dir must not be empty"
ENV_FILE="$STATE_DIR/.env"
SECRETS_DIR="$STATE_DIR/secrets"

env_get() { # $1 key, $2 file -> prints value, non-zero when absent
  [ -f "$2" ] || return 1
  local line
  line="$(grep -E "^$1=" "$2" 2>/dev/null | tail -n 1 || true)"
  [ -n "$line" ] || return 1
  printf '%s' "${line#*=}"
}

resolve() { # $1 current value, $2 env key, $3 default -> prints value
  if [ -n "$1" ]; then
    printf '%s' "$1"
    return 0
  fi
  local existing
  existing="$(env_get "$2" "$ENV_FILE" || true)"
  if [ -n "$existing" ]; then
    printf '%s' "$existing"
    return 0
  fi
  printf '%s' "$3"
}

json_escape() { # $1 string -> JSON string body
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '%s' "$s"
}

HOSTNAME="$(resolve "$HOSTNAME" PUBLIC_HOSTNAME "$DEFAULT_HOSTNAME")"
PORT="$(resolve "$PORT" PUBLIC_PORT "$DEFAULT_PORT")"
ADMIN_USER="$(resolve "$ADMIN_USER" KEYCLOAK_ADMIN "$DEFAULT_ADMIN_USER")"
FIRST_USER="$(resolve "$FIRST_USER" PIPELINE_FIRST_USER "")"
REPO_HOSTS="$(resolve "$REPO_HOSTS" PIPELINE_REPO_URL_HOSTS "$DEFAULT_REPO_HOSTS")"
OLLAMA_ENDPOINT="$(resolve "$OLLAMA_ENDPOINT" OLLAMA_ENDPOINT "$DEFAULT_OLLAMA_ENDPOINT")"
CALLBACK_PORT="$(resolve "$CALLBACK_PORT" PIPELINE_CLI_CALLBACK_PORT "$DEFAULT_CALLBACK_PORT")"
# Notification channel settings: none of these are secrets, so the usual
# precedence (flag > existing .env > built-in) applies. The SMTP port has a
# built-in default of 587; --notify-email-port still wins over it.
NOTIFY_EMAIL_HOST="$(resolve "$NOTIFY_EMAIL_HOST" PIPELINE_NOTIFY_EMAIL_HOST "")"
NOTIFY_EMAIL_PORT="$(resolve "$NOTIFY_EMAIL_PORT" PIPELINE_NOTIFY_EMAIL_PORT "$DEFAULT_NOTIFY_EMAIL_PORT")"
NOTIFY_EMAIL_USER="$(resolve "$NOTIFY_EMAIL_USER" PIPELINE_NOTIFY_EMAIL_USER "")"
NOTIFY_EMAIL_FROM="$(resolve "$NOTIFY_EMAIL_FROM" PIPELINE_NOTIFY_EMAIL_FROM "")"
NOTIFY_EMAIL_TO="$(resolve "$NOTIFY_EMAIL_TO" PIPELINE_NOTIFY_EMAIL_TO "")"

is_port() {
  case "$1" in
    '' | *[!0-9]*) return 1 ;;
  esac
  [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

# ---------------------------------------------------------------------------
# Step 2: inputs. Prompts only when interactive; secrets only from files in
# non-interactive mode.
# ---------------------------------------------------------------------------

prompt_value() { # $1 label, $2 default -> prints the answer
  local answer=""
  printf '%s [%s]: ' "$1" "$2" >&2
  IFS= read -r answer || answer=""
  if [ -z "$answer" ]; then
    printf '%s' "$2"
  else
    printf '%s' "$answer"
  fi
}

prompt_secret() { # $1 label -> prints the typed value; never echoed
  local value=""
  printf '%s (input hidden): ' "$1" >&2
  # -r: no backslash mangling; -s: do not echo the secret to the terminal.
  IFS= read -rs value || value=""
  printf '\n' >&2
  printf '%s' "$value"
}

if [ "$NONINTERACTIVE" -eq 0 ]; then
  HOSTNAME="$(prompt_value "Public hostname" "$HOSTNAME")"
  PORT="$(prompt_value "Public TLS port" "$PORT")"
  ADMIN_USER="$(prompt_value "Keycloak admin user" "$ADMIN_USER")"
  FIRST_USER="$(prompt_value "First pipeline user (blank to skip)" "$FIRST_USER")"
  REPO_HOSTS="$(prompt_value "Hosts the pipeline may clone from (comma separated)" "$REPO_HOSTS")"
  OLLAMA_ENDPOINT="$(prompt_value "Ollama endpoint" "$OLLAMA_ENDPOINT")"
  CALLBACK_PORT="$(prompt_value "CLI OAuth callback port" "$CALLBACK_PORT")"
  printf 'Notifications are optional; leave answers blank to disable.\n' >&2
  NOTIFY_EMAIL_HOST="$(prompt_value "SMTP host (blank to skip email)" "$NOTIFY_EMAIL_HOST")"
  if [ -n "$NOTIFY_EMAIL_HOST" ]; then
    NOTIFY_EMAIL_PORT="$(prompt_value "SMTP port" "$NOTIFY_EMAIL_PORT")"
    NOTIFY_EMAIL_USER="$(prompt_value "SMTP user" "$NOTIFY_EMAIL_USER")"
    NOTIFY_EMAIL_FROM="$(prompt_value "Envelope From address" "$NOTIFY_EMAIL_FROM")"
    NOTIFY_EMAIL_TO="$(prompt_value "Notification recipient" "$NOTIFY_EMAIL_TO")"
  fi
fi

# A half-configured email channel is worse than no channel: refuse it here, in
# both interactive and non-interactive mode, rather than write a config the
# runtime would fail on.
if [ -n "$NOTIFY_EMAIL_HOST" ]; then
  [ -n "$NOTIFY_EMAIL_FROM" ] || fail "--notify-email-from is required when email notifications are enabled"
  [ -n "$NOTIFY_EMAIL_TO" ] || fail "--notify-email-to is required when email notifications are enabled"
  is_port "$NOTIFY_EMAIL_PORT" || fail "--notify-email-port must be a number between 1 and 65535 (got: $NOTIFY_EMAIL_PORT)"
fi

[ -n "$HOSTNAME" ] || fail "--hostname must not be empty"
is_port "$PORT" || fail "--port must be a number between 1 and 65535 (got: $PORT)"
is_port "$CALLBACK_PORT" || fail "--cli-callback-port must be a number between 1 and 65535"
[ -n "$ADMIN_USER" ] || fail "--admin-user must not be empty"

read_secret_file() { # $1 path, $2 label -> prints the file's contents
  [ -n "$1" ] || return 0
  [ -f "$1" ] || fail "$2: not a readable file: $1"
  [ -r "$1" ] || fail "$2: not a readable file: $1"
  # The file is the only channel: never echo, never pass it on argv.
  cat -- "$1"
}

collect_secret() { # $1 label, $2 file path, $3 destination secret name
  local label="$1" path="$2" name="$3" value=""
  if [ -n "$path" ]; then
    value="$(read_secret_file "$path" "$label")"
  elif [ "$NONINTERACTIVE" -eq 0 ]; then
    value="$(prompt_secret "$label (blank to skip)")"
  fi
  SECRET_VALUES="$SECRET_VALUES
$name=$value"
}

SECRET_VALUES=""
collect_secret "Anthropic API key" "$ANTHROPIC_KEY_FILE" anthropic_api_key
collect_secret "Claude Code OAuth token" "$CLAUDE_OAUTH_TOKEN_FILE" claude_code_oauth_token
collect_secret "GitHub token" "$GH_TOKEN_FILE" gh_token
collect_secret "Ollama API key" "$OLLAMA_KEY_FILE" ollama_api_key
if [ -n "$NOTIFY_EMAIL_HOST" ]; then
  collect_secret "SMTP password" "$NOTIFY_EMAIL_PASSWORD_FILE" notify_email_password
fi
collect_secret "Slack incoming-webhook URL" "$NOTIFY_SLACK_WEBHOOK_FILE" slack_webhook_url

secret_value() { # $1 secret name -> prints the collected value
  local line
  line="$(printf '%s\n' "$SECRET_VALUES" | grep -E "^$1=" | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}

FIRST_USER_PASSWORD=""
if [ -n "$FIRST_USER" ]; then
  if [ -n "$FIRST_USER_PASSWORD_FILE" ]; then
    FIRST_USER_PASSWORD="$(read_secret_file "$FIRST_USER_PASSWORD_FILE" "First user password")"
  elif [ "$NONINTERACTIVE" -eq 0 ]; then
    FIRST_USER_PASSWORD="$(prompt_secret "Password for $FIRST_USER")"
    [ -n "$FIRST_USER_PASSWORD" ] || fail "the first user needs a password (or --first-user-password-file)"
  else
    log "install.sh: no --first-user-password-file; creating $FIRST_USER without a password"
  fi
fi

# ---------------------------------------------------------------------------
# Preflight (skipped for --dry-run, which never talks to Docker).
# ---------------------------------------------------------------------------

if [ "$DRY_RUN" -eq 0 ]; then
  [ -f "$COMPOSE_FILE" ] || fail "compose file not found: $COMPOSE_FILE"
  command -v docker >/dev/null 2>&1 || fail "docker is not installed or not on PATH"
  docker compose version >/dev/null 2>&1 || fail "the Docker Compose v2 plugin is missing (docker compose version failed)"
  command -v curl >/dev/null 2>&1 || fail "curl is required for the readiness probe"
  mem_total="$(docker info --format '{{.MemTotal}}' 2>/dev/null || printf '0')"
  case "$mem_total" in
    '' | *[!0-9]*) mem_total=0 ;;
  esac
  if [ "$mem_total" -gt 0 ] && [ "$mem_total" -lt "$MIN_MEMORY_BYTES" ]; then
    warn "Docker has under 6 GiB of memory ($mem_total bytes); the stack may be killed by the OOM reaper"
  fi
fi

# ---------------------------------------------------------------------------
# Step 3: secret files and .env. Existing files are never rewritten, so a
# re-run never silently regenerates a credential.
# ---------------------------------------------------------------------------

mkdir -p -- "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

rand_hex() {
  if command -v openssl >/dev/null 2>&1; then
    # 32 bytes of randomness, hex encoded: 64 characters.
    openssl rand -hex 32
  else
    od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
  fi
}

looks_like_placeholder() { # $1 value -> 0 when it is the example placeholder
  case "$1" in
    *[Cc][Hh][Aa][Nn][Gg][Ee]-[Mm][Ee]* | \
    *[Cc][Hh][Aa][Nn][Gg][Ee][Mm][Ee]*) return 0 ;;
  esac
  return 1
}

generated() { # $1 .env key -> existing value or a fresh random one
  local existing=""
  existing="$(env_get "$1" "$ENV_FILE" || true)"
  if [ -n "$existing" ]; then
    if looks_like_placeholder "$existing"; then
      fail "$1 in $ENV_FILE is still the example placeholder; delete the line and re-run"
    fi
    printf '%s' "$existing"
  else
    rand_hex
  fi
}

KEYCLOAK_ADMIN_PASSWORD="$(generated KEYCLOAK_ADMIN_PASSWORD)"
DASHBOARD_CLIENT_SECRET="$(generated PIPELINE_DASHBOARD_CLIENT_SECRET)"
MCP_CLIENT_SECRET="$(generated PIPELINE_MCP_CLIENT_SECRET)"

write_secret_if_absent() { # $1 name, $2 value
  local path="$SECRETS_DIR/$1"
  if [ -s "$path" ]; then
    # A kept value is about to be mounted as a credential: refuse a
    # placeholder that arrived from a hand-edited file or an old installer.
    if looks_like_placeholder "$(cat -- "$path")"; then
      fail "$1 in $path is still the example placeholder; delete it and re-run"
    fi
    # Keep the value: a re-run must never silently rotate a credential.
    # Make sure a umask accident cannot leave it group-readable.
    chmod 600 "$path" 2>/dev/null || true
    return 0
  fi
  # A missing or still-empty file is filled from this run's input. An empty file
  # is how the compose stack represents "unused", so this is safe to overwrite.
  if [ -n "$2" ]; then
    if looks_like_placeholder "$2"; then
      fail "$1 is still the example placeholder; pass a real credential"
    fi
    printf '%s' "$2" >"$path"
  elif [ ! -e "$path" ]; then
    : >"$path"
  fi
  chmod 600 "$path"
}

for name in $COMPOSE_SECRETS; do
  case "$name" in
    oidc_client_secret) value="$DASHBOARD_CLIENT_SECRET" ;;
    *) value="$(secret_value "$name")" ;;
  esac
  write_secret_if_absent "$name" "$value"
done
unset SECRET_VALUES

# notification.json is the notification config the runtime reads from
# STANDALONE_NOTIFY_CONFIG. It carries only env var *names*, never a secret
# value. "Enabled" is decided from persisted state -- the secret file on disk,
# not the file flag -- so a re-run without the flag keeps the channel the
# operator already configured.
NOTIFY_CONFIG_FILE="$STATE_DIR/notification.json"
NOTIFY_INSTANCES=""
NOTIFY_ROUTE_TO=""
if [ -n "$NOTIFY_EMAIL_HOST" ] && [ -n "$NOTIFY_EMAIL_FROM" ] && [ -n "$NOTIFY_EMAIL_TO" ]; then
  NOTIFY_INSTANCES="$(printf '"email":{"type":"email","host":"%s","port":%s,"user":"%s","from":"%s","to":["%s"],"password_env":"PIPELINE_NOTIFY_EMAIL_PASSWORD"}' \
    "$(json_escape "$NOTIFY_EMAIL_HOST")" "$NOTIFY_EMAIL_PORT" \
    "$(json_escape "$NOTIFY_EMAIL_USER")" "$(json_escape "$NOTIFY_EMAIL_FROM")" \
    "$(json_escape "$NOTIFY_EMAIL_TO")")"
  NOTIFY_ROUTE_TO='"email"'
fi
if [ -s "$SECRETS_DIR/slack_webhook_url" ]; then
  [ -z "$NOTIFY_INSTANCES" ] || NOTIFY_INSTANCES="$NOTIFY_INSTANCES,"
  NOTIFY_INSTANCES="${NOTIFY_INSTANCES}\"slack\":{\"type\":\"slack\",\"webhook_url_env\":\"SLACK_WEBHOOK_URL\"}"
  NOTIFY_ROUTE_TO="${NOTIFY_ROUTE_TO:+$NOTIFY_ROUTE_TO,}\"slack\""
fi
if [ -n "$NOTIFY_INSTANCES" ]; then
  printf '{"version":1,"instances":{%s},"routes":[{"name":"all","match":{"plan":"*"},"deliver":[{"events":["*"],"to":[%s]}]}]}\n' \
    "$NOTIFY_INSTANCES" "$NOTIFY_ROUTE_TO" >"$NOTIFY_CONFIG_FILE"
else
  printf '{"version":1,"instances":{},"routes":[]}\n' >"$NOTIFY_CONFIG_FILE"
fi
# A redirect onto an existing file keeps that file's old mode, so set 0644
# explicitly: the config carries no secret (only env var *names*), and the
# pipeline mounts it read-only into a container running as uid 10001. A 0600
# file owned by the host user is unreadable there, so the pipeline logs a load
# error and delivers nothing.
chmod 644 "$NOTIFY_CONFIG_FILE"

# Plan approval (SAD-01): safety gates default on (ADR 0004), so an ingested
# plan waits for approve_plan on a fresh install. Read the existing value with
# env_get, never resolve(): resolve() treats an explicit empty as absent and
# would flip an operator's deliberate `PIPELINE_PLAN_APPROVAL=` (off, per
# approval_enabled()) back to the default. Validate before the heredoc below
# rewrites .env, so an unusable value aborts without touching the file.
PLAN_APPROVAL=1
if existing_approval="$(env_get PIPELINE_PLAN_APPROVAL "$ENV_FILE")"; then
  PLAN_APPROVAL="$existing_approval"
fi
case "$PLAN_APPROVAL" in
  ""|on|1|true|off|0|false) ;;
  *)
    fail "PIPELINE_PLAN_APPROVAL=$PLAN_APPROVAL is not a value approval_enabled() accepts; use one of on, 1, true, off, 0, false, or empty for off"
    ;;
esac

if [ -f "$ENV_FILE" ]; then
  chmod 600 "$ENV_FILE" 2>/dev/null || true
fi
cat >"$ENV_FILE" <<EOF
# Written by deploy/install.sh (SA-16). Re-running the installer keeps existing
# values. This file holds generated client secrets: keep it out of git (it is
# gitignored) and out of logs.

# ---- Public origin --------------------------------------------------------

PUBLIC_HOSTNAME=$HOSTNAME
PUBLIC_PORT=$PORT
CADDY_TLS=internal

# ---- Code hosts and models ------------------------------------------------

PIPELINE_REPO_URL_HOSTS=$REPO_HOSTS
OLLAMA_ENDPOINT=$OLLAMA_ENDPOINT

# ---- Plan approval --------------------------------------------------------

PIPELINE_PLAN_APPROVAL=$PLAN_APPROVAL

# ---- Identity (Keycloak) --------------------------------------------------

KEYCLOAK_ADMIN=$ADMIN_USER
KEYCLOAK_ADMIN_PASSWORD=$KEYCLOAK_ADMIN_PASSWORD
PIPELINE_OIDC_AUDIENCE=$MCP_AUDIENCE
PIPELINE_CLI_CALLBACK_PORT=$CALLBACK_PORT
PIPELINE_DASHBOARD_CLIENT_SECRET=$DASHBOARD_CLIENT_SECRET
PIPELINE_MCP_CLIENT_SECRET=$MCP_CLIENT_SECRET

# ---- Notifications --------------------------------------------------------

STANDALONE_NOTIFY_CONFIG=$STATE_DIR/notification.json
# Notification settings are not secrets (the SMTP password and the Slack
# webhook live in secrets/), so they are recorded here and reused on a re-run
# exactly like every other non-secret value: flag > this file > built-in. An
# empty value means "channel disabled".
PIPELINE_NOTIFY_EMAIL_HOST=$NOTIFY_EMAIL_HOST
PIPELINE_NOTIFY_EMAIL_PORT=$NOTIFY_EMAIL_PORT
PIPELINE_NOTIFY_EMAIL_USER=$NOTIFY_EMAIL_USER
PIPELINE_NOTIFY_EMAIL_FROM=$NOTIFY_EMAIL_FROM
PIPELINE_NOTIFY_EMAIL_TO=$NOTIFY_EMAIL_TO
EOF
chmod 600 "$ENV_FILE"

log "install.sh: wrote $ENV_FILE and $SECRETS_DIR (mode 0600)"
log "install.sh: PIPELINE_PLAN_APPROVAL=$PLAN_APPROVAL: when on, an ingested plan dispatches only after its owner or a delegate runs approve_plan; off/0/false/empty disables approval"

# The realm template references the same ${env.*} names compose passes to
# Keycloak. Export this run's resolved values and render the template here: an
# import that saw a literal placeholder would produce dead redirect URIs and
# client secrets, so a rendering failure aborts before Docker is touched.
PIPELINE_PUBLIC_URL="https://$HOSTNAME:$PORT"
PIPELINE_CLI_CALLBACK_PORT="$CALLBACK_PORT"
PIPELINE_OIDC_AUDIENCE="$MCP_AUDIENCE"
PIPELINE_DASHBOARD_CLIENT_SECRET="$DASHBOARD_CLIENT_SECRET"
PIPELINE_MCP_CLIENT_SECRET="$MCP_CLIENT_SECRET"
export PIPELINE_PUBLIC_URL PIPELINE_CLI_CALLBACK_PORT PIPELINE_OIDC_AUDIENCE
export PIPELINE_DASHBOARD_CLIENT_SECRET PIPELINE_MCP_CLIENT_SECRET

# Render into a subdirectory of the state dir, never beside the template:
# STATE_DIR defaults to $SCRIPT_DIR/standalone, so "$STATE_DIR/realm.json" would
# be the tracked template itself. A first run would then overwrite realm.json
# with resolved client secrets and a re-run (documented as safe) would abort with
# "no ${env.*} placeholders found". import/ only ever holds rendered files.
RENDERED_REALM_FILE="$STATE_DIR/import/realm.json"
mkdir -p -- "$STATE_DIR/import"
chmod 700 "$STATE_DIR/import"
render_realm "$REPO_ROOT/deploy/standalone/realm.json" "$RENDERED_REALM_FILE"
log "install.sh: rendered $RENDERED_REALM_FILE"

# The compose file bind-mounts $STATE_DIR/model_registry.json as the server's
# registry. It names the provider/model each role resolves to, so it is
# operator-specific: seed it once, then never overwrite it on a re-run. Prefer
# the operator's local registry (the same file the non-container server uses)
# and fall back to the shipped provider-neutral catalog.
REGISTRY_FILE="$STATE_DIR/model_registry.json"
if [ -f "$REGISTRY_FILE" ]; then
  log "install.sh: keeping $REGISTRY_FILE"
elif [ -f "$REPO_ROOT/model_registry.local.json" ]; then
  cp -- "$REPO_ROOT/model_registry.local.json" "$REGISTRY_FILE"
  log "install.sh: wrote $REGISTRY_FILE from model_registry.local.json"
else
  cp -- "$REPO_ROOT/model_registry.json" "$REGISTRY_FILE"
  log "install.sh: wrote $REGISTRY_FILE from model_registry.json; set roles.* to models you can reach"
fi
# Not a secret, and the pipeline container (uid 10001) must be able to read it;
# umask 077 would otherwise leave it unreadable to anyone but the installer.
chmod 644 "$REGISTRY_FILE"

if [ "$DRY_RUN" -eq 1 ]; then
  log "install.sh: dry run; state written, Docker not touched"
  printf 'STANDALONE_REALM_FILE=%s docker compose -f %s up -d --build --wait\n' \
    "$RENDERED_REALM_FILE" "$COMPOSE_REL"
  printf '# then poll https://%s:%s/realms/%s/.well-known/openid-configuration\n' \
    "$HOSTNAME" "$PORT" "$REALM"
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 4: bring the stack up and wait for the IdP to answer through the proxy.
# ---------------------------------------------------------------------------

compose() { (cd -- "$REPO_ROOT" && docker compose -f "$COMPOSE_REL" "$@"); }

# The pipeline container runs as uid 10001 and deploy/lib/operator_env.sh refuses
# a secret file owned by anyone else, so the files (and the directory they live
# in) must belong to that uid. Only root can hand them over; otherwise say
# exactly what to run rather than failing later inside the container.
SECRETS_UID=10001
if [ "$(id -u)" -eq 0 ]; then
  chown -R "$SECRETS_UID:$SECRETS_UID" "$SECRETS_DIR"
else
  warn "secret files are owned by uid $(id -u) but the pipeline container runs as uid $SECRETS_UID;"
  warn "if the stack fails to start, run: sudo chown -R $SECRETS_UID:$SECRETS_UID $SECRETS_DIR"
fi

log "install.sh: starting the stack (this builds the pipeline image on first run)"
# Must be exported in this shell: compose aborts on ${STANDALONE_REALM_FILE:?...}.
export STANDALONE_REALM_FILE="$RENDERED_REALM_FILE"
compose up -d --build --wait

ready_url="https://$HOSTNAME:$PORT/realms/$REALM/.well-known/openid-configuration"
log "install.sh: waiting for $ready_url"
deadline=$(( $(date +%s) + READY_TIMEOUT_SECONDS ))
ready=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  # -k is deliberate and confined to this probe: the certificate is issued by
  # Caddy's internal CA, which is not in the machine's trust store yet. Step 6
  # exports that root for real clients. This probe only asks "is the IdP up?",
  # so a liveness check without verification is the right trade here.
  if curl -fsS -k --max-time 5 -o /dev/null "$ready_url" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 3
done
[ "$ready" -eq 1 ] || fail "the IdP did not answer at $ready_url within ${READY_TIMEOUT_SECONDS}s; check 'docker compose -f $COMPOSE_REL logs keycloak proxy'"

# ---------------------------------------------------------------------------
# Step 5: the first user and their confidential client, through kcadm inside
# the Keycloak container. Passwords travel over stdin, never on argv.
# ---------------------------------------------------------------------------

kcadm() { # runs kcadm inside the keycloak container; stdin passes through
  compose exec -T keycloak /opt/keycloak/bin/kcadm.sh "$@"
}

keycloak_login() {
  # --password comes from stdin so it never appears in `ps` or a container log.
  # The single-quoted script is evaluated inside the container, so "$1" expands
  # there, not here. The Keycloak image ships bash for kcadm.sh.
  # shellcheck disable=SC2016
  printf '%s' "$KEYCLOAK_ADMIN_PASSWORD" |
    compose exec -T keycloak bash -c \
      'IFS= read -r pw; /opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 --realm master --user "$1" --password "$pw"' \
      _ "$ADMIN_USER"
}

client_id_for() { # $1 keycloak clientId -> prints internal id, empty when absent
  local id
  id="$(kcadm get clients -r "$REALM" -q "clientId=$1" --fields id --format csv --noquotes 2>/dev/null | tr -d '\r' | tail -n 1)"
  case "$id" in
    id | '') printf '' ;;
    *) printf '%s' "$id" ;;
  esac
}

CLIENT_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pipeline"
CLIENT_CONFIG="$CLIENT_CONFIG_DIR/mcp-client.json"

if [ -n "$FIRST_USER" ]; then
  log "install.sh: configuring Keycloak ($REALM realm)"
  compose exec -T keycloak test -x /opt/keycloak/bin/kcadm.sh ||
    fail "kcadm.sh is missing from the keycloak container; cannot provision $FIRST_USER"
  keycloak_login

  if [ -z "$(kcadm get users -r "$REALM" -q "username=$FIRST_USER" --fields id --format csv --noquotes 2>/dev/null | tr -d '\r' | tail -n 1 | grep -v '^id$' || true)" ]; then
    if [ -n "$FIRST_USER_PASSWORD" ]; then
      printf '{"username":"%s","enabled":true,"emailVerified":true,"credentials":[{"type":"password","value":"%s","temporary":false}]}' \
        "$(json_escape "$FIRST_USER")" "$(json_escape "$FIRST_USER_PASSWORD")" |
        kcadm create users -r "$REALM" -f -
      log "install.sh: created user $FIRST_USER"
    else
      printf '{"username":"%s","enabled":true,"emailVerified":true}' \
        "$(json_escape "$FIRST_USER")" |
        kcadm create users -r "$REALM" -f -
      log "install.sh: created user $FIRST_USER without a password"
    fi
  else
    log "install.sh: user $FIRST_USER already exists, leaving it alone"
  fi

  user_client="pipeline-user-$FIRST_USER"
  client_internal_id="$(client_id_for "$user_client")"
  if [ -z "$client_internal_id" ]; then
    kcadm create clients -r "$REALM" \
      -s "clientId=$user_client" \
      -s enabled=true \
      -s protocol=openid-connect \
      -s publicClient=false \
      -s serviceAccountsEnabled=true \
      -s standardFlowEnabled=false \
      -s directAccessGrantsEnabled=false >/dev/null
    log "install.sh: created confidential client $user_client"
  fi
  client_internal_id="$(client_id_for "$user_client")"
  [ -n "$client_internal_id" ] || fail "could not look up client $user_client"

  # The audience mapper is what makes the minted token acceptable to the
  # pipeline's OIDC verifier (PIPELINE_OIDC_AUDIENCE).
  if ! kcadm get "clients/$client_internal_id/protocol-mappers/models" -r "$REALM" \
    --fields name --format csv --noquotes 2>/dev/null | tr -d '\r' | grep -qx 'pipeline-audience'; then
    kcadm create "clients/$client_internal_id/protocol-mappers/models" -r "$REALM" \
      -s name=pipeline-audience \
      -s protocol=openid-connect \
      -s protocolMapper=oidc-audience-mapper \
      -s 'config."included.custom.audience"'="$MCP_AUDIENCE" \
      -s 'config."access.token.claim"'=true \
      -s 'config."id.token.claim"'=false \
      -s 'config."introspection.token.claim"'=true >/dev/null
    log "install.sh: added the $MCP_AUDIENCE audience mapper to $user_client"
  fi

  client_secret="$(kcadm get "clients/$client_internal_id/client-secret" -r "$REALM" \
    --fields value --format csv --noquotes 2>/dev/null | tr -d '\r' | tail -n 1)"
  case "$client_secret" in
    value | '') fail "could not read the client secret for $user_client" ;;
  esac

  mkdir -p -- "$CLIENT_CONFIG_DIR"
  chmod 700 "$CLIENT_CONFIG_DIR"
  # mcp-token refuses a group/other-readable file; write then lock down.
  (
    umask 077
    printf '{\n  "token_url": "https://%s:%s/realms/%s/protocol/openid-connect/token",\n  "client_id": "%s",\n  "client_secret": "%s"\n}\n' \
      "$HOSTNAME" "$PORT" "$REALM" "$user_client" "$client_secret" >"$CLIENT_CONFIG"
  )
  chmod 600 "$CLIENT_CONFIG"
  unset client_secret
  log "install.sh: wrote $CLIENT_CONFIG for deploy/standalone/mcp-token"
  unset FIRST_USER_PASSWORD
else
  log "install.sh: no --first-user given; skipping the Keycloak user and per-user client"
  log "install.sh: re-run with --first-user NAME --first-user-password-file FILE to add one"
fi

# ---------------------------------------------------------------------------
# Step 6: export Caddy's internal root CA and print the client configuration.
# ---------------------------------------------------------------------------

CA_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pipeline"
CA_FILE="$CA_DIR/ca.crt"
mkdir -p -- "$CA_DIR"
chmod 700 "$CA_DIR"
compose cp proxy:/data/caddy/pki/authorities/local/root.crt "$CA_FILE" >/dev/null 2>&1 ||
  fail "could not copy Caddy's root CA out of the proxy container; is CADDY_TLS=internal?"
chmod 644 "$CA_FILE"

MCP_URL="https://$HOSTNAME:$PORT/mcp"
HELPER="$REPO_ROOT/deploy/standalone/mcp-token"

cat <<EOF

The stack is up. One environment variable teaches Node clients to trust the
internal CA:

  export NODE_EXTRA_CA_CERTS=$CA_FILE

Claude Code, OAuth mode (opens a browser and stores the token itself):

  claude mcp add --transport http --client-id pipeline-cli --callback-port $CALLBACK_PORT pipeline $MCP_URL

Claude Code, helper mode (no browser; mints a token per request from
$CLIENT_CONFIG). Add to .mcp.json:

  {
    "mcpServers": {
      "pipeline": {
        "type": "http",
        "url": "$MCP_URL",
        "headersHelper": "$HELPER --cacert $CA_FILE"
      }
    }
  }

OpenCode (~/.config/opencode/opencode.json):

  {
    "mcp": {
      "pipeline": {
        "type": "remote",
        "url": "$MCP_URL",
        "oauth": { "clientId": "pipeline-cli" }
      }
    }
  }

Dashboard: https://$HOSTNAME:$PORT/ (Keycloak admin console under /realms/master)
Logs:      docker compose -f $COMPOSE_REL logs -f
EOF
