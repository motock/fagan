# Pipeline story schema (`save_plan` / `ingest_plan`)

> Authoritative reference for the story shape `ingest_plan` actually reads. `save_plan`'s tool description shows only the epic-level shape and omits story-level fields; this is the verified shape (checked against `pipeline_mcp_server.py`).

## Plan / story shape

```json
{
  "repo_root": "/absolute/path/to/this/plan's/git/repo",
  "epics": [
    {
      "summary": "Epic name",
      "stories": [
        {
          "summary": "Story title — the unique key other stories reference as a dependency",
          "description": "Human-readable scope and rationale (Plane issue body when Plane is enabled; otherwise not stored in the manifest)",
          "agent_instructions": "The full implementation brief the dispatched agent receives: scope, approach, the TDD expectation, and negative/boundary cases to cover. Populate richly.",
          "acceptance": [{"path": "tests/acceptance_foo.rs", "source": "// optional read-only test fixture; the oracle grades the run on whether the impl makes these pass"}],
          "dependencies": ["Exact summary text (or explicit key) of a prerequisite story in this same plan"],
          "persona": "software-engineer",
          "model": "sonnet",
          "risk": "low",
          "backend": "optional: claude | local | ollama | lmstudio | mlx | litellm | auto",
          "files": ["pipeline/foo.py", "REFERENCE.md"],
          "blocked_by": ["other-plan:STORY-KEY"],
          "key": "optional explicit story key; omit to auto-mint a UUID"
        }
      ]
    }
  ],
  "role_config": {"review": {"provider": "mlx", "model": "qwen"}}
}
```

## Field semantics

- Only `summary` is required. `repo_root` is plan-level and required: `ingest_plan` rejects a non-existent directory and `advance_all_plans` scopes each plan to it.
- Fields reaching the agent and review gate: `agent_instructions` (the dispatch prompt), `acceptance` (`{path, source}` fixtures materialized into the worktree for the oracle; absent means the base harness with a "tests pass" bar), `persona`/`model`/`risk` (routing and merge gating), `dependencies`.
- `description`: Plane issue body only; the manifest and dispatch prompt ignore it.
- `backend` (optional): pins this story's provider regardless of `PIPELINE_BACKEND_DISPATCH`; `ingest_plan` rejects an unrecognized value.
- `tdd_split` (runtime, dispatcher-written — do NOT set by hand): `true` once the test-author phase succeeds; the oracle tamper check (`pipeline/ci.py:428-442`) reads it to keep a split story's acceptance fixture read-only, and re-ingest resets it. It does not control whether that phase runs — `[no-new-tests]` does.
- `files` (optional): repo-relative POSIX paths of the PRODUCTION files (docs included) the story may change; test files are never listed and always allowed. Absent means undeclared; empty means no production file may change. `patch_story` cannot edit it — change the plan and re-ingest (omitting clears it). Entries must be non-empty `str` with no `/` prefix, no `\`, no `..`; duplicates rejected.
- `blocked_by` (optional): cross-plan prerequisites, `"<plan>:<story_key>"` strings (the key may contain colons). Absent or `null` is valid; a malformed entry is rejected at `ingest_plan` naming the summary and the entry. The referenced plan/story need not exist yet. Re-ingest refreshes it.
- `key` (optional): an explicit story key that must be unique across plans. `ingest_plan` rejects a key matching (case-insensitively) another plan's story that is not done/merged or whose worktree still exists, since the branch (`agent/<key>`) and worktree path are keyed by the story key alone. Omitting `key` mints a UUID, which never collides.
- `role_config` (optional, plan-level — a sibling of `epics`): per-role provider/model overrides for `overlord`/`planner`/`dispatch`/`review`/`decompose`, e.g. `{"review": {"provider": "mlx", "model": "qwen"}}`. See the README's "Per-role provider/model configuration" and `model_registry.json`.
- `preflight_override` (optional, plan-level): a non-empty reason string admitting non-Claude stories whose brief lacks a real `Preflight:` line; every use is notified as event `preflight_override`. `ingest_plan` rejects non-Claude stories with no such line and no override.
- `dispatch_lease_expires_at` / `dispatch_lease_owner_pid` (runtime, written by `pipeline/dispatch_lease.py` — do NOT set by hand): the lease stopping a released plan lock from double-dispatching a story (LOCKSTARVE-B2). The timestamp is ISO-8601 UTC (`now + PIPELINE_DISPATCH_LEASE_TTL_SECONDS`, default 1800); the pid is the claimer's `os.getpid()`, diagnostic only. Missing, malformed, or expired is re-claimable; a live lease is never stolen.

## Do not invent fields

There is no `id`, `title`, `acceptance_criteria`, or `depends_on` field. `ingest_plan` fails with a bare `'summary'` KeyError if `summary` is missing. `dependencies` reference stories by their exact `summary` (or explicit `key`), not an invented ID. `acceptance` is an array of `{path, source}` fixtures, not criteria strings; testable criteria go in `agent_instructions`. In doubt, read an existing file under `~/.claude/plans/*.json`.

## Writing `agent_instructions` — the single most influential field

Sparse stories leave the agent to guess. Populate: what to build, the approach, the TDD expectation (write the failing test first), testable success criteria (checkable, e.g. *"`cargo test -p storage` passes"*, *"rejects a zero-length key with `StoreError::Corrupted`"*), and the negative/boundary cases the tests must cover. Criteria live here, not in a separate field.

### Opting a story out of the test-author phase: `[no-new-tests]`

The test-author phase is always-on for local-family dispatch: it writes a NEW failing suite before the weak executor touches code. A behavior-preserving refactor has no new test to write, and forcing the phase to invent one produced an unsatisfiable structural-assertion oracle that parked mid-write (Mode 51). Place the literal token `[no-new-tests]` anywhere in `agent_instructions` to skip the phase; the story then falls open to monolithic dispatch against the existing suite. It is the exact bracketed string — "no new tests" in prose does NOT opt out — and only use it when the existing suite genuinely covers the change. It applies wherever it appears EXCEPT when its own sentence negotiates that use (e.g. "[no-new-tests] must NOT be used here"): that is a *mention*, not an invocation, so an author explaining the opt-out cannot trigger it.

## Declare `files` from where the code actually lives

`files` is a hard gate: changing a production path outside it is sent back before any LLM review, and a dispatched agent cannot ask for the path (`request_decision` is permission-denied headless), so one that correctly stops with "the brief needs this file" counts as crashing and the story parks. Four parked this way in two days (2026-10-23/24, plan `external-context-sources-phase1/2`), always because the author wrote `files` from the brief instead of the code:

- **CL-14**: declared `pipeline/dispatch.py`, 999 lines against a 1000-line gate, so any addition forced a split into undeclared modules.
- **CL-20**: files mode has to be wired where dispatch-phase sources are resolved (`pipeline/dispatch_context.py`), undeclared.
- **CL-23**: the brief named `server.py` and `dashboard_routes.py`, but `decompose_plan` had moved onto `PipelineService` in `pipeline/service.py`.
- **CL-24**: the org-level merge belongs in `pipeline/ingest.py` (`_merged_context`, where CL-22 put the repo-level merge); only `context_sources.py` was declared.

Before writing or ingesting `files`, for each story:

1. **Find where the behavior lives.** Grep the function or constant the story extends and its call sites; declare the file that owns it, not the one the brief names.
2. **Check headroom.** `wc -l` every declared `.py`: within ~50 of 1000 means the story cannot add code there — declare the sibling module it moves to (or delegates to) and say so in the brief. `scope_headroom` at ingest will warn once the parked-story recovery plan lands.
3. **Declare the wiring file.** Behavior added in one module and called from another declares both; see 'Grading the integration' below.
4. **State a decided scope when it is not obvious.** If the brief alone would make a reviewer read a declared file as out of scope, add 'SCOPE IS DECIDED, NOT A FINDING: ...' naming the files and why — the reviewer reads the brief, not the `files` list.
5. **Parallel stories on one file.** Concurrent stories adding to the same module (especially one insertion point, e.g. the end of `context_sources.py`) conflict at the merge gate, which cannot resolve conflicts: chain them with `dependencies` or give each a new module.

Fixing it afterwards: `patch_story` cannot edit `files`. Update the plan and its brief, re-ingest (statuses and runtime state kept, authored fields refreshed — first confirm the manifest briefs match the plan), then rebase and push by hand. A stale verdict will not re-review until the flagged files change; that workaround is what `docs/plans/PARKED_STORY_RECOVERY_PLAN.md` removes.

## Grading the integration, not just the unit

A fixture that calls the changed function directly — never touching the call site, registration path, or wiring the story asks for — grades a path that bypasses the integration work. The oracle goes green as soon as the unit works in isolation, so the executor skips the ungraded wiring step however clearly `agent_instructions` states it, and the story ships dead code. Live on `harness-targeted-done-nudge` (2026-07-28): the fixture passed on `_no_tool_nudge(0)` alone and nothing graded the untouched call site.

Before writing an `acceptance` fixture, check: if `agent_instructions` requires a call-site change, a registration move, or wiring one piece into another, does the assertion fail when that wiring is missing — or does it pass with the piece sitting there unconnected? If it would pass half-done, exercise the real path instead: drive the actual entrypoint (`main()`, the CLI, the route handler) with the unit mocked only at its external boundary, or assert against the production source/registry that the wiring landed. `pipeline.build_detect._isolation_only_acceptance_warning` runs a non-blocking heuristic on this at `ingest_plan`; treat its notification as a prompt to re-check the fixture.

## Cumulative artifacts: registries, dispatch tables, and prompt strings a chain of stories grows together

When several stories each add in sequence to the SAME shared artifact — a registry dict, a `__all__` list, a system-prompt string, a dispatch table, or a file several UI stories edit in turn — never let one story's tests assert its exact total contents (equality on a set/list, a substring pinned to that story's additions, a total count, a byte-for-byte SHA-256). Assert only what THAT story added: membership (`"x" in registry`), a sorted/set comparison, or a structural relationship to a stable anchor (e.g. "this entry appears before the fixed closing sentence") — never the complete enumeration.

Three documented, independent occurrences, same root cause:

- `pipeline/triage.py` (`overlord-failure-triage`, 2026-08-18/20): a shared test pinned `triage.__all__ == [16 items in fixed order]`, so each of 8 later stories had to edit it; local-model success there was 33% vs. 75% on a sibling using one test file per story. See [[feedback_per_story_test_files_not_shared]].
- `app/chat.py`'s `SYSTEM_PROMPT` (`W2_CHAT_ENTRY_POINT_PLAN`, 2026-08-20): the story first filling `TOOLS` pinned the "Available tools: ..." sentence to its 3 tools, six later stories could not touch it, and 14 of the 23 registered tools ended up unnamed.
- `comms-ui-design-alignment` (2026-08-27): each UI story pinned a SHA-256 of the OTHER `static/` files it was told not to touch; three later siblings legitimately edited them, nothing re-pinned, and the final story parked on three permanently-red out-of-scope assertions.

**How to apply at plan-authoring time:**

- When 3+ stories are chained on a shared production artifact (see "Local (non-Claude) dispatch" below on chaining them sequentially), scan their `agent_instructions` for "add an entry to X" / "add a sentence to Y" and check whether the implied test asserts the OLD exact state or the growing state.
- State explicitly in `agent_instructions`, for every story in such a chain: *"`<artifact>` is cumulative — later sibling stories add more. Your tests must assert only what YOU add (membership / ordering relative to a fixed anchor), never the total contents, exact count, or exact full-string match."*
- An **insertion anchor** ("insert immediately before the exact sentence '...'") is guidance for the *implementer*, not license for the *test* to assert the exact position of the total string. Anchor the implementation; assert only the delta.
- If an artifact is regenerable from the registry it enumerates (a "these are the available tools" sentence), build it programmatically: the test asserts the generator once and no later story can leave it stale.
- A story's own "must not touch these other files" self-guard is legitimate scope discipline, but never pin a SHA-256 of a file a *later sibling* will edit; phrase it as a scope statement ("do not touch `static/index.html` here") and assert something narrower, e.g. a selector this story must not have introduced.

### CI enforces this: the pin-guard baseline

`tests/unit/test_pin_guard_baseline.py` scans every `tests/**/*.py` with `pipeline.pin_guard.scan_source` and fails CI on a NEW pin of any of three shapes: a sha256 literal, an exact enumeration of a cumulative name (`__all__`, `*_EXPORTS`, `*_TOOLS`, ...), or an exact count of one (`.mjs` tests are not scanned). Existing pins live in `tests/unit/pin_guard_baseline.json` (path -> line numbers), and the baseline only shrinks: removing or re-asserting a pin fails on the stale entry, so delete it from the JSON in the same change. For an exception, add the line to the baseline, justify it in the PR, and re-measure the shifted lines.

## A blanket "never modify existing tests" instruction breaks when the story legitimately changes a shape an old test pins exactly

Every agent is told not to modify existing tests (CLAUDE.md's "Protect existing tests") — right as a default, but a story meant to change a shape, a return value, or any contract an *existing* test pins via strict/exact equality collides with it. Neither phase may fix that test, so the story either ships with a legitimately-failing pre-existing test or (per CLAUDE.md Step 4) stops and escalates via `request_decision` — which neither does in practice, since nothing in `agent_instructions` flagged the test.

`chat-security-hardening`'s "Stamp `decided_by=chat` on decisions recorded through the chat tool" (2026-08-20, PR #405) had no carve-out: the one-line implementation was correct first time but broke `test_chat_decision_tools.py::test_posts_to_decisions_endpoint_with_body` (strict dict equality, no `decided_by`), two rework cycles blew `REWORK_SUITE_REJECT_CAP` (3), and the story parked with zero commits. This applies to EVERY story: deleting a config key a survivor list requires (the executor restored `PIPELINE_LOCAL_NUM_CTX`, undoing its own deliverable) or inserting doc text a verbatim-body test pins (`test_readme_reference_split.py`) collides the same way, and 48% of gate-synthesized failures across 500 PRs were in pre-existing files.

**How to apply at plan-authoring time:** do NOT grep for it — a grep for the touched key returned 22 files and missed the one that mattered. Run the impact check in `.claude/rules/local-dispatch-preflight.md` §1 ("Impact run"): apply a rough stand-in of the change in a scratch worktree, run the full suite, and treat every failure outside the story's new test files as a conflict. Highest-risk shapes: strict `==` on a dict/list/string, a survivor list, a verbatim body check, a hash pin, an exact count. For each conflict either:

- pre-authorize the exact reconciling edit by naming the file, the test, and the literal before/after text (mirroring `d914df45`'s "AUTHORIZED EDITS: make exactly these N edits, nothing else"), or
- name the conflicting test explicitly and instruct the agent to raise it via `request_decision` rather than guess.

A bare "do not modify existing tests" is safe only when the story genuinely adds new, non-conflicting behavior — verify that first.

## Lint-check hand-authored acceptance fixtures before ingesting

`acceptance` source bypasses every pipeline role: `test_author` only writes the "Tests to write" unit tests named in `agent_instructions`, never touching `acceptance` fixtures, and no review persona or `ingest_plan`-time check verifies their lint hygiene. A lint-violating fixture is a read-only oracle the agent is forbidden to touch, so CI's repo-wide lint gate fails every rework attempt with no way to fix it. Live 2026-08-05: 4 of 7 CI lint errors were four unused `fake, calls = _sequence_chat(...)` unpacks (ruff `RUF059`) in the author's own fixture, after a pytest dry run caught functional bugs but not lint.

Before `save_plan`/`ingest_plan` (or `patch_story` to edit `acceptance`), materialize any hand-authored fixture into a scratch copy of the repo (or throwaway worktree) and run BOTH its test command AND its lint command against it — pytest alone is not enough. Fix violations (e.g. prefix an unused unpacked variable with `_`) before the fixture is embedded. To correct one after dispatch, use `patch_story` on the plan's authoritative `acceptance` source (this also fixes `acceptance_digests`) and hand-fix the live worktree to match.

### Local (non-Claude) dispatch — hard-won rules

From live failures on weak/local executors; follow them whenever the backend is not `claude`. Story sizing (function count, file-size tiers, survivor lists) is covered by `.claude/rules/agent-dispatch-story-sizing.md`.

- **Rename-and-delegate over in-place re-indent.** A targeted edit *inside* a large function (~50+ lines) should become the rename-and-delegate shape (`foo` → a guard/setup wrapper calling a renamed `_foo_impl`), not a re-indent of the whole body through a truncated file-viewing tool; in-place re-indenting is the most reliable way to break a weak local executor. Name an existing example when one exists.
- **Move decorators, docstrings, and entry validation to the wrapper.** If the function carries a registrar decorator (`@mcp.tool()`, a route decorator, an event-handler registry), the decorator — plus docstring and any validation that ran as the function's first statements — must move to the wrapper `foo`, not stay on `_foo_impl`. Prescribe a criterion exercising the *registration path itself* (e.g. `mcp._tool_manager._tools["foo"].fn is foo`), not the bare module attribute: Python's late name-binding makes the two diverge invisibly, so an attribute-only test passes even when the decorator silently deregistered the entrypoint.
- **Prefer anchored `str_replace` over line-number `replace_lines` on files >~1,000 lines.** A resumed run's transcript is trimmed to the context budget, so line numbers from an earlier `view_file` are often stale, and a stale-line-number `replace_lines` can silently corrupt an unrelated span.
- **Cap a single local-dispatch story at two production files** (test files don't count). Three or more reliably costs a 20B-class model multiple step-cap resumes and rework cycles, each re-deriving context it already had. Split by file/concern instead — "add the detection function" as one story, "wire it into the gate" as a dependent follow-up — even when the combined work fits one PR by hand.
