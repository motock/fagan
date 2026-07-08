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

## Notes carried from the retro (context, not tasks)
- `approve_merge` already does the full rebase→CI→reverify→merge gate — the §4 gap was
  *bypassing* it with a manual merge, plus the `none`-grace hole (T3).
- `_plan_lock` is reentrant per-thread (line 2526) and flock-based; existing simple mutators
  (`mark_story_in_progress`, `mark_story_done`) do **not** take it — out of scope here, but
  worth a follow-up.
- `local_model_fallback` is a manifest field set only on the messaging plan; T1's merge must
  preserve it.
