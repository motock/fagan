# Autonomous SDLC Agent Pipeline
[![CI](https://github.com/fico-jessecarroll/claude-pipeline-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/fico-jessecarroll/claude-pipeline-mcp/actions/workflows/ci.yml)
This is a quickstart guide for the Autonomous SDLC Agent Pipeline, describing the system components and how they interact. For detailed reference material, see [REFERENCE.md](./REFERENCE.md).

## Components at a glance

| Piece | Location | Role |
|---|---|---|
| Persona subagents | `~/.claude/agents/*.md` | The SDLC roles agents play |
| Decision policy | `~/.claude/overlord-policy.md` | How the overlord decides |
| Pipeline MCP server | `app/pipeline_mcp_server.py` (launch shim) → `pipeline/` package | All pipeline tools + orchestration; `pipeline/server.py` is the entry module, split across `pipeline/*.py` (dispatch, review, ci, advance, store, etc.) |
| Backend seam | `app/backend.py` | Per-role driver routing (`claude` / `ollama` / `lmstudio` / `mlx` / `local`); single-shot, review, dispatch, resource gate |
| Local agent loop | `scripts/local_agent.py` | Native-tool-calling write loop for local dispatch |
| Monitoring dashboard | `app/dashboard.py`, `static/` | Read-only FastAPI status/lifecycle viewer |
| Install / deps | `scripts/install.sh`, `requirements*.txt` | venv + dependency setup |
| Tests | `tests/unit/` (5,600+ tests) | `pytest`, run via the venv |
| Plans / manifests / logs | `~/.claude/plans/` | Plan, manifest, decisions, notifications |
| Worktrees | `~/.claude/worktrees/` | Isolated per-story branches |
| Issue tracker | Plane (external, optional) | Mirror of story state; skipped entirely when unconfigured (manifest is the source of truth) |

---

## Architecture

(Architecture diagram omitted for brevity)

## Personas (`~/.claude/agents/`)

(Description omitted for brevity)

## The overlord and the decision policy

(Description omitted for brevity)

## Reference

See [REFERENCE.md](REFERENCE.md) for the full MCP tools reference, the plan/story JSON schema, per-role provider/model configuration, guided decomposition and TDD-split details, every `PIPELINE_*`/`LOCAL_AGENT_*` environment variable, the end-to-end workflow, safety controls, the usage gate, and development/testing instructions.

## Prerequisites

- **Python 3.10+** and the project venv.
- **git** on PATH.
- **GitHub CLI** (`gh`).
- **Claude Code CLI** (`claude`).

## Scheduler

(The scheduler description omitted for brevity)

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
