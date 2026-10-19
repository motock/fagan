# Review/rework/gate family review

The second family review prompted by
[`backlog-family-review-correction_2026-10-19.md`](backlog-family-review-correction_2026-10-19.md):
with event-aware numbers this family is the second-weakest among those with
20+ stories — 10/21 first-pass clean (47.6%), behind only overlord (38.5%) —
and it lost the most stories to the journal pass alongside the UI family
(13/21 → 10/21). It also governs the gate that grades every other change, so
its numbers compound into every other family's record.

**Coverage:** the 16 plans this family assigns by the backlog review's keyword
rule, completed between the 2026-08-12 backfill and 2026-09-21.
**Total:** 25 stories, 25 done, 2 escalated (8%), 10/21 event-aware clean.

Sources: each plan's `manifest.json`, `.notifications.jsonl` and per-story
`.journal.json`, plus the current `pipeline/` tree and its tests.

## 1. The record

| | |
|---|---|
| Plans | 16 (12 single-story, 3 two-story, 1 seven-story) |
| Stories | 25, all merged |
| Tiers | 12 on-device, 9 cloud-oss, 4 Claude (out of population) |
| Event-aware clean | 10/21 (47.6%) |
| Manifest-only clean | 13/21 (61.9%) |
| Escalated | 2 (8%) |
| Stories that hit the step cap | 8 (32%) — 7 on-device, 1 cloud-oss |
| `brief_patched` events | 2 |

The 16 plans are a chain of small, single-purpose fixes to the review and
rework gates: done-bar gate awareness, the retry-once suite exemption,
acceptance-block grading, the no-resume-when-agent-claims-done rule,
measurement freshness and tier attribution, the reviewer finding-target
guard, PR branch resolution, commit-message auto-fix, the reviewer prompt's
no-test-rerun rule, the test-collection allowlist, and the retro-append hook.

## 2. Findings

### 2.1 The rate is the on-device tier again

Seven of the eight step-cap stories ran on-device; the cloud-oss stories are
8/9 clean. The non-clean cloud-oss story is `MFR-06`, which also escalated.
This is the same tier pattern the overlord family review found (seven of nine
escalations local → cloud-oss), and the same one the 2026-10-18 window retro
measured at 0/9. Nothing in this family's data distinguishes it from that
tier effect.

The two escalations were both real tier changes to `ollama` and both merged:
`OPR-01` on rework-budget exhaustion (after three review cycles) and `MFR-06`
on no-new-commit (after three redispatches). Neither is a same-target no-op;
the guard that prevents one was added later in PR #1127.

### 2.2 What the family built still holds

Every deliverable is present in the current tree with tests:

- `pipeline/review.py` carries the file-inventory criterion in the reviewer
  prompt (`:456`), and `.claude/rules/code-review.md` carries it in the
  checklist.
- `pipeline/ecosystem_detect.py` / `scripts/local_agent_oracle*.py` carry the
  gate-aware `_full_suite_result`; `tests/unit/test_donebar_gate_aware.py` and
  `test_donebar_retry_once_exemption.py` pin both.
- `pipeline/escalation.py::_stamp_first_dispatch` and
  `pipeline/local_success.py` carry the pre-escalation tier attribution, with
  `test_ld90_escalation_pre_tier_stamp.py` and `test_ld90_tier_attribution.py`.
- `_resolve_story_branch` is shared by `merge.py`, `review_orchestrator.py`
  and `escalation.py`, the PR-branch fix intact.
- `brief_patched` is a first-class classifier reason
  (`pipeline/local_success.py`), backed by `pipeline/service.py`.

Seventy test files match review/rework/donebar/gate. The family's low rate
describes the work it *did* — hard gate logic on a weak tier — not defects in
what it shipped.

### 2.3 The event-aware drop is small and explainable

13/21 manifest → 12/21 with events → 10/21 with journals:

- `TGE-1` (`tick-grade-exemption`): `brief_patched` — the brief was rewritten
  mid-flight, not a first-pass clean run.
- `e7d46fdd` (`mode29-review-story-done-guard`) and `MODE-24-28`
  (`reviewer-finding-target-guard`): `step_cap_reached` journal entries. Both
  are on-device.

No story moves for a reason outside those two classes. The correction's
aggregate story (events and caps, nothing else) holds within this family.

### 2.4 Two stories are clean on a technicality worth naming

`FSU-01` and `TGE-1` both carry `brief_patched`; `FSU-01` additionally carries
`brief_rewrite_marker`. Both are correctly not-clean. But `FSU-01` is the only
story in the family whose brief was rewritten on a *review-feedback* signal
rather than a step cap — the same class as the mode 56 journal gap, and the
classifier can only see it because the rebrief leaves a marker in the brief.
There is no structural record equivalent to the journal entry; if a future
rebrief variant stops leaving the marker, the story would read clean. This is
an observation about the instrument, not a request for work.

## 3. What this retro does not say

- It does not line-by-line review the gate code; it verifies each deliverable
  exists and is tested, and reads the plans' event history.
- It cannot separate the on-device tier's weakness from the difficulty of the
  gate work these stories did. The two explanations are correlated by
  construction.
- It does not cover plans completed after 2026-09-21; the weekly window retros
  cover those.

## 4. Recommendations

1. **No new work for this family.** Every deliverable holds, and its losses are
   the tier effect already addressed by ODT-1 ("not cleared for dispatch") and
   by the 2026-10-19 switch of all roles to one non-Claude family.
2. **Keep gate/rework logic off the on-device tier** when it is ever
   re-enabled. This family is a second data point: 7 of 12 on-device stories
   hit the step cap.
3. **Treat `brief_patched` as the structural sibling of `step_cap_reached`
   when the instrument is next touched** (§2.4). Not scoped work now.

## Appendix — per-story record

`clean` is event-aware. `cap` means the journal holds a `step_cap_reached`
entry.

| Plan | Story | Tier | Clean | Reasons | Cap |
|---|---|---|---|---|---|
| check-story-status-acceptance-fail-review-samesha | 3869c76b | Claude (out) | — | — | |
| e2e-validate-reviewer-auto-fix | bcca562e | on-device | yes | — | |
| fix-review-backend-model | 95ae445e | Claude (out) | — | — | |
| fsu-retry-once-suite-exemption | FSU-01 | on-device | no | `brief_patched`, `brief_rewrite_marker` | |
| gate-aware-donebar-rejection | 5bfcaae2 | on-device | no | `step_cap_reached`, `step_cap_rebrief` | yes |
| harness-rework-gates | HRG-1 | cloud-oss | yes | — | |
| harness-rework-gates | HRG-2 | cloud-oss | yes | — | |
| mfr-measurement-freshness-review | MFR-01 | on-device | no | `step_cap_reached`, `step_cap_rebrief` | yes |
| mfr-measurement-freshness-review | MFR-02 | on-device | no | `step_cap_reached`, `step_cap_rebrief` | yes |
| mfr-measurement-freshness-review | MFR-03 | on-device | yes | — | |
| mfr-measurement-freshness-review | MFR-04 | on-device | yes | — | |
| mfr-measurement-freshness-review | MFR-05 | on-device | no | `step_cap_reached`, `step_cap_rebrief` | yes |
| mfr-measurement-freshness-review | MFR-06 | cloud-oss | no | `escalated`, `step_cap_reached`, `step_cap_rebrief` | yes |
| mfr-measurement-freshness-review | MFR-07 | on-device | yes | — | |
| mode29-review-story-done-guard | e7d46fdd | on-device | no | `step_cap_reached` | yes |
| openpr-worktree-branch-fix | OPR-01 | cloud-oss | no | `escalated`, `legacy_message`, `step_cap_rebrief` | |
| retro-merge-append-hook | RETRO-01 | Claude (out) | — | — | |
| review-commit-hygiene-autofix | 9b651a6a | on-device | no | `step_cap_reached`, `step_cap_rebrief` | yes |
| review-feedback-rework-done-bar | RFW-1 | cloud-oss | yes | — | |
| review-feedback-rework-done-bar | RFW-2 | cloud-oss | yes | — | |
| reviewer-finding-target-guard | MODE-24-28 | on-device | no | `step_cap_reached` | yes |
| reviewer-prompt-no-test-rerun | 8a4f2802 | cloud-oss | yes | — | |
| test-collection-gate-fix | d793cea5 | Claude (out) | — | — | |
| tick-grade-exemption | TGE-1 | cloud-oss | no | `brief_patched` | |
| tick-grade-exemption | TGE-2 | cloud-oss | yes | — | |
