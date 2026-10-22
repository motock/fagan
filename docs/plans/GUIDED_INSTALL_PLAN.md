# Guided installation plan

**Status:** proposal, not implemented. The pipeline registration of this plan
is `~/.claude/plans/guided-install.json` (3 epics, 13 stories); implementation
is deferred, and every story still needs its mandatory impact-run preflight
before the plan is ingested or dispatched.

## Problem

The install path is spread across four scripts, and none of them leaves a new
user with a working configuration:

| Script | What it actually does | What it leaves manual |
|---|---|---|
| `scripts/install.sh` | Creates `.venv`, installs runtime/dashboard deps, prints a doctor report and "next steps" | Provider routing, `.pipeline.env`, MCP registration, personas, services |
| `scripts/remote-install.sh` | Clones or updates, runs `install.sh`, copies personas/policy, registers the MCP server | Same configuration gaps; only on the curl path |
| `scripts/standalone-setup.sh` | Writes `.pipeline.env`, starts dashboard + scheduler, health-checks | Read-only dashboard instance; never registers MCP; reads the committed registry, which ships no roles |
| `scripts/choose_providers.py` | Interactive per-role provider picker | Separate manual step; no env, MCP, or service setup |

The result is that "installed" and "ready to work" are different states. With
the shipped registry declaring no `roles`, a fresh clone silently falls back to
the `claude` backend for every role, and the optional surfaces — email
notifications, Plane mirroring, usage gates, escalation, launchd/systemd — are
discovered only by reading `REFERENCE.md`.

## Goals

- One command takes a clone to a verified installation: prerequisites and
  authorization, venv, provider/model routing, optional configuration, MCP
  registration, personas/policy, services, and a final health/summary report.
- Existing values are never clobbered. `.pipeline.env` is backed up and merged;
  the committed `model_registry.json` is never modified (role routing goes to
  the gitignored `model_registry.local.json`).
- The flow is fully scriptable for automation and safe when piped.
- Secrets are never echoed, logged, or placed in an error message, and the env
  file holding them is written `0600`.

## Non-goals

- Packaging the project as a `pip`/`uvx` distribution. That is a separate
  design (`docs/specs/PIP_INSTALLABILITY_DESIGN.md`).
- Rewriting `remote-install.sh`. It stays behaviorally unchanged and keeps
  calling `install.sh` with no flags; piped stdin makes the guided flow take
  non-interactive defaults.
- Editing `REFERENCE.md`. At ~1,877 lines it exceeds this dispatch tier's safe
  edit ceiling; reference-level detail goes in `docs/guided-setup.md`.

## Interaction contract

- Prompts appear only when **stdin and stdout are both TTYs**.
- `--yes` and every non-TTY run apply documented secure defaults and never
  prompt, block, or hang.
- `--dry-run` prints every planned change and writes nothing.
- `--set KEY=VALUE` (repeatable) covers advanced/non-interactive configuration;
  every key is validated against a declarative schema.
- Section skips (`--skip-notifications`, `--skip-integrations`,
  `--skip-services`, `--skip-claude-assets`) and core flags (`--repo-root`,
  `--data-dir`, `--port`, `--autonomy`, `--role`/`--provider`/`--model`) cover
  the rest.
- `--repo-root` is optional and is **never** defaulted to the pipeline
  checkout. When it is omitted, `REPO_ROOT` stays unset and the summary points
  at `--repo-root` or the dashboard workspace picker. This prevents a fresh
  install from silently pointing the pipeline at its own repository.

## Secure defaults

| Setting | Fresh-install default | Rationale |
|---|---|---|
| `PIPELINE_AUTONOMY` | `dry-run` | Watch one plan before anything is dispatched or merged |
| Outbox / email notifications | `0` | No mail or spool until explicitly enabled |
| Plane / ticket provider | off / `none` | The manifest is authoritative; no external mirror by default |
| Background services | not registered | Persistent launchd/systemd jobs are opt-in |
| Dashboard/scheduler now | prompted; default yes only interactively | A usable session without installing persistence |
| Escalation backend/model | unset | Keep the code's dispatch-derived behavior |

## Architecture

New package `pipeline/setup/` (stdlib only, no import-time side effects) plus a
thin `scripts/configure.py` CLI:

```
pipeline/setup/
  schema.py        declarative KeySpec catalogue, defaults, validators
  envfile.py       .pipeline.env parse/merge/atomic write, backup, permissions
  detect.py        prerequisites, venv, MCP, assets, routing, services state
  ui.py            TTY detection and prompt primitives (injectable)
  providers.py     per-role routing through PipelineService.set_role_default
  notifications.py outbox + email configuration
  policy.py        autonomy, risk, concurrency, usage gates, escalation, audits
  integrations.py  Plane + ticket provider
  claude_setup.py  MCP registration, personas, overlord policy, global rules
  services.py      launchd/systemd rendering, start, health verification
  wizard.py        section plan -> one env write -> appliers -> summary
scripts/configure.py   argparse entry; lazy imports; no setup logic
scripts/install.sh     adds a flag loop and invokes configure.py once
```

`install.sh` remains the bootstrap: it must preserve every literal and ordering
assertion the installer tests pin, including the single
`scripts/install_checks.py` invocation and the `==> Done. Next steps:` block.
The guided logic is Python so it can be tested without spawning a shell wizard.

## Configuration coverage

- **Core:** `REPO_ROOT`, `PLAN_DIR`, `WORKTREE_ROOT`, `AGENTS_DIR`,
  `PIPELINE_MODEL_REGISTRY_PATH`.
- **Routing:** `PIPELINE_BACKEND_<ROLE>` for all nine roles, applied through the
  validating registry service; presets: claude, local ollama, cloud ollama,
  custom.
- **Policy:** `PIPELINE_AUTONOMY`, `PIPELINE_RISK_THRESHOLD`,
  `PIPELINE_MAX_CONCURRENT_AGENTS`, daily/weekly request thresholds,
  pause/resume thresholds, `PIPELINE_ESCALATION_BACKEND`/`_MODEL`,
  `PIPELINE_SECURITY_AUDIT_EVERY_DAYS`/`_COMMITS`.
- **Notifications:** `PIPELINE_NOTIFY_OUTBOX_ENABLED`/`_EVENTS` and
  `PIPELINE_NOTIFY_EMAIL_ENABLED`/`HOST`/`PORT`/`USER`/`PASSWORD`/`FROM`/`TO`/
  `TIMEOUT`.
- **Integrations:** `PLANE_BASE`/`API_KEY`/`WORKSPACE`/`PROJECT`,
  `PIPELINE_TICKET_PROVIDER`.
- **Claude assets:** MCP server registration, `agents/*.md` + overlord policy,
  opt-in global rules.
- **Services:** launchd (macOS) or systemd user units (Linux), in-session
  dashboard/scheduler start, `/api/health` verification.

## Stories

| Key | Story | Production files | Deps | Risk |
|---|---|---|---|---|
| SETUP-01 | Declarative guided-setup schema for every configuration key | `pipeline/setup/__init__.py`, `pipeline/setup/schema.py` | — | low |
| SETUP-02 | Read, merge, atomically write `.pipeline.env` with backups and secret-safe permissions | `pipeline/setup/envfile.py` | — | medium |
| SETUP-03 | Detect prerequisites, venv, MCP, assets, routing, rendered services | `pipeline/setup/detect.py` | — | low |
| SETUP-04 | TTY-safe prompt primitives that never block | `pipeline/setup/ui.py` | — | low |
| SETUP-05 | Configure provider/model routing per role through a local registry | `pipeline/setup/providers.py` | 01 | low |
| SETUP-06 | Configure outbox + optional email notifications | `pipeline/setup/notifications.py` | 01 | low |
| SETUP-07 | Configure runtime policy, usage gates, escalation, audit cadence | `pipeline/setup/policy.py` | 01 | low |
| SETUP-08 | Configure Plane ticketing behind explicit opt-in | `pipeline/setup/integrations.py` | 01 | medium |
| SETUP-08B | Install MCP, personas, policy, and opt-in global rules non-destructively | `pipeline/setup/claude_setup.py` | — | medium |
| SETUP-09 | Render, optionally enable, start, and health-verify services | `pipeline/setup/services.py` | — | medium |
| SETUP-10 | Orchestrate sections into an idempotent, dry-runnable plan with a summary | `pipeline/setup/wizard.py` | 02–09 | low |
| SETUP-11 | Add `scripts/configure.py` and wire it into `install.sh` | `scripts/configure.py`, `scripts/install.sh` | 10 | low |
| SETUP-12 | Document one-command guided setup | `README.md`, `docs/guided-setup.md` | 11 | low |

Stories 01–04 are independent foundations; 05–08B each depend only on the
schema; 10 composes them; 11 wires the CLI; 12 documents the result.

## Test-contract constraints

The installer surface is pinned by several suites that a rewrite would break:

- `tests/unit/test_install_script_wiring.py` and
  `tests/unit/test_install_script_dashboard_deps.py` pin exact `install.sh`
  literals, ordering, and the single doctor invocation.
- `tests/unit/test_remote_install_script.py` and
  `tests/unit/test_remote_install_claude_setup.py` pin the curl path's
  behavior, including no interactive commands and no extra network calls.
- `tests/unit/test_standalone_setup_script.py` pins the standalone env-file and
  health-check contract.
- `tests/unit/test_registry_ships_provider_neutral.py` pins the committed
  registry as role-free.
- `tests/unit/test_docs_provider_setup.py` and the README structure tests pin
  the documentation sections.

Exactly one existing test needs editing: `tests/unit/test_local_agent_split.py`
treats any unrecognized `scripts/*.py` as the local-agent split module, so
`configure.py` must be added to `_BASELINE_SCRIPTS`. The exact before/after text
is in the plan's SETUP-11 brief, and the change must be justified in the commit
message. No other existing-test edit is planned.

## Preflight and definition of done

This plan was authored for a cloud open-source dispatch tier, but the impact
run could not be performed while implementation was deferred. Before ingest:

1. In a scratch worktree off current `master`, apply a rough stand-in of each
   story's production change.
2. Run `.venv/bin/python -m pytest -q` and `.venv/bin/python -m ruff check .`.
3. Replace each story's `Preflight:` line with the measured result.
4. Pre-authorize, re-scope, or split any story whose change fails a
   pre-existing test.

Then follow the normal gates: TDD tests first, full suite green, `review_story`,
`approve_merge` (never a manual merge), and `mark_story_done`.

## Open questions

- Should the default routing preset be `claude` when no provider is chosen, or
  should the wizard require an explicit choice? The plan currently applies the
  `claude` preset on a fresh non-interactive install only when the registry has
  no roles and `claude/sonnet` is declared, preserving the documented fallback.
- Should persistent service registration default on for the one-line installer?
  The plan keeps it opt-in to preserve secure defaults; starting
  dashboard/scheduler for the current session is offered separately.
- Email timeout is `--set`-only rather than prompted, to keep the interactive
  flow short. Revisit if notification setup becomes a common path.
