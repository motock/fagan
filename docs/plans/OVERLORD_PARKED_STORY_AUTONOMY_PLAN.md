# Overlord Parked-Story Autonomy Plan

**Status: design doc → plan draft (2026-09-14).** Companion to
`OVERLORD_FAILURE_TRIAGE_PLAN.md` (which built the triage skeleton) and
`AUTONOMY_GAP_CLOSURE_PLAN.md` (whose G2/G3/G4 are closed; G1 shrinks to a
lint sliver here). Scope confirmed with the user 2026-09-14 across three
decisions:

1. Implement `split_story` plus the auditability layer plus the G1
   ingest-lint sliver; **`repo_issue` is cut** (stays in `DEFERRED_ACTIONS`,
   parks loudly forever).
2. **Park-for-human is no longer a gate.** The user's experience: every
   historical park resolved through a human action that added authority, not
   judgment — re-checking git state, signing off on the agent's already-made
   call, or hand-patching the plan from the agent's diagnosis. With a complete
   decision-and-reasoning audit capturing prior state, resolution authority
   is delegated to the overlord and reviewed **post-hoc**.
3. **`PIPELINE_AUTONOMY=full` includes high-risk merge adjudication** (the
   overlord already runs the security review on those stories), and the
   overlord may write **plan content** via `patch_acceptance` (plans yes; the
   repo never).

Dispatch tier: **cloud open-source** (registry/role chain resolves it; no
per-story `model` field). Sizing per `.claude/rules/agent-dispatch-story-sizing.md`
— every story ≤2-3 production files, ≤3 new functions, no file over the
~1000-line gate (`triage.py` 477, `parsers.py` 396, `merge.py` 389,
`ingest.py` 232, `overlord-policy.md` 129).

---

## 1. Research method

Swept **every** manifest under `~/.claude/plans/` (not name-matched —
enumerated by file, per the sweep-all-plans-by-file rule): 34 stories with a
terminal park/fail record or a live `parked_reason`. Classified each by
recorded reason, then by **what actually cleared it** (final status, PR URL,
review verdict, backend escalation trail). Cross-checked against the Mode
catalog and the decision logs.

## 2. Findings — the five park classes

| Class | Count | Recorded reason | What actually cleared it |
|---|---|---|---|
| Stale bookkeeping | 21 | "tests passed but no new commits vs master" | **~19 are now `done` with merged PRs.** The reason itself was wrong (prior-cycle WIP commit made the branch look empty, or the change was already on master — the NOTIFYLC-3 case). The human action was re-verifying live git state and correcting the record — never a redispatch. |
| Rework exhaustion | 8 | "rework budget exhausted after 3/4 review cycles" | Escalation to a stronger model with a cold-start rebrief, or direct repair of mechanical leftovers (rebase/lint — the Mode 49 class). Every one eventually merged. |
| Deliberate holds | 3 | "risk above threshold" / "high risk held for human review" | Explicit human merge adjudication — signing off on a decision the evidence had already made. |
| Abandoned scope | 2 | (none) | Nothing — iOS/Android client stories, deliberately never built. Still parked today, correctly. |
| Born-broken oracles | (Mode catalog) | not manifest-visible | Human diagnosed the fixture defect and hand-wrote the corrected plan content (#210: the 1,900-word PRIOR-ATTEMPT block). Structurally unfixable by *re-dispatch* — fixable by *editing the plan*. |

**Foundation rule 1:** the overlord must never act on `parked_reason` text.
It must re-derive live evidence (worktree diff, branch-vs-base commits, PR
state, current suite state) before ruling. An overlord that redispatched on
the recorded reason would have wrongly re-run 19 finished stories.

**Foundation rule 2 (the user's delegation finding):** across the entire
history, no park resolution required human judgment the machine lacked —
the human's contribution was *authority*. The `request_decision` flow
already records `decided_by: "overlord"` rulings as binding. Delegating the
remaining resolution authority, with a post-hoc-reviewable audit, removes a
latency gate without removing any decision quality.

## 3. The decision matrix

| Park class | Evidence signal | ACTION |
|---|---|---|
| Stale bookkeeping | live state contradicts the recorded reason (suite green at HEAD, branch has commits, PR merged) | **`mark_done`** — correct the story's record from live evidence; never re-dispatch finished work |
| Rework exhaustion, content near-correct | review verdicts show mechanical leftovers; suite fails narrowly | `escalate_model` (rebrief carries the diagnosis) |
| Repeated step-caps / watchdog on same story | `step_cap_streak` at threshold, scope too large for the tier | `split_story` |
| Born-broken oracle | oracle red at clean baseline, fixture named | **`patch_acceptance`** — rewrite the diagnosed-broken fixture's plan source through the validation gate (G1 prevents most upstream) |
| High-risk merge hold | `risk: high` | in `dry-run`/`gated`: still held; in `full`: **overlord adjudicates the merge** |
| Abandoned / superseded scope | no live work, plan superseded | leave parked (a *ruling*, not a gate — the overlord decides it stays, and the audit records why) |

The bar: **no park that a human would clear with a mechanical action stays
parked, and no park waits for a human sign-off the evidence has already
made.** "Every plan must succeed" is explicitly *not* the target — abandoned
scope and contradictory requirements still park, permanently, by overlord
ruling. The difference from the previous design: those parks are now
*decisions with recorded reasoning*, not gates waiting on a human.

## 4. What already shipped (corrections to the old gap framing)

The `overlord-failure-triage` plan (17/17 merged, PRs #368–#390) built more
than "one verb implemented":

- Full evidence→ruling→executor pipeline: `collect_triage_evidence`,
  `_current_suite_state` (live suite-vs-HEAD check — exists precisely for the
  stale-bookkeeping class), fail-closed ACTION parsing in `_parse_ruling`,
  `rule_on_story` (fail-open), `execute_ruling`, `run_triage_sweep` wired
  into the scheduler tick.
- **Auditability of rulings already exists**: `rule_on_story` appends a
  decision record (`decided_by: "overlord-triage"`, ruling/tier/risk/
  rationale/action) to the per-plan decisions log. What's missing is the
  *execution outcome* — mode, resulting action, prior state, and
  child-story provenance.
- Loop breakers ahead of the verbs: `TRIAGE_MAX_ATTEMPTS=2`,
  `TRIAGE_MAX_CREATED_STORIES=3`, `action_already_tried`,
  `TRIAGE_MAX_PER_TICK=1`, `plan_triage_budget_exhausted`.
- Opt-in + caution rails: `PIPELINE_AUTO_TRIAGE` kill switch (unset = OFF,
  fails closed), `PIPELINE_AUTONOMY` mode ladder (`dry-run` / `gated` /
  `full`).
- `escalate_model` executor, including the local-fallback rung.
- Dispatch-time oracle validation (G4 + born-broken-by-prior-gate detection
  in `pipeline/oracle_gate.py`), ingest-time advisory warnings
  (`_isolation_only_acceptance_warning`, `_platform_locked_fixture_warning`).
- The merge path already honors `PIPELINE_AUTONOMY` and
  `PIPELINE_RISK_THRESHOLD` (`pipeline/merge.py:158-184`): risk=high parks
  with `"high risk held for human review"` — the exact seam story 9 extends.

Remaining gaps, precisely: (a) live git-state facts are **absent from the
evidence**; (b) the decision matrix exists nowhere the ruling prompt reads
it; (c) no execution-outcome audit record with prior state; (d) `split_story`
parks loudly (`DEFERRED_ACTIONS`, PR #390); (e) `mark_done` /
`patch_acceptance` are not actions at all; (f) the risk=high merge hold is
unconditional rather than mode-aware; (g) acceptance-fixture **lint** at
ingest is still manual dogfooding (the PR #235 RUF059 class).

## 5. Safeguards and rollout

The caution lives in the **sequence**, not in permanent gates:

- **Default off.** `PIPELINE_AUTO_TRIAGE` unset → sweep disabled. Nothing
  changes for any operator who does nothing.
- **The mode ladder is the dial:**
  - **`dry-run`** — notify-only rulings (today's behavior). Run this first to
    accumulate real ruling history against real parked stories.
  - **`gated`** — execute reversible, manifest-only actions: `mark_done`,
    `split_story`, `patch_acceptance`. High-risk merges still held.
  - **`full`** — everything in `gated`, plus the overlord adjudicates
    high-risk merges (it already runs the security review on those stories).
- **Everything the overlord executes is manifest/plan-scoped.** It never
  edits the repo; the only outward-facing verb (`repo_issue`) is cut.
- **Every executed action records prior state** (previous status and
  `parked_reason`, previous acceptance digests) so any autonomous action is
  explainable *and undoable* by a human reading the decisions log after the
  fact — the property that makes post-hoc review a real substitute for the
  gate.
- **Loop breakers already bound the new verbs**: one split consumes 2 of the
  3 `TRIAGE_MAX_CREATED_STORIES` budget; `action_already_tried` forbids a
  repeat of the same action on the same story; `TRIAGE_MAX_PER_TICK=1`.
- **Fail-closed everywhere**: unparseable ACTION → `park_for_human`
  (existing); `split_story` without a valid SPLIT payload → parks loudly;
  `patch_acceptance` whose rewritten source fails lint/collection validation
  → parks loudly with the failures named; ingest lint failure → ingest
  rejected.

## 6. Stories

Nine stories. Sibling chains on shared files are sequenced via dependencies:
`triage.py` (1 → 3 → 5 → 6 → 7), `parsers.py` (4 → 6 → 7),
`overlord-policy.md` (2 → 4 → 5 → 6 → 7 → 9).

| # | Summary | Files | Deps |
|---|---|---|---|
| 1 | Add a live git-state probe to triage evidence | `pipeline/triage.py` | — |
| 2 | Encode the parked-story decision matrix and autonomy-mode ladder in the failure-triage policy | `overlord-policy.md` | — |
| 3 | Record executed triage actions with prior state in the decisions log | `pipeline/triage.py` | 1 |
| 4 | Parse a fail-closed SPLIT payload in triage rulings | `pipeline/parsers.py`, `overlord-policy.md` | 2 |
| 5 | Execute split_story rulings by creating child stories in the manifest | `pipeline/triage.py`, `overlord-policy.md` | 1, 3, 4 |
| 6 | Execute mark_done rulings by correcting the story record from live evidence | `pipeline/parsers.py`, `pipeline/triage.py`, `overlord-policy.md` | 1, 5 |
| 7 | Execute patch_acceptance rulings by rewriting a diagnosed-broken fixture through validation | `pipeline/parsers.py`, `pipeline/triage.py`, `overlord-policy.md` | 6, 8 |
| 8 | Lint and validate acceptance fixture sources at ingest time | `pipeline/ingest.py`, `pipeline/fixture_lint.py` (new) | — |
| 9 | Adjudicate high-risk merges through the overlord in full autonomy mode | `pipeline/merge.py`, `overlord-policy.md` | 3, 7 |

### Story 1 — live git-state probe

New `_current_git_state(worktree, story)` in `pipeline/triage.py`, mirroring
`_current_suite_state`'s fail-open shape (never raises, returns `""` on any
problem, bounded like `PIPELINE_TRIAGE_SUITE_TIMEOUT`). Reports: whether the
branch has new commits vs the base branch (reuse
`pipeline.git_ops._worktree_has_new_commits`; resolve the base branch the way
the check-status path does), the story's `pr_url` when present, and the
worktree HEAD sha. Wired into `collect_triage_evidence` as a section alongside
`current_state_section`. Tests mirror `test_triage_current_suite_state.py`
(stub the `globals()["subprocess.run"]` seam at triage.py:147). Done-criterion:
a story parked with a stale "no new commits" reason whose worktree actually
has commits produces an evidence section saying so.

### Story 2 — decision matrix and mode ladder in the policy

`overlord-policy.md` ships verbatim as the overlord's system prompt
(`pipeline.overlord._load_policy`). Add to the `## Failure triage` section —
**additive only**: the §3 matrix; Foundation rules 1 and 2; the mode-ladder
semantics (`gated` = reversible manifest-only actions, `full` = additionally
adjudicates high-risk merges); the honest-terminal-park framing (abandoned
scope parks *by ruling* with recorded reasoning, not as an unexplained gate).
The policy-contract oracle
(`test_acceptance_triage_policy_contract.py`) pins the exact
`ACTION: escalate_model | split_story | repo_issue | park_for_human`
contract line and requires every action named in the section: keep both
byte-identical/complete in this story (the line is extended by stories 6 and
7, each with its own pre-authorized test edit). New per-story test file
asserts the matrix markers by membership. **Do not** remove or reword the
four action definitions or the `fail closed` sentence.

### Story 3 — execution-outcome audit record with prior state

Append a second decisions-log record at execution time from
`execute_ruling` / `_apply_ruling_for_mode` (existing `_append_decision`):
`decided_by: "overlord-triage"`, `question: "failure triage execution"`,
story_key, action, the executor's result string, the autonomy mode, a
`children: []` list (populated by story 5), and **prior state** — the story's
previous `status` and `parked_reason` before the executor mutated anything
(the property that makes every later autonomous action undoable from the
log). Dry-run rulings also record (mode `dry-run`, no mutation). Wrapped in
try/except exactly like `rule_on_story`'s record append — never raises.
Existing key-based asserts in `test_triage_ruling.py` stay green.

### Story 4 — SPLIT payload parsing

`pipeline/parsers.py`: `_parse_ruling` gains a `split` key — the list of
child summaries parsed from `SPLIT: <child A> || <child B>` lines in the
output contract. Absent/malformed → `split: []`; the ACTION is preserved (the
executor, not the parser, fails closed on an empty payload).
`test_parse_ruling_action.py` uses key-based asserts — additive key is
green. Also adds the `SPLIT:` line to the output-contract section of
`overlord-policy.md` (after story 2, per the file-sibling chain) without
touching the ACTION contract line. No test pins `TRIAGE_ACTIONS` exactly
(verified) — the action set itself is untouched in this story.

### Story 5 — split_story executor

Remove `split_story` from `DEFERRED_ACTIONS` (`repo_issue` stays). Implement
`_execute_split_story(plan_name, story_key, story, ruling, manifest,
manifest_path)` in `pipeline/triage.py`:

- Guards, in order: valid `split` payload (2 children) else park loudly as
  today; `plan_triage_budget_exhausted(manifest)` else park;
  `action_already_tried` is enforced upstream by the sweep.
- Creates two child stories in `manifest["stories"]` with deterministic keys
  `{parent_key}-split-1` / `{parent_key}-split-2`: summaries from the SPLIT
  payload; `agent_instructions` = the parent's plus an appended
  `=== SPLIT FROM PRIOR STORY ===` block (ruling rationale + prior-attempt
  evidence summary + "this is one half of a split; the sibling story owns
  the other half"); `persona`/`risk`/`backend` copied from the parent;
  status `todo`; **no `acceptance`** (the parent's fixtures cannot be
  mechanically halved; agent-authored tests carry the bar). Children carry
  no inter-dependency (split halves are independent).
- Parent → `status: "parked"`, `parked_reason: "split into <child1>, <child2>
  by triage"`. `manifest["triage_created_stories"] += 2`. The execution
  record (story 3's seam) lists the child keys in `children`.
- One-sentence staleness fix in `overlord-policy.md`: "split_story and
  repo_issue are currently recorded and then parked for a human" → repo_issue
  only.

**Pre-authorized existing-test edits** (the story legitimately changes the
park-loudly contract for `split_story`; per CLAUDE.md Step 4 and the
pipeline-story-schema existing-test-conflict doctrine, these exact edits are
authorized at plan time — no others):

- `tests/unit/test_triage_execute_deferred.py:177`:
  `assert triage_mod.DEFERRED_ACTIONS == frozenset({"split_story", "repo_issue"})`
  → `assert triage_mod.DEFERRED_ACTIONS == frozenset({"repo_issue"})`
- Same file, the membership asserts around :173-176 asserting
  `"split_story" in triage.DEFERRED_ACTIONS` → assert `"split_story" not in`
  (and `"repo_issue" in` unchanged).
- Same file, the four split_story park-loudly tests
  (`test_split_story_parks_and_marks_deferred`,
  `test_split_story_notifies_with_key_and_action`,
  `test_split_story_parked_reason_names_action_not_implemented`,
  `test_split_story_does_not_escalate`) → rewritten to the new executor
  contract (split creates children, parent parked with provenance). The
  `repo_issue` twins in the same file stay unchanged.

### Story 6 — mark_done action

The dominant historical class (21 of 34 parks, ~19 wrongly labeled) becomes a
first-class executable action. Add `mark_done` to `TRIAGE_ACTIONS` in
`pipeline/parsers.py` (fail-closed normalization unchanged) and implement
`_execute_mark_done` in `pipeline/triage.py`: corrects the story's status
**only on corroborated live evidence** — the git-state probe (story 1)
showing new commits vs base or a merged `pr_url`, and/or the live suite check
showing green at HEAD; if the evidence does not corroborate, parks loudly
with "mark_done ruled but live evidence does not corroborate" (fail closed —
an uncorroborated done is the dangerous direction). Execution records prior
status/reason via story 3's seam. Updates the ACTION contract line in
`overlord-policy.md` to add `| mark_done`.

**Pre-authorized existing-test edit**:
`tests/unit/test_acceptance_triage_policy_contract.py`'s
`_CONTRACT_LINE = "ACTION: escalate_model | split_story | repo_issue | park_for_human"`
→ append ` | mark_done` (and extend the `_ACTIONS` tuple with
`"mark_done"`; the `names_every_action` loop then still passes). Story 7
extends the same line again with `patch_acceptance`.

### Story 7 — patch_acceptance action

The born-broken-oracle backstop (the #210 class — human hand-wrote the fix;
now the overlord, with the same evidence, does it). Add `patch_acceptance`
to `TRIAGE_ACTIONS` and implement `_execute_patch_acceptance`:

- Applies only to a story whose acceptance fixture is demonstrably broken at
  a clean baseline (the `pipeline/oracle_gate.py` classification already
  computes this; reuse it — do not re-derive).
- Re-invokes the overlord with a dedicated prompt: the fixture source, the
  failure output, the evidence sections, and the instruction to emit a
  corrected fixture source plus a short diagnosis. Fail-closed on parse
  failure → park loudly.
- **Validates before writing** — the dogfooding rule made mechanical: the
  rewritten source must pass the story-8 helper's lint and collection checks
  and must still *fail* on a clean baseline (a "fixed" fixture that passes
  without implementation is isolation-only — reject it). Validation failure
  → park loudly with the violations named; the plan's authoritative source is
  untouched.
- Writes via the manifest's `acceptance` field (the `patch_story`-class
  path), records the **previous acceptance digests** in the execution
  record (undoable), and re-dispatches the story fresh.
- Updates the ACTION contract line with `| patch_acceptance` (its own
  pre-authorized edit to `_CONTRACT_LINE` / `_ACTIONS`, building on story
  6's).

### Story 8 — ingest-time acceptance-fixture lint and validation

Close the G1 sliver: the PR #235 class (plan-authored fixture with a lint
violation becomes a read-only oracle CI rejects forever). New
`pipeline/fixture_lint.py` helper + wiring in `pipeline/ingest.py`:
`ingest_plan` rejects a story whose `acceptance` entry source fails `ruff
check` (exact pin `ruff==0.16.5`, run from the repo venv against a temp
materialization of the source, honoring the repo's ruff config;
**source-only — never imported or executed**). Applies to `.py` fixture
paths only. The rejection error lists the entry path and the exact
violations. The helper is deliberately a standalone module so story 7's
executor imports the same check (one implementation, two callers). Existing
ingest tests and the two advisory warnings stay green (different failure
class; clean fixture sources in existing tests pass lint). Note for the
implementer: the Mode 41 lesson — the pin must be exact, and a local-vs-CI
version mismatch must not silently change the bar.

### Story 9 — full-mode high-risk merge adjudication

`pipeline/merge.py:158-184` already honors `PIPELINE_AUTONOMY` and
`PIPELINE_RISK_THRESHOLD`; today risk=high unconditionally parks with
`"high risk held for human review"`. Change: in `PIPELINE_AUTONOMY=full`,
the hold becomes an **overlord adjudication** — invoke the overlord with the
merge context (PR checks state, review verdict, security-review verdict,
risk, story summary), record the ruling through the decisions log
(`decided_by: "overlord"`, with prior state per story 3's pattern), and
proceed or park per the ruling. In `dry-run` and `gated` the hold is
**unchanged** — this is the one action reserved for `full`. Update the
policy sentence "Triage never overrides the park-and-ping tier: a story held
for risk: high stays held regardless of the ruling" to the mode-conditional
truth. `PIPELINE_AUTONOMY` is exercised by ~122 test lines across the suite
— the brief instructs the agent to run every mode test and keep
`dry-run`/`gated` behavior byte-identical; `tests/unit/test_pipeline_mcp_server_escalation_retarget.py`
mentions the hold string and must be pre-audited before any edit, with
conflicts pre-authorized in the brief rather than improvised.

## 7. Definition of done

- A story parked with a stale reason receives live git-state facts in its
  ruling prompt, and the policy tells the overlord how to act on them.
- `mark_done`, `split_story`, and `patch_acceptance` all execute (manifest/
  plan-scoped only), respect the budget/loop-breaker caps, fail closed, and
  record prior state + reasoning + children/digests in the decisions log.
- A `patch_acceptance` rewrite is validated (lint + collection +
  still-fails-at-baseline) before it ever replaces the plan's source.
- A lint-dirty acceptance fixture is rejected at `ingest_plan` with the
  violations named.
- In `full` mode, a high-risk merge is adjudicated by the overlord with the
  ruling recorded; in `dry-run`/`gated` it still holds.
- `PIPELINE_AUTO_TRIAGE` unset = nothing changes for anyone.
- All existing tests green except the pre-authorized edits named above.

## 8. Deliberately out of scope

- **`repo_issue`** — cut. Stays in `DEFERRED_ACTIONS`, keeps parking loudly;
  the published ACTION contract keeps naming it (the overlord may still rule
  it honestly; the executor records and parks, exactly as today).
- **G5 (dev/CI parity), G6 (scaffolding visibility)** — separate follow-ups.
- **The autonomy-metric commit-authorship audit** (agent-done vs
  human-done, the #210 measurement problem) — the prior-state execution
  records here are the substrate it needs; the audit itself is its own
  follow-up.
- **Enabling the sweep or flipping modes** — operational, user's call, after
  dry-run accumulation. Not a story.