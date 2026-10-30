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
| `AGENTS_DIR` | `/app/agents` | Where the persona `.md` files live (`COPY ... agents ./agents` under `WORKDIR /app`). Leaving it unset makes `pipeline/paths.py` fall back to `~/.claude/agents`, which does not exist in this image. |
| `GIT_AUTHOR_NAME` / `GIT_COMMITTER_NAME` | `pipeline` | Author and committer name for the commits the server makes. |
| `GIT_AUTHOR_EMAIL` / `GIT_COMMITTER_EMAIL` | `pipeline@localhost` | Author and committer email for the commits the server makes. |
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
| `PIPELINE_SCHEDULER_DAEMON` | `1` | Whether the entrypoint starts `python -m pipeline.scheduler_daemon` and supervises it alongside the dashboard and the MCP transport. Any value other than `0` or `1` is a start failure. Set `0` to run exactly one daemon outside the container instead (one daemon per `PLAN_DIR`). |

The client secret is a **mounted file**, never an environment value:
environment variables are visible to `docker inspect` and to every process in
the container, while a file mount can be rotated and permission-restricted
independently of the image. `.pipeline.env` in the repo root is the operator
reference for the remaining (non-secret) settings.

Commits the server makes (the WIP checkpoint commit, plan bookkeeping) carry
the `GIT_AUTHOR_*` / `GIT_COMMITTER_*` identity above; an operator deploying on
behalf of a real team should override all four with the same variable names at
`docker run -e` / compose `environment:`.

### Credentials

The entrypoint reads the operator's credentials from mounted files and exports
them under the names the backends read. The loader is
[`deploy/lib/operator_env.sh`](lib/operator_env.sh), which
[`deploy/entrypoint.sh`](entrypoint.sh) sources before it starts anything; the
split exists so the loader is testable without any "skip the process start"
switch in the script that runs in production (a switch is fail-open under
`dash`, where an executed script that returns early exits `0`). Both start-up
scripts ship **root-owned and non-writable** in the image
(`deploy/entrypoint.sh` `0555`, `deploy/lib/operator_env.sh` `0444`): the runtime
uid is the uid agent children run as, and a code-executing agent that could
rewrite either file would get its own code re-run at the next container start,
with every mounted secret already exported into that process. Hardening only the
sourced library would leave the *caller* mutable on the same trust boundary.
Never pass any of these as an environment value: an environment value shows up
in `docker inspect` and in `/proc/<pid>/environ` of every child, including the
agents.

| Secret file (default) | Path variable | Exported as |
| --------------------- | ------------- | ----------- |
| `/run/secrets/anthropic_api_key` | `ANTHROPIC_API_KEY_FILE` | `PIPELINE_ANTHROPIC_API_KEY` |
| `/run/secrets/claude_code_oauth_token` | `CLAUDE_CODE_OAUTH_TOKEN_FILE` | `CLAUDE_CODE_OAUTH_TOKEN` |
| `/run/secrets/gh_token` | `GH_TOKEN_FILE` | `GH_TOKEN` |
| `/run/secrets/ollama_api_key` | `OLLAMA_API_KEY_FILE` | `OLLAMA_API_KEY` |

- Each file must be a **regular file, mode `0600`, owned by the uid the
  container runs as**. The entrypoint refuses to start when the mode has any
  group or other bit (`stat -c %a` & `077` non-zero) or when the owner differs
  from `id -u`; the refusal names the variable and never the value. It also
  refuses a path that is not a regular file, and a **symlinked** one: `stat`
  follows links, so a link would let one `*_FILE` variable re-point at another
  credential's `0600` file and have it loaded under the wrong name.
- A **missing or empty** file is not an error. `compose.yaml`'s `secrets:`
  requires every listed file to exist, so an unused credential is created
  empty; the backend that needs it fails closed when it is used. A file that
  holds only whitespace is treated as empty, and in that case the variable is
  **unset**: an ambient `GH_TOKEN`/`OLLAMA_API_KEY`/... in the container
  environment is discarded, never trusted, so a credential never comes from an
  environment value.
- The value is read from the file, a trailing newline stripped, and exported
  for the process tree. Only `loaded <VARIABLE> from secret file` is logged —
  never the value.
- **Exposure boundary (tracked risk).** Everything exported here is inherited by
  every child of the entrypoint. Whichever consumer strips a carrier is named
  here, per credential, because this section is the map an operator reads before
  mounting anything. What removes it before a child starts, today:
  - `GH_TOKEN` (and `GITHUB_TOKEN`, `GITLAB_TOKEN`, `GLAB_TOKEN`,
    `CI_JOB_TOKEN` from the same forge family): stripped from every agent child
    by `pipeline/execution.py::_AGENT_SECRET_ENV` - the consumer for the agent
    child (`spawn_local`, `spawn_harness`, and the docker sandbox's allowlist
    downstream).
  - Nothing else. `PIPELINE_ANTHROPIC_API_KEY`, `OLLAMA_API_KEY` and
    `CLAUDE_CODE_OAUTH_TOKEN` are **readable by agent shells**: an agent is
    prompt-injectable code running as the same uid, and a bare `env` prints
    them. `CLAUDE_CODE_OAUTH_TOKEN` is additionally the token the agent process
    authenticates with, so it cannot be stripped from that child.
  - The `claude` CLI child also inherits `GH_TOKEN`:
    `app.backend_claude.py::_first_party_claude_env()` drops only the
    provider-redirect variables (`ANTHROPIC_*`, `CLAUDE_CODE_USE_BEDROCK`,
    `CLAUDE_CODE_USE_VERTEX`), and its Bash tool executes commands driven by
    model output. `PIPELINE_ANTHROPIC_API_KEY` is *not* mapped to
    `ANTHROPIC_API_KEY` there, so the mounted model key does not authenticate
    the CLI - it is only inherited, unread by the CLI.
  This is recorded as a risk with mitigations rather than claimed as solved:
  scope every credential to exactly what the pipeline needs, budget-cap the
  model account, and mount nothing an agent must not see. Do not deploy a forge
  token with broader rights than clone/push/PR needs. The two strips that would
  remove the remaining carriers are **not** in this story's scope: SA-09 removes
  the model-key carrier from the `claude` CLI child
  (`app/backend_claude.py::_first_party_claude_env()`) and SF-03 adds the model
  keys to the agent denylist (`pipeline/execution.py::_AGENT_SECRET_ENV`). Until
  they land, this table plus the risk note is the contract: the deploy layer
  mounts nothing it does not also name here.
  Until a credential broker lands (the agent talks to a local proxy, or receives
  a per-agent, per-run scoped key) run the server only for operators you would
  let read the mounted files, and **budget-cap every operator model key and the
  Claude account** - the cap is the only control that bounds the blast radius.
- The exported name is `PIPELINE_ANTHROPIC_API_KEY`, not `ANTHROPIC_API_KEY`:
  `app/backend_claude.py` strips an ambient `ANTHROPIC_API_KEY` so a redirected
  provider cannot be smuggled in through the environment. The story that passes
  an operator-supplied key to the `claude` CLI is expected to read this explicit
  variable; nothing in this image maps it to `ANTHROPIC_API_KEY` today, so the
  mounted key does not yet authenticate the CLI.
- When `GH_TOKEN` is set (from the file only) and `PIPELINE_FORGE` is unset, the
  entrypoint exports `PIPELINE_FORGE=github`. An explicit `PIPELINE_FORGE=gitlab`
  is kept.
- When `PIPELINE_GIT_TOKEN_FILE` is unset and `GH_TOKEN` was loaded from a secret
  file, it is exported as the resolved `GH_TOKEN_FILE` path (default
  `/run/secrets/gh_token`), so overriding `GH_TOKEN_FILE` moves both. The
  repo-clone story (SA-05, not implemented yet) is expected to read that *path*,
  not the value, so the token itself never has to be placed in `git`'s
  environment. Until it lands, nothing in the image reads
  `PIPELINE_GIT_TOKEN_FILE`.
- `PIPELINE_GIT_TOKEN_FILE` itself is **accepted verbatim from the ambient
  environment** — the "a credential never comes from an environment value"
  invariant above covers the four secret files, not this path. That is
  defensible because `pipeline/repo_clone.py::_token_file` re-validates it with
  `lstat` before use: regular file, no group/other bits, owner == euid, and
  non-empty. But note the consequence: an ambient `PIPELINE_GIT_TOKEN_FILE` can
  point the askpass helper at any `0600` file the uid owns — including the OIDC
  client secret — and have its contents sent to the forge. Do not let an
  untrusted party set it.

Repo defaults the entrypoint sets (both exported, both overridable):

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `PIPELINE_REPOS_ROOT` | `/var/lib/pipeline/repos` | The directory SA-05 (the repo-clone story, not implemented yet) will clone into, laid out as `<host>/<owner>/<repo>`. Created at start with mode `0750` on the mounted volume; a pre-existing root is `chmod 750`-ed, and the entrypoint refuses to start if group write or any other-user access remains (it is the clone trust boundary). A root **you set** that cannot be created or tightened is fatal, so `PIPELINE_ALLOWED_REPO_ROOTS` never points at a path that does not exist; the image default stays best effort, which is not a fail-open because `pipeline/repo_clone.py::_make_dirs()` creates the root and its parents mode `0750` at clone time, under a path the allowlist already admitted. The volume must therefore be writable by the container uid (see [Volumes](#volumes)). A **symlinked** root is refused rather than followed, and a root that is itself a **mount point** is refused when its mode is too loose instead of being `chmod`-ed — for a bind mount that would mutate the host directory the operator owns, so the operator sets the mode on the volume. |
| `PIPELINE_ALLOWED_REPO_ROOTS` | `$PIPELINE_REPOS_ROOT` | The `pipeline/workspace.py` allowlist a plan's `repo_root` must live under. Defaulting it to the repos root means the dashboard's decompose route and the scheduler are confined to the directory the server owns; a preset value is kept as-is. |

## Volumes

`/var/lib/pipeline` is declared as a `VOLUME` and owned by uid 10001. It holds:

- `plans/` — the plan directory (`PLAN_DIR`), including `.dashboard_ui_state.json`
  and `.scheduler_health.json` that `/api/health` reports on;
- `worktrees/` — the git worktree root (`WORKTREE_ROOT`);
- `repos/` — `PIPELINE_REPOS_ROOT`, created by the entrypoint on first start
  with mode `0750`: the directory SA-05 clones into and the
  `PIPELINE_ALLOWED_REPO_ROOTS` default. An operator-supplied root that cannot
  be created or tightened is fatal (`pipeline/repo_clone.py::_make_dirs()`
  creates the image default at clone time, mode `0750`), so the volume must be
  writable by uid 10001 and a bind-mounted directory must already carry mode
  `0750`.

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

`/app/.venv` is a symlink to `/opt/venv`, not a second virtualenv: the local
dispatch harness launches `<repo>/.venv/bin/python3`, which inside this image
is `/app/.venv/bin/python3`, while the virtualenv built by the image lives at
`/opt/venv`.

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
- It carries no secrets. The OIDC client secret and the operator credentials
  (see [Credentials](#credentials)) are mounted files, not environment values,
  and bearer-token verification is JWKS-only; the image only asserts that the
  OIDC mount exists so a missing credential fails at start rather than at the
  first token exchange.

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

### Bringing the stack up and asserting against it

You need a docker daemon. Do this in a throwaway checkout or worktree, because
step 2 rewrites `deploy/keycloak/realm.json` in place:

```sh
export PIPELINE_MCP_CLIENT_SECRET=... PIPELINE_DASHBOARD_CLIENT_SECRET=... \
       PIPELINE_SMOKE_OWNER_PASSWORD=... PIPELINE_SMOKE_STRANGER_PASSWORD=... \
       PIPELINE_SMOKE_USER_CLIENT_SECRET=... KEYCLOAK_ADMIN_PASSWORD=...
# 1. Build the image under the ref compose.yaml names.
docker build -f deploy/Dockerfile -t pipeline-smoke:pinned .
# 2. Substitute the realm's ${env.X} placeholders, which Keycloak's
#    --import-realm stores literally (the CI step "Render the realm's env
#    placeholders" in .github/workflows/smoke.yml; run that script here).
# 3. Bring the stack up, wait for the realm, assert, tear down.
docker compose -f deploy/compose.yaml up -d --wait
bash deploy/smoke/smoke.sh
docker compose -f deploy/compose.yaml down -v
```

Without step 2, every token request fails with `invalid_client_credentials`.

Those variables are throwaway values for a disposable stack: they belong to one
run, are read from the environment (`compose.yaml` refuses to start without
them), and never appear in a file. `deploy/smoke/smoke.sh` exits non-zero naming
the step that failed, so a red run says which invariant broke: `token`,
`list_plans`, `ingest`, `approve`, `dispatch`, `poll-reviewed`,
`stranger-denied`, `stranger-no-plans`, `verify_audit`, `auth-fail-429`,
`broker-once` or `restart-no-loss`.

### The smoke stack in CI

`.github/workflows/smoke.yml` runs the same stack on every pull request and on
every push to `master`: it builds `deploy/Dockerfile` with buildx, brings the
compose file up with `docker compose up -d --wait`, runs
`deploy/smoke/smoke.sh`, and uploads the compose logs as the `compose-logs`
artifact. The logs are collected before teardown, so a failing run still has
them; a job-level timeout bounds a build or container start wait that never
settles. The
workflow is the only place the image is actually built and the stack actually
started — the local suite reads the artifacts as text because it has no docker
daemon.

The stack job is **advisory for now**. It is gated on the repository variable
`ENABLE_SMOKE_CI` and off by default, because the smoke run has not yet passed,
and a red check on every pull request would block every merge. The stack itself
boots: all five containers come up healthy on a Linux engine. The run fails
after `dispatch`; the open defects are listed in
[`docs/plans/ENTERPRISE_REMAINING_WORK_PLAN.md`](../docs/plans/ENTERPRISE_REMAINING_WORK_PLAN.md#outcome-verified-2026-10-29).
Set the variable to `true` once a run is green. On a push to `master` a
second job publishes the image to `ghcr.io/<owner>/fagan`, tagged with the
commit sha and `master`, with SBOM and provenance attestations; `packages:
write` is granted to that job alone, that job is not gated, and every action is
pinned to a full commit SHA.

### How the pipeline container is wired

The stack-specific startup lives in the `pipeline` service's `entrypoint:` in
`deploy/compose.yaml`, which runs before, and then `exec`s, the image's own
`deploy/entrypoint.sh`:

1. it materialises the OIDC client-secret file that the image entrypoint refuses
   to start without (verification itself is JWKS-only), mode 600, from the client
   secret the IdP was given;
2. it writes a model registry that maps every model role onto the scripted
   server, because selecting a model is a registry fact the pipeline validates,
   not an environment-only switch;
3. it `exec`s the image entrypoint, so signals and exit status pass through
   unchanged; the image entrypoint in turn starts and supervises the scheduler
   daemon, the only durable event-bus consumer (ADR 0010), alongside the
   dashboard and the MCP transport.

The git origin is the one piece of state that must survive a restart, and its
bootstrap deliberately lives in `deploy/smoke/smoke.sh` (`origin-seed`) rather
than in the shim: Docker hands a fresh named volume to the container root-owned,
so the image's non-root user cannot write the first object into it. The step runs
once, guarded on `/git-origin/sample.git/HEAD`, and hands the volume back to the
service uid; a restart therefore keeps the refs a story pushed, and the smoke
run's `restart-no-loss` assertion measures the broker rather than a reseeded
repository.

`tests/smoke/scripted_model_server.py` is the deterministic model the `model`
service runs. It speaks the provider wire protocol and consumes its scripted
replies in order; the smoke run never relies on a reply being replayed.

`docker compose up` is not part of `pytest`: the unit-test container has the
docker client but no daemon, so the stack is asserted statically by
`tests/smoke/test_deploy_contract.py` and exercised for real by the commands
above and by `.github/workflows/smoke.yml`.

