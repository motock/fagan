## What changed

<!-- One or two sentences: what this PR does and why. -->

## Checklist

Per [CONTRIBUTING.md](../CONTRIBUTING.md) / [CLAUDE.md](../CLAUDE.md):

- [ ] Followed TDD: a failing test existed before the implementation (for bug
      fixes, the test reproduces the bug and was observed failing first).
- [ ] `.venv/bin/python -m pytest -q` passes locally.
- [ ] `.venv/bin/python -m ruff check .` is clean.
- [ ] One concern per PR, ideally under ~400 lines of diff.
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org).
- [ ] If this modifies or removes an existing test, this description states
      which assertion changed, why the old expectation is no longer correct,
      and what the new expected behavior is (unexplained changes are a
      blocking review finding by default).
- [ ] Docs (`README.md` / `REFERENCE.md`) updated if this changes externally
      visible behavior (an MCP tool's contract, an env var, the dashboard
      API, a CLI flag).

## Related issues

<!-- Closes #123, relates to #456, etc. -->
