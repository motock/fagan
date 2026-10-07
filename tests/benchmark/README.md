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

## Tiers

Every task carries a `tier` field. **Tier 1** is a single-file, dependency-free
kata written into an empty repo from scratch (`cron_field`, `interval_merge`,
`lru_cache`, `retry_backoff`, `token_bucket`) — this measures pipeline mechanics
and raw greenfield capability, but nothing about the harder, more common
real-world shape of *changing existing code*. **Tier 2** seeds the repo with an
existing, already-committed codebase (via `tasks/<name>/seed/`, see below) that
the story must modify — currently `ratelimiter_bugfix`: fix a reported bug in an
existing implementation without breaking its existing test suite, following the
same diagnose-then-regression-test-then-fix workflow this repo's own `CLAUDE.md`
prescribes for bug work. **Tier 3** is cross-module: a change touching an API,
its consumer, and documentation together — currently `inventory_pagination`:
turn a non-paginated `list_items(limit=100)` into a cursor-paginated API
(`list_items(cursor=None, limit=50) -> (items, next_cursor)`, limit clamped to
100, `ValueError` on a bad limit or cursor), page through every record in
`report.count_all()` (which silently under-counts the 250-record catalog), and
document the new contract in `README.md` while keeping the seeded test suite
green. Tier 3 tasks declare `extra_impl_files` in `spec.json` so the ground
truth can import every module the agent changed, not just `impl_file`. They
have no `mock_impls.py` entry: the mock backend writes a single `impl_file`
and cannot express a cross-module change, so a Tier 3 cell must run against a
real model.

Tiers exist because Tier 1 alone can be a misleading proxy: a model or pipeline
change that helps on greenfield katas may not transfer to modify-existing-code
work, which is both harder (existing tests must be preserved, not just written)
and closer to what a real project actually needs. Filter a run by tier by
naming tasks explicitly, e.g. `matrix.py --tasks ratelimiter_bugfix --models
gptoss_temp03`. In principle, per-tier results should inform
`PIPELINE_LOCAL_MAX_RISK` for the `auto` dispatch router — "local handles
Tier-1-shaped stories, Claude gets Tier 2+" as a measured policy rather than a
guess — but that wiring doesn't exist yet either.

## Layout

```
tests/benchmark/
  tasks/<name>/
    spec.json        story fed to the pipeline (tier, summary, agent_instructions, persona, model, risk, impl_file, extra_impl_files)
    seed/            Tier 2+ only: existing codebase mirrored into the repo's initial commit
    acceptance.py    hidden oracle materialized read-only into the worktree; the agent must make it pass
    groundtruth.py   investigator-owned, independent; run against the MERGED code (never enters the worktree)
  tasks/inventory_pagination/   Tier 3: cursor pagination across the API, its report consumer and the README
  harness.py         single-cell runner (one task x one model x one trial)
  models.py          model -> environment configs (devstral, minimax, gptoss, gptoss_temp03, gptoss_devstral_review, lmstudio_gemma4, mlx, sonnet, mock)
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
| `gptoss`  | local (Ollama) | free; needs Ollama up. Tag via `BENCH_GPTOSS_TAG` (default `gpt-oss:20b`). Runs at `temperature=1.0`, `num_ctx=32768`. |
| `gptoss_temp03` | local (Ollama) | Same tag/`num_ctx` as `gptoss`, `temperature=0.3` only — the A/B comparison arm. This is the value now baked into `backend.py`'s per-model tuning table for real (non-benchmark) dispatch/review, so a fresh `gptoss` run tests the *old*, superseded setting unless you're deliberately re-verifying temp=1.0. |
| `gptoss_devstral_review` | local (Ollama) | Same dispatch settings as `gptoss_temp03` (gpt-oss:20b, temp=0.3, ctx=32768), but review runs on `devstral:24b` via `PIPELINE_LOCAL_REVIEW_MODEL` instead of gpt-oss reviewing its own work with identical weights (`code-reviewer.md` and `software-engineer.md` both declare `model: sonnet`). Bakes `PIPELINE_BACKEND_REVIEW=local` into the config itself rather than relying on the invoking shell. |
| `lmstudio_gemma4` | local (LM Studio) | `PIPELINE_LOCAL_PROVIDER=lmstudio`, model `google/gemma-4-e4b` by default (`BENCH_LMSTUDIO_TAG`). Requires `lms server start` running with the model already downloaded (`lms ps`) at `BENCH_LMSTUDIO_ENDPOINT` (default `http://localhost:1234`) — the harness does not start it for you. |
| `mlx`     | local (mlx_lm.server) | `PIPELINE_LOCAL_PROVIDER=mlx`, tag via `BENCH_MLX_TAG` (default is a tiny 1.5B model that validated the wire protocol but did not converge in a review loop — expect weak signal until you point it at a larger MLX-served model). Requires `mlx_lm.server` already running at `BENCH_MLX_ENDPOINT` (default `http://localhost:8080`). |
| `sonnet`  | cloud (claude) | consumes Claude usage. Arm A of the published matrix: Claude implements and Claude reviews — `role_config` pins every role (planner, dispatch, test_author, review, overlord) to claude/sonnet. |
| `glm_claude_review` | cloud (Ollama) | Arm B of the published matrix: `glm-5.3-flash:cloud` implements (`temperature=0.3`, `num_ctx=32768`); Claude (sonnet) plays every other role — planner, test_author, review and overlord — via `role_config`. 1800s dispatch timeout. |
| `gptoss_claude_review_s60` | local (Ollama) | Arm C at the shipped 60-step budget: `gpt-oss:20b` implements (`temperature=0.3`, `num_ctx=32768`); Claude (sonnet) plays every other role — planner, test_author, review and overlord — via `role_config`. 1800s dispatch timeout. |
| `gptoss_claude_review_s120` | local (Ollama) | Arm C at double the step budget (120): identical to `gptoss_claude_review_s60` except `PIPELINE_LOCAL_MAX_STEPS`; Claude (sonnet) plays every other role — planner, test_author, review and overlord — via `role_config`. 1800s dispatch timeout. |
| `mock`    | offline        | writes a known-correct reference impl; for self-testing the harness only. |

`lmstudio_gemma4`/`mlx` dispatch through `inference_providers.py` (see
`MODEL_PROVIDER_ABSTRACTION_PLAN.md`) instead of Ollama's native `/api/chat` —
the wire protocol differs (OpenAI-compatible `/v1/chat/completions`, blocking
rather than streamed) but the harness mechanics (dispatch loop, tool set,
guards) are identical.

By **default** the review gate runs on the cloud Claude reviewer
(`PIPELINE_BACKEND_REVIEW=claude`), regardless of which model implemented — so
even local-model cells consume a small amount of usage at the review step.
Setting `PIPELINE_BACKEND_REVIEW` (e.g. to `local`) in the invoking shell
before running `matrix.py`/`harness.py` overrides this for that run. Doing so
trades review quality for avoiding Claude usage — see REVIEW-LOCAL-FALLBACK.

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
- `--timeout S` — per-cell wall-clock budget (default 3600s).
- `--tick S` — seconds between `advance_pipeline` ticks (default 10).
- `BENCH_DEVSTRAL_TAG`, `BENCH_MINIMAX_TAG`, `BENCH_OLLAMA_ENDPOINT` — override
  local model tags/endpoint to match `ollama list`.
- `BENCH_LMSTUDIO_TAG`, `BENCH_LMSTUDIO_ENDPOINT` — override the `lmstudio_gemma4`
  cell's model id/endpoint to match `lms ps`.
- `BENCH_MLX_TAG`, `BENCH_MLX_ENDPOINT` — override the `mlx` cell's model
  id/endpoint to match whatever `mlx_lm.server` is currently serving.

## Published run

`run_published_matrix.sh` is the one-command entry point for the published
benchmark grid. It fixes the task x arm grid in one place and runs it in two
phases:

```bash
# From anywhere; DIR is created if missing.
tests/benchmark/run_published_matrix.sh --workdir _runs/published

# Print the two commands without running anything (run_meta.json is still written):
tests/benchmark/run_published_matrix.sh --dry-run --workdir _runs/published
```

- **main** — the main arms (`sonnet`, `glm_claude_review`,
  `gptoss_claude_review_s60`) over every published task, 3 trials each, into
  `DIR/main`.
- **steps** — the step arm (`gptoss_claude_review_s120`) over the same tasks
  into `DIR/steps`.

Both phases pass `--resume`, so an **infra-skipped** cell (Ollama down, a
timeout, a transient network failure) is re-run by simply repeating the same
command — completed cells are skipped and only the missing ones run.

Before anything runs, the script writes `DIR/run_meta.json` recording the
provenance of the run: `repo_sha` (`git rev-parse HEAD`), `repo_dirty`
(`git status --porcelain`), `started_utc`, the task/arm grid, `trials`, and the
model registry the pipeline will read — `registry.path` is
`$PIPELINE_MODEL_REGISTRY_PATH` when set and non-empty, else the repo's
`model_registry.json`, and `registry.sha256` is that file's digest (`null` if
the file does not exist). The registry matters because the arm friendly names
(`glm`, `gpt-oss`) resolve against it, so a run is only reproducible if the
registry contents are pinned alongside the repo sha.

The script uses `<repo>/.venv/bin/python` (override with `PY`) because
`matrix.py` imports the app package; it falls back to `python3` only when that
venv is absent.

### Verify the run afterwards

Confirm that every cell actually dispatched the model its arm names — an arm
that silently ran another model is the failure this check exists to catch:

```bash
jq -r '[.model,.dispatched_model]|@tsv' DIR/*/*/result.json | sort | uniq -c
```

Each line should pair an arm with the model that arm pins; a mismatch (or a
`dispatched_model` that is empty) means the cell's result is not attributable
to the arm and must not be published.

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
   - `spec.json` — must include `name`, `tier` (`"T1"` for a greenfield kata,
     `"T2"` for modify-existing-code), `summary`, `impl_file`,
     `agent_instructions`, and `persona`/`model`/`risk`. Keep the API in
     `agent_instructions` identical to what both test files import.
   - `acceptance.py` — the oracle the agent is graded on inside the pipeline.
   - `groundtruth.py` — an **independently authored** suite that imports the same
     `impl_file` module. Make it harder than the acceptance oracle (more edge and
     negative cases).
2. **Tier 2+ only:** add `tasks/<name>/seed/`, a real directory tree (not
   JSON-embedded strings) mirrored verbatim into the repo and folded into the
   same initial commit `setup_workspace` creates — so the dispatched agent's
   first `git log`/`git status` sees one clean commit containing a pre-existing
   codebase, not a suspicious second "seed" commit or a dirty tree. Include
   both the existing implementation AND its existing, currently-passing test
   suite; the story's `agent_instructions` must explicitly say those existing
   tests must still pass and must not be rewritten or deleted — an agent
   given free rein over a repo with pre-existing tests will sometimes "fix"
   a failure by gutting the test rather than the implementation.
   Verify the seeded test suite genuinely passes against the seeded
   (deliberately buggy, for a bugfix task) implementation before committing —
   an already-failing seed doesn't test "preserve existing behavior" at all.
3. Verify both suites agree on a correct reference implementation before
   committing — a buggy oracle invalidates every run that uses it. Both suites
   must cover, at minimum: a no-mutation test for any function taking a
   mutable argument (list/dict/set), and a negative/boundary test for every
   input the spec's `agent_instructions` declares as validated. A gap here lets
   a subtly wrong implementation merge clean (see FM-G/FM-F in FINDINGS.md).
4. For the offline `mock` self-test to cover the new task, add a correct
   reference implementation to `_MOCK_IMPLS` in `harness.py`.

## Self-tests

```bash
../../.venv/bin/python -m pytest tests/benchmark/test_harness_selftest.py -q
```

These run fully offline and assert (a) the harness drives a correct impl to
`done` with ground-truth passing, and (b) an impl that passes the visible
acceptance oracle but is actually wrong is still caught by the ground-truth.
