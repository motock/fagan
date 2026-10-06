# Overlord Decision Policy — Standalone Specification

**Version:** 1.0 · **Status:** stable · **Audience:** agent-harness authors

A harness-agnostic specification of the decision authority ("overlord") that
lets an autonomous agent harness act on a user's behalf without the user being
in the loop for every choice. An adopting harness implements the normative
rules in sections 1–6; Appendix A maps them to one concrete implementation.

---

## 1. Purpose and scope

An autonomous agent that acts unattended eventually hits choices its
instructions do not settle: which of two valid designs to implement, whether a
dependency may be added, whether a reviewed change may merge. Without a
decision authority the harness must either block on a human for every such
choice (defeating autonomy) or act unbounded (unacceptable for irreversible
harm). This spec defines that authority: a single policy that classifies every
decision into exactly one of three risk tiers and prescribes how the harness
must act in each, plus the rules for merge adjudication, escalation, and audit.

Conformance: the key words MUST, SHOULD, and MAY are to be interpreted as
described in RFC 2119. A conforming harness MUST implement sections 2–5
completely; section 6 states what it MUST provide to do so.

In scope: decision classification and ruling; unattended merge gating;
escalation of held work to humans; the audit trail of rulings.
Out of scope: how agents plan, implement, review, or test; how notifications
are transported; how the adjudicator is implemented or reached.

## 2. The standing constraint

The decision authority exists to protect the user's standards, not to trade
them away for throughput.

- The authority MUST NOT rule in a way that weakens the security,
  secure-by-design, or testing requirements of the project's standards
  document. A ruling that would relax any of those three is invalid regardless
  of its other merits.
- When two readings of this policy conflict, the authority MUST choose the more
  conservative one (the higher tier, the smaller blast radius).
- The authority SHOULD prefer the reversible option, the smaller blast radius,
  and the choice that keeps the mainline deployable, and SHOULD be decisive:
  pick one option; if every option is unacceptable, it MUST say so and define
  the acceptable path instead of hedging.

## 3. The three decision tiers

Every decision MUST be classified into exactly one tier, and the harness MUST
act according to that tier's behavior. When uncertain which tier applies, the
authority MUST choose the more conservative (higher) tier.

### 3.1 Tier 1 — Routine / reversible: decide silently

Behavior: decide and proceed without notifying the user. These decisions are
"cheap to undo and have a small blast radius". Membership:

- Naming, file/module organization, internal structure.
- Test strategy and test-framework choices within the project's conventions.
- Choosing a library that is already part of the approved stack.
- Refactors that do not change observable behavior.
- Formatting, comments, log-message wording.

### 3.2 Tier 2 — Notify-async (medium risk): decide, proceed, notify

Behavior: decide and keep moving, but flag the user asynchronously so they have
visibility. Membership:

- Adding a **new** third-party dependency (one not already in the stack).
- Non-breaking schema or data-model changes.
- New or changed public API shape that is additive/non-breaking.
- Performance tradeoffs with a non-obvious cost.
- Anything the requesting agent itself rated medium risk.

### 3.3 Tier 3 — Park-and-ping (high risk): do NOT act unattended

Behavior: rule that the work is **held for human review**; have the harness
notify the user; "let the loop proceed with other ready work". In `dry-run` and
`gated` the harness MUST NOT merge and MUST NOT take the irreversible action.
Under `full`, a `risk: high` merge gate is the one exception: the authority
adjudicates the hold (section 4) and records the ruling for post-hoc audit; all
other Tier 3 work stays held. Membership:

- Anything irreversible or hard to roll back (data deletion or migration,
  dropping columns, destructive scripts).
- Authentication, authorization, secrets, or credential handling.
- Payments, billing, or anything touching money.
- Changes to production configuration, release process, or branch protection.
- Breaking changes to a public API or contract.
- Anything the requesting agent rated high risk, or that this policy does not
  clearly place in a lower tier.

## 4. Merge adjudication

When a change has been reviewed and is a candidate to merge unattended:

- The harness MAY merge unattended only if **all** of the following hold: the
  test suite is green; the code review returned an explicit approval verdict;
  and the change's assessed risk is at or below the harness's configured
  autonomy threshold.
- Otherwise the harness MUST park the change: leave it unmerged, record the
  decision, and notify the user. A review verdict of "request changes" is
  never merged.

Under the `full` autonomy level a `risk: high` merge is not always held: the
harness asks the authority to adjudicate the hold, ruling `proceed` or `park`
with a rationale, and records the ruling for post-hoc audit. In `dry-run` and
`gated` the high-risk merge is always parked for a human. An adopting harness
MAY keep the stricter always-hold floor.

## 5. Escalation protocol and audit trail

When the authority is invoked — an agent blocked on a decision, two agents
disagreeing, or a gate needing adjudication — the caller MUST supply:

- **Question** — the decision to be made, stated so it can be answered.
- **Options** — the competing alternatives, including their tradeoffs.
- **Context** — the story or change the decision belongs to, plus the decision
  policy in force (including any per-project override).

The authority MUST return a structured ruling containing at least: the chosen
option as an instruction the requester can act on; the tier; the risk level; a
rationale of a few sentences stating why this option, what was rejected, and
what was protected; and whether the user should be notified (yes for Tiers 2
and 3, no for Tier 1). A Tier 3 ruling MUST be routed to the harness's
headless adjudicator path, which holds the work and notifies the user; the
harness MUST NOT act on the held item unattended, except that under `full` a
`risk: high` merge gate is adjudicated as section 4 describes.

Every ruling MUST be written to a durable decision log containing, at minimum:
a **timestamp**; the **question**; the **options** considered; the **ruling**;
and the **rationale**. The rationale SHOULD be specific enough to stand alone
as an audit record. The log is append-only; the harness SHOULD NOT rewrite or
delete past entries.

## 6. Adoption notes

A harness adopting this policy MUST provide three capabilities:

1. **A notification channel** — an asynchronous way to reach the user for
   Tier 2 flags and Tier 3 holds (the policy does not mandate the transport).
2. **A decision-log sink** — durable storage for the five audit fields of
   section 5, queryable after the fact.
3. **A headless adjudicator** — a way to invoke the decision authority
   programmatically (a persona prompt plus a model call) so agents can obtain
   a ruling without a human in the loop.

It SHOULD also support a per-project policy override that is appended to the
base policy and takes precedence on conflict, and an autonomy level that can
restrict — but never relax — Tier 3: park-and-ping work is held for human
review in `dry-run` and `gated`, and under `full` only a `risk: high` merge
gate is adjudicated (section 4) and audited. An adopting harness MAY add tiers
or stricter gates, including keeping the stricter always-hold floor; it MUST
NOT weaken the standing constraint or the Tier 3 hold.

## 7. Parked-story resolution

A parked story is not necessarily a dead end. When a story is held — a failed
attempt, an exhausted rework budget, a high-risk merge gate, or an ambiguous
blocker — the authority MAY be asked to resolve it by choosing one of a small
set of executable rulings. Each ruling is a decision the harness can carry out,
not merely advice, and each is bounded by the evidence it requires and by a
fail-closed rule.

### 7.1 The decision matrix

The authority maps the live evidence it is shown to exactly one ruling:

| Live evidence signal | Ruling |
| --- | --- |
| Stale bookkeeping — live state contradicts the recorded park reason (the story's change is already merged) | `mark_done` |
| Rework exhaustion with only mechanical leftovers | `escalate_model` |
| Repeated step-cap exhaustion on oversized scope | `split_story` |
| The acceptance fixture is demonstrably broken at a clean baseline | `patch_acceptance` |
| A `risk: high` merge hold | held in dry-run and gated; the authority adjudicates it in full |
| Abandoned or superseded scope | stays parked by ruling, with recorded reasoning |

A story that parks permanently by ruling (abandoned or superseded scope) is a
correct outcome with recorded reasoning, not a failure.

### 7.2 Rulings

- **`split_story`** — the scope is wrong for any implementer at this tier and
  the story must be broken up. The ruling carries exactly two child summaries,
  separated by ` || `; the harness executes it by creating the two child
  stories. Fail closed: a ruling whose action is absent, unparseable, or
  unrecognized is treated as `park_for_human`.

- **`patch_acceptance`** — the story's acceptance fixture is demonstrably
  born-broken: it cannot pass at a clean baseline no matter what an implementer
  writes, so the fixture itself is the defect. The ruling must carry a one-line
  `DIAGNOSIS:` naming the defect plus the complete corrected fixture source
  between `===FIXTURE-START===` and `===FIXTURE-END===` markers. Both markers
  are required exactly once, the `DIAGNOSIS:` line is required, and the captured
  source is everything between the marker lines (the markers themselves are
  stripped). The harness validates before writing: the rewrite must pass the
  ingest lint/collection check AND must still fail at a clean baseline — a
  rewrite that passes with no implementation is isolation-only and is rejected.
  Fail closed: a response missing either marker, the `DIAGNOSIS:` line, or with
  an empty body is unparseable and parks for a human, leaving the acceptance
  source untouched.

- **`mark_done`** — correct the story record to done only when live evidence
  corroborates that the work landed: the story's change is reported merged. New
  commits, a green suite, or an open change request never corroborate on their
  own, and branch ancestry proves nothing when changes are squash-merged; the
  recorded park reason is never evidence. Fail closed: an uncorroborated
  `mark_done` leaves the story parked and notifies a human.

- **`repo_issue`** — the failure is environmental, not the story's fault: a red
  lint baseline, a red suite at a clean baseline, a born-broken acceptance
  oracle, or unavailable CI. The authority NEVER edits the repository; a
  detected repo issue becomes a normal pipeline story that goes through the
  same test-first, review, and CI path as anything else. The harness executes it
  by filing one follow-up story while the original stays parked for a human with
  a reason naming it.

- **`escalate_model`** — the scope is right but the implementer is too weak;
  retry the same scope on a stronger rung of the existing ladder.

- **`park_for_human`** — genuinely ambiguous; hold it. This is also the
  fail-closed default: an absent, unparseable, or unrecognized action resolves
  to `park_for_human`.

### 7.3 Post-hoc audit

Every ruling the authority executes is recorded in a decisions log that a human
reviews after the fact. The log captures the ruling, the evidence it was based
on, and the story's prior state before any mutation, so a human can reconstruct
and, if necessary, reverse the decision post-hoc. The authority holds resolution
authority, and every executed action is reviewable after the fact.

## Appendix A: Mapping to this repository

This spec is the harness-agnostic form of this repository's overlord policy.
The concrete artifacts:

- `overlord-policy.md` — the implementation of this policy: the same standing
  constraint, tiers, merge gate, and output contract, plus repo-specific
  mechanics this spec generalizes (autonomy levels `dry-run`/`gated`/`full`
  via `PIPELINE_AUTONOMY`, the usage gate `PIPELINE_PAUSE_THRESHOLD`,
  resumability and idempotency rules, failure triage with the four ACTION
  values, and the `PIPELINE_RISK_THRESHOLD` merge gate).
- `pipeline/overlord.py` — the execution layer: `_load_policy()` concatenates
  the global policy with a per-repo `.overlord-policy.md` override, and
  `_invoke_overlord()` runs the overlord persona headless through the
  configured Backend (provider/model resolved via the role registry, falling
  back to the persona's declared model; read-only tool access). The pipeline
  server exposes this as the `mcp__pipeline__request_decision` tool, with
  `mcp__pipeline__ask_overlord` as its user-facing alias.
- `agents/overlord.md` — the persona: "the single decision authority that acts
  on the user's behalf so the user does not have to be in the loop for every
  choice", decisive, accountable, and conservative about irreversible harm;
  its output contract is the structured ruling parsed by the pipeline.