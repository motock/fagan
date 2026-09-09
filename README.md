# Autonomous SDLC Agent Pipeline

[![CI](https://github.com/motock/claude-pipeline-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/motock/claude-pipeline-mcp/actions/workflows/ci.yml)

This is a quickstart guide for the Autonomous SDLC Agent Pipeline, describing the system components and how they interact. For detailed reference material, see [REFERENCE.md](./REFERENCE.md).

**Before you start:** read [Reliability & limitations](#reliability--limitations)
below. This is an autonomous coding pipeline with real, documented failure
modes — it is not a hands-off "describe a feature, get a PR" tool yet.

## Platform support

Developed and run day-to-day on **macOS**. The core (MCP server, dashboard,
Claude-backend dispatch/review, the full test suite) is plain Python and CI
tests it on Ubuntu across Python 3.12–3.14 on every push. Two pieces are
**macOS-only**:

- **`launchd/*.plist`** — the scheduler/MLX-supervisor/usage-poller are
  packaged as launchd jobs. On Linux, run the same entry points directly
  (e.g. `python3 -m pipeline.scheduler_daemon`) under your init system or
  supervisor of choice, or in a foreground terminal/`tmux` session.
- **MLX** (`PIPELINE_LOCAL_PROVIDER=mlx`) — Apple Silicon only. Local dispatch
  works fine on Linux via **Ollama** or **LM Studio** instead
  (`PIPELINE_LOCAL_PROVIDER=ollama` / `lmstudio`).

Windows is untested.

## Quickstart

This gets the MCP server registered and a first plan running end-to-end.
Dispatch/review default to the `claude` backend, which needs no local model —
it shells out to the Claude Code CLI. The shipped registry deliberately ships
no role routing, so provider selection is a setup step, not a default: see
**Provider selection & authorization** below.

```bash
# 1. Clone and install the Python environment
git clone https://github.com/motock/claude-pipeline-mcp.git
cd claude-pipeline-mcp
scripts/install.sh          # creates .venv, installs requirements.txt

# 2. Register the MCP server with Claude Code (adjust the path to where you cloned it)
claude mcp add -s user pipeline "$(pwd)/.venv/bin/python3" "$(pwd)/app/pipeline_mcp_server.py"

# 3. Copy the persona subagents and decision policy into place
mkdir -p ~/.claude/agents
cp agents/*.md ~/.claude/agents/
cp overlord-policy.md ~/.claude/overlord-policy.md

# 4. Restart Claude Code (or start a new session) so it picks up the MCP server
```

`scripts/install.sh` creates the `.venv`, installs `requirements.txt` and
`requirements-dashboard.txt` (the dashboard's `fastapi`/`uvicorn` deps, installed
on every run; a `--dev` install uses `requirements-dev.txt`, which already
includes the dashboard deps), and reports on the tools the pipeline shells out
to — required: `git`, `gh`, and the `claude` CLI; optional: `ollama` and
`docker` — with graceful-degradation messaging, and is safe to re-run. It does **not** register the MCP server, set environment
variables, or install the persona subagents — steps 2–4 above cover those. With
nothing but the `claude` backend configured, `ollama`/`docker` being absent is
expected, not an error.

From a Claude Code session in the project you want the pipeline to work on:

1. Ask the `product-analyst` subagent to turn a goal into epics/stories, or
   hand-write a plan per [the schema](REFERENCE.md#plan--story-schema).
2. `mcp__pipeline__save_plan` (or `ingest_plan`) with that plan and a
   `repo_root` pointing at the target project — **not** this pipeline repo.
3. `mcp__pipeline__list_ready_stories` to see what's unblocked, then
   `mcp__pipeline__dispatch_story` to claim and start one.
4. Watch progress with the dashboard: `scripts/dashboard.sh start`, then open
   `http://localhost:8000`.
5. For unattended operation, run the scheduler so ready stories advance
   without you calling `advance_pipeline` by hand:
   `.venv/bin/python3 -m pipeline.scheduler_daemon` (foreground, or under
   launchd/systemd/tmux — see [Scheduler](#scheduler) below).

Start with `PIPELINE_AUTONOMY=dry-run` (plans and logs only, nothing is
dispatched or merged) until you've watched one plan run and trust the gates —
see [Autonomy levels](REFERENCE.md#configuration-environment-variables).

**Only using the `claude` backend?** The `PIPELINE_LOCAL_*` and
`PIPELINE_BACKEND_*=ollama/lmstudio/mlx` variables, and Ollama/MLX/LM Studio
setup, only matter if you opt a role into local-model dispatch — but provider
selection itself is still a required setup step (the shipped registry routes
nothing; see **Provider selection & authorization** below), and even the
`claude` path needs two credentials before the first dispatch: `gh auth login`
(the pipeline opens and merges PRs through the GitHub CLI) and the Claude Code
CLI's own login. See
[Minimal configuration](REFERENCE.md#minimal-configuration) for the handful of
variables actually worth setting on day one, versus the ~100 that exist purely
for tuning.

### Provider selection & authorization

**Provider selection is a required setup step.** The shipped
`model_registry.json` deliberately declares which models exist per provider
but ships **no `roles` routing**: this project decouples from any single
provider, so the operator chooses. There are two supported ways to select a
provider per role, checked in this order by `resolve_role`:

1. **Plan role config** — a plan's per-role `provider`/`model` beats
   everything below.
2. **`PIPELINE_BACKEND_<ROLE>` environment variables** — e.g.
   `PIPELINE_BACKEND_DISPATCH=ollama` opts the dispatch role into Ollama.
3. **A `roles` block in a registry file** — see below.
4. **The caller's own fallback** — for dispatch/review this is the `claude`
   backend.

The same two registry files work for both selection styles:

- **`PIPELINE_MODEL_REGISTRY_PATH`** points the pipeline at any registry
  JSON you like.
- **`model_registry.local.json`** (repo root) is the convention for a
  personal registry: it is gitignored, so your per-role routing stays out of
  the repo. Point `PIPELINE_MODEL_REGISTRY_PATH` at it, or copy it over
  `model_registry.json` locally if you prefer not to set the variable.

A `roles` block names a provider and a *friendly* model name per role; the
friendly name must exist under that provider's `models` in the same file, and
the concrete tag is resolved from there. A typo raises an error rather than
silently falling back.

**Authorization matrix.** Selecting a provider also selects which credentials
you must establish first — `scripts/install_checks.py` probes these and
reports `unauthorized` (remedy: a login, not an install) where it can:

| Provider / tool | Credential needed | How to establish it |
| --- | --- | --- |
| `git` / `gh` | GitHub auth (the pipeline opens and merges PRs through `gh`) | `gh auth login` |
| `claude` backend | Claude Code CLI's own login | `claude auth login` (check: `claude auth status`) |
| any `:cloud` ollama tag | An ollama.com account, signed into the **local daemon** | `ollama signin` |
| `litellm` backend | Per-vendor API keys | See [docs/specs/LITELLM_PROVIDER.md](docs/specs/LITELLM_PROVIDER.md) |
| on-device ollama / lmstudio / mlx tag | Nothing extra | — |

On the `:cloud` rows: those calls are proxied through `https://ollama.com` by
the local ollama daemon, which sends its own credential — the pipeline sends
no credential of its own. `:cloud` tags are the *only* ollama tags that need
a sign-in; purely on-device tags need nothing beyond the daemon running.

### Getting-started walkthrough

No local model is required anywhere in this walkthrough: with
`PIPELINE_BACKEND_DISPATCH=claude` (set it explicitly, or add a `roles` block
to a local registry — the shipped registry routes nothing; see **Provider
selection & authorization** above) dispatch and
review shell out to the Claude Code CLI and never touch ollama.

1. **Install** — one command: `scripts/install.sh` (see the quickstart above
   for what it does and does not do).
2. **Register the MCP server and personas** — quickstart steps 2–3 above
   (`claude mcp add ...` plus copying `agents/*.md` and the overlord policy),
   then restart Claude Code.
3. **Start the dashboard** — `scripts/dashboard.sh start`, then open
   `http://localhost:8000` and pick your target project in the workspace
   picker.
4. **Decompose a tiny goal** — ask the `product-analyst` subagent (or the
   dashboard's decompose action) to turn a one-liner goal into
   epics/stories, then `mcp__pipeline__save_plan` the result with its
   `repo_root` field pointing at your target project — not this pipeline repo.
5. **Dispatch the first ready story** — `mcp__pipeline__list_ready_stories`,
   then `mcp__pipeline__dispatch_story` on the first one, and watch the story
   advance across the kanban board in the dashboard.
6. **Watch it merge** — with `PIPELINE_AUTONOMY=gated` (the default), a
   risk-`low` story that passes review merges unattended. Start with
   `PIPELINE_AUTONOMY=dry-run` first, per the quickstart advice above.
7. **Prefer the scripted path?** — `python scripts/smoke_getting_started.py`
   runs the same flow end-to-end without the dashboard, in a scratch
   `PLAN_DIR` that never touches your real plans.

For what can still go wrong, see
[Reliability & limitations](#reliability--limitations).

### Companion MCP server (overlord + acceptance-oracle only)

Not ready to adopt the whole orchestrator? `pipeline/companion_server.py` is a
second, smaller MCP server (`pipeline-companion`) exposing only the two
adoptable ideas from Plan B5: `escalate_decision` (the overlord decision path)
and the acceptance-oracle helpers `classify_oracle_outcome` /
`acceptance_digests`. It imports the real `pipeline.overlord` and
`pipeline.oracle_gate` modules rather than duplicating them, so it stays in
sync with the main server. Add it alongside the main server as a second
`mcpServers` entry:

```json
{
  "mcpServers": {
    "pipeline": {
      "command": ".venv/bin/python3",
      "args": ["app/pipeline_mcp_server.py"]
    },
    "pipeline-companion": {
      "command": ".venv/bin/python3",
      "args": ["-m", "pipeline.companion_server"]
    }
  }
}
```

The adoptable specs this server exports live in `docs/specs/`:
`OVERLORD_POLICY_SPEC.md` (the overlord decision path),
`ACCEPTANCE_ORACLE_PATTERN.md` (the acceptance-oracle grading pattern), and
`DOCKER_SANDBOX.md` (the opt-in Docker sandboxing behavior).

## Components at a glance

| Piece | Location | Role |
|---|---|---|
| Persona subagents | `~/.claude/agents/*.md` | The SDLC roles agents play |
| Decision policy | `~/.claude/overlord-policy.md` | How the overlord decides |
| Pipeline MCP server | `app/pipeline_mcp_server.py` (launch shim) → `pipeline/` package | All pipeline tools + orchestration; `pipeline/server.py` is the entry module, split across `pipeline/*.py` (dispatch, review, ci, advance, store, etc.) |
| Backend seam | `app/backend.py` | Per-role driver routing (`claude` / `ollama` / `lmstudio` / `mlx` / `local`); single-shot, review, dispatch, resource gate |
| Local agent loop | `scripts/local_agent.py` | Native-tool-calling write loop for local dispatch (subprocess) |
| Monitoring dashboard | `app/dashboard.py`, `static/` | Read-only FastAPI status/lifecycle viewer |
| Install / deps | `scripts/install.sh`, `requirements*.txt` | venv + dependency setup |
| Tests | `tests/unit/` (5,600+ tests) | `pytest`, run via the venv |
| Plans / manifests / logs | `~/.claude/plans/` | Plan, manifest, decisions, notifications |
| Worktrees | `~/.claude/worktrees/` | Isolated per-story branches |
| Issue tracker | Plane (external, optional) | Mirror of story state; skipped entirely when unconfigured (manifest is the source of truth) |

---

## Architecture

```
            ┌─────────────────────────────────────────────────┐
            │ Orchestrator loop (cron / /loop skill)          │
            │ advance_pipeline(plan) — one idempotent tick    │
            └───────────────────────┬─────────────────────────┘
     ready stories                   │  gates adjudicated by overlord
     (deps satisfied)                │
                                      ▼
 
   ┌───────────────┐ resolve backend + persona/model  ┌─────────────────────────────┐
   │ Plan/Manifest │─────────────────────────────────►│ Dispatch:                   │
   │ (JSON, Plane) │                                  │  • claude -p  OR  local loop│
   └──────────┴────┘                                  │  • tech-lead planner →      │
              │                                       │    .agent_plan.md (local)   │
              │                                       │                             │
              │                                       └──────────────┬──────────────┘
              │                                                      │
              │ audit → decisions log                                ▼
              │                                       ┌─────────────────────────────┐
              │                                       │ Headless story agent        │
              │                                       │ in git worktree             │
              │                                       └──────────────┬──────────────┘
              │                       tests + acceptance oracle      │
              │                   local fail → escape to Claude    │
              │                                                      ▼
```

## Personas (`~/.claude/agents/`)

Each persona is a Claude Code subagent: a markdown file with YAML frontmatter
(`name`, `description`, `model`, and optionally `memory: user`) and a
system-prompt body. The pipeline reads the body and dispatches a headless agent
with it as the role.

`memory: user` injects the user-memory directory into the system prompt on
every Claude call — high-leverage context but expensive in tokens. The
**reviewer personas** (`code-reviewer`, `security-engineer`) deliberately omit
it: their job is a mechanical check (run tests, read diff, emit `VERDICT`),
the CLAUDE.md rules they need are in the persona body, and skipping the
~132 KB memory injection shaves ~30-40% off every review call's input tokens.
The dispatch and overlord personas keep it because they benefit from project
context and are lower-volume.

| Persona | Default model | Responsibility |
|---|---|---|
| `product-analyst` | opus | Decompose a goal into epics/stories with acceptance criteria, dependencies, and per-story `persona`/`model`/`risk` |
| `solution-architect` | opus | General system design, tech selection, API design (delegates mobile to `mobile-architect`) |
| `software-engineer` | sonnet | Default TDD implementer for non-mobile work |
| `security-engineer` | opus | Threat modeling and security review (OWASP, Secure by Design) |
| `devops-release-engineer` | sonnet | Build/CI, branch & worktree hygiene, releases |
| `code-reviewer` | sonnet | Reviews a branch, emits a `VERDICT`, opens a PR |
| `tech-writer` | haiku | Docs for externally visible changes |
| `overlord` | opus | The decision authority (see below) |

Existing mobile specialists (`mobile-architect`, `mobile-engineer`,
`ux-mobile-principal`, `qa-test-engineer`) are unchanged and used for mobile work.

To change a persona's behavior or default model, edit its `.md` file. The
frontmatter `model:` line is the fallback model when a story does not specify one.

---

## The overlord and the decision policy

The **overlord** (`~/.claude/agents/overlord.md`) rules on the user's behalf when
a story agent is blocked, two personas disagree, or a gate needs adjudication. It
follows `~/.claude/overlord-policy.md` (plus an optional per-repo
`<repo>/.overlord-policy.md` override).

**Decision tiers:**

1. **Routine / reversible** → decide silently (naming, internal structure, a
   library within the approved stack, refactors).
2. **Notify-async** (`risk: medium`) → decide, proceed, flag the user (new
   dependency, schema change, additive API change).
3. **Park-and-ping** (`risk: high`) → do **not** act unattended; hold for human
   review and notify. Anything irreversible, security/auth, money, production
   config, or breaking changes. **Always parked regardless of autonomy level.**

The overlord returns a structured ruling (`RULING` / `TIER` / `RISK` /
`RATIONALE` / `NOTIFY_USER`) that is parsed and written to the plan's decisions
log as an audit record.

---

## Reference

See [`REFERENCE.md`](REFERENCE.md) for the full MCP tools reference, the plan/story JSON schema, per-role provider/model configuration, guided decomposition and TDD-split details, every `PIPELINE_*`/`LOCAL_AGENT_*` environment variable, the end-to-end workflow, safety controls, the usage gate, and development/testing instructions.

## Prerequisites

- **Python 3.10+** and the project venv.
- **git** on PATH.
- **GitHub CLI** (`gh`).
- **Claude Code CLI** (`claude`).

## Scheduler

The **advance-scheduler** is now a long‑lived daemon rather than a 60s launchd tick. Launchd now only crash‑restarts the daemon via KeepAlive.

### Environment Variables
- **PIPELINE_SCHEDULER_INTERVAL_S** – default reconcile sweep interval (default 60 seconds).
- **PIPELINE_SCHEDULER_HEALTH_PATH** – optional path where the daemon writes its health JSON each iteration.

### Rendering the launchd files for your machine

The committed `launchd/*.plist` files and `launchd/pipeline-logs.newsyslog.conf`
are a reference copy: they carry the maintainer's own absolute paths (a
`/Users/<name>/...` home directory, a specific model cache path) and will not
work unedited on another machine. On a fresh install, regenerate them yourself
with `scripts/generate_launchd_plists.sh` (install.sh does not run this for
you) — it fills the templates in `launchd/`
(`launchd/com.claude.pipeline.*.plist.template`) from three flags:

- `--repo-root` — the pipeline checkout the rendered files should point at
  (default: the repo that contains the script).
- `--out-dir` — where the rendered files are written (default:
  `<repo-root>/launchd`).
- `--mlx-model-path` — the local MLX model directory baked into the
  mlx-supervisor plist. As an alternative to the flag you can set the
  `MLX_MODEL_PATH` environment variable; the flag wins when both are given.
  The script fails closed — it exits with an error — when neither is supplied.

The same script also renders `launchd/pipeline-logs.newsyslog.conf` from
`launchd/pipeline-logs.newsyslog.conf.template`, substituting only the repo root.

```bash
scripts/generate_launchd_plists.sh \
  --repo-root "$HOME/.claude/mcp-servers/pipeline" \
  --out-dir "$HOME/.claude/mcp-servers/pipeline/launchd" \
  --mlx-model-path "$HOME/.cache/qwen2.5_coder_14b_manual"
```

These launchd files are macOS-only — see [Platform support](#platform-support);
on Linux, run the entry points under your own init system instead.

## Reliability & limitations

This pipeline runs real autonomous coding loops, and they fail in specific,
documented ways — read this before pointing it at anything you care about.

- **Local (non-Claude) model dispatch is the weak point.** It works well for
  small, mechanically-scoped stories (one concern, ≤2 production files) and
  degrades sharply on anything bigger: large-file edits, multi-function
  stories, and anchored inserts into long existing functions reliably cause
  step-cap timeouts, stalls, or file corruption from stale line-number edits.
  `docs/plans/*.md` and `retros/*.md` in this repo are the actual incident
  record this finding comes from, not a marketing claim — read a few before
  trusting local dispatch on anything non-trivial. `PIPELINE_BACKEND_DISPATCH=auto`
  exists specifically to escalate a struggling local attempt to Claude rather
  than let it loop.
- **A green test suite is not proof of a correct or complete change.** An
  executor (local or Claude) converges to the minimum diff that turns its own
  tests green, and can write a self-consistently wrong test that encodes the
  same bug as its implementation. See CLAUDE.md's ["Merge-gate and AI-review
  lessons"](CLAUDE.md) section — every lesson there came from a real merged
  regression, not a hypothetical.
- **A story marked `done` is not proof its title's full scope shipped.** A
  "migrate everything" or "remove all X" story can pass review and merge
  having only done part of the job, because review grades the story's own
  tests, not the title's claim. See `.claude/rules/agent-dispatch-story-sizing.md`.
- **The overlord's `park-and-ping` tier is a real safety floor, not a
  suggestion** — high-risk decisions (irreversible actions, auth/security,
  money, production config, breaking changes) always stop for a human,
  regardless of autonomy level. Start any new deployment at
  `PIPELINE_AUTONOMY=dry-run` and read the decisions log before trusting
  `gated` or `full`.
- **This is a single-maintainer research project**, not a maintained product
  with an SLA. The test suite and CI are real gates, but expect rough edges,
  and expect the failure-mode catalog to keep growing as new ones are found.

If you hit a new failure mode, it's worth documenting (see `retros/` for the
existing format) rather than working around it silently — the whole value of
this project's design is that failure modes get named and fed back into how
stories are sized and reviewed.

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE) and
[NOTICE](NOTICE).

