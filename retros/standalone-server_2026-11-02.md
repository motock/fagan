# Plan retro — standalone-server (2026-11-02)

Plan: `standalone-server` (19 stories authored SA-00..SA-19, plus the
triage-minted `SA-10-repo-issue`; 20 total), ingested 2026-10-30 from
`docs/plans/STANDALONE_SERVER_PLAN.json` on `master` `5c8f8ac`. Window:
2026-10-30 12:07 → 2026-11-02 03:40 UTC (~2.6 days). Dispatch tier:
cloud open-source (`roles.dispatch` → `ollama/deepseek-v4.1-flash:cloud`
via `PIPELINE_BACKEND_DISPATCH=ollama`); SA-11 ran on `claude` as the one
deliberate exception. All 20 stories reached `done` with merged PRs
(#1423–#1443). Rollup from the plan report:

- 54 rework cycles, 5 escalations, ~103 dispatch events (~5.2/story),
  41 interrupts; first-pass-clean 5% (1/20), cost 79 (9.9 per merged story).

The control group that reframes those numbers: `standalone-security-defaults`
(2 stories, 2026-11-01/02, same dispatch tier, same security surface)
finished **first-pass clean 2/2 with zero rework**, and
`standalone-agents-provisioning-fix` (2 stories, same tier) completed
one-shot. The tier was not the dominant variable. Plan shape and harness
loops were.

## 1. Timeline

1. **2026-10-30 12:07** — plan ingested; six stories dispatched in the
   first round. All six come back green on code but four are bounced by
   the **scope gate**, not by their code (see §2.1). The plan doc's own
   "Repair after the first dispatch round" section (written the same day)
   concedes: "In every case the branch's own work was green … so the
   defect was in the declared scope, not in the story."
2. **2026-10-30 ~14:35** — the operator stages a rollout: SA-11..SA-18 are
   parked with reason "held while SA-08/SA-19 finish". `overlord-triage`
   then re-triages each of them 2–3 times within 30 minutes
   (`standalone-server.decisions.json`: ~15 consecutive `park_for_human`
   rulings on stories whose hold was deliberate sequencing, not a
   failure). Triage policy forbids acting on `parked_reason` text and the
   evidence bundle carries no dependency status, so every pass correctly
   rules "keep parked" — and the loop correctly re-raises it next tick.
3. **2026-10-30 15:45** — SA-07 merges (PR #1427), carrying a
   doubled-`linux_` URL into `deploy/Dockerfile` (§2.5). The GHCR publish
   job turns red on that push and stays red for **25 consecutive master
   runs**; no merge gate, no notification, and no person notices for
   three days.
4. **2026-10-30 15:13–22:16** — `risk:high` merge adjudication re-parks
   the same PRs repeatedly (SA-04 ×8, SA-00/09/12/13/14/15 similar);
   SA-12 is ruled `proceed` at 20:03 and re-ruled `park` at 20:06 —
   rulings are per-tick, not durable. Some of SA-04's parks were correct
   (a real CI failure: `ClonedRepo._flavour` `AttributeError`, 17 tests,
   a Python-3.11-floor pathlib internal that passed on the author's newer
   local interpreter), but the diagnosis was never encoded into the next
   attempt.
5. **2026-10-30 evening → 2026-11-02 03:40** — the plan grinds to done:
   rework counters reset for SA-01/SA-08 after the scope widening,
   `PIPELINE_AUTO_ESCALATE` turned off (escalation resolved to the same
   model that had just failed), SA-08 burns 7 review rounds and ends with
   "rework budget exhausted after 3 review cycles" parked for human, then
   merges after the staged rollout releases.
6. **2026-11-02** — plan completed; both follow-up plans
   (`standalone-security-defaults`, `standalone-agents-provisioning-fix`)
   ship clean on the repaired authoring rules.

## 2. What went wrong (each tied to evidence)

### 2.1 Under-declared `files` — 4 of the first 6 stories died on the scope gate
SA-01 needed `app/dashboard_routes.py` (the dashboard's `GET /api/config`
leaks the same env the story was closing in the MCP surface); SA-08
needed `deploy/Dockerfile` and `deploy/lib/operator_env.sh`; SA-00 was
"unwinable by construction" — the scope gate ordered it to revert
`docs/specs/IDENTITY_AND_POLICY.md` while the reviewer blocked on exactly
that spec being stale; the branch "spent four of its seven commits
oscillating between the two" (SA-00 journal). SA-08 added → reverted →
re-added the same credential strips in `app/backend_claude.py` /
`pipeline/execution.py` across rounds 2–5, each revert and re-add burning
a dispatch. **The preflight impact run exists to derive `files` from where
the code actually lives; this plan's preflight verified suite impact but
under-derived file scope.** The mid-flight repair (widened scopes +
`SCOPE_CONFLICT` escape + counter resets) is what the two clean follow-up
plans then inherited.

### 2.2 Two stories sharing write-ownership of one file
SA-08's reviewer demanded edits in `app/backend_claude.py` and
`pipeline/execution.py` "which SA-09 now owns" (triage ruling,
2026-10-30 18:43); SA-08 parked waiting on SA-09's merge. SA-08's own
599-line acceptance test (`tests/smoke/test_entrypoint_secrets.py`) was
repeatedly flagged out of its own scope — the story's acceptance surface
was not in its `files`. A file-ownership gate plus cross-file reviewer
demands is a thrash generator unless the seam is declared up front.

### 2.3 Sequencing holds and failure parks are the same state
~15 triage calls in 30 minutes on deliberately-held stories (§1.2). A
staged-rollout hold needs a distinct "do not re-triage until the
dependency event fires" marker; without it the triager's only correct
ruling ("keep parked, wait for human") is also a no-op that guarantees the
next tick re-raises it.

### 2.4 `risk:high` merge adjudication is per-tick, not durable
14/20 stories were `risk:high`; every merge went through adjudication,
which parks for human sign-off. Unattended, "park for human" means "re-fire
next tick with the same evidence": SA-04 ×8 parks (pending → genuinely
red CI → red again), SA-12 proceed→park flip-flop, SA-00/09/13/14/15
similar. Each ruling is individually defensible; the loop is not, because
the ruling is not recorded as discharged keyed to (PR, base-sha).

### 2.5 The artifact blind spot: a change that broke every build of the image merged clean
SA-07's brief specified the correct final URL but an ambiguous
intermediate — "amd64→(gh: linux_amd64, node: linux-x64)" — and the agent
transcribed gh's asset suffix into `GH_ARCH` while the URL template
already carried `_linux_`: every `docker build` of `deploy/Dockerfile`
then requested `gh_2.102.0_linux_linux_amd64.tar.gz` (404). It merged
because **no PR-side check ever built the artifact**: the 9 contract
tests in `tests/smoke/test_image_tooling_contract.py` pin digests,
versions and ordering — structure, not composition — the full pytest suite
passed, and the only real build lives in the post-merge publish job
(`smoke.yml`, master-push only), where failure gates nothing and
notifies nobody. The brief's one real-build check ("build the image once
if docker is available; if unavailable say so in the PR description — do
not fake it") never fired: PR #1427's description is the one-line
auto-generated stub. 25 red publish runs later
(2026-10-30→11-02, runs 4138…–41390170581), `ghcr.io/motock/fagan` is
stale at the pre-SA-07 image and every standalone compose `--build`
fails. **This is the retro's headline defect: "validate the gate against
the real environment" was violated by the entire loop, not the executor —
the planning container has no docker daemon, the dispatch host has no
docker daemon, and PR CI has no build job.** The executor was told to
verify the one thing no component of the harness could verify.

### 2.6 Escalation that changed nothing
`_escalation_target()` resolved through `roles.dispatch` — the same
`ollama/deepseek` model the story had just failed on — so each of the 5
escalations re-ran the same model against the same finding. Fixed
mid-flight by `PIPELINE_AUTO_ESCALATE=off`; the lesson (escalation must
change the executor or the brief, never re-run the failure verbatim) is
now codified as CLAUDE.md Step 9.

### 2.7 The smoke that never smoked
SA-18 ("Run the standalone install and a repo_url plan in a gated CI
smoke") shipped `deploy/standalone/smoke.sh` and
`.github/workflows/standalone-smoke.yml` — gated on
`ENABLE_STANDALONE_SMOKE_CI`, which is off. Every run of that workflow in
history is `skipped`; the standalone install path has never been
exercised end-to-end by CI. Same pattern as the publish job: the story
merged because the gate was deliberately dark, and the plan-level
"verified" signal (green suite) measured the wrong thing.

## 3. What went right

- The mid-flight **"Repair after the first dispatch round"** section is
  the best artifact this plan produced: it diagnosed the scope-gate
  failure mode correctly, invented `SCOPE_CONFLICT`, widened scopes, reset
  poisoned counters, and its rules carried into the follow-up plans —
  which went clean (control group above). Scope derived from the call
  graph + preflight impact runs is the variable that separates this
  plan's first day from its last.
- The high-risk merge adjudicator, whatever its loop cost, **held the
  line** on every red-CI merge (SA-04's `_flavour` failure never merged
  red) and parked credential-handling merges for human review — correct
  per policy.
- Audit/journal/decision plumbing made this retro writable from data
  three days after the fact, with per-dispatch and per-ruling evidence.

## 4. Items

- **P0 — PR CI must build deploy artifacts.** Registered as plan
  `standalone-image-build-fix` (stories IMGURL-1 fix + CIBUILD-1 build
  job) the same day; also fix the 25-run-red publish job by fixing the
  URL itself. *Status: in flight 2026-11-02.*
- **P0 — the standalone smoke must actually run, or the gate must be
  honest about never having run.** Either flip `ENABLE_STANDALONE_SMOKE_CI`
  after one verified green run, or make SA-18-style stories block on a
  real execution, not on a gated workflow existing. Today the
  "installable server" claim rests on no executed install.
- **P1 — sequencing holds need a state distinct from failure parks**,
  released by the dependency's completion event, not re-triaged per tick
  (~15 wasted calls in 30 minutes here).
- **P1 — merge adjudication rulings must be durable** (keyed to PR +
  base-sha): a `proceed` re-parked next tick, and `park` re-adjudicated
  identical evidence up to 8 times, both costing model calls and adding
  no information.
- **P2 — post-merge-only jobs must notify on failure.** The pipeline has
  a notification bus; the publish job's 25 failures reached nobody.
- **P2 — briefs that require verifying an artifact must name where that
  verification can physically run** ("CI is the only host with a daemon;
  the PR description must carry the `docker build` output or say it was
  not run"). A check no component can execute is documentation, not a
  gate.
- **P3 — the Python-floor matrix is the authority.** `_flavour` broke only
  on 3.11; treat local green on a newer interpreter as non-evidence for
  gate-shaped changes.

Marker discharged: `standalone-server` removed from `retros/PENDING.md`.