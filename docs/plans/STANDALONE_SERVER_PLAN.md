# Standalone server: install with compose, connect a local agent client over HTTP

- **Status:** preflighted and ingested 2026-10-30 as plan `standalone-server`
  (19 stories, SA-00..SA-19 with no SA-06), on `master` `5c8f8ac` after
  `smoke-stack-green` merged. The plan file is
  [`STANDALONE_SERVER_PLAN.json`](STANDALONE_SERVER_PLAN.json); the briefs
  there are authoritative where they differ from the drafts below (see
  "Changes at preflight").
- **Goal:** someone runs one installer, gets a running server, and points
  Claude Code, OpenCode or another MCP client at its HTTP endpoint. They then
  use it instead of a local stdio pipeline.
- **Dispatch tier:** cloud open-source, Claude-class sizing. The live registry
  routes `roles.dispatch` to `ollama / deepseek-v4.1-flash`. Stories that edit
  `REFERENCE.md` (over 1000 lines) or need real investigation pin
  `backend: claude`.

## Decisions (2026-10-29, from the operator)

1. **Standalone install, server clones from a URL.** A plan names a git URL,
   and the server clones it into its own volume. No host directory is mounted.
2. **Client auth: both modes.** OAuth discovery, so a client's built-in login
   flow works, and a long-lived-credential mode for clients or scripts that
   cannot run a browser flow.
3. **Claude auth: both.** Either an API key or a subscription token
   (`claude setup-token`, read from `CLAUDE_CODE_OAUTH_TOKEN`). The operator
   should confirm that Anthropic's terms allow unattended use of a subscription
   token on a shared server.
4. **Remote tools: all of them.** The 13 tools that are local-only today become
   remotely callable, each behind the policy.
5. **Install path: an interactive script, plus a written guide.** The script
   is primary. The guide is the reference it mirrors, for operators who cannot
   run the script and for debugging.

## What exists versus what the goal needs (verified 2026-10-29)

| Need | Today | Gap |
|---|---|---|
| Processes | One image runs the MCP transport (:8080) and the dashboard (:8000); the compose shim starts the scheduler | SG-05 moves the scheduler into the image entrypoint |
| Dashboard reachable | Starts and is healthy, but not published. Caddy routes only `/realms/*` and the MCP endpoint, and the dashboard's Host check (`app/auth.py` `host_is_allowed`) cannot be configured | SA-11, SA-12 |
| Model credentials | No `claude` CLI and no Node in the image. `app/backend_claude.py` strips `ANTHROPIC_API_KEY` from every `claude` call. Ollama has an endpoint variable but no API-key support | SA-07..SA-10 |
| Forge | No `gh` in the image and `PIPELINE_FORGE` unset, so no PRs and no merges | SA-07, SA-08 |
| Repos | `repo_root` is a server path, with no clone from a URL. The smoke run builds its repo inside the container | SA-04, SA-05 |
| Getting a plan in | `save_plan` is not remote. `ingest_plan` reads a file already on the server; the smoke run uses `docker cp` | SA-02, SA-03 |
| Remote tools | 17 of 30. Not exposed: `save_plan`, `decompose_plan`, `review_story`, `advance_pipeline`, `advance_all_plans`, `request_decision`, `set_story_status`, `mark_story_in_progress`, `patch_plan`, `checkpoint`, `checkpoint_story`, `get_effective_config`, `record_security_audit` | SA-01, SA-02 |
| Client login | Bearer verification only. No RFC 9728 metadata, so a client cannot discover the IdP. Realm clients are machine-to-machine (plus a smoke-only password client), and tokens last 300 s | SA-13..SA-15 |
| Install | Six exported secrets, a hand-run realm render, and the image build | SA-16, SA-17 |

## Stories (draft; files to be confirmed in preflight)

### Epic 1: remote tool surface (security, `risk: high`)

- **SA-01: Gate the three tools that authorize nothing.** `decompose_plan` asks
  for `plan:ingest`. `get_effective_config`, `advance_all_plans` and
  `record_security_audit` are host-scoped and ask for `config:write`, the
  existing host-admin action, so no new `Action` is needed. A remote
  `advance_all_plans` advances only the plans the caller may see. Local stdio
  behaviour is unchanged. Files: `pipeline/service.py` has 44 lines of headroom;
  move the bodies into a `service_*_ops` module if they do not fit.
  `pipeline/authorization.py`.
- **SA-02: Put all 13 tools on the remote allowlist.**
  `pipeline/remote_tool_policy.py`: tools that authorize themselves go into
  `REMOTE_ENFORCED`. Plan-scoped reads (`get_effective_config(plan_name)`) get
  the same hidden-refusal treatment as the other plan-scoped reads. First check
  that `checkpoint_story` (`pipeline/server.py`) authorizes; gate it if not. Also
  update the remote-tools section of `REFERENCE.md` (`backend: claude`, since
  the file is over 1000 lines).
- **SA-03: A remote plan may not name a server path.** A plan saved or ingested
  by a remote principal that sets `repo_root` is refused. It must use
  `repo_url` (SA-05). This is deny by default: without it, a remote caller
  could point a plan at any directory the server can read.
  `PIPELINE_ALLOWED_REPO_ROOTS` (`pipeline/workspace.py`) stays as the second
  layer.

### Epic 2: repositories from a URL

- **SA-04: `pipeline/repo_clone.py` (new).** It maps `repo_url` to
  `PIPELINE_REPOS_ROOT/<host>/<owner>/<repo>` and clones the repo, or fetches
  it if the clone exists.
  - URLs are validated: `https` only, the host must be on
    `PIPELINE_REPO_URL_HOSTS` (default `github.com`), and there are no
    credentials in the URL and no `..` segments.
  - The token comes from `PIPELINE_GIT_TOKEN_FILE` through a `GIT_ASKPASS`
    helper, never on the command line or in `.git/config`.
  - Tests use a local bare repo, enabled through a test-only allowance for
    `file://` that is off by default.
- **SA-05: Wire `repo_url` into `save_plan` and `ingest_plan`.** A plan names
  `repo_url`, and the server sets `repo_root` to the managed clone. Naming both
  is refused. The image defaults `PIPELINE_ALLOWED_REPO_ROOTS` to
  `PIPELINE_REPOS_ROOT`. `pipeline/ingest.py` is at 998 lines, so the wiring
  goes in the service path or a new module. Verify that dispatch already
  fetches the base branch from origin (`123ebbe`); if not, fetch before
  creating the worktree.

### Epic 3: image tooling and credentials

- **SA-07: Install `gh` and the `claude` CLI in the image.**
  - `gh` comes from a pinned release tarball with its checksum.
  - Node LTS and `@anthropic-ai/claude-code` are installed at an exact pinned
    version.
  - The image stays non-root, and the runtime stage gets no compiler toolchain.
- **SA-08: Credentials as mounted secret files.** `deploy/entrypoint.sh` reads
  `ANTHROPIC_API_KEY_FILE`, `CLAUDE_CODE_OAUTH_TOKEN_FILE`, `GH_TOKEN_FILE` and
  `OLLAMA_API_KEY_FILE` (default `/run/secrets/*`). It refuses files looser
  than 0600 or not owned by the service uid, exports the values only to the
  processes that need them, and never echoes them. A missing file is not an
  error; the backend that needs it fails closed when used.
- **SA-09: Allow the API key without allowing provider redirects.**
  `app/backend_claude.py` keeps stripping `ANTHROPIC_BASE_URL`, model aliases
  and the Bedrock/Vertex switches. It passes `ANTHROPIC_API_KEY` through only
  when the operator supplied it through SA-08's secret file. A test asserts
  that the redirect variables are still stripped when the key is passed.
- **SA-10: Ollama API key.** When `OLLAMA_API_KEY` is set, the Ollama provider,
  and the local agent's own streaming path, send
  `Authorization: Bearer <key>`. This is for `https://ollama.com` used directly.
  A local `http://host.docker.internal:11434` endpoint needs no key.

### Epic 4: the standalone stack

- **SA-11: `deploy/standalone/compose.yaml` (new; the smoke file stays the
  smoke file).**
  - **Pipeline:** the published GHCR image, overridable by tag.
  - **Keycloak:** in `start` mode, with a persistent volume and a configurable
    hostname.
  - **Caddy:** its internal CA by default, or ACME when `PUBLIC_HOSTNAME` is a
    real DNS name. Routes: `/mcp` and `/.well-known/oauth-protected-resource`
    go to the pipeline on :8080, `/realms/*` to the IdP, and everything else to
    the dashboard on :8000.
  - **NATS:** included.
  - **Configuration:** credentials through compose `secrets:` (from a
    gitignored `deploy/standalone/secrets/`), non-secret settings from `.env`,
    and the model registry mounted from a file. No scripted model.
- **SA-12: The dashboard behind a proxy.** Add
  `PIPELINE_DASHBOARD_ALLOWED_HOSTS` to `app/auth.py` `host_is_allowed`. When
  unset, behaviour is unchanged, and an entry with a wildcard is refused. Point
  the OIDC redirect URI at the dashboard's `/auth/callback` through the proxy.
- **SA-13: A standalone realm template.**
  - No smoke users.
  - A public `pipeline-cli` client using PKCE with loopback redirect URIs, for
    a client's OAuth login.
  - A confidential client per user for long-lived-credential mode.
  - Configurable access-token lifespan.
  - No literal secret; the installer renders placeholders.

### Epic 5: client authentication

- **SA-14: RFC 9728 protected-resource metadata.**
  `GET /.well-known/oauth-protected-resource` is unauthenticated and returns
  the `resource` URL, `authorization_servers: [issuer]`,
  `bearer_methods_supported: ["header"]` and the scopes. The 401 challenge adds
  `resource_metadata="<url>"`. File: `pipeline/remote_transport.py`.
- **SA-15: Long-lived-credential mode.** A `deploy/standalone/mcp-token` helper
  mints a short-lived token from the user's client credentials on each call,
  so the long-lived thing is a client secret in a 0600 file, not a token.
  Document it for clients that run a header helper. Before building, verify
  which header-helper mechanisms Claude Code and OpenCode support; if one does
  not, document a refresh workaround for it instead.

### Epic 6: install

- **SA-16: `deploy/install.sh`, interactive, with `--non-interactive` flags
  for CI.**
  1. Check that docker and compose are present and enough memory is allocated.
  2. Ask for the hostname, the first user and password, the model backends
     (Claude API key and/or subscription token, Ollama endpoint and optional
     key), the GitHub token, and the allowed repo hosts.
  3. Write the secrets at 0600, write `.env`, and render the realm.
  4. Pull or build the image, then run `up --wait`, and wait for the realm to
     load.
  5. Create the first user.
  6. Print the CA-trust steps (`NODE_EXTRA_CA_CERTS` for Node clients), the
     exact `claude mcp add --transport http ...` command and the OpenCode
     config snippet.
  7. Be idempotent: re-running reconfigures and never regenerates secrets
     silently.
- **SA-17: `docs/DEPLOY_STANDALONE.md`.** The same steps by hand. It covers
  connecting Claude Code and OpenCode in both auth modes, upgrading, backup
  (the state volume and the Keycloak volume), and troubleshooting: the CA,
  token expiry, the realm render, and memory.
- **SA-18: Standalone smoke in CI.** Run `install.sh --non-interactive`
  against the standalone stack with the scripted model, then connect, ingest
  from a `repo_url` on a local git server, and dispatch. Gated like
  `ENABLE_SMOKE_CI` until green.

### Epic 7: grading capacity

- **SA-19: Cap concurrent detached grades.** Observed 2026-10-30 on
  `smoke-stack-green`: SG-01, SG-02 and SG-04 finished together, and one tick
  started all three grades at 02:35:51. Each grade runs the full suite with
  `-n auto` (`pyproject.toml` addopts), so it fans out to every core. With the
  agents' own suites running too, about 70 xdist workers ran on 10 cores (load
  average about 50), and a 2-minute suite took more than 13 minutes. Nothing
  bounds the count today: `pipeline/detached_grade.py` `start_detached_grade`
  spawns on demand, and a grade has no wall-clock limit while its process is
  alive.
  - **The cap:** `PIPELINE_MAX_CONCURRENT_GRADES` (default 1). The count is
    taken from the live `grading_pid` values across every manifest, and dead
    pids do not count. A story over the cap stays `in_progress` with an
    unchanged `.agent_done.consumed` marker and is graded on a later tick, in
    `dispatched_at` order so none starves.
  - **The worker count:** an optional `PIPELINE_GRADE_XDIST_WORKERS` passes
    `-n <value>` to the grade command when the detected runner is pytest with
    xdist. It is unset by default, which leaves the command unchanged.
  - **Validation:** a value that is not a positive integer fails closed to the
    default, with a WARN.
  - **Before calling it done:** validate the cap against the real host
    (`.claude/rules/testing-config-gates.md`). Print the measured suite time
    under the cap next to the uncapped time, and confirm a queued story still
    grades within one tick of a slot freeing.
  - **Files:** `pipeline/detached_grade.py` (343 lines) holds the counting and
    admission helper. The call-site wiring goes in `pipeline/story_status.py`,
    which is at 987 lines, so it gets at most a few lines there, or a
    rename-and-delegate split if it does not fit. No dependency on Phase A or
    the other epics.

## Changes at preflight (2026-10-30)

A combined impact run (rough stand-ins of all 19 stories in one scratch
worktree) and a closer read of the code changed these drafts:

- **SA-00 (new): owner authority.** `app/policy.py` granted a remote plan
  owner only approve, reject, dispatch, merge and decision:answer, so most of
  the 13 newly exposed tools would still refuse every remote user. The
  operator chose to extend owners and delegates to story:patch,
  story:interrupt, story:mark_done, plan:pause and plan-scoped config:write.
  Host-scoped config:write stays local-operator only. ADR 0009 gets an
  amendment, and three existing tests that pinned the old boundary are
  pre-authorized edits.
- **SA-01 shrank** to `get_effective_config`: a remote caller must name a plan
  and gets only `roles`, never host env values or paths. Gating
  `decompose_plan` in the service broke two AST tests that pin its first
  statement, and filtering `advance_all_plans` there broke enforcement tests.
  Both moved into SA-02's middleware: a `workspace` argument from a remote
  caller is refused, and `advance_all_plans` results are filtered to visible
  plans with the existing `_scope_audit` helper.
- **SA-03** also makes `validate_workspace` enforce
  `PIPELINE_ALLOWED_REPO_ROOTS`, so the dashboard's decompose route is
  confined too. SA-08 defaults that variable to `PIPELINE_REPOS_ROOT` in the
  image.
- **SA-05** needs no dispatch change: dispatch already fetches the base branch
  from origin (`123ebbe`).
- **SA-07** pins gh 2.102.0, Node 24.22.0 and Claude Code 2.1.301, each at
  least two weeks old, with sha256 checksums for amd64 and arm64.
- **SA-08/SA-09:** the entrypoint exports the operator's key as
  `PIPELINE_ANTHROPIC_API_KEY`, and `app/backend_claude.py` maps it to
  `ANTHROPIC_API_KEY` after stripping the redirect variables, so an ambient
  key is still stripped. An empty secret file counts as absent, because
  compose needs every listed file to exist.
- **SA-10** passes the `headers` keyword only while `OLLAMA_API_KEY` is set.
  Passing it always broke about 60 tests whose httpx fakes have fixed
  signatures.
- **SA-13/SA-15 client facts** (verified 2026-10-30):
  - Claude Code checks RFC 9728 metadata first and supports `headersHelper`.
    The helper runs at connect and on reconnect and re-runs after a 401 or 403.
    Pre-registered redirects use `http://localhost:<port>/callback` with
    `--client-id` and `--callback-port`.
  - OpenCode supports OAuth with a pre-set `clientId`, and its redirect is
    `http://127.0.0.1:19876/mcp/oauth/callback`. It has no header helper, only
    `{env:VAR}` headers, so OAuth is its recommended mode.
- **SA-19** fits entirely in `pipeline/detached_grade.py`, so
  `story_status.py` is untouched. Stories over the cap wait for a later tick
  in no particular order.

## Order

As ingested: SA-00, SA-01, SA-04, SA-07, SA-08, SA-09, SA-10, SA-12, SA-13,
SA-14 and SA-19 have no prerequisites. SA-03 follows SA-01, SA-02 follows
SA-00, SA-01 and SA-03, and SA-05 follows SA-03 and SA-04. SA-11 follows
SA-07, SA-08 and SA-12..14, and SA-15 follows SA-13. SA-16 follows SA-11,
SA-13 and SA-15. SA-17 and SA-18 are last.

## Open questions

- **A forge besides GitHub.** GitLab exists in `pipeline/forge_gitlab.py` but
  needs `glab`. These stories add `gh` only; say so if GitLab is needed.
- **One user or several on one server.** Tenancy is enforced per plan owner
  (ADR 0009), but plan and story keys are not namespaced per tenant yet (the
  roadmap's "Per-tenant naming"). A server with several users needs that before
  keys collide.
- **Dispatched worker identity (SYS-03)** is still undesigned. Workers act as
  the local operator inside the container.
