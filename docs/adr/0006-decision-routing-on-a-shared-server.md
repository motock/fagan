# 0006. Decisions on a shared server route to the plan owner

- **Status:** Accepted
- **Date:** 2026-10-23

## Context

Today `request_decision` escalates to the `overlord` role and appends to the
plan's decision log. That is sound while one operator runs their own pipeline
(T1) or several plans on one machine (T2): the person who started the plan is
the person who answers. A shared server (T3) breaks the assumption. The human
who should answer is not necessarily the one who started the plan, and nothing
in the system can say whose a decision request is: neither a plan nor a story
carries an owner, and the MCP path has no notion of caller identity.

The topology record (`docs/plans/SHARED_SERVICE_TOPOLOGY_PLAN.md`) therefore
makes this question the first prerequisite for building T3: *who answers a
decision on a plan they did not create?* Building a transport before it is
answered would produce the wrong tenancy model. The project owner confirmed
the rule below on 2026-10-23.

## Decision

We route decisions on a shared server by plan ownership:

1. **One owner per plan.** A plan has a single `owner`, taken from the
   authenticated MCP caller at ingest. It is never supplied in the plan file,
   so an author cannot assign a plan to someone else.
2. **Overlord first.** `request_decision` goes to the overlord, as today.
3. **Escalation to the owner.** Anything the overlord escalates goes to the
   plan owner. The plan may carry an optional `delegates` list of identities
   that may also answer.
4. **Unanswered decisions park the story.** A decision nobody answers within a
   bounded time parks the story. It does not proceed, and it does not fall
   through to a default answer.

Ownership is enforced in the **mutating tool surface**, not only in routing.
Tools that change a plan or story (interrupt, patch, pause, mark done, answer a
decision, and the like) must check the caller against the owner and delegates
and deny by default. Routing alone would only decide who is *asked*; without
enforcement anyone who can reach the server could still answer or act.

Namespaced branches (`PIPELINE_BRANCH_NAMESPACE`) are a **prerequisite** for a
multi-tenant server. Branches and worktrees are keyed by the story key alone,
so two engineers sharing one `origin` can overwrite each other's
`agent/{key}` branch.

### Alternatives considered

- **Team-owned plans.** A team or group as the owner. Not chosen: it makes
  "who answers" a membership question and leaves no single accountable person
  when the team does not respond. Per-plan `delegates` give the same reach
  while keeping one named owner.
- **Routing by risk class.** Send low-risk decisions to one party and
  high-risk ones to another. Not chosen: it needs a risk taxonomy the
  decision log does not carry, and it still would not say who owns a plan.
  Risk can refine routing later; it cannot replace ownership.
- **Last writer answers.** Whoever responds first, or most recently, wins.
  Not chosen: it is an open door, since anyone able to reach the server can
  settle another engineer's plan, and it gives a race instead of an answer.

## Consequences

- Plans gain an `owner` and an optional `delegates` list, and the mutating
  tools gain an ownership check. That is new schema, a new identity input, and
  a new permission surface to test, including negative cases for non-owners.
- Authentication of the MCP caller becomes a hard dependency of ownership: an
  owner can only be taken from an authenticated caller, so ownership cannot
  ship before authentication.
- Parking on timeout trades throughput for safety. A plan whose owner is away
  stalls rather than proceeding on a guess; the bound is a tunable that must
  be chosen and documented when it is built.
- A single owner is a single point of delay. `delegates` mitigates this, but
  reassigning an owner is not addressed here.
- Namespaced branches are a prerequisite, and namespacing is a migration for
  existing branches and the resume path, not a one-line change.
- **Not addressed by this decision:** `.pipeline.env` precedence (the file
  wins over inherited environment, so operators with different pins cannot
  coexist in one process tree) and the stdio transport (one client per
  process). Each needs its own ADR before T3 is built.
- **No T3 code is authorized by this decision.** It records the routing rule
  only. Transport, authentication, ownership enforcement, and per-operator
  configuration remain unbuilt, and a future contributor should not treat this
  ADR as approval to start them.
