# Wave 1 retrospective — auto-merge off switch, sidebar settings, guided install

Recorded 2026-10-22. Figures are measured from git and the plan manifests
unless marked as an estimate.

## Plans in the wave

| Plan | Stories | Outcome |
|---|---|---|
| `pipeline-auto-merge-off` | 4 | all done and merged |
| `dashboard-sidebar-settings` | 5 | all done and merged |
| `guided-install` | 13 | all done and merged |

## Timeline

- First dispatch: SETUP-01 at 10:19 UTC.
- Last merge: SETUP-12 (`a7d3b5f`) at 14:09 UTC.
- **Wall clock: about 3 h 50 min.**
- The figure includes pauses for status checks and for patching the
  SETUP-03/04 briefs. It excludes the planning, drafting and preflight
  impact runs done beforehand (a few hours of attended work).

## Size

Diff from `eb08922` (0.8.0) to `a7d3b5f`:

- Commits: 24 = 22 story commits + 2 Dependabot bumps (#1183 ruff, #1184 fastapi).
- Files: 56 changed, 35 of them new.
- Lines: +12,014 / −85.

| Area | Files | Lines |
|---|---|---|
| Production (`pipeline/`, `app/`, `static/`, `scripts/`) | 23 | +4,092 / −38 |
| Tests | 28 | +7,833 / −43 |
| Docs (README, REFERENCE, rules) | 3 | +87 / −2 |

## Scope

- **Auto-merge off switch (4):** `PIPELINE_AUTO_MERGE` flag, human gate in the
  merge decision, parked-PR URL in notifications, README/REFERENCE docs.
- **Sidebar settings (5):** SYSTEM settings group in the sidebar, deep-linked
  settings views, styling, Workspace meta refresh fix, dead-code removal.
- **Guided install (13):** declarative config schema with validators; atomic
  env-file read/write; TTY-safe prompts; routing, policy, email and Plane
  configuration; non-destructive Claude Code asset install; idempotent
  orchestrator; `configure` CLI wired into `install.sh`; launchd label fix;
  docs.

## Estimate: seasoned engineer (rough, from line counts, not measured)

- Code, TDD, resolving frozen-pin test conflicts, review and CI cycles:
  about 2–3 weeks (10–15 working days) for one engineer.
- With PR turnaround, 3–4 calendar weeks.
- Roughly 25–40x faster in wall clock; a human waits on checks rather than
  typing.

## Caveats

- Not every story landed first time. Some needed rework, and the
  `pipeline/setup/__init__.py` scope-gate / add-add conflict across
  SETUP-02/03/04 cost extra cycles before the briefs were patched.
- Execution moved from ollama `deepseek-v4.1-flash:cloud` to Claude/sonnet
  partway through (ollama usage limit); SETUP-10's pin was changed to
  `claude` and SETUP-12 ran on `claude`.
- The engineer comparison assumes no review wait and is a judgment call.

## Follow-ups

> **Update 2026-10-22:** `dashboard-worktree-diff`, `dashboard-pin-guard-and-split`
> and `cross-plan-awareness` have since shipped (all stories merged), and
> `fagan-ruff-0-17-target` made the tree lint-clean under ruff 0.17 (now pinned
> at 0.17.1). Only
> `external-context-sources` is still held. The list below is as written.

- Held plans ready to prep/ingest: `dashboard-worktree-diff`,
  `external-context-sources`, `dashboard-pin-guard-and-split`,
  `cross-plan-awareness`. Re-run the preflight impact run first; master moved.
- PIN-2 and XP-3 both edit `.claude/rules/pipeline-story-schema.md`; sequence them.
- `external-context-sources`: CL-14/15/21/22/23/24 still need full impact runs.
- Review the ruff 0.17.1 bump (#1183), since ruff's exit code gates merges.
- Decision record for team deployment: `docs/plans/SHARED_SERVICE_TOPOLOGY_PLAN.md`.
