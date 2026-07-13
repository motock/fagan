# Pipeline MCP Reliability Hardening — Direct Execution Plan

**Source:** 2026-07-07 web-client-epic session retro (`e2e-decentralized-messaging`).
**Mode:** Execute directly in this repo (`~/.claude/mcp-servers/pipeline`). Do **not** route
through the pipeline's own story workflow. Standard TDD still applies: write the failing
test first, then the change.

**Files in play:**
- `pipeline_mcp_server.py` (3100 lines) — tools + manifest IO + merge gate
- `backend.py` (957 lines) — local model driver (dispatch + review loops)
- `test_pipeline_mcp_server.py` — suite (~373 tests at session start); mirror its style,
  add new tests, **do not modify existing tests** without flagging first.

**Test runner:** `pytest` (pyproject/pytest present). Run `pytest -q` after each task and
the full suite at the end. Report pass/fail counts.

**Suggested order:** T1 → T2 (they remove the hand-editing that caused the worst incidents),
then T5 (small, isolated, high-clarity), then T3 + T4 (merge-gate hardening), then T6.

---

## T1 — Stop `ingest_plan` from clobbering an existing manifest  *(confirmed data-loss bug)*

**Where:** `pipeline_mcp_server.py`, `ingest_plan()` at line 1580; manifest built fresh at
line 1605 (`manifest = {"epics": {}, "stories": {}, "repo_root": repo_root}`) and
`_atomic_write_json`'d wholesale at line 1674. `only_epics` (line 1620) only filters
iteration — it can never merge, so re-running on a live plan destroys all prior stories'
`status`/`pr_url`/`review_verdict`/history.

**Change:** Make the destructive overwrite opt-in and merge-by-default.
1. Before building the new manifest, load the existing one if `manifest_path` exists.
2. For each story key produced this run:
   - If the key already exists in the old manifest, **preserve** its runtime fields
     (`status`, `pr_url`, `review_verdict`, `worktree`, `journal`, any rework counters) and
     only refresh the plan-authored fields (`summary`, `agent_instructions`, `dependencies`,
     `persona`, `model`, `acceptance`, `risk`). A re-ingest is a plan edit, not a reset.
   - If the key is new, add it as `status: "todo"` (current behavior).
3. **Keep** any story present in the old manifest but absent from this run's epics (this is
   the `only_epics` case — those stories must survive untouched).
4. Add a parameter `overwrite: bool = False`. Only when `overwrite=True` do you build from
   scratch and drop absent stories (the current behavior, now explicit and opt-in).
5. Preserve top-level runtime keys on merge: `paused`, `local_model_fallback`, and any other
   non-`epics`/`stories`/`repo_root` field already in the old manifest.

**Acquire `_plan_lock(plan_name)`** around the read-merge-write so a concurrent scheduler tick
can't interleave (model it on `dispatch_story` line 1778 / `_set_plan_paused` line 3032).

**Tests (new):**
- Re-ingesting a plan with `only_epics=[new epic]` preserves all pre-existing stories and their
  `status`/`pr_url`.
- Re-ingesting a story whose key already exists refreshes `agent_instructions` but keeps a
  `status: "done"` and its `pr_url`.
- `overwrite=True` reproduces the old wholesale-replace behavior.
- Top-level `paused: true` / `local_model_fallback` survive a merge re-ingest.

---

## T2 — Add a first-class `patch_story` (and `set_story_status`) tool  *(kills hand-editing)*

**Why:** The `_plan_lock` flock (line 2526) serializes *tool calls* but a text-editor/Write
edit takes no lock, so it races the 60s scheduler tick (retro §6, hit twice). The fix is to
make the edits the user actually needed available as lock-holding tools so nobody hand-edits
the JSON.

**Where:** New `@mcp.tool()`s in `pipeline_mcp_server.py`. Model the body on
`mark_story_in_progress` (line 2143) **but acquire `_plan_lock`** (those existing mutators
currently don't — do not "fix" them here, just don't copy that gap).

**`patch_story(plan_name, story_key, fields: dict)`:**
- Allow-list the editable keys: `agent_instructions`, `model`, `persona`, `risk`,
  `dependencies`, `acceptance`, `pr_url`, `summary`. Reject any key outside the allow-list with
  a clear error (deny-by-default — do not let it set `status` or `worktree`; those have their
  own paths).
- Under `_plan_lock`: read manifest, verify `story_key` exists (else `{"ok": False, ...}`),
  apply only allow-listed fields, `_atomic_write_json`, return the updated story.

**`set_story_status(plan_name, story_key, status)`:**
- Allow-list valid target statuses (`todo`, `in_progress`, `tests_passed`, `interrupted`,
  `parked`, `done`, `pr_open` — cross-check the exact set the code uses via
  `grep -n '"status"\] = ' pipeline_mcp_server.py`).
- Under `_plan_lock`. This is the sanctioned, auditable replacement for hand-resetting a
  story (e.g. `parked` → `interrupted` for the scheduler to retry) — the operation the retro
  §8 permission-classifier flagged when done by raw edit.

**Tests (new):** patch refreshes an allow-listed field; patch rejects a non-allow-listed key;
patch on a missing story returns `ok:False`; `set_story_status` to a valid status works and to
a garbage status is rejected; both serialize under the lock (can assert the lock is taken by
mirroring an existing lock-based test).

---

## T5 — Make the local-reviewer step cap honor live env, like dispatch does  *(explains §7)*

**Root cause (found in code):** In `backend.py`, `review_max_steps` is read **once** in
`__init__` (line 458: `self.review_max_steps = int(os.environ.get("PIPELINE_LOCAL_REVIEW_MAX_STEPS","20"))`)
and consumed from `self.` in the review loop (line 605: `for i in range(self.review_max_steps)`).
The **dispatch** cap, by contrast, is re-read **live** at dispatch time (line 846:
`int(os.environ.get("PIPELINE_LOCAL_MAX_STEPS", str(self.max_steps)))`). That asymmetry is
exactly why raising `PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS`/`MAX_STEPS` propagated in-session
but raising `PIPELINE_LOCAL_REVIEW_MAX_STEPS` did not.

**Change:** In the review loop (line ~605), read the cap live the same way dispatch does:
```python
review_max_steps = int(os.environ.get("PIPELINE_LOCAL_REVIEW_MAX_STEPS", str(self.review_max_steps)))
for i in range(review_max_steps):
    ...
    remaining = review_max_steps - i   # update line 621 too
```
Leave the `__init__` default assignment as the fallback. This makes review symmetric with
dispatch and makes in-session config edits take effect without restarting the MCP process.

**Separate hypothesis to note, not fix here:** even with live reads, a launchd plist env
change only reaches *newly launched* processes; a long-lived in-session MCP server keeps its
launch-time `os.environ`. That's an operational caveat (restart the session's MCP server, or
set the var in the session's own env) — document it in the docstring near line 839's existing
note, don't try to solve it in code.

**Tests (new):** with `PIPELINE_LOCAL_REVIEW_MAX_STEPS` monkeypatched *after* the backend is
constructed, the review loop honors the new value (mirror however dispatch's live-read is
tested; if untested, add a small test that patches the env and asserts the loop bound). Keep
the existing default-20 behavior test green.

---

## T3 — Close the `_ci_status` `none`-grace hole + require the gate on human merges  *(§4)*

**Context:** The CI gate works — `approve_merge` (line 2967) already runs
rebase → force-push → `_ci_status` → `_reverify_acceptance` → merge. PR #48 slipped through
because it was merged by hand (`gh pr merge`), bypassing `approve_merge` entirely, **and**
because `_ci_status` (line 927) treats `none` (no checks reported yet) as pass (line 956),
which is indistinguishable from "repo has no CI."

**Change A — grace-poll for checks to register (`_ci_status`, line 927):**
- Before accepting `none` as pass, distinguish "repo has CI configured" from "no CI." Detect a
  workflows dir once: `(<repo>/.github/workflows).is_dir()`. Thread the repo path in (the
  caller has `REPO_ROOT`/`_scoped_repo_root`), or accept it as an arg.
- If CI **is** configured but `gh pr checks` returns empty/`none`, keep polling until the
  existing `deadline` for checks to *appear*, instead of immediately returning pass. Only
  return `none`→pass when there is genuinely no workflows dir. This closes the
  merge-before-CI-registers window that let PR #48's red Linux job through.
- Preserve the existing `gh`-absent / non-zero-exit / unparseable → `none` behavior (a machine
  without `gh` must not be blocked).

**Change B — behavioral, cheap:** Update `CLAUDE.md`'s Definition of Done / Step 7 to state
explicitly: a story is not "done" on `review_story` APPROVE + local tests — the merge must go
through `approve_merge` (or the scheduler's merge path), or, for a genuinely manual merge, a
recorded `gh pr checks <branch>` all-green confirmation. "Reviewer approved + I tested on
macOS" is insufficient for native/platform-specific deps (npm optional deps, Rust build
scripts). This is a doc gate, not code.

**Tests (new):** `_ci_status` returns `pass` when no `.github/workflows` exists and checks are
empty; returns `pending` (not `pass`) when workflows exist but checks are empty and the
deadline elapses; still returns `fail`/`pass` correctly when buckets are present. Mock the
`gh` subprocess (mirror existing `_ci_status` tests — `grep -n "_ci_status" test_pipeline_mcp_server.py`).

---

## T4 — Add a build step to the pre-merge reverify  *(§3 — catch compile/build breaks)*

**Context:** The reviewer is an agent reading the diff; it trusts agent-reported "tests pass"
and never runs `npm run build`. `_reverify_acceptance` (line 966) re-runs the *test* suite
before merge but not the *build* — so §3.1's `npm run build` failure (Node `crypto` can't
bundle for browser) and the `tsconfig` JSX break were only caught by a human.

**Change:** Add a best-effort build reverify that runs alongside `_reverify_acceptance` in the
merge path (both `approve_merge` line ~3018 and the scheduler merge path near line 2886–2919).
- Detect a build command from the same signals `detect_test_command` uses:
  `package.json` with a `build` script → `npm run build` (respect `yarn.lock`);
  `Cargo.toml` → `cargo build`; extend conservatively, allow-list only.
- Run in the rebased worktree. On non-zero exit → block the merge with a clear error
  (`{"ok": False, "error": "build reverify fail: ..."}`), same shape as the acceptance-fail
  branch at line 3019.
- If no build command is detectable → return a `none`/skip state and **do not** block (a repo
  without a build step must merge freely). Gate the whole thing behind
  `PIPELINE_MERGE_BUILD_GATE` (default on) so slow-build repos can opt out, mirroring
  `PIPELINE_REVERIFY_FULL_SUITE`.

**Tests (new):** build-gate blocks merge when the detected build command exits non-zero;
skips (does not block) when no build command is detectable; honors `PIPELINE_MERGE_BUILD_GATE=0`.
Mock the subprocess; mirror `_reverify_acceptance` tests.

---

## T6 — Classify repeated give-up / syntax-terminal as "likely under-specified"  *(§3.2, lowest confidence)*

**Context:** The WASM story failed twice because a referenced API genuinely didn't exist — a
story-scoping bug, not a model-capability one. No cheap automated fix for "references a
missing API," but the pipeline can stop burning rework budget on identical retries.

**Change:** When a story's dispatch terminates twice on the *same* non-productive terminal
state — explicit give-up ("I can't complete this task") or a compile/syntax terminal (never
compiled) — surface a distinct signal (`needs_clarification` / a `park` reason string) instead
of a third identical auto-retry. Find where terminal state + rework counters are handled
(`grep -n "rework\|park\|terminal\|gave up\|give up\|attempts" pipeline_mcp_server.py backend.py`)
and add the classification + a one-line reason the human sees. This is a *routing/label* tweak,
not new gating — keep it small; if the existing rework machinery doesn't cleanly expose the
terminal reason, stop and report what's needed rather than reworking that machinery.

**Tests (new):** a story hitting the same give-up terminal twice is routed to
`needs_clarification`/park-with-reason rather than re-dispatched a third time; a story that
makes progress between attempts is unaffected.

---

## Final verification (all tasks)

1. `pytest -q` — full suite green; report pass/fail counts (baseline was ~373 passing).
2. Confirm no existing test was modified (git diff the test file).
3. Sanity-check the touched tools load: `python -c "import pipeline_mcp_server"` (import must
   not raise) and, if practical, a smoke `ingest_plan` merge round-trip on a throwaway plan in
   a temp dir.
4. Present a per-file change summary + Conventional-Commit message per task (these are logically
   separate commits: `fix(pipeline): merge ingest_plan instead of clobbering manifest`,
   `feat(pipeline): add patch_story/set_story_status tools`,
   `fix(pipeline): read local-review step cap from live env`,
   `fix(pipeline): close CI-gate none-grace hole`,
   `feat(pipeline): build reverify before merge`,
   `feat(pipeline): flag repeat give-up stories as under-specified`).
   Ask before committing.

---

## Source: 2026-07-12 follow-up (`web-client-production-hardening` plan, e2e-decentralized-messaging)

Observed on a fully-local run: `PIPELINE_BACKEND_DISPATCH=local`, `PIPELINE_BACKEND_REVIEW=local`,
dispatch `gpt-oss:20b`, review + `local_model_fallback` `glm-5.2:cloud`. T1/T2/T5/T3/T4/T6 above
all look already landed per `git log` (#76, #77, #79, #87, #90, #92, #93 etc.) — this section is
new findings from that later session, not a re-report of the above.

### T7 — `auto_wip_commit` commits unverified/unparseable diffs on timeout or park *(confirmed)*

**Where:** `scripts/local_agent.py:451-454`:
```python
def auto_wip_commit(reason: str) -> None:
    git("add", "-A")
    git("commit", "-m", f"WIP ({reason})")
```
Called unconditionally from three sites when the loop terminates early: the wall-clock-timeout
path (line ~663), the repetition-park path (line ~739), and both read-heavy-park paths
(line ~789, ~808) — none of them validate the staged diff first. There **is** a defense-in-depth
syntax guard for Python writes (`_python_syntax_error`, line 467) applied at write-time via
`run_tool`, but nothing equivalent for TS/JS/anything else, and even the Python guard doesn't
run again at commit time — it only rejects a single bad write, not a diff assembled across many
edits.

**Observed impact (this session):** the backup-storage-key story's first dispatch hit the
wall-clock timeout mid-edit and committed `"WIP (wall-clock timeout)"` with a syntax-broken
TypeScript file (a dangling function body — would not compile); its first rework attempt
committed `"WIP (wall-clock timeout)"` again with a missing import (`getStoragePassword` used
but not imported) — a `tsc --noEmit`-catchable error. Both were later caught by review, but only
after a full review round-trip each time, burning 2 of the story's 3 rework attempts on defects
a cheap local check could have caught before the commit even landed.

**Change:** Before `auto_wip_commit`, run a fast, best-effort syntax/typecheck pass over the
files the loop actually touched this run (track touched paths — `run_tool`'s write path already
knows them for the Python guard) and fold the result into the commit:
1. Extend `_python_syntax_error`'s pattern to TS/JS: if `tsc --noEmit` (or a faster single-file
   parse check, e.g. `node --check` for plain JS — TS needs `tsc`) is available in the repo and
   completes within a short budget (a few seconds, not the full build), run it and capture
   failures. Don't invent a new dependency — detect `tsconfig.json`/`node_modules/.bin/tsc`
   the same conservative, allow-list way `detect_test_command`-style helpers already do
   elsewhere in this codebase.
2. If the check fails, do **not** silently commit as if nothing happened: put the failure output
   in the commit message body (e.g. `WIP (wall-clock timeout) — KNOWN BROKEN: <first error
   line>`) so a resumed run (this repo already resumes from the prior transcript per #93) or the
   reviewer immediately sees it's non-compiling, rather than re-discovering it from scratch.
3. If no checker is detectable for the touched language, commit as today (this must stay a
   best-effort addition, not a new hard requirement — a repo/language without a fast local check
   must not block the WIP commit that exists specifically to not lose work).

**Tests (new, in `test_local_agent.py`):** a touched `.ts` file with a syntax error produces a
WIP commit whose message includes the error; a touched `.ts` file that's valid produces the
existing plain `WIP (<reason>)` message unchanged; no `tsc` available → falls back to today's
unconditional commit (existing behavior preserved, add a regression test if none covers this
already).

---

### T8 — Security-persona local-skip only applies under `PIPELINE_BACKEND_DISPATCH=auto` *(confirmed, design gap)*

**Where:** `pipeline_mcp_server.py:1926`, `_route_dispatch_backend()`:
```python
def _route_dispatch_backend(story: dict[str, Any]) -> str:
    """A-priori backend choice for a new dispatch (called only when
    PIPELINE_BACKEND_DISPATCH=auto). ...
    """
```
`_LOCAL_SKIP_PERSONAS = {"security-engineer"}` (line 186) and the risk-ceiling check
(`PIPELINE_LOCAL_MAX_RISK`, line 185) only take effect inside this function, which its own
docstring says is called **only** when `PIPELINE_BACKEND_DISPATCH=auto`. Under explicit
`PIPELINE_BACKEND_DISPATCH=local` (a legitimate, documented mode — see `README.md`'s backend
routing section), every story dispatches to `local` unconditionally, security persona or not.

**Observed impact (this session):** the `web-client-production-hardening` plan's one
`security-engineer`/high-risk story (a storage-encryption-key fix) was dispatched **and**
reviewed entirely by local models (`gpt-oss:20b` dispatch, `glm-5.2:cloud` review) with zero
automatic Claude involvement, on a plan that also had `local_model_fallback` configured — so
even the step-cap-to-Claude escalation added in #90 never applies either, since that escalation
is explicitly suppressed whenever `local_model_fallback` is set (per this file's own note in
"Notes carried from the retro" below and #90's commit message). There is currently no code path
under explicit `local` mode that ever routes a security-persona story to Claude. This contradicts
the intent stated in `README.md`'s backend-routing section ("Security-persona stories always go
to Claude regardless of this setting") — that sentence is true only for `auto`, not for explicit
`local`, and is not caveated as such in the README.

**Change (needs a product decision, not just a mechanical fix — flag before implementing):**
Pick one:
- **(a) Hard override, all modes:** apply the `_LOCAL_SKIP_PERSONAS` check unconditionally at
  dispatch time, regardless of `PIPELINE_BACKEND_DISPATCH`. A security-engineer-persona story
  always resolves to the Claude backend, even under explicit `local`. This matches the README's
  current wording literally, at the cost of "explicit `local`" no longer meaning "everything runs
  local, no exceptions" — worth confirming that's an acceptable behavior change for anyone
  relying on fully-local runs today.
- **(b) Warn, don't override:** leave routing as-is under explicit `local`, but log a loud
  one-time warning (and consider surfacing it in `advance_pipeline`'s return payload, e.g. a
  `notify` entry) the first time a security-persona story dispatches on `local`, so an operator
  running `local` intentionally still gets a visible signal rather than silent divergence from
  the documented behavior.
- At minimum, fix the README/docstring wording regardless of which behavior is chosen, so it
  accurately scopes the guarantee to `auto` only if (b) is chosen.

**Tests (new, in `test_pipeline_mcp_server.py`):** for whichever option — (a): a
security-engineer-persona story dispatched under `PIPELINE_BACKEND_DISPATCH=local` resolves to
the Claude backend, not local. (b): the same scenario produces a `notify`/warning signal in
`advance_pipeline`'s result. Either way, add a test that explicit `local` with a **non**-security
persona is unaffected (still dispatches local) — this must not become an `auto`-only behavior
being silently generalized to override an operator's explicit `local` choice for ordinary stories.

---

### T9 — Fallback model (`local_model_fallback`) has no entry in `_LOCAL_MODEL_TUNING` *(confirmed, lower priority)*

**Where:** `backend.py:283-290`. The tuning table has one empirically-derived entry
(`"gpt-oss:20b": {"temperature": 0.3, "num_ctx": 32768}`, from the 2026-07-03 A/B benchmark
cited in the comment above it). `glm-5.2:cloud` — the model this repo's plans set as
`local_model_fallback` specifically because the primary is struggling — has no entry, so
`_tuned_temperature`/`_tuned_num_ctx` (line 294/302) fall through to `OllamaDriver`'s
constructor defaults (`PIPELINE_LOCAL_NUM_CTX` env or 16384; `PIPELINE_LOCAL_TEMPERATURE` env or
0.3) rather than anything benchmarked for that model specifically.

**Observed impact (this session):** one story (`Group conversation: real member key
distribution`) needed 3 total dispatch attempts — including 2 on the fallback model — before
producing any commits at all; not conclusive evidence the untuned config caused this (could
equally be story complexity), but it's a real gap: the model invoked *because* something else
struggled is running on default config, unvalidated.

**Change:** Run the same kind of A/B benchmark methodology already used for `gpt-oss:20b`
(`tests/benchmark/` per the existing comment's pointer) against `glm-5.2:cloud` at a couple of
temperature/num_ctx combinations, and add a tuned entry once settled — same as the existing one,
with a dated comment citing the run. If no bandwidth to benchmark properly right now, at minimum
leave a `# TODO: benchmark glm-5.2:cloud, see 2026-07-03 gpt-oss:20b methodology` comment in the
table so it isn't silently forgotten.

**Tests:** none required for a data-only table addition beyond whatever `test_backend.py` already
asserts about `_tuned_temperature`/`_tuned_num_ctx` reading the table (confirm those tests are
parameterized over model tag, not hardcoded to `gpt-oss:20b`, so a new entry gets exercised for
free).

---

### Minor — `[parking: ...]` log text prints even when `LOCAL_AGENT_PARK_ENABLED=0` suppresses the actual park

**Where:** `scripts/local_agent.py` lines ~739-745 and ~787-798/806-816. Confirmed by reading
the code, not a bug: `PARK_ENABLED` (line 225) correctly gates whether the function *returns*
(actually parks/terminates) — when `0`, it falls through to `break`/`continue` and the step cap
is the real bound, as the inline comment says ("Suppressed: let the nudge steer and continue...
The step cap bounds the run"). But the `print("   [parking: ...]", ...)` line above that gate is
unconditional, so a `LOCAL_AGENT_PARK_ENABLED=0` run's log reads as if it parked several times
when it actually kept going every time. The wall-clock-timeout path (line ~661) is unaffected —
it is not, and should not be, gated by `PARK_ENABLED` at all (it's a hard time bound, not a
stuck-model heuristic), so no change needed there.

**Change (cosmetic, low priority):** when `PARK_ENABLED` is `False` and the code is about to
`break` instead of `return 3`, change the log line to something like
`"   [would park: repeated action after nudge — continuing, PARK_ENABLED=0]"` so operators
reading a `LOCAL_AGENT_PARK_ENABLED=0` run's log aren't misled into thinking parking happened
when it didn't.

**Tests:** a log-text assertion in `test_local_agent.py` if one already captures stdout for this
path; skip if none does and this isn't worth adding test infra just for a log string.

---

---

### T10 — Scale local dispatch step-cap/timeout by story complexity instead of one flat global value *(confirmed plumbable, needs a scaling heuristic decision)*

**Context:** `PIPELINE_LOCAL_MAX_STEPS` and `PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS` are single
global env values applied identically to every story on the local backend, regardless of scope —
a one-line CSS/meta-tag story and a multi-file TDD feature get the same budget. Observed directly
this session: the group-membership story (medium risk, multi-file, new test file + iterative
debug cycles) needed 3 separate dispatch attempts just to accumulate enough cumulative *step*
budget across resumes, despite making real, non-repetitive progress each time — not stuck, just
under-budgeted. The backup-storage-key story (high risk) showed the same shape: several nudge/park
cycles before its first edit landed, well within a single 60-step run.

**Where — confirmed both values are resolved with zero story context:**
- `backend.py:826` `OllamaDriver.dispatch()`'s signature (`prompt, system, model, allowed_tools,
  cwd, log_path, append, acceptance, resume_transcript_path, resume_append_content`) has no
  `risk`/complexity parameter.
- `backend.py:846-848` resolves `max_steps` from `PIPELINE_LOCAL_MAX_STEPS` (env only); `:831`
  writes `LOCAL_AGENT_TIMEOUT` from `self.dispatch_timeout` (also env-only, resolved once in
  `__init__` at line 466). Neither considers the story being dispatched.
- **The call site already has everything needed, unused:** `pipeline_mcp_server.py:2562-2568`
  builds `dispatch_kwargs` from the full `story` dict (which has `story["risk"]`) immediately
  before `backend.get_backend("dispatch", name=dispatch_backend).dispatch(**dispatch_kwargs)`
  (line 2581) — this is the exact same pattern already used to conditionally thread `acceptance`
  through only for the local backend (line 2568: `if dispatch_backend == "local" and
  acceptance_paths: dispatch_kwargs["acceptance"] = acceptance_paths`). Threading `risk` through
  the same way is a small, precedented change, not new plumbing.

**Change:**
1. Add an optional `risk: str | None = None` parameter to `OllamaDriver.dispatch()`.
2. At the `max_steps`/`dispatch_timeout` resolution points (`backend.py:846`, `:831`), apply a
   risk-tier multiplier to the resolved base value before writing it into the subprocess env —
   e.g. `{"low": 1.0, "medium": 1.5, "high": 2.0}` — configurable via new env vars (mirror the
   existing naming: `PIPELINE_LOCAL_STEP_RISK_MULTIPLIER_MEDIUM`/`_HIGH`,
   `PIPELINE_LOCAL_TIMEOUT_RISK_MULTIPLIER_MEDIUM`/`_HIGH`), **defaulting to `1.0` for every
   tier** so this is opt-in and reproduces today's flat behavior until explicitly configured —
   same "gate behind an env var, default preserves current behavior" pattern as T3/T4 above.
3. In `pipeline_mcp_server.py`, thread `story.get("risk")` into `dispatch_kwargs["risk"]` only
   for the local backend, mirroring the `acceptance` precedent at line 2568.
4. **Do not** conflate this with `_route_dispatch_backend`'s risk-based Claude-routing (T8 above,
   `auto`-only) — that decides *which backend*; this decides *how much budget* once local is
   already the chosen backend, and applies regardless of `PIPELINE_BACKEND_DISPATCH` mode.
5. Story-count-of-files / agent_instructions-length is a plausible richer heuristic than the flat
   `risk` tier, but `risk` is already authored on every story today (no new field, no plan schema
   change) — start there; revisit a finer-grained heuristic only if risk tier alone proves too
   coarse in practice.

**Tests (new, in `test_backend.py`):** `dispatch(risk="high")` writes a larger
`LOCAL_AGENT_MAX_STEPS`/`LOCAL_AGENT_TIMEOUT` into the subprocess env than `risk="low"`, with the
multiplier env vars unset (defaults) reproducing today's exact flat value; `risk=None` (existing
callers, back-compat) also reproduces today's flat value unchanged; multiplier env vars, when
set, take effect without restarting the MCP server (mirror the live-read pattern already tested
for T5/`PIPELINE_LOCAL_MAX_STEPS`).

---

---

## Source: 2026-07-12 local-agentic-workflow review (goal: local implements, cloud only reviews)

Reviewed against the fully-local production deploy (`PIPELINE_BACKEND_DISPATCH=local`,
`PIPELINE_BACKEND_REVIEW=local`, dispatch `gpt-oss:20b`, review + `local_model_fallback`
`glm-5.2:cloud`). T1–T6 confirmed landed; T7–T10 above still open. These three are new findings
from that review, prioritized because under `local` mode there is **no escalation valve** — a
bad reviewer or a wasted rework attempt parks correct work for a human instead of being caught
by an a-posteriori Claude escalation (`_auto_escalation_enabled()` is false whenever dispatch is
explicit `local`, not `auto` — see T8, `pipeline_mcp_server.py:1754`).

### T11 — REQUEST_CHANGES with no substantive findings still burns rework budget *(confirmed)*

**Where:** `pipeline_mcp_server.py:3399-3414` (the `else` branch of `if verdict == "APPROVE":` in
`review_story`). FM-B already special-cases rate-limit and transient-backend-error responses
(`_is_rate_limited`, `_is_transient_backend_error`, checked only when `_parse_verdict` returns
`UNKNOWN`) so those don't touch `rework_attempts`. But a response that **does** parse a clean
`VERDICT: REQUEST_CHANGES` line with little or no findings text above it — e.g. the reviewer
producing just the verdict line, or a one-sentence non-actionable rejection — is treated
identically to a genuine, detailed rejection: `story["review_feedback"] = reviewer_output` is
saved as-is and `rework_attempts` increments unconditionally (line 3405).

**Observed impact:** `project_gptoss_run_learnings.md` records a 2026-07-02 run where 3 of the
run's parks were **not** genuine reviewer findings — 2 were `UNKNOWN`+empty feedback (now caught
by FM-B) and 1 was exactly this case: a parseable `REQUEST_CHANGES` with no findings text,
which burned rework budget blind. With the current 3-attempt cap and zero escalation valve under
`local` mode, one content-free rejection consumes a third of a story's entire budget for nothing
the redispatched agent can act on.

**Change:** After `_parse_verdict` returns `REQUEST_CHANGES`, check whether `reviewer_output`
contains any content beyond the verdict line itself (e.g. strip the `VERDICT:` line and anything
before a recognizable "no issues" boilerplate, then check remaining non-whitespace length against
a small floor — mirror the conservative, pattern-based style already used by `_is_rate_limited`/
`_is_transient_backend_error` rather than inventing a new NLP heuristic). If the findings are
empty/negligible:
- Treat it like the inconclusive path (line 3367's `if verdict == "UNKNOWN":` block) — increment
  `review_inconclusive_count`, not `rework_attempts`, and retry review next tick rather than
  redispatching on empty feedback.
- Log a distinct notify reason (e.g. `"review approved-changes-requested-empty"`) so this is
  distinguishable from a genuine rate-limit defer or a real rejection in `advance_pipeline`'s
  notify stream.
- Do **not** touch this path if findings text is present, however short — a terse but
  substantive rejection ("missing null check on line 40") must still count against the budget;
  only a genuinely empty/boilerplate response should be treated as inconclusive.

**Tests (new, in `test_pipeline_mcp_server.py`):** a `REQUEST_CHANGES` response with only the
verdict line increments `review_inconclusive_count`, not `rework_attempts`, and status stays at
its pre-review value; a `REQUEST_CHANGES` response with real findings text behaves exactly as
today (rework_attempts increments, feedback saved, redispatch); the inconclusive cap
(`REVIEW_INCONCLUSIVE_MAX`) still parks after repeated empty responses, mirroring the existing
`UNKNOWN` park test.

---

### T12 — Security review pass silently runs on the local reviewer, not Claude *(confirmed, contradicts own docstring)*

**Where:** `_run_security_reviewer` (`pipeline_mcp_server.py:1018-1041`). Its docstring
(line 1021-1022) states: *"delegates to the configured Backend (always Claude —
security-engineer is in `_LOCAL_SKIP_PERSONAS`)"* — but the implementation (line 1036) calls
`backend.get_backend("review").complete(...)`, the **same** role-based lookup every ordinary
review call uses. `_LOCAL_SKIP_PERSONAS` (`pipeline_mcp_server.py:186`) is only consulted inside
`_route_dispatch_backend()` (T8's finding — itself `auto`-only, for the *dispatch* backend, not
review). There is no code path that forces `_run_security_reviewer` onto Claude regardless of
`PIPELINE_BACKEND_REVIEW`. Under the current deploy (`PIPELINE_BACKEND_REVIEW=local`, resolved
model `glm-5.2:cloud`), the extra security-engineer pass that `review_story` runs for
`risk == "high"` stories (line 3344-3360, "High-risk stories require an additional
security-engineer pass") is executed entirely by the local reviewer. This is the same gap as
T8 but in the *review* path rather than dispatch, and worse: the docstring actively asserts the
opposite of what the code does, so it reads as intentional when reviewed in isolation.

**Why this matters more than T8 for the stated goal:** the project's own `CLAUDE.md` (Secure by
Design / Code Review sections) states security-sensitive changes require human review regardless
of source or apparent quality, and the whole point of a dedicated high-risk security pass is
extra scrutiny beyond the ordinary reviewer. Silently downgrading that specific pass to the same
local model already reviewing the story defeats its purpose without any signal to the operator.

**Change (needs the same product decision as T8 — resolve together, not independently):**
Regardless of which option T8 picks for *dispatch* routing, the security-review pass specifically
should almost certainly always go to Claude — that is the one place "cloud only as reviewer" and
"security review needs human-grade scrutiny" are the same requirement, not competing ones:
1. Change `_run_security_reviewer` to call `backend.get_backend("review", name="claude")`
   explicitly, ignoring `PIPELINE_BACKEND_REVIEW` for this one call — mirroring how
   `_escalate_review_to_claude` already hard-sets `story["backend"] = "claude"` elsewhere rather
   than relying on the ambient env setting.
2. Fix the docstring to match (it already claims this behavior; make it true).
3. Apply the same rate-limit-defer handling (FM-B, lines 3351-3355) unchanged — Claude usage
   pressure should defer the security pass, not silently fall back to local.
4. Update `README.md`'s backend-routing section in the same change if it makes the same
   `auto`-only-guarantee claim for review that T8 found for dispatch — check its current wording
   before assuming it needs the same fix.

**Tests (new, in `test_pipeline_mcp_server.py`):** a high-risk story's security-review pass calls
`get_backend("review", name="claude")` (or equivalent explicit-Claude invocation) even when
`PIPELINE_BACKEND_REVIEW=local` is set; the ordinary (non-security) review pass for the same
story is unaffected and still honors `PIPELINE_BACKEND_REVIEW`; a rate-limited security-review
response still defers rather than silently downgrading to local.

---

### T13 — No host-memory gate on local dispatch; `resource_status()` only checks reachability *(confirmed, operational)*

**Where:** `OllamaDriver.resource_status()` (`backend.py:895-910`). Delegates entirely to
`self.provider.reachable(self.endpoint)` — a TCP/HTTP reachability check. Nothing in
`_role_resource_ok` (`pipeline_mcp_server.py:1947-1966`) or the dispatch path checks host memory
pressure before launching an agent subprocess.

**Observed impact:** the qwen3-coder:30b trials this session showed the failure mode directly —
with the model resident (~18-21GB on a 24GB M4), free memory dropped to ~68-85MB and macOS
silently killed backgrounded shell launches with zero output/error, diagnosed via `vm_stat`/
`ollama ps`/`log show`. A server that is *reachable* can still be running on a machine with no
headroom to actually complete a dispatch — reachability and capacity are different questions, and
today only the first is checked. Currently low-impact for the deployed `gpt-oss:20b` (13GB
resident, real headroom at `PIPELINE_MAX_CONCURRENT_AGENTS=2`), but this is exactly the guard
that makes it safe to try new local candidates (deepseek-coder-v2:16b, or any future model with
tighter margins) without a repeat of the qwen3-coder session's silent-kill investigation, and
protects unattended/scheduler-driven runs where nobody is watching `vm_stat` in real time.

**Change:** Add a free-memory floor check to `OllamaDriver.resource_status()`, macOS-first (this
repo's target hardware per `backend.py:413`'s existing 24GB-unified-memory comment) with a
conservative cross-platform fallback:
1. Read available memory via `vm_stat` (parse `Pages free` × page size, matching the diagnostic
   method already used this session) or `psutil` if already a dependency — check
   `requirements.txt`/`pyproject.toml` before adding a new one.
2. Gate behind a new env var, e.g. `PIPELINE_LOCAL_MIN_FREE_MEMORY_MB` (default a conservative
   value such as `2048`, or `0`/unset to fully disable — must default to **on** with a safe
   floor per this repo's "secure/safe defaults" convention used elsewhere, e.g. T4's
   `PIPELINE_MERGE_BUILD_GATE`).
3. When free memory is below the floor, `resource_status()` returns `{"ok": False, "reason":
   "insufficient free memory (Xmb < Ymb floor)"}` — `_role_resource_ok` already treats any
   `ok: False` as "defer dispatch," so no caller-side change should be needed beyond this method.
4. Platform fallback: if `vm_stat` (or the chosen memory API) is unavailable/fails to parse,
   fail open (`ok: True`) with a logged reason — mirror the existing "a machine without `gh` must
   not be blocked" precedent (T3) rather than blocking dispatch on a platform where the check
   can't run.

**Tests (new, in `test_backend.py`):** `resource_status()` returns `ok: False` when parsed free
memory is below the configured floor; returns `ok: True` when above it; the floor is configurable
via env and defaults to a non-zero safe value; a `vm_stat` parse failure fails open (`ok: True`)
rather than blocking dispatch; existing reachability-failure behavior (server down) is unchanged
and still takes priority (check reachability first, memory second, or combine reasons if both
fail — pick one order and test it explicitly).

---

---

### T14 — `_ci_status`'s `gh pr checks` call has no `cwd`, so it silently queries the wrong repo  *(confirmed, high-confidence root cause)*

**Where:** `pipeline_mcp_server.py:1497-1499`, inside `_ci_status()`:
```python
r = subprocess.run(["gh", "pr", "checks", branch, "--json", "bucket"],
                   capture_output=True, text=True)
```
No `cwd=` argument. `gh pr checks <branch>` (no `--repo` flag) resolves which
GitHub repo to query from the current process's working directory's git
remote — and `_scoped_repo_root()` (line 355), the mechanism every caller
relies on to target the right repo for a given plan, only reassigns the
module-level `REPO_ROOT` **Python variable**; it never calls `os.chdir()`
(confirmed by reading its full body — a `try/finally` around a plain
variable reassignment). The MCP server process's actual OS-level cwd is
whatever it was at launch (no `cwd` is set in `~/.claude.json`'s
`mcpServers.pipeline` entry, so it's inherited from whatever process/session
happened to start it — not necessarily, and with a multi-plan/multi-repo
pipeline server, not reliably, the plan's own repo). Every sibling call in
the same code path gets this right: `_rebase_onto_master`'s `git fetch`/`git
rebase` (`cwd=REPO_ROOT`/`worktree`) and `approve_merge`'s `git push`
(`cwd=REPO_ROOT`, line ~4016) all explicitly thread the repo path through.
`_ci_status`'s own `gh` call is the one call in this sequence that doesn't.

**Observed impact (this session):** `approve_merge` on three separate,
independently-verified-green PRs (confirmed via `gh pr checks <PR>` run
directly against the correct repo — same head SHA, every job `pass`) all
returned `"CI still pending: CI did not complete within timeout"` on every
attempt, including retries several minutes apart — a pattern consistent
with querying a repo where the branch/PR simply doesn't exist (so `gh pr
checks` returns empty output every time, which `_ci_status` treats as
"checks configured but not yet registered" and polls the full
`PIPELINE_MERGE_CI_TIMEOUT` — default 300s — before giving up as
`"pending"`), not with actually-slow CI. Ended up merging all three
manually via `gh pr merge` with explicit user sign-off, bypassing the gate
entirely — exactly the failure mode T3's `_ci_status` hardening was
originally meant to prevent (a merge landing without the tool's own CI
confirmation), just via a different root cause than PR #48's.

**Change:** Add `cwd=REPO_ROOT` to the `gh pr checks` call in `_ci_status`
(line ~1498), matching every sibling call in this file. Since `_ci_status`
doesn't currently take a repo-root parameter (it reads the module-level
`REPO_ROOT` global implicitly via being called only from within a
`_scoped_repo_root()` block), either capture `REPO_ROOT` at call time inside
the function body, or — more robust against a future caller that forgets
the `_scoped_repo_root` wrapper — add an explicit `repo_root: str | None =
None` parameter, defaulting to the global, and have callers pass it
explicitly the way `worktree`/`branch` already are.

**Tests (new, in `test_pipeline_mcp_server.py`):** mock `subprocess.run` and
assert the `gh pr checks` call receives `cwd=<the scoped REPO_ROOT>` (not
the ambient process cwd) — mirror however `_rebase_onto_master`'s `cwd`
threading is already tested, if it is; if untested, add a test that sets
`REPO_ROOT` to a value distinct from the test process's actual cwd and
asserts the mocked call's `cwd` kwarg matches it. Also worth a regression
test at the `approve_merge` level: with `gh` mocked to only succeed when
invoked with the expected `cwd`, `approve_merge` succeeds — this is the
scenario that silently failed three times in a row this session.

---

---

### T15 — Memory-floor gate can self-inflict its own trip every dispatch round *(confirmed, observed repeatedly this session)*

> **Status: Fix #1 (measurement) implemented and merged 2026-07-13 (PR #101, `3d2bc69`).**
> `_free_memory_mb()` now sums free + inactive + purgeable pages instead of free-only
> (live sanity check on this machine: ~410MB strict-free vs. ~3028MB combined - confirms
> the diagnosis below exactly). 3 new tests added in `test_backend.py` mocking
> `subprocess.run` directly against a synthetic `vm_stat` output (not just mocking
> `_free_memory_mb()` itself, which the existing `resource_status()` tests already did).
> Fix #2 (accounting for the dispatched model's own resident footprint - proactive
> idle-unload or per-model floor sizing) is **not** implemented; left as the harder,
> lower-urgency follow-up described below. Default floor left at 2048MB - the measurement
> fix alone should make it trip only under genuine pressure now.

**Where:** `backend.py:895-923` (`OllamaDriver.resource_status()`) and `:930-951`
(`_free_memory_mb()`). The floor (`PIPELINE_LOCAL_MIN_FREE_MEMORY_MB`, default
2048MB) compares against **strict `vm_stat` "Pages free"** only — it does not
count macOS's inactive/purgeable pages, which are readily reclaimable and
counted as "available" by Activity Monitor and most memory-pressure tooling.
This makes the gate materially more conservative than the OS's own notion of
memory pressure.

**Observed impact (this session, repeatedly):** a single loaded local model
(`gpt-oss:20b` via Ollama's `llama-server` subprocess) was observed holding
**~12.8GB RSS** by itself — nowhere near the 2048MB floor's assumption of what
"enough headroom for a dispatch" looks like. Because Ollama keeps a model warm
in memory for a while after last use (its own idle-unload timer, not
controlled by this pipeline), the sequence became self-reinforcing: a tick
dispatches, `llama-server` loads/keeps the model resident, free memory drops
under 2048MB, the *next* tick's `resource_status()` check gates on the very
memory the prior tick's own dispatch is holding, stories get interrupted, and
the cycle repeats until something outside the pipeline (the user closing
other apps, or Ollama's own idle-unload finally firing) frees enough. This
recurred across 10+ consecutive ticks (~3 hours of wall-clock) in this
session before free memory happened to clear on its own.

**Change (two independent, complementary fixes):**
1. **Fix the measurement.** Use a metric closer to true "available" memory
   (e.g. `vm_stat`'s free + inactive + purgeable pages, or shell out to
   `memory_pressure` / `sysctl vm.page_free_count` if a more authoritative
   API is available) instead of strict free-only. Free-only understates
   headroom on a system that's simply caching aggressively but would
   readily yield that memory under real pressure.
2. **Account for the local model's own footprint.** The floor currently
   only asks "is there 2048MB free," never "is the thing I'm about to load
   already taking up 13GB that would be released if idle-unloaded." Either
   (a) proactively unload an idle Ollama model before the floor check when
   the check is about to fail — a `POST /api/generate` with
   `keep_alive: 0` against the currently-loaded model, or an equivalent
   `ollama stop` — so the pipeline manages the tradeoff itself instead of
   waiting on Ollama's own idle timer; or (b) size the floor relative to
   the specific model about to be dispatched (a small model needs much
   less headroom than a large one) rather than one flat constant for every
   model tag.
3. This session's workaround was setting `PIPELINE_LOCAL_MIN_FREE_MEMORY_MB=0`
   in the MCP server's env (`~/.claude.json`'s `mcpServers.pipeline.env`) to
   disable the gate entirely for the remainder of a stalled plan — acceptable
   as a manual one-off, but confirms the gate has no lighter-weight "trust me,
   proceed anyway for this plan" escape hatch short of disabling it globally
   for every plan this server manages. A per-plan or per-call override (mirroring
   how `local_model_fallback` is plan-scoped) would avoid a global config edit
   for what was actually a single-plan, temporary decision.

**Tests (new, in `test_backend.py`):** `_free_memory_mb`'s replacement metric
(if changed) returns a value consistent with `vm_stat`'s free+inactive+purgeable
sum on a mocked `vm_stat` output; `resource_status()` with a mocked "model
already loaded and holding N GB" scenario either triggers the proactive
unload path (if implemented) or is documented as a known gap if not.

---

## Notes carried from the retro (context, not tasks)
- `approve_merge` already does the full rebase→CI→reverify→merge gate — the §4 gap was
  *bypassing* it with a manual merge, plus the `none`-grace hole (T3).
- `_plan_lock` is reentrant per-thread (line 2526) and flock-based; existing simple mutators
  (`mark_story_in_progress`, `mark_story_done`) do **not** take it — out of scope here, but
  worth a follow-up.
- `local_model_fallback` is a manifest field set only on the messaging plan; T1's merge must
  preserve it.
