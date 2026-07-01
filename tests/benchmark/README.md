# Pipeline Benchmark Suite

Repeatable, real-world tests that drive tasks through the **full pipeline**
(`ingest → dispatch → check_story_status → review → advance/merge`) and compare
how different local and cloud models fare. The goal is to measure how close the
pipeline is to completing a plan **autonomously, without human intervention** —
and to give a fixed yardstick for pipeline and model improvements.

## What it measures

Each *cell* = one `(task, model, trial)`. A cell runs a single algorithmic story
through the real pipeline inside a throwaway, fully isolated workspace, then
grades the result two ways:

- **Did the plan complete?** — the story reached `done` (dispatched, tests
  passed, review APPROVED, merged).
- **Is the code actually correct?** — an **independent ground-truth** test suite
  (`groundtruth.py`), which the implementing model never sees, passes against the
  merged code.

A cell is a **success** only when *both* hold. The split matters: a cell that
merges but fails ground-truth is the pipeline **landing wrong code** — the most
important failure mode to surface (reported as `merged-but-wrong`).

Why an independent ground-truth? A model can write an implementation that passes
its *own* tests (or the visible acceptance oracle) while still being wrong.
Grading on a separate, hidden suite is what catches that — see the self-test
`test_groundtruth_catches_oracle_gaming_impl`.

## Layout

```
tests/benchmark/
  tasks/<name>/
    spec.json        story fed to the pipeline (summary, agent_instructions, persona, model, risk, impl_file)
    acceptance.py    hidden oracle materialized read-only into the worktree; the agent must make it pass
    groundtruth.py   investigator-owned, independent; run against the MERGED code (never enters the worktree)
  harness.py         single-cell runner (one task x one model x one trial)
  models.py          model -> environment configs (devstral, minimax, sonnet, mock)
  matrix.py          drives the full grid and renders the scorecard
  scorecard.py       aggregates cell results into a markdown comparison table
  test_harness_selftest.py   offline pytest self-tests (no model/network)
  _runs/             generated workspaces + results (gitignored)
```

## Models

| Name      | Backend        | Notes |
|-----------|----------------|-------|
| `devstral`| local (Ollama) | free; needs Ollama up. Tag via `BENCH_DEVSTRAL_TAG`. |
| `minimax` | local (Ollama) | free; needs Ollama up. Tag via `BENCH_MINIMAX_TAG`. |
| `sonnet`  | cloud (claude) | consumes Claude usage. |
| `mock`    | offline        | writes a known-correct reference impl; for self-testing the harness only. |

The **review gate always runs on the cloud Claude reviewer**
(`PIPELINE_BACKEND_REVIEW=claude`), regardless of which model implemented — so
even local-model cells consume a small amount of usage at the review step.

## Running

```bash
PY=../../.venv/bin/python

# One real cell (free, needs Ollama):
$PY harness.py --task token_bucket --model devstral

# Full grid: all tasks x {devstral, minimax, sonnet} x 3 trials (default):
$PY matrix.py

# A cheaper slice:
$PY matrix.py --tasks token_bucket lru_cache --models devstral --trials 1

# Offline plumbing check (no model/network), should be 100%:
$PY matrix.py --models mock --trials 2
```

Outputs land in `--workdir` (default `_runs/`): per-cell `result.json`, an
aggregate `results.json`, and `scorecard.md`.

Cells run **sequentially** by default — local models share one Ollama/GPU, so
`--jobs > 1` only makes sense for an all-cloud (`sonnet`) run.

### Knobs

- `--trials N` — trials per cell; success is reported as a pass-rate, since LLM
  runs are non-deterministic (default 3).
- `--timeout S` — per-cell wall-clock budget (default 1800s).
- `--tick S` — seconds between `advance_pipeline` ticks (default 10).
- `BENCH_DEVSTRAL_TAG`, `BENCH_MINIMAX_TAG`, `BENCH_OLLAMA_ENDPOINT` — override
  local model tags/endpoint to match `ollama list`.

## How isolation works (and why it's safe)

Every cell gets its own directory tree with its own `PLAN_DIR`, `WORKTREE_ROOT`,
a throwaway git repo (the plan's `repo_root`), and a **local bare `origin`
remote**. Because the origin is a real local git remote, every `git`
push/rebase/worktree operation the pipeline performs is genuine — only the three
`gh`-calling seams (`_open_pr`, `_merge_pr`, the CI gate) are stubbed to local
git equivalents, so the full state machine runs to `done` hermetically with no
GitHub artifacts. Nothing here ever touches `~/.claude/plans` or
`~/.claude/worktrees`.

## Adding a task

1. `mkdir tasks/<name>` and add three files:
   - `spec.json` — must include `name`, `summary`, `impl_file`,
     `agent_instructions`, and `persona`/`model`/`risk`. Keep the API in
     `agent_instructions` identical to what both test files import.
   - `acceptance.py` — the oracle the agent is graded on inside the pipeline.
   - `groundtruth.py` — an **independently authored** suite that imports the same
     `impl_file` module. Make it harder than the acceptance oracle (more edge and
     negative cases).
2. Verify both suites agree on a correct reference implementation before
   committing — a buggy oracle invalidates every run that uses it.
3. For the offline `mock` self-test to cover the new task, add a correct
   reference implementation to `_MOCK_IMPLS` in `harness.py`.

## Self-tests

```bash
../../.venv/bin/python -m pytest tests/benchmark/test_harness_selftest.py -q
```

These run fully offline and assert (a) the harness drives a correct impl to
`done` with ground-truth passing, and (b) an impl that passes the visible
acceptance oracle but is actually wrong is still caught by the ground-truth.
