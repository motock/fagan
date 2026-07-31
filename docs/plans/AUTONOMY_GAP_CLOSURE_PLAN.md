# Autonomy Gap Closure Plan

**Status: scoped 2026-07-30, not started.**

Why a maturity-plan story still cannot reach `merged` without a stronger model
hand-editing the branch — and the specific harness changes that close it.

Companion to `MATURITY_AND_UNIQUENESS_PLANS.md` (this is A1/A3 work, framed by
failure rather than by feature) and `REVIEWER_ESCALATION_PLAN.md` (whose L2/L3
remain unimplemented and are *not* what is blocking here).

---

## 1. The measurement

Every commit the harness itself produces carries a template subject:

| Producer | Subject |
|---|---|
| test-author phase | `test: add spec suite …` / `test: add acceptance fixture …` |
| oracle executor (`local_agent_oracle.py:883,1754`) | `feat: implement task (acceptance oracle green)` |
| dispatch watchdog (`pipeline/git_ops.py:98`) | `wip(<story-key>): <step>` |

Anything else — a scoped Conventional Commit with a hand-written body — is a
human or Claude commit, and carries `Co-Authored-By: Claude`. That makes
autonomy directly countable rather than a matter of impression:

| PR | Story | Harness commits | Claude commits | Outcome |
|---|---|---|---|---|
| #200 | TRANSPORT-ALIAS-READERS | 3 | 0 | autonomous (green-but-incomplete, Mode 47 → needed #205) |
| #205 | TRANSPORT-ALIAS-CLEANUP | 3 | 0 | autonomous |
| #208 | 5XX-ESCALATE-BASE | 0 | 2 | parked "needs human review" → hand-written |
| #209 | 5XX-ESCALATE-ORACLE | 0 | 1 | hand-written |
| #210 | **LAUNCHD-PLIST-PORTABILITY** (maturity A4) | **0** | **4** | hand-written end-to-end |

The maturity story produced **zero autonomous content**. Its manifest records
`status: done`, `review_verdict: APPROVE`, `merge_attempts: 1` — an accurate
description of a story a human wrote inside the agent's worktree. **The
pipeline's own bookkeeping cannot distinguish "the agent did it" from "a human
did it in the agent's worktree."** That is why progress here reads as closer
than it is, and it is the first thing worth fixing about how we measure.

### The observed failure chain on #210

1. The acceptance fixture called `plistlib.load` on
   `launchd/com.claude.pipeline.mlx-supervisor.plist`, whose XML comment
   contains `uv venv --python`. `--` is illegal inside an XML comment; expat
   rejects it. **The oracle crashed at collection against both the ground-truth
   file and any correct output.** It additionally asserted that regenerating
   from the *worktree* root reproduces the committed plists — impossible, since
   those bake in the real repo path.
2. gpt-oss:20b burned 68 steps, ~30 of them with no successful edit, against a
   grader that could never go green. Parked.
3. A human diagnosed it and hand-wrote a ~1,900-word
   `=== PRIOR-ATTEMPT DIAGNOSIS ===` block into `agent_instructions` containing
   four pre-validated fixes (GEN-1/2/3, TPL-1) — i.e. the solution.
4. A transient ollama 500 killed the converging retry (the defect #208/#209
   later fixed).
5. The corrected fixture used `plutil`. CI is `ubuntu-latest` only. Merge gate:
   `ci fail: Test (Python 3.12): failure` → rework 1/3.
6. Two further hand commits **rewrote the read-only acceptance oracle**, and the
   merge gate re-verified against the rewrite. Merged.

The merged fixture's docstring still claims it parses via `plutil` while the
code uses `plistlib` — Mode 47 (green-but-incomplete) reproduced inside the
oracle itself.

**Conclusion: this was not an implementer-capability failure.** Two of five
stories landed autonomously on the same model. The three that did not were lost
to harness-side gaps, enumerated below.

---

## 2. The gaps

### G1 — Acceptance fixtures are authored with zero validation

`dispatch_story` (`pipeline/server.py:1132-1139`) writes `entry["source"]` into
the worktree and immediately launches the executor. Nothing checks that the
fixture collects, that it fails *for the right reason* on the base branch, or
that it can run on the CI platform. The only fixture check that exists —
`_isolation_only_acceptance_warning` (`pipeline/build_detect.py:337`) — is
advisory and addresses a different problem.

A broken oracle is the most expensive single failure in the system: it consumes
the entire step budget, produces no usable signal, and then requires a human to
author the diagnosis before any retry can succeed.

### G2 — The escalation ladder is switched off

Every escalation call site — `pipeline/server.py:1783, 1857, 2110, 2965, 3004,
3197` — is gated on `_auto_escalation_enabled()`, which is exactly
`PIPELINE_BACKEND_DISPATCH == "auto"` (`pipeline/escalation.py:135`). The
operating configuration sets `PIPELINE_BACKEND_DISPATCH: "local"`.

So step-cap streaks, exhausted rework budgets, and inconclusive reviews **all
park terminally rather than escalating**. #208 parked for precisely this reason
(`parked: review inconclusive after 2 attempts - needs human review`). The
recovery machinery is built and tested; it is disabled by one config value, and
enabling it is coupled to changing dispatch routing, which is a separate
decision.

### G3 — Escalation carries no diagnosis even when it does fire

`_escalate_to_claude` deletes the worktree, branch, and journal, then
re-dispatches with `agent_instructions` **unchanged**. `CLAUDE.md` Step 9
("encode the diagnosis into the next attempt's instructions") is implemented
nowhere in code — only in the operator's head. Every automated retry is a blind
retry, which is the pattern Step 9 exists to forbid.

### G4 — The read-only oracle is not read-only

Immutability exists solely as prompt text (`pipeline/planner.py:567-583`).
Nothing hashes the fixture, and `_reverify_acceptance` (`pipeline/ci.py:242-252`)
runs whatever fixture file is *currently in the worktree*, never comparing it to
the manifest's authoritative `source`. This is the hole #210 merged through.

### G5 — No dev/CI environment parity

Dispatch, the `check_story_status` test gate, and the merge-gate reverify all
run on macOS. CI runs `ubuntu-latest` × Python 3.12/3.13/3.14 and nothing else.
A macOS-only dependency passes every local gate and fails only after the PR is
open, converting a clean run into a rework cycle. Observed via `plutil`; Mode 46
flags the same class for npm optional deps and Rust build scripts.

### G6 — The weak-executor scaffolding fails open, silently

`_run_test_author_phase` (`pipeline/planner.py:646-702`) returns `False` on
refusal, dispatch failure, timeout, or no-commit with a `logging.warning` and
**no `_notify_user`**. The fail-open itself is correct and deliberate (§2.5 of
`TDD_SPLIT_PRODUCTION_PLAN.md`); the silence is not.

The correlation is exact:

| Plan | `role_config` | test-author commit present? | Outcome |
|---|---|---|---|
| transport-alias-deprecation | `planner` + `test_author` + `review` → ollama/glm | yes (#200, #205) | both autonomous |
| 5xx-recovery-escalation | `review` only | **no** | parked |
| launchd-plist-portability | `review` only | **no** | parked |

The two plans that overrode only `review` left `test_author`/`planner` on the
`model_registry.json` default (`claude/sonnet`). Those phases produced nothing,
the weak executor silently lost its TDD-split and tech-lead-checklist crutches,
and no operator-visible signal was emitted.

---

## 3. Side findings (outside the six items above)

- **The test suite writes into the live plans directory.**
  `tests/unit/test_check_story_status_lint_gate.py:253,275` drives
  `check_story_status("cap1", "S1")` with `p.PLAN_DIR` patched to a tmp dir, but
  `pipeline/persistence.py:48` (`_journal_path`) reads its own module-level
  `PLAN_DIR` binding imported from `pipeline.paths`. Journal writes therefore
  escape to the real `~/.claude/plans/cap1.S1.journal.json`, which has
  accumulated 344 `{"step": "step_cap_reached", "commit": "deadbeef"}` records
  since 2026-07-18 (59 of them on 2026-07-30 alone). A general fix belongs in
  `tests/unit/conftest.py`, not in the individual test.

- **Reviewer exceptions are undiagnosable by construction.** The
  `except Exception` handler at `pipeline/server.py:2828-2840` logs only
  `type(e).__name__` for secret hygiene. #210's review `RuntimeError` therefore
  cannot be root-caused after the fact. A redacted traceback to a log file
  preserves the hygiene property while restoring diagnosability.

- **The autonomous driver is not installed.** `~/Library/LaunchAgents/` holds
  only `.bak` copies of `com.claude.pipeline.advance-scheduler.plist`;
  `launchctl list` shows only `mlx-supervisor` and `usage-poller`. Every tick in
  the runs above was a manual MCP call, so "end-to-end" was hand-driven by
  construction. Reinstalling it is an operational step, not a code change, and
  is deliberately **not** a story here — but nothing in this plan produces an
  unattended loop until it is done.

---

## 4. The work

Ordered by hand-editing removed per unit of effort. Stories are decomposed for a
**local ~20B-class implementer** (`PIPELINE_BACKEND_DISPATCH=local`,
`gpt-oss:20b`): ≤2 production files each, one concern each, anchored
`str_replace` on `pipeline/server.py` (3,967 lines).

| Epic | Gap | Stories | Effect |
|---|---|---|---|
| E1 Recovery paths | G2 | 1 | Parks become automated recoveries |
| E2 Oracle validation | G1, G5b | 4 | Broken graders rejected before a step is spent |
| E3 Oracle immutability | G4 | 3 | A rewritten grader can no longer merge |
| E4 Automated re-brief | G3 | 4 | Removes the diagnosis step done by hand today |
| E5 Scaffolding visibility | G6 | 2 | Silent loss of the TDD-split crutch becomes visible |
| E6 Environment parity | G5 | 1 | Platform failures caught before the PR |
| E7 Side findings | §3 | 2 | Test-isolation leak; reviewer traceback |

**Recommended sequencing:** E1 and E2 first — E1 is a near-zero-effort switch
that converts three of the observed terminal parks into automated recoveries,
and E2 removes the largest cost driver. E3 depends on E2's digest work. E4 is
the largest piece and benefits from E2 landing first, since a validated oracle
sharply reduces how often the re-brief path is even reached.

**Self-modification note:** E2 and E3 change `dispatch_story` and
`_reverify_acceptance` — the machinery executing these very stories. The MCP
server does not hot-reload (see the stale-MCP-after-merge finding), so a merged
change takes effect only for the *next* story, and the server must be restarted
and reconnected between them. Sequence these one at a time, not in parallel.

---

## 5. Definition of done for this plan

The plan is complete when a story from `MATURITY_AND_UNIQUENESS_PLANS.md`
reaches `merged` with **zero commits carrying `Co-Authored-By: Claude`** and no
`needs human review` notification — measured by the same commit-authorship audit
used in §1, run against the resulting PR.
