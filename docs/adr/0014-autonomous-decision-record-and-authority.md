# 0014. Autonomous decisions are recorded, bounded by layered authority and reviewed by humans

- **Status:** Proposed
- **Date:** 2026-11-05
- **Related:** [0006](0006-decision-routing-on-a-shared-server.md),
  [0007](0007-plan-approval-at-activation.md),
  [0009](0009-tenancy-and-authorization-model.md),
  [0011](0011-scheduler-scale-out-leased-plan-advancement.md),
  [0012](0012-store-contract-v2.md),
  [0013](0013-plan-commands-and-lock-scope.md)

## Context

The pipeline already lets an automaton act on a plan with no human in the loop.
Six observations, made on 2026-11-05, show what the current records carry and
what an autonomous decision-maker cannot see after the fact.

1. **The policy contradicts itself on authority.** `overlord-policy.md` defines
   the autonomy tiers, the merge adjudication rules, seven triage ACTIONs and an
   autonomy ladder with three levels - `dry-run`, `gated` and `full`. At `full`
   the overlord already rules proceed or park on `risk:high` merges, but the same
   file also states that at `full` the park-and-ping tier is "always held
   regardless of level". Both cannot hold, and nothing in the code decides
   between them: whoever reviews a decision cannot tell which rule the actor
   believed it was following.

2. **The decision log is a convenience file, not a record.** Decisions live in
   `<plan>.decisions.json`. It holds 59 records across 19 plans, 26 of them
   overlord rulings (24 park, 2 proceed). Each record carries the question, the
   ruling, a rationale, the prior status, `decided_by` and `decided_at`. It
   carries no evidence references, no policy or persona version, no model, no
   tier and no risk. It is not hash-chained, so a record can be edited or
   removed without a trace.

3. **The audit stream is chained but only locally verifiable.** The
   `<plan>.audit.jsonl` stream written by `pipeline/audit.py` is hash-chained;
   it holds 705 records over 26 plans. The chain makes tampering evident inside
   the file, but tail truncation is undetectable without the off-box anchor in
   `pipeline/audit_anchor.py`.

4. **Human and agent are the same actor.** The principal produced by
   `app/identity.py` has no client id and no principal type beyond the `system:`
   subjects of [ADR 0009](0009-tenancy-and-authorization-model.md). A human and
   an agent session that holds the human's token are therefore recorded as the
   same actor, and an approval the human typed into a chat appears as
   `self_approved: true`. There is no field on which "an agent did this" can be
   decided later.

5. **The automated reviewer errs, and a verdict is trusted anyway.** The reviewer
   compared a branch against a stale recorded base and reported already-merged
   master files as drift, twice; it returned a verdict before its asynchronous
   review had finished; it skipped a re-review as "unchanged" without saying
   why. Each result looks like a verdict, and a decision-maker that trusts the
   verdict inherits the error.

6. **A repo-local file can loosen the global rules.** A per-repo
   `.overlord-policy.md` "takes precedence on conflict", so in a shared control
   plane a tenant can loosen the global rules by editing a file in its own
   repository, and the platform has no record that it happened.

## Decision

**We record every autonomous decision as evidence, bind each decision class to a
tier that layered rules can only tighten, and require a human to review both the
decisions and the rules.**

1. **Every autonomous decision is a record.** A decision record carries
   `decision_id`, `class`, `subject` (`plan`, `story`, `pr`, `sha`), `evidence`
   (a list of `kind`, `ref` and `sha256`), `rule` (the sha of every applying
   layer, the persona sha and the model), the alternatives considered, the
   ruling, the `tier`, `reversible` (a boolean), the `outcome`, the
   `correlation_id`, the `actor` and the `principal_type`. Records carry
   identifiers and outcomes only - never payloads, prompts or secrets.

2. **Records form one append-only, hash-chained stream per (namespace, plan)**,
   with the head hash shipped through the audit anchor. The legacy
   `<plan>.decisions.json` stays readable and is versioned, as
   [ADR 0012](0012-store-contract-v2.md) versions the store. Core carries only
   the opaque namespace `default`.

3. **A new principal type `agent` is bound to a team scope**, one per tenant and
   not one per person, next to the `system:scheduler` and `system:worker`
   subjects of [ADR 0009](0009-tenancy-and-authorization-model.md). Its type and
   its client id come from the signed token, never from a caller-supplied field.
   An agent principal can never hold `plan:approve`, approve a rule change, or
   hold `config:write`. How a team's agent subject is provisioned belongs to the
   team onboarding flow and is out of scope here; its name is
   `agent:<org-id>/<team-id>`, and tenancy (org and team, a namespace per team)
   is defined in enterprise ADR 0001 (tenancy). Core assumes only that the
   principal arrives from the identity provider with that type and scope. A team
   agent acts only inside its team scope.

4. **An authority matrix, held as versioned data, maps each decision class** to a
   tier (`human`, `auto+notify`, `auto`), the named evidence checks that must
   pass first, and a never-list. The initial classes are `high_risk_merge`,
   `override_false_positive_verdict`, `scope_widen` and `existing_test_edit`. The
   never-list is always locked: bypassing a gate precondition, deleting a test,
   changing the matrix, the policy or the persona, and acting on the overlord's
   own rule files.

5. **The matrix is layered: an org layer, then team layers.** Each rule declares
   a delegation of `locked`, `tighten_only` or `adjustable`. An adjustable rule
   carries bounds in one of four declared types: `ordered_enum` (values from
   tightest to loosest, with a `loosest` bound), `numeric` (a declared tighter
   direction, with a minimum and a maximum), `set` (`must_include`, or
   `may_choose_from`) and `flag` (a declared tighter value). The loader folds the
   layers from the org down and validates each step against the delegation of
   the layer above. A team value that breaks its envelope is ignored, the
   stricter value stays in force, the violation is recorded and the team leaders
   are alerted - fail closed.

   ```json
   {"class": "high_risk_merge",
    "values": ["human", "auto+notify", "auto"],
    "delegation": "adjustable",
    "bounds": {"loosest": "auto+notify"}}
   ```

   Folded down, that rule behaves like this. The org sets `high_risk_merge` to
   `human` with a loosest bound of `auto+notify`. A team tries `auto`: it is a
   legal value of the enum but looser than the envelope, so it is ignored, the
   effective tier stays `human` and the violation is recorded. The next layer
   tries `auto+notify`: that is inside the envelope, so it is valid and the
   effective tier becomes `auto+notify`. A later layer tries `auto` again: it is
   ignored and the effective tier stays `auto+notify`. The mistake to avoid is
   accepting the attempted value merely because it is a legal enum value, or
   reporting "ignored" while still overwriting the effective tier - either one
   corrupts the comparison the next layer makes.

6. **Verification is code.** An overlord decision names evidence checks that
   return structured evidence, stored in the record: the PR file list against the
   story's declared files, computed against the real merge base; the CI state for
   the exact head sha; a recorded reviewer APPROVE for that sha; no test deleted;
   the assertion count not reduced. A gate verdict is an input to a decision,
   never a substitute for the checks.

7. **Existing-test edits and scope widening are decision classes judged
   mechanically**, never by the overlord's opinion of the test: the assertion
   count is not reduced, nothing is skipped, xfailed or deleted, and expected
   values are unchanged unless the story names them.

8. **Rollout is per class and per rule version**: shadow (record the ruling,
   execute nothing, record the human decision beside it), then act-and-notify
   with a revert window, then autonomous with a digest only. Promotion needs at
   least 20 shadow decisions in the class, 0 false proceeds and a false-park rate
   of at most 10%; any false proceed demotes the class to shadow at once.
   Promotion is always a human decision. Concretely: 20 shadow decisions with 0
   false proceeds and 2 false parks is a 10% false-park rate, so the class is
   eligible; a human promotes it to act-and-notify; the next decision
   false-proceeds, so the class is demoted to shadow immediately and the
   following decision is recorded as shadow and executes nothing. The mistake to
   avoid is demoting only the false-proceed record while leaving the class state
   at act-and-notify, so the next decision still acts.

9. **Humans review.** A periodic digest lists decisions with their evidence. A
   human marks each one correct, wrong or should-have-escalated in a review
   record chained to the decision; the reviewer principal must be human and
   different from the actor. Changes to the matrix, the policy or the persona are
   digest-bound proposals approved by a human holding the approver role for that
   layer: org approvers for the org layer, team leaders for a team layer. The
   approver count is team-configurable above an org-set floor of at least one
   human, and an agent never counts toward it. Every proposal carries the output
   of replaying recorded decisions against the candidate rules. So with an org
   floor of two humans and a team setting of two, one human and one agent
   approving leaves the human count at one and the proposal is not approved; the
   agent's approval is stored but not counted; a second human then approves, the
   human count reaches two and the proposal is approved. The mistake to avoid is
   counting the agent and approving early.

10. **Tenant isolation.** Decision records are partitioned by namespace and are
    not readable across tenants.

11. **Safety rails ship off**: a kill switch for overlord actions that defaults
    to off, a per-class hourly cap, a per-plan blast-radius cap, and protection
    of the rule files, the policy, the persona and the review code against edits
    by an agent principal.

12. **Core ships the record format, the matrix and the layer interface with a
    file source, the evidence checks and the review loop.** The per-tenant store
    source, multi-approver workflows and per-tenant quotas belong to the
    enterprise overlay.

## Consequences

- **More writes per decision, and a new stream to retain.** Every decision now
  writes a record with its evidence references, its rule shas and its model into
  a chained stream that must be retained and anchored: more storage, a retention
  obligation, and an anchor that must keep working or the chain stops verifying.
- **`agent` is a new principal type and needs a provider change first.** It
  extends [ADR 0009](0009-tenancy-and-authorization-model.md), and nothing can be
  enforced until the identity provider issues the type, the team scope and the
  client id. Until then an agent session stays indistinguishable from the human
  whose token it holds, and the never-list is a policy without a mechanism.
- **Shadow mode delays autonomy by weeks, not days.** Collecting 20 shadow
  decisions per class costs the rate at which that class occurs. High-risk merges
  are rare, so `high_risk_merge` may need weeks before it is eligible at all.
  That is the intended price of "0 false proceeds" meaning something.
- **A loose team rule inherits the risk the org allowed.** A team that sets an
  adjustable rule to the loosest value its envelope permits carries that risk,
  and the org has already accepted it by declaring the bound.
- **Replay needs recorded evidence snapshots.** Replay works only if a record
  keeps hashes of what it cited, so evidence must stay addressable and immutable.
- **Tempting shortcuts to avoid.** Each looks like a smaller change and each
  makes the system worse:
  - **Trusting a reviewer verdict without the evidence checks.** The reviewer has
    already reported stale-base drift, early verdicts and silent skips.
  - **Letting a repo-level file loosen an org-locked rule.** That moves the
    per-repo precedence problem rather than solving it.
  - **Counting an agent toward the approver floor.** Two "approvals", one human,
    is one human.
  - **Logging every failed check attempt instead of the outcome.** It buries the
    decision in noise.
  - **Letting the overlord edit its own rules.** A rule author that is also the
    rule's subject is not governed.
  - **Promoting a class on agreement alone while ignoring false proceeds.** A
    class that proceeds wrongly is not ready, however many humans liked it.

### Alternatives considered

- **Keep `.decisions.json` and extend it in place.** A smaller change, but the
  file is editable and unchained: it records what someone said afterwards, not
  what was decided.
- **Make the reviewer's verdict the gate.** Wiring a verdict to a merge is a few
  lines. Rejected: the verdict is derived data observed wrong in three distinct
  ways, and a gate inheriting those errors autonomously is worse than none.
- **Keep per-repo policy precedence.** Convenient for a tenant that wants to move
  faster. Rejected: in a shared control plane precedence is a loosening channel,
  and a tenant-local file cannot be the authority for a platform-level rule.
- **A single flat matrix, no layers.** Simpler to load and reason about. Rejected:
  it forces every team to one tier, so either the org is too strict for some
  teams or too loose for all.
- **Judge test edits by reviewer opinion.** A model can read a diff. Rejected: the
  properties that matter - assertion count, skips, deletions, unchanged expected
  values - are mechanical, and a deterministic check cannot be argued out of its
  answer.

### Not decided here

- **A correction of fact.** [ADR 0013](0013-plan-commands-and-lock-scope.md)
  originally said date `2026-11-04` and used `retry_after` where the code returns
  `retry_after_seconds`. That correction is recorded here; ADR 0013 itself stays
  unchanged, as the index's immutability rule requires.
- **Whether the author of a rule change may also approve it.** Proposal: the org
  default is no, adjustable per team above that floor.
- **The exact principal naming.** `agent:<org-id>/<team-id>` names the shape; the
  canonical encoding belongs with the identity-provider change.
- **Where enterprise stores team layers.** A tenancy record - enterprise ADR 0001
  (tenancy) - owns that; core ships a file source and does not decide it.
- **The final field names.** They are fixed in the record-format story; the fields
  in decision 1 are the required content, not the final spelling.
