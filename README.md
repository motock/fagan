# Autonomous SDLC Agent Pipeline

[![CI](https://github.com/fico-jessecarroll/claude-pipeline-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/fico-jessecarroll/claude-pipeline-mcp/actions/workflows/ci.yml)

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
`claude`-backend dispatch/review (the default) needs no local model — it
shells out to the Claude Code CLI.

```bash
# 1. Clone and install the Python environment
git clone https://github.com/fico-jessecarroll/claude-pipeline-mcp.git
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

**Only using the `claude` backend?** Skip every `PIPELINE_LOCAL_*`,
`PIPELINE_BACKEND_*=ollama/lmstudio/mlx`, and Ollama/MLX/LM Studio setup
entirely — those only matter if you opt a role into local-model dispatch.
See [Minimal configuration](REFERENCE.md#minimal-configuration) for the
handful of variables actually worth setting on day one, versus the ~100 that
exist purely for tuning.

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
      "command": "python",
      "args": ["-m", "pipeline.companion_server"]
    }
  }
}
```

The B5-01/B5-02 specs in `docs/specs/` (the overlord decision path and the
acceptance-oracle pattern) are the adoptable specs this server exports.

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

