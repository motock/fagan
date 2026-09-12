# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This is a single-maintainer research project rather than a maintained product
with an SLA - see the README's "Reliability & limitations" for what that means
in practice.

## [0.2.0] - 2026-09-12

### Added

**Streaming chat**

- Streaming chat over SSE: `POST /api/chat/stream` backed by a `stream_turn`
  event generator, with the dashboard consuming the stream for incremental
  tool activity (#685, #686, #689, #695, #707).

**Per-backend usage reporting**

- Per-backend usage reporting: `collect_backend_status` surfaces
  `resource_status` per configured role, served by the dashboard usage
  endpoint and rendered as one row per backend in the usage banner
  (#688, #693, #694).

**Plan-completion notifications**

- Plan-completion notifications: plan completion is detected exactly once and
  emitted as a `plan_completed` notification, spooled to a per-plan outbox and
  delivered over SMTP with credential handling and redaction; the scheduler
  tick drains the outbox (#710, #713, #714, #715, #716, #717, #718).

**Canonical agent.log grammar**

- Canonical agent.log line grammar: a single `agent_log_format` module defines
  the grammar, `local_agent` delegates to it, and a Claude stream-json
  translator feeds ClaudeCliDriver dispatch while raw NDJSON is kept in a
  sidecar (#687, #690, #691, #692, #699).

**Process**

- Started an ADR practice: `docs/adr/` with an index, a template and the first
  four decision records (#682).
- Introduced this CHANGELOG (#681).

### Fixed

**Plan-lock starvation**

- Merge adjudication moved to the start of the tick (#704), and the plan lock
  is now released around `dispatch_story` and `review_story`, guarded by a
  dispatch lease so a released lock cannot double-dispatch (#697, #698, #709,
  #712); per-plan per-tick dispatch is capped (#708).
- The `.agent_done` marker is claimed before its event is published and is
  written as soon as `finish_if_green` decides the run is finished (#703,
  #705).
- `reconcile_fn` gained the same bounded join watchdog `scan_fn` already has
  (#700).
- The daemon exits for a launchd restart after repeated abandoned watchdog
  workers, since a leaked plan lock is only released by process death (#706).

**Watchdog**

- The stale-activity watchdog floors the activity age at the current
  dispatch's elapsed time, so a redispatch into a reused worktree is not
  killed at elapsed 0s (#711), and records the last agent.log line and the
  branch commit count when it kills a dispatch (#702).

**Done-bar**

- The done-bar no longer narrows to a story's own new test file (cf48767),
  guarded by `tests/unit/test_done_bar_not_narrowed.py`.

## [0.1.0] - 2026-09-11

First public release.

Fagan splits software engineering along the line where model cost actually
differs: a frontier model decomposes the work, plans it, reviews the diff and
adjudicates anything risky, while a local model writes the implementation at no
marginal cost. What makes the cheap half trustworthy is inspection.

### Added

**Orchestration**

- MCP server exposing the pipeline to any MCP client (plan ingest, story
  dispatch, review, advance, merge approval, decisions).
- Web dashboard with a kanban board, per-story replay timeline, effective-config
  view and workspace picker - the pipeline can be driven entirely from the UI
  with no MCP client registered.
- Scheduler daemon that advances ready stories unattended, with per-plan locking
  and a wedged-story detector.

**Cost-tiered execution**

- Per-role provider/model routing: dispatch, review, planner, overlord,
  decompose, test_author, diagnosis, chat and security each resolve
  independently.
- Backends: `claude` (Claude Code CLI), `ollama`, `lmstudio`, `mlx` and
  `litellm`, plus an `aider` harness adapter.
- Escalation from a struggling local attempt to a stronger model, with
  spend-aware usage gating that pauses work near configured thresholds.

**Quality gates**

- TDD enforced before implementation, including an optional test-author phase
  that writes the failing suite before a weak executor touches code.
- Independent review gate returning APPROVE / REQUEST_CHANGES with bounded
  rework cycles.
- Acceptance-oracle grading against read-only fixtures materialized into the
  worktree.
- Risk-tiered overlord that stops for a human on anything irreversible,
  regardless of autonomy level.
- Merge gate that rebases onto the default branch, polls real CI and re-runs the
  suite before merging.

**Isolation**

- One git worktree per story, with runtime artifacts excluded from tracking.
- Optional Docker sandbox per worktree (ships disabled, fails closed).
- Optional remote execution over SSH.

**Operations**

- Standalone mode (`scripts/standalone-setup.sh up`) provisioning a scratch data
  dir, personas, dashboard and scheduler in one command.
- Runtime preflight reporting resolved configuration and flagging disabled
  safety gates.
- Configuration provenance showing where each setting was resolved from.
- CI on Linux (Python 3.12/3.13/3.14) and macOS.

### Known limitations

Documented rather than hidden - see the README's "Reliability & limitations"
and `retros/` for the incident record these came from:

- Local (non-Claude) dispatch degrades sharply on large or multi-concern
  stories.
- A green test suite is not proof of a correct or complete change.
- A story marked `done` is not proof its title's full scope shipped.

[0.2.0]: https://github.com/motock/fagan/releases/tag/v0.2.0
[0.1.0]: https://github.com/motock/fagan/releases/tag/v0.1.0
