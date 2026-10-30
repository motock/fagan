# Deployable server image for the pipeline: dashboard + remote MCP transport.
#
# This is NOT the MCP-directory image. The repo-root Dockerfile packages the
# stdio MCP server for directory listings (one process, no HTTP). This one is
# the long-running server an operator deploys:
#
#   * multi-stage, so compilers and pip caches never reach the runtime layer;
#   * an exact python:3.12-slim tag (no `latest`: a rebuild must be
#     reproducible);
#   * git in the runtime stage, because the pipeline shells out to it for
#     worktrees and commits;
#   * a numeric non-root uid (10001) that owns everything it writes;
#   * PLAN_DIR and WORKTREE_ROOT inside one declared VOLUME, so plans and
#     worktrees survive container replacement;
#   * streamable-http MCP behind OIDC, refusing to start otherwise (see
#     pipeline/remote_transport.py);
#   * a HEALTHCHECK on a credential-free dashboard route: the image exports
#     PIPELINE_IDENTITY_PROVIDER=oidc, which puts a bearer-token gate on every
#     /api/ route (app/dashboard.py), so the probe uses "/" instead;
#   * no secrets baked in. Every credential arrives as a mounted file at
#     runtime; nothing secret is COPYed and no build ARG carries a credential,
#     because both leak into image history. The only build args are the three
#     public tooling version pins and TARGETARCH, none of which are secret.
#
# Build:  docker build -f deploy/Dockerfile -t pipeline-server .
# Run:    docker run --rm -p 8000:8000 -p 8080:8080 \
#           -v pipeline-state:/var/lib/pipeline \
#           --env-file .pipeline.env \
#           -v /secure/oidc_client_secret:/run/secrets/oidc_client_secret:ro \
#           pipeline-server
# See deploy/README.md for the full contract.

# --------------------------------------------------------------------------
# Stage 1: build the virtualenv. Only requirements files are needed here, so
# a dependency bump does not invalidate the application layers below.
# --------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY requirements.txt requirements-dashboard.txt ./

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir \
        -r requirements-dashboard.txt -r requirements.txt

# --------------------------------------------------------------------------
# Stage 2: runtime. No pip cache, no compiler toolchain, no root user.
# --------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Pinned tooling versions. The sha256 digests in the install step below are
# bound to these exact versions: whenever any of them changes, every checksum
# must be refreshed from the upstream release page in the same commit, or the
# build fails at `sha256sum -c` (which is the point -- a silent upstream swap is
# the failure mode this pinning exists to prevent).
ARG GH_VERSION=2.102.0
ARG NODE_VERSION=24.22.0
ARG CLAUDE_CODE_VERSION=2.1.301
# docker build supplies this per target platform; the install step maps it onto
# the two different arch spellings upstream releases use.
ARG TARGETARCH

# PYTHONUNBUFFERED/PYTHONDONTWRITEBYTECODE: logs stream to the container log
# driver and the read-only install tree stays clean.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    PLAN_DIR=/var/lib/pipeline/plans \
    WORKTREE_ROOT=/var/lib/pipeline/worktrees \
    AGENTS_DIR=/app/agents \
    GIT_AUTHOR_NAME=pipeline \
    GIT_AUTHOR_EMAIL=pipeline@localhost \
    GIT_COMMITTER_NAME=pipeline \
    GIT_COMMITTER_EMAIL=pipeline@localhost \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8000 \
    DASHBOARD_RELOAD=0 \
    PIPELINE_MCP_TRANSPORT=streamable-http \
    PIPELINE_MCP_HOST=0.0.0.0 \
    PIPELINE_MCP_PORT=8080 \
    PIPELINE_IDENTITY_PROVIDER=oidc \
    PIPELINE_POLICY_ENFORCE=1 \
    PIPELINE_OIDC_CLIENT_SECRET_FILE=/run/secrets/oidc_client_secret

# git is a runtime dependency (worktrees, commits, diffs); ca-certificates is
# needed for the IdP's JWKS fetch; curl and xz-utils fetch and unpack the pinned
# gh, Node and Claude Code tooling below. Lists are dropped in the same layer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl xz-utils \
 && rm -rf /var/lib/apt/lists/*

# Fixed numeric ids so the volume can be pre-created with host-side ownership
# and every rebuild maps to the same uid/gid.
RUN groupadd --gid 10001 pipeline \
 && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /var/lib/pipeline --shell /usr/sbin/nologin pipeline

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Explicit COPY paths rather than `COPY . .`: the build context is the repo
# root, which holds operator-local .env files and API keys that must never be
# baked into a layer. Only runtime code and data are copied.
COPY --chown=10001:10001 requirements.txt requirements-dashboard.txt ./
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 pipeline ./pipeline
COPY --chown=10001:10001 scripts ./scripts
COPY --chown=10001:10001 agents ./agents
COPY --chown=10001:10001 global-rules ./global-rules
COPY --chown=10001:10001 static ./static
COPY --chown=10001:10001 model_registry.json pyproject.toml ./
COPY --chown=10001:10001 deploy/entrypoint.sh ./deploy/entrypoint.sh

RUN chmod 0755 ./deploy/entrypoint.sh \
 && ln -s /opt/venv /app/.venv \
 && chown 10001:10001 /app \
 && install -d -o 10001 -g 10001 -m 0750 \
        /var/lib/pipeline/plans /var/lib/pipeline/worktrees

# Mutable state: plans, worktrees, scheduler health. Mount a named volume or a
# host path here, or every restart starts from an empty plan directory.
VOLUME ["/var/lib/pipeline"]

# --------------------------------------------------------------------------
# Pinned operator tooling: gh (pull requests, merges) and Node + the Claude Code
# CLI (the Claude backend). Both come from upstream release tarballs whose
# digests are checked here, so a corrupted or silently re-tagged download fails
# the build instead of landing in a layer. No compiler toolchain is involved and
# nothing is fetched from a mutable tag. Runs as root, so it must stay above the
# USER line below.
#
# TARGETARCH is supplied by BuildKit for every build; when it is absent (a
# legacy builder), fall back to the host machine's arch and map that the same
# way. Anything that is neither amd64 nor arm64 fails here, before a single byte
# is downloaded.
#
# The npm install below spells the Claude Code version out literally (an
# npm range such as a caret or tilde spec would let a rebuild pick up unreviewed
# code); keep it identical to CLAUDE_CODE_VERSION above when bumping.
# --------------------------------------------------------------------------
RUN set -eux; \
    case "${TARGETARCH:-$(uname -m)}" in \
        amd64|x86_64) \
            GH_ARCH=linux_amd64; \
            NODE_ARCH=x64; \
            GH_SHA256=bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386; \
            NODE_SHA256=cc2f601cee82d9a5a233302fe307abc600727035fd7d2c3a3571755989e95008; \
            ;; \
        arm64|aarch64) \
            GH_ARCH=linux_arm64; \
            NODE_ARCH=arm64; \
            GH_SHA256=7862c86c72f43df3a2d93ddde6f473285b4e2af61b494849846827e513ef6484; \
            NODE_SHA256=fef00bb4d36e1c50104a9775a10359beee98f162b4104dd12472c75065c6a143; \
            ;; \
        *) \
            echo "unsupported TARGETARCH '${TARGETARCH:-<unset>}': this image ships amd64 or arm64 tooling only" >&2; \
            exit 1; \
            ;; \
    esac; \
    curl -fsSL -o gh.tar.gz \
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${GH_ARCH}.tar.gz"; \
    echo "${GH_SHA256}  gh.tar.gz" | sha256sum -c -; \
    tar -xzf gh.tar.gz; \
    install -m 0755 "gh_${GH_VERSION}_linux_${GH_ARCH}/bin/gh" /usr/local/bin/gh; \
    curl -fsSL -o node.tar.xz \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"; \
    echo "${NODE_SHA256}  node.tar.xz" | sha256sum -c -; \
    mkdir -p /usr/local/lib/nodejs; \
    tar -xJf node.tar.xz -C /usr/local/lib/nodejs --strip-components=1; \
    for binary in node npm npx; do \
        ln -sf "/usr/local/lib/nodejs/bin/${binary}" "/usr/local/bin/${binary}"; \
    done; \
    npm install -g --prefix /usr/local --omit=dev "@anthropic-ai/claude-code@2.1.301"; \
    npm cache clean --force; \
    test -x /usr/local/bin/gh; \
    test -x /usr/local/bin/node; \
    test -x /usr/local/bin/npm; \
    test -x /usr/local/bin/npx; \
    test -x /usr/local/bin/claude; \
    rm -rf gh.tar.gz node.tar.xz "gh_${GH_VERSION}_linux_${GH_ARCH}" /root/.npm; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

USER 10001:10001

EXPOSE 8000 8080

# Probe the dashboard's index route, not the MCP endpoint and not an /api/
# route: this image exports PIPELINE_IDENTITY_PROVIDER=oidc, and
# app/dashboard.py's middleware then requires a bearer token on *every*
# /api/ route -- /api/health included -- so an anonymous probe there can only
# ever get a 401. "/" is outside that prefix, so it is reachable without a
# credential, and rendering it still reads the API-key store and the plan
# directory, which is where a wedged or mis-owned container fails first.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; port=os.environ.get('DASHBOARD_PORT','8000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/' % port, timeout=4).status==200 else 1)"

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
