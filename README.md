# Autonomous SDLC Agent Pipeline

[![CI](https://github.com/fico-jessecarroll/claude-pipeline-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/fico-jessecarroll/claude-pipeline-mcp/actions/workflows/ci.yml)

This is a quickstart guide for the Autonomous SDLC Agent Pipeline, describing the system components and how they interact. For detailed reference material, see [REFERENCE.md](./REFERENCE.md).

## Components at a glance

| Piece | Location | Role |
|---|---|---|
| Persona subagents | `~/.claude/agents/*.md` | The SDLC roles agents play |
| Decision policy | `~/.claude/overlord-policy.md` | How the overlord decides |
| Pipeline MCP server | `app/pipeline_mcp_server.py` | All pipeline tools + orchestration |
| Backend seam | `app/backend.py` | Per-role driver routing (`claude` / `ollama` / `lmstudio` / `mlx` / `local`); single-shot, review, dispatch, resource gate |
| Local agent loop | `scripts/local_agent.py` | Native-tool-calling write loop for local dispatch (subprocess) |
| Monitoring dashboard | `app/dashboard.py`, `static/` | Read-only FastAPI status/lifecycle viewer |
| Install / deps | `scripts/install.sh`, `requirements*.txt` | venv + dependency setup |
| Tests | `tests/unit/test_pipeline_mcp_server.py`, `tests/unit/test_backend.py`, `tests/unit/test_dashboard.py` | `pytest`, run via the venv |
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

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE) and
[NOTICE](NOTICE).

## Scheduler

The **advance-scheduler** is now a long‑lived daemon rather than a 60s launchd tick. Launchd now only crash‑restarts the daemon via KeepAlive.

### Environment Variables
- **PIPELINE_SCHEDULER_INTERVAL_S** – default reconcile sweep interval (default 60 seconds).
- **PIPELINE_SCHEDULER_HEALTH_PATH** – optional path where the daemon writes its health JSON each iteration.
