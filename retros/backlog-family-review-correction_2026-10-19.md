# Backlog family review — event-aware correction

Corrects the first-pass-clean column of
[`backlog-family-review_2026-10-19.md`](backlog-family-review_2026-10-19.md).
That review classified each story from its manifest alone and said so; every
rate in it is an upper bound. This pass keeps the identical population and
family rules and adds the two other records the classifier can now read:

1. **Sidecar events** (`<plan>.notifications.jsonl`) — `story_parked`,
   `brief_patched`, `escalated` events the manifest does not carry.
2. **Step-cap journals** (`<plan>.<key>.journal.json`) — the
   `step_cap_reached` entry added to the classifier by M56-1 (PR #1126) on
   2026-10-19.

**Population:** the same 184 plans and 659 stories (every plan with a
completion marker dated on or before 2026-09-21, plus the 57 undated backfill
markers). No plan was added or dropped; 589 stories are in population.

## 1. Headline

| Classification | First-pass clean | Rate |
|---|---|---|
| Manifest only (as published) | 406/589 | 68.9% |
| + sidecar events | 384/589 | 65.2% |
| + step-cap journals | **354/589** | **60.1%** |

The published rate overstates first-pass clean by **52 stories, 8.8
percentage points**. Sidecar events account for 22 of them, step-cap journals
for the other 30. Escalations are unchanged: 92/659 (14.0%).

## 2. By family

| Family | Plans | Stories | Escalated | Manifest | +events | +journal |
|---|---|---|---|---|---|---|
| dashboard/chat/workspace UI | 37 | 244 | 50 | 131/228 | 125/228 | 118/228 (51.8%) |
| other | 51 | 146 | 11 | 103/129 | 99/129 | 94/129 (72.9%) |
| dispatch/escalation/locking | 28 | 76 | 9 | 48/62 | 44/62 | 41/62 (66.1%) |
| config/env/provenance | 11 | 52 | 6 | 37/49 | 32/49 | 28/49 (57.1%) |
| watchdog/timing/resume | 15 | 38 | 1 | 27/33 | 27/33 | 23/33 (69.7%) |
| overlord/triage/autonomy | 2 | 26 | 9 | 12/26 | 10/26 | 10/26 (38.5%) |
| review/rework/gate | 16 | 25 | 2 | 13/21 | 12/21 | 10/21 (47.6%) |
| ci | 9 | 18 | 0 | 16/16 | 16/16 | 13/16 (81.3%) |
| token/context/tuning | 7 | 16 | 4 | 3/8 | 3/8 | 3/8 (37.5%) |
| edit-corruption guards | 6 | 15 | 0 | 14/15 | 14/15 | 12/15 (80.0%) |
| bench | 2 | 3 | 0 | 2/2 | 2/2 | 2/2 (100%) |
| **Total** | **184** | **659** | **92** | **406/589** | **384/589** | **354/589 (60.1%)** |

## 3. What changes in the reading

1. **CI is no longer perfect.** 16/16 manifest-clean becomes 13/16: three CI
   stories hit the step cap and were rebriefed. The original review's
   "no signal of trouble" for CI does not survive the journal pass.
2. **review/rework/gate is the second-weakest family, not the UI.** Among
   families with 20+ stories the corrected order is overlord 38.5%, **review
   47.6%**, UI 51.8%, config 57.1%, dispatch 66.1%, watchdog 69.7%, other
   72.9%. The first review called out the UI because it was second-lowest on
   manifest-only data; event data moves review/rework/gate below it.
3. **config/env/provenance drops 9 stories** (37/49 → 28/49), the largest
   absolute family drop. Five of them are one plan,
   `w3b-dashboard-config-ui`: four stories were parked as high-risk holds and
   one was escalated on rework-budget exhaustion, all recorded only as
   untyped messages, so the classifier reports them under `legacy_message`.
4. **No family's rate improves.** Every family moves down or stays flat
   (bench and token/context/tuning are unchanged), which is what an upper
   bound should look like when the additional records are read.

The overlord family's separate review
([`overlord-triage-family_2026-10-19.md`](overlord-triage-family_2026-10-19.md))
reached the same 12/26 → 10/26 correction independently; this pass confirms
it from the same data.

## 4. What this correction does not say

- It does not re-review any family's code. It is a reclassification of the
  same plans with more of their own records.
- It does not change the escalation counts, the family-assignment heuristic,
  or the population.
- The 60.1% is still not a "true" rate for the historical window: the sidecar
  only exists for plans that wrote one, and the journal only for plans that
  checkpointed. It is the best rate the records available today support.

## 5. Recommendation

Use the **+journal column** as the baseline whenever these families are
compared, and read `backlog-family-review_2026-10-19.md` with this
correction. Any future family retro should classify with sidecar events and
journals from the start rather than adding them in a later pass.
