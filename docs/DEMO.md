# Watching Fagan work

This repository is developed *by* the pipeline it contains. Nothing below is
invented: the pull requests are real and linked, the epic titles are quoted from
the plan, and the journal entries are quoted from the files the agents wrote as
they worked — a labelled subset, not every line. The failure modes are
documented too, in [`retros/`](../retros/) and [`docs/plans/`](plans/).

If you want to see it run yourself, skip to [Try it](#try-it).

## Try it

```bash
curl -fsSL https://raw.githubusercontent.com/motock/fagan/master/scripts/remote-install.sh | bash
```

That clones to `~/.fagan` and runs the installer. Prefer to read before you
pipe: download the script, read it, then run it (it is 39 lines of bash and
fails closed — see its [source](../scripts/remote-install.sh)).

Then check the prerequisites — this builds nothing and calls no model:

```bash
cd ~/.fagan
.venv/bin/python scripts/smoke_getting_started.py --check-preconditions
```

and, once that passes, run one story through to `tests_passed` (the story is
implemented and its tests pass — not merged; the PR/merge gate is not
exercised):

```bash
.venv/bin/python scripts/smoke_getting_started.py
```

Use `.venv/bin/python`, not a bare `python` — the installer puts the `mcp` and
`httpx` dependencies in the virtualenv it creates, so a bare interpreter fails
with `ModuleNotFoundError: No module named 'mcp'` unless you happen to have
them installed globally.

That drives the **real** pipeline — save plan → ingest → dispatch → test
(`tests_passed`) — against a scratch repository and a scratch `PLAN_DIR`, so it
never writes into your real `~/.claude/plans`. The review → PR → merge gate is
**not** exercised: the scratch repo's origin is a local bare repo, which `gh pr
create` cannot target.

It runs on whatever dispatch provider you have configured and says so before
it starts, so you always know which backend you just validated:

```
smoke: validating dispatch on ollama/glm-5.3-flash:cloud (source: env var PIPELINE_BACKEND_DISPATCH)
...
PASS: story S1 implemented, tests passed on provider ollama model glm-5.3-flash:cloud
  final status: tests_passed
```

That transcript is from a real run on 2026-09-16, on a local-family backend at
no API cost. Exit 2 now means your configured provider is empty or
unrecognised — a configuration error — not that you picked a local model. The
`claude` CLI is required only when the resolved provider is `claude`.

Two honest caveats. It spends real model usage on whichever provider you
configured: one implementing agent, and no reviewer, since the run stops before
the review gate. And **PASS depends on the model you configured actually
completing the story** — an exit 4 on a weak local model is that model's
verdict, not evidence the pipeline is broken. That is the price of not pinning
you to one vendor, and it is the right trade. The
[docstring](../scripts/smoke_getting_started.py) lists every exit code and what
each one means.

## A real run: five stories, planned and merged autonomously

On 2026-09-15 the pipeline was handed one goal: the chat view was telling the
model to call `ingest_plan`, which cannot run from a chat origin. It decomposed
that into three epics and five stories, implemented them across dependencies,
and merged all five. The epic titles, verbatim:

- **Chat prompt must agree with the origin gate it runs behind** — stop
  instructing `ingest_plan` from chat, and mark the two chat-refused tools
  UI-ONLY.
- **Give the chat's ingest hand-off a real human surface** — a pure DOM-free
  ingest client, the panel markup in the Comms view, and the wiring that turns a
  typed plan name into a UI-origin POST.
- **Docs match the surface that actually exists** — correct the README's
  standalone-mode claim that the dashboard exposes every MCP operation.

Each story became one pull request:

| Story | PR |
|---|---|
| Stop instructing `ingest_plan` from chat; mark the two chat-refused tools UI-ONLY | [#782](https://github.com/motock/fagan/pull/782) |
| Add `static/app/ingest.js` — a pure, DOM-free plan-ingest client module | [#783](https://github.com/motock/fagan/pull/783) |
| Add the ingest panel markup to the Comms view | [#784](https://github.com/motock/fagan/pull/784) |
| Wire the panel in `static/app/comms.js` | [#787](https://github.com/motock/fagan/pull/787) |
| Correct the README's standalone-mode claim | [#788](https://github.com/motock/fagan/pull/788) |

### What the agent actually did

Agents write a checkpoint journal as they work, so a killed run can resume
instead of starting over. These are six of the eleven entries from the fourth
story (`static/app/comms.js` wiring), quoted exactly as the agent wrote them:

> **step-2-assertions-recorded** — Recorded all test assertions verbatim in
> .agent_scratchpad.md (ids, fetch url/method/header/body, encoding, keydown
> keys, empty-input, 400 detail, rejection, success story count, XSS escaping,
> structural checks).
>
> **step-6-function-added** — Added async function submitIngestPlan() after the
> module-load applyTraceVisibility() call: normalize -> empty guard (textContent
> 'enter a plan name', no fetch) -> pending -> await ingestPlan inside try ->
> renderIngestStatusHtml success/failure, catch renders failure. node --check OK;
> pinned helpers untouched.
>
> **step-7-wiring-added** — Added click listener (ingestPlanSubmitButton ->
> submitIngestPlan) and keydown listener (ingestPlanNameInput, gated on event.key
> === 'Enter'). Briefly deleted the if (!name) guard with a bad anchor; restored
> it in the next edit and verified the full function body. node --check OK.
>
> **step-8-node-suite-green** — node test_ingest_ui_wiring.mjs: 19/19 passed,
> exit 0 — all behaviour (POST once, encoding, whitespace, Enter-only keydown,
> empty guard, 400 detail, rejection caught, success story count, XSS escaping)
> and structural checks green.
>
> **step-11-full-suite-green** — Full suite green: 10632 passed, 10 skipped.
> Fixed 14 test_dashboard_comms_subtitle.py failures by guarding the two new
> addEventListener calls with if (el) — the subtitle harness's getElementById
> returns null for the new ids, so unguarded wiring threw at module load. ruff
> clean; git status shows only static/app/comms.js modified.
>
> **committed-and-pushed** — Squashed the wip checkpoint commits into one
> feat(CIH-4) commit (a065b26, 37 insertions in static/app/comms.js only),
> re-verified node 19/19 + pytest 56 passed + ruff clean on the committed state,
> and pushed agent/cih-4 to origin.

Two things in there are worth noting, because they are the pipeline working as
designed rather than a demo being flattering:

- **A mistake is recorded, not hidden.** Step 7 deleted a guard with a bad
  anchor and restored it — the journal kept the error rather than rewriting
  history.
- **It caught its own regression.** Step 11's 14 subtitle-test failures were
  caused by the story's own wiring; the agent diagnosed the cause (unguarded
  `addEventListener` against a null element), fixed it, and re-ran everything
  before committing.

## What every merge has to pass

A story is not merged because an agent said it was finished. Before merge the
pipeline requires all of:

- **A failing test first.** TDD is enforced by writing the test suite for a
  story *before* the implementing agent runs (`pipeline/test_author.py`). If the
  two run on different model families, the split degrades and says so.
- **An independent acceptance oracle.** Where a story ships one, it is
  materialized read-only into the agent's worktree and the agent cannot edit it.
- **A review gate** with its own checklist
  ([`.claude/rules/code-review.md`](../.claude/rules/code-review.md)),
  including a mandatory finding for any modified existing test without a stated
  justification.
- **CI on the real repository**, re-run against the rebased branch before merge.

Separately from the merge gate, the benchmark harness grades merged code against
a ground-truth suite the implementing model never sees. That is how the pipeline
measures *itself* rather than taking an agent's word for it — and how a
"merged but wrong" change gets caught. It is not part of the normal merge path.

## What it can't do yet

Read these before pointing it at anything you care about — they are the reason
this is a research project and not a product:

- **Local (non-Claude) dispatch is the weak point.** It handles small,
  mechanically-scoped stories well and degrades sharply on larger ones.
  [`retros/`](../retros/) is the incident record, not marketing.
- **Nothing exercises the end-to-end path in CI, and it silently rotted.** The
  getting-started smoke — the script whose entire job is answering *"does a
  fresh install actually work?"* — was broken from the day it was added
  (2026-09-02) until 2026-09-16, by four independent defects stacked so each
  one masked the next: a crash at ingest on a malformed field, a scratch repo
  with no `origin` for dispatch to fetch, a success bar that demanded a GitHub
  PR the scratch repo could never produce, and a test-command detector that ran
  `npm test` against a repo with no `package.json`. Its unit tests passed the
  entire time, because they mock the pipeline. A live end-to-end test does
  exist, but it is skipped unless `SMOKE_E2E=1`, and CI has no `claude` CLI — so
  it had never run. All four are fixed
  ([#791](https://github.com/motock/fagan/pull/791)–[#794](https://github.com/motock/fagan/pull/794),
  [#802](https://github.com/motock/fagan/pull/802)), but the lesson outlives
  them: a green suite is not evidence the thing works end to end. Only running
  it end to end is.
- **The local memory floor still wants an env var on this machine.** Dispatch
  to a local (non-`:cloud`) model is gated on free memory, and the 2048 MB
  default floor sits inside this host's normal idle band (~1.8–2.1 GB) — so an
  on-device story that has not started yet can be deferred indefinitely while
  the floor goes unmet. The worse version of this, which is what a run on
  2026-09-15 actually hit, was that a tripped gate would *interrupt the agent
  already running* and redispatch it straight back into the same wall: because
  the agent's own resident weights are much of what depresses the reading, the
  gate was partly tripped by the very work it killed. Measured that day: 26
  interrupts in 16 minutes, zero progress. That interrupt defect is fixed
  ([#790](https://github.com/motock/fagan/pull/790), merged the same day, with
  regression tests). Setting `PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_OLLAMA=0` —
  which this repository's own scheduler already does — clears both the fix's
  predecessor and the remaining deferral. **Whether the default floor itself
  should come down is still open**: a gate that can withhold work should be
  biased against blocking work already known to run.
- **There is no clean cross-backend benchmark on record yet.** The one full
  model-comparison run was contaminated mid-run by rate limits
  ([`tests/benchmark/FINDINGS.md`](../tests/benchmark/FINDINGS.md)). The
  cleanest number that file does carry is narrow — `gpt-oss:20b` on-device,
  two small tasks, 2/2 success with the independent oracle green on the merged
  code, one trial each. That is directionally encouraging and statistically
  meaningless; do not read it as a model comparison. The `$20/month` framing in
  the README is the design *goal*, not a measured result.
- **A story marked `done` is not proof its title's full scope shipped**, and a
  green test suite is not proof of a correct change. Both are documented in
  [`docs/plans/`](plans/) with the incidents that produced them.
- **This is a single-maintainer project** with no SLA.

## Where to look next

| If you want to… | Read |
|---|---|
| Understand the architecture | [`README.md`](../README.md#architecture) |
| See the failure-mode catalog | [`retros/`](../retros/) and [`docs/plans/`](plans/) |
| Run the benchmark yourself | [`tests/benchmark/README.md`](../tests/benchmark/README.md) |
| Read the rules the agents work under | [`CLAUDE.md`](../CLAUDE.md) and [`.claude/rules/`](../.claude/rules/) |
