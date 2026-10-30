#!/bin/sh
# Operator environment for the container entrypoint: the operator's credentials,
# read from mounted secret files, and the repository roots the pipeline clones
# into.
#
# Sourced by deploy/entrypoint.sh before it starts anything. It starts nothing
# itself, which is why tests/smoke/test_entrypoint_secrets.py can source it
# directly and exercise the real shipped shell code.
#
# That split is deliberate. A "stop before the process start" switch inside the
# production entrypoint fails open: under dash - /bin/sh in the Debian-slim image
# and on the Ubuntu CI runners - a top-level `return` in an *executed* script
# stops it with status 0. Any invocation that reached such a branch (a rename in
# a derived image, a k8s `command:` override, a wrapper that copies the script, a
# stray variable leaking in from a shared .env) would then leave a container that
# looks healthy with the dashboard, the MCP transport and the scheduler all
# absent, exit 0, and no healthcheck firing. Here there is no branch to reach:
# every invocation of the entrypoint runs its start-up assertions.
#
# `set -eu` repeats the entrypoint's own mode (it only ever tightens the caller)
# so the library behaves the same when a test sources it on its own.
set -eu

log() {
    # Action + subject only: no paths, tokens or payloads (Secure by Design).
    echo "entrypoint: $*" >&2
}

die() {
    log "fatal: $1"
    exit 1
}

# GNU coreutils stat is what the image has; the BSD form keeps the checks
# working in a developer's macOS checkout, where the smoke tests run.
stat_mode() {
    stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null
}

stat_uid() {
    stat -c %u "$1" 2>/dev/null || stat -f %u "$1" 2>/dev/null
}

# True when $1 is a mount point: a directory whose device differs from its
# parent's. Used to refuse chmod-ing a path the container does not own (a bind
# mounted host directory), rather than silently repairing somebody else's tree.
# Unknown device numbers return false, which keeps the caller on its original
# tighten-then-verify path.
is_mount_point() {
    _mp_dev=$(stat -c %d "$1" 2>/dev/null || stat -f %d "$1" 2>/dev/null) || return 1
    _mp_parent_dev=$(stat -c %d "$1/.." 2>/dev/null || stat -f %d "$1/.." 2>/dev/null) \
        || return 1
    [ -n "$_mp_dev" ] && [ -n "$_mp_parent_dev" ] || return 1
    [ "$_mp_dev" != "$_mp_parent_dev" ]
}

# Credentials are mounted files, never environment values: an environment
# value is visible to `docker inspect` and in /proc/<pid>/environ of every
# child, including the agents. compose `secrets:` requires every listed file to
# exist, so the installer creates the unused ones empty; a missing or empty
# credential is therefore not an error here. The backend that needs it fails
# closed when it is used.
#
# Path indirection without eval: only the four names this image mounts are
# accepted, so no future edit can turn an operator-supplied value into a
# re-parsed assignment.
secret_file_path() {
    case "$1" in
        ANTHROPIC_API_KEY_FILE) printf '%s' "${ANTHROPIC_API_KEY_FILE:-$2}" ;;
        CLAUDE_CODE_OAUTH_TOKEN_FILE)
            printf '%s' "${CLAUDE_CODE_OAUTH_TOKEN_FILE:-$2}" ;;
        GH_TOKEN_FILE) printf '%s' "${GH_TOKEN_FILE:-$2}" ;;
        OLLAMA_API_KEY_FILE) printf '%s' "${OLLAMA_API_KEY_FILE:-$2}" ;;
        *) return 1 ;;
    esac
}

# load_secret <ENV_NAME> <FILE_ENV_NAME> <default path>
#
# The value is exported into this process, so every child of the entrypoint
# inherits it. Be exact about what removes it before a child starts - today
# only this:
# - pipeline.execution._AGENT_SECRET_ENV strips GH_TOKEN, GITHUB_TOKEN,
#   GITLAB_TOKEN, GLAB_TOKEN and CI_JOB_TOKEN from every agent child.
# - Nothing else. PIPELINE_ANTHROPIC_API_KEY, OLLAMA_API_KEY and
#   CLAUDE_CODE_OAUTH_TOKEN reach agent children as well, and the `claude`
#   CLI child inherits GH_TOKEN too, because
#   app.backend_claude._first_party_claude_env() only drops the provider
#   redirect variables. CLAUDE_CODE_OAUTH_TOKEN is also the token the agent
#   process authenticates with.
# So an agent - prompt-injectable code running as the same uid, whose Bash
# tool executes commands driven by repository content - can read the model
# keys, the OAuth token and (via the CLI child) the forge token. That is a
# tracked risk, not an accepted one; it is recorded in deploy/README.md,
# "Credentials", with the mitigations that bound it until a credential broker
# or a per-child scrub lands: scope every credential to what the pipeline
# needs, budget-cap the model account, and mount nothing an agent must not see.
# Changing this boundary means changing the strip sites above, not this file.
# The missing strips are deferred, not forgotten: SA-09 removes the model-key
# carrier from the `claude` CLI child and SF-03 adds the keys to the agent
# denylist (pipeline/execution.py::_AGENT_SECRET_ENV). Both files are outside
# this story's scope, which is why the deploy layer documents the gap instead
# of silently relying on it.
load_secret() {
    _secret_env=$1
    _secret_file_env=$2
    _secret_default=$3

    _secret_path=$(secret_file_path "$_secret_file_env" "$_secret_default") \
        || die "$_secret_env: unknown secret file variable $_secret_file_env"

    _secret_value=
    # Refuse a symlink before anything stat() resolves: `stat` follows links, so
    # without this any 0600 file the container uid owns (for example the OIDC
    # client secret) could be re-pointed at by ANTHROPIC_API_KEY_FILE and loaded
    # under a model-key name. The mount is a plain file written by the
    # orchestrator; a link is either a mistake or an attempt to widen what one
    # credential name can reach, so neither is accepted.
    if [ -h "$_secret_path" ]; then
        die "$_secret_env: secret file is a symlink; mount a regular file"
    fi
    if [ -f "$_secret_path" ]; then
        _secret_mode=$(stat_mode "$_secret_path") \
            || die "$_secret_env: cannot determine secret file mode"
        [ -n "$_secret_mode" ] \
            || die "$_secret_env: cannot determine secret file mode"
        # Group/other readable is a leak: any process in the container, including
        # an agent-spawned one, could read the credential from disk.
        [ $((0$_secret_mode & 077)) -eq 0 ] \
            || die "$_secret_env: secret file is group or other accessible; chmod 600 it"

        _secret_uid=$(stat_uid "$_secret_path") \
            || die "$_secret_env: cannot determine secret file owner"
        [ -n "$_secret_uid" ] \
            || die "$_secret_env: cannot determine secret file owner"
        # A file owned by someone else could be swapped between this check and
        # the read, so refuse it rather than trust a path we do not own.
        [ "$_secret_uid" = "$(id -u)" ] \
            || die "$_secret_env: secret file is not owned by this user"

        _secret_value=$(cat "$_secret_path") \
            || die "$_secret_env: secret file is not readable"
        # Empty, or nothing but whitespace, means "not mounted": an operator who
        # does not use this backend leaves the secret blank.
        case "$_secret_value" in
            *[![:space:]]*) ;;
            *) _secret_value= ;;
        esac
    else
        # Not mounted is not an error, but a directory in its place is a
        # misconfigured mount rather than an absent credential.
        if [ -e "$_secret_path" ]; then
            die "$_secret_env: secret file is not a regular file"
        fi
    fi

    if [ -z "$_secret_value" ]; then
        # Fail closed against inheritance: a credential never comes from an
        # environment value, so an ambient one is discarded here rather than
        # trusted as if the operator had mounted it.
        unset "$_secret_env"
        return 0
    fi

    # No eval: the assignment is performed by export itself, so the value is
    # never re-parsed as shell syntax.
    export "$_secret_env=$_secret_value"
    # Name only - the value never reaches a log line or a `die` message.
    log "loaded $_secret_env from secret file"
}

# Load every credential this image knows about, then derive the defaults that
# follow from them. Safe to call once, at start-up.
load_operator_credentials() {
    load_secret PIPELINE_ANTHROPIC_API_KEY ANTHROPIC_API_KEY_FILE /run/secrets/anthropic_api_key
    load_secret CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN_FILE /run/secrets/claude_code_oauth_token
    load_secret GH_TOKEN GH_TOKEN_FILE /run/secrets/gh_token
    load_secret OLLAMA_API_KEY OLLAMA_API_KEY_FILE /run/secrets/ollama_api_key

    # A forge token is the github token unless the operator says otherwise.
    if [ -n "${GH_TOKEN:-}" ] && [ -z "${PIPELINE_FORGE:-}" ]; then
        PIPELINE_FORGE=github
        export PIPELINE_FORGE
    fi

    # The clone path takes the token *path*, not the value, so the same mounted
    # file serves both the forge CLI and repo_clone.
    if [ -z "${PIPELINE_GIT_TOKEN_FILE:-}" ] && [ -e /run/secrets/gh_token ]; then
        PIPELINE_GIT_TOKEN_FILE=/run/secrets/gh_token
        export PIPELINE_GIT_TOKEN_FILE
    fi
}

# The repository root the server owns; everything the clone path is allowed to
# touch lives under PIPELINE_ALLOWED_REPO_ROOTS, which defaults to it.
#
# Mutable state belongs on the volume, so the directory is created with least
# privilege rather than inherited from the umask. Unlike a mounted credential, a
# root that cannot be created is fatal: a warning would leave
# PIPELINE_ALLOWED_REPO_ROOTS pointing at a path that does not exist, and
# pipeline.workspace.check_repo_root_allowed() resolves the allowlist entry
# without requiring it to exist - so the clone trust boundary would fail open at
# the first clone instead of at start-up. The container mounts its volume at
# /var/lib/pipeline (deploy/compose.yaml), so the default path is creatable
# there.
ensure_repos_root() {
    PIPELINE_REPOS_ROOT="${PIPELINE_REPOS_ROOT:-/var/lib/pipeline/repos}"
    # This path is operator-supplied and the mode tightening below mutates
    # whatever it resolves to, which for a bind mount is a host directory. A
    # symlink would let a typo (or a link planted by a process in the container)
    # chmod an unrelated tree, so refuse the link rather than follow it. stat()
    # follows links; nothing below is allowed to.
    if [ -h "$PIPELINE_REPOS_ROOT" ]; then
        die "PIPELINE_REPOS_ROOT is a symlink; point it at a real directory"
    fi
    if [ ! -d "$PIPELINE_REPOS_ROOT" ]; then
        [ ! -e "$PIPELINE_REPOS_ROOT" ] \
            || die "PIPELINE_REPOS_ROOT exists but is not a directory"
        mkdir -p "$PIPELINE_REPOS_ROOT" 2>/dev/null \
            || die "PIPELINE_REPOS_ROOT could not be created; mount a writable volume"
        chmod 0750 "$PIPELINE_REPOS_ROOT" 2>/dev/null \
            || die "PIPELINE_REPOS_ROOT could not be set to 0750"
    fi
    # A pre-existing root is the clone trust boundary, so it gets the same
    # fail-closed treatment as a mounted secret: tighten it to the created mode
    # and refuse to start if it stays group-writable or accessible to other
    # users (a writable root lets any process in the container plant a
    # repository an agent then runs). 0750 - group read/traverse, no other
    # access - is the norm here, so the mask is group write plus every "other"
    # bit, not the secrets' 077.
    if [ -d "$PIPELINE_REPOS_ROOT" ]; then
        _repos_mode=$(stat_mode "$PIPELINE_REPOS_ROOT") \
            || die "PIPELINE_REPOS_ROOT: cannot determine directory mode"
        if [ -n "$_repos_mode" ] && [ $((0$_repos_mode & 027)) -ne 0 ]; then
            # A mount point is somebody else's tree - for a bind mount, the
            # host's. Never chmod it from in here; the operator sets the mode on
            # the volume and a wrong mode is a start-up error, not a silent
            # repair of a directory this process does not own.
            if is_mount_point "$PIPELINE_REPOS_ROOT"; then
                die "PIPELINE_REPOS_ROOT is a mount point that is group writable or other accessible; chmod 750 the mounted directory"
            fi
            chmod 0750 "$PIPELINE_REPOS_ROOT" 2>/dev/null || true
            _repos_mode=$(stat_mode "$PIPELINE_REPOS_ROOT") \
                || die "PIPELINE_REPOS_ROOT: cannot determine directory mode"
            if [ -z "$_repos_mode" ] || [ $((0$_repos_mode & 027)) -ne 0 ]; then
                die "PIPELINE_REPOS_ROOT is group writable or other accessible; chmod 750 it"
            fi
        fi
    fi
    PIPELINE_ALLOWED_REPO_ROOTS="${PIPELINE_ALLOWED_REPO_ROOTS:-$PIPELINE_REPOS_ROOT}"
    export PIPELINE_REPOS_ROOT PIPELINE_ALLOWED_REPO_ROOTS
}
