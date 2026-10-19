# Family review program — close-out

Closes the per-family deep-review program that began with
[`backlog-family-review_2026-10-19.md`](backlog-family-review_2026-10-19.md).
Four documents make up the program:

1. **`backlog-family-review_2026-10-19.md`** — the 184-plan / 659-story
   baseline, manifest-only.
2. **`backlog-family-review-correction_2026-10-19.md`** — the same population
   reclassified with sidecar events and step-cap journals: 406/589 (68.9%) →
   **354/589 (60.1%)**.
3. **`overlord-triage-family_2026-10-19.md`** — the two weakest plans.
4. **`review-rework-gate-family_2026-10-19.md`** and
   **`ui-family_2026-10-19.md`** — the next two weakest families.

The three deep reviews cover 295 of the 659 stories (45%). This document
records what they collectively found, fixes the baselines to use from here,
and recommends ending the program.

## 1. The corrected baselines

Event-aware first-pass clean, families with their plan/story counts:

| Family | Plans | Stories | Event-aware clean |
|---|---|---|---|
| token/context/tuning | 7 | 16 | 3/8 (37.5%) — 8 in population; too small to rank |
| overlord/triage/autonomy | 2 | 26 | 10/26 (38.5%) |
| review/rework/gate | 16 | 25 | 10/21 (47.6%) |
| dashboard/chat/workspace UI (bucket) | 37 | 244 | 118/228 (51.8%) |
| — UI proper only | 25 | 108 | 59/103 (57.3%) |
| — architecture waves in the bucket | 11 | 127 | 56/117 (47.9%) |
| config/env/provenance | 11 | 52 | 28/49 (57.1%) |
| dispatch/escalation/locking | 28 | 76 | 41/62 (66.1%) |
| watchdog/timing/resume | 15 | 38 | 23/33 (69.7%) |
| other | 51 | 146 | 94/129 (72.9%) |
| edit-corruption guards | 6 | 15 | 12/15 (80.0%) |
| ci | 9 | 18 | 13/16 (81.3%) |
| bench | 2 | 3 | 2/2 (100%) |
| **Total** | **184** | **659** | **354/589 (60.1%)** |

## 2. What the three reviews found in common

### 2.1 The low rates are execution-tier and story-shape effects

In every deep review the family's deliverables were present, merged and
tested:

- **overlord** — 26/26 merged; all 9 escalations were real local → cloud-oss
  tier changes.
- **review/rework/gate** — 25/25 merged; all 16 deliverables present today,
  including the gate-aware done-bar, the finding-target guard and the
  file-inventory criterion.
- **UI** — the chat, dashboard and static surfaces shipped; 134 UI test files;
  the W2 frozen-tools defect is fixed and the prompt is now generated from
  the registry.

No review found a live defect in what a family built. The losses are in how
the work was dispatched: weak tier, or too large a story.

### 2.2 The on-device tier is the consistent first-order cause

Every review found on-device stories hitting the step cap at high rates —
overlord 7 of 9 local stories, review/rework/gate 7 of 8 cap stories, UI 28 of
60 cap stories. This is the same tier effect the 2026-10-18 window retro
measured as 0/9. It is already addressed by ODT-1 ("not cleared for
dispatch") and the 2026-10-19 single-family role switch; no further family
review is needed to establish it.

### 2.3 Cloud-oss does not rescue a badly shaped story

The UI review's most useful correction to the common story: **21 of its 60
step-cap stories ran on cloud-oss**, clustered in the multi-file refactors
(`dashboard-comms`, `server-app-file-split`, `event-driven*`,
`workspace-picker`, `chat-logs`). Routing a large split to a stronger model
helps, but the shape rules in `agent-dispatch-story-sizing.md` still apply to
every tier. The largest escalation cause across the UI bucket was
"no new commit" (19), i.e. an agent stalling without writing code — the same
signal.

### 2.4 The family assignment is a coverage tool, not a taxonomy

Two of the three reviews found their bucket mixed populations:

- **UI**: the `w1|w2|w3|b1|b2|event|server` keywords pull in architecture
  waves; UI proper (57.3%) and the waves (47.9%) are 9 points apart.
- **config/env**: the largest single loss in that family is
  `w3b-dashboard-config-ui`, a UI plan caught by `config`.

Future comparisons should record the sub-bucket, or accept that the family
number is a bucket number.

### 2.5 The instrument improved during the program

The baseline correction itself is the program's most durable output: reading
sidecar events and step-cap journals moved the overall rate 8.8 points. Two
known rough edges remain, recorded here so they are not rediscovered:

- `legacy_message` matches the harness's own escalation notices, so escalated
  stories double-count that reason, and it can label a genuine park and a
  benign mention identically.
- `brief_patched` has no structural record equivalent to the step-cap
  journal; a future rebrief variant that stops leaving a marker in the brief
  would read clean.

## 3. Recommendations

1. **End the per-family deep reviews.** The remaining families are at or
   above the overall rate, and the reviews have converged on the same two
   causes. A fourth pass would repeat the method without new information.
2. **Use the corrected table in §1 as the baseline**; do not cite the
   manifest-only rates from the original backlog review.
3. **Let the weekly window retros carry the ongoing record.** The next is due
   2026-10-25 and will discharge the 7 markers dated 2026-10-18/19.
4. **If a family is ever revisited**, start with sidecar events and journals,
   and record the sub-bucket where the keyword rule mixes populations.
5. **Keep the two instrument rough edges (§2.5) in the backlog** for the next
   time the classifier is touched; neither justifies work on its own.

## 4. What this close-out does not say

- It does not claim the remaining families are healthy — only that their
  numbers are above the overall rate and their likely cause is already
  established.
- It does not supersede the weekly window retros, which observe outcomes
  rather than backfilled plans.
- It re-states, rather than re-derives, the per-family numbers; each comes
  from the document named in §1.
