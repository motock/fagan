# Engineering Best Practices (global)

Applies to every project. A project's own CLAUDE.md adds to this and wins on conflict
(e.g. a project mandating a specific workflow tool, branch model, or commit format).

## Core Principles

1. **Correctness over cleverness.** Working, readable code beats elegant but opaque code.
2. **Minimal footprint.** Add only what the task requires — no speculative features, premature abstractions, or unused helpers.
3. **Leave it better.** Don't degrade what you touched; don't refactor adjacent code that wasn't in scope.
4. **Verify before assuming.** Read the relevant code before changing it. Don't guess at behavior.

## Code Quality

- Simplest solution that works; no abstractions for hypothetical needs; delete dead code rather than commenting it out.
- Names describe intent (`processOrder`, not `doStuff`); booleans read as assertions (`isEnabled`).
- Small single-purpose functions; early returns over deep nesting. Comments explain *why*, not *what*. Don't add docstrings, annotations, or comments to code you didn't change.
- Handle only errors that can occur — no fallbacks for impossible cases. Validate at boundaries (user input, external APIs, file I/O); trust internal interfaces. Never swallow an exception without logging or re-raising.

## Testing

- Test observable behavior through the public interface, not internals.
- **Write both positive and negative tests.** Negative covers invalid or malformed input (nulls, empty strings, out-of-range, wrong types), missing required fields, boundaries (zero, one, max, min, empty collection), expected exceptions (assert type and message), and unauthorized access. For every valid-input test, ask what the input's wrong or missing form should do — if that is defined, test it.
- One behavioral outcome per test, with arrange/act/assert distinct; no loops or conditionals in tests; name tests for what they verify (`should_reject_expired_token`).
- Mock only at external boundaries (databases, APIs, filesystem, time), never internal logic. Prefer integration tests over heavily mocked units on critical paths.
- **Contract tests** assert conformance to the spec (OpenAPI, GraphQL, Protobuf), not the current implementation: valid requests return the documented schema, every documented error returns the right status and body, numeric bounds at min/max and one beyond, required versus optional fields, pagination limits and cursor formats. Test both the client that must respect a limit and the server that enforces it. Version breaking changes explicitly — never silently alter a response shape or drop a field. Run them in CI.
- Meaningful coverage beats 100% line coverage. Untested code touching security, data integrity, or money needs a justification comment.
- **Test resolution logic, not today's configured values.** Stub the config source and assert against the fixture, never the live config. Exception: a hardcoded fallback invariant — stub it empty and assert the fallback fires.
- **Validate any gate that can withhold work against the real environment.** A green mocked suite proves branch logic, not that a threshold is survivable. Run it once for real and print the measured value beside the threshold; prefer stable signals, and bias toward never blocking work known to succeed.

## Security

- Validate and sanitize all input at entry, including environment variables and external API data. Parameterized queries or ORM protection only — never interpolate user data into a query. Encode output for its context (HTML, SQL, shell).
- Never commit secrets, tokens, keys, or credentials; reference them from the environment or a secrets manager. A committed secret is compromised — rotate it immediately.
- Least privilege: request only the permissions a component needs, scope tokens and roles narrowly, and don't store sensitive data you don't need.
- Keep dependencies current; audit new ones (well-maintained, narrow scope); remove unused ones.
- Mind the OWASP Top 10 for authentication, authorization, data exposure, and user input.

## Secure by Design

- **Fail securely, deny by default.** A failed, throwing, or unexpected check lands denied. Authorization explicitly grants; everything else is denied.
- **Secure defaults.** Default configuration is the most restrictive. New features, flags, and endpoints ship off or locked down — no debug modes, verbose logging, or backdoors by default.
- **Defense in depth.** Layer controls (API boundary *and* service *and* datastore); assume any single one can be bypassed.
- **No security by obscurity.** Controls must hold when the attacker knows how they work.
- **Data minimization.** Collect, log, cache, and retain only what is needed; mask or truncate sensitive values outside their primary storage.
- **No sensitive data in logs or errors.** Never log passwords, tokens, keys, PII, session IDs, or internal paths. Return generic errors to callers; log the detail server-side with a correlation ID, after checking the payload for sensitive fields.
- **Audit logging.** Log authentication attempts (with source and user), authorization failures, sensitive-data access, privilege changes, and config changes — as identifiers and outcomes, never payloads. Audit logs must be tamper-evident.

## Observability & Logging

Write logs for the engineer paged at 2am with no context.

- Structured (e.g. JSON) with consistent fields: `timestamp`, `level`, `service`, `correlation_id`, `message`. Dynamic values go in fields, never concatenated into the message.
- Levels: `ERROR` needs immediate attention or signals data loss or instability (not expected failures like a wrong password); `WARN` is recovered or degraded; `INFO` is significant lifecycle events, not high-frequency noise; `DEBUG` is off in production by default; `TRACE` is never on in production.
- Assign a correlation ID at every inbound request, preserve an upstream one when given, propagate it through downstream calls and logs, and return it in error responses.
- Log outcomes and decisions, not every step; nothing inside tight loops; drop entries that always accompany another. Never log sensitive data, full request or response payloads, or stack traces below `WARN`.

## Architecture & Design

- One responsibility per module, class, or function; keep business logic apart from I/O, networking, and persistence; don't leak implementation details across layers.
- Target under 1000 lines per file — crossing it is a signal to split by concern. Don't add substantial code to an over-limit file without splitting first.
- Depend on abstractions only where the flexibility is genuinely needed; no circular dependencies; pass dependencies explicitly rather than through globals.
- APIs should be easy to use correctly and hard to use incorrectly; expose little, since adding surface is easier than removing it; version breaking changes.
- Minimize shared mutable state; make transitions explicit; isolate side effects.
- Don't optimize prematurely — but fix known bottlenecks (N+1 queries, unbounded memory growth, blocking I/O) as a baseline, and document non-obvious trade-offs.

## Commit Standards

Conventional Commits unless the project says otherwise: `<type>(<scope>): <subject>`, then an optional body and footer, with types `feat`, `fix`, `chore`, `refactor`, `test`, `docs`, `ci`, `perf`.

Subject imperative, at most 72 characters, no trailing period. Body wrapped at 72, explaining *why* rather than *what*. Breaking changes get `!` after the type plus a `BREAKING CHANGE:` footer. Reference issues in the footer (`Closes ABC-123`).

## Branching

Trunk-based by default: short-lived branches off the main branch, small incremental merges, delete after merge. Never work directly on the main branch. Project rules override.

## Working Workflow

**1. Diagnose before acting (bug work).** Read the paths end-to-end, find the exact line or condition responsible, reproduce it concretely (a `curl`, a failing assertion, a log line), and state the root cause before writing code. If you cannot pinpoint it, say so rather than trying a likely-looking fix.

**2. Test first.** Write failing tests that define the behavior and confirm they fail *for the right reason* — a failing assertion, not an import or syntax error — then write the minimum implementation to pass, refactoring with tests green. For a bug, the diagnosis becomes a test that reproduces the exact failure, observed failing *before* the fix; a test written after the fix cannot be trusted.

**3. Existing tests.** Don't loosen, delete, or rewrite an existing test just to make it pass. If a task legitimately changes behavior a test pins, stop and explain which test, why the old behavior is no longer correct, and the new expectation, then wait for confirmation. Adding test cases never needs approval; justify any change in the commit message.

**4. Detect the test runner.** Don't assume one: `pom.xml` → `mvn test`; `build.gradle[.kts]` → `./gradlew test`; `package.json` → `npm test` (`yarn test` with `yarn.lock`); a `test` target in `Makefile` → `make test`; `pyproject.toml` or `setup.py` → `pytest`; `Cargo.toml` → `cargo test`. In a multi-project repo, run every suite the change could affect — a shared contract breaks both sides.

**5. Verify with the full suite.** All tests must pass. A pre-existing failure unrelated to your change: stop and surface it. Report pass and fail counts.

**6. Review before committing.** Present the branch, the changed files with a one-line description each, and the proposed commit message. Ask before committing, and ask again before pushing, opening a PR, or merging.

**7. Definition of Done.** All new and existing tests pass · a reproducing test was observed failing before the fix (bugs) · self-reviewed as a peer's PR (no unrequested features, no scratch files, existing-test changes justified) · no regressions in the areas touched, with the critical path smoke-tested where tests don't cover it · docs updated for externally visible change (API, config, CLI flags, user-facing behavior) · commit message follows Commit Standards.

## Code Review Standard

Approving asserts: it does what it claims; tests cover the new behavior including negative and boundary cases; no obvious security issues; it matches the surrounding architecture and style; the commit message is accurate; docs are updated for visible changes; any modified existing test is justified; the diff contains no unrequested files or scratch scripts; and generated code carries no unrequested features, adjacent refactors, or speculative abstractions.

- Label feedback **Blocking** (incorrect, unsafe, or violates a standard), **Suggestion** (author decides), or **Nit** (style). Default to Suggestion when in doubt.
- Aim for PRs under about 400 changed lines, one concern per PR, mechanical changes separated from behavioral ones.
- A green suite proves the tested branches, not spec-completeness — diff against the requirement, and re-derive boundary and numeric logic yourself, since an AI-authored test can be self-consistently wrong.
- Pin the exact version of any tool whose exit code gates a merge (linters, formatters, type checkers), and keep it identical locally and in CI.
- Security-sensitive changes (authentication, payments, data access) need human review regardless of source.
