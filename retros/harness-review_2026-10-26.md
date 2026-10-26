# Harness review — Oct 19 to Oct 26, 2026

Window retro in the shape of
[`harness-review_2026-10-18.md`](harness-review_2026-10-18.md), written under the
drain policy in `retros/PENDING.md` (set 2026-09-29). **Window:** every plan that
finished between 2026-10-19 and 2026-10-26 inclusive. **Coverage:** the 46
plan-completion markers listed in §6, which this document discharges.

Population: the pipeline-self-repo plans that carry a completion marker for this
window. **133 stories across 46 plans, every one `done`; no plan abandoned.**
First-pass-clean numbers come from `pipeline.local_success.classify_story` over
manifest fields plus each plan's notification sidecar (present for all 46 plans)
and step-cap journals. Test suite at `9f19279`: 17,064 passed, 15 skipped, 80 s;
`ruff check .` clean.

Why the population is larger than the previous window: the Aug 12 backfill has
drained to nothing and the 2026-10-22/23/24 plans were mostly many-story plans
(`guided-install` 13, `external-context-sources-phase1` 7,
`notification-channels` 8, `dashboard-worktree-diff` 7).

---

## 1. Timeline

1. **Oct 19** — six small closes: `on-device-tier-guidance`,
   `mode56-step-cap-journal-classifier`, `escalation-same-target-noop`,
   `review-gate-notice-transition`, `legacy-reason-precision`, and
   `security-audit-fixes-2026-10` (3).
2. **Oct 20** — six one-story fixes: `product-analyst-model-pin`,
   `review-verdict-fallback`, `chat-turn-cap-answer`, `usage-gate-parser-fix`,
   `usage-banner-model-grouping`, `scheduler-stale-checkout-alert`.
3. **Oct 21** — the edit-guard and catalog day: `replace-lines-symbol-loss-ungated`,
   `mode22-create-file-content-loss`, `mode32-omission-marker-guard`,
   `mode45-acceptance-rematerialize`, `mode49b-base-restore`,
   `mode50-ci-fail-feedback-merge`, plus two catalog syncs. All one-story.
4. **Oct 22** — the window's heaviest day, 37 stories: `guided-install` (13),
   `dashboard-worktree-diff` (7), `dashboard-sidebar-settings` (5),
   `dashboard-pin-guard-and-split` (4), `pipeline-auto-merge-off` (4), and four
   one-story fixes.
5. **Oct 23** — `cross-plan-awareness` (5), `topology-collision-guards` (4),
   `local-fleet-branch-safety` (4) + its `-verify`, `check-story-status-terminal`.
6. **Oct 24** — `external-context-sources-phase1` (7) and `-phase2` (6),
   `notification-channels` (8), plus registration and test-isolation follow-ups.
7. **Oct 26** — 36 stories in one day: `parked-story-recovery` (7),
   `harness-resilience` (7), `enterprise-prep-tier1` (3), `enterprise-plan-approval`
   (7), `forge-abstraction` (6), `tenancy-seams` (5), `tenancy-seam-fix` (1).

---

## 2. Learnings

### 2.1 Quote the tiers separately; the on-device tier partially recovered

| Tier | Stories | First-pass clean |
|---|---|---|
| on-device (`gpt-oss-20b-high`) | 21 | **11 / 21 = 52.4%** |
| cloud-oss (`deepseek-v4.1-flash:cloud`) | 72 | **60 / 72 = 83.3%** |
| in-population total | 93 | 71 / 93 = 76.3% |
| outside population (`claude`/sonnet) | 40 | not graded (by definition) |

Against the previous window (on-device 0/9, cloud-oss 88.4% over 43, in-population
73.1%), the on-device tier moved from 0% to 52% on a sample more than twice as
large, and cloud-oss gave back 5 points. The in-population total is slightly up.
**Learning:** the on-device recovery is real but partial, and it is still the
weaker tier by 31 points; the 5-point cloud-oss dip is within the noise this
instrument has shown across windows and is not evidence of a regression.

### 2.2 Parks, not step caps, are the dominant non-clean reason

Event-aware reason histogram over the 133 stories: `escalated` 17,
`story_parked` 15, `brief_patched` 6, `step_cap_rebrief` 3,
`brief_rewrite_marker` 3, `step_cap_reached` 1.

The 15 parks cluster in three plans and one family:

- `external-context-sources-phase1/2` — 6 parks (CL-12, CL-14, CL-15, CL-20,
  CL-23, CL-24), out of 13 stories;
- `guided-install` — SETUP-02 and SETUP-03 (both also `step_cap_rebrief`);
- `parked-story-recovery` — HR-03, HR-04, HR-07;
- `forge-abstraction` — FORGE-S1B, FORGE-S3.

That first cluster is what produced `parked-story-recovery` in the first place
(the CL-14/CL-20/CL-23/CL-24 `files` failures documented in
`.claude/rules/pipeline-story-schema.md`). **Learning:** the dominant loss is no
longer "the model ran out of steps"; it is "the story's declared scope did not
match where the work lived, and the agent had no way to say so". A park is the
symptom; the plan author's `files` derivation is the cause.

### 2.3 The prior-findings guard re-parked a finished branch ten hours after the recovery plan merged

`FORGE-S3` is the window's clearest harness defect, and it is a *review-side*
version of the same class §2.2 describes.

Sequence: the story was dispatched, failed, and reworked — the rework
(`cd35b96`, 2026-10-26 08:36) resolved the actual finding and pushed a green
branch. `parked-story-recovery` had merged its last story at 00:51 the same day,
including the `amend_scope` path and the `SCOPE-CONFLICT` marker for exactly the
"the brief is missing a file I may not touch" situation. The plan's declared
scope was then amended by hand to include `pipeline/forge_github.py`, and the
branch's copy of that file became **byte-identical to `master`** — nothing left
to change. The gate still re-parked the story:

```python
# pipeline/review_orchestrator.py:686
diff_res = subprocess.run(["git", "diff", "--name-only", story["last_reviewed_sha"], "HEAD"], ...)
untouched = [p for p in story["last_review_findings"] if p not in changed_files and not _is_test_file_path(p)]
if untouched:
    verdict = "REQUEST_CHANGES"   # "Prior Blocking finding(s) were never addressed"
```

`pipeline/forge_github.py` never appears in `changed_files`, so it stays
"unaddressed" forever, and every subsequent tick re-parks a branch that already
passes its suite. It took manual manifest surgery (`last_review_findings`,
`review_feedback`, `review_verdict`, triage counters) to let it merge as #1257.

The guard already contains the escape it needs, for a different case: a flagged
*test* file is exempt because "the full suite … is provably green right now …
strictly stronger … proof the finding is resolved than 'were the file's own
bytes touched'". The scope-amendment case has equally objective proof — **the
file no longer differs from the base branch** — and it is currently ignored.
**Learning:** a guard that requires a change to clear a finding can deadlock on
a finding that is moot. Every "was it touched" gate needs a "is there anything
left to touch" escape.

### 2.4 A Homebrew interpreter upgrade silently broke the MCP tick path

Homebrew installed `python@3.14.10` at 10:20 on Oct 26 and **deleted the 3.14.9
tree**. Two long-lived processes were running from that removed path: the
scheduler daemon (started Oct 24) and the MCP server (started Oct 25 22:50).
Because already-imported modules keep working, both looked healthy — read-only
MCP tools answered normally — while any *lazily* imported stdlib submodule failed
with `ModuleNotFoundError`. The MCP server's `advance_all_plans` returned
`No module named 'importlib.resources'` for **every non-paused plan**; the paused
ones returned before reaching the failing import.

Scope of the blast radius, stated precisely: the MCP path is proven broken; the
daemon is proven still working at 11:21 (plans merged at 13:53 and after the
10:20 upgrade, so its import set was still sufficient) and is a *latent* instance
— the same deleted path would fail on the next new submodule import. Restarting
both on 3.14.10 cleared it. The daemon's per-plan exceptions are not logged
(`advance_all_plans` returns them in the tick result), so a daemon-side failure
would also have been silent.

**Learning:** an interpreter path in `ps` is not evidence of a working
interpreter. A long-lived process can outlive the tree it was started from, and
its failure mode is per-import, silent, and indistinguishable from success for
everything it has already loaded.

### 2.5 The cleanest plan of the window was the one shaped by the rules

`tenancy-seams` went 5/5 clean, 0 escalations, 0 parks, on cloud-oss — the largest
of the window's two fully-clean multi-story plans (the other is
`enterprise-prep-tier1`, 3/3), and it followed the playbook literally: one
production file per story (`app/identity.py` twice, `app/policy.py`,
`app/dashboard.py`), ≤3 new symbols, a spec-only story carrying `[no-new-tests]`,
and an impact run in a scratch worktree before ingest that found the one real
constraint (`app/auth.py` may import no `app.*` module — which the first
implementation attempt did, and which the brief then forbade by name).

`harness-resilience` (6/7) and `enterprise-plan-approval` (5/7) sit behind it, and
`forge-abstraction` (4/6) behind them, with its two non-clean stories being the
review-state incidents in §2.3 rather than model failures.

**Learning:** the shape rules transfer across tiers. The two clean multi-story
plans were the ones split by file and by concern before dispatch, not after.

### 2.6 The record

- **23 of the 36 plans that carry in-population stories were fully clean.**
- **46 markers discharged** (§6). Three markers remain outside this window:
  two dated 2026-10-18 (`failure-mode-catalog-sync`,
  `python-311-floor-ci-and-ruff`), owed to the previous window's retro, and one
  harness-gap line. They are not reviewed here.
- **Instrument caveat, stated because it affects this window's numbers:** during
  the FORGE-S1B/S3 recovery the manifest counters were reset by hand
  (`rework_attempts`, `triage_attempts`, review records). The manifest therefore
  reads `0` rework cycles for `forge-abstraction`; the true count is unknown.
  The classifier above reads notification sidecars and journals, so the
  `story_parked` and `escalated` reasons survive — the counter reset did not
  erase them — but any future rework-rate metric for this window would be wrong.
- **Catalog bookkeeping:** the 2026-10-21/22 run needed five `PR #<n>` placeholder
  fill-ins across two days. The standing recommendation from that week still
  holds: a closing story records the **commit hash** (knowable before merge) and
  omits the PR number.

### 2.7 One test-authoring lesson worth keeping from `mode32-stall-bound-tests`

Recorded here rather than lost with the marker's inline note: the first version
of that story's timeout test patched the **shared stdlib** `time` module
(`b.time.sleep` / `b.time.monotonic`). Inside an xdist worker that reached
pytest, subprocess and httpx in the same process, and failed an unrelated
env-sharing test on the 3.12 CI job. The merged version patches the module's own
reference with a fake object instead, confining the clock to `backend_ollama`.
**Learning:** when faking time (or any stdlib singleton) in a test, patch the
consumer module's reference, never the stdlib module's attributes — the blast
radius of the latter is every thread in the worker, not the test.

---

## 3. Harness improvement areas

### P0 — gates that can deadlock, and processes that outlive their interpreter

1. **Let a moot finding clear.** In `pipeline/review_orchestrator.py`, exempt a
   flagged non-test file from the "was it touched" check when
   `git diff --quiet origin/<default> -- <path>` reports no difference: the file
   is already identical to the base branch, so the finding cannot be addressed
   in it, exactly as a green suite proves the test-file case. Add a unit test for
   each branch (touched / untouched-and-differs / untouched-and-identical), and
   keep the fail-open behaviour on a git error. Evidence: FORGE-S3, §2.3.
2. **Refuse to start on a missing interpreter tree.** At the top of
   `pipeline/scheduler_daemon.py` and `app/pipeline_mcp_server.py`, assert that
   `Path(sys.base_prefix)` exists (`importlib.resources`' own package directory
   is enough); if it does not, log one ERROR naming the missing prefix and exit
   non-zero, so launchd's `KeepAlive` restarts the process on the current
   interpreter instead of it serving stale imports for hours. Evidence: §2.4.

### P1 — shape and record

3. **Size a story by its effective tier, not its own `backend` field.** The
   ingest-time `_story_sizing_warning` gate read only `story["backend"]`, so
   every story that inherits the process-wide tier (registry
   `roles.dispatch`, `PIPELINE_BACKEND_DISPATCH`) skipped the check —
   including all five stories of this retro's own P0 plan. The headroom half
   of this item was wrong as written: `scope_headroom_warning` already covers
   every declared `.py` path, `app/` included. What does need watching is that
   `app/dashboard.py` is **908** lines, not 880, and `pipeline/service.py`
   971; either will hit the 1000-line gate mid-story, the CL-14 failure by a
   different file. Addressed in `harness-instrument-fixes` (HIF-01).
4. **Record manual manifest surgery.** A human unblocking a parked story by
   clearing `rework_attempts` / `last_review_findings` (allowed, but invisible)
   currently corrupts the retro instrument (§2.6). An append-only sidecar event
   (`story_unparked_manually`, with the cleared field names) would keep the record
   honest without forbidding the intervention.

### P2 — process

5. **Drain status.** This retro discharges 46 markers; the backlog is now the
   three 2026-10-18/19 lines noted in §2.6. The next window is Oct 27 onward.
6. **Keep the two instrument rough edges on the backlog**
   (`legacy_message` double-counting, `brief_patched` having no structural
   equivalent) — neither justifies work on its own, and both were live again in
   this window's histogram.

---

## 4. What worked

- **Delivery:** 133 of 133 stories done across 46 plans, 0 abandoned; suite green
  and lint clean at HEAD.
- **The line-limit rule held.** No production Python file is over 1000 lines
  (`pipeline/server.py` 994, `scheduler_daemon.py` 999, `service.py` 971), and the
  two additions that needed room (`ENT-LC-00`'s `TOOLS` registration loop,
  `TEN-WIRE-01`) made it rather than crossing it.
- **The impact run paid for itself** on `tenancy-seams`: one full-suite run in a
  scratch worktree found the `app/auth.py` import rule before dispatch, and the
  brief then named it. Five clean stories followed.
- **The one-story close pattern** (28 of the 46 plans were a single story) kept
  the window's median plan cheap even on the weak tier.
- **`parked-story-recovery` and `harness-resilience`** shipped 14 stories that
  removed several ways a story could silently stall — but see §2.3: the review
  side of the same class is still open.

---

## 5. Status

- **Not verified here:** the per-plan detail behind the 17 `escalated` flags
  beyond the plan reports; whether the daemon (as opposed to the MCP server) ever
  failed a tick during the interpreter window; any plan outside
  `repo_root = ~/git/fagan` (the markers are pipeline-self-repo by construction).
- **Carried forward from the previous window:** the two `legacy_message` /
  `brief_patched` instrument edges, and the commit-hash-not-PR-number convention
  for closing stories.
- **New this window:** P0 items 1 and 2, P1 items 3 and 4. None is registered as a
  plan.

---

## 6. Plans discharged

The 46 completion markers this document discharges, with their story counts:

```
escalation-same-target-noop (1)            legacy-reason-precision (1)
mode56-step-cap-journal-classifier (1)     on-device-tier-guidance (1)
review-gate-notice-transition (1)          security-audit-fixes-2026-10 (3)
chat-turn-cap-answer (1)                   product-analyst-model-pin (1)
review-verdict-fallback (1)                scheduler-stale-checkout-alert (1)
usage-banner-model-grouping (1)            usage-gate-parser-fix (1)
failure-mode-catalog-modes-52-54 (1)       failure-mode-catalog-sync-2 (1)
mode22-create-file-content-loss (1)        mode32-omission-marker-guard (1)
mode45-acceptance-rematerialize (1)        mode49b-base-restore (1)
mode50-ci-fail-feedback-merge (1)          replace-lines-symbol-loss-ungated (1)
dashboard-pin-guard-and-split (4)          dashboard-sidebar-settings (5)
dashboard-worktree-diff (7)                failure-mode-catalog-placeholders (1)
guided-install (13)                        mode32-stall-bound-tests (1)
mode36-agent-log-scorecard (1)             pipeline-auto-merge-off (4)
scheduler-heartbeat-flake-bound (1)        check-story-status-terminal (1)
cross-plan-awareness (5)                   local-fleet-branch-safety (4)
local-fleet-branch-safety-verify (1)       topology-collision-guards (4)
external-context-sources-phase1 (7)        external-context-sources-phase2 (6)
notification-channels (8)                  notification-channels-registration (1)
notification-config-test-isolation (1)     enterprise-plan-approval (7)
enterprise-prep-tier1 (3)                  forge-abstraction (6)
harness-resilience (7)                     parked-story-recovery (7)
tenancy-seam-fix (1)                       tenancy-seams (5)
```

Totals: 46 plans, 133 stories.
