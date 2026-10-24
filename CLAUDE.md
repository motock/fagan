# Engineering Best Practices

This file governs how AI-assisted development should be conducted in this codebase. It applies to all contributors using Claude Code and serves as a shared reference for engineering standards across teams.

---

## How to use this template

Shared engineering-standards template — fork and adapt for your repo.

**Keep as-is (invariant):** the standards in `.claude/rules/engineering-standards.md` (Core Principles, Code Quality, Testing, Security, Secure by Design, Observability & Logging, Architecture & Design). Weakening these reduces the template's value and cross-team shareability.

**Customize for your context:** Agent Workflow Step 1 (swap in your pipeline tool's primitives; the sequence stays), Step 5 (only if the test command can't be inferred from build files), Commit Standards, and the Definition of Done gates.

**Where content lives:** CLAUDE.md is loaded on every turn, so it holds only what every contributor needs always-on. Everything longer lives in `.claude/rules/*.md` and is read on demand (table below) — **do not use `@` imports for it; an `@` import loads the whole file every turn.** Put new additive sections in a rules file with a provenance note and leave a one-line pointer here.

---

## Local Development Setup

Clone the repo and run `scripts/install.sh --dev` (creates `.venv/`, installs
runtime + dev dependencies, and reports on external tools the pipeline shells
out to). Prerequisites: Python 3.11+ (CI tests 3.11–3.14), `git`, the GitHub
CLI (`gh`, authenticated via `gh auth login`), and the Claude Code CLI
(authenticated). Register the MCP server and copy the persona subagents into
place per the [README Quickstart](README.md#quickstart).

Run the test suite with `.venv/bin/python -m pytest -q` (parallelized via
`pytest-xdist` by default) and `.venv/bin/python -m ruff check .` before
opening a PR — see [CONTRIBUTING.md](CONTRIBUTING.md). No external service is
required for the test suite or a `claude`-backend pipeline; routing a role to
a local model additionally needs Ollama, LM Studio, or MLX running — see the
README's [Provider selection & authorization](README.md#provider-selection--authorization).

---

## Engineering standards (summary)

Full text: `.claude/rules/engineering-standards.md`.

- **Correctness over cleverness; minimal footprint; leave it better; verify before assuming.**
- **Tests:** positive *and* negative (invalid input, missing fields, boundaries, expected exceptions, unauthorized access); one behavioral outcome per test; mock only at external boundaries; API contract tests assert the spec, not the implementation.
- **Security:** validate at boundaries, parameterized queries, no secrets in source, least privilege, deny by default, no sensitive data in logs or errors.
- **Logging:** structured JSON with `timestamp`, `level`, `service`, `correlation_id`, `message`; dynamic values in fields.
- **Architecture:** one responsibility per module; under 1000 lines per file; explicit dependencies.
- **Commits:** Conventional Commits `<type>(<scope>): <subject>`, imperative, ≤72 chars, body wrapped at 72 explaining *why*; `!` plus `BREAKING CHANGE:` footer for breaking changes.
- **Branching:** trunk-based, short-lived branches off `main`, delete after merge.
- **Code review:** default to `Suggestion` when in doubt, reserve `Blocking` for real problems; PRs under ~400 lines, one concern each.

---

## Rules to read on demand

These files are **not** loaded automatically. Read one when the task matches.

| File | Read it when |
|---|---|
| `.claude/rules/engineering-standards.md` | you need the full text of a standard |
| `.claude/rules/pipeline-story-schema.md` | authoring or reviewing a plan / `agent_instructions` |
| `.claude/rules/agent-dispatch-story-sizing.md` | sizing stories for a weaker executor |
| `.claude/rules/local-dispatch-preflight.md` | before ingesting any non-Claude story |
| `.claude/rules/code-review.md` | reviewing a change or modifying an existing test |
| `.claude/rules/testing-config-gates.md` | testing config-driven logic or resource gates |

---

## Agent Workflow

These are mandatory procedural steps that must be followed for every task, in order.

### Step 1 — Claim the work from the pipeline (before any code changes)

**Always drive work through the mcp pipeline tools — never start coding against an ad hoc, untracked request.**

1. **Before authoring or ingesting a plan, establish which implementer strength class the stories will run on** — this determines how aggressively the product-analyst must split stories and how much it must spell out in `agent_instructions` (a rule tuned for Claude is unsafe for a weak local model, and vice versa is needlessly conservative). Resolve it, don't guess:
   - Check, in priority order: a `dispatch` entry in `model_registry.json`'s `roles`, then the `PIPELINE_BACKEND_DISPATCH` env var, then a plan-level `role_config.dispatch` you already know you're going to set. If none are set, dispatch defaults to Claude.
   - State what you found (e.g. "no dispatch override configured — this will run on Claude" or "`PIPELINE_BACKEND_DISPATCH=local` — this targets a local model") and let the user confirm or correct it before you proceed. Treat `auto` as "assume the weak end" — it means dispatch escalates only after a local attempt fails.
   - Map the confirmed class to a strength tier before briefing the product-analyst: **Claude-class** (the ~400-line/split-on-judgment default), **cloud open-source** (as Claude-class, plus grade every mechanically-checkable requirement per .claude/rules/pipeline-story-schema.md), or **local ~20B-class** (≤2 production files per story, rename-and-delegate over in-place re-indent, anchored `str_replace` over line-number edits, one concern per story — see .claude/rules/pipeline-story-schema.md's "Local (non-Claude) dispatch" section in full). For cloud-open-source and local tiers alike, also apply .claude/rules/agent-dispatch-story-sizing.md — the ≤2-file cap alone under-counts stories with too many new functions, an oversized file in scope, an anchored insert into a large function, a fixture-heavy test, or an unenumerated deletion/migration target list.
   - Pass the confirmed tier to the product-analyst explicitly (e.g. "target implementer: local ~20B-class — apply the local-dispatch splitting rules"); it cannot infer this on its own since `_run_decompose`'s single-turn call has no user to ask.
2. If the work hasn't been decomposed into a plan yet, produce epics/stories (typically via the product-analyst agent) and register them with `mcp__pipeline__save_plan` or `mcp__pipeline__ingest_plan`. **The story schema `ingest_plan` actually reads is not what `save_plan`'s tool description implies** — the verified shape, field semantics, and rules for writing `agent_instructions` (the single most influential field on outcome quality) are in .claude/rules/pipeline-story-schema.md. Key points: only `summary` is required; `repo_root` is plan-level and required; do not invent fields like `id`, `title`, `acceptance_criteria`, or `depends_on` (`dependencies` references other stories by exact `summary` text or explicit `key`); `acceptance` is an array of `{path, source}` file fixtures, not a list of strings; when in doubt read an existing `~/.claude/plans/*.json` as ground truth. **Before ingesting, derive each story's `files` from where the code actually lives and check line-limit headroom — see "Declare `files` from where the code actually lives" in .claude/rules/pipeline-story-schema.md.** **For any non-Claude dispatch tier, every story must pass .claude/rules/local-dispatch-preflight.md before `save_plan`/`ingest_plan` — including follow-up stories written mid-incident.** Its impact run (apply a rough stand-in of the change in a scratch worktree and run the full suite) replaces grepping for conflicting tests.
3. Find available work with `mcp__pipeline__list_ready_stories`, or check a specific story with `mcp__pipeline__check_story_status`.
4. Claim the story with `mcp__pipeline__dispatch_story`, then mark it in progress with `mcp__pipeline__mark_story_in_progress`.
5. Branch from the dispatched story ID: `agent/{STORY-ID}` (e.g., `agent/PIPE-123`). The branch name comes from the pipeline, not an invented identifier.
6. If you hit a decision you're not authorized to make alone, raise it with `mcp__pipeline__request_decision` (or check `mcp__pipeline__list_decisions` for precedent) rather than guessing.

**Never use the `Agent` tool to implement pipeline stories.** `mcp__pipeline__dispatch_story` starts a local implementation worker automatically — do not duplicate this by spawning a separate `Agent(subagent_type: "software-engineer")` or any other implementation agent. Doing so runs two workers against the same branch, wastes API quota, and can corrupt the worktree. The `Agent` tool is permitted only for review gates the pipeline does not handle itself (e.g., `security-engineer` for crypto/transport stories).

**If a request doesn't map to a pipeline story:**
- Stop before making any changes.
- Register it as a plan/story via `mcp__pipeline__save_plan` or `mcp__pipeline__ingest_plan` first, or ask the user to do so.
- Do not proceed on work that isn't tracked in the pipeline.

### Step 2 — Diagnose before acting (bug work only)

**For bug fixes, understand the root cause before writing any code or tests.**

1. Read the relevant code paths end-to-end — do not guess at cause from symptoms alone.
2. Identify the exact line or condition responsible for the wrong behavior.
3. Reproduce the failure with a concrete example (a `curl`, a failing assertion, a log line).
4. State the root cause explicitly before proceeding. If you cannot pinpoint it, say so rather than trying a likely-looking fix.

Skip this step for new features where there is no pre-existing defect to diagnose.

### Step 3 — Write tests first (TDD)

Follow strict Test-Driven Development for all feature and bug-fix work:

1. **Understand the requirement** before writing any code. Ask clarifying questions if the behavior is ambiguous.
2. **Write failing tests** that define the expected behavior. Tests should fail at this point — that is correct.
3. **Confirm the tests fail for the right reason** (failing assertion, not a syntax error or import failure).
4. **Write the minimum implementation code** needed to make the tests pass. Do not write more than what the tests require.
5. **Refactor** if needed, keeping all tests green throughout.

Do not write implementation code before the tests exist. If you catch yourself writing implementation first, stop and write the test first.

#### Bug fixes: translate your diagnosis into a test

For bug work, Steps 2 and 3 are a deliberate hand-off. The diagnosis from Step 2 must become a test before any fix is written:

1. **Write a test that reproduces the exact failure identified in Step 2.** The test should call the same code path, with the same inputs that triggered the bug. It must fail — and fail *for the right reason* (the bug, not a missing import or bad test setup).
2. **Do not write the fix yet.** A passing test before the fix means the test is not actually exercising the bug.
3. **Write the minimum fix** needed to make the new test pass while keeping all existing tests green.
4. **The new test is now a permanent regression guard.** It lives alongside the fix and will catch any future reintroduction of the same defect.

This sequence matters: a bug that is fixed without a reproducing test will likely recur. A test written after the fix cannot be trusted — it was never observed failing.

### Step 4 — Existing tests may only be changed when warranted, and the change must be justified

**The rule against touching existing tests exists to stop an implementer from "gaming" a task — loosening or deleting an inconvenient assertion just to make it pass. It is not a blanket ban on ever updating a test.** A test that pins behavior the task is *legitimately* changing (a documented contract change, an intentional bugfix to previously-wrong behavior, a return-shape change the story asks for) may need its assertion updated to match. The bar is: does the modification reflect a real, requested behavior change, or does it just make an inconvenient test stop failing?

Responsibility for telling these apart splits across two roles — neither is sufficient alone:

- **The implementer must justify, not silently edit.** Before modifying an existing test, state explicitly: which test/assertion is changing, why the previously-asserted behavior is no longer correct, and what the new expected behavior is. This justification must be visible in the commit message or PR description — never folded into an unrelated diff. If the task's own scope does not require the behavior change the test would need to reflect, do not touch it; find another way to satisfy the test as written.
- **The reviewer is the actual gate.** Every existing-test modification is a mandatory review checklist item (see .claude/rules/code-review.md): verify the new assertion still tests real, intended behavior — not weakened, loosened, or deleted just to pass — and that it matches a behavior change the task genuinely called for. A modification with no stated justification, or one that merely relaxes a check without a corresponding behavior change, is a **Blocking** finding by default.

This applies to all test files — test logic, assertions, setup/teardown, test data, and test naming. Adding new test cases to an existing test file never needs justification; modifying or removing existing ones always does.

**When a live user is available** (interactive sessions), still stop and explain the proposed change — which test(s), why, and the impact — and wait for explicit confirmation before proceeding. The user is the cheapest, most authoritative check and should be used when present.

**When no user is available** (headless/pipeline dispatch), the reviewer above is the sole gate, since `request_decision` can itself be permission-blocked with nothing to grant it. Plan authors should pre-authorize any test edit they can anticipate directly in `agent_instructions` (name the file, the test, and the literal before/after text) rather than leaving the dispatched agent to guess — see pipeline-story-schema.md's guidance on existing-test conflicts and cumulative shared artifacts. Any modification the plan did not anticipate must still carry the implementer's justification above, so the reviewer has something concrete to check instead of a bare diff.

### Step 5 — Detect the test runner

**Do not assume a fixed test command. Detect the project's test runner before running tests.**

Inspect the project root for build/config files in this order:

| File found | Test command |
|---|---|
| `pom.xml` | `mvn test` |
| `build.gradle` or `build.gradle.kts` | `./gradlew test` |
| `package.json` | `npm test` (or `yarn test` if `yarn.lock` is present) |
| `Makefile` with a `test` target | `make test` |
| `pyproject.toml` / `setup.py` | `pytest` |
| `Cargo.toml` | `cargo test` |

This table is illustrative, not exhaustive — extend it for any ecosystem the repo uses. If multiple build files are found, run the suite for each one the change could affect rather than guessing which is primary. If no recognizable build file is found, ask the user how to run the tests before proceeding.

**Multi-project repositories:** When a repo contains more than one sub-project with its own build file (e.g., a Python API alongside an Angular frontend), run every test suite that your changes could affect — not just the one closest to the files you edited. A change to a shared contract, API response shape, or URL parameter can break both suites simultaneously, as they each validate different sides of the same boundary.

### Step 6 — Verify with the full test suite

**After implementation, run the full project test suite using the detected test runner.**

- All tests must pass. Do not consider the task done if any tests are failing.
- If a pre-existing test fails unrelated to your changes, stop and surface it to the user before proceeding.
- Report the test results (pass/fail counts) as part of your completion summary.

### Step 7 — Submit for review through the pipeline, then prompt before committing

**Once all tests are passing, route the change through the pipeline's review gate before committing anything.**

1. Submit the story with `mcp__pipeline__review_story` to engage the review gate. Do not commit on a blocking verdict — fix the issues and resubmit.
2. Once `review_story` returns a clean verdict, advance the story with `mcp__pipeline__advance_pipeline`.

   **A clean review verdict plus a local test run is not sufficient sign-off for a merge.** `mcp__pipeline__approve_merge` (and the scheduler's own merge path) rebases onto the current default branch, force-pushes, polls the repo's actual CI (`gh pr checks`), and re-runs the acceptance/test suite against the rebased branch before merging — a manual `gh pr merge` bypasses every one of those checks. If a story's merge is ever done by hand instead of through `approve_merge`, first confirm `gh pr checks <branch>` is all-green (not merely absent/pending) — local, single-platform test runs do not catch native/platform-specific dependency failures (npm optional deps, Rust build scripts needing platform assets) that only surface in CI.
3. Present the user a summary: the branch name (from Step 1), a list of all files changed with a one-line description of each, and the proposed commit message formatted per the project's **Commit Standards**.
4. Ask: *"Review approved via the pipeline. Please review the changes above. Shall I commit?"*

Do not assume a pull request is always desired. After confirming the commit, ask the user how they want to proceed: commit only, commit and raise a PR, or commit and merge to a target branch. Wait for explicit direction before pushing, opening a PR, or merging. If the user requests changes to the diff or commit message, apply them, resubmit through `review_story` if the change is non-trivial, then ask again before proceeding.

5. After the commit (and PR/merge, if any) lands, close the story with `mcp__pipeline__mark_story_done`.

### Step 8 — Definition of Done

A task is not complete when the code is written. It is complete when all of the following are true:

- [ ] All new and existing tests pass
- [ ] A reproducing test exists and was observed failing before the fix (bug fixes only)
- [ ] The change has passed `mcp__pipeline__review_story` with a clean verdict (see .claude/rules/code-review.md)
- [ ] The merge went through `mcp__pipeline__approve_merge` (or the scheduler's own merge path) or, for a manual merge, `gh pr checks <branch>` was confirmed all-green first — not just "reviewer approved + tests passed locally"
- [ ] No regressions in areas touched by the change — smoke-test the critical path if automated tests do not cover it
- [ ] Documentation is updated if the change alters externally visible behavior (API contracts, configuration, user-facing functionality)
- [ ] The commit message follows the project's **Commit Standards**
- [ ] The story has been closed with `mcp__pipeline__mark_story_done`

Teams should extend this checklist with their own gates (e.g., QA sign-off, security review for sensitive changes, performance benchmarks for critical paths). This list is the minimum floor, not a ceiling.

### Step 9 — Diagnose before redispatching a stalled or failed automated attempt

When an automated attempt (agent, script, or pipeline worker) fails, stalls, or gives up, do not resubmit the same task or say "try again." Diagnose first:

1. **Pull the actual failure evidence** — the failing test's traceback, the exact exception, the specific log line — not a vague summary like "it didn't work."
2. **Read the relevant code and identify the precise root cause.** Trace the failure to the exact line, condition, or interaction; do not guess from symptoms.
3. **Encode the diagnosis into the next attempt's instructions** — state the exact defect and the minimal fix required, not an open-ended retry. An attempt that failed once with no new information is likely to fail the same way again.

This matters most for weaker or resource-constrained executors (limited context or step budget), which thrash in repeated unproductive self-debugging loops when left to re-diagnose from scratch; a two-sentence root-cause statement resolves in one cycle what a generic "fix the failing tests" burns several on. The weaker the executor, the more of the diagnosis must be done up front. If the stalled attempt's scope was also too broad, narrow the follow-up to a single concern — a small retry with a precise root cause converges far better than a broad retry with none.

---
