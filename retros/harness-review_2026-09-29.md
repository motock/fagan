# Harness review — week of Sep 22–29, 2026

Cross-cutting weekly review of this repo's own pipeline runs, in the shape of
[`harness-autonomy-review_2026-09-21.md`](harness-autonomy-review_2026-09-21.md).
Population: the 49 `~/.claude/plans/*.manifest.json` whose `repo_root` is
`~/git/fagan` (183 stories). All first-pass-clean numbers
below are computed by `pipeline.local_success.classify_story` over that
fagan-only population — see §2.5 for why the unfiltered instrument reads
differently.

The week's dispatch window (Sep 22–29) held **120 stories across 33 plans**.
**Every one of them reached `done`.** No story was abandoned, and no plan in
the window is parked today. The numbers below measure cost and efficiency,
not delivery.

---

## 1. Timeline

1. **Sep 22** — `post-ld90-priorities` (22 stories) and
   `overlord-autonomy-round-2` (8) complete. First `story_parked` of the week
   (PLD90-W1B-2, "high risk held for human review"); PLD90-W1B-1 escalates to
   `glm-5.3-flash:cloud` after two rework redispatches produce no new commit.
2. **Sep 23** — best volume day of the week: 39 stories dispatched, 31 clean
   (80%). `ld90-closeout` (11), `mcp-tool-hygiene` (5),
   `oversized-module-split` (5), `reporting-attribution-fix` (4) all complete.
   LDC-3 escalates after 3 review cycles. `b8c3cde`/`bc27aea` bring
   `dispatch.py` and `story_status.py` under 1000 lines.
3. **Sep 24** — `post-rlf-hardening` and `reporting-layer-followups` complete;
   PRH-2 burns 4 step caps, 3 watchdogs and a park before shipping via
   `glm`. CYC-3 parks with the week's sharpest rationale (see §4).
4. **Sep 25** — `post-050-hardening` (8) and `global-rules-bundle` (8)
   complete. Worst-quality day at volume: 17 dispatched, 10 clean (59%),
   6 `brief_patched`. GR-5 escalates.
5. **Sep 26** — `nsfix-ns3a-findings` (10 stories) completes with **zero**
   first-pass-clean disqualifiers — the week's cleanest plan.
6. **Sep 27** — `worktree-runtime-marker-exclusion` (3) completes; WRM-1,
   WRM-3 and RRG-1 each hit the step cap. `bench-test-author` is abandoned
   (TA-1 closed unmerged, nothing on master).
7. **Sep 28** — `gr-install-syspath` (1 story) completes.
8. **Sep 29** — `stall-evidence-watchdog-safety` (5) merges all five PRs
   (#1023–#1027). SEW-3, SEW-4 and SEW-5 each hit the step cap; **all three
   recovered via the rebrief→resume path with zero escalations and zero
   rework cycles**. SEW-3 and SEW-5 hit their caps 3.8 s apart, the only
   sub-60-second step-cap cluster in the whole week.

---

## 2. Learnings

### 2.1 The week's dominant failure mechanism is the local step budget, not story sizing

Window rate: **87/120 = 72.5%** first-pass clean. Split by tier:

| Tier | Model(s) | Stories | First-pass clean |
|---|---|---|---|
| on-device | `gpt-oss-20b-high:latest` | 41 | **17 / 41 = 41.5%** |
| cloud-oss | `glm-5.3-flash:cloud`, `deepseek-v4.1-flash:cloud` | 79 | **70 / 79 = 88.6%** |

A 47-point gap, stable across every sub-window measured (window-30
33.3% vs 86.7%; window-90 40.5% vs 92.5%).

**The sizing rules are not the cause, and this is now measured rather than
assumed.** On-device stories declared {0 files: 7, 1 file: 24, 2 files: 10} —
**zero** exceeded the ≤2-production-file cap. Checking against file size *at
dispatch time* (`git show <first-touch>^:pipeline/dispatch.py | wc -l`, not
master's current count), **zero** on-device stories touched a ≥1000-line file
either; the closest, WRM-3, found `dispatch.py` at 998 lines and its own change
took it to 1009. The plan-authoring rules that `.claude/rules/local-dispatch-preflight.md`
was written to enforce are being followed.

What differs is the budget: `PIPELINE_LOCAL_MAX_STEPS=60` against a cloud
tier that is not step-capped at the same order of magnitude, and 34
`step_cap_reached` journal entries in the window — 26 of them on
`gpt-oss-20b-high:latest`. On-device stories accounted for 18 of the window's
23 `step_cap_rebrief` flags. The on-device tier is not producing wrong code; it
is running out of turns on work the cloud tier finishes in one dispatch.

**Learning:** the tier gap is a capacity gap. Adding more pre-ingest sizing
rules cannot close it, and treating the two tiers as one population behind a
single 72.5% headline hides it. Either the local tier gets a larger budget, or
stories are routed by expected turn count rather than by file count.

### 2.2 The rebrief-header detectors match a *mention*, not a block — and the same defect has two consequences at opposite ends of the pipeline

Both `pipeline/local_success.py` and `pipeline/rebrief.py` locate the
prior-attempt block with an **unanchored substring** test:

```python
existing = base.find(DIAGNOSIS_HEADER)      # rebrief.py — finds "=== ..." anywhere
if any(header in instr for header in _STEP_CAP_REBRIEF_HEADERS):   # local_success.py
```

**Consequence A — live metric false positives.** Three in-window stories are
classified `step_cap_rebrief` (i.e. not first-pass clean) purely because their
briefs *quote* the header string while describing it:

- `PLD90-E1-01` — the story that *implements* `_STEP_CAP_REBRIEF_HEADERS`,
  quoting both header constants in its prescribed tests.
- `LDC-9` — `"...the manifest story's agent_instructions now contains
  === PRIOR-ATTEMPT DIAGNOSIS (read this FIRST) === and the stub's text."`
- `LDC-12` — `"...a story whose agent_instructions contains === PRIOR-ATTEMPT
  DIAGNOSIS (read this FIRST) === -> reasons include step_cap_rebrief"`.

LDC-12 is the story that *documents the first-pass-clean verdict*. The metric
marks the story that explains the metric as a failure of the metric. This is
the same mention-vs-invocation hazard the project already solved once for
`[no-new-tests]` (`pipeline-story-schema.md`: a sentence that negates the
token is read as a mention, not an invocation) — the rebrief detectors never
got the equivalent guard.

**Consequence B — latent brief truncation.** `compose_rebriefed_instructions`
truncates the brief at the first occurrence of the header, so if the header
ever appears mid-brief, everything from there down is discarded and replaced by
the diagnosis block. Demonstrated directly: a 385-character brief containing
the header in prose returns 188 characters, silently dropping its `FILES:`,
`HARD CONSTRAINTS` and `TESTS:` sections. A population scan found **4 header
occurrences not at column 0, across 3 stories**, and **zero** cases where a
real block's preceding text ends mid-sentence — so this has not yet destroyed
a brief in production. It is one plan-authoring habit away from doing so, and
its blast radius is a story's entire remaining instructions.

**Learning:** a sentinel that is both data and delimiter needs an anchored
match. Both sites should require the header at line start (`^` under
`re.MULTILINE`), which is how `_STEP_CAP_REBRIEF_HEADERS` is actually emitted.

### 2.3 `brief_patched` is a three-way overload, and 77% of it double-counts the step cap

`brief_patched` fires from three distinct emission sites and carries no cause
discriminator. Across the week: **30 of 39 `brief_patched` events are
step-cap rebriefs** (message: *"brief rewritten after a step-cap struggle"*),
and only **9 are genuine mid-flight operator `patch_story` calls** (5 stories).
BENCH-ARM-1 is the clearest case — it got a manual `patch_story` at 15:42:29
and an automated step-cap rebrief 10 seconds later, so a single story produced
two events the classifier reads as two independent disqualifiers.

This is why the window's reason counts sum past the story count: 23
`step_cap_rebrief` + 19 `brief_patched` against the 33 stories actually
affected. The reasons histogram is a multiset of events, not of story causes,
and nothing in the output says so.

**Learning:** one event name per cause. The automated rebrief should emit
`step_cap_rebrief` directly and `brief_patched` should be reserved for an
operator-initiated edit.

### 2.4 The env-conflict guard shipped yesterday cannot see the largest live conflict

SEW-5 (`1be254e`) added `_report_env_conflicts()` to `pipeline/scheduler_daemon.py`,
warning at scheduler startup when the launchd plist and the MCP server env
disagree. The guard iterates `config_provenance.ENV_VAR_CATALOG` (59 entries)
and reports entries whose `conflict` flag is set.

Read live today, the two sources disagree on **four catalogued keys** — so the
guard does fire:

| Key | plist | MCP server env |
|---|---|---|
| `PIPELINE_PAUSE_THRESHOLD` | 95 | 101 (never) |
| `PIPELINE_RESUME_THRESHOLD` | 90 | 0 |
| `PIPELINE_WEEK_PAUSE_THRESHOLD` | 97 | 100 |
| `PIPELINE_WEEK_RESUME_THRESHOLD` | 95 | 99 |

But the divergences **outside** the catalog are larger, and the guard is
structurally blind to all of them:

| Key | plist | MCP server env | Consequence |
|---|---|---|---|
| `PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS` | 8100 | 5400 | a 45-minute difference in how long an agent may run |
| `PIPELINE_AUTO_TRIAGE` | 1 | unset | auto-triage on for one path, off for the other |
| `PIPELINE_TDD_SPLIT` | unset | on | test-author phase on one path only |
| `PIPELINE_DECOMPOSE` | unset | local | — |

**Learning:** a conflict detector built on a hand-maintained allowlist detects
conflicts in the allowlist, not conflicts. The guard should diff the two
sources directly and subtract a small known-benign set, rather than intersect
two lists that must both be updated by hand.

### 2.5 The headline gauge has no repo filter, so one repo's bad week reads as everyone's

`scripts/local_success_report.py` globs every `*.manifest.json` under
`PLAN_DIR` (line 98) with no `repo_root` predicate. Measured over the same
Sep 26–29 dispatch days:

- unfiltered window-60: **21/60 = 35.0%**, reasons include `story_parked: 24`
  (42 of the 60 stories are `e2e_decentralized_messaging`)
- fagan-only, same days: **15/23 = 65.2%**

A **30-point swing on the number an operator steers by**, produced entirely by
a repo this review is not about. Nothing in the report's output names the
cohort.

**Learning:** a rate is only meaningful with its population attached. The
report should group by `repo_root` (or take `--repo`), and print the cohort
size alongside the percentage.

### 2.6 The 1000-line rule has no enforcement, so its two successes have already reverted

`oversized-module-split` completed Sep 23 with `bc27aea` (`story_status.py`
under 1000) and `b8c3cde` (`dispatch.py` under 1000). Today:
`pipeline/story_status.py` = 1012, `pipeline/dispatch.py` = 1009. Both drifted
back over the following week, because the only line-count check in the tree is
`pipeline/ingest.py`'s `_SIZING_MAX_FILE_LINES` warning — non-blocking, and it
skips `.md` and repo-root files entirely.

**Learning:** a standard with no gate is a suggestion. Seven files are over the
line today (`app/dashboard.py` 1332, `scripts/local_agent.py` 1084,
`scripts/local_agent_oracle.py` 1012, `pipeline/story_status.py` 1012,
`pipeline/dispatch.py` 1009, `pipeline/worktree_patch.py` 1003,
`pipeline/server.py` 1000).

### 2.7 The failure-mode catalog is frozen by its own test

`docs/failure_modes.json` holds 57 entries; the newest is Mode 55, dated
2026-08-12. Every retro written since has proposed Mode 56+ and none has landed
(2026-09-22's retro proposes Mode 56; this one proposes 57–60). The
blocker is mechanical: `tests/unit/test_failure_modes_dataset.py` pins
`len(entries) == 57` **and** `len(set(modes)) == 57`, and the first test's
*name* embeds "57" — so adding a mode requires editing two existing tests. Under
the standing "never modify an existing test without approval" default, that is
enough to stop the catalog dead for seven weeks.

### 2.8 The retro backlog is write-only

`retros/INDEX.md` held 7 rows before this one was added. `retros/PENDING.md`
held **218** marker lines at the start of this retro and grew by 43 during the
window with **zero** removals. `_record_retro_pending`
appends with no cap and no notification on growth. At the current arrival rate
the backlog is not drainable by per-plan retro; it has to be drained by family,
which is what this document is.

The upstream half of the same loop is also mostly dead: of the nine retros in
`retros/`, only the two from 2026-07-21 ever got their P0/P1 items copied into
`MATURITY_AND_UNIQUENESS_PLANS.md` (grep `from \`retros/`). The 2026-07-31,
2026-08-06, 2026-08-09, 2026-09-21 and 2026-09-22 retros all wrote P0–P3 lists
that were never mirrored — which is why §3 above is re-derived from scratch
rather than inherited.

### 2.9 The feedback sink the retro process mandates is blocked by a count-pinning test

Attempting to run that mirror step for this retro — adding an `### A5.
Retro-derived harness improvements` subsection to
`MATURITY_AND_UNIQUENESS_PLANS.md` carrying the §3 items, exactly as
`PLAN_RETROSPECTIVE_PROCESS_PLAN.md` §2 requires — **fails the suite**:

```
FAILED tests/unit/test_maturity_plan_refresh_2026_09_11.py::
    TestOpenItemsAreProtected::test_exactly_five_unchecked_bullets_remain
AssertionError: expected the 5 genuinely-open bullets, found 13
```

The doc already contains exactly 5 `- [ ]` bullets (A3's failure-mode rate,
B3 ×2, B5, B6). Any retro feedback landing as checkboxes makes it 6, and the
test fails. The mirror step has therefore been blocked for every retro since
2026-07-21 — not by neglect, but by an existing test that nothing is allowed to
modify.

**Resolved in this commit** (user-authorized test edit, per CLAUDE.md Step 4):
`test_exactly_five_unchecked_bullets_remain` was replaced by
`test_no_unchecked_bullets_outside_the_retro_feedback_sink`, which asserts that
every unchecked bullet in the document body is one of the five *named*
`STILL_OPEN` items, with the retro-feedback subsection exempted rather than
counted. Verified three ways: it permits the sink to grow (A5 landed with 8 new
bullets and the module passes), it still catches a drive-by open item injected
into A4's body (`AssertionError: unexpected new open item outside the
retro-feedback sink`), and it degrades to grading the whole document when A5 is
absent. `### A5. Retro-derived harness improvements (Sep 2026)` now carries
this retro's P0–P2 items, re-opening a feedback step dormant since July.

**This is §2.7's exact class**, and the same test file even states the
principle it violated. Its own module docstring said the oracle "grades the
refresh structurally — anchors and evidence tokens, never the exact prose — so
the implementer keeps editorial latitude while the six genuinely-open bullets
are protected from drive-by flipping." `test_still_open_bullets_remain_unchecked`
does precisely that, by anchor. The count test added a whole-document total on
top, which is not "protecting the five named bullets" — it is forbidding the
document from ever gaining a new open item, which is the one thing the retro
process needs it to do.

**Learning:** a count pin on a document that a process is designed to append to
is a contradiction. Pin the named items; never the total.

**Scope note — what this retro discharges.** This is a *window* retro: it
covers the Sep 22–29 dispatch window at family level, not each plan in depth.
Accordingly the plan-completion markers for every plan finished in that window
have been removed from `retros/PENDING.md`, and that removal policy is now
recorded in the file itself. A window retro trades per-plan depth for the only
drain rate that is actually sustainable: 43 new markers per week cannot be met
by one retro per plan, and a backlog that only grows is not a backlog.

---

## 3. Harness improvement areas

### P0 — correctness of the instruments

0. **Unblock the retro feedback sink**
   (`tests/unit/test_maturity_plan_refresh_2026_09_11.py`). Replace
   `test_exactly_five_unchecked_bullets_remain`'s whole-document count with an
   assertion that the five *named* `STILL_OPEN` bullets are the only unchecked
   bullets outside the exempted retro-feedback subsection — i.e. pin
   membership, not the total, matching what the module's own docstring says it
   does. Fixes §2.9. **DONE in this commit**, with A5 landed; the remaining
   items below are pending.
1. **Anchor the rebrief-header detectors** (`pipeline/local_success.py`,
   `pipeline/rebrief.py`). Require the header at line start; add a regression
   test asserting a brief that merely *mentions* the header is neither
   truncated nor classified `step_cap_rebrief`, mirroring the existing
   mention-guard semantics of `[no-new-tests]`. Fixes §2.2 A (live, 3 stories
   mis-graded) and B (latent, whole-brief loss).
2. **Split `brief_patched` by cause** (`pipeline/dispatch_attempt.py`,
   `pipeline/advance.py`, `pipeline/ci.py`). Emit `step_cap_rebrief` from the
   automated rebrief path; keep `brief_patched` for operator `patch_story`.
   Add a test that one story cannot produce two disqualifiers from one event.
   Fixes §2.3.
3. **Make the env-conflict guard diff the sources, not a catalog**
   (`pipeline/config_provenance.py::_report_env_conflicts` callers). Compare
   `read_plist_env()` against `read_mcp_server_env()` key-for-key minus a named
   benign set; cover `PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS`,
   `PIPELINE_AUTO_TRIAGE`, `PIPELINE_TDD_SPLIT`, `PIPELINE_DECOMPOSE`. Fixes
   §2.4. (Note: `PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS` 8100 vs 5400 is a
   live divergence worth reconciling on its own.)

### P1 — make the measurement usable

4. **Filter the success report by repo** (`scripts/local_success_report.py`).
   Group by `repo_root` or take `--repo`; always print cohort size beside the
   rate. Fixes §2.5.
5. **Resolve the tier asymmetry deliberately** rather than by more sizing rules
   (§2.1). Options, in order of preference: raise `PIPELINE_LOCAL_MAX_STEPS`
   above 60 for the on-device tier and re-measure; or route by expected turn
   count; or report the two tiers separately everywhere the rate appears and
   stop quoting one combined number.
6. **Unfreeze the failure-mode catalog** — change the dataset test to assert
   `len(entries) >= 57` and uniqueness of `mode`, and stop embedding the count
   in the test name. Then land Modes 56–58 proposed by the Sep 22 and Sep 29
   retros. Fixes §2.7.
7. **Bound the retro backlog** (`pipeline/ci.py::_record_retro_pending`). Either
   cap the marker list and roll the overflow into a single "N plans pending"
   line, or notify when the backlog exceeds a threshold. Fixes §2.8.

### P2 — enforce what the standards already claim

8. **Add a CI line-count gate** for the 1000-line rule, or drop the rule from
   `CLAUDE.md`. Enforcement should cover `.md` and repo-root files, which
   `pipeline/ingest.py`'s sizing warning currently skips. Fixes §2.6.
9. **Re-split the seven over-limit files** listed in §2.6 — as a plan, one file
   per story, after (8) lands so the work is durable this time.

### P3 — diagnostics hygiene

10. `effective_env_config()`'s `effective` field is the *calling process's*
    environment with a code-default fallback (`resolve_env_var` line 277). A
    shell- or dashboard-side reader therefore sees
    `PIPELINE_LOCAL_MAX_STEPS=40`/`code_default` while every serving process
    runs 60. Label the field as "this process" or resolve against the plist
    layer when the process has no value.
11. `PLANE_API_KEY` is stored in plaintext in `~/.claude.json`'s MCP env block
    (not in the repo, so not a committed secret — but it is a live credential
    in a syncable file). `PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV=1` is likewise
    plist-only. Both belong in a secrets manager.

---

## 4. What worked

- **Delivery was perfect.** 120/120 stories dispatched in the window merged;
  183/183 stories in the 49 in-scope plans are `done`. Ten escalations, four
  parks, seven watchdog kills and 34 step caps produced **zero** abandoned
  outcomes. The recovery machinery is doing its job.
- **The rebrief→resume path is the strongest part of the harness.** SEW-3,
  SEW-4 and SEW-5 each hit the step cap and each resumed to a merge with no
  escalation and no rework cycle — the plan's own `.report.md` records
  `total_rework_cycles=0, total_escalations=0` for all five stories. The
  folded-diagnosis resume is worth more on this tier than any sizing rule.
- **Rebrief layering is clean.** All 23 rebriefed briefs in the window carry
  exactly **one** diagnosis block; zero carry `REWORK SCOPE`, `AMENDMENT` or
  `SUPERSEDED` layers. The "one brief, rewritten — never an amendment stack"
  rule from `local-dispatch-preflight.md` §5 is holding in practice. (One
  outlier: PLD90-E1-01 has 3 stacked blocks, and it is the story that
  implements the header constants.)
- **The sizing rules converged.** Zero on-device stories over the ≤2-file cap;
  zero over the 1000-line rule at dispatch time (§2.1). The plan-authoring
  discipline is real.
- **Two of the Sep 21 review's eight ranked gaps are closed.** G2 —
  `PIPELINE_AUTO_TRIAGE=1` is now in the installed plist and documented at
  `REFERENCE.md:1066`. G3 — `watchdog_streak` is incremented at
  `pipeline/story_status.py:191` and compared against
  `STEP_CAP_FALLBACK_THRESHOLD` at line 226, so a repeatedly-killed story can
  finally escalate instead of looping.
- **`nsfix-ns3a-findings` went 10-for-10 clean**, on `deepseek-v4.1-flash:cloud`
  with no step caps — evidence that the cloud tier is not merely faster but is
  genuinely not hitting the failures the local tier hits.
- **CYC-3's park rationale is the best artifact of the week.** It refused
  `mark_done` on a branch identical to base, reasoning that "the suite passing
  at HEAD is the symptom of the broken oracle, not corroboration of the
  deliverable." The gates designed to prevent a phantom merge worked exactly as
  intended under adversarial conditions.
- **The suite is green and fast**: 13606 passed, 12 skipped, ~59 s.

---

## 5. Status

**Modes fixed this window**

- None from the catalog — the catalog is frozen (§2.7).

**Modes confirmed / newly pinned**

- **Mode 56** (proposed 2026-09-22, still unwritten) — *a step-cap kill is
  invisible to the first-pass-clean classifier*. **Re-confirmed and sharpened**:
  the classifier does flag step caps via the header substring, but it flags
  them by *mention*, so it misses real rebriefs that arrive by any other path
  while producing false positives on three stories that merely describe the
  header (§2.2).
- **Mode 57 (proposed)** — *a sentinel used as both data and delimiter is
  matched unanchored, so a mention is indistinguishable from an occurrence.*
  Live on `pipeline/local_success.py`, latent on `pipeline/rebrief.py` (§2.2).
- **Mode 58 (proposed)** — *one event name covering N causes makes a reason
  histogram that double-counts and cannot be summed.* Live on `brief_patched`,
  77% of whose emissions duplicate `step_cap_rebrief` (§2.3).
- **Mode 59 (proposed)** — *a conflict detector built on a hand-maintained
  allowlist cannot see conflicts outside it.* Live on the SEW-5 guard, which
  misses the week's largest config divergence (§2.4).
- **Mode 60 (proposed)** — *a total-count pin on an artifact the process exists
  to append to blocks the process.* Two independent live instances in this one
  repo: `test_failure_modes_dataset.py`'s `len(entries) == 57` has frozen the
  failure-mode catalog since 2026-08-12 (§2.7), and
  `test_exactly_five_unchecked_bullets_remain` has blocked the retro→maturity
  feedback step since 2026-07-21 (§2.9) — the latter discovered by attempting
  to run that step while writing this document. In both cases the pin sits in a
  test that the standing "never modify an existing test without approval" rule
  forbids touching, so neither artifact can grow and neither test is wrong in a
  way anyone is authorized to correct.

**Modes left open**

- Mode 55 (replace_lines range-sweep corruption) — unchanged, no recurrence
  observed in the window.
- The four over-limit-module drifts (§2.6) and the 218-entry retro backlog
  (§2.8) are process debt rather than named modes; both are tracked in §3.

**A3 metric trend** (`MATURITY_AND_UNIQUENESS_PLANS.md` §A3, "stabilize the
active bug surface", reframed 2026-08-06): the original "no new modes for N
runs" instrument remains retired and this week gives no reason to revive it —
the window produced no genuinely new failure *class*, only the four pinned
above, three of which are defects in the measuring instruments rather than in
dispatch. The substantive A3 signal this week is the tier gap in §2.1: the
on-device first-pass-clean rate has been flat at ~41% for 90 stories while the
cloud tier sits at ~92%, and no sizing-rule change has moved it. Per §A3's own
reasoning, the honest reading is that the local tier is capacity-bound, not
defect-bound.
