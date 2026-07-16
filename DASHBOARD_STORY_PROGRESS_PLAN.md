# Plan — Story-Level Progress in the Dashboard

**Status:** Scoped, not started. Written 2026-07-15. Blocked on
`GUIDED_DECOMPOSITION_PLAN.md`'s ship/kill decision for Tier 1 (see §4).

**One-line goal:** when a story is `in_progress`, let the dashboard show *how far
into that attempt* the agent is — not just "in progress" with no further signal.

---

## 1. Background

Raised while discussing the fleet-wide/per-plan status breakdown bar
(`dashboard.py:_status_counts`, `renderOverview` in `static/app.js`): that shows
progress *across stories in a plan*, not progress *within one story's current
attempt*. This plan is scoped to the latter.

What already exists and is reused, not rebuilt:

- **Checkpoint journal** (`dashboard.py:_read_journal`, rendered as a timeline in
  the story modal) — free-text `step`/`summary`/`next_hint` entries. Qualitative,
  no fixed total.
- **Log tail** (`dashboard.py:_read_story_log`) — raw dispatch output,
  contain-checked under `PLAN_DIR`. For local-backend stories this includes
  `[step N] ...` markers up to `PIPELINE_LOCAL_MAX_STEPS` (default 40, see
  `backend.py:472`).
- **Guided decomposition** (`GUIDED_DECOMPOSITION_PLAN.md`, `PIPELINE_DECOMPOSE`
  flag, `pipeline_mcp_server.py:2749-2798`) — when on, a tech-lead planner writes
  a fixed 3-7 item numbered checklist to `.agent_plan.md` in the story's
  **worktree** (`WORKTREE_ROOT / story_key`, default `~/.claude/worktrees`, see
  `pipeline_mcp_server.py:65,2642`), and the executor is told to keep
  `.agent_scratchpad.md` updated with what's done and what's next. Both files are
  git-excluded and currently **invisible to the dashboard** — it only ever reads
  under `PLAN_DIR`, never the worktree.

## 2. Design — two independent tiers

### Tier 0 — surface the checklist/scratchpad read-only (no % yet)

No dependency on the live experiment; ships independently.

- New `dashboard.py` helper `_read_worktree_file(story, filename)` mirroring
  `_read_story_log`'s containment pattern: resolve `story["worktree"]` from the
  manifest, contain-check the resolved path is strictly under `WORKTREE_ROOT`
  (new env var read in `dashboard.py`, same default as `pipeline_mcp_server.py`),
  degrade to `{"available": false}` on any missing/OSError/outside-root case —
  never 500. A worktree is routinely deleted after merge/cleanup, so "gone" is a
  normal state, not an error.
- New endpoint: `GET /api/plans/{plan}/stories/{story}/plan` → `.agent_plan.md` +
  `.agent_scratchpad.md` content (or `available:false` for stories not run under
  `PIPELINE_DECOMPOSE`, i.e. almost all stories today).
- Frontend: a "Checklist" section in the story modal (same pattern as the
  existing Journal/Log tabs), showing the numbered plan and the current
  scratchpad text. Empty state: nothing rendered (most stories won't have this).

### Tier 1 — a real percentage, once the experiment ships

`GUIDED_DECOMPOSITION_PLAN.md` is still mid-experiment (H3 untested, breadth-heavy
task not yet run, §4.5 ship/kill call not made as of 2026-07-15). Its scratchpad
instruction ("keep a short running summary... which step is next") is
intentionally free text — tightening it to a machine-parseable field is a prompt
change that would alter what that experiment is measuring.

**Do not implement Tier 1 until `GUIDED_DECOMPOSITION_PLAN.md` records a
ship/kill decision.** Once shipped:

- Amend the scratchpad instruction (`pipeline_mcp_server.py` ~L2786-2792) to
  require a leading machine-parseable line, e.g. `PROGRESS: <done>/<total>`.
- Dashboard: count numbered items in `.agent_plan.md` for `total` (regex
  `^\d+\.`), parse `PROGRESS:` out of the scratchpad tail for `done`. Render as
  an actual `N/total · X%` bar on the card face (not just inside the modal),
  reusing the `--c-in_progress` styling already defined for the status stripe.
- Fail open: any parse miss (old-format scratchpad, malformed line) falls back to
  Tier 0's plain-text display, never a crash or a bogus percentage.

### Tier 2 — fallback for stories without a checklist (most stories)

Independent of Tiers 0/1; only meaningful for local-backend dispatches, since
Claude's `claude -p --output-format stream-json` loop has no fixed step cap.

- Parse the highest `[step N]` marker out of the existing log tail
  (`_read_story_log`'s output), compare against the story's resolved
  `PIPELINE_LOCAL_MAX_STEPS` (would need to be recorded on the story at dispatch
  time, e.g. `story["max_steps"]`, since the env var can change between
  dispatches).
- Label explicitly as "step budget used," not "% complete" — it measures runway
  consumed, not closeness to done (a story can finish in 6 steps or grind
  through all 40 without landing). Keep this visually distinct from Tier 1's
  checklist percentage so the two aren't conflated.

## 3. Implementation phases (each a pipeline story, TDD, < ~400 LOC)

1. **Tier 0 backend:** `_read_worktree_file` + containment tests (mirror the
   existing `_read_story_log` path-traversal test coverage: outside-root path,
   missing worktree dir, missing file, non-UTF-8 content) + the new endpoint.
2. **Tier 0 frontend:** Checklist section in the story modal, empty-state
   handling, unit tests in `test_dashboard_script.py`-equivalent JS test file.
3. **Tier 2 (independent of Tier 0/1):** `story["max_steps"]` recorded at
   dispatch, log-tail step-marker parsing, "budget used" badge on local-backend
   `in_progress` cards. Ships whenever, doesn't block on the experiment.
4. **Tier 1 (blocked):** only after `GUIDED_DECOMPOSITION_PLAN.md`'s ship/kill
   decision. Scratchpad format change + checklist-length parsing + card-face
   percentage bar.

## 4. Dependencies / blockers

- **Tier 1 is hard-blocked** on `GUIDED_DECOMPOSITION_PLAN.md` §4.5's ship/kill
  call — do not change the scratchpad prompt before then (confounds the running
  experiment).
- Tiers 0 and 2 have no dependency and can be picked up any time.

## 5. Risks / open questions

- Worktree cleanup timing: does `WORKTREE_ROOT / story_key` still exist while a
  story shows `in_progress`/`interrupted` in the UI, or can it be removed out
  from under a dashboard read mid-request? Must degrade to `available:false`,
  not 500 — same discipline as `_read_story_log`'s missing-file handling.
- `WORKTREE_ROOT` is currently only read in `pipeline_mcp_server.py`; `dashboard.py`
  needs its own env read (same default) rather than importing the orchestrator
  module, to keep the dashboard's "never touches pipeline-owned files, read-only
  boundary" property (see `dashboard.py`'s module docstring) intact — it's adding
  a new *read* boundary (worktree), not a new *write* boundary.
- Tier 2's `[step N]` markers are local-agent-loop-specific output formatting
  (`scripts/local_agent.py:696-712`); if that logging format ever changes, the
  regex silently stops matching and the badge should disappear (fail open), not
  show a stale/wrong number.
