# Contributing

Thanks for looking at this project. It's a single-maintainer research
project (see the README's
[Reliability & limitations](README.md#reliability--limitations)), so please
open an issue to discuss anything non-trivial before investing time in a PR —
there's no roadmap review process beyond that.

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
```

The suite runs in parallel by default (pytest-xdist's `-n auto`, set in `pyproject.toml`'s
`addopts`) — it fans out across your machine's cores automatically, no flag needed.

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

## Reporting bugs / requesting features

Open a GitHub issue. For a suspected security vulnerability, see
[`SECURITY.md`](SECURITY.md) instead — please don't file those publicly.

## Code of conduct

Be respectful and constructive. Disagreements about approach are fine and
expected; personal attacks aren't.
