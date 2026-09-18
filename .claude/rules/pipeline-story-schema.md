# Pipeline story schema (`save_plan` / `ingest_plan`)

> Authoritative reference for the story shape `ingest_plan` actually reads.
> `save_plan`'s tool description only shows the epic-level shape and omits
> story-level fields; this is the verified shape (checked against
> `pipeline_mcp_server.py`). Consult this when authoring or reviewing a plan.

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
          "key": "optional explicit story key; omit to auto-mint a UUID"
        }
      ]
    }
  ],
  "role_config": {"review": {"provider": "mlx", "model": "qwen"}}
}
```

## Field semantics

- Only `summary` is required. `repo_root` is plan-level and required — `ingest_plan` validates it's an existing directory and `advance_all_plans` scopes each plan's work to it.
- Fields that reach the agent and the review gate: `agent_instructions` (the brief, seeded into the dispatch prompt), `acceptance` (optional array of `{path, source}` read-only test fixtures — when present, the harness materializes them into the worktree and the oracle grades the run on whether the impl makes them pass; when absent, the story runs on the base harness with a "tests pass" bar), `persona`/`model`/`risk` (routing and merge gating), and `dependencies`.
- `description` is read only for the Plane issue body when Plane is enabled; the manifest and dispatch prompt do not use it — treat it as optional human context for plan review.
- `backend` (optional) pins this one story's dispatch provider, independent of the process-wide `PIPELINE_BACKEND_DISPATCH`. `ingest_plan` rejects an unrecognized value at ingest time. Omit to use the process-wide default.
- `role_config` (optional, plan-level — a sibling of `epics`, not a story field): per-role provider/model overrides for `overlord`/`planner`/`dispatch`/`review`/`decompose`, e.g. `{"review": {"provider": "mlx", "model": "qwen"}}`. See the README's "Per-role provider/model configuration" section and `model_registry.json` for the full priority chain and available providers/models.
- `dispatch_lease_expires_at` / `dispatch_lease_owner_pid` (runtime, written by `pipeline/dispatch_lease.py` — do NOT set these by hand in a plan file): the dispatch lease that stops a released plan lock from double-dispatching a story (LOCKSTARVE-B2). `dispatch_lease_expires_at` is an ISO-8601 UTC timestamp string (`now + PIPELINE_DISPATCH_LEASE_TTL_SECONDS`, default 1800); `dispatch_lease_owner_pid` is the claimer's `os.getpid()` (diagnostic only, never trusted for ownership). A missing/malformed/expired lease is re-claimable (fail secure); a live lease is never stolen.

## Do not invent fields

There is no `id`, `title`, `acceptance_criteria`, or `depends_on` field. `ingest_plan` fails with a bare `'summary'` KeyError if `summary` is missing. `dependencies` must reference other stories by their exact `summary` string (or explicit `key`), not an invented ID. `acceptance` is an array of `{path, source}` file fixtures, not a list of strings; testable criteria go in `agent_instructions`. If in doubt, read an existing file under `~/.claude/plans/*.json` as ground truth.

## Writing `agent_instructions` — the single most influential field

Sparse stories (a summary alone) leave the agent to guess. Populate, at minimum: what to build, the approach, the TDD expectation (write the failing test first), testable success criteria (concrete, checkable statements such as *"`cargo test -p storage` passes"* or *"rejects a zero-length key with `StoreError::Corrupted`"*), and the negative/boundary cases the tests must cover. The testable criteria live here, not in a separate field.

### Opting a story out of the test-author phase: `[no-new-tests]`

For local-family dispatch the test-author phase is always-on: it writes a NEW failing test suite before the weak executor touches code. A **behavior-preserving refactor** (e.g. W1a's "move a tool body onto `PipelineService`; the existing `test_pipeline_mcp_server.py` already covers it") has no new test to write — forcing the phase to invent one produced a redundant, unsatisfiable structural-assertion oracle that parked mid-write (Mode 51, live 2026-08-10 on W1a-10). To skip the phase for such a story, place the literal token `[no-new-tests]` anywhere in `agent_instructions`. The phase then falls open to monolithic dispatch and the executor runs against the existing suite — the correct grade for a behavior-preserving move. The token is the exact bracketed string `[no-new-tests]`; free-text phrases like "no new tests" do NOT opt out (they are common in ordinary briefs and must not silently skip the phase). Use this only when the existing suite genuinely covers the change; if there is any new behavior to grade, let the phase run. The token opts a story out wherever it appears in `agent_instructions`, EXCEPT when the sentence containing it negates its own use (e.g. "[no-new-tests] must NOT be used for this story") -- such a sentence is read as a *mention* of the token, not an invocation, so a plan author explaining the opt-out cannot accidentally trigger it; say the same thing in prose instead, e.g. "the no-new-tests opt-out must not be used for this story" (without the brackets).

## Grading the integration, not just the unit

An acceptance fixture that calls the changed function directly — never
touching the call site, registration path, or wiring the story actually
asks for — creates a graded path that bypasses the integration work. The
oracle goes green the moment the unit works in isolation, so a weak
executor (and even a capable one under time pressure) will skip the
ungraded wiring step no matter how clearly `agent_instructions` states it,
and the story ships dead code. This is not hypothetical: it happened live
on `harness-targeted-done-nudge` (2026-07-28) — the fixture asserted
`_no_tool_nudge(0)` returns the right string, the brief said "wire this at
the call site," and the executor never touched the call site because
nothing graded it. The full-suite done-bar didn't catch it either, since it
doesn't exercise the call site any more than the fixture did.

Before writing an `acceptance` fixture, check: if `agent_instructions`
requires a call-site change, a decorator/registration move, or wiring one
piece into another, does the fixture's assertion actually fail when that
wiring is missing — or does it still pass if the wired piece is just
sitting there unconnected? If the fixture would still pass with the change
half-done, rewrite it to exercise the real path: drive the actual
entrypoint (`main()`, the CLI, the route handler) with the unit mocked only
at its true external boundary, or assert against the production source/
registry to confirm the wiring landed, rather than calling the unit
directly. `pipeline.build_detect._isolation_only_acceptance_warning` runs a
non-blocking heuristic on this at `ingest_plan` and posts a notification
when it looks isolation-only — treat that notification as a prompt to
re-check the fixture, not noise to ignore.

## Cumulative artifacts: registries, dispatch tables, and prompt strings a chain of stories grows together

When several stories in a plan each add to the SAME shared artifact in
sequence — a registry dict, a `__all__` list, a system-prompt string,
a dispatch table, or a shared markup/style/script file multiple UI stories
edit in turn — never let one story's tests assert the artifact's exact
total contents (equality on a set/list, an exact substring pinned to that
story's own additions, an exact total count, or a byte-for-byte SHA-256 of
a file/region pinned as "must stay unchanged"). Assert only what THAT story
added: membership (`"x" in registry`), a sorted/set comparison, or a
structural relationship to a stable anchor (e.g. "this new entry appears
before the fixed closing sentence") — never the complete enumeration.

**Why this matters and is not hypothetical:** three documented, independent
occurrences, same root cause:

- `pipeline/triage.py` (`overlord-failure-triage`, 2026-08-18/20): one shared
  test file asserted `triage.__all__ == [16 items in a fixed order]`. Every
  one of 8 later stories had to edit that assertion just to add its own
  symbol at the right index — local-model success on that epic was 33% vs.
  75% on a sibling epic that used one test file per story with membership
  assertions. See [[feedback_per_story_test_files_not_shared]].
- `app/chat.py`'s `SYSTEM_PROMPT` (`W2_CHAT_ENTRY_POINT_PLAN`, 2026-08-20):
  the story that first populated the `TOOLS` registry wrote a test pinning
  the "Available tools: ..." sentence to the exact 3 tools it added, and
  asserting that exact string appeared immediately before the prompt's
  final sentence. Six later dependent stories each legitimately registered
  more tools but could not touch that frozen sentence without violating
  the "never modify an existing test" rule. Two of those stories (W2-03,
  W2-05) burned their full local rework budget — 96+ minutes combined,
  zero commits produced in several of the cycles — failing a test that had
  nothing to do with their own assigned work. Worse: because nothing could
  ever update the enumeration, the defect shipped all the way to `master`
  — 14 of the 23 tools ultimately registered were never named anywhere in
  the prompt the chat model actually receives, and no test caught it,
  because the test measured a frozen snapshot instead of the live registry.
- `comms-ui-design-alignment` (2026-08-27), a chain of UI stories each
  editing `static/index.html`/`static/style.css`/`static/app/main.js` in
  turn: each story's test-author phase pinned a SHA-256 hash of the OTHER
  files it was told not to touch, as a self-guard against its own scope
  creep (e.g. "`INDEX_HTML_SHA256` = ... as of this dispatch — this story
  is CSS-only"). Three later sibling stories then legitimately edited those
  exact files (a subtitle-wiring story touched `main.js`, a landing-hero
  redesign touched `index.html`, a toast-color fix touched the same
  `style.css` region), and nothing ever re-pinned the earlier guards.
  The final story in the chain ("wire suggestion-chip clicks") inherited
  three permanently-red assertions about files it never touched and had
  no scope to fix, and parked after 3 rework attempts with zero commits —
  notable because this dispatch was Claude/sonnet, not a weak local model;
  the "never modify an existing test without approval" default (CLAUDE.md
  Step 4) was itself what turned a two-line fix into an unwinnable loop,
  since a headless dispatch has no user to ask before touching a test file
  it didn't author. Unblocked by re-pinning the three hashes to the
  current, correctly-evolved file contents and re-reviewing — the
  implementation had been correct since the first attempt.

**How to apply at plan-authoring time:**
- When 3+ stories are chained on a shared production artifact (see the
  "Local (non-Claude) dispatch" note below on chaining these sequentially
  in the first place), scan every story's `agent_instructions` for "add an
  entry to X" / "add a sentence to Y" and check: does the test this story's
  brief implies would assert the OLD exact state, or the growing state?
- State explicitly in `agent_instructions`, for every story in such a
  chain: *"`<artifact>` is cumulative — later sibling stories add more to
  it. Your tests must assert only what YOU add (membership / ordering
  relative to a fixed anchor), never the total contents, exact count, or
  exact full-string match."*
- An **insertion anchor** ("insert this immediately before the exact
  sentence '...'") is correct and necessary guidance for the *implementer*
  — it is not license for the *test* to assert exact position of the total
  string. Anchor the implementation; assert only the delta.
- If an artifact is regenerable from a registry it enumerates (like a
  "these are the available tools" prompt sentence), prefer building it
  programmatically from the registry itself over hand-writing prose that
  can drift — then the test asserts the generator's correctness once,
  and no later story can ever leave it stale.
- A story's own "must not touch these other files" self-guard is a
  legitimate scope-discipline test, but never let it pin a SHA-256/byte
  hash of a file (or region) that a *later sibling* is scheduled to
  legitimately edit. If the plan already has a follow-up story touching
  that same file, phrase the guard as a scope statement in
  `agent_instructions` instead ("do not touch `static/index.html` in this
  story") and let the test assert something narrower and durable (e.g. a
  specific string/selector this story must not have introduced), not a
  hash of the whole file that the next sibling will legitimately break.

## A blanket "never modify existing tests" instruction breaks when the story legitimately changes a shape an old test pins exactly

Every dispatched agent is told not to modify existing tests (CLAUDE.md's
"Protect existing tests" step) — correct as a default, but a story whose
whole point is to change a request/response shape, a return value, or any
other contract an *existing* test already asserts via strict/exact
equality will collide with that default. Neither the test-author phase nor
the implementer is authorized to fix the old test, so the story either
ships with a legitimately-failing pre-existing test, or (per CLAUDE.md
Step 4) should stop and escalate via `request_decision` — which in
practice neither phase does, because nothing in the story's
`agent_instructions` told them this specific test existed or that touching
it was in scope.

**Not hypothetical:** `chat-security-hardening` story "Stamp
`decided_by=chat` on decisions recorded through the chat tool" (2026-08-20,
PR #405). `agent_instructions` said only `"Do NOT modify any existing
test"` with no carve-out. The one-line implementation (stamping
`decided_by: "chat"` onto the `answer_decision` POST body) was correct on
the first attempt — the reviewer said so explicitly — but broke a
pre-existing test (`test_chat_decision_tools.py::test_posts_to_decisions_endpoint_with_body`,
from an earlier story) that asserted the POST body via *strict dict
equality* with no `decided_by` key. Two full local rework cycles (backend
`ollama`/`deepseek-v4-flash:cloud`) then failed to converge — the agent
edited the file, couldn't get the full-suite rework done-bar green within
`REWORK_SUITE_REJECT_CAP` (3) attempts, and the story parked with zero
commits (`"no new commit after 2 rework redispatches"`). Unblocked only by
a human patching `agent_instructions` with the exact before/after text of
the one authorized edit and redispatching.

**This applies to EVERY story, not only shape changes.** The same collision
happens when a story deletes a config key a survivor-list test requires
(`gptoss-num-ctx-ceiling`, 2026-09-18: `test_launchd_templates_no_routing_env.py`
pinned `PIPELINE_LOCAL_NUM_CTX` as a must-survive key; the executor then
restored the key to go green, undoing its own deliverable), or inserts doc
text inside a section a verbatim-body test pins (`test_readme_reference_split.py`).
Across 500 PRs since 2026-08-01, 48% of gate-synthesized test failures were
in pre-existing test files the story never touched.

**How to apply at plan-authoring time:** do NOT rely on grepping — a grep for
the touched key returned 22 files and the one that mattered was missed. Run
the impact check in `.claude/rules/local-dispatch-preflight.md` §1: apply a
rough stand-in of the change in a scratch worktree, run the full suite, and
treat every failure outside the story's own new test files as a conflict. A
strict `==` on a dict/list/string, a survivor list, a verbatim/byte-for-byte
body check, a hash pin, and an exact count are the highest-risk shapes. For
each conflict, either:
- pre-authorize the exact reconciling edit by naming the file, the test,
  and the literal before/after text (mirroring `d914df45`'s "AUTHORIZED
  EDITS: make exactly these N edits, nothing else" pattern elsewhere in
  this plan), or
- name the conflicting test explicitly and instruct the agent to raise it
  via `request_decision` rather than guess.
A bare "do not modify existing tests" with no carve-out is only safe when
the story genuinely adds new, non-conflicting behavior — verify that
before assuming it.

## Lint-check hand-authored acceptance fixtures before ingesting

`acceptance` fixture source is plan-authored content that bypasses every
pipeline role. `test_author` (the always-on-for-local-family pre-executor
phase, see README's TDD-split section) only authors the agent's own
"Tests to write" unit tests named in `agent_instructions` — it never
touches or reviews `acceptance` fixtures. No review persona or
`ingest_plan`-time check verifies fixture lint hygiene either
(`_isolation_only_acceptance_warning` only checks for isolation-only
fixtures, a different failure class — see the section above). A fixture
with a lint violation becomes a read-only oracle the dispatched agent is
forbidden from touching, so CI's repo-wide lint gate fails every rework
attempt with no way for the agent to ever fix it — a variant of the
born-broken-oracle class, caused by the plan author rather than a prior
merged gate.

This happened live 2026-08-05 on `mode31-off-task-drift-guard` story 2
(PR #235): the plan author dry-ran both acceptance fixtures with pytest
and a full-suite regression check before ingesting, which caught
functional bugs but missed lint hygiene. 4 of 7 CI lint errors traced to
the plan author's own fixture: four `fake, calls = _sequence_chat(...)`
lines where `calls` was never used (ruff `RUF059`). The dispatched local
agent burned a full rework cycle unable to resolve it, since 4 of the 7
errors were in a file it could not edit.

Before `save_plan`/`ingest_plan` (or `patch_story` to edit an existing
`acceptance` field), materialize any hand-authored fixture source into a
scratch copy of the repo (or a throwaway worktree) and run BOTH the
project's test command AND its lint command against it — pytest alone is
not enough. Fix any violation (e.g. prefix an unused unpacked variable
with `_`) before the fixture is ever embedded as the read-only oracle. If
a fixture needs correcting after a story has already been dispatched, use
`patch_story` to update the plan's authoritative `acceptance` source (this
also fixes what `acceptance_digests` recomputes to on the next dispatch —
see `pipeline/server.py`'s dispatch path) and hand-fix the same content in
the story's live worktree so the two stay identical before resuming.

### Local (non-Claude) dispatch — hard-won rules

These rules come from live dispatch failures on weak/local executors; follow them when the dispatch backend is not `claude`:

- **Rename-and-delegate over in-place re-indent.** If the change is a targeted edit *inside* a large existing function (~50+ lines), prescribe the rename-and-delegate shape (`foo` → a guard/setup wrapper that calls a renamed `_foo_impl`) rather than re-indenting the whole body through a truncated file-viewing tool. In-place re-indenting is the single most reliable way to break a weak local executor. Name an existing example of the pattern in the codebase when one exists.
- **Move decorators, docstrings, and entry validation to the wrapper.** If the function carries a decorator that registers at import/decoration time (`@mcp.tool()`, a route decorator, an event-handler registry), the decorator — plus the docstring and any argument validation that ran as the function's first statements — must move to the new wrapper `foo`, not stay on `_foo_impl`. Prescribe a success criterion that exercises the *registration path itself* (e.g. `mcp._tool_manager._tools["foo"].fn is foo`), not just a call to the bare module attribute `foo(...)`. A test that only calls the attribute passes even when the decorator silently deregistered the real entrypoint — Python's late name-binding makes the attribute and the registered object diverge invisibly.
- **Prefer anchored `str_replace` over line-number `replace_lines` on files >~1,000 lines.** A resumed run's transcript is trimmed to fit the context budget, so line numbers computed from an earlier `view_file` are frequently stale by the time a later step acts on them; a stale-line-number `replace_lines` can silently delete or corrupt an unrelated span next to the intended edit.
- **Cap a single local-dispatch story at two production files** (test files don't count). A story touching three or more reliably costs a 20B-class model multiple step-cap resumes and rework cycles before it converges, each resume re-deriving codebase context it already had. Split by file/concern instead (e.g. "add the detection function" as one story, "wire it into the gate" as a dependent follow-up) even when the combined work is small enough for a single PR by hand.