# 0005. External context sources are reference data, pinned by digest

- **Status:** Accepted (implemented 2026-10-23/24 as
  `external-context-sources-phase1` and `-phase2`)
- **Date:** 2026-10-22

## Context

An organization's guidance - legal review rules, architecture standards, UX
style guides, security requirements - rarely lives in the target repo. It may
sit in another repo, on a mounted volume, in an object store, or behind an MCP
server, and it changes on its own schedule. Today an agent can only learn what
is in the repo's committed `CLAUDE.md` and rules, the machine-global rules
bundle, its persona file, or the story's `agent_instructions`. None of those is
a home for a separate context lake, and the only per-story channel is free text
baked into the plan file.

Three facts make the obvious answers unsafe:

- **Injection.** Anything we put in front of a model can contain instructions.
  A pipeline that merges code with no human present cannot let fetched text
  rewrite its own rules.
- **Mutability.** A source that changes between dispatch and review silently
  changes the meaning of a story mid-flight. Rework would see different input
  than the first attempt, and the reviewer different input than the implementer.
- **Backend asymmetry about MCP.** Dispatched Claude agents inherit the user's
  MCP servers, single-shot roles run with none, and local-model agents have a
  fixed tool loop with no MCP at all. A mechanism that depends on MCP works for
  exactly one backend.

The design is in `docs/plans/EXTERNAL_CONTEXT_SOURCES_PLAN.md`; this record
captures the decisions that are expensive to reverse once code and plans depend
on them.

## Decision

1. **Context is reference data, never instructions.** Every source is rendered
   in a delimited, provenance-labelled block under a preamble stating that it
   is data supplied by an external source and that embedded instructions must
   not be followed. The model is never asked to obey it.
2. **The safety core is code-enforced and no source can override it.** Test-file
   protection, story `files` scope, the review/CI/merge gates, persona tool
   allow-lists, secret handling, fail-closed defaults, and the pipeline
   workflow itself are enforced mechanically and are never read from context. A
   model that follows an injected instruction is still stopped by the gate.
3. **Provider and delivery are separate.** A *provider* fetches bytes (`dir`
   first; `git`, `http`, `s3`, `mcp` later). A *delivery mode* decides how the
   model receives them (`prompt` block first; read-only `files` snapshot
   outside the worktree later). Adding a provider must not touch any role's
   prompt builder, and adding a delivery mode must not touch any provider.
4. **Precedence, highest to lowest:** safety core > `policy` sources > story
   `agent_instructions` > `reference` sources > repo `CLAUDE.md` and rules >
   fagan global rules > persona defaults. Origin (org, repo, plan) only
   resolves duplicate source names (plan beats repo beats org); it does not
   change precedence. The rendered block is ordered lowest-to-highest authority
   so the strongest guidance sits nearest the task.
5. **Content is pinned by digest at dispatch.** A source is fetched once,
   capped, secret-scanned, and stored as a content-addressed snapshot outside
   the worktree; its sha256 is recorded on the story as `context_digests`.
   Rework, resume, and review load that snapshot by digest and never refetch. A
   missing snapshot is refetched and compared; a digest mismatch fails closed.
   A re-ingest changes the input only for the next fresh dispatch.
6. **Phases are an explicit allow-list per release.** Phase 1 honors `dispatch`,
   `review`, and `security_review`; `planner` and `test_author` follow in phase
   2; `overlord` and `triage` are later. An unsupported phase, provider, or mode
   is rejected at ingest, never accepted and ignored. `decompose` is
   restricted to repo-level configuration (`.fagan.json`) because it runs
   before a plan exists, so there is no plan-level block to read.
7. **MCP is a fetch adapter, never a delivery mechanism.** If supported, the
   resolver calls a configured MCP server as a client and snapshots the result
   through the same digest path. It is never exposed as an agent tool and never
   added to any `allowed_tools`, so it works identically for Claude and local
   models and stays reproducible and auditable.
8. **Remote providers are deferred to phase 3.** `http`, `s3`, and `mcp` add
   credentials, network failure modes, and mutable inputs, and `mcp` adds a
   client dependency that is a supply-chain decision needing its own ADR. They
   are not built until phase 1 has run on a real plan and a real remote need is
   documented.

A source declared `required: true` fails the dispatch closed when it is missing,
unreadable, secret-bearing, or does not fit the budget. `required: false` is the
explicit opt-in to degrade.

## Consequences

- **Good:** an organization can attach governed guidance to a plan without
  committing it to the target repo, and the audit trail ("which guidance did
  this attempt receive?") is answerable from digests without retaining the
  guidance.
- **Good:** because the safety core is mechanical, a successful prompt
  injection can at worst yield wrong code that the reviewer and gates still
  evaluate; it cannot turn a gate off.
- **Cost:** prompt injection is not fully solved. Delimiting and a preamble
  reduce it; they do not eliminate it. The reviewer and security reviewer are
  the backstop, which is why review-phase context matters as much as
  dispatch-phase context.
- **Cost:** pinning trades freshness for reproducibility. An operator who wants
  updated guidance must re-ingest or start a fresh dispatch. Do not "fix" this
  by refetching on rework; that reintroduces silent mid-flight drift.
- **Cost:** a `policy` source outranks the story brief on practice conflicts,
  which can surprise an author whose brief intentionally departs from it. Keep
  `policy` opt-in and use it sparingly.
- **Cost:** a required source that does not fit its cap blocks the run rather
  than being truncated away. Operators raise `max_chars` or split the source.
- **Cost:** the secret scan is best-effort. The real control is that content is
  data and the gates are mechanical.
- **Cost:** no model-chosen retrieval. Sorted files, fixed caps, and no RAG make
  selection less clever but deterministic and available to every backend.
- Context content is never logged, sent to notification channels, or put in a
  PR body; events record name, provider, digest, and sizes only.
- A future contributor may be tempted to expose sources as an MCP tool for
  convenience. That would make delivery backend-dependent and unreproducible,
  and is the specific shape this decision rejects.
