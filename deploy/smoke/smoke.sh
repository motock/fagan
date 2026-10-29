#!/usr/bin/env bash
#
# End-to-end smoke run for the stack in deploy/compose.yaml.
#
# What it proves, in order, against a live stack:
#
#   1. A client-credentials token from the IdP is accepted by the remote MCP
#      transport over HTTPS through the Caddy proxy (the transport has no TLS of
#      its own, so a plain-HTTP success here would not prove the wiring).
#   2. `list_plans` answers over that transport.
#   3. A plan is ingested and approved as `owner`.
#   4. One story is dispatched on the scripted model and reaches "reviewed",
#      with the merge parked: PIPELINE_FORGE is unset, pipeline/forge.py drives
#      GitHub or GitLab only, and this stack has neither.
#   5. `stranger` is refused `approve_plan` and sees no plans.
#   6. `verify_audit` reports every chain intact.
#   7. The dispatch notification crossed the broker exactly once: one line in
#      the plan's notifications log, and the JetStream consumer shows it acked.
#   8. A `docker compose restart pipeline` mid-run loses and duplicates nothing:
#      still exactly one line, still one delivered / one acked.
#   9. The auth-fail limit answers 429 once it is exhausted.
#
# Ordering is not cosmetic. The failed-auth bucket is keyed by the CLIENT
# ADDRESS (pipeline/rate_limit.py's `failed_auth_key`), and every caller here
# arrives from the same address, so the 429 flood in step 9 locks this runner
# out for the token-bucket window. It runs last; no assertion that needs an
# answer from the pipeline may follow it.
#
# Every wait is bounded (POLL_INTERVAL_S x POLL_ATTEMPTS) and names the step it
# is waiting inside, so a hang is reported as a failure rather than a timeout.
#
# Usage:
#   bash deploy/smoke/smoke.sh
#
# Environment (all have defaults suited to deploy/compose.yaml):
#   COMPOSE_FILE        docker compose file            (deploy/compose.yaml)
#   SMOKE_BASE_URL      proxy origin                 (https://localhost:8443)
#   SMOKE_CA_FILE       Caddy internal CA to trust    (read from the caddy-data volume)
#   SMOKE_REALM         IdP realm                                         (pipeline)
#   SMOKE_PLAN_NAME     plan to ingest and dispatch                          (smoke)
#   SMOKE_STORY_KEY     story to dispatch                                 (SMOKE-1)
#   POLL_INTERVAL_S     seconds between polls                                  (2)
#   POLL_ATTEMPTS       polls before a bounded wait fails                     (60)
set -euo pipefail

STEP="startup"
# Name the failing step and stop. The trap is set before any step runs, so an
# unexpected `set -e` exit inside a step is still attributed to that step.
# The line number, never $BASH_COMMAND: a failing curl may carry a secret in
# its arguments, and the trap output goes to the job log.
trap 'echo "FAILED: ${STEP} (line ${LINENO})" >&2; exit 1' ERR

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/deploy/compose.yaml}"
SMOKE_BASE_URL="${SMOKE_BASE_URL:-https://localhost:8443}"
SMOKE_REALM="${SMOKE_REALM:-pipeline}"
SMOKE_PLAN_NAME="${SMOKE_PLAN_NAME:-smoke}"
SMOKE_STORY_KEY="${SMOKE_STORY_KEY:-SMOKE-1}"
SMOKE_STORY_TWO_KEY="${SMOKE_STORY_TWO_KEY:-SMOKE-2}"
POLL_INTERVAL_S="${POLL_INTERVAL_S:-2}"
POLL_ATTEMPTS="${POLL_ATTEMPTS:-60}"

PLAN_DIR_IN_CONTAINER="/var/lib/pipeline/plans"
NOTIFICATIONS_FILE=""
CA_FILE=""
SESSION_ID=""

log() { echo "smoke: $*"; }

fail() {
    echo "FAILED: ${STEP}: $*" >&2
    exit 1
}

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# ---------------------------------------------------------------------------
# The git origin bootstrap
# ---------------------------------------------------------------------------

# Create the bare origin and its working clone exactly once.
#
# Two properties matter, and both are why this is not in the service's own
# entrypoint: Docker creates a fresh named volume root-owned, so the non-root
# process the pipeline image runs as cannot write the first object into it; and
# a restart must keep the refs a story pushed, which an unconditional re-seed
# (rm -rf, or clone --bare every time) would drop - the refs would fall back to
# the single seed branch and the loss would look like a dropped event.
#
# So: guard first, seed once, then hand the volume back to the service uid.
seed_origin_if_missing() {
    local uid
    STEP="origin-seed"
    uid="$(_pipeline_uid)"
    if compose run --rm --no-deps -T --user 0:0 --entrypoint /bin/bash pipeline -c "
        set -euo pipefail
        if [ -e /git-origin/sample.git/HEAD ]; then
            echo 'smoke: sample origin already seeded; keeping its refs'
            chown -R ${uid}:${uid} /git-origin
            exit 0
        fi
        install -d -m 0750 /git-origin
        git init --bare /git-origin/sample.git >/dev/null
        git init -b main /git-origin/work >/dev/null
        printf '# sample origin\n\nThe bare repository the smoke run pushes story branches to.\n' > /git-origin/work/README.md
        git -C /git-origin/work add -A
        git -C /git-origin/work -c user.name='pipeline smoke' -c user.email='smoke@example.invalid' commit -q -m 'chore: seed the sample repository'
        git -C /git-origin/work remote add origin /git-origin/sample.git
        git -C /git-origin/work push -q --set-upstream origin main
        chown -R ${uid}:${uid} /git-origin
    "; then
        log "git origin is seeded"
    else
        fail "could not seed the git origin"
    fi
}

# The uid the pipeline image runs as, read from the image rather than assumed.
_pipeline_uid() {
    local uid
    uid="$(compose images --format '{{.ID}}' pipeline 2>/dev/null | head -1)"
    [ -n "$uid" ] || uid="$(compose ps -q pipeline 2>/dev/null | head -1)"
    if [ -n "$uid" ]; then
        docker image inspect --format '{{.Config.User}}' "$uid" 2>/dev/null | cut -d: -f1 \
            | grep -E '^[0-9]+$' && return 0
    fi
    echo "10001"
}

# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------

# Trust the proxy's own CA, copied out of the volume it keeps it on. Verifying
# the chain is the point of the check: `-k` would pass against any certificate.
resolve_ca() {
    local id
    id="$(compose ps -q proxy 2>/dev/null || true)"
    [ -n "$id" ] || fail "the proxy container is not running"
    CA_FILE="$(mktemp)"
    if ! docker cp "$id:/data/caddy/pki/authorities/local/root.crt" "$CA_FILE" 2>/dev/null; then
        fail "could not read the proxy's internal CA certificate"
    fi
}

# One JSON-RPC call to the streamable-http MCP endpoint, printing the raw body.
# The SDK's streamable transport answers either JSON or an SSE frame; both are
# handled by the caller's extractor.
mcp_post() {
    local token="$1" body="$2"
    local args=(-sS --fail-with-body -X POST "$SMOKE_BASE_URL/mcp"
        -H "Authorization: Bearer ${token}"
        -H "Content-Type: application/json"
        -H "Accept: application/json, text/event-stream")
    [ -n "$CA_FILE" ] && args+=(--cacert "$CA_FILE")
    [ -n "$SESSION_ID" ] && args+=(-H "Mcp-Session-Id: ${SESSION_ID}")
    local response headers
    headers="$(mktemp)"
    response="$(curl "${args[@]}" -D "$headers" -d "$body" 2>&1)" || {
        rm -f "$headers"
        fail "MCP request was refused"
    }
    local session
    session="$(grep -i '^mcp-session-id:' "$headers" | tr -d '\r' | awk '{print $2}' | tail -1)"
    [ -n "$session" ] && SESSION_ID="$session"
    rm -f "$headers"
    printf '%s' "$response"
}

# Pull the JSON-RPC payload out of a body that may be a bare JSON object or an
# SSE frame ("event: message" then "data: {...}").
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

mcp_initialize() {
    local token="$1"
    local body
    body='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"pipeline-smoke","version":"1"}}}'
    SESSION_ID=""
    mcp_post "$token" "$body" >/dev/null || fail "MCP initialize failed"
    [ -n "$SESSION_ID" ] || fail "the MCP transport returned no session id"
    # The SDK expects the initialized notification before any tool call.
    mcp_post "$token" '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null || true
}

# mcp_tool <token> <tool> <json-args> -> prints the tool's result JSON
mcp_tool() {
    local token="$1" tool="$2" args_json="$3"
    local call body
    call="$(python3 -c '
import json, sys
print(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": sys.argv[1], "arguments": json.loads(sys.argv[2])}}))
' "$tool" "$args_json")"
    body="$(mcp_post "$token" "$call")"
    printf '%s' "$body" | json_payload | python3 -c '
import json, sys
payload = json.load(sys.stdin)
if "error" in payload:
    print(json.dumps({"transport_error": payload["error"]}))
    raise SystemExit(0)
result = payload.get("result") or {}
structured = result.get("structuredContent")
if structured is not None:
    print(json.dumps(structured))
    raise SystemExit(0)
content = result.get("content") or []
if content and isinstance(content[0], dict) and "text" in content[0]:
    text = content[0]["text"]
    try:
        print(json.dumps(json.loads(text)))
    except json.JSONDecodeError:
        print(json.dumps({"text": text}))
    raise SystemExit(0)
print(json.dumps(result))
'
}

# HTTP status of a tool call, for the cases where the STATUS is the assertion
# (403 for a refused principal, 429 for an exhausted limiter).
mcp_status() {
    local token="$1" tool="$2" args_json="$3"
    local call
    call="$(python3 -c '
import json, sys
print(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": sys.argv[1], "arguments": json.loads(sys.argv[2])}}))
' "$tool" "$args_json")"
    local args=(-s -o /dev/null -w '%{http_code}' -X POST "$SMOKE_BASE_URL/mcp"
        -H "Content-Type: application/json"
        -H "Accept: application/json, text/event-stream")
    [ -n "$token" ] && args+=(-H "Authorization: Bearer ${token}")
    [ -n "$CA_FILE" ] && args+=(--cacert "$CA_FILE")
    [ -n "$SESSION_ID" ] && args+=(-H "Mcp-Session-Id: ${SESSION_ID}")
    curl "${args[@]}" -d "$call" || true
}

# ---------------------------------------------------------------------------
# Broker / state helpers (read inside the pipeline container)
# ---------------------------------------------------------------------------

# JetStream consumer accounting for this deployment, as
# "delivered=<n> acked=<n> pending=<n>" summed over every consumer.
jetstream_counts() {
    compose exec -T pipeline python3 -c '
import json, sys, urllib.request

with urllib.request.urlopen("http://127.0.0.1:8222/jsz?consumers=true", timeout=5) as response:
    data = json.loads(response.read().decode())

delivered = acked = pending = 0
for account in data.get("account_details") or []:
    for stream in account.get("stream_detail") or []:
        for consumer in stream.get("consumer_detail") or []:
            delivered += int(consumer.get("delivered", {}).get("consumer_seq", 0) or 0)
            acked += int(consumer.get("ack_floor", {}).get("consumer_seq", 0) or 0)
            pending += int(consumer.get("num_ack_pending", 0) or 0)
print(f"delivered={delivered} acked={acked} pending={pending}")
'
}

# Lines in the plan's notifications log, and how many mention the story.
notification_counts() {
    local story="$1"
    compose exec -T pipeline python3 -c '
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
story = sys.argv[2]
total = matching = 0
if path.exists():
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        total += 1
        if story in line:
            matching += 1
print(f"total={total} matching={matching}")
' "$NOTIFICATIONS_FILE" "$story"
}

story_status() {
    compose exec -T pipeline python3 -c '
import json, sys
from pathlib import Path

plan, story = sys.argv[1], sys.argv[2]
path = Path(sys.argv[1] + ".manifest.json") if False else Path("/var/lib/pipeline/plans") / f"{plan}.manifest.json"
manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
print((manifest.get("stories") or {}).get(story, {}).get("status", ""))
' "$SMOKE_PLAN_NAME" "$SMOKE_STORY_KEY"
}

wait_for() {
    local description="$1"
    shift
    local attempt=1
    while [ "$attempt" -le "$POLL_ATTEMPTS" ]; do
        if "$@"; then
            return 0
        fi
        sleep "$POLL_INTERVAL_S"
        attempt=$((attempt + 1))
    done
    fail "${description} did not settle after $((POLL_INTERVAL_S * POLL_ATTEMPTS))s"
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

step_token() {
    STEP="token"
    local response token
    response="$(curl -sS --fail-with-body --cacert "$CA_FILE" \
        -X POST "$SMOKE_BASE_URL/realms/$SMOKE_REALM/protocol/openid-connect/token" \
        -d grant_type=client_credentials \
        -d client_id=pipeline-mcp \
        --data-urlencode "client_secret=${PIPELINE_MCP_CLIENT_SECRET:?set PIPELINE_MCP_CLIENT_SECRET}" \
        2>&1)" || fail "the IdP refused the client-credentials grant"
    token="$(printf '%s' "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')" \
        || fail "the token response carried no access_token"
    [ -n "$token" ] || fail "the access token is empty"
    TOKEN_CLIENT="$token"
}

# A named principal's token, via the direct-grant client. Only the smoke stack's
# IdP enables that flow, and only for these two users.
user_token() {
    local username="$1" password="$2"
    local response
    response="$(curl -sS --fail-with-body --cacert "$CA_FILE" \
        -X POST "$SMOKE_BASE_URL/realms/$SMOKE_REALM/protocol/openid-connect/token" \
        -d grant_type=password \
        -d client_id=pipeline-smoke-user \
        -d "username=${username}" \
        --data-urlencode "password=${password}" \
        --data-urlencode "client_secret=${PIPELINE_SMOKE_USER_CLIENT_SECRET:?set PIPELINE_SMOKE_USER_CLIENT_SECRET}" \
        2>&1)" || fail "the IdP refused the direct grant for a named principal"
    printf '%s' "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

step_list_plans() {
    STEP="list_plans"
    mcp_initialize "$TOKEN_CLIENT"
    local result
    result="$(mcp_tool "$TOKEN_CLIENT" list_plans '{}')"
    [ -n "$result" ] || fail "list_plans returned nothing"
    printf '%s' "$result" | python3 -c 'import json,sys; json.load(sys.stdin)' \
        || fail "list_plans did not answer with JSON"
}

step_ingest() {
    STEP="ingest"
    # The plan file has to exist inside the pipeline container's PLAN_DIR, which
    # is the volume the MCP server reads.
    # `docker cp`, not `compose cp`: `-q` yields a container id, and
    # `compose cp` reads its first argument as a *service* name.
    docker cp "$REPO_ROOT/deploy/smoke/plan.json" \
        "$(compose ps -q pipeline):${PLAN_DIR_IN_CONTAINER}/${SMOKE_PLAN_NAME}.json" \
        || fail "could not copy the plan into the pipeline container"
    local result
    result="$(mcp_tool "$TOKEN_OWNER" ingest_plan "{\"plan_name\": \"${SMOKE_PLAN_NAME}\", \"overwrite\": true}")"
    # Name the refusal the server returned: the structured reason is the only
    # thing that says *why* (authorization, malformed plan, missing repo_root),
    # and a bare "refused" leaves the operator guessing.
    if printf '%s' "$result" | grep -q '"ok": *false'; then
        fail "ingest_plan refused the plan: $(printf '%s' "$result" | tr -d '\n' | cut -c1-400)"
    fi
}

step_approve() {
    STEP="approve"
    local result
    result="$(mcp_tool "$TOKEN_OWNER" approve_plan "{\"plan_name\": \"${SMOKE_PLAN_NAME}\"}")"
    # `if`, not `a | grep -q ... && fail`: under `set -o pipefail` a grep that
    # finds nothing makes the pipeline fail, so the `&& fail` form kills the
    # script exactly when the call succeeded.
    if printf '%s' "$result" | grep -q '"ok": *false'; then
        fail "approve_plan refused the plan for its owner: $(printf '%s' "$result" | tr -d '\n' | cut -c1-400)"
    fi
}

step_dispatch() {
    STEP="dispatch"
    local result
    result="$(mcp_tool "$TOKEN_OWNER" dispatch_story \
        "{\"plan_name\": \"${SMOKE_PLAN_NAME}\", \"story_key\": \"${SMOKE_STORY_KEY}\"}")"
    if printf '%s' "$result" | grep -q '"ok": *false'; then
        fail "dispatch_story refused the story: $(printf '%s' "$result" | tr -d '\n' | cut -c1-400)"
    fi
}

step_poll_reviewed() {
    STEP="poll-reviewed"
    local attempt=1 status=""
    while [ "$attempt" -le "$POLL_ATTEMPTS" ]; do
        status="$(story_status)"
        if [ "$status" = "reviewed" ]; then
            return 0
        fi
        if [ "$status" = "failed" ] || [ "$status" = "blocked" ]; then
            fail "the story ended in '${status}' instead of 'reviewed'"
        fi
        sleep "$POLL_INTERVAL_S"
        attempt=$((attempt + 1))
    done
    fail "the story never reached 'reviewed' within $((POLL_INTERVAL_S * POLL_ATTEMPTS))s"
}

step_stranger_denied() {
    STEP="stranger-denied"
    local status
    status="$(mcp_status "$TOKEN_STRANGER" approve_plan "{\"plan_name\": \"${SMOKE_PLAN_NAME}\"}")"
    [ "$status" = "403" ] || fail "approve_plan by a non-owner answered ${status}, expected 403"
}

step_stranger_no_plans() {
    STEP="stranger-no-plans"
    local result
    result="$(mcp_tool "$TOKEN_STRANGER" list_plans '{}')"
    # The stranger may list the directory the transport exposes, but the plan's
    # own contents must not be reachable: read_plan-style tools are refused.
    printf '%s' "$result" | grep -q "$SMOKE_PLAN_NAME" \
        && fail "a non-owner saw the plan it does not own"
    return 0
}

step_verify_audit() {
    STEP="verify_audit"
    local result
    result="$(mcp_tool "$TOKEN_OWNER" verify_audit '{}')"
    printf '%s' "$result" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
if payload.get("transport_error"):
    raise SystemExit("verify_audit was refused: " + json.dumps(payload["transport_error"]))
broken = [key for key, value in payload.items() if value is False]
if broken:
    raise SystemExit("audit chains reported not intact: " + ",".join(sorted(broken)))
if not payload:
    raise SystemExit("verify_audit returned no chains")
' || fail "verify_audit reported a broken chain"
}

step_broker_once() {
    STEP="broker-once"
    local counts
    counts="$(notification_counts "$SMOKE_STORY_KEY")"
    [ "$(printf '%s' "$counts" | sed 's/.*matching=//')" = "1" ] \
        || fail "expected exactly one notification line for the story, got ${counts}"
    wait_for "the JetStream consumer acking the dispatch notification" consumer_settled
    local js
    js="$(jetstream_counts)"
    [ "$(printf '%s' "$js" | sed 's/.*acked=//; s/ pending.*//')" -ge 1 ] \
        || fail "the JetStream consumer acknowledged nothing: ${js}"
    printf '%s' "$js" | grep -q 'pending=0' \
        || fail "the JetStream consumer still holds unacknowledged messages: ${js}"
}

consumer_settled() {
    local js
    js="$(jetstream_counts)" || return 1
    printf '%s' "$js" | grep -q 'pending=0'
}

step_restart_no_loss() {
    STEP="restart-no-loss"
    # Restart mid-run: the pipeline container goes down while the plan is in
    # flight. The broker keeps the message until it is acked, and the durable
    # consumer name is stable, so a restart must neither drop nor duplicate it.
    compose restart pipeline || fail "docker compose restart pipeline failed"

    wait_for "the restarted pipeline to accept MCP calls" pipeline_ready
    local counts before after
    before="$(notification_counts "$SMOKE_STORY_KEY")"
    wait_for "the restarted consumer to settle" consumer_settled
    after="$(notification_counts "$SMOKE_STORY_KEY")"

    [ "$before" = "$after" ] \
        || fail "the notification log changed across the restart (${before} then ${after})"
    [ "$(printf '%s' "$after" | sed 's/.*matching=//')" = "1" ] \
        || fail "the restart duplicated or lost the notification: ${after}"

    local js
    js="$(jetstream_counts)"
    printf '%s' "$js" | grep -q 'pending=0' \
        || fail "the restarted consumer holds unacknowledged messages: ${js}"
    case "$js" in
        *delivered=1*acked=1*) : ;;
        *) fail "expected the dispatch notification delivered once and acked once, got ${js}" ;;
    esac

    # Follow-up: a second dispatch must move the counters to exactly two. This
    # is what distinguishes "delivered once" from "delivered once by accident".
    local result
    result="$(mcp_tool "$TOKEN_OWNER" dispatch_story \
        "{\"plan_name\": \"${SMOKE_PLAN_NAME}\", \"story_key\": \"${SMOKE_STORY_TWO_KEY}\"}")"
    if printf '%s' "$result" | grep -q '"ok": *false'; then
        fail "the follow-up dispatch was refused: $(printf '%s' "$result" | tr -d '\n' | cut -c1-400)"
    fi
    wait_for "the second dispatch to be acked" consumer_settled
    local second
    second="$(jetstream_counts)"
    case "$second" in
        *delivered=2*acked=2*) : ;;
        *) fail "expected two delivered and two acked after the follow-up dispatch, got ${second}" ;;
    esac
}

pipeline_ready() {
    mcp_status "$TOKEN_OWNER" list_plans '{}' | grep -q '^200$'
}

step_auth_fail_429() {
    STEP="auth-fail-429"
    # LAST STEP. The failed-auth bucket is keyed by client address, so this
    # exhausts the runner's own allowance for the window: every later call from
    # this host would be answered 429 regardless of its bearer token.
    local limit="${PIPELINE_MCP_AUTH_FAIL_PER_MIN:-10}"
    local attempt status
    attempt=1
    while [ "$attempt" -le "$limit" ]; do
        status="$(mcp_status "" approve_plan "{\"plan_name\": \"${SMOKE_PLAN_NAME}\"}")"
        [ "$status" = "401" ] || fail "unauthenticated call ${attempt} answered ${status}, expected 401"
        attempt=$((attempt + 1))
    done
    # One more failure than the limit allows, then a call WITH a valid token:
    # the limiter refuses before authentication, so even a good token is 429.
    status="$(mcp_status "" approve_plan "{\"plan_name\": \"${SMOKE_PLAN_NAME}\"}")"
    [ "$status" = "429" ] || fail "exhausting the auth-fail limit answered ${status}, expected 429"
    status="$(mcp_status "$TOKEN_OWNER" list_plans '{}')"
    [ "$status" = "429" ] \
        || fail "a valid token was answered ${status} after the flood, expected 429"
}

main() {
    STEP="preflight"
    command -v docker >/dev/null 2>&1 || fail "docker is not on PATH"
    compose ps --status running --format '{{.Service}}' | grep -qx pipeline \
        || fail "the pipeline service is not running; bring the stack up first"
    resolve_ca
    seed_origin_if_missing

    # Name each step as it starts: a failure inside a command substitution can
    # exit without the ERR trap firing, and then the log says nothing at all.
    run_step() {
        STEP="$1"
        log "step: ${STEP}"
        "step_${STEP}"
    }

    run_step token
    TOKEN_OWNER="$(user_token owner "${PIPELINE_SMOKE_OWNER_PASSWORD:?set PIPELINE_SMOKE_OWNER_PASSWORD}")"
    TOKEN_STRANGER="$(user_token stranger "${PIPELINE_SMOKE_STRANGER_PASSWORD:?set PIPELINE_SMOKE_STRANGER_PASSWORD}")"
    NOTIFICATIONS_FILE="${PLAN_DIR_IN_CONTAINER}/${SMOKE_PLAN_NAME}.notifications.jsonl"

    run_step list_plans
    run_step ingest
    run_step approve
    run_step dispatch
    run_step poll_reviewed
    run_step stranger_denied
    run_step stranger_no_plans
    run_step verify_audit
    run_step broker_once
    run_step restart_no_loss
    run_step auth_fail_429

    STEP="done"
    log "all assertions passed: reviewed with the merge parked, one notification, one ack"
}

main "$@"
