# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This is a single-maintainer research project rather than a maintained product
with an SLA - see the README's "Reliability & limitations" for what that means
in practice.

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

[0.1.0]: https://github.com/motock/fagan/releases/tag/v0.1.0