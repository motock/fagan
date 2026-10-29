# Deploying the pipeline server image

`deploy/Dockerfile` builds the **long-running server** image: the read-only
dashboard and the remote MCP transport in one container, running as a non-root
user, with all mutable state on a volume.

It is a different artifact from the repo-root `Dockerfile`, which packages the
stdio MCP server for MCP-directory listings (one process, no HTTP, no
dashboard). Change that one only for directory packaging; change this one for
deployments.

Everything below is the contract the image enforces at start-up — the
entrypoint refuses to run when a precondition is unmet rather than serving a
half-configured process.

## Build

The build context is the repo root (the Dockerfile copies `app/`, `pipeline/`,
`scripts/`, `agents/`, `global-rules/` and `static/` from there):

```sh
docker build -f deploy/Dockerfile -t pipeline-server .
```

The build is two-stage: dependencies install into `/opt/venv` in a builder
stage and only the finished virtualenv is copied into the runtime stage, so no
compiler toolchain or pip cache reaches the published image. Both stages pin
the exact tag `python:3.12-slim` — never `latest`, so a rebuild of the same
commit produces the same base.

No secret is baked in. There is no `ARG`, no `.env` is copied, and the build
context is never copied wholesale; a `COPY . .` from the repo root would sweep
operator-local `.env` files and API keys into a layer, where they remain
readable to anyone who can pull the image.

## Run

```sh
docker run --rm \
  -p 8000:8000 -p 8080:8080 \
  -v pipeline-state:/var/lib/pipeline \
  --env-file .pipeline.env \
  -v /secure/oidc_client_secret:/run/secrets/oidc_client_secret:ro \
  pipeline-server
```

Published ports:

| Port | Service | Override |
| ---- | ------- | -------- |
| 8000 | dashboard (uvicorn, `app.dashboard:app`) | `DASHBOARD_PORT` |
| 8080 | MCP remote transport (`streamable-http`) | `PIPELINE_MCP_PORT` |

Both surfaces are authenticated, in different ways:

- **`:8080` (MCP)** verifies an OIDC bearer token against the configured
  issuer's JWKS. `pipeline/remote_transport.py` refuses to start without an
  identity provider.
- **`:8000` (dashboard)** is gated by `PIPELINE_IDENTITY_PROVIDER=oidc` as
  well: `app/dashboard.py`'s middleware then requires an OIDC bearer token on
  **every** `/api/*` route, `/api/health` included. The browser page at `/`
  requires no token, but it is not an anonymous surface either — it is
  protected by the `Host` check in `app/auth.py` (`host_is_allowed`): any
  `Host` that is not the bound address or a loopback alias gets a 403.

A reverse proxy therefore has to (a) obtain a bearer token for the `/api/*`
calls it forwards, and (b) forward the bound address as the upstream `Host`
(nginx: `proxy_set_header Host 127.0.0.1:8000;`). A proxy that passes the
public hostname through with the default `$host` form gets a 403.

## Required environment

| Variable | Default in the image | Meaning |
| -------- | -------------------- | ------- |
| `PIPELINE_MCP_TRANSPORT` | `streamable-http` | HTTP MCP transport. The entrypoint accepts only this value; the stdio server is the other image. |
| `PIPELINE_IDENTITY_PROVIDER` | `oidc` | Bearer-token verification. `pipeline/remote_transport.py` refuses to open a socket without an OIDC provider, so this must be `oidc`. |
| `PIPELINE_POLICY_ENFORCE` | `1` | Tool-policy enforcement. The transport also refuses to start with this off. |
| `PIPELINE_OIDC_ISSUER` | unset — required | Issuer URL of the identity provider. |
| `PIPELINE_OIDC_AUDIENCE` | unset — required | Expected `aud` claim. |
| `PIPELINE_OIDC_JWKS_URL` | unset — required | JWKS endpoint used to verify signatures. |
| `PIPELINE_OIDC_CLIENT_SECRET_FILE` | `/run/secrets/oidc_client_secret` | Path of the mounted client-secret file. The entrypoint fails fast when it is unreadable. Bearer-token verification itself is JWKS-only; this file is a deployment credential, not an input to signature checking. |
| `PIPELINE_MCP_ALLOWED_HOSTS` | unset | Comma-separated allowlist enabling DNS-rebinding protection for a proxied deployment. Set it whenever the container sits behind a reverse proxy. |
| `PLAN_DIR` | `/var/lib/pipeline/plans` | Where plans and decision files live. |
| `WORKTREE_ROOT` | `/var/lib/pipeline/worktrees` | Where per-plan git worktrees live. |

The client secret is a **mounted file**, never an environment value:
environment variables are visible to `docker inspect` and to every process in
the container, while a file mount can be rotated and permission-restricted
independently of the image. `.pipeline.env` in the repo root is the operator
reference for the remaining (non-secret) settings.

## Volumes

`/var/lib/pipeline` is declared as a `VOLUME` and owned by uid 10001. It holds:

- `plans/` — the plan directory (`PLAN_DIR`), including `.dashboard_ui_state.json`
  and `.scheduler_health.json` that `/api/health` reports on;
- `worktrees/` — the git worktree root (`WORKTREE_ROOT`).

Mount a named volume (as above) or a host path. Without one, plans and
worktrees are lost when the container is replaced, and the entrypoint's
writable-directory check fails if the mount is read-only or owned by another
uid.

The image runs as uid/gid `10001` (`pipeline`) with no home directory. When
binding a host path instead of a named volume, `chown 10001:10001` it first.

`/app` (the working directory) is owned by uid 10001 as well. The dashboard
creates its shared-secret file at `.dashboard_api_key` inside the repo root on
first use, so a root-owned `/app` would make the index route return 500. That
file is part of the container's writable layer, not of the volume: it is
regenerated when the container is replaced.

## Health

The image declares

```
HEALTHCHECK CMD python -c "... http://127.0.0.1:$DASHBOARD_PORT/ ..."
```

It probes the dashboard's index route rather than the MCP endpoint or an
`/api/` route, because neither of those is reachable without a credential:
MCP verifies a bearer token, and `PIPELINE_IDENTITY_PROVIDER=oidc` puts the
same requirement on every `/api/*` route, `/api/health` included. `/` sits
outside that prefix, so the probe needs nothing but the loopback `Host` the
gate already allows — and rendering it reads the plan directory and the
dashboard's API-key store, so a container that cannot write its own state (or
whose dashboard died) turns unhealthy instead of staying green.

The probe is deliberately shallow: it starts the container healthy only after
`--start-period`, and reports `unhealthy` after three consecutive failures
~30 s apart.

## What the image is not

- It is not a sandbox for agent tool calls; worktrees it writes are real git
  worktrees in the mounted volume.
- It does not serve MCP over stdio. `ENTRYPOINT` starts the streamable-http
  transport (the exec'd foreground process), the dashboard, and the scheduler
  daemon; set `PIPELINE_SCHEDULER_DAEMON=0` to run the daemon outside the
  container.
- It carries no secrets. The OIDC client secret is a mounted file, and
  bearer-token verification is JWKS-only; the image only asserts that the
  mount exists so a missing credential fails at start rather than at the first
  token exchange.

## The smoke stack and the merge park

`deploy/compose.yaml` brings up a complete, disposable stack: this image, a
Keycloak IdP in dev mode with `deploy/keycloak/realm.json` imported, a Caddy
proxy terminating TLS with its internal CA in front of the MCP port (the
transport has no TLS of its own), a NATS JetStream broker the scheduler daemon
consumes from, and the scripted model server from
`tests/smoke/scripted_model_server.py`. `deploy/smoke/smoke.sh` then drives a
real run against it: a client-credentials token, a plan ingested and approved by
`owner`, one story dispatched and reviewed, a stranger refused, the audit chains
verified, exactly one notification across the broker, and the same assertion
surviving a `docker compose restart pipeline`.

**`PIPELINE_FORGE` is deliberately unset in that stack, and the smoke run stops
at "reviewed, merge parked".** The merge loop needs a code host:
`pipeline/forge.py` supports GitHub and GitLab, and an explicit `PIPELINE_FORGE`
selects one of them. A stack with no forge, no `gh`/`glab` CLI and no API token
therefore cannot merge, and this stack does not pretend to: the story reaches
`reviewed`, the merge stays parked, and `deploy/smoke/smoke.sh` asserts that
parked state rather than a merge. To exercise the merge loop end to end, point
the stack at a real (or emulated) GitHub or GitLab endpoint by setting
`PIPELINE_FORGE` and the matching token, and expect the run to merge instead of
parking.

