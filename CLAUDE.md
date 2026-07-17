# Engineering Best Practices

This file governs how AI-assisted development should be conducted in this codebase. It applies to all contributors using Claude Code and serves as a shared reference for engineering standards across teams.

---

## How to use this template

This file is a starting point, not a final specification. Fork it, adapt it, and commit it as `CLAUDE.md` at the root of your repository.

**Keep as-is (invariant):** Core Principles, Code Quality, Testing, Security, Secure by Design, Observability & Logging, and Architecture & Design. These are universal engineering standards; weakening them reduces the value of the template for your team and makes it harder to share practices across teams.

**Customize for your context:**
- **Agent Workflow — Step 1:** If your team's pipeline tool exposes different story/plan primitives than `mcp__pipeline__*`, swap in the equivalent calls. The sequence (ingest plan → claim story → mark in progress) should stay the same.
- **Agent Workflow — Step 5:** Detection is intentionally language-agnostic — it inspects whatever build/config files are present rather than assuming one ecosystem. No customization should be needed unless your repo's test command can't be inferred from its build files.
- **Commit Standards:** Adopt Conventional Commits as written, or substitute your team's preferred format — but pick one and apply it consistently.
- **Definition of Done:** Add or remove gates to match your team's release process (e.g., QA sign-off, load test, security review).

**Extend with team-specific sections:** Add language-, framework-, or domain-specific guidance below the existing sections. Prefer adding new sections over editing shared ones so the document stays composable with future updates to the base template.

---

## Local Development Setup

> **Template instruction:** Replace this section with your project's actual setup steps before committing. Delete this notice when done.

Document the minimum steps required to get a new contributor running locally:

```
# 1. Clone and install dependencies
#    <replace with your commands>

# 2. Configure environment variables
#    <list required variables and where to get values>

# 3. Start the application
#    <replace with your start command>

# 4. Run the test suite
#    <replace with your test command>
```

Include any prerequisites (runtime versions, required tools, local services) and note any common setup pitfalls. If a service dependency (database, message broker, etc.) is required, document how to start it — preferably via a single command such as `docker compose up`.

---

## Core Principles

1. **Correctness over cleverness.** Working, readable code beats elegant but opaque code.
2. **Minimal footprint.** Only add what the task actually requires. No speculative features, premature abstractions, or unused helpers.
3. **Leave it better.** Don't degrade quality in the area you touched, but don't refactor adjacent code that wasn't part of the task.
4. **Verify before assuming.** Read the relevant code before suggesting or making changes. Don't guess at behavior.

---

## Code Quality

### Simplicity
- Prefer the simplest solution that correctly solves the problem.
- Avoid over-engineering: don't design for hypothetical future requirements.
- Three similar lines of code is better than a premature abstraction.
- Remove dead code rather than commenting it out.

### Naming
- Names should describe intent, not implementation (`processOrder`, not `doStuff`).
- Avoid abbreviations unless they are universally understood in the domain.
- Boolean variables and functions should read as assertions (`isEnabled`, `hasPermission`).

### Readability
- Keep functions small and focused on a single responsibility.
- Avoid deep nesting; prefer early returns and guard clauses.
- Only add comments where the logic is not self-evident. Code should explain *what*; comments explain *why*.
- Do not add docstrings, type annotations, or comments to code you did not change.

### Error Handling
- Only handle errors that can actually occur. Don't add fallbacks for impossible scenarios.
- Validate inputs at system boundaries (user input, external APIs, file I/O). Trust internal interfaces.
- Propagate errors meaningfully — don't swallow exceptions without logging or re-raising.

---

## Testing

### What to test
- Test observable behavior, not internal implementation details.
- Focus on the public interface; avoid testing private methods directly.
- Tests should document expected behavior, not just exercise code paths.
- **Always write both positive and negative tests.** A test suite that only covers the happy path is incomplete.

Negative tests must cover, at minimum:
- Invalid or malformed inputs (nulls, empty strings, out-of-range values, wrong types)
- Missing required fields or parameters
- Boundary conditions (zero, one, max, min, empty collections)
- Expected exceptions — assert both the exception type and message where meaningful
- Unauthorized or forbidden access attempts where applicable

For every behavior you test with valid input, ask: *"What should happen when this input is wrong or missing?"* If the answer is defined, write the test.

### Test design
- Each test should have a single, clear assertion or behavioral outcome.
- Arrange, Act, Assert: keep setup, execution, and verification distinct.
- Avoid logic (loops, conditionals) in tests — if it's complex, it's likely testing too much.
- Name tests to describe what they verify: `should_reject_expired_token`, not `test_auth`.

### Mocking
- Only mock at external system boundaries (databases, APIs, file system, time).
- Do not mock internal code or application logic — it creates false confidence.
- Prefer integration tests over heavily mocked unit tests for critical paths.

### API contract testing

An API contract is the agreed interface between a producer and its consumers — the set of endpoints, request shapes, response shapes, status codes, and error formats that both sides depend on. Contract violations are a leading cause of integration failures that unit and component tests cannot catch.

**What to contract-test:**
- All valid request shapes return the documented response schema
- All documented error conditions return the correct status code and error format
- Boundary values of every numeric parameter (min, max, and one beyond each limit)
- Required fields — requests missing them must be rejected with a clear, consistent error
- Optional fields — their presence or absence must not cause unexpected behavior

**How to write contract tests:**
- Treat the API specification (OpenAPI, GraphQL schema, Protobuf, etc.) as the source of truth. Tests assert conformance to the spec, not to the current implementation.
- Cover both sides of every contract boundary: the endpoint that enforces a constraint and the client that must respect it. A query parameter limit on the server (e.g., `limit ≤ 100`) must be matched by a client that never exceeds it — and a test on each side that verifies this.
- When a contract must change, version it explicitly. Do not silently alter response shapes or remove fields; consumers will break without warning.

**What must never be skipped:**
- Numeric bounds on query parameters and request body fields — these are contract terms, not implementation details
- Error response bodies — consumers parse error messages; changing their structure is a breaking change
- Pagination contracts — page size limits, cursor formats, and total-count fields are part of the interface

Contract tests belong in the same repository as the service they test and must run in CI on every change. A contract violation that reaches production is a regression.

### Coverage expectations
- Aim for meaningful coverage, not 100% line coverage for its own sake.
- Untested code that touches security, data integrity, or money requires a justification comment.

---

## Security

### Input validation
- Validate and sanitize all input at the point of entry. Never trust data from users, external APIs, or environment variables without validation.
- Use parameterized queries or ORM-level protections for all database access. Never interpolate user data into queries.
- Encode output appropriately for the context (HTML, SQL, shell, etc.) to prevent injection.

### Secrets management
- Never commit secrets, tokens, API keys, or credentials to source control.
- Reference secrets via environment variables or a secrets manager. Never hardcode them.
- If a secret is accidentally committed, treat it as compromised and rotate it immediately.

### Least privilege
- Request only the permissions a component actually needs.
- Scope tokens, roles, and service accounts as narrowly as possible.
- Avoid storing sensitive data you don't need.

### Dependencies
- Keep dependencies up to date. Outdated dependencies are a common attack surface.
- Audit new dependencies before adding them — prefer well-maintained libraries with narrow scope.
- Remove unused dependencies.

### OWASP awareness
- Be mindful of the OWASP Top 10 when writing code that handles authentication, authorization, data exposure, or user input.
- When in doubt about a security decision, surface the concern rather than guessing.

---

## Secure by Design

Security must be built in from the start, not added after the fact. The following principles apply at every layer — architecture, implementation, and configuration.

### Fail securely / deny by default
- When a security check fails, throws, or produces an unexpected result, the system must land in a **denied or closed state**, never an open one.
- Avoid patterns where an exception or missing value silently grants access.
- Authorization logic should explicitly grant access; anything not explicitly permitted is denied.

### Secure defaults
- Default configuration must be the most restrictive option available. Users and operators should opt in to permissiveness, not opt out of it.
- New features, flags, and endpoints should ship disabled or locked down by default.
- Never ship with debug modes, verbose logging, or admin backdoors enabled by default.

### Defense in depth
- Do not rely on a single security control. Layer independent controls so that the failure of one does not expose the system.
- Examples: validate at the API boundary *and* enforce at the service layer *and* restrict at the database level.
- Assume any individual control can be bypassed; design so that bypass alone is insufficient.

### No security by obscurity
- Security must not depend on hiding implementation details, endpoint paths, field names, or algorithm choices.
- Treat all internal logic as potentially visible to an attacker. Controls must hold even when the attacker knows how they work.
- Obscurity may be used as an *additional* layer but never as the *primary* control.

### Data minimization
- Only collect, store, and process the data actually required for the feature.
- Do not log, cache, or pass through fields you don't need — you cannot leak what you don't have.
- Apply retention limits: data that is no longer needed should be deleted.
- Mask or truncate sensitive values (card numbers, SSNs, tokens) wherever they appear outside their primary storage.

### No sensitive data in logs or errors
- Log messages, stack traces, and error responses must never contain passwords, tokens, API keys, PII, session identifiers, or internal file paths.
- Return generic error messages to callers; log the full detail server-side with a correlation ID.
- Before adding a log statement that includes request/response data, explicitly check whether it could contain sensitive fields.

### Audit logging
- Log all security-relevant events with enough context to support investigation:
  - Authentication attempts (success and failure), including source IP and user identifier
  - Authorization failures
  - Access to sensitive or regulated data
  - Privilege escalation or role changes
  - Configuration changes
- Audit logs must be tamper-evident and written to a location the application cannot modify after the fact.
- Do not log sensitive values in audit events — log identifiers and outcomes, not payloads.

---

## Observability & Logging

Good logging is what makes a system debuggable in production. Write logs for the engineer who is paged at 2am with no prior context — not for the developer who just wrote the code.

### Structured logging
- Log in a structured format (e.g., JSON) rather than free-form strings. Structured logs are searchable, filterable, and parseable by tooling.
- Include consistent fields across all log entries: `timestamp`, `level`, `service`, `correlation_id`, and `message` at a minimum.
- Do not concatenate dynamic values into message strings — use structured fields instead.

```
# Avoid — dynamic values concatenated into the message string
log.info("User " + userId + " placed order " + orderId)

# Prefer — dynamic values as structured fields
log.info("Order placed", { "userId": userId, "orderId": orderId })
```

### Log levels
Use log levels consistently and deliberately:

| Level | Use for |
|---|---|
| `ERROR` | Failures that require immediate attention or indicate data loss / system instability |
| `WARN` | Unexpected conditions the system recovered from, or degraded behavior |
| `INFO` | Normal, significant lifecycle events (service start, job complete, connection established) |
| `DEBUG` | Detail useful during development or targeted troubleshooting — must be off in production by default |
| `TRACE` | Fine-grained internals — never on in production |

Do not use `ERROR` for expected failure cases (e.g., a user entering a wrong password). Do not use `INFO` for high-frequency events that would flood logs under normal load.

### Correlation IDs
- Every inbound request must be assigned a unique correlation ID at the entry point.
- Propagate the correlation ID through all downstream calls, log entries, and error responses.
- Return the correlation ID to the caller in error responses so they can reference it in support requests.
- If an upstream correlation ID is provided (e.g., via a request header), preserve and use it rather than generating a new one.

### Log signal, not noise
- Log outcomes and decisions, not every step of execution.
- Avoid logging inside tight loops or high-frequency paths — aggregate instead.
- A log entry that always appears alongside another adds no information; eliminate the redundant one.
- Regularly review log volume. If a level produces so much output it obscures real events, it is miscalibrated.

### What not to log
- Sensitive data (passwords, tokens, PII, card numbers) — see Secure by Design.
- Full request/response payloads unless explicitly required for compliance and masked appropriately.
- Stack traces at `INFO` or below — stack traces belong at `ERROR` or `WARN` only.

---

## Architecture & Design

### Separation of concerns
- Each module, class, or function should have one clear responsibility.
- Keep business logic separate from infrastructure concerns (I/O, networking, persistence).
- Avoid leaking implementation details across layer boundaries.

### Dependencies
- Depend on abstractions, not concrete implementations, where the flexibility is genuinely needed.
- Avoid circular dependencies. If two modules need each other, extract a shared abstraction.
- Prefer explicit dependencies (passed as arguments) over implicit ones (global state, singletons).

### API design
- APIs should be easy to use correctly and hard to use incorrectly.
- Be conservative in what you expose. It is easier to add surface area than to remove it.
- Breaking changes to public interfaces require explicit versioning or migration paths.

### State management
- Minimize mutable shared state. Prefer immutable data structures where practical.
- Make state transitions explicit and traceable.
- Side effects should be isolated and clearly identified.

### Scalability & performance
- Don't optimize prematurely. Measure before tuning.
- Identify and address known bottlenecks (N+1 queries, unbounded memory growth, blocking I/O) as a baseline.
- Document non-obvious performance trade-offs in the code.

---

## Commit Standards

Use [Conventional Commits](https://www.conventionalcommits.org) format for all commit messages:

```
<type>(<scope>): <subject>

[optional body]

[optional footer]
```

**Types:**

| Type | Use for |
|---|---|
| `feat` | New feature or user-facing capability |
| `fix` | Bug fix |
| `chore` | Maintenance, dependencies, config — no behavior change |
| `refactor` | Code restructuring — no behavior change, no bug fix |
| `test` | Adding or updating tests only |
| `docs` | Documentation only |
| `ci` | CI/CD pipeline changes |
| `perf` | Performance improvement |

**Rules:**
- Subject line: imperative mood, ≤72 characters, no trailing period (`add user login`, not `Added user login.`)
- Body: wrap at 72 characters; explain *why*, not *what*
- Breaking changes: append `!` to the type (`feat!: remove legacy auth endpoint`) and add a `BREAKING CHANGE:` footer
- Reference issues in the footer: `Closes ABC-123`

This format enables automated changelogs and makes `git log` scannable. Teams may substitute an alternative format by updating this section — but whichever format is chosen, apply it consistently across all commits in the repository.

---

## Branching Strategy

This project uses **trunk-based development**. All work happens on short-lived branches that merge directly into `main`. There are no long-lived feature branches, release branches, or develop branches.

### Rules
- Branch from `main`; merge back to `main`. The mainline is always deployable.
- Keep branches short-lived — ideally merged within one to two days. Long-running branches accumulate merge debt and make integration harder.
- Merge small, incremental changes rather than large batches. A branch that touches dozens of files is a sign of too much scope.
- Delete branches after merge. Stale branches are noise.

### Why trunk-based development
Long-lived branches delay integration feedback. Bugs and conflicts that would be caught immediately on `main` are discovered late — when they are more expensive to fix. Trunk-based development enforces continuous integration by keeping the team working close to the shared state of the codebase.

---

## Agent Workflow

These are mandatory procedural steps that must be followed for every task, in order.

### Step 1 — Claim the work from the pipeline (before any code changes)

**Always drive work through the mcp pipeline tools — never start coding against an ad hoc, untracked request.**

1. If the work hasn't been decomposed into a plan yet, produce epics/stories (typically via the product-analyst agent) and register them with `mcp__pipeline__save_plan` or `mcp__pipeline__ingest_plan`.

   **`save_plan`'s story schema is not what its tool description implies.** The tool description only shows the epic-level shape (`{"summary", "stories": [...]}`) and omits story-level fields entirely. The shape `ingest_plan` actually reads (verified against `pipeline_mcp_server.py`) is:

   ```json
   {
     "repo_root": "/absolute/path/to/this/plan's/git/repo",
     "epics": [
       {
         "summary": "Epic name",
         "stories": [
           {
             "summary": "Story title — the unique key other stories reference as a dependency",
             "description": "Human-readable scope and rationale (becomes the Plane issue body when Plane is enabled; otherwise not stored in the manifest)",
             "agent_instructions": "The full implementation brief the dispatched agent receives: scope, approach, the TDD expectation, and negative/boundary cases to cover. Populate richly.",
             "acceptance": [{"path": "tests/acceptance_foo.rs", "source": "// optional read-only test fixture; the oracle grades the run on whether the impl makes these pass"}],
             "dependencies": ["Exact summary text (or explicit key) of a prerequisite story in this same plan"],
             "persona": "software-engineer",
             "model": "sonnet",
             "risk": "low",
             "backend": "optional: claude | local | ollama | lmstudio | mlx | auto",
             "key": "optional explicit story key; omit to auto-mint a UUID"
           }
         ]
       }
     ],
     "role_config": {"review": {"provider": "mlx", "model": "qwen"}}
   }
   ```

   Only `summary` is required. `repo_root` is plan-level (required — `ingest_plan` validates it's an existing directory and `advance_all_plans` scopes each plan's work to it). The fields that actually reach the agent and the review gate are `agent_instructions` (the implementation brief, seeded into the dispatch prompt), `acceptance` (an optional array of `{path, source}` read-only test fixtures — when present, the harness materializes them into the worktree and the oracle grades the run on whether the implementation makes them pass; when absent, the story runs on the base harness with a "tests pass" bar), `persona`/`model`/`risk` (routing and merge gating), and `dependencies`. `description` is read only for the Plane issue body when Plane is enabled; the manifest and the dispatch prompt do not use it, so treat it as optional human context for plan review.

   **Each story benefits from rich content — sparse stories (a summary alone) leave the agent to guess.** Populate, at minimum:
   - **`agent_instructions`** — the implementation brief: what to build, the approach, the TDD expectation (write the failing test first), the testable success criteria (concrete, checkable statements such as *"`cargo test -p storage` passes"* or *"rejects a zero-length key with `StoreError::Corrupted`"*), and the negative/boundary cases the tests must cover. This is the single most influential field on outcome quality — the testable criteria live here, not in a separate field.
   - **`acceptance`** — *optional*: an array of `{path, source}` read-only test fixtures. The harness writes each `source` to `path` in the worktree (read-only — the agent may not edit them) and the oracle grades the run on whether the implementation makes them pass. Omit it for ordinary TDD stories where the agent writes its own tests (per `agent_instructions`) and the "tests pass" bar plus the code-reviewer gate suffice; reserve it for cases where you want to pre-specify the acceptance test the implementation must satisfy.
   - **`persona` / `model` / `risk`** — match the work: `software-engineer`/`sonnet`/`low` for mechanical backend work, `security-engineer`/`opus`/`high` for auth or crypto, etc. `risk` drives the overlord's merge gating.
   - **`description`** — optional human context for plan review (and the Plane issue body). Useful but not dispatched.
   - **`backend`** — *optional*: pins this one story's dispatch provider (`claude`/`local`/`ollama`/`lmstudio`/`mlx`/`auto`), independent of the process-wide `PIPELINE_BACKEND_DISPATCH`. `ingest_plan` rejects an unrecognized value at ingest time. Omit to use the process-wide default.
   - **`role_config`** — *optional, plan-level* (a sibling of `epics`, not a story field): per-role provider/model overrides for `overlord`/`planner`/`dispatch`/`review`/`decompose`, e.g. `{"review": {"provider": "mlx", "model": "qwen"}}`. See the README's "Per-role provider/model configuration" section and `model_registry.json` for the full priority chain and the list of available providers/models.

   Do **not** invent fields like `id`, `title`, `acceptance_criteria` (there is no such field — testable criteria go in `agent_instructions`; `acceptance` is an optional array of `{path, source}` file fixtures, not a list of strings), or `depends_on` — `ingest_plan` will fail with a bare `'summary'` KeyError if a story is missing `summary`, and `dependencies` must reference other stories by their exact `summary` string (or their explicit `key`), not by an invented ID. If in doubt, read an existing file under `~/.claude/plans/*.json` as ground truth before guessing.
2. Find available work with `mcp__pipeline__list_ready_stories`, or check a specific story with `mcp__pipeline__check_story_status`.
3. Claim the story with `mcp__pipeline__dispatch_story`, then mark it in progress with `mcp__pipeline__mark_story_in_progress`.
4. Branch from the dispatched story ID: `agent/{STORY-ID}` (e.g., `agent/PIPE-123`). The branch name comes from the pipeline, not an invented identifier.
5. If you hit a decision you're not authorized to make alone, raise it with `mcp__pipeline__request_decision` (or check `mcp__pipeline__list_decisions` for precedent) rather than guessing.

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

### Step 4 — Protect existing tests

**Never modify an existing test without explicit user approval.**

If a change you are making would require altering an existing test:
- Stop before making the modification.
- Explain to the user: which test(s) would change, why the change is needed, and what the impact is.
- Wait for explicit confirmation before proceeding.

This applies to all test files — test logic, assertions, setup/teardown, test data, and test naming. Adding new test cases to an existing test file is permitted; modifying existing ones is not.

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
- [ ] The change has passed `mcp__pipeline__review_story` with a clean verdict (see **Code Review**)
- [ ] The merge went through `mcp__pipeline__approve_merge` (or the scheduler's own merge path) or, for a manual merge, `gh pr checks <branch>` was confirmed all-green first — not just "reviewer approved + tests passed locally"
- [ ] No regressions in areas touched by the change — smoke-test the critical path if automated tests do not cover it
- [ ] Documentation is updated if the change alters externally visible behavior (API contracts, configuration, user-facing functionality)
- [ ] The commit message follows the project's **Commit Standards**
- [ ] The story has been closed with `mcp__pipeline__mark_story_done`

Teams should extend this checklist with their own gates (e.g., QA sign-off, security review for sensitive changes, performance benchmarks for critical paths). This list is the minimum floor, not a ceiling.

---

## Code Review

Code review is the last quality gate before code enters the shared codebase. Its purpose is to catch what tests cannot: design problems, unclear intent, missing edge cases, security concerns, and drift from team standards.

### What a reviewer is signing off on

Approving a pull request is a statement that the reviewer has verified all of the following:

- The change does what it claims to do
- Tests cover the new behavior, including negative and boundary cases
- No obvious security issues — injection, unvalidated input, exposed secrets, excessive permissions
- The implementation is consistent with the architecture and style of the surrounding code
- The commit message accurately describes the change
- If the change alters externally visible behavior (API contracts, configuration, CLI flags, user-facing functionality), documentation is updated — request changes and name the specific doc if it's missing, rather than approving with the gap unaddressed

An approver who has not checked these items should not approve.

### Blocking vs. non-blocking feedback

Be explicit about the weight of your feedback so the author can prioritize:

| Label | Meaning |
|---|---|
| **Blocking** | Must be addressed before merge. The change is incorrect, unsafe, or violates a standard. |
| **Suggestion** | Worth considering but will not block merge. Author decides. |
| **Nit** | Minor style or wording preference. Author may ignore. |

Default to `Suggestion` when in doubt. Reserve `Blocking` for genuine problems — overusing it trains authors to discount all feedback.

### Pull request size

Small, focused pull requests are easier to review, less risky to merge, and produce more useful feedback.

- **Aim for PRs under 400 lines of changed code.** This is a guideline, not a hard limit — a well-scoped 600-line change is better than five artificial splits — but consistently large PRs indicate a scoping problem.
- **One concern per PR.** A PR that fixes a bug *and* refactors a module *and* updates dependencies is three PRs. Mixing concerns makes review harder and rollback nearly impossible.
- **Separate mechanical changes from behavioral ones.** Refactors, formatting fixes, and dependency updates should not be bundled with feature work. When something breaks, you need to identify which change caused it.
- If a task genuinely requires a large change, break it into a sequence of reviewable steps merged incrementally to `main`.

### Reviewing AI-generated code

AI-generated code requires the same scrutiny as human-written code. Additionally:

- Verify the AI did not add unrequested features, quietly remove behavior, or silently refactor adjacent code
- Check that tests were written before the implementation (per the Agent Workflow), not retrofitted after
- Be skeptical of plausible-looking code that has not been exercised against the actual system — AI can generate syntactically correct code that is logically wrong
- Security-sensitive changes (auth, payments, data access) require human review regardless of source or apparent quality

---

## Working with AI Assistance

When using Claude Code or similar tools in this codebase:

- **Scope requests tightly.** Ask for the specific change needed, not a broad rewrite.
- **Review all AI-generated code** as carefully as you would a peer's pull request.
- **Do not accept AI-generated code that adds unrequested features**, refactors adjacent code, or introduces speculative abstractions.
- **Security-sensitive changes** (auth, payments, data access) require human review regardless of source.
- **Generated code must pass the same standards** as hand-written code: tests, security review, and architectural fit.
