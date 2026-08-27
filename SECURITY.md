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
- **The monitoring dashboard (`app/dashboard.py`) is unauthenticated by
  design** as a localhost-only, read-mostly status viewer. Do not expose it
  on a network interface beyond `localhost` without adding your own auth in
  front of it (a reverse proxy, etc.).
- **Secrets** (`PLANE_API_KEY`, provider tokens) are read from environment
  variables only — never commit them, and never put them in a plan JSON or
  `agent_instructions`, which are logged and may be shown to a dispatched
  model.
- The **workspace-selection** work in progress (`docs/plans/workspace-selection.json`)
  adds an HTTP surface that accepts an arbitrary filesystem path and can
  create directories/git repos; it includes its own dedicated
  security-hardening story (`WS-07`) before that surface is considered safe
  to expose.
