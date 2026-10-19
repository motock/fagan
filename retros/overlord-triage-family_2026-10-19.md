# Overlord/triage family review

Not a data-only retro. It reads the two plans' manifests, journals and
notification sidecars, and checks the code those plans shipped against the
current tree. It is the code/history review that
`backlog-family-review_2026-10-19.md` §4 recommendation 1 asked for: that
review named overlord/triage as the weakest family (12/26 manifest-only
clean, 9/26 escalated) and could not explain why.

**Plans covered:** `overlord-failure-triage` (17 stories, 2026-08-18 →
08-19) and `overlord-parked-story-autonomy` (9 stories, 2026-09-14).
**Family total:** 26 stories, 26 done, 9 escalated (34.6%).
**First-pass clean:** 10/26 (38.5%) event-aware; 12/26 (46.2%) on the
manifest alone.

Sources: `~/.claude/plans/<plan>.manifest.json`,
`<plan>.<key>.journal.json`, `<plan>.notifications.jsonl` (250 records for
the first plan, 21 for the second), the plan sources, and the current
`pipeline/` tree.

Both plans ran with `repo_root ~/.claude/mcp-servers/pipeline`, the installed
MCP server copy. The first plan's PRs are on `fico-jessecarroll/claude-pipeline-mcp`
(#368–#390); the second's are on `motock/fagan` (#736–#747). The numbers
below describe the harness before the fagan consolidation.

## 1. The record

| | overlord-failure-triage | overlord-parked-story-autonomy |
|---|---|---|
| Stories | 17 (6 epics) | 9 |
| Window (UTC) | Aug 18 04:42 → Aug 19 23:17 (~42.6 h) | Sep 14 14:49 → 21:23 (~6.6 h) |
| Merged | 17/17 | 9/9, all `APPROVE` |
| Escalated | 9 (53%) | 0 |
| First-pass clean | 5 (29%) | 5 (56%) |
| Stories that hit the step cap | 6 (35%) | 2 (22%) |
| Review verdicts | mixed; 2 parked for changes, 2 parked inconclusive | 9/9 `APPROVE`; 2 high-risk holds |

**The escalation ladder worked.** All 9 escalations were real tier changes:
`local` → `ollama/deepseek-v4-flash:cloud`. Six fired on rework-budget
exhaustion and three on the no-new-commit path. Every escalated story
merged. This is the pre-M56 behavior doing what it was designed to do —
contrast the later M56-1 no-op, fixed 2026-10-19 in PR #1127.

## 2. Findings

### 2.1 The family's low rate is a role-config and dispatch-tier effect, not a triage defect

The second plan is the control: consistent role config, 0 escalations, 9/9
`APPROVE`, 56% first-pass clean. The triage layer itself shipped clean —
split/mark_done/patch_acceptance/repo_issue executors, the sweep, the policy,
and overlord merge adjudication all merged first try and are covered today by
38 triage-related test files. The first plan's 53% escalation rate comes from
what it ran *on*, not what it built.

### 2.2 Plan 1 ran a mixed model family on every story

Dispatch resolved to `local`, `test_author` to `ollama`, `planner` to
`claude`. All 17 stories carried the harness's own warning:

> `local dispatch on 'local' but test_author resolves to 'ollama', planner
> resolves to 'claude': the test-first split will not run on the same family
> as the executor`

The warning is advisory by design (`pipeline/test_author.py`, "never blocks
ingest"). Nine of those 17 stories escalated (53%). The plan is the
strongest evidence the harness has for this condition — and it proceeded
anyway. Plan 2, with one family, had none of it.

Today's registry has every role on `ollama/deepseek-v4.1-flash`, so the
condition cannot arise. If roles are ever split across families again, this
plan is the case for making the warning a gate rather than advice.

### 2.3 A false review gate dominated the first plan

159 of its 250 sidecar records (64%) are:

> `Review backend gated (Claude usage gate tripped): deferring review.`

All 159 fall between 15:59 and 19:13 on 2026-08-18 — 3.2 hours at roughly one
record per scheduler tick, 51 of them in a single hour. Review was not
actually unavailable: the registry's review role was `ollama/glm` the whole
time, and the gate was reading Claude's usage state anyway. The false gate
has its own record in `pipeline/usage.py::_role_resource_ok`, which now
resolves the provider review will actually run on.

The per-tick repetition is a separate, still-live defect: the notice has no
transition state and no `dedup_key` (`pipeline/advance.py:889`). It
reproduced on 2026-10-19 — four identical deferrals for M56-1, one per
minute from 15:15 to 15:18.

The false-gate half is fixed twice over: `_role_resource_ok` follows the
resolved review backend, and `pipeline/review_deferral.py` bounds consecutive
rate-limited deferrals per story. The notice repetition is not addressed.

### 2.4 Step caps were 31% of the family, and the rebrief path absorbed the on-device ones

Eight of 26 stories hit a `step_cap_reached` journal entry. Three were
on-device stories that were rebriefed and completed **without** escalation
(`5d73d24e`, `a590080b`, `b2db12a7`); the other three are among the
escalated stories. The rebrief path is what kept the on-device cap rate from
becoming an escalation rate — this is the mechanism the 2026-10-18 window
retro measured as 0/9 on-device, and it is consistent with the decision to
stop dispatching that tier for harness work.

### 2.5 The backlog review's 46% was an upper bound

The two rates differ because `OPSA-7` and `OPSA-9` were parked with
`story_parked` events ("high risk held for human review") and later released
and merged. The manifest alone shows them `done` with an `APPROVE` verdict;
the event history shows the park. The honest family rate is **10/26
(38.5%)**. This retro therefore reduces the family's number, not raises it.

### 2.6 The sidecar carries orphan story keys

Thirty-three distinct story keys appear in the mixed-family warnings; only 17
are in the manifest, and the other 16 are in no manifest on this machine.
The plan source has no explicit `key` fields, so the most likely explanation
is an earlier ingest whose UUIDs a re-ingest replaced — the exact hazard the
preflight's "every story has an explicit `key`" rule exists to prevent.
Not a defect in this family, but it makes the sidecar's per-story counts
unreliable without a manifest join.

## 3. What this retro does not say

- It does not re-review the triage code line by line; it checks that the
  shipped surface exists, is tested, and behaved as designed.
- It cannot prove the mixed-family warning *caused* the escalations. The
  correlation is complete within plan 1 (17/17 warned, 9/17 escalated) and
  absent in plan 2, but the on-device tier is weak on its own, so the two
  explanations are not separable from this data.
- It does not cover the later `overlord-autonomy-round-2` plan, which has a
  decisions log of its own.

## 4. Recommendations

1. **Deduplicate the review-gated notice** (`pipeline/advance.py:889`): give
   it a stable `dedup_key`, or notify on the transition into the gated state
   rather than every tick. Small, live-reproduced, no behavior change.
2. **Keep dispatch, `test_author` and `planner` on one model family.** Today's
   role config does; if a future split reintroduces the warning, treat it as
   a gate for locally-dispatched stories, using this plan as the evidence.
3. **Keep review off the Claude backend by default.** It was the single
   largest recorded blocker of this family and recurred on M56-1.
4. **No work on the triage layer itself.** `DEFERRED_ACTIONS` is empty, every
   action has an executor, and the family's losses are explained by the two
   items above.

## Appendix — per-story record

`clean` is event-aware (`classify_story` with the sidecar records and the
journal). `cap` means the journal holds a `step_cap_reached` entry.

| Story | Tier | Clean | Reasons | Cap |
|---|---|---|---|---|
| 4e469538 | on-device | yes | — | |
| 5d73d24e | on-device | no | `step_cap_reached`, `step_cap_rebrief` | yes |
| cc446d9f | cloud-oss | no | `escalated`, `legacy_message` | |
| fda57485 | on-device | yes | — | |
| 30e5f9fc | cloud-oss | no | `escalated`, `legacy_message`, `step_cap_reached`, `step_cap_rebrief` | yes |
| 381b9f81 | cloud-oss | no | `brief_rewrite_marker`, `escalated`, `legacy_message` | |
| d79a3292 | on-device | yes | — | |
| 2b5c01d8 | cloud-oss | no | `escalated`, `legacy_message` | |
| b5028031 | cloud-oss | no | `escalated`, `legacy_message` | |
| e7a79869 | cloud-oss | no | `escalated`, `legacy_message` | |
| a590080b | on-device | no | `step_cap_reached`, `step_cap_rebrief` | yes |
| 11d6cf05 | cloud-oss | no | `escalated`, `legacy_message` | |
| ef63b85c | cloud-oss | no | `escalated`, `legacy_message`, `step_cap_reached`, `step_cap_rebrief` | yes |
| b2db12a7 | on-device | no | `step_cap_reached`, `step_cap_rebrief` | yes |
| d9704bc1 | on-device | yes | — | |
| 16affd21 | on-device | yes | — | |
| feb40f46 | cloud-oss | no | `escalated`, `legacy_message`, `step_cap_reached`, `step_cap_rebrief` | yes |
| OPSA-1 | cloud-oss | no | `step_cap_reached`, `step_cap_rebrief` | yes |
| OPSA-2 | cloud-oss | yes | — | |
| OPSA-3 | cloud-oss | yes | — | |
| OPSA-4 | cloud-oss | yes | — | |
| OPSA-5 | cloud-oss | no | `step_cap_reached`, `step_cap_rebrief` | yes |
| OPSA-6 | cloud-oss | yes | — | |
| OPSA-7 | cloud-oss | no | `story_parked` | |
| OPSA-8 | cloud-oss | yes | — | |
| OPSA-9 | cloud-oss | no | `story_parked` | |

`legacy_message` on every escalated story is the classifier matching the
word "escalat" in the escalation notification, not an independent reason —
it double-counts the `escalated` flag in these old sidecars.
