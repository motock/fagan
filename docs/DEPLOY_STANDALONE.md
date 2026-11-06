# Deploying the pipeline as a standalone server

`deploy/install.sh` takes a clean machine with Docker to a running, TLS-fronted
pipeline that several people can reach from their own Claude Code or OpenCode.
This guide is the same installation by hand: for operators who cannot or will not
run the script, and for debugging a stack the script has already built.

The stack it installs is `deploy/standalone/compose.yaml`. That file, not this
one, is the contract — where the two disagree, the compose file wins and this
guide is the bug.

`deploy/compose.yaml` is a *different* file: the throwaway smoke stack (dev-mode
Keycloak, a scripted model, throwaway volumes). Do not deploy it.

## 1. What you get

`docker compose -f deploy/standalone/compose.yaml up -d` starts four containers.
Only one of them publishes a port on the host.

| Service | Image | Listens (inside) | Published on the host | Persistent state |
| --- | --- | --- | --- | --- |
| `proxy` | `caddy:2.8.4-alpine` | `8443` (TLS), plus the in-network site `proxy:8443` | `${PUBLIC_PORT}` → `8443` | `caddy-data` (internal CA and its private keys), `ca-public` (CA bundle, public material) |
| `keycloak` | `quay.io/keycloak/keycloak:26.0.7` | `8080` | none | `keycloak-data` (users, sessions, realm) |
| `pipeline` | built from `deploy/Dockerfile`, tag `${PIPELINE_IMAGE:-pipeline-standalone:local}` | `8000` (dashboard and OIDC login), `8080` (remote MCP) | none | `pipeline-state` at `/var/lib/pipeline` (plans, manifests, worktrees, the audit chain) |
| `nats` | `nats:2.10.22` | `4222` (JetStream) | none — it shares the `pipeline` container's network namespace, so the broker is loopback-only | `nats-data` (streams and consumer state) |

The `pipeline` container runs three processes as children of one entrypoint
(`deploy/entrypoint.sh`): the dashboard (`python -m uvicorn app.dashboard:app`),
the remote MCP transport (`streamable-http`), and the scheduler daemon. The
entrypoint stays in the foreground until one of them exits, so a container that
has lost half its service is restarted rather than left looking healthy.

The Caddyfile routes by path: `/mcp*` and `/.well-known/oauth-protected-resource*`
go to the pipeline's MCP port (8080); `/realms/*` and `/resources/*` go to
Keycloak; everything else is the dashboard on 8000. That is why the MCP endpoint
and the dashboard can share one origin and one certificate.

Volumes you must back up are `pipeline-state` and `keycloak-data`; see §7.

### Prerequisites

- **Docker with the Compose v2 plugin** (`docker compose version` must succeed).
  `docker` alone is not enough.
- **At least 6 GiB of memory available to the Docker VM**, not to the host. The
  installer compares `docker info --format '{{.MemTotal}}'` against `6442450944`
  bytes and warns below it. See §8 for the failure this prevents.
- **A hostname** that resolves to the machine for every client — `PUBLIC_HOSTNAME`.
  It is baked into the OIDC issuer, the redirect URIs and the certificate, so
  changing it later means re-provisioning, not editing one line.
- `curl` (the installer's readiness probe), `bash`, and `openssl` (or
  `/dev/urandom` — the installer falls back to `od -An -tx1 -N32`).
- A checkout of this repository, since the image is built from it.

### Checking the memory prerequisite

6 GiB is `6 × 1024³ = 6442450944` bytes. That is not 6,000,000,000 — "6 GB"
is about 5.6 GiB, and a stack sized for the former is under-provisioned on a
machine sized to the latter. Check the allocation Docker actually has:

```sh
docker info --format '{{.MemTotal}}'
# must be >= 6442450944
```

Checking `sysctl hw.memsize` (macOS), `free -b` (Linux) or "the host has 16 GB"
answers a different question: on Docker Desktop the VM's allocation is a setting,
not the host's RAM. A host with 32 GB can still hand Docker 4 GiB (§8).

## 2. Installing

### With the script

```sh
bash deploy/install.sh
```

It prompts for the public hostname, TLS port, Keycloak admin user, an optional
first pipeline user, the code hosts, the Ollama endpoint and the CLI callback
port, and reads every secret with `read -rs` (no echo). Re-running it is safe:
it keeps existing values and never silently rotates a generated credential.

For a scripted install (CI, or a machine with no TTY):

```sh
bash deploy/install.sh --non-interactive \
  --hostname pipeline.example.internal \
  --port 8443 \
  --first-user alice \
  --anthropic-key-file ~/keys/anthropic \
  --gh-token-file ~/keys/gh_token \
  --first-user-password-file ~/keys/alice-password
```

`--dry-run` writes the state directory and prints the compose command without
touching Docker — the fastest way to see exactly what the rest of this section
produces.

It also seeds `deploy/standalone/model_registry.json` once — from
`model_registry.local.json` when present, otherwise the shipped
`model_registry.json` — and never overwrites it on a re-run. Edit its `roles.*`
entries to name models the deployment can actually reach; a role with no
reachable model fails at dispatch, not at start-up.

Secrets are only ever accepted from files (`--anthropic-key-file`,
`--claude-oauth-token-file`, `--gh-token-file`, `--ollama-key-file`,
`--first-user-password-file`). Passing a secret as a flag value is an error by
design: an argv value lands in `ps`, shell history and CI logs.

### By hand

Each step below mirrors one step of `deploy/install.sh`, in the same order. Run
them from the repository root.

**Step 1 — choose the deployment directory.** The installer's `--state-dir`
defaults to `deploy/standalone`; `.env` and `secrets/` live there and the compose
file mounts them by relative path, so keep this directory. Every file created
below is private from the moment it exists:

```sh
cd /path/to/this/repository
umask 077
export STATE_DIR=deploy/standalone
mkdir -p "$STATE_DIR/secrets"
chmod 700 "$STATE_DIR/secrets"
```

**Step 2 — generate the credentials only the server needs.** Two OIDC client
secrets and the Keycloak admin password are random, not typed:

```sh
export KEYCLOAK_ADMIN_PASSWORD="$(openssl rand -hex 32)"
export PIPELINE_DASHBOARD_CLIENT_SECRET="$(openssl rand -hex 32)"
export PIPELINE_MCP_CLIENT_SECRET="$(openssl rand -hex 32)"
```

On a re-run, reuse the values already in `.env` rather than generating new ones.
New client secrets silently invalidate every existing session; a new Keycloak
admin password locks the console.

**Step 3 — write the secret files.** One file per credential, every one of them
present even when unused (see §3). Replace the placeholders with the real value,
or write an empty file for a credential you do not have:

```sh
printf '%s' "$PIPELINE_DASHBOARD_CLIENT_SECRET" > "$STATE_DIR/secrets/oidc_client_secret"
printf '%s' '<anthropic-api-key>'               > "$STATE_DIR/secrets/anthropic_api_key"
printf '%s' '<claude-code-oauth-token>'         > "$STATE_DIR/secrets/claude_code_oauth_token"
printf '%s' '<github-token>'                    > "$STATE_DIR/secrets/gh_token"
: > "$STATE_DIR/secrets/ollama_api_key"          # empty: this deployment has none
chmod 600 "$STATE_DIR"/secrets/*
chown -R 10001:10001 "$STATE_DIR/secrets"       # as root; see below
```

Two mistakes here account for most failed first starts:

- **Omitting the `chmod 600`** (or the `umask 077` before it). The entrypoint
  refuses a secret file that is group- or other-readable, so the container exits
  at start-up with a mode error instead of running with a loose credential.
- **Creating only the files you have a value for.** `docker compose up` fails on
  a *missing* file listed under `secrets:`, while `docker compose config` does
  not check it — so the stack builds cleanly and then refuses to start. Create
  all five; leave the unused ones empty.

The pipeline container runs as uid `10001` and the credential loader refuses a
secret file owned by anyone else. As root:

```sh
chown -R 10001:10001 "$STATE_DIR/secrets"
```

Not root? You will see the installer's warning and the files stay owned by you;
if the stack then fails to start, run that same `chown` with `sudo`.

**Step 4 — write the model registry.** Compose bind-mounts
`$STATE_DIR/model_registry.json` as the server's registry and refuses to start
without it. The installer seeds it once — from `model_registry.local.json` when
one exists, otherwise from the shipped `model_registry.json` — and never
overwrites an existing one, because the roles are operator-specific. Doing it by
hand:

```sh
cp model_registry.local.json "$STATE_DIR/model_registry.json"   # or model_registry.json
```

Edit `roles.dispatch` (and the other roles) so each names a provider and model
you can actually reach. The file is gitignored; leave it out of version control.

**Step 5 — write `.env`.** Compose reads it from the directory holding the
compose file, so it must be `deploy/standalone/.env`:

```sh
cat > "$STATE_DIR/.env" <<EOF
PUBLIC_HOSTNAME=pipeline.example.internal
PUBLIC_PORT=8443
CADDY_TLS=internal

PIPELINE_REPO_URL_HOSTS=github.com
OLLAMA_ENDPOINT=http://host.docker.internal:11434

KEYCLOAK_ADMIN=admin
KEYCLOAK_ADMIN_PASSWORD=$KEYCLOAK_ADMIN_PASSWORD
PIPELINE_OIDC_AUDIENCE=pipeline-mcp
PIPELINE_CLI_CALLBACK_PORT=33418
PIPELINE_DASHBOARD_CLIENT_SECRET=$PIPELINE_DASHBOARD_CLIENT_SECRET
PIPELINE_MCP_CLIENT_SECRET=$PIPELINE_MCP_CLIENT_SECRET
EOF
chmod 600 "$STATE_DIR/.env"
```

`CADDY_TLS=internal` issues the certificate from Caddy's own CA — right for a
private name. Set it to an email address instead (e.g. `CADDY_TLS=ops@example.com`)
to use ACME, which requires a publicly resolvable `PUBLIC_HOSTNAME`.
`PIPELINE_DASHBOARD_CLIENT_SECRET` and `PIPELINE_MCP_CLIENT_SECRET` are
substituted into the realm template at import time and *must* be set: Keycloak
only substitutes variables that exist, so an absent one leaves the literal
`${env.PIPELINE_DASHBOARD_CLIENT_SECRET}` as the client secret.

The other mistake that ruins a manual install is forgetting the compose project:
running `docker compose up` from a different directory, or without `-f`, brings
up the smoke stack (or nothing) against a different `.env`, and the resulting
containers look almost right. Always pass `-f` and let compose take the project
name from the file (`pipeline-standalone`).

**Step 6 — bring the stack up.**

```sh
docker compose -f deploy/standalone/compose.yaml up -d --build --wait
```

The first run builds the pipeline image. Then wait for the identity provider to
answer through the proxy:

```sh
curl -fsS -k --max-time 5 -o /dev/null \
  "https://pipeline.example.internal:8443/realms/pipeline/.well-known/openid-configuration"
```

The `-k` is confined to this one probe: the certificate comes from Caddy's
internal CA, which is not in the machine's trust store yet. Clients get that CA
in step 7. The installer polls this URL for up to 180 seconds.

**Step 7 — create the first user and its client.** Only if you want a user
created for you. It runs `kcadm` inside the Keycloak container, and the password
travels on stdin so it never appears in `ps`:

```sh
printf '%s' '<alice-password>' | docker compose -f deploy/standalone/compose.yaml \
  exec -T keycloak bash -c \
  'IFS= read -r pw; /opt/keycloak/bin/kcadm.sh config credentials \
     --server http://localhost:8080 --realm master --user "$1" --password "$pw"' \
  _ admin
```

With `kcadm` logged in, create the user in the `pipeline` realm, then a
confidential client named `pipeline-user-<name>` with service accounts enabled
and browser/direct grants off:

```sh
docker compose -f deploy/standalone/compose.yaml exec -T keycloak \
  /opt/keycloak/bin/kcadm.sh create users -r pipeline \
  -s username=alice -s enabled=true -s emailVerified=true

docker compose -f deploy/standalone/compose.yaml exec -T keycloak \
  /opt/keycloak/bin/kcadm.sh create clients -r pipeline \
  -s clientId=pipeline-user-alice -s enabled=true -s protocol=openid-connect \
  -s publicClient=false -s serviceAccountsEnabled=true \
  -s standardFlowEnabled=false -s directAccessGrantsEnabled=false
```

Then add the audience mapper — without it, `mcp-token`'s tokens are rejected by
the pipeline's OIDC verifier, which requires the `pipeline-mcp` audience:

```sh
docker compose -f deploy/standalone/compose.yaml exec -T keycloak \
  /opt/keycloak/bin/kcadm.sh create \
  "clients/$(docker compose -f deploy/standalone/compose.yaml exec -T keycloak \
     /opt/keycloak/bin/kcadm.sh get clients -r pipeline -q clientId=pipeline-user-alice \
     --fields id --format csv --noquotes | tr -d '\r' | tail -n 1)/protocol-mappers/models" \
  -r pipeline -s name=pipeline-audience -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper \
  -s 'config."included.custom.audience"=pipeline-mcp' \
  -s 'config."access.token.claim"=true' \
  -s 'config."id.token.claim"=false' \
  -s 'config."introspection.token.claim"=true'
```

Read the client secret and write the credentials file that `mcp-token` uses:

```sh
docker compose -f deploy/standalone/compose.yaml exec -T keycloak \
  /opt/keycloak/bin/kcadm.sh get clients/<internal-id>/client-secret -r pipeline \
  --fields value --format csv --noquotes
```

```sh
mkdir -p ~/.config/pipeline && chmod 700 ~/.config/pipeline
umask 077
cat > ~/.config/pipeline/mcp-client.json <<'EOF'
{
  "token_url": "https://pipeline.example.internal:8443/realms/pipeline/protocol/openid-connect/token",
  "client_id": "pipeline-user-alice",
  "client_secret": "<client-secret-from-keycloak>"
}
EOF
chmod 600 ~/.config/pipeline/mcp-client.json
```

`mcp-token` refuses this file if it is group- or other-accessible.

**Step 8 — trust and configure the clients.** Export Caddy's internal root CA
and point the clients at it:

```sh
docker compose -f deploy/standalone/compose.yaml \
  cp proxy:/data/caddy/pki/authorities/local/root.crt ~/.config/pipeline/ca.crt
chmod 644 ~/.config/pipeline/ca.crt
export NODE_EXTRA_CA_CERTS=~/.config/pipeline/ca.crt
```

§5 covers what to put in the Claude Code and OpenCode configuration.

## 3. Credentials

Every credential is a mounted **file**, never an environment value: an
environment value is visible to `docker inspect` and in `/proc/<pid>/environ`
of every child process, including the agents. The entrypoint's credential
loader (`deploy/lib/operator_env.sh`) reads each file and exports the matching
variable, so the same mounted file serves the dashboard, the MCP transport and
the agents that shell out to `gh` or `git`.

| Secret file (`deploy/standalone/secrets/`) | Becomes | Used by |
| --- | --- | --- |
| `oidc_client_secret` | `PIPELINE_OIDC_CLIENT_SECRET` (via `PIPELINE_OIDC_CLIENT_SECRET_FILE`) | dashboard login |
| `anthropic_api_key` | `PIPELINE_ANTHROPIC_API_KEY` | model calls over the Anthropic API |
| `claude_code_oauth_token` | `CLAUDE_CODE_OAUTH_TOKEN` | the `claude` CLI's own authentication |
| `gh_token` | `GH_TOKEN` | the forge CLI, and (derived) `PIPELINE_FORGE=github` and `PIPELINE_GIT_TOKEN_FILE` |
| `ollama_api_key` | `OLLAMA_API_KEY` | a protected local Ollama |

`PIPELINE_ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` are different
credentials, not two spellings of one: the first is a metered Anthropic API key
used for model calls, the second is a subscription token the `claude` CLI
authenticates with. Fill in the one your account actually has; leave the other
empty.

`PIPELINE_GIT_TOKEN_FILE` is derived, not set by hand: when `gh_token` yields a
value, the loader points it at that same file so `pipeline.repo_clone` can
authenticate a clone from a private repository. `PIPELINE_FORGE=github` is
derived the same way unless you set it explicitly.

### File rules

- **Mode `0600`, owner uid `10001`.** The loader refuses a file that is
  group- or other-readable and refuses a symlink. A file owned by anyone else is
  refused too, which is why the installer `chown`s the directory to `10001`.
- **An empty file means "unused".** The loader treats an empty or
  all-whitespace file as an absent credential and *unsets* the variable, so an
  ambient `GH_TOKEN` inherited from the environment is discarded rather than
  trusted. This is deliberate: a deployment without a GitHub token still needs
  the file to exist for compose, and must not end up with an empty string
  standing in for a credential.
- **A missing file is an error; an empty one is not.** `docker compose up`
  fails on a listed-but-absent secret file. Create every file, empty where
  unused.

A concrete illustration of the empty-file rule: a 0-byte `gh_token` means
`GH_TOKEN` is unset and every forge operation runs unauthenticated (fine for
public repositories, a rate-limited failure for anything else), while a
40-byte file means `GH_TOKEN` is set and clones of private repositories work.
An implementation that passed the empty string through as a real credential
would send an empty bearer token and fail with an authentication error instead
of simply not authenticating — two very different debugging sessions.

### A note on the subscription token

`CLAUDE_CODE_OAUTH_TOKEN` is a personal subscription token. **Before you put one
on a shared server, confirm that Anthropic's terms of service allow unattended,
server-side use of a subscription token** — a token minted for one person's
interactive use is not necessarily licensed for a machine that several people
drive. If they do not allow it, use `PIPELINE_ANTHROPIC_API_KEY` (a metered API
key) instead, or leave both empty and point the registry at a local model
(§4). This is the operator's call to make and record; this guide cannot make it
for you.

An agent process runs as the same uid as the server and its Bash tool executes
commands driven by repository content, so it can read whatever the container can
read — including these files' exported values. Bound that exposure rather than
assume it away: scope every credential to the minimum the pipeline needs, budget-cap
the model account, and do not mount a credential no agent should see.

## 4. Configuration reference

These variables go in `deploy/standalone/.env`. The compose file consumes them
and passes a few onward under a container-level name; the last column says what
breaks when the value is wrong.

| Variable | Purpose | Default | Example | Effect when wrong |
| --- | --- | --- | --- | --- |
| `PUBLIC_HOSTNAME` | The name every client uses | none — required, else compose refuses to start | `pipeline.example.com` | Baked into the OIDC issuer, both redirect URIs and the certificate. Changing it later invalidates every issued token and session. |
| `PUBLIC_PORT` | The TLS port clients reach | `8443` | `8443` | Published as `${PUBLIC_PORT}:8443` and interpolated into the issuer and resource URL. A port that does not match what clients dial is a 401 (§8), not a redirect. |
| `PIPELINE_DASHBOARD_ALLOWED_HOSTS` | `Host` header allowlist for the dashboard | set by compose to `${PUBLIC_HOSTNAME}` | `pipeline.example.com,pipeline.internal` | Comma-separated. A `Host` not on the list is refused; an empty list disables the check. |
| `PIPELINE_MCP_ALLOWED_HOSTS` | `Host` header allowlist for the MCP transport (DNS-rebinding protection) | set by compose to `${PUBLIC_HOSTNAME},${PUBLIC_HOSTNAME}:*`; unset means the SDK default | `pipeline.example.com,pipeline.example.com:*` | A request whose `Host` is not listed gets **421 Invalid Host** (§8). Setting `PUBLIC_HOSTNAME` alone is not enough if you override this list and leave the host out. |
| `PIPELINE_MCP_RESOURCE_URL` | The RFC 9728 identifier for this MCP server | set by compose to `https://${PUBLIC_HOSTNAME}:${PUBLIC_PORT}/mcp` | `https://pipeline.example.com:8443/mcp` | Must be absolute `https://`, with no userinfo, query or fragment, or the transport refuses to start. It is what a 401 challenge advertises as `resource_metadata`; a mismatch leaves clients unable to start a login flow (§8). |
| `PIPELINE_REPO_URL_HOSTS` | Hosts the pipeline may clone from | `github.com` | `github.com,git.example.com` | Comma-separated, hosts only, no scheme. A plan naming any other host is refused at clone time. Only `https://` URLs are accepted. |
| `PIPELINE_REPOS_ROOT` | Root of the managed clone volume | `/var/lib/pipeline/repos` (image default, on `pipeline-state`) | `/var/lib/pipeline/repos` | Where `https://<host>/<owner>/<repo>` is cloned. Must be a real directory, not a symlink; an explicit value that cannot be created is fatal. |
| `PIPELINE_ALLOWED_REPO_ROOTS` | Allowlist confining any workspace the server resolves or validates | the value of `PIPELINE_REPOS_ROOT`; a no-op when unset or blank | `/var/lib/pipeline/repos` | Comma-separated roots, matched component-wise on resolved paths. A workspace outside every root is denied. |
| `PIPELINE_MODEL_REGISTRY_PATH` | Which model registry the server reads | `/app/model_registry.local.json` in the container, a read-only bind of `deploy/standalone/model_registry.json` (seeded once by the installer and kept on re-runs) | `/app/model_registry.local.json` | The registry resolves the model for each role. Point it at a file whose `roles.dispatch` entry names a model you can actually reach. |
| `OLLAMA_ENDPOINT` | Base URL of a local model server | `http://host.docker.internal:11434` | `http://host.docker.internal:11434` | Passed to the container as `PIPELINE_LOCAL_ENDPOINT_OLLAMA`. `host.docker.internal` resolves on Linux too, via the `host-gateway` extra host. |
| `PIPELINE_CLI_CALLBACK_PORT` | Local port a CLI client's browser flow returns to | `33418` | `33418` | Substituted into the realm's `pipeline-cli` redirect URI as `http://localhost:<port>/callback`, so it must match `--callback-port` on the client (§5). Change it and the realm must be re-imported (§8). |
| `PIPELINE_PLAN_APPROVAL` | Gate that requires a plan to be approved before it can be dispatched | `1` (fresh install) | `PIPELINE_PLAN_APPROVAL=0` | An unrecognised value makes ingest fail closed with an error naming the variable (§6) |
| `PIPELINE_DASHBOARD_PUBLIC_URL` | Origin the review link in an approval notification is built from | set by compose to `https://${PUBLIC_HOSTNAME}:${PUBLIC_PORT:-8443}` | `https://pipeline.example.com:8443` | No trailing slash and no path: it is an origin, and the path comes from the next row (§6) |
| `PIPELINE_APPROVAL_LINK_PATH` | Path appended to that origin in an approval notification's review link | `/` | `/ext/approvals/` | A plain path only — no scheme, query or `..`. An invalid value falls back to `/` rather than failing a notification |
| `PIPELINE_DASHBOARD_PLUGINS` | Dashboard extensions to mount, in `module:attr` form | unset — no extension is mounted, and no approval page exists | `approvals_app:plugin` | Comma-separated. A bad entry stops dashboard startup instead of being skipped, and whoever sets it runs code in the server (§6) |
| `PIPELINE_OIDC_INTERNAL_BASE_URL` | In-network origin used for server-side OIDC discovery and token exchange | set by compose to `https://proxy:8443` | `https://proxy:8443` | The issuer (`KC_HOSTNAME`) is the public origin, which the pipeline cannot resolve inside the compose network. This origin replaces it only for requests the server makes; the browser still follows the public authorization endpoint and the `iss` claim is still the public issuer. Must be absolute `https://`. |

One row here is rendered rather than authored: the installer writes
`STANDALONE_NOTIFY_CONFIG` into this `.env`, pointing at
`deploy/standalone/notification.json`, and generates that file from its
`--notify-*` answers — an `email` instance, a `slack` instance, both or neither,
and an `all` route delivering every event to each channel. It holds environment
variable *names* only; the values stay in the `secrets/` files.

### The public origin, worked through

`PUBLIC_HOSTNAME`, `PUBLIC_PORT` and `PIPELINE_MCP_RESOURCE_URL` are one value
seen three ways, and the port is part of the identifier — not decoration.

- `PUBLIC_HOSTNAME=pipeline.example.com`, `PUBLIC_PORT=8443` →
  the MCP endpoint is `https://pipeline.example.com:8443/mcp` and the dashboard
  is `https://pipeline.example.com:8443/`. This is the default install.
- `PUBLIC_HOSTNAME=pipeline.example.com`, `PUBLIC_PORT=443` → clients use
  `https://pipeline.example.com/mcp` (the default HTTPS port is not written).
  The compose file interpolates the port verbatim, so the value it computes is
  `https://pipeline.example.com:443/mcp`. Those are different identifiers to an
  RFC 9728 client: if your clients dial the bare name, set
  `PIPELINE_MCP_RESOURCE_URL=https://pipeline.example.com/mcp` in a compose
  override as well.

The mistake to avoid is hard-coding `443` into prose or a client config while
the deployment is actually on `8443` — or writing `:443` into a URL that clients
reach without a port. Both produce a token the server will not accept, and the
error surfaces as a bare 401 (§8).

Setting `PUBLIC_HOSTNAME` is *not* by itself enough to serve a host: the MCP
transport also checks `Host` against `PIPELINE_MCP_ALLOWED_HOSTS`. If you
override that list and leave your hostname out, the request gets **421 Invalid
Host** even though the certificate, the DNS and the login all work.

### Reading `/api/health`

`ok` means the API answered; `healthy` and `degraded` carry the verdict. `/livez`
("restart me"; scheduler freshness only) and `/readyz` ("route to me"; adds the
broker and the outbox) are the anonymous probes; Docker alone never restarts an
unhealthy container. Never put a dependency check in a liveness probe. Untested
example, using the image's 30s interval and 5s timeout:

```yaml
livenessProbe: {httpGet: {path: /livez, port: 8000}, periodSeconds: 30, timeoutSeconds: 5}
readinessProbe: {httpGet: {path: /readyz, port: 8000}, periodSeconds: 30, timeoutSeconds: 5}
```

## 5. Connecting clients

> Remote-only setup at a glance — the OpenCode and Claude Code configs, the
> internal CA export, the removed local entry and the remote tool policy — is
> collected in [Connecting MCP clients (remote)](#connecting-mcp-clients-remote).
> This section is the long form: OAuth and helper modes, credentials, redirects.

Both clients support two modes. OAuth mode opens a browser, signs a human in,
and stores the resulting token itself — the right choice for a person at a
workstation. Helper mode (`mcp-token`) mints a token from a per-user Keycloak
client secret and needs no browser — the right choice for a headless machine,
a CI job or a shared box.

### Trusting the certificate first

With `CADDY_TLS=internal` the certificate comes from Caddy's own CA, which no
client trusts by default. Export it once (§2 step 7) and give Node clients the
path:

```sh
export NODE_EXTRA_CA_CERTS=~/.config/pipeline/ca.crt
```

Do not disable verification (`NODE_TLS_REJECT_UNAUTHORIZED=0`, `--insecure`) to
get past this: it turns a trust problem into a silent man-in-the-middle hole and
hides the real fault (§8).

### Claude Code

OAuth mode — one command, then a browser:

```sh
claude mcp add --transport http \
  --client-id pipeline-cli \
  --callback-port 33418 \
  pipeline https://pipeline.example.com:8443/mcp
```

`pipeline-cli` is a public PKCE client: it holds no secret, and the realm
already lists `http://localhost:<PIPELINE_CLI_CALLBACK_PORT>/callback` as a
redirect URI. `--callback-port` must be the same port as
`PIPELINE_CLI_CALLBACK_PORT`, or the browser returns to a URI the realm does not
list and the flow fails with a redirect-URI error.

Helper mode — no browser; the client mints a token per request in
`.mcp.json`:

```json
{
  "mcpServers": {
    "pipeline": {
      "type": "http",
      "url": "https://pipeline.example.com:8443/mcp",
      "headersHelper": "~/bin/mcp-token --cacert ~/.config/pipeline/ca.crt"
    }
  }
}
```

`deploy/standalone/mcp-token` prints `{"Authorization": "Bearer <token>"}` on
stdout, which is exactly what `headersHelper` expects. It reads
`~/.config/pipeline/mcp-client.json` (or `$PIPELINE_MCP_CREDENTIALS`, or
`--config`) and refuses a file that is group- or other-readable. `--cacert` is
what lets it reach a token endpoint behind the internal CA.

### OpenCode

OAuth mode, in `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "pipeline": {
      "type": "remote",
      "url": "https://pipeline.example.com:8443/mcp",
      "oauth": { "clientId": "pipeline-cli" }
    }
  }
}
```

OpenCode completes the browser flow against
`http://127.0.0.1:19876/mcp/oauth/callback`, which the realm already lists as a
redirect URI on the same `pipeline-cli` client — that port is OpenCode's, not
yours to configure, and it is why the realm carries a second redirect URI
alongside the configurable one.

If a browser flow is impossible, use the env-token workaround instead:

```sh
export PIPELINE_MCP_TOKEN="$(deploy/standalone/mcp-token --raw \
  --cacert ~/.config/pipeline/ca.crt)"
```

That token is short-lived and OpenCode does **not** refresh it. The lifespan is
the realm's `accessTokenLifespan` (900 seconds by default, §7). With a
3600-second lifespan: a token minted at 09:00 works at 09:59:59 and gets a 401
at 10:00:00. Re-run `mcp-token` at 10:05 and the replacement works until 11:05.
The mistakes to avoid are putting that `export` in a shell profile as though it
were a permanent credential, and assuming OpenCode will renew it. Both end in a
401 in the middle of work, and the fix is to mint a fresh token — or switch to
OAuth mode, which does refresh.

## 6. Using the server

### Plans name a repository by URL

A plan written for a shared server names `repo_url`, never `repo_root`. The
server clones the URL into its own managed root and derives `repo_root` itself:

```json
{
  "epic": "Billing",
  "repo_url": "https://github.com/example/billing",
  "stories": []
}
```

`repo_url` must be `https://` on a host listed in `PIPELINE_REPO_URL_HOSTS`.
Naming `repo_root` — a path on the operator's filesystem — is refused with
`a remote plan must name repo_url, not repo_root or a workspace`, and naming
both is refused with `a plan names repo_url or repo_root, not both`. `save_plan`
additionally refuses a `workspace` argument from a remote caller.

The mistake: a plan that was written and tested locally carries an absolute
`repo_root`, and copy-pasting it to the server produces a plan that is refused
on save — or, worse, one that appears to work until a dispatch tries to clone
into a path that does not exist on the server.

### Which tools a remote caller may use

The remote transport is **default-deny**. A tool is callable only if it is in
one of two lists in `pipeline/remote_tool_policy.py`; anything else — including a
tool added later — is refused before its handler runs. Refusals are recorded in
the host audit chain.

Mutating tools are authorized per plan, through the ADR 0009 policy. Since the
amendment of 2026-10-30, a plan's **owner and its delegates** may perform these
ten plan-scoped actions on that plan: `plan:approve`, `plan:reject`,
`plan:dispatch`, `plan:pause`, `story:patch`, `story:interrupt`,
`story:merge`, `decision:answer`, `story:mark_done`, and plan-scoped
`config:write`. Ownership is additive only, never applies to a `system:`
subject, and a plan with no owner falls back to the role rule.

| Tool | Required right | Who holds it |
| --- | --- | --- |
| `approve_plan` | `plan:approve` | owner or delegate |
| `reject_plan` | `plan:reject` | owner or delegate |
| `dispatch_story` | `plan:dispatch` | owner or delegate |
| `advance_pipeline` | `plan:dispatch`, per plan | owner or delegate of each plan it touches |
| `advance_all_plans` | `plan:dispatch`, per plan | as above; plans the caller does not own are skipped |
| `mark_story_in_progress` | `plan:dispatch` | owner or delegate |
| `review_story` | `plan:dispatch` | owner or delegate |
| `pause_plan` | `plan:pause` | owner or delegate |
| `resume_plan` | `plan:pause` | owner or delegate |
| `interrupt_story` | `story:interrupt` | owner or delegate |
| `patch_story` | `story:patch` | owner or delegate |
| `set_story_status` | `story:patch` | owner or delegate |
| `request_decision` | `story:patch` | owner or delegate |
| `mark_story_done` | `story:mark_done` | owner or delegate |
| `approve_merge` | `story:merge` | owner or delegate |
| `checkpoint`, `checkpoint_story` | `story:patch` | owner or delegate |
| `patch_plan` | plan-scoped `config:write` | owner or delegate |
| `ingest_plan`, `save_plan` | `plan:ingest` | the plan's owner; `save_plan` also refuses a remote `workspace` |
| `record_security_audit` | host-scoped `config:write` | **local operator only — not reachable remotely** |
| `check_story_status`, `get_effective_config`, `get_story_diff`, `list_decisions`, `list_ready_stories` | none beyond visibility | caller must own the plan or be its delegate; anything else gets one fixed "not available" reply |
| `list_plans`, `verify_audit` | none beyond visibility | results are filtered to visible plans; `verify_audit` also drops its `host` entry for remote callers |
| `check_usage` | none | host-level and unfiltered: deployment-wide counters that belong to no plan |
| `decompose_plan` | — | **never** remotely; refused by default deny |
| anything not listed above | — | **never** remotely; refused by default deny |

`record_security_audit` is the row worth reading twice. It appears in the
policy's enforced set, but its action is *host-scoped* `config:write`, which is
decided against a resource with no owner and no delegates — so the ownership
grant can never match it and only the local operator holds it. Over the remote
transport it is a denial. It resets the record the periodic security-audit
reminder trusts, and a forged record would silence that reminder for a whole
repository, so it stays on the server's own console.

Two mistakes this table exists to prevent: listing every tool as available to
anyone with a valid token (the policy is default-deny, and a valid token buys
nothing a plan's owner has not granted), and listing `record_security_audit` as
remote-callable because it appears in the enforced set (it is enforced, and the
enforcement refuses it).

### Plan approval

A fresh standalone install turns the plan-approval gate **on**. With the gate
on, `ingest_plan` records `approval.status = pending_approval` on the plan,
together with a digest of the authored content, and nothing dispatches until a
human approves it: the scheduler tick and `dispatch_story` both refuse an
unapproved plan. `approve_plan` moves the plan to `approved`; `reject_plan`
records `changes_requested` with a reason. Any later authored change — a
re-ingest, `patch_story` or `patch_plan` — resets the record to
`pending_approval`, so an approved plan cannot be edited into something that was
never reviewed. The full lifecycle, the manifest fields and the refusal
messages are the contract in [PLAN_APPROVAL.md](specs/PLAN_APPROVAL.md).

Approval goes through the same policy as every other mutating tool: it is
allowed for the plan's owner (the authenticated principal who first ingested it,
ADR 0006), for the subjects listed in the plan's top-level `delegates`, and for
the local operator. A `system:` principal such as the scheduler never can.
Self-approval is allowed but recorded — `self_approved` is set on the decision
record — so it stays visible in the audit chain. This matches the `approve_plan`
and `reject_plan` rows of the tool table above.

The default comes from the compose file, which resolves
`${PIPELINE_PLAN_APPROVAL-1}`, and from `install.sh`, which writes `1` into
`deploy/standalone/.env`. Only the standalone stack defaults it on: outside
that stack the server-level default is unchanged (off when unset), so the
REFERENCE.md row still describes it correctly.

To turn the gate off, set `PIPELINE_PLAN_APPROVAL=0` in
`deploy/standalone/.env` and recreate the pipeline container:

```sh
docker compose -f deploy/standalone/compose.yaml up -d
```

An unrecognised value is not treated as on or off: ingest fails closed with an
error that names the variable.

If you are enabling the gate on a server that already has plans, see the
Upgrading paragraph in §7 — each existing plan has to be re-ingested and
approved once.

### Approving plans

With the gate on, an ingested plan is written `pending_approval` and cannot be
dispatched until someone approves that exact authored content. The gate is
enabled with `PIPELINE_PLAN_APPROVAL=on`. The standalone compose file resolves
the variable as `${PIPELINE_PLAN_APPROVAL-1}`, so a **missing line means on**:
an `up -d` against a `.env` that never had the line — fresh or upgraded alike —
turns the gate on. It is off only when the file carries an explicit off value —
`0`, `off`, `false` or empty — either written by `install.sh` (which keeps an
existing install's gate off that way) or added by you. To set it yourself, add
the line to `deploy/standalone/.env` and recreate the container, or set it in a
compose override the way §4 shows override files being layered:

```yaml
services:
  pipeline:
    environment:
      PIPELINE_PLAN_APPROVAL: "on"
```

A `.env` line is easier; use an override only when the value must live outside
the env file.

Turning the gate on does **not** gate plans that were already ingested: those
carry no approval record, so they stop dispatching until each is re-ingested and
approved once. §7 ("Plan approval on an existing server") gives that sequence —
follow it before enabling the gate on a server that has plans.

**Who may approve.** The plan's owner (the principal who first ingested it), any
subject listed in the plan's top-level `delegates`, and the local operator. A
team lead who is not the owner must be *named in the plan's `delegates`*: holding
the `approver` role on its own grants no approval right. A `system:` principal
such as the scheduler never may, and a decision is refused for a plan whose
digest no longer matches the content shown.

**The three events.** Notifications fire on:

| Event | Fires when |
| --- | --- |
| `plan_pending_approval` | an ingest (or a role-config change) puts a plan in the approval queue |
| `plan_approved` | a decision approves the plan's current digest |
| `plan_changes_requested` | a decision asks for changes instead of approving |

Each notice carries identifiers (the plan name and the content digest), the story
counts with a risk summary, a warning count, and a review link — never the plan's
brief text, filesystem paths or any secret. The link is built from
`PIPELINE_DASHBOARD_PUBLIC_URL` (the compose file sets it to
`https://${PUBLIC_HOSTNAME}:${PUBLIC_PORT:-8443}`) and
`PIPELINE_APPROVAL_LINK_PATH` (default `/`). The link never approves anything: it
opens whatever review UI you run at that path, and the decision is still made
there or through the tool or API below.

**Core ships no browser approval page.** Approve a plan with the MCP
`approve_plan` tool, or over the digest-bound JSON API:

1. `GET /api/plans/{plan}/review` — the payload you review: the stored approval
   record, the plan's *current* content digest, and the projection of its stories
   and warnings.
2. `POST /api/plans/{plan}/review/approve` or
   `POST /api/plans/{plan}/review/request-changes` — send the digest you read
   (`{"digest": "..."}`, plus a non-empty `"reason"` when requesting changes).

A decision made against any other digest is refused with **409** — the plan
changed under you, so re-read and decide again. Both POSTs require a dashboard
login (a verified OIDC session); a shared-key or local-operator caller gets a
fixed "dashboard approval requires login" refusal and should use the MCP tool.

If you want a review UI, mount one through `PIPELINE_DASHBOARD_PLUGINS`: an
extension's routes are served under `/api/ext` (authenticated), and its static
pages under `/ext/<name>`. **Static files are served without authentication**, so
an extension's HTML/JS/CSS must hold no secrets — put tokens and data in its
`/api/ext` routes and let the page fetch them. Then point the notification link
at that page with `PIPELINE_APPROVAL_LINK_PATH`.

Route the notices per plan in `notification.json`. A route is one element of the
file's top-level `routes` array; the example below shows the element, so put it
inside that array (after the installer-generated ones, before the catch-all).
The first matching route wins, so put a specific route **before** any catch-all
`"plan": "*"` route:

```json
{"name": "approvals", "match": {"plan": "team-a-*"},
 "deliver": [{"events": ["plan_pending_approval"], "to": ["team-a-lead-mail"]},
             {"events": ["plan_approved", "plan_changes_requested"], "to": ["team-a-slack"]}]}
```

The mistake to avoid is leaving the catch-all first: every approval notice then
goes to the catch-all's channels and the team lead never hears about the plan.
The second is pointing `PIPELINE_APPROVAL_LINK_PATH` at a page that does not
exist — the link is appended as-is, so a wrong path is a 404 in every notice.

## 7. Upgrading, backup and token lifespan

### Upgrading

Every image tag in the compose file is exact (no `latest`), so an upgrade is a
reviewed change to that file, not an automatic pull.

```sh
git pull                     # or fetch the release you are moving to
docker compose -f deploy/standalone/compose.yaml build pipeline
docker compose -f deploy/standalone/compose.yaml up -d --wait
```

`up -d` recreates only the containers whose image or configuration changed and
leaves the volumes alone, so plans and Keycloak sessions survive. `--wait`
returns only once the healthchecks pass; without it you are racing the IdP.

Back up first (§below) whenever the upgrade moves the Keycloak image: a newer
Keycloak migrates its schema on start, and a migration is not reversible by
putting the old image back.

If `PIPELINE_IMAGE` was set to a published tag, `build` is a no-op and the new
tag is what gets pulled — either way, `up -d` is the step that swaps the
container.

After an upgrade, re-export the CA only if you recreated the stack from scratch;
`caddy-data` keeps the same root CA across a restart.

#### Plan approval on an existing server

The `up -d` sequence above does not run the installer. Compose resolves
`PIPELINE_PLAN_APPROVAL` as `${PIPELINE_PLAN_APPROVAL-1}`, so upgrading a
`deploy/standalone/.env` that has no `PIPELINE_PLAN_APPROVAL` line turns the
plan-approval gate **on**. Plans ingested before the upgrade carry no approval
record, so they stop dispatching until they are approved. To keep the old
behaviour, add `PIPELINE_PLAN_APPROVAL=0` to `deploy/standalone/.env` (or re-run
`bash deploy/install.sh`, which writes `0` for you) *before* `up -d`.

To turn the gate on for plans that already exist, set
`PIPELINE_PLAN_APPROVAL=1`, run `up -d`, then for each existing plan run
`ingest_plan` once and then `approve_plan`. `approve_plan` on its own is refused
with `plan content changed; re-ingest before approving`, because a plan
ingested while the gate was off has no stored digest to approve.

### Backup and restore

Two volumes hold state you cannot rebuild: **`pipeline-state`** (plans,
manifests, worktrees, the audit chain) and **`keycloak-data`** (users, sessions,
the realm). Back up both. `nats-data` holds undelivered events, and
`caddy-data`/`ca-public` hold the CA — back those up too if losing every
client's trust setup would hurt.

The volume names are prefixed with the compose project name,
`pipeline-standalone`:

```sh
mkdir -p ~/backup
docker run --rm \
  -v pipeline-standalone_pipeline-state:/data:ro \
  -v ~/backup:/backup \
  alpine tar czf /backup/pipeline-state.tgz -C /data .

docker run --rm \
  -v pipeline-standalone_keycloak-data:/data:ro \
  -v ~/backup:/backup \
  alpine tar czf /backup/keycloak-data.tgz -C /data .
```

Restore **both**, with the stack stopped — a restore under a running service is
a corrupt restore, because Keycloak's H2 database and the pipeline's audit chain
are both being written to:

```sh
docker compose -f deploy/standalone/compose.yaml stop

docker run --rm \
  -v pipeline-standalone_pipeline-state:/data \
  -v ~/backup:/backup \
  alpine sh -c 'rm -rf /data/..?* /data/.[!.]* /data/* && tar xzf /backup/pipeline-state.tgz -C /data'

docker run --rm \
  -v pipeline-standalone_keycloak-data:/data \
  -v ~/backup:/backup \
  alpine sh -c 'rm -rf /data/..?* /data/.[!.]* /data/* && tar xzf /backup/keycloak-data.tgz -C /data'

docker compose -f deploy/standalone/compose.yaml up -d --wait
```

The restore is a point-in-time snapshot of both volumes, so it is consistent
only if both archives were taken at (nearly) the same moment. Take them
together, and note when.

Worked example: back up both volumes at 10:05, then create a plan at 10:10.
Restore both archives and restart, and only the pre-10:05 plans exist — the
10:10 plan is gone. Create a new plan at 10:20 and it is present, because it was
written after the restore.

The mistakes: backing up only `pipeline-state` (you keep the plans but lose
every user, and nobody can log in to them), restoring only one of the two (the
plans reference principals Keycloak no longer has, or the reverse), and restoring
while the stack is running (the running H2 has the old pages cached and rewrites
them over your restored files).

### Changing the token lifespan

Access-token lifetime is the realm's `accessTokenLifespan`, in seconds, set in
`deploy/standalone/realm.json` (**900**, i.e. 15 minutes, by default). Longer
lifespans mean fewer 401s for `mcp-token` users and a longer window in which a
stolen token works; shorter ones are the reverse.

Change it in the Keycloak admin console (*Realm settings → Tokens → Access token
lifespan*) for a running realm. Editing `realm.json` and restarting is **not**
enough once the realm exists — `--import-realm` only creates a realm that does
not exist yet (§8).

Whichever route you take, the change applies to tokens **issued after it**. A
JWT's expiry is fixed at issue time and nothing retroactive is possible:

- A token issued at 09:00 with a 3600-second lifespan expires at 10:00.
- At 09:30 you raise the default lifespan to 7200 and restart. That token still
  expires at 10:00 — the restart does not re-sign it, and neither does editing
  `realm.json`.
- A token issued at 09:35 expires at 11:35, because it was minted under the new
  setting.
- The 09:00 token does not become valid again after 10:00; its holder must mint
  a new one.

The mistake is claiming — in a runbook, a ticket or a change note — that a
restart or a realm-JSON edit extends tokens already in the wild. It does not.
Plan a lifespan change the way you plan any credential rotation: announce it,
expect the holders of old tokens to re-mint, and do not shorten a lifespan
during a window when clients cannot easily re-authenticate.

## 8. Troubleshooting

Start with the logs, which name the container that refused to start:

```sh
docker compose -f deploy/standalone/compose.yaml ps
docker compose -f deploy/standalone/compose.yaml logs keycloak proxy pipeline
```

### Untrusted CA

A Node client reports `unable to verify the first certificate`,
`SELF_SIGNED_CERT_IN_CHAIN` or `x509: certificate signed by unknown authority`;
Python reports `CERTIFICATE_VERIFY_FAILED`. The cause is `CADDY_TLS=internal`:
the certificate comes from Caddy's own CA, which the client does not trust.

Fix it by teaching the client about the CA, not by disabling verification:

```sh
export NODE_EXTRA_CA_CERTS=~/.config/pipeline/ca.crt   # Claude Code, OpenCode
deploy/standalone/mcp-token --cacert ~/.config/pipeline/ca.crt --raw
export SSL_CERT_FILE=~/.config/pipeline/ca.crt         # Python clients
```

Regenerate `~/.config/pipeline/ca.crt` if the stack was recreated from an empty
`caddy-data`, and remember that `NODE_EXTRA_CA_CERTS` must be set in the
environment that starts the client — an `export` in another shell does nothing.
Setting `NODE_TLS_REJECT_UNAUTHORIZED=0` "works" and is the wrong fix: it hides
this class of error and accepts any certificate.

### 401 whose challenge names `resource_metadata`

The client gets `401` with
`WWW-Authenticate: Bearer resource_metadata="https://…/.well-known/oauth-protected-resource"`.
That is the server telling the client where to discover how to log in; it is
working as designed. The failure is almost always that the metadata names a
resource identifier the client did not request.

The resource URL must be the *public* one. If `PIPELINE_MCP_RESOURCE_URL` points
at the internal service — say `http://pipeline:8000/mcp` while clients use
`https://pipeline.example.com:8443/mcp` — the published identifier does not
match what the client dialed, and the client refuses to start its login flow,
so it keeps getting 401. It is also refused at start-up if it is not absolute
`https://`, so a plaintext value fails loudly rather than silently.

Fetch the metadata and compare it with the URL in your client config:

```sh
curl -sS --cacert ~/.config/pipeline/ca.crt \
  https://pipeline.example.com:8443/.well-known/oauth-protected-resource | head -20
```

Set `PIPELINE_MCP_RESOURCE_URL` to the URL clients actually use, then restart
the pipeline container.

### 401 after a while, with no other change

The token expired. `mcp-token` mints short-lived tokens (§7); an exported
`PIPELINE_MCP_TOKEN` does not renew itself and OpenCode does not refresh an
env token. Mint a fresh one, or use OAuth mode. A 401 that arrives with an
`invalid_token`-style message a fixed interval after login is this, not a policy
problem — check the token's `exp` before suspecting the server.

### Dashboard login says `authentication_unavailable`

The browser reaches `/auth/login` and the dashboard answers
`{"error":"authentication_unavailable"}` with a 503. That is the server failing
its **own** discovery request to the OIDC issuer, not a browser or certificate
problem: the identity provider is pinned to the public origin (`KC_HOSTNAME`),
and the pipeline container cannot resolve that name to the proxy. The log line
is `OIDC login unavailable` with `reason=discovery_failed`.

The standalone compose file already sets `PIPELINE_OIDC_INTERNAL_BASE_URL` to
`https://proxy:8443`, so discovery and the token exchange go to the proxy's
in-network name while the browser and the `iss` claim keep the public origin. If
you run the pieces by hand, set the same variable, and keep
`PIPELINE_OIDC_ALLOW_PRIVATE_JWKS=1` so the fetch's private-address guard accepts
the proxy's in-network address. The public issuer stays the value in
`PIPELINE_OIDC_ISSUER`; do not point that at `proxy`, or the `iss` check and the
browser redirect will both fail.

### A realm edit does not take effect

`start --import-realm` imports `realm.json` **only when the realm does not
already exist**. Its database lives in `keycloak-data`, so after the first start
the realm exists and every later start skips the import.

- First start, empty `keycloak-data`, `start --import-realm` → the realm is
  imported from the JSON.
- Edit the JSON (a new redirect URI, a different lifespan), restart → nothing
  changes; the log says the realm already exists and the import is skipped.
- Only after you delete the realm (admin console → *Realm settings* → delete),
  or start from an empty `keycloak-data` volume, does a later start import the
  new definition.

The mistake is editing the JSON, restarting, and expecting the running realm to
update. It will not. Change the running realm through the admin console, or
delete the realm and let the next start re-import it — and note that deleting
the realm deletes its users and sessions, so re-create the first user (§2 step 6)
afterwards.

### Containers killed, restarts, or nothing responds after a while

A host with less than 6 GiB *allocated to Docker* can OOM-kill Keycloak or the
pipeline containers even though the machine itself has plenty of RAM — the
kernel sees only the VM's memory. Symptoms are containers that exit with status
`137`, repeated restarts, Keycloak dying mid-login, or `docker compose ps`
showing a service cycling.

```sh
docker info --format '{{.MemTotal}}'    # must be >= 6442450944
docker compose -f deploy/standalone/compose.yaml ps
```

Raise the Docker VM's memory allocation (Docker Desktop → Settings → Resources;
a `Resources.Memory` setting under `~/.docker/daemon.json` on Desktop, or the VM
definition on Linux) to at least 6 GiB. Checking the host's total RAM does not
answer this question (§1).

### 421 Invalid Host

The MCP transport answers **421 Invalid Host** when a request's `Host` header is
not in `PIPELINE_MCP_ALLOWED_HOSTS`. This is the SDK's DNS-rebinding protection,
and it fires on the `Host` header the client sent, not on DNS.

Example: `PUBLIC_HOSTNAME=pipeline.example.com`, the certificate and DNS are
correct, the allowed-hosts list contains it — and a request that arrives with
`Host: 127.0.0.1` gets 421. So does one arriving through a load balancer or an
`ssh -L` tunnel that rewrites `Host`, or one sent to an IP literal.

Add every name clients legitimately use, comma-separated, and restart the
pipeline container:

```sh
PIPELINE_MCP_ALLOWED_HOSTS=pipeline.example.com,pipeline.example.com:*,pipeline.internal,pipeline.internal:*
```

Leaving it empty makes the transport fall back to the SDK default instead of
accepting everything, so an empty list is not a workaround. And do not "fix"
this by adding `*`: the point of the check is to make a rebinding attack fail.

## Standalone install smoke test

`deploy/standalone/smoke.sh` grades the whole operator path in a throwaway state
directory: it runs `deploy/install.sh --non-interactive`, then saves, ingests,
approves and dispatches a `repo_url` plan through the MCP transport over HTTPS
and polls the story until it reaches `tests_passed` (review and merge are out of
scope for the smoke). Run it before you trust an install, and run it locally
before you turn on its CI job.

### Prerequisites

- Docker with the Compose **v2** plugin. This is the first thing the script
  checks; without it, it stops with `the Docker Compose v2 plugin is missing`.
- A shell with the tools the script uses on `PATH`: `bash`, `python3`, `openssl`,
  `git` and `curl`. The script's header lists its requirements.
- **Not a host that already runs a standalone stack.** The run publishes host
  port 8443 and uses the compose project `pipeline-standalone` (fixed in the
  compose file), and its teardown runs `docker compose … down -v`, which deletes
  that project's named volumes (Keycloak data, pipeline state). Use a separate
  machine or CI instead.
- Free host ports. The stack publishes exactly one port, from
  `deploy/standalone/compose.yaml`:

  ```yaml
  ports:
    - "${PUBLIC_PORT:-8443}:8443"
  ```

  Caddy listens on that port, and its site address is `{$PUBLIC_HOSTNAME}:8443`
  (`deploy/standalone/Caddyfile`), so every URL the run touches is on that one
  host and port:
  - `https://{$PUBLIC_HOSTNAME}:8443/realms/pipeline` — the Keycloak realm, which
    is also the OIDC issuer the pipeline is configured with
  - `https://{$PUBLIC_HOSTNAME}:8443/mcp` — the MCP transport
  - `https://{$PUBLIC_HOSTNAME}:8443/.well-known/oauth-protected-resource` — the
    RFC 9728 metadata document
  - everything else on the site is the dashboard (`pipeline:8000` inside the
    network), and the `/realms/*` and `/resources/*` paths go to `keycloak:8080`.
    `proxy:8443` is the same site under its in-network name, which the pipeline
    uses to fetch its JWKS.

  The smoke run is fixed to that address: the script assigns
  `PUBLIC_HOSTNAME=localhost` and `PUBLIC_PORT=8443` itself, ignoring any value
  you export, so every run binds `https://localhost:8443`. It verifies Caddy's
  internal CA rather than skipping verification.
- No sudo is required on macOS: Docker Desktop maps the bind mount to the
  container uid, so the pipeline's uid 10001 can read the secret files without a
  `chown`. Do not prefix the command with it on macOS — the script detects
  Darwin and skips the escalation entirely.

### Running it

```sh
bash deploy/standalone/smoke.sh
```

The run tears the stack down when it ends, successfully or not: the `EXIT` trap
runs `docker compose … down -v`, so a failure at any step still leaves a clean
compose project and the next run does not hit "port already allocated".

To keep the stack and the temporary state directory for inspection, set the keep
flag:

```sh
STANDALONE_SMOKE_KEEP=1 bash deploy/standalone/smoke.sh
```

With the flag set, the run leaves both in place and prints the inspect command
with the paths it used:

```sh
docker compose -f <checkout>/deploy/standalone/compose.yaml -f <temp dir>/override.yaml ps
```

Paste the printed line as-is. The first `-f` is the operator's compose file and
the second is the throwaway override that points the stack at the scripted model
backend; the stack is not reachable with the base file alone.

### What a green run prints

The last line is the verdict:

```text
standalone-smoke: PASSED: the standalone install ran a repo_url plan through dispatch and grading (tests_passed); review and merge are out of scope
```

Before it, the run walks through the stages it completed — for example
`seeded the bare repository`, `save_plan accepted the repo_url plan`,
`ingest_plan cloned the file:// repository`, `approve_plan succeeded`,
`oauth-protected-resource metadata is served`, and
`story SMOKE-1 reached <state>`. Any failure prints `FAILED: <step>: <reason>`
and exits non-zero.

### Verification checklist

Tick these off against the log of a run you did not keep, or against the stack
of a `STANDALONE_SMOKE_KEEP=1` run:

- [ ] Containers are up: `docker compose -f deploy/standalone/compose.yaml ps` lists `pipeline`, `keycloak`, `proxy` and `nats` running. Only `proxy` and `pipeline` report health (`proxy` waits for the exported CA root, `pipeline` probes a credential-free dashboard route); `keycloak` and `nats` define no healthcheck and show no health status.
- [ ] The install completed: the log shows `install.sh: starting the stack` and the readiness wait returning, with no `FAILED:` line.
- [ ] The realm was rendered and imported: the log shows `install.sh: rendered` the realm file, then `install.sh: configuring Keycloak (pipeline realm)`.
- [ ] Login is reachable at the Caddy URL: `https://{$PUBLIC_HOSTNAME}:8443/realms/pipeline/.well-known/openid-configuration` answers, and you reached it through Caddy rather than a `curl -k` bypass.
- [ ] Teardown happened: after a run without `STANDALONE_SMOKE_KEEP=1`, `docker compose -f deploy/standalone/compose.yaml ps` shows no containers and the volumes are gone.

### Enabling the CI gate

`.github/workflows/standalone-smoke.yml` runs this same script on a
`pull_request` that touches `deploy/**` or `pipeline/remote_*.py`, and on
`workflow_dispatch`. The job is gated on a repository variable and **skipped by
default** (`if: vars.ENABLE_STANDALONE_SMOKE_CI == 'true'`), so it does not block
a merge until you turn it on.

Turn it on only after one green local run:

1. Open the repository on GitHub and go to **Settings → Secrets and variables →
   Actions → Variables**.
2. Add a repository variable named `ENABLE_STANDALONE_SMOKE_CI` with the value
   `true`. The comparison is an exact string equality: `1`, `TRUE` or `yes`
   leaves the job skipped.
3. Re-run the workflow, or open a pull request that touches `deploy/**`, and
   confirm the job now appears instead of being skipped.

Do not edit the workflow file to change the gate. Set the variable: removing the
`if:` condition would run the job for every operator on every matching pull
request, and `tests/smoke/test_standalone_smoke_contract.py` enforces the gate
and the pinned action SHAs.

## Connecting MCP clients (remote)

The standalone stack is remote-only: the MCP server runs inside the `pipeline`
container and is reachable solely through the Caddy front door. The site address
in `deploy/standalone/Caddyfile` is `{$PUBLIC_HOSTNAME}:8443`, the paths
`/mcp*` and `/.well-known/oauth-protected-resource*` reverse-proxy to the
pipeline's MCP port, and the published port in `deploy/standalone/compose.yaml`
is `"${PUBLIC_PORT:-8443}:8443"`. With the default `PUBLIC_PORT`, that gives:

```
https://{$PUBLIC_HOSTNAME}:8443/mcp
```

Replace `{$PUBLIC_HOSTNAME}` with the `PUBLIC_HOSTNAME` you put in `.env` — the
same token the Caddyfile site address uses. Only if you changed `PUBLIC_PORT`
does the `:8443` change, and `PIPELINE_MCP_RESOURCE_URL` has to change with it
(§4, "The public origin, worked through").

### OpenCode

In `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "pipeline": {
      "type": "remote",
      "url": "https://{$PUBLIC_HOSTNAME}:8443/mcp",
      "oauth": { "clientId": "pipeline-cli" }
    }
  }
}
```

`pipeline-cli` is the public PKCE client defined in
`deploy/standalone/realm.json`: no secret belongs in this file. The key line is
`type: "remote"` (spelled `"type": "remote"` in JSON above) — without it OpenCode
expects a local server to launch. OpenCode completes the browser flow at its own
loopback callback, which the realm already lists as a redirect URI on that
client.

### Claude Code

The certificate comes from Caddy's own CA (mounted into the proxy container at
`/data/caddy/pki/authorities/local/root.crt`), not from a public root. Export the
copy the install made (§5, "Trusting the certificate first") so Node — and
therefore Claude Code — can validate the endpoint:

```sh
export NODE_EXTRA_CA_CERTS=~/.config/pipeline/ca.crt
claude mcp add --transport http pipeline https://{$PUBLIC_HOSTNAME}:8443/mcp
```

`NODE_EXTRA_CA_CERTS` must point at that internal CA from the standalone
deployment; the public trust store will not validate this endpoint, and the
symptom is an `UNABLE_TO_VERIFY_LEAF_SIGNATURE` error rather than a login prompt
(§8, "Untrusted CA"). Add `--client-id pipeline-cli --callback-port 33418` to
start the OAuth browser flow at registration time.

### Removed: local stdio entry

The previous local configuration launched the pipeline server as a child
process — an `mcpServers` entry naming an interpreter and a server script — and
it has been removed, not deprecated. The server is remote-only now: it lives
behind Caddy in a container, holds the state directory and the credentials, and
has no stdio transport listening. A client started from a local script has
nothing to connect to, so configure the remote URL above instead.

### Remote tool policy

A remote call arrives over the network and runs against the server's own state
and checkouts, not the caller's disk. `pipeline/remote_tool_policy.py` encodes
that boundary, and §6 ("Which tools a remote caller may use") lists the whole
surface. The rules that bite first:

- **Plans must pass `repo_url`** — an `https://` URL on a host listed in
  `PIPELINE_REPO_URL_HOSTS`. `repo_root` and `workspace` are refused from a
  remote caller: those name paths on the caller's machine, which the server
  cannot see.
- **Reads are scoped to that repository.** `list_plans`, `verify_audit` and the
  per-plan reads only return plans, runs and audit entries belonging to the
  repositories the caller can see.
- **`decompose_plan` and `record_security_audit` are refused over the remote
  transport.** Both act on the host's own checkout and host-scoped
  configuration, so they stay local-operator tools; a separate follow-up plan
  covers making them remote-capable.
