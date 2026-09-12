# Releasing

How a release is cut in this repo. This is a manual procedure, written down so
the next release does not depend on anyone's memory. There is no release
automation: the only GitHub Actions workflow is `ci.yml`, and releases are made
by hand with `git` and `gh`.

Like the project itself, this is a single-maintainer research project rather
than a maintained product with an SLA: releases happen when there is something
worth releasing, and there is no promised cadence and no support policy. What
follows is simply the procedure that produced `v0.1.0`, written down so it can
be repeated.

## Versioning

Versions are semver-shaped `0.x.y` strings: `MAJOR.MINOR.PATCH`, tagged as
`vX.Y.Z`. The project is pre-1.0, so the major version stays at `0` and the
meaning of the other two numbers is what matters:

- **Minor bump** (`0.1.0` -> `0.2.0`): new user-visible capability — a new
  component, a new tool or dashboard surface, a new documented workflow. If a
  reader of the README would have to learn something new, it is a minor bump.
- **Patch bump** (`0.2.0` -> `0.2.1`): fixes and internal changes with no new
  user-visible surface — bug fixes, test and CI repairs, doc corrections,
  dependency bumps.

There is no `BREAKING` convention at 0.x: if something changes in a way that
breaks an documented workflow, say so in the changelog entry rather than
inventing a new version scheme.

## Pre-flight checks

Work through this checklist before touching the tag. All four must be true.

- [ ] **master CI is green.** Run `gh run list --branch master --limit 1` and
      confirm the most recent run on `master` shows `completed` / `success` —
      not merely absent, not queued, not in progress. CLAUDE.md's
      **Definition of Done** is explicit that a local test run is not a
      substitute for confirmed-green CI; the same standard applies to a
      release. If the last run is stale (an older commit), push nothing and
      wait for the run on the current `master` HEAD to finish.
- [ ] **The working tree is clean.** `git status --short` prints nothing. A
      dirty tree means the tag could describe a state that was never committed.
- [ ] **No plan has open stories.** A story mid-flight means a feature
      straddles the tag: part of it is in the release and part of it is not.
      Close or defer the open stories first, then cut the release from a
      quiet tree.
- [ ] **The full suite passes locally.** Run `pytest` and confirm zero
      failures. This is necessary but not sufficient — see the CI check above.

## Writing the changelog entry

Add a new `## [X.Y.Z]` section to `CHANGELOG.md` **above** the previous
version's section — the file is newest-first (Keep a Changelog format), so the
top of the file is always the unreleased/most recent version. Date it with the
release date, not the day you started the work.

What goes where:

- **Added** — new user-visible capabilities: new components, new scripts, new
  dashboard or MCP surfaces, new documentation.
- **Fixed** — corrections to behaviour that already shipped: bugs, broken
  paths, regressions.
- **Changed** — behaviour that still exists but works differently now:
  renamed flags, moved defaults, revised defaults or formats.

Anything internal-only (test refactors, CI tweaks) can go under **Changed**
or be left out; the changelog is for readers of the project, not a commit log.

## Tagging and publishing

From a clean tree on `master`, with the changelog entry committed and pushed:

```bash
git tag -a vX.Y.Z -m "<short description>"
git push origin vX.Y.Z
gh release create vX.Y.Z --title "vX.Y.Z - <title>" --notes-file <notes>
```

- The tag is **annotated** (`-a`), with a short description of the release as
  the message.
- Write the release notes to a temporary file and pass it with `--notes-file`;
  the notes usually start from the same text as the `CHANGELOG.md` section for
  this version.

## After the release

Two checks, then you are done:

- **The release renders on GitHub.** Open the releases page and confirm the
  new release appears with the title, notes and tag you intended, and that the
  notes render as markdown (no mangled code blocks or unescaped characters).
- **The tag is on master.** `git tag --contains vX.Y.Z` from a fresh
  `git pull`, or simply check that `git rev-parse vX.Y.Z` matches
  `git rev-parse master`. A tag that points at a commit that is not on
  `master` means the release was cut from the wrong tree.