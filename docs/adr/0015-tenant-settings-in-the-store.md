# 0015. Tenant settings are versioned Store records resolved per team and repository, not environment variables

- **Status:** Proposed
- **Date:** 2026-11-14
- **Builds on:** [0004](0004-safety-gates-default-on-and-fail-closed.md) (safety
  gates default on and fail closed), [0009](0009-tenancy-and-authorization-model.md)
  (service-layer policy over a verified principal),
  [0012](0012-store-contract-v2.md) (namespaced records; core gives the namespace
  no meaning), [0014](0014-autonomous-decision-record-and-authority.md) (layered
  authority; the audit record of an autonomous decision)
- **Related:** enterprise ADR 0001 (tenancy; the enterprise distribution's
  record of how namespaces map to organizations and teams, which this ADR
  consumes but does not define)

## Context

Plan-level behavior is controlled today by environment variables read at
process start: `PIPELINE_MERGE_CI_GATE`, `PIPELINE_MERGE_BUILD_GATE`,
`PIPELINE_TEST_SCOPE`, `PIPELINE_MERGE_CI_TIMEOUT`, and five more. That was
tolerable when one deployment served one team. It fails for an organization
with several teams on one pipeline:

- **Settings cannot differ between teams.** One process has one value for each
  variable, so a team that needs a different CI timeout or test scope changes
  it for every team on the host, or runs a second deployment.
- **Settings cannot be audited or changed safely at runtime.** An environment
  variable has no version, no author, and no history. A merge that passed under
  a gate that has since been turned off leaves no record of which setting
  allowed it.
- **Safety gates are weakened by the same mechanism that sets them.** A gate
  such as the merge CI gate is a control (ADR 0004). When any operator with
  access to the process environment can turn it off for the whole deployment,
  the control is only as strong as that environment. A lower layer must not be
  able to loosen a control an upper layer set, and a missing value must not
  fall open.
- **Repository settings have no natural home.** An organization repository
  record is shared by every team that merges into that repository, but teams
  may legitimately want different CI rules for it, within limits the
  organization sets.

ADR 0012 gives the Store a namespace and typed record kinds, which is the
precondition for settings that are data rather than process state. This ADR
decides what those settings are, how they resolve, and what happens to the
environment variables that carry them today.

## Decision

1. **Settings are versioned records in the namespaced Store, resolved per use.**
   A setting is a `settings` record kind (added to ADR 0012's record table by
   decision 9 below), keyed by namespace, key and layer, and versioned on every
   write. Each use resolves a key for a `(namespace, team, repo)` triple, reading
   the layers in order from general to specific:

   1. deployment
   2. org
   3. team
   4. team-x-repo

   A plan or story may carry its own value for a key only when that key's
   registry entry allows a plan-level value. A plan-level value never overrides a
   ceiling (decision 2) and never reaches a deployment-only key.

2. **Every key is declared in a core registry, with a kind and a safe value.**
   The registry is code in core, so a key cannot exist without a declared kind
   and safe value. The kinds are:

   - **`lock`** (deployment only). Set once for the deployment; no lower layer
     may set or change it.
   - **`ceiling`**. A lower layer may only tighten the value. The deployment can
     lock any ceiling. A layer above may set `allow_loosen` for that one key,
     which lets the layer below it loosen the value; nothing else can.
   - **`pref`**. The most specific layer that sets the key wins.

   Every key has a **safe value** that the resolver uses when the Store cannot be
   read. The safe value is always the restrictive choice, never the permissive
   one: a fail-closed read returns the gate on, never the gate off (ADR 0004).

   The eight keys moved into the registry by plan 1:

   | Key | Kind | Type | Safe value (Store unreadable) |
   |---|---|---|---|
   | `merge_ci_gate` | ceiling | bool | on |
   | `merge_build_gate` | ceiling | bool | on |
   | `test_scope` | ceiling (`full` is tighter than `scoped`) | `full` \| `scoped` | `full` |
   | `merge_ci_timeout_s` | pref | int | 900 |
   | `merge_readiness_timeout_s` | pref | int | 180 |
   | `rework_on_ci_fail` | pref | bool | off |
   | `reverify_full_suite` | pref | bool | off |
   | `grade_xdist_workers` | ceiling (a lower value is tighter) | int | 1 |

   The safe value for a `pref` is the built-in default that exists today. For the
   timeouts and the rework switch that is the value the process has used with no
   environment variable set, so an unreadable Store changes nothing for a
   deployment that never configured them.

3. **Repository settings are per team per repository, not on the organization's
   repository record.** A repository has no single CI policy. Each `(team, repo)`
   pair has its own team-x-repo layer, and the org layer supplies the ceilings
   those pairs must stay within. The consequence is stated plainly:

   - **Two teams on one repository may merge under different CI rules**, as long
     as each stays within the org ceilings and the deployment locks.
   - **The audit record of a merge must name the layer and the team that supplied
     each gate value.** A reviewer asking "why did this merge skip the build gate"
     must get an answer from the record, not from the current configuration. The
     audit entry carries, for each gate key, the value, the layer it came from,
     the team, and the settings record version.

4. **Environment variables stop being tenant configuration.** They remain the
   deployment layer only for deployment-only (`lock`) keys, such as the Store
   location and the broker address. A tenant-scoped key found in the environment
   is **refused at startup**, and the refusal names the replacement, for example:

   > `PIPELINE_MERGE_CI_GATE` is no longer read. Set `merge_ci_gate` in the
   > Store (`settings` record, layer `team` or `org`), or run
   > `seed-from-env` once to copy the current value.

   The legacy names, kept only for `seed-from-env` and the startup refusal, are:
   `PIPELINE_MERGE_CI_GATE`, `PIPELINE_MERGE_BUILD_GATE`, `PIPELINE_TEST_SCOPE`,
   `PIPELINE_MERGE_CI_TIMEOUT`, `PIPELINE_MERGE_READINESS_TIMEOUT`,
   `PIPELINE_REWORK_ON_CI_FAIL`, `PIPELINE_REVERIFY_FULL_SUITE`, and
   `PIPELINE_GRADE_XDIST_WORKERS`.

   The migration is a one-time `seed-from-env` command. It reads the current
   environment values and writes them to the Store as settings at the layer the
   operator names. It does not run on startup.

   **This is a breaking change.** A deployment that sets any legacy variable
   fails to start until it runs `seed-from-env` or removes the variable. The
   CHANGELOG entry for it is written by story ts-12 and is the operator-facing
   record of the change.

5. **Core gives the namespace no meaning; enterprise supplies it.** Following ADR
   0012, core passes the constant `"default"` and attaches no meaning to it. The
   enterprise distribution supplies real namespaces and implements the `settings`
   record kind in its Postgres store. Core ships three implementations of the
   kind: file, SQLite and in-memory. All three must pass the same settings
   conformance suite, so the resolution rules in decisions 1 and 2 hold for every
   store and do not depend on which one is configured.

6. **`PIPELINE_SETTINGS_PLUGIN` is an optional read-only source of the org and
   team layers.** A deployment that already keeps org or team settings in an
   external system points `PIPELINE_SETTINGS_PLUGIN` at it, and the resolver reads
   those layers from it. While the variable is set, the write tool refuses the org
   and team scopes, so each scope has exactly one source. The deployment and
   team-x-repo layers remain in the Store.

7. **Follow-up groups are not in plan 1.** The following are recorded here so
   their shape is fixed before they are built, and each is its own decision and
   its own story:

   - **Credentials** behind a `CredentialRef` per namespace. A setting names a
     credential by reference; the secret lives in the credential store, never in
     a settings record.
   - **A usage gate keyed by credential**, so a credential's budget applies
     across the teams that use it.
   - **Role routing per team**, which model or provider a team's role resolves to
     (ADR 0002 and 0003 cover the registry this would read).
   - **Notification routes**, where each team's notifications go.
   - **Per-team concurrency shares**, so one team's load does not starve another's
     under the scheduler (ADR 0011).

8. **Settings resolution is part of the audit record, not an afterthought.**
   Every gate decision reads its value through the resolver, and the decision is
   written with the provenance from decision 3. A change to a setting is itself a
   recorded event with an author, a timestamp, and the previous and new values.

9. **The `settings` record kind is added to ADR 0012.** Its record table gains a
   `settings` row: versioned, keyed by `(namespace, layer, team, repo, key)`, with
   the write history retained. This is recorded by a dated amendment to ADR 0012.

## Consequences

- **Cost: a breaking change for every existing deployment.** Any deployment that
  sets a legacy variable must run `seed-from-env` or remove the variable before
  it will start. The startup refusal is loud on purpose: a silently ignored
  variable would leave the operator believing a gate is set when it is not.
  Release notes must say so, and the CHANGELOG entry from story ts-12 is the
  reference.

- **Cost: teams can merge under different rules.** Decision 3 is a deliberate
  loss of uniformity. An operator who expects one CI policy per repository will
  find two. The mitigation is the provenance in the merge audit record and the
  org ceilings, which bound how different the two can be. A future contributor
  who tries to move these settings back onto the repository record will
  reintroduce the single-policy assumption this ADR removes.

- **Cost: resolution happens on every use.** Each gate check reads the Store.
  The read is small, but a Store outage now changes behavior. That is why every
  key has a safe value: an unreadable Store makes gates stricter, never looser,
  and that is the behavior to preserve when optimizing the read. A cache is
  acceptable, but it must not hold a permissive value past a failed refresh.

- **Benefit: the deployment can lock what must not vary.** A `ceiling` key can
  be locked by the deployment, and `allow_loosen` is the only way a lower layer
  gains room. A future contributor who makes a ceiling a `pref` to simplify the
  registry removes the control, not just a feature.

- **Benefit: each gate value is explained by the record.** The merge audit names
  the layer and the team, so an investigation starts from the record, not from
  reconstructing the configuration of a past day.

- **Risk: the registry becomes the authority for every key.** A key that is read
  but not declared has no kind and no safe value, so it must not be read at all.
  The conformance suite should fail when code reads a settings key that the
  registry does not declare.

- **Risk: enterprise stores must match the kind exactly.** A Postgres
  implementation that resolves layers in a different order, or that treats a
  missing layer as permissive, breaks decisions 1 and 2 without any core code
  changing. The settings conformance suite is the guard, and an enterprise store
  is not accepted until it passes it.

### Alternatives considered

- **Keep environment variables and document them.** Rejected: one process has
  one value, the value carries no version or author, and any operator with the
  environment can loosen a control for every team.
- **Put repository CI policy on the organization's repository record.** Rejected:
  it forces one policy per repository even when teams need different rules
  within the org ceilings, and it puts the team dimension nowhere.
- **Let a plan or story set any key.** Rejected: a plan author would be able to
  loosen a gate, which is the control ADR 0004 exists to prevent. Only keys whose
  registry entry allows a plan-level value accept one.
- **Fail open when the Store cannot be read.** Rejected: an outage would silently
  disable every merge gate. The safe value is the restrictive one.
