# Harness review — Sep 30 to Oct 18, 2026

Window retro in the shape of
[`harness-review_2026-09-29.md`](harness-review_2026-09-29.md), written under
the drain policy in `retros/PENDING.md` (set 2026-09-29). **Window:** every
plan that finished between 2026-09-30 and 2026-10-18 inclusive. **Coverage:**
the 22 plan-completion markers listed in §6, which this document discharges.

Population: the `~/.claude/plans/*.manifest.json` whose `repo_root` is
`~/git/fagan`, stories with `dispatched_at >= 2026-09-30`. First-pass-clean
numbers come from `pipeline.local_success.classify_story` over that
fagan-only population. The window held **62 stories across 22 plans. Every
one is `done`**; no plan is parked or abandoned. Test suite at `7a81e9f`:
14515 passed, 15 skipped, 80 s; `ruff check .` clean.

---

## 1. Timeline

1. **Sep 30 – Oct 1** — `review-skip-gate-fingerprint` (2) and
   `retro-2026-09-29-harness` (15) land. The latter is the Sep 29 review's
   own P0–P3 list turned into a plan; it is the window's most expensive plan
   (§2.2). RH-08 adds the two-tier 1000-line CI gate (`410ab92`).
2. **Oct 1** — `harness-gaps-2026-09-30` (2) completes.
3. **Oct 5–6** — bench role pinning (#1067–#1071); `latch-repeat-view-advisory`,
   `latch-guard-onesided-range`, `create-file-false-nudge` all merge Oct 6,
   addressing the latch/nudge false-positive family. `pr-title-conventional-commits`
   (`1ebe3f4`) derives the PR title from the branch's Conventional Commits
   subject. `housekeeping-2026-10` (5) scrubs home paths and re-renders launchd.
4. **Oct 7** — busiest day: `bench-published-matrix` (8),
   `oracle-harness-done-bar-and-subject` (3), `pipeline-gate-and-deferral-defects`
   (3), `scheduler-startup-reconcile-bound`, `claude-resource-status-model-tag`
   (a review-gate crash), `bench-grid-fail-fast`, `harness-no-import-time-syspath`.
5. **Oct 8** — `scheduler-liveness` (3) and `test-author-detach` (5): the
   test-author phase becomes a detached, non-blocking step in `dispatch_story`.
6. **Oct 9** — `wedge-claude-done` and `merge-ci-gate-fail-closed` (3). An
   unreadable CI status now parks the merge instead of being read as "no CI".
   v0.7.0 is cut (`c5e4c97`).
7. **Oct 17–18** — dependency bumps; `fagan-ruff-0-17-target` (2) lands
   #1116/#1117.

---

## 2. Learnings

### 2.1 The on-device tier has nearly stopped being dispatched, and still goes 0 for 9

| Tier | Stories | First-pass clean |
|---|---|---|
| on-device (`gpt-oss-20b-high`, by first-dispatch tag) | 9 | **0 / 9** |
| cloud-oss (`deepseek-v4.1-flash:cloud`, `glm-5.3*:cloud`) | 43 | **38 / 43 = 88.4%** |
| all in-population | 52 | 38 / 52 = 73.1% |

Against the Sep 29 window (on-device 41.5% over 41 stories, cloud-oss 88.6%
over 79), the cloud-oss rate is unchanged and the on-device share fell from
34% of the population to 17%. The 9 on-device stories are all escalations
whose first dispatch was on-device; none was clean. 20 of the 62 stories ran
on `sonnet`, which is outside the local-success population by definition, so
the headline 73.1% is not comparable to Sep 29's 72.5%: the denominator
changed character. **Learning:** quote the two tiers separately, as the Sep 29
review recommended (its item 5). Nine for nine escalated is a small sample, but
nothing in this window argues the on-device tier recovered.

Reason histogram over the 52, computed from manifest fields only (the event
sidecar was not loaded, so `story_parked` and `brief_patched` events are not
counted here; the plan reports show parks, e.g. every RH-09 to RH-14 story):
`escalated` 14, `step_cap_rebrief` 1. The Sep 29 double-counting of
`brief_patched` against `step_cap_rebrief` was not re-measured against the
event stream.

### 2.2 `retro-2026-09-29-harness` cost 48 rework cycles and 9 escalations to deliver 15 stories

The plan's report records `total_rework_cycles=48, total_escalations=9` for
15 stories. By comparison, `merge-ci-gate-fail-closed` (3 stories) had 0 and 0,
`test-author-detach` (5) had 1 and 0. The expensive stories were two groups:

- **The six file splits** (RH-09 to RH-14: `app/dashboard.py`,
  `scripts/local_agent.py`, `scripts/local_agent_oracle.py`,
  `pipeline/story_status.py`, `pipeline/dispatch.py`,
  `pipeline/worktree_patch.py`). Every one escalated and was `story_parked`
  first. This is the edit shape `agent-dispatch-story-sizing.md` already warns
  about (editing deep inside ~1000-line files), so the failure was predictable
  from the rules, not a surprise.
- **Three instrument stories** (RH-02 split rebrief events, RH-04 repo-filter
  the report, RH-07 relabel `effective`): each escalated and parked once.

The outcome is good: all six production files are now under the limit, and
the only files over 1000 lines today are `REFERENCE.md` (1848) and five test
files (largest `test_pipeline_mcp_server_reverify_build.py`, 1252), plus
`tests/benchmark/harness.py` at 1009. Those are in the gate's warn-only tier.
**Learning:** a plan that is "fix every item the retro listed" mixes cheap
instrument fixes with the hardest edit shape the repo has. Sequencing the
splits as their own plan on a stronger tier from the start would have avoided
most of the 48 cycles. This was a plan-authoring choice, not an executor defect.

### 2.3 Most of the Sep 29 recommendations shipped, but the failure-mode catalog does not say so

Status of the Sep 29 §3 list, checked against master:

| # | Item | State |
|---|---|---|
| 0 | Unblock retro feedback sink | done (earlier) |
| 1 | Anchor rebrief-header detectors | done: `^` + `re.MULTILINE` in `pipeline/rebrief.py:512` and `local_success.py` |
| 2 | Split `brief_patched` by cause | done (RH-02, #1050); `brief_patched` still emitted at `pipeline/service.py:805` for operator patches only |
| 3 | Env-conflict guard diffs sources, not a catalog | **done in shape**: `scheduler_daemon._report_env_conflicts` now iterates `set(plist) | set(mcp)` minus `_BENIGN_ONE_SIDED_DIFFS`. Not exercised against the live plist in this review |
| 4 | Repo filter on success report | done (RH-04): `--repo`, `--all-repos` |
| 5 | Resolve tier asymmetry deliberately | **open**; `PIPELINE_LOCAL_MAX_STEPS` documented default is still 40 in `REFERENCE.md:1376` |
| 6 | Unfreeze failure-mode catalog | done: test asserts `len(entries) >= 57`; modes 56–60 present |
| 7 | Bound the retro backlog | **partly done** (RH-06, `ca0c0b8`): one notification when the marker count crosses `_RETRO_BACKLOG_NOTIFY_AT = 100`. It is a notification, not a cap, and it re-arms only after the backlog drains below 100; `PENDING.md` still holds about 185 markers after this document removes 22, so it will not fire until then |
| 8 | CI line-count gate | done (RH-08, `scripts/check_line_limit.py`, `ci.yml:99`) |
| 9 | Re-split over-limit files | done for production Python (§2.2) |
| 10 | Label `effective_env_config` field | done (RH-07) |
| 11 | Secrets out of `~/.claude.json` | open, out of repo scope |

The gap: `docs/failure_modes.json` still lists Modes 56, 57, 58, 59 and 60 as
`NOT fixed — PROPOSED 2026-09-29 (pinned, not yet shipped)`. Items 1, 2, 3
and 6 are the fixes for 57, 58, 59 and 60 respectively, and they are merged.
The catalog is the document an operator reads to learn whether a mode is live,
and today it is wrong about four of five. This review did not edit it: it needs
each mode's `status` and `fix_ref` set to the merged PRs, and the dataset test
pins the file, so it is its own small story. Mode 56 (a step-cap kill is
invisible to the classifier) is partly addressed by the anchoring fix but its
path-coverage question from the Sep 22 retro was not re-verified here.

### 2.4 An open PR shows the stale-commit-message gap live, on the Python-version floor

PENDING.md's "Harness gaps with no plan yet" lists *a rescoped story keeps its
round-1 commit message and nothing checks it* (OA2-05, Sep 22). `1ebe3f4`
(pr-title-conventional-commits) derives the PR title from the branch's commit
subject, which fixes squash-title drift, but does not compare the title to
the final diff. Evidence it is still live: **PR #1119**, open at the time of
writing, is titled "docs: revert out-of-scope CLAUDE.md change"; its diff
touches `README.md` only, and changes the prerequisite line from
"Python 3.11+" to "Python 3.12+". The branch carries two commits, "docs: state
Python 3.12+ as the supported minimum" and the revert one; the title is the
second.

That PR also pulls on a real inconsistency on master: `pyproject.toml` pins
ruff `target-version = "py312"`, CI tests 3.12–3.14, while the merged commit
`b744afc` says "require Python 3.11" and `README.md:492` and `CLAUDE.md` say
3.11+. Whether the supported floor is 3.11 or 3.12 is a decision for the
maintainer. It is not made here. The retro's point is narrower: a reviewer
looking at #1119's title would not expect a README floor change.

### 2.5 What the window says about merge safety

`merge-ci-gate-fail-closed` (3 stories, 0 rework, 0 escalations) closed the
case where `gh` CI status queries ran in the wrong repo (`0407e1c`) or came
back unreadable (`d920434`) and were treated as "no CI". This is the
fail-securely rule from `CLAUDE.md` applied to the merge gate, and the earlier
"approve_merge restarts CI on rebase" memory note is consistent with the
repaired behaviour. `scheduler-liveness` and `scheduler-startup-reconcile-bound`
removed two ways a scheduler could look dead or hang at startup (health written
before first reconcile; dead-owner dispatch lease no longer treated as live).

---

## 3. Harness improvement areas

### P0 — correctness of the record

1. **Update Modes 56–60 in `docs/failure_modes.json`.** Set `status` and
   `fix_ref` for 57 (anchoring), 58 (RH-02), 59 (env-conflict diff) and 60
   (dataset floor) to the merged work; leave 56 open until path coverage is
   confirmed. One story, one file plus its dataset test. (§2.3)
2. **Decide the supported Python floor (3.11 vs 3.12)** and make
   `pyproject.toml`, `README.md:492`, `CLAUDE.md` and ruff's target agree. Then
   close or retitle #1119. (§2.4)

### P1 — gates that compare the claim to the change

3. **Check a PR title against the final diff.** The simplest durable check:
   when the diff's file set has no overlap with the paths named in the title or
   the story's `files`, flag it at the review gate. This is the OA2-05 gap
   from PENDING.md, now with a second instance. (§2.4)
4. **Resolve the on-device step budget deliberately** (carried over from Sep 29
   item 5). 0/9 here, 17/41 then. Either raise `PIPELINE_LOCAL_MAX_STEPS`
   for the tier and re-measure, or stop dispatching it for work it has not
   cleared and say so in `REFERENCE.md`.

### P2 — process

5. **Drain the backfilled backlog.** RH-06's notification cannot fire again
   until `PENDING.md` falls below 100 markers, and about 185 remain (most are
   the 2026-08-12 backfill). Discharge them by family via window retros, as the
   drain policy allows, rather than adding more code. (§2.3)
6. **Plan the next "implement the retro" plan in two parts:** cheap
   instrument/label fixes on the cloud tier, structural file splits on a
   stronger tier from the start. (§2.2)

---

## 4. What worked

- **Delivery:** 62 of 62 stories done across 22 plans; the suite is green and
  lint clean at HEAD.
- **Cloud-oss held at 88.4%** on 43 stories, essentially the same as the
  88.6% measured on Sep 29.
- **Fail-closed merge gate** and the scheduler liveness fixes landed with zero
  rework (§2.5).
- **The line-limit rule is now enforced, not advisory:** production Python has
  no file over the limit and the CI gate (`ci.yml:99`) prevents the reversion
  the Sep 29 review documented. (§2.2)
- **Detached test-author phase** (`test-author-detach`, 5 stories, 1 rework
  cycle) landed as a clean chain of small steps.

---

## 5. Status

- **Modes fixed this window:** 57, 58, 59, 60 are fixed in code but still
  recorded as proposed (§2.3, P0 item 1). Mode 56 stays open.
- **New modes:** none. The one new observation (§2.4) is a second instance of
  an already-tracked gap, not a new class.
- **Not verified here:** whether `_report_env_conflicts` fires against the
  installed plist; the per-story detail behind the 14 `escalated` flags beyond
  the plan reports; any plan outside `repo_root = ~/git/fagan`.

## 6. Plans discharged

triage-git-evidence-base-branch, language-agnostic-gates,
review-skip-gate-fingerprint, retro-2026-09-29-harness,
harness-gaps-2026-09-30, latch-repeat-view-advisory,
latch-guard-onesided-range, create-file-false-nudge,
pr-title-conventional-commits, bench-grid-fail-fast,
claude-resource-status-model-tag, housekeeping-2026-10,
oracle-harness-done-bar-and-subject, scheduler-startup-reconcile-bound,
harness-no-import-time-syspath, pipeline-gate-and-deferral-defects,
bench-published-matrix, scheduler-liveness, test-author-detach,
wedge-claude-done, merge-ci-gate-fail-closed, fagan-ruff-0-17-target.
