# Security Policy

## Supported versions

This project does not yet cut releases or maintain version branches — `master`
is the only supported line. Security fixes land there.

## Reporting a vulnerability

Please **do not open a public GitHub issue** for a suspected vulnerability.

Instead, use [GitHub's private vulnerability reporting](../../security/advisories/new)
for this repository (Security tab → "Report a vulnerability"). If that isn't
available to you, open a regular issue asking for a private contact channel
and no other detail — a maintainer will follow up.

Include, where you can:
- A description of the issue and its potential impact.
- Steps to reproduce, or a minimal proof of concept.
- The affected file(s)/commit.

There is no formal SLA (single-maintainer project — see the README's
[Reliability & limitations](README.md#reliability--limitations) section), but
reports will be acknowledged and investigated in good faith.

## Scope notes specific to this project

A few things worth knowing when assessing this codebase's attack surface:

- **The dispatched coding agents run with real write access** to git worktrees
  and, depending on backend, real subprocess/bash execution
  (`scripts/local_agent.py`). This is inherent to what an autonomous coding
  pipeline does, not a bug — but it means a malicious or compromised plan
  (`agent_instructions`, `acceptance` fixtures) is a genuine code-execution
  vector. Don't ingest a plan from a source you don't trust.
- **The dashboard's HTTP API is gated by a shared-secret key.** Every request
  under the `/api/` prefix (`app/dashboard.py`'s middleware) must present a
  matching `X-Pipeline-Api-Key` header, checked with a constant-time
  comparison (`app/auth.py`); the key is generated on first use and stored in
  a `0600` file at the repo root. There is no unauthenticated route under
  `/api/`.
- **The dashboard index route (`/`) is deliberately exempt from that gate,
  and that exemption depends on staying bound to loopback.** `/` is what
  injects the key into the page for the browser to use on subsequent
  requests, so gating it behind the same header it exists to hand out would
  be an unreachable chicken-and-egg lock (see `app/auth.py`'s module
  docstring). `scripts/dashboard.sh` defaults `DASHBOARD_HOST` to
  `127.0.0.1`, which keeps this safe. **If you run the dashboard with
  `DASHBOARD_HOST=0.0.0.0` (or otherwise put it on a network-reachable
  interface), `/` becomes an unauthenticated key handout to anyone who can
  reach that port** — put a reverse proxy or your own auth in front of it
  before doing that, or leave `DASHBOARD_HOST` at its loopback default.
- **The chat tool registry's `read_file`, `list_directory`, and
  `search_code` tools (`app/chat.py`) are prompt-reachable filesystem
  reads.** An attacker who controls chat input can direct the model to call
  them, but every call is resolved through
  `pipeline/workspace_fs.py`'s `resolve_within_workspace` (search is a
  `grep` confined to the same root), which rejects any path — including via
  a symlink — that resolves outside the active workspace, fail-closed. The
  guard is what enforces the boundary, not the model's cooperation: what's
  reachable is any file inside the active workspace; nothing outside it is,
  and the workspace root itself can never be set to this repo's own root
  (`pipeline/workspace.py`'s deny list rejects it), so the dashboard API key
  file is not reachable through these tools.
- **Workspace selection is path-scoped, not open.** Selecting or creating a
  workspace (`pipeline/workspace.py`) resolves the requested path, rejects
  symlinked workspaces and symlink escapes, and denies a fixed list of
  sensitive locations (including this repo's own root) regardless of how the
  path is spelled; every check re-runs fail-closed before any filesystem
  write. This replaced an earlier, narrower posture — see the workspace
  security work referenced from `docs/plans/workspace-selection.json` and its
  follow-up hardening plans.
- **Secrets** (`PLANE_API_KEY`, provider tokens) are read from environment
  variables only — never commit them, and never put them in a plan JSON or
  `agent_instructions`, which are logged and may be shown to a dispatched
  model.
