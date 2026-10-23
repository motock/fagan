# Plans awaiting a retro

Durable backlog for the retro process described in
`docs/plans/PLAN_RETROSPECTIVE_PROCESS_PLAN.md` §2.1. Going forward,
`mark_story_done` appends a line here automatically (with a real
completion date) whenever a pipeline-self-repo plan's last story goes
done. **Remove a plan's line once its retro is written and added to
`retros/INDEX.md`** — this file is a to-do list, not an archive.
`_record_retro_pending` writes those markers at end-of-file, so no
section added here may be the last one in the file.

**Drain policy (set 2026-09-29).** A *window* retro —
`retros/harness-review_<date>.md`, covering a stated dispatch window at
family level — discharges every plan-completion marker whose plan finished
inside that window. Per-plan retros remain the richer option when a single
plan warrants one, but they cannot keep pace with the arrival rate (43 new
markers in the week of Sep 22–29 against zero removals), and a backlog that
only grows stops being a to-do list. A window retro must state its window
and its coverage in its own body. Backfilled markers predating 2026-08-12
are discharged by family as the relevant subsystem is reviewed.

## Harness gaps with no plan yet

One-off harness defects observed during incident work that still need
either a plan or a retro write-up. Remove a line once one exists.

_None outstanding_ — the rescoped-commit-message gap (OA2-05) was addressed by the warn-only `pr_title_mismatch` notification (PR #1123). The scheduler-heartbeat wall-clock flake was fixed by plan `scheduler-heartbeat-flake-bound`.


## Abandoned plans

Plans stopped before completion. `mark_story_done` never fires for
these, so no marker is written automatically. Remove a line once its
retro is written.

_None outstanding_ — `bench-test-author` was discharged by
`retros/harness-review_2026-09-29.md`.

## Plan completion markers

The entries below are a one-time backfill (2026-08-12) of every
pipeline-repo plan that was already fully done before the automated
marker existed. Their true completion dates predate this file and were
not individually reconstructed — check `git log` on the relevant PR/files
if a precise date is needed when writing one of these up.

- failure-mode-catalog-sync — completed 2026-10-18, 1 story
- python-311-floor-ci-and-ruff — completed 2026-10-18, 1 story
- pr-title-diff-mismatch-warning — completed 2026-10-18, 1 story
- on-device-tier-guidance — completed 2026-10-19, 1 story
- mode56-step-cap-journal-classifier — completed 2026-10-19, 1 story
- escalation-same-target-noop — completed 2026-10-19, 1 story
- review-gate-notice-transition — completed 2026-10-19, 1 story
- legacy-reason-precision — completed 2026-10-19, 1 story
- security-audit-fixes-2026-10 — completed 2026-10-19, 3 stories
- product-analyst-model-pin — completed 2026-10-20, 1 story
- review-verdict-fallback — completed 2026-10-20, 1 story
- chat-turn-cap-answer — completed 2026-10-20, 1 story
- usage-gate-parser-fix — completed 2026-10-20, 1 story
- usage-banner-model-grouping — completed 2026-10-20, 1 story
- scheduler-stale-checkout-alert — completed 2026-10-20, 1 story
- replace-lines-symbol-loss-ungated — completed 2026-10-21, 1 story

<!-- Context for replace-lines-symbol-loss-ungated's eventual window retro (marker stays until then): Mode 55 fix shipped in PR #1151 (commit 28560f8). `_dropped_top_level_defs` now runs unconditionally before the `confirm_removals` gate in both `scripts/local_agent_tools.py` and `scripts/local_agent_oracle_tools.py`, so a rename that classifies as a rewrite can no longer silently drop top-level symbols. 12 new tests (6 cases × 2 variants), full suite 14657 passed / 0 failed, ruff clean. -->
- failure-mode-catalog-modes-52-54 — completed 2026-10-21, 1 story

<!-- Context for failure-mode-catalog-modes-52-54's eventual window retro (marker stays until then): catalog drift, PR #1152 (commit 1fb1696). Modes 52/53/54 were all fixed in code in Aug 2026 (PRs #301/#303/#305) plus Mode 54's replace_lines path on 2026-10-21 (#1151), but the dataset still read "NOT fixed". Three anchored entry syncs + one pre-authorized spot-check re-pin; 11-line diff, full suite 14657 passed / 0 failed. Standing check worth adopting: the catalog is hand-synced, so any mode closed by a plan needs an explicit dataset update in that same plan. -->
- mode45-acceptance-rematerialize — completed 2026-10-21, 1 story
- failure-mode-catalog-sync-2 — completed 2026-10-21, 1 story

<!-- Context for mode45-acceptance-rematerialize's eventual window retro (marker stays until then): PR #1153 (commit 0d01d34). Root cause pinned: `resuming` in pipeline/dispatch.py is true when the worktree directory exists, so a still-`todo` story re-dispatched after a dead attempt skipped acceptance-fixture materialization and could grade the base branch's stale fixture to a false green. The skip is now gated on the status-based `resumed_story` and each preserve/materialize decision is logged at INFO. 4 new tests; full suite 14661 passed / 0 failed. -->

<!-- Context for failure-mode-catalog-sync-2's eventual window retro (marker stays until then): PR #1154 (commit 6d5eb95). Mode 22 corrected to PARTIAL (non-.py files still have no content-preservation check) and the `PR #<n>` placeholders on Modes 45/55 filled with #1153/#1151. This is the second hand-synced catalog repair in one day; the standing recommendation is still to make "closing a mode updates docs/failure_modes.json" part of the closing plan. -->
- mode22-create-file-content-loss — completed 2026-10-21, 1 story

<!-- Context for mode22-create-file-content-loss's eventual window retro (marker stays until then): PR #1155 (commit 9c27770). create_file now rejects a non-.py overwrite whose content drops at least max(2, 10%) of the old non-blank lines, reusing edit_guards.classify_removed_lines with the confirm_removals escape; .py keeps its existing top-level-symbol guard. 12 new tests (6 cases x 2 variants). One follow-up: mode 22's fix_ref still carries the `PR #<n>` placeholder and needs #1155 on the next catalog pass. Pattern note: three merge-time placeholder fill-ins in one day - consider having the closing story record the commit hash instead of a PR number, which is knowable before merge. -->
- mode50-ci-fail-feedback-merge — completed 2026-10-21, 1 story

<!-- Context for mode50-ci-fail-feedback-merge's eventual window retro (marker stays until then): PR #1156 (commit a5033c6). The merge-gate CI-fail branch now merges prior review_feedback ahead of the CI error via _merge_ci_rework_feedback, dropping only a superseded previous CI block, instead of replacing it. 6 new tests including an integration test that drives the real adjudication tick; one pre-authorized survivor-list needle updated in test_notification_event_names.py. Mode 50 stays PARTIAL because no diagnosis role runs on the CI failure itself. Placeholder follow-up: mode 50's fix_ref needs #1156. -->
- mode49b-base-restore — completed 2026-10-21, 1 story

<!-- Context for mode49b-base-restore's eventual window retro (marker stays until then): PR #1157 (commit 74f0e29). restore_file gained a from_base=true mode in both agent variants, restoring `git show origin/<default>:<path>` (symbolic-ref then origin/master then origin/main), so an agent whose own commits captured a bad change has a first-class safe route back to the base branch that the destructive-git block otherwise refuses. 14 new tests (7 cases x 2 variants); one pre-authorized description-pin update in test_restore_file_cap.py. Oracle-agent drift/churn guards remain absent, so Mode 49 stays REFRAMED. Placeholder follow-up: mode 49's fix_ref needs #1157. CI note: the macOS 3.14 job was cancelled at its 15-minute limit twice before succeeding on the auto-rerun - runner contention, not a test failure. -->
- mode32-omission-marker-guard — completed 2026-10-21, 1 story

<!-- Context for mode32-omission-marker-guard's eventual window retro (marker stays until then): PR #1158 (commit 19ec536). create_file, str_replace and replace_lines now reject a write whose final non-blank line is a truncation artifact ("# ... (rest of file remains same)" and variants), so a context-exhausted model can no longer land a silently truncated file that passes the syntax check. 10 new tests (5 cases x 2 variants); only the last line is examined so a mid-file mention of the artifact stays writable. Mode 32 stays PARTIAL: non-Ollama provider calls are still single blocking reads with no per-chunk silence bound, and no test pins the read-silence timeout. Placeholder follow-up: mode 32's fix_ref needs #1158. -->
- failure-mode-catalog-placeholders — completed 2026-10-22, 1 story

<!-- Context for failure-mode-catalog-placeholders' eventual window retro (marker stays until then): PR #1159 (commit ae5a127). Filled the last four `PR #<n>` placeholders (modes 22/32/49/50) with the merged numbers and hashes; `grep -c 'PR #<n>'` is now 0. Note for the retro: five placeholder fill-ins in two days - future briefs should have the closing story record the commit hash (known before merge) and let the PR number be omitted. Also note: approve_merge's acceptance reverify reported 2 transient failures in tests/unit/test_aider_harness_availability.py after the merge had already landed via the scheduler; they pass in isolation and in a full master run (14703 passed / 0 failed), so that was a flake, not a regression. -->
- mode32-stall-bound-tests — completed 2026-10-22, 1 story

<!-- Context for mode32-stall-bound-tests' eventual window retro (marker stays until then): PR #1160 (commit 0226d35). Two test files now pin the Ollama streaming read timeout (READ_SILENCE_SECONDS reaches httpx.Timeout.read) and the backend's single global retry deadline (sleeps capped at the remaining role-call budget), in both agent variants where applicable; mutation checks confirmed each fails when its invariant is broken. Process lesson worth keeping: the first version patched the shared stdlib time module (b.time.sleep / b.time.monotonic), which reached pytest, subprocess and httpx inside the same xdist worker and made an unrelated env-sharing test fail on the 3.12 CI job. The merged fix patches b.time with a fake module object, confining the clock to backend_ollama - when faking time in tests, patch the module's reference, never the stdlib module's attributes. -->
- mode36-agent-log-scorecard — completed 2026-10-22, 1 story

<!-- Context for mode36-agent-log-scorecard's eventual window retro (marker stays until then): PR #1161 (commit 58a36d8). Added scripts/agent_log_scorecard.py (per-attempt steps, tool histogram, first mutation step, productive ratio, guard trips, outcome) plus parse regexes in pipeline/agent_log_format.py; the scorecard slices at the final [boot] like rebrief does, because agent.log is appended across resumes. Two review rounds were needed: round 1 fixed parking-vs-nudge precedence (real parking lines are indented and were counted as guard trips) and cid leakage into done_summary; round 2 counted guard-message step numbers; round 3 added the last-attempt scope. Process notes: (1) a CI-fail rework auto-dispatched and committed an unauthorized loosening of the scheduler heartbeat test - interrupted and reverted, and the flake is now a harness-gap backlog line; (2) master went red on an unrelated docs plan leaking a home path, fixed directly (PR-less docs commit 3ca57b8); (3) the placeholder fix_ref `PR #<n>` on mode 36 still needs #1161. -->
- scheduler-heartbeat-flake-bound — completed 2026-10-22, 1 story

<!-- Context for scheduler-heartbeat-flake-bound's eventual window retro (marker stays until then): PR #1162 (commit a79dcfc). test_worker_outliving_deadline_is_abandoned_at_deadline's upper bound moved from 0.55s to 1.0s for its 0.4s join deadline, after 15 parallel runs measured 0.436-0.597s (6 over the old ceiling). The bound still fails loudly on the real regression (the worker running its full 5s). The harness-gap backlog line was discharged in the same commit. -->
- pipeline-auto-merge-off — completed 2026-10-22, 4 stories
- dashboard-sidebar-settings — completed 2026-10-22, 5 stories
- guided-install — completed 2026-10-22, 13 stories
- dashboard-pin-guard-and-split — completed 2026-10-22, 4 stories
- dashboard-worktree-diff — completed 2026-10-22, 7 stories
- cross-plan-awareness — completed 2026-10-23, 5 stories
