# Harness retro — resume-rebase remote sync (2026-09-22)

A look-back at the `resume-rebase-remote-sync` plan (2 stories, PR #894 /
#895, merged commit `4bbbe05`). Both stories merged and the fix is verified
live, but the plan's real subject turned out to be the *instrument*: the
first-pass-clean metric this harness is steered by cannot see the failure
that produced SRB-2, and is overstating the current rate by roughly ten
points.

**Not a committed plan doc — a retro.** Read alongside
`MATURITY_AND_UNIQUENESS_PLANS.md` (A3: failure-mode rate),
`docs/failure_modes.json`, and `project_dispatch_failure_modes` Mode 31.

---

## 1. Timeline (what happened)

All times UTC, quoted from
`~/.claude/plans/{overlord-autonomy-round-2,resume-rebase-remote-sync}.notifications.jsonl`.

1. **2026-09-21 19:03:30** — `OA2-01` (plan `overlord-autonomy-round-2`)
   dispatches. Its test-author phase falls open ("role unconfigured or
   resolves to the same backend as dispatch"), so it runs monolithically.
2. **19:22:02–19:22:24** — `OA2-01`'s merge gate fails 3x on a *different*
   defect class: acceptance reverify `ImportError: cannot import name
   'run_triage_sweep'` (the triage↔server circular-import idiom, since
   addressed by the seed-prompt rule in `pipeline/persona.py`).
3. **19:26:18–19:30:30** — triage fails open (`RuntimeError`); attempts (2)
   reach the cap → `parked` for human.
4. **23:00:00** — brief patched (`patch_story`, `brief_patched` event).
5. **23:01:16** — the resumed dispatch rebases: *"story OA2-01 worktree base
   predates origin/master by 4 commit(s); rebased onto origin/master before
   resume."* **Nothing pushes the rewritten branch.**
6. **23:09:55** — merge gate fails a 4th time: *"rebase: Auto-merging
   tests/unit/test_oa2_triage_blocked_oracle.py CONFLICT (add/add)"*. The
   conflict is the agent's own doing — `4c0ea9b` on `agent/oa2-01`,
   *"Merge remote-tracking branch 'origin/agent/oa2-01' into agent/oa2-01"*:
   the seed prompt told it to push, the push was rejected non-fast-forward
   because the rebase had rewritten the SHAs while the remote kept the
   pre-rebase history, and a weak model's recovery from a rejected push is a
   **merge**, not a force-push.
7. **23:10:01** — `parked` (triage at cap).
8. **2026-09-22 01:17:03** — `OA2-01` merges (PR #886) after manual history
   linearization: `git checkout -B` at `origin/master`, re-apply the two
   changed files, `--force-with-lease`.
9. **02:11:18 → 02:19:39** — `SRB-1` dispatched to `glm-5.3-flash:cloud`
   (cloud-oss) and merged as PR #894: **8m21s, first dispatch**, 15 tests,
   APPROVE, no rework.
10. **SRB-2's first dispatch** — the 6-line `pipeline/persona.py` edit landed
    correctly and its test file was written, but the attempt then drifted:
    **60 steps used, `view_file` x33 / `bash` x16 / `search` x8 /
    `str_replace` x3**, against unrelated modules
    (`scripts/smoke_getting_started.py`, `app/backend_ollama.py`,
    `app/ollama_prompt_utils.py`, `pipeline/config_provenance.py`). Guards
    fired and were ignored: `[read-heavy nudge: 6 reads in a row]` x4,
    `[parking: read-heavy after nudge]` x3, `[repetition nudge]`. It never
    committed and never ran the three prescribed verification commands.
11. **03:02:17 → 03:11:55** — interrupted, then re-dispatched **with a
    `PRIOR-ATTEMPT DIAGNOSIS` + `PRIOR-ATTEMPT FACTS` block**; merged as PR
    #895 (~9 minutes), 39 tests, APPROVE.
12. **Verification** — full suite on the merged tree: **12132 passed / 12
    skipped**, `ruff` clean; `SRB-1`'s wiring test drives
    `p.dispatch_story(...)` through the `_ServerRef`, so the call site is
    graded, not assumed.
13. **Live** — pull fast-forwarded `1068f9d → 4bbbe05`, scheduler restarted;
    `.scheduler_health.json` reports `config.checkout_sha: 4bbbe05…`,
    `checkout_behind_origin: 0`.
14. **Retro backlog** — marker committed (`deaca7e`, CI 5/5 green after a
    branch-protection bypass on push); the solved gap entry removed
    (`d6fa5c8`).

---

## 2. Learnings

**L1 — the resume rebase is a one-way SHA rewrite with a stale remote, and the
agent's own push cannot recover it.** `_rebase_onto_master` rewrites the
branch's history; `origin/agent/<story>` keeps the pre-rebase history; the
seed prompt's last instruction is "push the branch"; the push is rejected
non-fast-forward; and the recovery a weak model reaches for is `git merge
origin/<branch>` — resurrecting exactly the commits the rebase rewrote away.
On `OA2-01` that cost 4 merge-gate cycles, a park, and manual
linearization. **The fix is not a smarter recovery, it is removing the
rejected-push condition** — that is what PR #894 does (L4 in §4).

**L2 — the harness already knew the pattern, in three of four places.** Every
other rebase site pushes immediately after: `pipeline/merge.py:94`,
`pipeline/merge.py:572`, `pipeline/pr.py:108`. The resumed-dispatch site at
`pipeline/dispatch.py:~450` was the one that didn't — a copy-of-a-pattern
applied 3 of 4 times, with no shared entry point to make the fourth
impossible. This is the same shape as the "a 'done' story is not proof its
title's scope was fully delivered" lesson: the invariant lived in prose, not
in code.

**L3 — the first-pass-clean classifier cannot see a step-cap kill, so the
headline rate is inflated by ~10 points.** `pipeline/local_success.py::`
`classify_story` marks a story non-clean only on `status != done`, an
escalation, or events in `{escalated, model_fallback, story_parked,
brief_patched}`, plus two message/brief regexes. A step-cap or watchdog kill
followed by a diagnosis rebrief emits **none** of those: `SRB-2`'s sidecar
holds only `story_merged` and `plan_completed`, and
`_RE_BRIEF_REWRITE` matches only the words `REWORK|AMENDMENT`, not the
`PRIOR-ATTEMPT DIAGNOSIS` header. Measured over the current 60-story window:
**6 of the 39 stories counted clean carry a `PRIOR-ATTEMPT DIAGNOSIS` block**
— `SRB-2`, `MFR-01/02/05`, one `anagram-service` story, one
`pipeline-reliability-2026-09-17-retro` story. So the reported **65.0%**
(39/60) is at most **55.0%** (33/60) under a strict definition. No test pins
this as intended (`tests/unit/test_ld90_local_success_classifier.py` covers
the other reasons only), so it reads as a gap rather than a decision.
**Proposed Mode 56** — "step-cap kill invisible to the first-pass-clean
classifier".

**L4 — the read-heavy guard detects and parks, but cannot recover; recovery
comes from a whole extra dispatch.** On `SRB-2` the guard nudged 4x and parked
3x, and the attempt still consumed its full 60 steps. The mechanism that
actually recovered it was the step-cap rebrief (L5), at the cost of a wasted
attempt plus ~9 minutes. A guard that fires correctly but cannot salvage the
attempt is a *detector*, and the current design bills its output to the next
dispatch's budget.

**L5 — completion-without-commitment: the work was done and the attempt still
burned out (Mode 31 variant).** The measured facts state it plainly — the
briefed edit "already landed correctly … so there is no code defect to fix.
The attempt stalled because, after the edit was in place, it never committed
and never ran the required … verification." Off-task drift is a known
model-capability class (Mode 31, with a drift-detection guard), but this is a
distinct *tail* shape: drift that begins **after** the deliverable exists.
The guard family watches for drift; nothing watches for "you are done — run
these three commands and commit".

**L6 — the rebrief machinery turns a stall into a one-cycle success on the
same weak model.** `pipeline/dispatch.py:1398–1404` runs
`diagnose_failure` → `compose_rebriefed_instructions` →
`compose_attempt_facts`; the diagnosis is produced by the `diagnosis` role on
the story's **own** backend and model (here local `gpt-oss-20b-high`), and
the facts block is measured, not guessed (files changed, step counts, guards
that fired). The second attempt needed ~9 minutes. This is CLAUDE.md Step 9's
"the weaker the executor, the more of the diagnosis must be done up front"
working in production, with the diagnosis itself produced by the same tier
being rescued.

---

## 3. Harness improvement areas (prioritized)

### P0 — Count a step-cap kill in the first-pass-clean classifier

`pipeline/local_success.py::classify_story` should add a reason (e.g.
`step_cap_rebrief`) when the dispatched brief carries
`pipeline/rebrief.py::DIAGNOSIS_HEADER` / `FACTS_HEADER`, or when the story
holds an `interrupted_at` stamp together with a diagnosis block. Guard it in
`tests/unit/test_ld90_local_success_classifier.py` (positive: a diagnosis
block ⇒ not clean; negative: a plain done story with only a merge record ⇒
still clean).

- **Why P0:** this instrument *is* the 90% target. Until it is fixed, every
  measurement decision is ~10 points optimistic, and the error is
  concentrated on exactly the class the target exists to drive down.
- **Caveat to keep visible:** the 11 `interrupted_at` stamps that carry no
  diagnosis block (resource-gate pauses, deliberate interrupts) look like a
  *deliberate* exclusion for interrupts that aren't the agent's fault — the
  fix should key on the diagnosis block, not on `interrupted_at` alone.

### P0/P1 — Make "the deliverable exists, now verify and commit" an explicit step

The `SRB-2` facts block already told the second attempt exactly this and it
worked. Promote that from a step-cap-only path to a *completion* signal:
when the briefed edit is already present in the worktree and the transcript
turns read-heavy without a commit, inject the verify+commit list (or park
immediately) rather than spending the remaining budget.

- **Where:** `pipeline/dispatch.py` rebrief call site (~1398) + the
  read-heavy/drift guards in `app/backend_ollama.py` (~677).
- **Cheaper partial:** at park time, emit the same FACTS block so the next
  attempt starts informed even if the diagnosis role is unavailable.

### P1 — Make "every rebase is followed by a remote sync" structural, not conventional

PR #894 put the sync at the *call site* in `dispatch.py`. A fifth rebase site
added later can repeat the original bug the same way the fourth one did.
Move the invariant into `pipeline/rebase.py` (e.g. `_rebase_onto_master(...,
sync_remote=True)` doing the push itself on success) or add a test that
asserts every `_rebase_onto_master` call site is followed by
`_sync_branch_remote`.

- **Where:** `pipeline/rebase.py`, `pipeline/dispatch.py:~467`.
- **Note:** the current fail-open stance (a sync failure warns and dispatches
  anyway) is correct and should survive the refactor — a failed sync must
  never gate dispatch.

### P2 — A read-heavy park should cost a partial, not a whole, attempt

When the briefed edit is already present, parking the whole attempt discards
work that only needed a commit and three commands. See the P0/P1 item; this
is the budget-side framing of the same defect.

- **Where:** the guard/park decision in `app/backend_ollama.py`.

### P3 — Doc-only direct pushes bypass the 5 required status checks

Both retro-doc pushes to `master` this session were accepted with
`Bypassed rule violations … 5 of 5 required status checks are expected`, and
CI then ran after the fact (5/5 green both times). Low risk, but it means the
gate is advisory for doc commits; either route them through a PR or accept it
deliberately and keep the after-the-fact check in the loop (which requires
someone to actually look).

---

## 4. What worked

- **Pre-flighting the brief as literal text.** The preflight impact run
  predicted 0 pre-existing test conflicts for both stories and 0 occurred —
  three production files touched, not one pre-existing test tripped.
  `ruff`-checking the prescribed literal in advance caught the `PLW1510`
  (`subprocess.run` needs an explicit `check=`) *before* it could become a
  born-broken oracle inside a brief.
- **Anchored edits with an explicitly identified anchor.** Every edit named
  its exact insert point (the unique 6-line `logging.getLogger("pipeline")`
  block, the `__all__` block, the `_ServerRef` line). SRB-1 landed in 8m21s
  on a cloud-oss model with no rework.
- **Grading the wiring, not the unit.** The `SRB-1` acceptance path drives
  `p.dispatch_story(...)` and asserts the call lands inside
  `if result["ok"]:` — so "the helper exists" could not pass as "the fix is
  wired". Verified after the fact on the merged diff: the call site sits
  inside the `ok` branch, before the conflict park.
- **The rebrief machinery (L6).** A step-cap stall became a one-cycle success
  on the same model, with a locally-generated diagnosis plus measured facts.
  This is the single highest-leverage mechanism exercised in this plan.
- **The scheduler health file's `checkout_sha`.** Verifying "the daemon is
  running the merged code" is now one read (`checkout_sha: 4bbbe05…`,
  `checkout_behind_origin: 0`) instead of inferring it from `ps -o lstart`.

---

## 5. Status

- **Resume-rebase class: FIXED** (PRs #894/#895, `4bbbe05`), with regression
  guards in `tests/unit/test_resume_rebase_remote_sync.py` (15 tests) and
  `tests/unit/test_seed_prompt_no_remote_sync.py` (14 tests). Verified live on
  the restarted scheduler.
- **Mode 31 (off-task drift): confirmed recurring** in the tail shape L5
  (drift after completion), on local `gpt-oss-20b-high`. Not fixed; the
  P0/P1 item is the candidate fix.
- **Proposed Mode 56 — step-cap kill invisible to the first-pass-clean
  classifier.** New; the P0 item is the fix. `docs/failure_modes.json` still
  ends at Mode 55 (57 entries, incl. `16b` / `16-recur`), so this would be
  the next mint.
- **A3 failure-mode discovery rate:** unchanged in kind — the reframed
  position (a raw mode count is the wrong instrument; classes are harness
  defects / plan-authoring defects / model-capability limits) still holds, and
  L3 is a new instance of the *harness-defect* class, found by dispatching
  real work rather than a benchmark cell.
- **Local success rate:** 60-story window **65.0%** as reported (39/60);
  **≤55.0%** strict once the 6 clean-but-rebriefed stories are excluded.
  On-device 63.2%, cloud-oss 65.9%. This is flat against the 63.3% baseline
  cited in the 2026-09-21 cross-cutting review — the Sept 18–21 fix burst has
  not moved it yet, and **the P0 metric fix should land before the next
  measurement is treated as a reading of anything.**
- **This plan's own stories:** `SRB-1` clean (1 dispatch); `SRB-2` counted
  clean but was **2 dispatches with a 60-step stall** — the first data point
  for the P0 fix.
