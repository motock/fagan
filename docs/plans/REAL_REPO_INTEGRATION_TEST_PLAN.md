# Plan: Real-repo integration test for the E2E harness (MODE-29-REVIEW-STORY-LOCK-GUARD)

**Status:** Planned, not started. Persisted 2026-07-23 for later ingestion (as a
pipeline story, or direct build) — not executed this session.
**Origin:** 2026-07-22/23 MODE-29-REVIEW-STORY-LOCK-GUARD redispatch (see
`project_dispatch_failure_modes.md` Modes 38-39, commits `8cdb620`/`9da5fdd`,
merged PR #160 `42984f2`). The user wants that exact story replayable as a
harness integration test and a cross-model comparison benchmark, not a one-off.

---

## Context

That session spent ~2.5 hours diagnosing and fixing four "garbage-in" gaps in
the pipeline's local-dispatch harness (planner recipe completeness, a
misleading done-rejection message, a live reviewer-persona config drift, and
missing story-authoring guidance), then watched a full unattended run of the
actual MODE-29-REVIEW-STORY-LOCK-GUARD story go from cold dispatch through two
rework cycles to a genuine, independently-verified APPROVE and a real GitHub
merge (PR #160). That run is unusually valuable as a test case: it's real
production code (not a toy), it required the model to survive infra failures
(Ollama timeouts), a repetition/net-progress guard, and a subtle multi-step
correctness bug (FastMCP tool-registry rebinding) that took three attempts to
actually resolve — exactly the kind of scenario that distinguishes a capable
dispatch model from a weak one.

The goal: make this replayable as an integration test for the harness itself,
and as a way to compare different dispatch models on identical, realistic
conditions — not a synthetic kata.

## Why this doesn't fit the existing benchmark harness as-is

`tests/benchmark/harness.py`'s 8 existing tasks all go through
`setup_workspace()`, which builds a tiny synthetic scaffold repo (a
`pyproject.toml` plus a few seed files) — sized for self-contained kata
modules (`ratelimiter.py`, `token_bucket.py`, etc.). `pipeline/server.py` is
not self-contained: it imports `pipeline.config`, `pipeline.persona`,
`backend`, `role_registry`, `fastmcp`, and lives alongside ~90 sibling test
files (1388 tests) it must not break. `run_groundtruth()` also copies a
single impl file into an *empty* scratch dir before grading — that can't
import a file with real package dependencies. So this needs a parallel
driver that reuses the harness's working pieces rather than editing the
existing 1054-line `harness.py` (avoids regression risk to the 8 existing
tasks).

## Confirmed design decisions (from user Q&A, 2026-07-23)

- **Stop point**: run all the way through the harness's existing **local
  merge stub** (`install_merge_stubs` — hermetic, git-only, never touches
  real GitHub) to `done`. No driver changes needed for this:
  `TERMINAL = {"done", "failed", "parked"}` in `harness.py` already includes
  `done`, and `drive()`'s tick loop can be reused completely unmodified —
  merge only fires on a tick where the story is *already* `pr_open` at tick
  start (confirmed by tracing `_advance_pipeline_locked` and the real
  session's own observed status transitions), so a story never merges
  within the same tick it gets reviewed.
- **Base commit**: `9da5fddf96811de958e763ec520656898fe099e6` — the exact
  clean state right after the four garbage-in fixes, right before the
  story's own fix. Every trial starts byte-identical to the real redispatch
  conditions.
- **First validation trial**: `gptoss` (`gpt-oss:20b` via Ollama, from
  `tests/benchmark/models.py`'s existing `MODELS["gptoss"]` config) — same
  model as the real run, directly comparable.

## New files

### 1. `tests/benchmark/tasks/review_story_lock_guard/spec.json`
Fields: `name`, `summary`, `agent_instructions` (the real, already-diagnosed
root-cause brief used for the actual redispatch — copy verbatim; preserved in
the session transcript and the merged PR's story history),
`persona: "software-engineer"`, `risk: "low"`,
`impl_file: "pipeline/server.py"`,
`base_commit: "9da5fddf96811de958e763ec520656898fe099e6"`. Deliberately **no
`acceptance.py`** — mirrors the real story exactly (the model authors its own
tests; the real story ran with `acceptance: []`).

### 2. `tests/benchmark/tasks/review_story_lock_guard/groundtruth.py`
An independent oracle — never trusts the dispatched model's own tests — run
against the final repo state. Encodes exactly what was verified manually
three separate times during the real run and what the real reviewer caught:
- `p.mcp._tool_manager._tools["review_story"].fn is p.review_story` (the lock
  guard actually reaches the registered MCP tool, not a dead-code
  monkeypatch)
- `"advance_pipeline" in p.mcp._tool_manager._tools` (no regression to the
  sibling tool — this is the exact bug the model introduced on its second
  attempt)
- `p.review_story.__doc__` is non-empty (docstring survived onto the real
  wrapper)
- an invalid `plan_name` (e.g. `"../../../../tmp/x"`) raises `ValueError`
  **before** any lock file is created outside `PLAN_DIR`
  (validate-before-lock ordering — the exact path-traversal finding from
  review #1)
- a genuine concurrency check: with the plan's lock file held externally
  (mirroring `test_review_story_lock_guard.py`'s own
  `test_review_story_skips_when_lock_held` pattern — `fcntl.flock` on the
  `.lock` path), `review_story(...)` returns `{"skipped": "locked"}` and
  never invokes the reviewer — the actual Mode 29 race this story exists to
  close

### 3. `tests/benchmark/run_real_repo_task.py`
New driver script. Imports and reuses from `harness.py` without modifying
it: `install_merge_stubs`, `drive`, `TERMINAL`, `VENV_PY`, `_sh`,
`_set_review_backend_env`.

- `setup_real_repo_workspace(cell: Path, base_commit: str) -> dict[str, Path]`:
  `git clone --local` the live pipeline repo (**not** `git worktree add` — a
  full standalone clone shares no worktree registry with whatever session is
  interactively using this same repo, so repeated benchmark trials can never
  interfere with it) into `cell/repo`, then `git checkout <base_commit>`
  inside it. Sets up a local bare `origin` remote and pushes `master` to it
  (same pattern `harness.py`'s `setup_workspace` already uses). Symlinks
  `.venv` to the real pipeline venv so `detect_test_command`/pytest resolve
  correctly.
- Same environment variables `harness.py`'s `main()` sets before importing
  `pipeline_mcp_server`: `PLAN_DIR`, `WORKTREE_ROOT`, `REPO_ROOT`,
  `PIPELINE_AUTONOMY=full`, `PIPELINE_RISK_THRESHOLD=low`,
  `PIPELINE_MAX_CONCURRENT_AGENTS=1`, `_set_review_backend_env()` (defaults
  review to real Claude), plus the chosen model's env block from
  `models.MODELS`.
- Builds the plan directly as a dict (not via `harness.py`'s `build_plan()`,
  which hard-requires an acceptance fixture) — one epic, one story: `key`,
  `summary`, `agent_instructions`, `persona`, `model` tier, `risk`,
  `dependencies: []`, `acceptance: []` — mirroring the real story's shape
  exactly. `p.save_plan(...)` / `p.ingest_plan(...)`.
- Drives with `harness.py`'s `drive()` unmodified.
- Grading runs **in-place** against the final repo directory (not
  `run_groundtruth`'s isolated-scratch-copy, which can't import a real
  multi-file package): writes `groundtruth.py`'s content to a throwaway
  `test_groundtruth_review_story_lock_guard.py` at the repo root, runs
  `{VENV_PY} -m pytest test_groundtruth_review_story_lock_guard.py -q` from
  there, captures pass/fail + tail, then removes the throwaway file.
- Scorecard written to `cell/result.json` and printed to stdout, same shape
  as `harness.py`'s existing `result.json` (`final_status`, `review_verdict`,
  `merged`, `dispatched_model`, `elapsed_s`, `ticks`, `groundtruth_passed`,
  `groundtruth_tail`) plus two additions useful for cross-model comparison:
  a rework/review-cycle count and an infra-failure count, both parsed from
  the story's journal file (`<plan>.<STORY_KEY>.journal.json`).
- CLI surface matches `harness.py`'s `main()`: `--model`, `--trial`,
  `--workdir` (defaulting to `tests/benchmark/_runs/`), `--timeout`,
  `--tick` — so running a second model for comparison is just
  `--model sonnet` etc., using `models.py`'s existing registry unchanged.

### 4. Light unit tests
For the two purely-mechanical new pieces — `setup_real_repo_workspace`
(clones and checks out the right commit, sets up the local origin, symlinks
`.venv`) and the in-place groundtruth runner (reports pass/fail correctly,
cleans up the throwaway file) — against a tiny throwaway git fixture repo,
not the full pipeline repo. Keeps these tests fast and hermetic (no
Ollama/live model dependency); the actual full-flow validation is the real
trial run itself, not a unit test.

## Execution order

1. TDD the two unit-testable pieces (test first, then implement
   `setup_real_repo_workspace` and the in-place groundtruth runner).
2. Write `spec.json` and `groundtruth.py` for the new task.
3. Wire up `run_real_repo_task.py`'s `main()` (CLI, plan build, drive, grade,
   scorecard).
4. Run the full existing test suite
   (`--override-ini=testpaths=. --ignore=tests`) to confirm nothing
   regressed.
5. Run one real trial:
   `python tests/benchmark/run_real_repo_task.py --task review_story_lock_guard --model gptoss --trial 0`.
   This dispatches to real Ollama/Claude and will take real wall-clock time
   (the real live run took ~2.5 hours across 2 rework cycles and 2
   infra-failure resumes; expect a similar order of magnitude). Report the
   resulting scorecard.
6. Commit the new harness files (not the trial's `_runs/` output, which
   follows the existing `.gitignore` convention for `tests/benchmark/_runs/`).

This can be executed either as a direct build (matching this session's
established pattern for harness-improvement work, bypassing the formal
pipeline-story workflow) or ingested as a proper pipeline story via
`ingest_plan`/`save_plan` per `CLAUDE.md`'s Agent Workflow Step 1 — the
`agent_instructions` content above (sections 1-4 plus execution order) is
already written at the right level of detail to serve as a story's
`agent_instructions` field if dispatched that way. Decide which path at
ingestion time.
