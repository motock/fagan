# Harness retro — w3a-effective-config-provenance (2026-08-06 to 2026-08-09)

Backfilled 2026-08-12 as part of clearing the retro-process backlog (see
`docs/plans/PLAN_RETROSPECTIVE_PROCESS_PLAN.md` §2.1). Written from git
history (commit messages, PR numbers, dates) plus in-session memory notes
from the plan's own execution, not from a live transcript — some internal
detail (exact step counts on early stories) is unavailable and is marked
as such below rather than guessed.

Scope: a read-only "effective config + provenance" view (per-role and
per-env-var, which of five config sources won, whether they conflict,
whether a change needs a restart) — the first concrete step of
`PLATFORM_DECOUPLING_AND_SCALE_PLAN.md`'s W3a workstream. 9 stories tracked
in the manifest (the originally-scoped 6, plus 3 more spawned mid-plan),
across 12 PRs (#247–#258), all dispatched local ~20B-class
(`gpt-oss-20b-high` via Ollama).

The plan's own memory summary undersold what actually happened: this was
not a clean 6-story run. It repeatedly broke the local-dispatch harness in
**new** ways, each fixed live and merged the same or next day, and one
story required direct human repair after automated rework caused real
damage.

---

## 1. Timeline

1. 2026-08-06 — Story 1 (`fb6ec9ad`, `config_provenance.py` scaffold: plist
   + `~/.claude.json` readers) merged, PR #247.
2. 2026-08-06/07 — Story 2 (`f7fd39c4`, share `backend.py`'s dead-env-var
   list) failed **identically across 5 separate dispatch attempts**: after
   enough proactive context trimming, a `create_file` overwrite of
   `config_provenance.py` rewrote the file from memory and silently dropped
   4 of its 5 functions. None of the dropped functions were called from
   within that same file, so the existing `_newly_undefined_module_defs`
   reference check (which only applies to `str_replace`/`replace_lines`)
   never caught it. Fixed live: commit `7660794` (2026-08-06) added
   `_dropped_top_level_defs` — a `create_file`-overwrite guard comparing
   module-level def/class names before/after, applied to both
   `scripts/local_agent.py` and its verbatim oracle copy.
3. 2026-08-07 — The same story (Story 2) then hit a **second**, independent
   harness gap: the off-task-drift guard's escalation only fired on a
   *distinct* off-task path, so a model that fixated on the same wrong file
   after its one nudge got 9 more unguarded mutations with zero
   intervention. Fixed live: commit `ffd92ba` — off-task guard now
   escalates on *any* further off-task mutation post-nudge; new same-path
   edit-churn guard (successful-but-non-converging edits with no
   intervening test run — the actual biggest cost in this live incident:
   22 edits, 0 test runs); failing-`str_replace` guard now escalates
   instead of going silent after one nudge; off-task detection extended to
   bash-based mutations (`ruff --fix`, `sed -i`). Story 2 finally merged,
   PR #248, 2026-08-07.
4. 2026-08-07 — Stories 3 and 4 (`d7a2a68e` `ENV_VAR_CATALOG`, `343666ed`
   `resolve_env_var`) merged clean, PRs #249 and #250.
5. 2026-08-07 — Story 5 (`dcb1dd1b`, `effective_env_config` aggregator)
   merged clean, PR #251.
6. 2026-08-08 — Story 6 (`121556a5`, `resolve_role_provenance` layered
   resolution) initially merged as PR #252, but with a review-flagged
   defect: it hand-rolled its own provider/model precedence walk instead of
   delegating to the existing `role_registry.resolve_role`, which could not
   yet return per-source attribution. Rather than patch around it, a
   prerequisite story was filed in a **separate** small plan
   (`role-registry-environ-delegation`): `8fbabe4a` — add an optional
   `environ` parameter to `resolve_role` (PR #253) — a clean instance of
   "a missing seam is a prerequisite story, not a splitting problem."
7. 2026-08-08 — The delegation rework itself (`b5577e47`, PR #254) then
   **failed on multiple automated rework attempts**: "one destructive
   deletion, one clobbered ~190 lines of unrelated already-merged
   functionality" (direct quote, commit message). Direct human repair:
   restored `config_provenance.py` to master's baseline and re-implemented
   the delegation cleanly, plus fixed two structural defects in the story's
   own test suite (an overly-broad import-cycle check, two test literals
   that didn't match `resolve_role`'s real behavior).
8. 2026-08-08 — This same story (`121556a5`) was, independently, the
   intended first live-validation target for a step-cap "rebrief measured
   facts" feature that had just shipped (commit `4db35c3` — a measured
   git-diff-vs-base block with `POSSIBLE CLOBBER` detection, tool
   histogram, bounded failing-test re-run). A dry run against the paused
   worktree surfaced three things the old LLM-only rebrief never did: the
   exact `TypeError: tuple indices must be integers` line, an **invented
   off-task production file** (`pipeline/role_provenance.py` — note the
   near-miss name collision with the real `config_provenance.py`), and
   `NEVER RAN THE TESTS` across 9 steps.
9. 2026-08-08 — #254's own rework introduced a regression: it collapsed
   two distinct `RoleRegistryError` branches (model-not-declared vs.
   no-model-configured) into one all-null shape, losing the `"unset"`
   distinction a **queued, not-yet-dispatched** downstream story (`8ae416bf`)
   depended on for its own grading. Caught and fixed same day, PR #255,
   with regression tests added — before the downstream story ever ran into
   it.
10. 2026-08-08 — Story 7 (`8ae416bf`, `effective_role_config` aggregator)
    merged clean, PR #256.
11. 2026-08-08/09 — Stories 8 and 9 (`fbd37d2e` `get_effective_config` MCP
    tool, `dfa436db` `/api/config` dashboard endpoint) merged clean, PRs
    #257 and #258.
12. 2026-08-09 — Plan marked done; `MATURITY_AND_UNIQUENESS_PLANS.md` and
    `PLATFORM_DECOUPLING_AND_SCALE_PLAN.md` updated to reflect W3a
    complete.
13. Throughout stories 6/7/8 (PRs #252, #254, #255) — GitHub Actions was
    billing-blocked (account payment failure, every job failing in 1-2s
    with zero steps run). Each of those three merges went through local
    CI (full suite + `ruff check .`) per the standing authorization for
    this exact scenario, not as an ad hoc shortcut.

---

## 2. Learnings

**L1 — `create_file` overwrites had zero content-loss protection, and
context trimming was the actual trigger.** `replace_lines`/`str_replace`
already had reference/deletion guards; `create_file`'s "non-destructive
editor unlocks after one `view_file`" design (intentional, dating to
2026-07-15/16, to avoid deadlocking weak models) meant that once unlocked,
nothing checked what the from-memory rewrite actually kept. This didn't
need a large or unusual story to trigger — a plain "share this list between
two files" task did, once the transcript had been trimmed enough that the
model was reconstructing the file from memory instead of reading it fresh.
**Now fixed** (`7660794`) — but the fact that it took 5 identical failures
on one story to surface is itself notable: nothing about the failure
pattern was visible to the operator until it recurred enough times to look
systematic rather than a fluke.

**L2 — an escalation guard that only re-triggers on a *new* signal is not
a guard against a model that repeats the *same* mistake.** The pre-`ffd92ba`
off-task guard's "distinct path" condition meant a model already flagged
once for editing the wrong file could keep editing that same wrong file
indefinitely with no further intervention — the exact opposite of what an
escalation policy should do. Same root shape as the failing-`str_replace`
guard's pre-fix behavior (nudge once, then silent forever). **Pattern to
watch for in future guards:** "escalate on repetition of the same
violation," not just "escalate on a new kind of violation."

**L3 — a destructive-rework incident happened *after* the edit-damage
guards (edit-guard-enforcement plan, PRs #218/#219/#221/#222/#223/#224,
all landed by 2026-08-03) and the `create_file` guard (`7660794`,
2026-08-06) were already live, and it is not established which guard gap
let it through.** Story 6's rework (step 7 above, 2026-08-08) "clobbered
~190 lines of unrelated already-merged functionality" — the exact class of
damage those guards exist to block. The corrupted state was discarded
during direct repair, so root cause is unverified: possibilities include a
tool path the guards don't cover, a legitimate `confirm_removals=true`
escape hatch used destructively, or a rework-specific code path that
doesn't route through the same guarded `run_tool`. **This is the single
open item this retro could not close** — see P0 below.

**L4 — a review-flagged design shortcut (hand-rolled precedence walk
instead of delegating) correctly became its own prerequisite story rather
than a bolt-on patch**, and that prerequisite was filed in a *separate*
plan (`role-registry-environ-delegation`) rather than shoehorned into this
one. This is the injection-seam pattern already captured in
`feedback_verify_injection_seams_at_plan_time` working as intended, live,
for the first time on a self-discovered (not pre-planned) case.

**L5 — a regression introduced mid-rework was caught by a downstream
story's test requirements before that story ever ran, not by re-testing
the story that broke.** #255 exists because a *queued* story's acceptance
criteria implicitly encoded behavior #254's rework had silently changed.
This only worked because the downstream story hadn't been dispatched yet
when the regression was noticed — had dispatch order been different, this
would have surfaced as a confusing failure in the *wrong* story instead.

---

## 3. Concrete harness improvements, prioritized

- [ ] **P0 — determine what actually let the 190-line clobber through on
      story 6's rework (L3), and close that specific gap.** Both the
      general edit-damage guards and the `create_file`-specific guard were
      live when this happened; the class of damage they were built to stop
      still got through once. Without root cause this is a live gap, not a
      historical one. Concrete next step: instrument (or re-derive from
      `local_agent.py`'s journal/checkpoint history, if retained) which
      tool call produced the clobber on a similarly-shaped rework, since
      the original transcript is gone.
- [ ] **P1 — the `create_file` content-loss guard (`_dropped_top_level_defs`)
      only fires on module-level def/class name loss. Verify it (or a
      sibling check) also catches loss of module-level *constants/dict
      literals* (e.g. a dropped `ENV_VAR_CATALOG`-shaped table), since this
      plan's own domain is exactly that kind of data.** Not confirmed
      broken — flagged because it's untested against this plan's own
      failure shape.
- [ ] **P2 — surface "this story has failed identically N times" as an
      explicit operator-visible signal**, rather than relying on a human
      noticing the pattern across resumed attempts. Story 2 took 5 tries to
      even become diagnosable as one root cause instead of "the model keeps
      messing up." The step-cap rebrief's measured-facts work (§1 step 8,
      already shipped) is a step in this direction but wasn't live yet when
      Story 2 was thrashing.
- [ ] **P3 — this plan is further evidence that `role-registry-environ-delegation`-style
      "spawn a tiny prerequisite plan mid-flight" should be a named,
      lightweight pattern in the product-analyst's playbook**, not just an
      ad hoc judgment call each time a seam gap is discovered live.

---

## 4. What worked

- The escalating-guard fixes (L1, L2) were each diagnosed to a specific
  mechanism and shipped the same or next calendar day the failure was
  observed — not deferred to "someday harden the harness" backlog items.
  Both are now covered by their own regression tests.
- The injection-seam prerequisite (L4) was recognized and filed correctly
  on first encounter, without a prior failed attempt to patch around it.
- The regression catch (L5) happened before it could cost a second story's
  step budget — sequencing (fix landed, downstream story still queued)
  worked in the harness's favor here, even though that was partly luck of
  dispatch order rather than a designed safeguard.
- Direct repair (step 7) was scoped narrowly — restore to master baseline,
  re-implement cleanly, fix the two test defects it found along the way —
  not expanded into an unrelated cleanup pass.
- The GHA-billing-blocked merges (step 13) correctly used the standing
  local-CI fallback rather than either blocking on an external outage or
  skipping verification.

## 5. Status

Modes referenced: none of L1–L5 map cleanly onto an existing numbered Mode
in `project_dispatch_failure_modes.md` as of this writing — L1 and L2 are
now-fixed harness gaps with their own commits (`7660794`, `ffd92ba`) but
were not retroactively given Mode numbers; L3 is an **open, unresolved**
item and a strong candidate for a new Mode number once root-caused. The
maturity-plan mode count (`MATURITY_AND_UNIQUENESS_PLANS.md` A3) has not
been updated to reflect L1–L3 — follow-up.
