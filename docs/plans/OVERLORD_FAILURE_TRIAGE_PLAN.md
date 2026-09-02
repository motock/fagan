# Overlord Failure Triage Plan

**Status: DONE 2026-08-31 — 17/17 stories merged (PRs #368-#390).** Outcome was
mixed: roughly half the parked stories recovered autonomously, but the split
(8/17 local vs 9/17 escalated) is confounded by reviewer-inconclusive parks, a
Claude usage gate mid-run, and harness changes landing while the plan executed —
so the headline recovery rate should not be read as a clean benchmark.

The escalation ladder now fires (see §1) but it knows exactly one move: throw a
bigger model at the same story. When that move is exhausted the story parks
terminally and waits for a human. The decisions a human then makes — *escalate,
split, or fix something in the repo* — are the last routine judgment calls in
the loop, and they are what this plan automates.

Successor to `AUTONOMY_GAP_CLOSURE_PLAN.md` (whose G2/G3/G4 are now closed) and
a sibling of `REVIEWER_ESCALATION_PLAN.md` (L2/L3 still unimplemented and still
not what blocks here).

---

## 1. What closed, and what that exposed

`AUTONOMY_GAP_CLOSURE_PLAN.md` §G2 recorded the escalation ladder as switched
off: `_auto_escalation_enabled()` was exactly `PIPELINE_BACKEND_DISPATCH ==
"auto"` while the operating config set `local`. That is fixed.
`PIPELINE_AUTO_ESCALATE` now decouples the two (`pipeline/escalation.py:169-187`)
and is set to `1` in **both** config sources — the scheduler plist and the MCP
server env. G3 closed too: `pipeline/rebrief.py` folds a measured diagnosis into
`agent_instructions` before a step-cap retry. G4 closed via
`pipeline/oracle_gate.py`'s dispatch-time digests.

So the ladder runs. What that exposed is that the ladder is one rung tall:

| Trigger | Site | The one move | Then |
|---|---|---|---|
| Step-cap streak ≥ `STEP_CAP_FALLBACK_THRESHOLD` | `server.py:2556-2606` | swap to `local_model_fallback`, else `_escalate_to_claude` | resume same scope |
| Rework budget exhausted | `server.py:3894-3925` | escalate to Claude if not already escalated | else **park** |
| Review inconclusive ×2 | `server.py:3626-3679` | escalate to Claude if not already | else **park** |
| No new commit after rework | `server.py:2851-2876` | escalate to Claude if not already | else **park** |

Every one of those paths terminates in the same place: `story["status"] =
"parked"`, a `parked_reason` string, and `_notify_user(... "needs human
review")`. `parked` and `failed` are both excluded from the scheduler's ready
list (`server.py:4022-4027`, which admits only `todo`/`interrupted`/
`changes_requested`), so both are terminal until a human calls
`set_story_status`. Nothing ever revisits them.

**The ladder escalates capability. It never re-examines scope, and it never
questions whether the repo itself is the problem.**

---

## 2. Trigger correction: `interrupted` is not a failure state

The obvious framing is "triage failed and interrupted stories." Half of that is
wrong. `interrupted` is in the ready list and is the *healthy* state for a
usage-gate pause or a mid-progress step-cap — the scheduler resumes it on the
next tick, by design. Triaging on `interrupted` would fire continuously during
normal operation.

The triggers are the two terminal states, plus one derived signal:

- `parked` — every site in the table above.
- `failed` — `server.py:2390` (dispatch produced nothing usable), `2772`
  (empty agent branch), and the merge-gate failures at `4387`/`4416`.
- `step_cap_streak` at threshold — already tracked on the story
  (`server.py:2561-2566`); this is the "interrupted" signal worth acting on,
  and it is a *streak*, not a single interrupt.

---

## 3. Constraints the existing code imposes

These are not design preferences; they are properties of the code the feature
has to live inside. Each was checked, not assumed.

### C1 — The overlord cannot look at anything

`_invoke_overlord` (`pipeline/overlord.py:36-55`) calls `complete(prompt,
system=..., model=..., allowed_tools="Read")` with **no `cwd`**. Both drivers
treat "allowed_tools without Bash, no cwd" as the signal to stay single-shot and
skip the tool loop entirely (`app/backend_ollama.py:288,371`;
`app/backend_claude.py:91`), and `test_complete_stays_single_shot_for_overlord_
style_call` pins that behavior. The `allowed_tools="Read"` argument is
vestigial — it grants nothing.

**Consequence:** every fact the overlord rules on must be pre-packaged into the
prompt string. The ceiling on this whole feature is the evidence packer, not the
model.

### C2 — `_invoke_overlord` raises, and today that is correct

`app/backend_claude.py:105-107` says so explicitly: "`_invoke_overlord` has no
such guard, so the overlord path will now raise on a transport error - an
acceptable, surfacing behavior change." It *is* acceptable for `request_decision`,
a human-initiated MCP tool call. It is not acceptable on the scheduler's
autonomous path, where a transport blip would take down the tick.

Compare `pipeline/rebrief.py`, which is fail-open by explicit design
(`diagnose_failure` at `rebrief.py:433-450` returns `None` on every error path)
for exactly this reason. Triage needs its own fail-open wrapper whose default is
today's behavior: park and notify.

### C3 — `_parse_ruling` does not validate

`pipeline/parsers.py:34-48` regex-matches five known field names and defaults
every miss to `""`. There is no schema check and no error path. A new `ACTION`
field must therefore **fail closed to `park_for_human`** on absent, unparseable,
or unrecognized values — which conveniently is exactly today's behavior, so the
degraded mode is the current mode.

### C4 — Adding a story to a live plan works, but not the way it looks

*This corrects an earlier read of mine, and the note in memory that "ingest_plan
clobbers the manifest."* With `overwrite=False` (the default),
`_ingest_plan_impl` **merges**: an already-tracked story keeps all pipeline-owned
runtime state and refreshes only `_INGEST_AUTHORED_STORY_FIELDS`
(`server.py:1195-1205`), and a story not previously present is simply added
(`server.py:1366-1375`). Wholesale replacement happens only on `overwrite=True`.

The real constraints are narrower:

1. `_ingest_plan_impl` reads the plan from `PLAN_DIR/<plan_name>.json`
   (`server.py:1244-1248`), which is authoritative. A story injected into the
   manifest alone is silently dropped on the next re-ingest. Triage must write
   the new story into the **plan JSON** and then re-ingest.
2. Ingest takes `_plan_lock` and returns `{"skipped": "locked"}` under
   contention (`server.py:1285-1291`). A triage sweep running inside the
   scheduler contends with dispatch on the same plan and must handle the skip
   rather than assume success.

So this is a small, precise piece of work — not a new primitive.

### C5 — Splitting a story can silently deadlock the plan

`dependencies` are authored as exact `summary` text and translated to manifest
keys at ingest (`server.py:1346-1352`). The scheduler's ready check is
`all(d in done for d in v.get("dependencies", []))` (`server.py:4026`). If story
B depends on A and triage splits A into A1/A2, B blocks forever against a key
that will never reach `done`.

The failure is a silent stall, not an error. Splitting must either retain the
parent as a satisfied umbrella (children become its dependencies; the parent
completes when they do) or rewrite every dependent. **The umbrella shape is
strongly preferred** — it needs no rewrite of stories triage did not author.

### C6 — Naive wiring silently reintroduces Claude spend

**Resolved 2026-08-13 — but the fix is not yet committed.** As authored,
`model_registry.json` had no `overlord` entry, so it fell back to the persona's
declared tier, `claude` / `opus` (`pipeline/overlord.py:49-53`), while every
other configured role here is `ollama`. Firing the overlord on every step-cap
streak and rework exhaustion would have put opus spend on the hot path of a run
whose zero-Claude-token property was deliberately validated.

`roles.overlord = ollama/glm` has since been added and verified to resolve
(`ollama` / `glm-5.2:cloud`), with `PIPELINE_BACKEND_OVERLORD` unset in both
config sources so the registry is the sole authority. **It exists only as a
working-tree modification.** `role_registry` resolves against the installed
repo's copy, so the running server already sees it — but CI does not, and one
`git checkout` reverts it. Commit it before E4 merges; the plan additionally
sets `overlord` in its own `role_config` so the guarantee does not rest on the
registry alone.

`_run_diagnosis_role` already solves this: it refuses to spend Claude by default,
falling open to `None` when the story's own backend is Claude-family or absent
(`rebrief.py:420-430`). Triage inherits that discipline, or ships with an
explicit registry entry — one or the other, decided before it is wired.

### C7 — Half the `repo_issue` class is deterministic and should not be asked

Handing "is the repo broken?" to an LLM reading an `agent.log` tail is strictly
worse than measuring it. Several detectors already exist:

- `oracle_gate.classify_oracle_outcome` / `validate_acceptance_fixtures` —
  distinguishes a born-broken grader from a correctly-failing one, including the
  Mode 49 prior-gate case (`oracle_gate.py:52-70`). It runs pre-dispatch and is
  **not consulted at triage time**.
- Repo-wide lint red at the merge-base — the exact condition that silently
  rejected every rework `done` until commit `d88037f`.
- Full suite red at a clean baseline.
- CI unavailable (GHA billing cap) versus CI genuinely failing.

These run first and their results become part of the evidence. The overlord
adjudicates *what to do about* a detected repo issue; it does not detect one.

### C8 — The policy file the overlord reads is not the one in this repo

Found during decomposition, and it would otherwise have shipped the whole slice
dead on arrival.

`_load_policy()` reads `POLICY_PATH`, which defaults to
`~/.claude/overlord-policy.md` (`pipeline/paths.py:26`). `OVERLORD_POLICY` is
unset in both config sources. On this machine that path is a **separate copy** of
the repo's `overlord-policy.md` — different inode, not a symlink, byte-identical
but last written 2026-06-18.

**Editing the repo's copy changes nothing at runtime.** Since the design fails
closed on an unrecognized `ACTION` (C3), an overlord that never learned the field
exists returns no `ACTION` at all, and every ruling degrades silently to
`park_for_human` — i.e. the feature appears to work, spends tokens, and does
nothing. There is no error to notice.

Two consequences:

1. The E3 policy story's acceptance fixture must grade the **repo** file, not
   `_load_policy()`. A fixture asserting the live policy contains `ACTION` is
   born-broken — unpassable by any implementer, since no code change can alter a
   file outside the repo. (This was caught in draft; recording it so it is not
   reintroduced.)
2. **A deployment step is required after E3 merges**: copy `overlord-policy.md`
   to `~/.claude/overlord-policy.md`, or point `OVERLORD_POLICY` at the repo
   copy. This belongs in the definition of done, not in an operator's memory.

The durable fix — making the repo copy authoritative rather than keeping two —
is deliberately out of scope here and worth its own story.

---

## 4. The design

### 4.1 Order of operations

```
terminal story (parked | failed | step-cap streak at threshold)
  └─ deterministic classifiers  (C7)      → structured findings
  └─ evidence pack              (C1)      → bounded prompt string
  └─ overlord single-shot       (C2, C6)  → RULING + ACTION, fail-open
  └─ action executor            (C4, C5)  → existing primitives only
  └─ decisions log                        → same log request_decision writes
```

Fail-open at every stage, with today's park-and-notify as the invariant default.

### 4.2 The `ACTION` enum

`overlord-policy.md`'s existing `RULING / TIER / RISK / RATIONALE /
NOTIFY_USER` contract is unchanged. One machine-parseable field is added so the
executor can branch programmatically instead of a human reading prose:

| `ACTION` | Meaning | Executed via |
|---|---|---|
| `escalate_model` | scope is right, implementer is too weak | existing `_escalate_to_claude` / model swap, now chosen rather than blind |
| `split_story` | scope is wrong for any implementer at this tier | `decompose` role scoped to one story → umbrella shape (C5) |
| `repo_issue` | failure is environmental, not the story's fault | **new story** via plan JSON + re-ingest (C4) |
| `park_for_human` | genuinely ambiguous | today's behavior, unchanged |

### 4.3 The hard constraint on `repo_issue`

**The overlord never edits the repo.** A detected repo issue always becomes a
normal pipeline story that goes through TDD, review, and CI like anything else.
This preserves the invariant the rest of `CLAUDE.md` is built on — every code
change is reviewed — and it keeps the overlord a decision-maker rather than an
unreviewed committer.

### 4.4 Where it runs

A **sweep over terminal-state stories in the scheduler**, not inline in
`dispatch_story`'s exit paths. The sweep:

- cannot crash dispatch (C2's blast radius is one story, not the tick);
- is naturally rate-limited by tick cadence;
- reads settled state with complete evidence, rather than mid-teardown;
- degrades to exactly today's behavior if it never runs.

### 4.5 Loop breaker

Triage adds actions, so it also adds cycles: escalate → fail → split → each
child fails → triage each child. Required from the first story, not bolted on:

- a per-story `triage_attempts` counter with a hard cap;
- the same `ACTION` may not be chosen twice for the same story;
- a plan-level ceiling on triage-created stories, so `repo_issue` cannot fan out.

Past the cap: `park_for_human`, which is where the story would have been anyway.

### 4.6 The kill switch — `PIPELINE_AUTO_TRIAGE`

Automating a judgment call is not universally wanted. An operator who wants a
human making the escalate/split/repo-fix decision must be able to keep that, and
the feature must not be something they have to discover and disable after it has
already acted.

**Default OFF**, per `CLAUDE.md` §"Secure by Design / Secure defaults": new
features ship disabled and the operator opts in.

Mirror `PIPELINE_AUTO_ESCALATE` exactly (`pipeline/escalation.py:169-187`),
which solves the same problem for the ladder below it: an explicit env override
in `("1","true","yes","on")` / `("0","false","no","off")`, and a documented
fallback when unset or unrecognized. Same naming convention, same parsing, same
place in the config — an operator who already knows one knob knows the other.
`_auto_escalation_enabled`'s own docstring records why this shape exists: the
predecessor welded two unrelated decisions onto one variable, and an operator
could not change one without changing the other. Do not repeat that — triage is
independent of both dispatch routing and `PIPELINE_AUTO_ESCALATE`.

**Three effective modes**, because the existing `PIPELINE_AUTONOMY` levels
already compose with this and no new machinery is needed:

| `PIPELINE_AUTO_TRIAGE` | `PIPELINE_AUTONOMY` | Behavior |
|---|---|---|
| off (default) | any | today's behavior exactly — park and notify, no overlord call, no spend |
| on | `dry-run` | overlord rules; ruling is logged to the decisions log and notified; **story stays parked**. The human decides, with a recommendation in hand |
| on | `gated` / `full` | triage acts, subject to the tier rules and the §4.5 loop breaker |

The middle row is the important one and is worth building deliberately rather
than falling out by accident. It is:

- the answer for an operator who wants the human element but not the blindness —
  they get the diagnosis without ceding the decision;
- the correct rollout path for everyone else, since ruling quality can be
  measured against what a human would have chosen **before** the executor is
  ever allowed to act;
- the safe mode for a repo where a wrong `split_story` is expensive.

`dry-run` is already documented as "plan and log only; never dispatch, never
merge, never take an irreversible action" (`overlord-policy.md` §"Autonomy
levels"), so this reading is the existing contract, not a new one.

**Config-source warning.** The flag must be set in **both** the scheduler plist
and the MCP server env, or the scheduler sweep and any interactive path will
silently disagree — the same drift that has bitten `PIPELINE_BACKEND_DISPATCH`
before. The MCP server also does not hot-reload its env, so a change there needs
a restart to take effect.

---

## 5. The work

Decomposed for a local ~20B-class implementer per
`.claude/rules/pipeline-story-schema.md`: ≤2 production files each, one concern
each, anchored `str_replace` against `pipeline/server.py` (4,500+ lines).

| Epic | Concern | Stories | Depends on |
|---|---|---|---|
| E1 Repo-health classifiers | C7 — baseline lint/suite/CI probes at the merge-base, returning structured findings | 2 | — |
| E2 Triage evidence pack | C1 — extend `collect_failure_evidence` for the triage question (attempt counters, tier, classifier findings) | 1 | E1 |
| E3 `ACTION` contract | C3 — extend `_parse_ruling`, fail closed on unknown; update `overlord-policy.md` | 2 | — |
| E4 Triage sweep | §4.4 + §4.5 + §4.6 — terminal-state sweep, fail-open wrapper (C2), budget and loop breaker, `PIPELINE_AUTO_TRIAGE` kill switch | 2–3 | E2, E3 |
| E5 Executors: escalate / park | reuse existing escalation; park is a no-op | 1 | E4 |
| E6 Executor: `split_story` | C5 umbrella shape via the `decompose` role | 2 | E4 |
| E7 Executor: `repo_issue` | C4 plan-JSON write + re-ingest, lock-skip handling | 2 | E4 |

**Sequencing.** E1→E2→E3→E4→E5 delivers a working, useful loop on its own:
better-informed escalation plus honest parks, with no new failure surface. E6
and E7 are the parts that create state, and both should land one at a time
behind live observation.

**Backend decision required before E4** (C6): either add an `overlord` entry to
`model_registry.json` or apply `_run_diagnosis_role`'s no-Claude-by-default
rule. Do not leave it implicit.

**Self-modification note.** E4 changes the scheduler that executes these very
stories, and the MCP server does not hot-reload — a merged change takes effect
only for the *next* story, after a restart and reconnect. Sequence one at a time,
never in parallel.

---

## 6. Non-goals

- **Merge adjudication.** The merge gate is mechanical — risk threshold, `gh pr
  checks`, rebase, acceptance re-verification — and is not improved by an LLM
  ruling. `overlord-policy.md` §"Merge adjudication" already describes the
  mechanical rule; the code implements it without calling the overlord, and that
  stays true.
- **Triaging `interrupted`.** See §2.
- **The overlord writing code.** See §4.3.
- **Un-parking stories parked for `risk: high`.** Park-and-ping is held
  regardless of autonomy level (`overlord-policy.md` §"Autonomy levels"); triage
  does not override that tier.

---

## 7. Definition of done

The plan is complete when a story that today parks terminally is observed
reaching either `merged` or a filed `repo_issue` follow-up **without a human
touching the manifest or the worktree** — measured the same way
`AUTONOMY_GAP_CLOSURE_PLAN.md` §1 measures autonomy, by commit authorship on the
resulting PR.

Supporting gates:

- `~/.claude/overlord-policy.md` (or `OVERLORD_POLICY`) carries the `ACTION`
  contract — verified by an actual ruling containing an `ACTION` line, not by
  the repo file's contents (C8);
- `model_registry.json`'s `overlord` entry is **committed**, not merely present
  in a working tree (C6);
- with `PIPELINE_AUTO_TRIAGE` unset or off, behavior is byte-for-byte today's:
  no overlord call, no token spend, no manifest field written (§4.6);
- with the flag on and `PIPELINE_AUTONOMY=dry-run`, a ruling is logged and
  notified while the story stays `parked`;
- every triage ruling appears in the plan's decisions log and is visible via
  `list_decisions`, alongside `request_decision`'s rulings;
- a forced overlord transport failure leaves the story parked exactly as today,
  with the scheduler tick unaffected (C2);
- a malformed `ACTION` parks rather than acting (C3);
- a split story's dependents still become ready (C5);
- a full local run's Claude token spend is unchanged from before triage, or
  changed by an explicit, recorded configuration decision (C6).

---

## 8. Open questions

1. **Should `repo_issue` block the story it came from?** Filing the follow-up as
   a dependency of the parked story is the honest model, but it stalls the
   original until the repo fix merges. The alternative — file it, leave the
   original parked, let the human decide — is weaker but never deadlocks.
2. **Does `split_story` need `acceptance` fixtures for its children?** Splitting
   a story with a read-only oracle raises the question of which child inherits
   it. Simplest defensible answer: children inherit no fixtures and run on the
   test bar, since a fixture authored for the parent's scope grades the wrong
   thing — but this needs a decision before E6.
3. **Should the classifier layer (E1) run pre-dispatch too?** It would catch a
   red baseline before an implementer spends a step budget, which is
   `oracle_gate`'s whole rationale applied one level up. Deliberately out of
   scope here, but it is the obvious follow-on.
