# Backlog family review — plans completed through Sep 21, 2026

A **data-only** window retro under the drain policy in `retros/PENDING.md`.
It reads no individual plan's code or review history. It groups the
backlog's plans into subsystem families by name and reports what the plan
manifests record. Its job is to discharge markers that predate the weekly
window retros (`harness-review_2026-09-29.md`, `harness-review_2026-10-18.md`),
not to explain them.

**Window:** every plan with a completion marker dated on or before
2026-09-21, plus the 57 undated backfill markers (all pre-2026-08-12).
**Coverage:** 184 plans, 659 stories, listed by family in §2. The three
markers dated 2026-10-18 are not covered and stay in `PENDING.md`.

> **Correction (2026-10-19):** the first-pass-clean column below is
> manifest-only and is an upper bound. The event-aware reclassification of the
> same 184 plans is in
> [`backlog-family-review-correction_2026-10-19.md`](backlog-family-review-correction_2026-10-19.md)
> — 406/589 (68.9%) becomes 354/589 (60.1%), and review/rework/gate replaces
> the UI family as the second-weakest family.

Source: `~/.claude/plans/<plan>.manifest.json` for each marker (all 187
markers in the file had a manifest, 184 of them covered here; 168 of the 187 have `repo_root` `pipeline`, the
repo's earlier directory name, and 19 have `fagan`). First-pass-clean numbers
use `pipeline.local_success.classify_story` over the manifest alone.

## 1. What the data says

- **Every story is done.** All 659 stories in the 184 covered plans have
  `status: done`; no covered plan is parked or abandoned.
- **92 of 659 stories (14.0%) carry the `escalated` flag.**
- **406 of 589 in-population stories (68.9%) classify first-pass clean** from
  the manifest alone. The manifest does not hold parks or `brief_patched`
  events, so this is an upper bound, and it mixes tiers and eras; use it to
  compare families against each other, not against the weekly retros' rates.

## 2. Families

Families are assigned by keyword match on the plan name (rules in the
appendix), first match wins. The assignment is a heuristic: `other` is large
(51 plans) because many names carry no subsystem word, and the UI family
absorbs some plans only because a short token such as `w1` or `event` matched.
Treat the counts as orders of magnitude.

| Family | Plans | Stories | Escalated | First-pass clean (manifest-only) | Examples |
|---|---|---|---|---|---|
| dashboard/chat/workspace UI | 37 | 244 | 50 | 131/228 (57%) | DASHBOARD_PROGRESS_TIER1, dashboard-start-stop-script |
| other | 51 | 146 | 11 | 103/129 (80%) | REBRIEF_HARNESS_IMPROVEMENTS, launchd-plist-portability |
| dispatch/escalation/locking | 28 | 76 | 9 | 48/62 (77%) | 5xx-recovery-escalation, final-rework-model-escalation |
| config/env/provenance | 11 | 52 | 6 | 37/49 (76%) | a2-config-surface, role-registry-environ-delegation |
| watchdog/timing/resume | 15 | 38 | 1 | 27/33 (82%) | local-agent-resume-read-grace, mode30-scheduler-git-race |
| overlord/triage/autonomy | 2 | 26 | 9 | 12/26 (46%) | overlord-failure-triage, overlord-parked-story-autonomy |
| review/rework/gate | 16 | 25 | 2 | 13/21 (62%) | mode29-review-story-done-guard, review-commit-hygiene-autofix |
| ci | 9 | 18 | 0 | 16/16 (100%) | ci-gate-and-lint-fixes, ci-green-after-billing-reset |
| token/context/tuning | 7 | 16 | 4 | 3/8 (38%) | gptoss_onboarding, local_model_tuning_table |
| edit-corruption guards | 6 | 15 | 0 | 14/15 (93%) | edit-guard-enforcement, mode31-off-task-drift-guard |
| bench | 2 | 3 | 0 | 2/2 (100%) | benchmark-harness-load-task-fixes |
| **Total** | **184** | **659** | **92** | **406/589 (68.9%)** | |

Observations the data supports:

1. **Overlord/triage/autonomy has the lowest first-pass-clean rate among
   families with 20+ stories (46%, 12/26) and the highest escalation rate
   (9 of 26, 35%).** It is two plans. The Sep 21 review
   (`harness-autonomy-review_2026-09-21.md` §3) already named this layer the
   least-invested relative to its importance; the data is consistent with it.
2. **Token/context/tuning is lowest by rate (3/8, 37%)** on a sample too small
   to rank (8 stories).
3. **The UI family accounts for 244 of 659 stories (37%) and 50 of 92
   escalations (54%).** Its manifest-only rate (57%) is the second lowest
   among families with 20+ stories. Whether that is the UI work itself or the
   keyword rule over-collecting is not distinguishable from this data.
4. **CI maintenance and edit-corruption guards are at or above 93%.** Small
   samples (16 and 15 stories); no inference beyond "no signal of trouble".

## 3. What this retro does not say

- It does not say why any family's rate is what it is.
- It does not say whether any fix in a discharged plan held. Plan outcomes in
  the later weekly retros (Sep 22 onward) are the evidence for that.
- It does not cover the Sep 22–Oct 18 plans, which the two weekly retros do.
- The family rules are a heuristic written for this document; they are not
  stored anywhere the pipeline reads.

## 4. Recommendations

1. **Next, review the overlord/triage family by reading its two plans' event
   history,** since it combines the lowest rate with a known coverage gap. A
   code-level review is the missing step; this document cannot supply it.
2. **If the backlog should stay drained, record the family on each new marker**
   (a short tag in the `PENDING.md` line) so the next data-only pass does not
   need name keywords. This is a suggestion, not scoped work.

## Appendix — family rules (first match wins, applied to the lower-cased plan name)

overlord/triage/autonomy: `overlord|triage|autonomy`; bench: `bench`;
ci: `(^|[-_])ci([-_]|$)|macos|lint|ruff|pin`;
token/context/tuning: `token|context|num[-_]?ctx|gptoss|temperature|tuning`;
watchdog/timing/resume: `watchdog|resume|hang|dead|reconcile|wedge|timeout|liveness|stall|scheduler`;
dispatch/escalation/locking: `dispatch|escalat|lock|lease|race|oracle|mode30|ld90|local`;
review/rework/gate: `review|rework|donebar|gate|grad|mode29|merge|tge|fsu|rfw|pr[-_]`;
edit-corruption guards: `edit|corrupt|mode\d|guard|strreplace|latch|nudge|repair`;
config/env/provenance: `config|env|provenance|transport|alias|a2|w3a`;
dashboard/chat/workspace UI:
`dashboard|chat|workspace|ui[-_]|static|event|server|w1|w2|w3|b1|b2`;
anything else: `other`.
