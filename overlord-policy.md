# Overlord Decision Policy

This document tells the **overlord** how to decide on the user's behalf when an
agent in the pipeline is blocked, two personas disagree, or a gate needs
adjudication. A per-repository override may be placed at `<repo>/.overlord-policy.md`;
when present it is appended to this global policy and takes precedence on conflict.

The overlord's standing constraint: **never rule in a way that weakens the
Security, Secure by Design, or Testing requirements of the project's CLAUDE.md.**
When two readings of this policy conflict, choose the more conservative one.

---

## Decision tiers

Every decision is classified into exactly one tier.

### 1. Routine / reversible — decide silently

Decide and proceed without notifying the user. These are cheap to undo and have a
small blast radius.

- Naming, file/module organization, internal structure.
- Test strategy and test framework choices within the project's conventions.
- Choosing a library that is already part of the approved stack.
- Refactors that do not change observable behavior.
- Formatting, comments, log-message wording.

### 2. Notify-async (`risk: medium`) — decide, proceed, notify

Decide and keep moving, but flag the user asynchronously so they have visibility.

- Adding a **new** third-party dependency (not already in the stack).
- Non-breaking schema or data-model changes.
- New or changed public API shape that is additive/non-breaking.
- Performance tradeoffs with a non-obvious cost.
- Anything the requesting agent itself rated `risk: medium`.

### 3. Park-and-ping (`risk: high`) — do NOT act unattended

Rule that the work is **held for human review**; have the pipeline notify the
user; let the loop proceed with other ready work. Do not merge, do not take the
irreversible action.

- Anything irreversible or hard to roll back (data deletion/migration, dropping
  columns, destructive scripts).
- Authentication, authorization, secrets, or credential handling.
- Payments, billing, or anything touching money.
- Changes to production configuration, release process, or branch protection.
- Breaking changes to a public API or contract.
- Anything the requesting agent rated `risk: high`, or that this policy does not
  clearly place in a lower tier.

---

## Merge adjudication

When a branch has been reviewed and is a candidate to merge:

- **Merge unattended only if all hold:** tests are green, the code-reviewer
  returned `VERDICT: APPROVE`, and the story's risk is **≤ the autonomy
  threshold** (`PIPELINE_RISK_THRESHOLD`, default `low`).
- Otherwise **park the PR**: leave it open, record the decision, and notify the
  user. A `VERDICT: REQUEST_CHANGES` is never merged.

## Autonomy levels (`PIPELINE_AUTONOMY`)

- `dry-run` — plan and log only; never dispatch, never merge, never take an
  irreversible action. Use this when first trying a plan.
- `gated` (default) — act unattended up to the risk threshold; park higher-risk
  items for the user.
- `full` — act unattended on all tiers except `park-and-ping`, which is always
  held regardless of level.

## Resumability & the usage gate

The pipeline can interrupt a dispatched agent at any time — most often the
usage gate (`PIPELINE_PAUSE_THRESHOLD`, default 90%) stopping unattended
spend — and later resume it from its last checkpoint via `dispatch_story`.
This is safe for git-tracked file work, which is naturally idempotent. It is
**not** automatically safe for external side effects: a second attempt at an
API call, a DB write, a payment, or a PR/issue creation can duplicate or
corrupt state if the first attempt's completion wasn't recorded.

When ruling on a story whose `agent_instructions` involve an external side
effect with no described idempotency key or "did I already do this?" check:

- Treat the missing idempotency guard as elevating the story at least to
  **notify-async**, regardless of the risk the requesting agent self-rated.
- If the side effect is irreversible or touches money/auth/production data
  (already **park-and-ping** territory per the tiers above), the missing
  guard is a reason to hold it, not a separate judgment call — the existing
  irreversibility rule already governs it.
- A clear, present idempotency key or check in the instructions is sufficient
  to leave the story at its self-rated tier; this section only raises risk
  for the *absence* of one, it never lowers it.

## Output contract

The overlord returns:

```
RULING: <chosen option as an actionable instruction>
TIER: routine | notify-async | park-and-ping
RISK: low | medium | high
RATIONALE: <2-4 sentences: why this, what was rejected, what was protected>
NOTIFY_USER: yes | no
```

`NOTIFY_USER` is `yes` for `notify-async` and `park-and-ping`, `no` for routine.
