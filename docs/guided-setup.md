# Guided setup

## One command

Run `scripts/install.sh` (or the one-line `curl ... | bash` installer). It finishes by running `scripts/configure.py`. Before changing anything, configure detects the current state: prerequisites and authentication (via `install_checks`), the virtualenv, MCP registration, installed personas, role routing, and rendered services. It then applies only what is missing or explicitly requested.

## Interactive, `--yes`, and piped installs

Prompts appear only when **both** stdin and stdout are TTYs. `--yes` and any non-TTY run apply secure defaults and never prompt or hang. `curl ... | bash` is the non-interactive example: stdin is a pipe, so defaults are applied.

## Flags

| Flag | Meaning |
|---|---|
| `--repo-root` | Project the pipeline manages. Optional: the installer never defaults it to the Fagan checkout; when omitted, `REPO_ROOT` stays unset and each plan's `repo_root` (or the dashboard workspace picker) selects the target project. |
| `--data-dir` | Directory for pipeline data. |
| `--port` | Dashboard port (default `8001`). |
| `--autonomy` | `dry-run`, `gated`, or `full` (sets `PIPELINE_AUTONOMY`). |
| `--role` | Role whose routing to set. |
| `--provider` | Backend for `--role`. Requires `--role`. |
| `--model` | Model for `--role`. Requires `--role`. |
| `--set KEY=VALUE` | Set any schema key. Repeatable. |
| `--skip-notifications` | Skip the notifications section. |
| `--skip-integrations` | Skip the integrations section. |
| `--skip-services` | Skip service rendering and registration. |
| `--skip-claude-assets` | Skip MCP registration and persona installation. |
| `--yes` | Accept defaults without prompting. |
| `--dry-run` | Print what would change without writing. |

## What it configures

| Section | Keys |
|---|---|
| core | `REPO_ROOT`, `PLAN_DIR`, `WORKTREE_ROOT`, `AGENTS_DIR`, `PIPELINE_MODEL_REGISTRY_PATH` |
| routing | `PIPELINE_BACKEND_<ROLE>` for `OVERLORD`, `PLANNER`, `DISPATCH`, `REVIEW`, `DECOMPOSE`, `TEST_AUTHOR`, `DIAGNOSIS`, `SECURITY`, `CHAT` |
| policy | `PIPELINE_AUTONOMY`, `PIPELINE_RISK_THRESHOLD`, `PIPELINE_MAX_CONCURRENT_AGENTS`, `PIPELINE_DAILY_REQUEST_THRESHOLD`, `PIPELINE_WEEKLY_REQUEST_THRESHOLD`, `PIPELINE_PAUSE_THRESHOLD`, `PIPELINE_RESUME_THRESHOLD`, `PIPELINE_ESCALATION_BACKEND`, `PIPELINE_ESCALATION_MODEL`, `PIPELINE_SECURITY_AUDIT_EVERY_DAYS`, `PIPELINE_SECURITY_AUDIT_EVERY_COMMITS`, `PIPELINE_AUTO_MERGE` |
| notifications | `PIPELINE_NOTIFY_OUTBOX_ENABLED`, `PIPELINE_NOTIFY_OUTBOX_EVENTS`, `PIPELINE_NOTIFY_EMAIL_ENABLED`, `PIPELINE_NOTIFY_EMAIL_HOST`, `PIPELINE_NOTIFY_EMAIL_PORT`, `PIPELINE_NOTIFY_EMAIL_USER`, `PIPELINE_NOTIFY_EMAIL_PASSWORD` (**secret**), `PIPELINE_NOTIFY_EMAIL_FROM`, `PIPELINE_NOTIFY_EMAIL_TO`, `PIPELINE_NOTIFY_EMAIL_TIMEOUT` |
| integrations | `PLANE_BASE`, `PLANE_API_KEY` (**secret**), `PLANE_WORKSPACE`, `PLANE_PROJECT`, `PIPELINE_TICKET_PROVIDER` |
| Claude assets | MCP server registration and persona files in `AGENTS_DIR` (no `.pipeline.env` keys) |
| services | Dashboard and scheduler launchd/systemd definitions (no `.pipeline.env` keys) |

## Secure defaults

- The notification outbox and email are off.
- Plane is off and the ticket provider is `none`.
- Background services are opt-in.
- Autonomy is `dry-run` on a fresh install.
- The dashboard and scheduler are started in this session only when you choose it.

## Secrets, permissions, and backups

Secret values are never echoed or logged. `.pipeline.env` is written `0600` when it contains a secret and `0644` otherwise. An existing file is backed up before a rewrite, and unchanged content is not rewritten.

## Idempotency and preserving your settings

Re-running is safe. Existing `.pipeline.env` values are preserved; only explicit flags, `--set`, and answers override them. The committed `model_registry.json` is never modified; role routing goes to the gitignored `model_registry.local.json`.

## Services

Services can be render-only (files are written, nothing is loaded) or registered (`launchctl` on macOS, `systemctl --user` on Linux). Alternatively, configure can start the dashboard and scheduler in this session and verify them via `/api/health`.

On macOS, rendering launchd files requires `MLX_MODEL_PATH` because the `mlx-supervisor` job uses it; the generator fails closed without it. Set it, or start the services in-session instead. Linux uses systemd user units.

## Moving from dry-run to gated

Review a dry run first, then re-run with `--autonomy gated` (or set `PIPELINE_AUTONOMY=gated`). In gated mode the risk threshold (`PIPELINE_RISK_THRESHOLD`) governs which changes may merge unattended; anything above it waits for you.

## Troubleshooting

- **`gh` or `claude` missing or unauthenticated:** install it, run `gh auth login` / sign in to Claude Code, then re-run.
- **A non-TTY run applied defaults:** re-run from a terminal without `--yes` to be prompted, or pass the values as flags / `--set`.
- **MCP registration failed:** register manually:
  `claude mcp add -s user pipeline <repo>/.venv/bin/python3 <repo>/app/pipeline_mcp_server.py`
- **Re-run configure alone:** `.venv/bin/python scripts/configure.py` (add `--dry-run` to preview).
