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

A compose example that wires the same contract up next to a NATS broker and a
Keycloak realm lives alongside this file.

## Required environment

| Variable | Default in the image | Meaning |
| -------- | -------------------- | ------- |
| `PIPELINE_MCP_TRANSPORT` | `streamable-http` | HTTP MCP transport. The entrypoint accepts only this value; the stdio server is the other image. |
| `PIPELINE_IDENTITY_PROVIDER` | `oidc` | Bearer-token verification. `pipeline/remote_transport.py` refuses to open a socket without an OIDC provider, so this must be `oidc`. |
| `PIPELINE_POLICY_ENFORCE` | `1` | Tool-policy enforcement. The transport also refuses to start with this off. |
| `PIPELINE_OIDC_ISSUER` | unset — required | Issuer URL of the identity provider. |
| `PIPELINE_OIDC_AUDIENCE` | unset — required | Expected `aud` claim. |
| `PIPELINE_OIDC_JWKS_URL` | unset — required | JWKS endpoint used to verify signatures. |
| `PIPELINE_OIDC_CLIENT_SECRET_FILE` | `/run/secrets/oidc_client_secret` | Path of the mounted client-secret file. The entrypoint fails fast when it is unreadable. |
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

## Health

The image declares

```
HEALTHCHECK CMD python -c "... http://127.0.0.1:$DASHBOARD_PORT/api/health ..."
```

It probes the dashboard rather than the MCP endpoint on purpose: MCP requires a
bearer token, and a health probe must not need a credential. `/api/health`
reports the resolved `plan_dir` and scheduler liveness, so a wedged scheduler
shows up as an unhealthy container.

## What the image is not

- It is not a sandbox for agent tool calls; worktrees it writes are real git
  worktrees in the mounted volume.
- It does not run the scheduler daemon or the MCP transport over stdio. Those
  are separate processes in the operator's host deployment.
- It carries no secrets. A deployment that cannot mount the OIDC client secret
  should not run this image.
