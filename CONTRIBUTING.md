# Contributing

Thanks for looking at this project. It's a single-maintainer research
project (see the README's
[Reliability & limitations](README.md#reliability--limitations)), so please
open an issue to discuss anything non-trivial before investing time in a PR —
there's no roadmap review process beyond that.

## Contribution licensing

By submitting a contribution to this repository you grant the project
owner (Jesse Carroll) a perpetual, worldwide, non-exclusive, royalty-free
right, with the right to sublicense, to use, reproduce, modify, distribute
and otherwise exploit your contribution under any license the owner
chooses — including this repository's AGPL-3.0-only license and the
commercial terms of Fagan Enterprise. Contributions that do not carry this
grant cannot be merged. The project currently contains no third-party
contributions; this policy exists to keep that sustainable.

## Ground rules

This repo's own engineering standard is [`CLAUDE.md`](CLAUDE.md) — it governs
how work is done here (code quality, testing, security, commit format,
branching, code review) whether you're a human or an AI assistant. Read it;
a PR that doesn't meet its bar (tests including negative/boundary cases, no
unrequested refactors, Conventional Commits, etc.) will get review comments
asking for the gap to be closed rather than a quick merge.

## Setup

```bash
git clone https://github.com/motock/fagan.git
cd fagan
scripts/install.sh --dev   # venv + requirements-dev.txt (adds pytest, ruff)
```

## Before opening a PR

```bash
.venv/bin/python -m pytest -q       # full suite must pass
.venv/bin/python -m ruff check .    # must be clean
.venv/bin/python -m mypy            # must be clean
```

mypy checks `pipeline/` and `app/`. Every module in `pipeline` and `app` is checked; there is no exemption list, so fix new errors rather than ignoring a module.

Ruff enforces a function complexity ceiling of 30 (rule `C901`, `max-complexity` in `pyproject.toml`). The ceiling measures the function itself, so moving a large function into its own file does not satisfy it. Files that already exceed it are listed under `[tool.ruff.lint.per-file-ignores]`; refactor a listed file and remove it from that list once its functions are under 30, but never add a file to the list.

No function in `pipeline/`, `app/` or `scripts/` may be longer than 150 lines; the functions already over that are a baseline in `tests/unit/test_function_length_ceiling.py` that may only shrink (remove an entry when you split its function, never add one). Check with `.venv/bin/python -m pytest -q tests/unit/test_function_length_ceiling.py`.

The suite runs in parallel by default (pytest-xdist's `-n auto`, set in `pyproject.toml`'s
`addopts`) — it fans out across your machine's cores automatically, no flag needed.

To see which lines the suite does not exercise (optional, not a merge gate):

```bash
.venv/bin/python -m pytest -q --cov=pipeline --cov=app --cov=scripts \
    --cov-report=term-missing:skip-covered
```

CI builds the same XML on one matrix leg (`ubuntu-latest`, Python 3.14) and
uploads it as the `coverage-xml` artifact on every `ci.yml` run: open the
run's "Artifacts" section and download `coverage.xml`. The report is not a
gate, so no coverage threshold applies and a drop never blocks a merge.

- Keep PRs focused — one concern per PR, under ~400 lines of diff as a
  guideline (see CLAUDE.md's "Pull request size"). A large change is better
  submitted as a sequence of smaller reviewable PRs than one big one.
- Follow strict TDD for behavior changes: a failing test first, then the
  minimum implementation to pass it. For bug fixes, the regression test must
  reproduce the bug (observed failing) before the fix lands.
- Don't modify an existing test without stating, in the PR description, which
  assertion changed, why the old expectation is no longer correct, and what
  the new expected behavior is (CLAUDE.md Step 4). An unexplained loosened or
  deleted assertion is a blocking review finding by default.
- Use [Conventional Commits](https://www.conventionalcommits.org) for commit
  messages (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`, `ci:`,
  `perf:`).
- If your change alters externally visible behavior (an MCP tool's
  contract, an environment variable, the dashboard's API, a CLI flag),
  update `README.md`/`REFERENCE.md` in the same PR.

### Broad exception handlers

Ruff selects `BLE001`, so a handler that catches `Exception` or `BaseException` without re-raising or logging the traceback fails lint. Where the broad catch is intended, write `# noqa: BLE001 - <reason>` with at least three words saying what must not be interrupted and where the failure is reported; `tests/unit/test_broad_except_reasons.py` rejects a bare or too-short reason in production code. Narrowing the handler to the exceptions that can actually occur is preferred over a `noqa`.

## Test seams

Tests reach production code through shortcuts that make refactors expensive: patching names on `pipeline.server`, production code routing reads back through that module so the patches land, and `inspect.getsource` assertions on source text.

- Patch a name on the module that owns it, never on `pipeline.server`.
- Assert behaviour, not source text.
- The baseline only shrinks: `tests/unit/test_seam_baseline.py` fails when a count grows or a new key appears, and when a count drops without the baseline being updated. Inspect with `python scripts/test_seam_inventory.py`; after removing seams, run it with `--write` and commit the new `tests/unit/test_seam_baseline.json`.

## Reporting bugs / requesting features

Open a GitHub issue. For a suspected security vulnerability, see
[`SECURITY.md`](SECURITY.md) instead — please don't file those publicly.

## Code of conduct

Be respectful and constructive. Disagreements about approach are fine and
expected; personal attacks aren't.
