# Harness autonomy cross-cutting review — 2026-09-21

Cross-plan review (not a single-plan retro): the last 7 days of commits,
the full plan/manifest population (278 manifests, 1005 stories), the
notification record (Sept 18–21), the 182-entry `PENDING.md` backlog, and
the overlord/triage/escalation/review-orchestration code paths at HEAD
(841b179). Prompted by the Sept 18–21 fix burst (ASB/HRG/RFW/TGE/FSU/
SST/SU/MFR/LD90/ESC) and the gpt-oss `num_ctx` raise (32k → 81920/slot
at `OLLAMA_NUM_PARALLEL=2`; 131072 reachable at parallelism 1).

## 1. Measurements (the honest part)

`scripts/local_success_report.py` at review time:

| Window | Cohort | Overall | On-device | Cloud-oss |
|---|---|---|---|---|
| 14 stories (~1 day) | 10/14 (71.4%) | 3/5 (60%) | 7/9 (78%) | — |
| 60 stories (Sept 17–21) | 38/60 (63.3%) | 15/23 (65.2%) | 23/37 (62.2%) | — |

- The 60-story window is **flat at the 2026-09-18 baseline (65%)**. The
  one-day 71.4% is promising but n=14 is noise, and nearly all of the
  fix burst landed Sept 20–21 — **there is no post-fix cohort yet, so
  no fix in the burst is outcome-proven.**
- Top disqualifiers in the 60-story window: `escalated` (13),
  `legacy_message` (11, pre-attribution notifications), `story_parked`
  (7), `brief_patched` (5), `brief_rewrite_marker` (5).
- Cloud-oss (62.2%) trails on-device (65.2%) in this cohort — bigger
  context alone does not produce first-pass-clean (see §5).
- The dominant failure mode in the notification record — "no new commit
  after 2–3 rework redispatches → escalate → park, needs human review" —
  hit every gptoss-num-ctx-ceiling story (Sept 18), three stories
  Sept 19, and ASB-2 as recently as Sept 21 afternoon. It persists.

Execution hygiene is healthy: 1003 stories done across 278 manifests;
only abandoned stories anywhere are the two deliberately-paused e2e
mobile clients (Aug 18); worktrees clean in all three repos; recent
parks all resolved same-day.

### Recent fixes verified solid in code

- Baseline exemption (ASB-1/2, TGE-1): fail-closed on every axis (no
  baseline, green baseline, unparseable ids, any new failing id);
  agent-side worktree marker and tick-side exemption share one parse.
- Retry-once suite exemption (FSU-01): re-runs a red suite once;
  reports the first run's output on a reproducible failure.
- Rework done-claim guard (HRG-1/2): wired into `resume_via_transcript`
  with fail-open parse; tick-side Mode 27 same-SHA guard routes
  no-progress redispatches to changes_requested and escalates at cap.
- Reviewer-feedback rework signal (RFW-1/2): the same round is graded
  on the bar the agent was held to — the Sept 21 oracle-green
  false-promotion class is closed.
- Reviewer transport deferral (LD90-W5): rate-limit, transient, and
  unexpected exceptions defer without burning inconclusive counts.
  (Gap G4 below: the security pass never received the same treatment.)
- Escalation repo-root fix (ESC-REPO-ROOT-01), tier attribution
  (MFR-*), dead-pid detached grading, dispatch lease / LOCKSTARVE
  per-tick cap — all verified.

## 2. Ranked remaining automation gaps (the overlord layer)

| # | Gap | Where | Severity |
|---|-----|-------|----------|
| G1 | `blocked_oracle` is a dead status: `patch_acceptance` — the recovery purpose-built for born-broken oracles — can never see one (triage candidates match only parked/failed/step-cap streak; dispatch ready-list excludes it) | `pipeline/dispatch.py:796`, `advance.py:287`, `triage.py:956` | Blocks entirely |
| G2 | The parked/failed triage ladder never runs: `PIPELINE_AUTO_TRIAGE` is off by design and absent from the installed plist. Every terminal park (rework exhaustion, review-inconclusive, no-new-commit, dispatch/merge exhaustion, rebase-conflict resume) has **no automated recovery**. Also: once `TRIAGE_MAX_ATTEMPTS=2` caps, nothing ever resets `triage_attempts` | `pipeline/triage.py:902`, installed `com.fagan.pipeline.advance-scheduler.plist` | Blocks entirely |
| G3 | Watchdog kills loop forever: the stale-activity/wall-clock path returns `interrupted` and increments **no** counter (not `dispatch_attempts`, not any streak) — a repeatedly-hanging agent is killed and resumed indefinitely; escalation never engages. This is the "keeps parking/crashing without writing code" signature | `pipeline/story_status.py:112` | Unbounded loop |
| G4 | Security-reviewer pass has none of the main reviewer's deferrals: no try/except around `_run_security_reviewer`; a broken security backend aborts the plan's review pass every tick and wedges high-risk stories in tests_passed forever; a non-rate-limited UNKNOWN verdict silently becomes REQUEST_CHANGES | `pipeline/review_orchestrator.py:575` | Blocks high-risk |
| G5 | Merge-park dead-ends: re-adjudication requires the exact reason string `"high risk held for human review"` + `PIPELINE_AUTONOMY=full`; triage excludes any story carrying `merge_park_evidence`. Parks with reason `not approved` / `risk above threshold` — and every merge park in gated mode — are unreachable by both | `pipeline/triage.py:960`, `advance.py:838`, `merge.py:457` | Edge → total in gated mode |
| G6 | `request_decision` has no fail-open (an overlord exception propagates as a raw tool error to the blocked agent; contrast `rule_on_story`'s fail-open to park_for_human) and never executes its ruling — the action is only logged. Local-backend agents have no request_decision tool at all | `pipeline/service.py:637` | Degrades silently |
| G7 | `give_up` and non-escalatable terminal failure are blind notify-only: the rebrief machinery exists and fires on step-cap/watchdog/escalation paths but not on plain surrender or exhaustion with escalation unavailable | `pipeline/advance.py:696`, `escalation.py:281` | Residual human queue |
| G8 | `repo_issue` rulings park "not implemented yet" (the executor was never built — OVERLORD_FAILURE_TRIAGE_PLAN.md §E7); triage's infra-skip branch can silently strand an already-parked story with a stale infra marker every tick; the Ollama `-np` probe is warn-only with no auto-fallback to concurrency 1 | `pipeline/triage.py:40,866`, `dispatch.py:739` | Edge cases |

Dead-status inventory: `parked` and `failed` are revisited only by triage
(off — G2); `blocked_oracle` by nothing (G1). Everything else has a
requeue path. No TODO/FIXME markers exist in `pipeline/`.

## 3. Retro-backlog analysis (what 182 unwritten retros say)

The retro loop is the bottleneck of the learning loop: newest actual
retro is 2026-08-09, ~125 plans completed since, 182 entries pending,
two completed plans missing from the marker entirely. Theme counts
over the backlog (multi-plan clusters, backfill + dated):

| Family | ~Plans | Recurrence signal |
|---|---|---|
| Review/rework/gate-grading loop | ~18 (local_review_*, reviewer-*, gate-aware-donebar, mode29, harness-rework-gates, RFW/TGE/FSU…) | **Largest repair cluster.** Eight weeks of point patches on individual gates; never a systemic treatment of the loop as one state machine. G2+G7 are the same family. |
| Watchdog/timing/resume | ~9 (test-gate-and-watchdog-hardening, scheduler-hang-hardening, async-dead-pid-grading, activity-watchdog, watchdog-stale-activity-floor, scheduler-reconcile-resilience, resume-read-grace…) | Same taxes paid repeatedly across Aug→Sept. G3 is the next instance. |
| Dispatch/escalation/locking races | ~12 (mode30 scheduler-git-race, scheduler-lock-starvation ×13 stories, dispatch-*, escalation gate fixes, leases) | Converging — lease + LOCKSTARVE work closed most; ESC-REPO-ROOT-01 was the residual. |
| Edit-corruption guards ("Mode N") | ~10 plans covering modes 22–55 (edit-guard-enforcement ×6 stories, mode25/27, mode31, mode52-55, strreplace-var-guard…) | Purely reactive: each new corruption mode gets a new guard. No registry links mode → family → open/closed, so the same families keep paying tax invisibly. |
| Config/env/provenance drift | ~12 (config-unification ×17 stories, transport-alias-deprecation, a2-*, w3a, env-file-routing…) | w3a's provenance work was systemic; residual churn is env-consumer bugs. |
| CI-green maintenance | 7 (ci-gate-*, ci-green ×3, macos-ci-reenable, ci-unmasked-failures) | Recurring; the version-pinning lesson in `.claude/rules/code-review.md` is the known antidote — verify it's actually enforced everywhere a merge-gating tool runs. |
| Token/context management | 5 (token-context-a1/closeout, resume-transcript-context-trim, local_model_tuning_table, gptoss_temperature_experiment) | Measurement-driven and converging; §5 extends it. |
| **Overlord/triage/autonomy** | **2** (overlord-failure-triage ×17, overlord-parked-story-autonomy ×9, both ≤ Sept 14) | **Least-invested layer relative to being the current #1 bottleneck.** §2's gap list is effectively overlord-failure-triage round 2. |

Enhancement recommendations distilled from the backlog:

1. **Retro in families, not per plan.** ~55 of 182 entries are 1-story
   plans; the per-plan retro cadence can't keep up and the signal is in
   the families anyway. Write one retro per family (review-loop,
   watchdog/timing, edit-guards, config-drift) at one paragraph per
   plan, and clear the backlog in ~8 documents instead of 182.
2. **Add a harness mode registry** (`docs/plans/HARNESS_MODE_REGISTRY.md`):
   one row per Mode (22–55+): family, root cause, fix plan, status.
   New incidents get classified against it — this converts the
   reactive guard-whack-a-mole into visible recurring-family telemetry.
3. **Give every kill/timeout path a convergence property.** The
   watchdog family recurs because kill paths don't converge: a fix for
   G3 should be phrased as the invariant ("every termination increments
   a counter that participates in a streak/cap"), then property-tested
   across all watchdog/timeout sites — not as another point patch.
4. **Re-balance investment toward the overlord layer.** Two plans in
   ~125 completions while it's the bottleneck; §2 G1–G7 is the backlog.
5. **The 48% lesson is still the incidence lever.** The 2026-09-18
   baseline attributed 48% of gate test failures to plan-authoring
   defects (pre-existing-test collisions). The Sept-18 preflight gate is
   the incidence fix; the Sept 18–21 burst is all recovery-side. Verify
   preflight compliance (the `Preflight:` line on every non-Claude
   story) before assuming 90% is reachable from harness fixes alone.

## 4. Next steps, ordered

1. Enable `PIPELINE_AUTO_TRIAGE` in the installed plist + add
   `blocked_oracle` to `triage_candidates` + a `triage_attempts` reset
   policy. (G1+G2 — one flag and two small changes give the entire
   parked/failed/blocked_oracle population a ladder.)
2. Watchdog streak counter (G3), phrased per §3.3's convergence
   invariant.
3. Security-reviewer deferral trio (G4) — copy the main reviewer's
   wrapper.
4. Widen merge-park re-adjudication to reason-match the gate's actual
   emitted reasons; let triage see merge-parked stories whose evidence
   doesn't match a live hold (G5).
5. `request_decision` fail-open + ruling execution; route give_up /
   terminal failure through the existing rebrief diagnosis (G6+G7).
6. Re-measurement gate: re-run the success report once ~60 stories have
   dispatched on post-2026-09-21 code; hold the 90% plan to that number.
7. Restart the retro loop per §3 (family retros + mode registry).

## 5. The 1000-line file cap vs the gpt-oss `num_ctx` raise

**Keep the cap for now — the 32k→132k raise addresses only one of the
cap's three failure mechanisms.**

- What the raise actually bought: dispatch injects the tuning-table
  `num_ctx` (81920/sslot) as `PIPELINE_TRANSPORT_NUM_CTX`, and the
  agent's trim budget is `NUM_CTX × chars-per-token × 0.75` — retained
  transcript grew ~98k → ~245k chars (~2.5×). That genuinely weakens
  **mechanism 2**: context eviction making earlier file-views stale
  (the stale-line-number corruption mode).
- What it did not touch: **(1)** `view_file` still truncates at 3000
  chars (`scripts/local_agent_tools.py`) — a 1500-line file needs ~20
  ranged views regardless of context; **(3)** the 60-step on-device cap
  is unchanged, so those views eat a third of the step budget before
  editing starts, and read-heavy park nudges still fire.
- Strongest evidence the cap was never purely context: the sizing
  rule's incidents (~1500-line file, step caps/watchdog
  timeouts/escalation, no exceptions) reproduced **on cloud open-source
  models that never had a 32k limit**. Cloud-oss at 62.2% in the
  current window confirms it.
- The cap is also cheap: it's an advisory tier-routing nudge
  (`_SIZING_MAX_FILE_LINES = 1000` warning at ingest); its only cost is
  routing big-file stories up a tier.

**Experiment (decision rule, not a vibe):**

1. Scale `view_file`'s truncation cap with the transport budget —
   e.g. `max(3000, NUM_CTX // 8)` chars (~10k at 81920), same for the
   ranged path. Small, testable, and it targets the actual bottleneck
   the rule proxies.
2. Dispatch 2–3 on-device stories editing 1000+ line files
   (append-a-method shapes, not anchored-interior insertion, per
   `.claude/rules/agent-dispatch-story-sizing.md`), graded by
   `first_pass_clean` in the success report.
3. ≥2 of 3 first-pass-clean → raise `_SIZING_MAX_FILE_LINES` and soften
   the rules-file cap (to ~2000 lines, or tier-conditional). Still
   failing → the mechanism is view-truncation/steps, not context: the
   next fix is an anchored-view/search tool, not a bigger context.

## Scope notes

- This review covers the fagan repo's harness; several Sept plans
  (smoke-*, registry-single-source-of-truth, etc.) target the pipeline
  MCP-server repo and are out of `PENDING.md`'s scope by design.
- Baseline metrics cited from the 2026-09-18 measurement
  (`docs/plans/LOCAL_DISPATCH_90_PLAN.md` context): 65% first-pass-clean
  flat Aug→Sep; 28% of stories merged only after escalation/park; 7%
  only after a human patched the brief; 48% of gate test failures in
  pre-existing test files; docs missing in ~48% of change requests;
  missing wiring/dead code 16%; scope creep 12%.