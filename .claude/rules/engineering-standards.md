# Engineering standards (full text)

> Moved out of CLAUDE.md so it is not loaded on every turn. These sections
> are also carried by the contributor's global `~/.claude/CLAUDE.md`. Read
> this file when you need the full wording of a standard (testing, security,
> logging, architecture, commit format, branching) and it is not already in
> context. CLAUDE.md keeps only a summary and the repo-specific workflow.

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

## Testing Configuration-Driven Logic and Resource Gates

Additive section — now maintained at .claude/rules/testing-config-gates.md:
two specific testing failure modes, generalized from production incidents,
that the generic Testing section above doesn't call out by name: (1) test
the resolution logic against a stubbed config source, never today's
configured values; (2) validate any gate that can withhold work against the
real environment before trusting it.

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
- Target under 1000 lines per file, new or updated. A file crossing that line is a signal to split by concern, not a hard blocker — but don't add substantial new code to a file already over the limit without splitting it first.

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

## Working with AI Assistance

When using Claude Code or similar tools in this codebase:

- **Scope requests tightly.** Ask for the specific change needed, not a broad rewrite.
- **Review all AI-generated code** as carefully as you would a peer's pull request.
- **Do not accept AI-generated code that adds unrequested features**, refactors adjacent code, or introduces speculative abstractions.
- **Security-sensitive changes** (auth, payments, data access) require human review regardless of source.
- **Generated code must pass the same standards** as hand-written code: tests, security review, and architectural fit.
