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
          "backend": "optional: claude | local | ollama | lmstudio | mlx | auto",
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

## Do not invent fields

There is no `id`, `title`, `acceptance_criteria`, or `depends_on` field. `ingest_plan` fails with a bare `'summary'` KeyError if `summary` is missing. `dependencies` must reference other stories by their exact `summary` string (or explicit `key`), not an invented ID. `acceptance` is an array of `{path, source}` file fixtures, not a list of strings; testable criteria go in `agent_instructions`. If in doubt, read an existing file under `~/.claude/plans/*.json` as ground truth.

## Writing `agent_instructions` — the single most influential field

Sparse stories (a summary alone) leave the agent to guess. Populate, at minimum: what to build, the approach, the TDD expectation (write the failing test first), testable success criteria (concrete, checkable statements such as *"`cargo test -p storage` passes"* or *"rejects a zero-length key with `StoreError::Corrupted`"*), and the negative/boundary cases the tests must cover. The testable criteria live here, not in a separate field.

### Local (non-Claude) dispatch — hard-won rules

These rules come from live dispatch failures on weak/local executors; follow them when the dispatch backend is not `claude`:

- **Rename-and-delegate over in-place re-indent.** If the change is a targeted edit *inside* a large existing function (~50+ lines), prescribe the rename-and-delegate shape (`foo` → a guard/setup wrapper that calls a renamed `_foo_impl`) rather than re-indenting the whole body through a truncated file-viewing tool. In-place re-indenting is the single most reliable way to break a weak local executor. Name an existing example of the pattern in the codebase when one exists.
- **Move decorators, docstrings, and entry validation to the wrapper.** If the function carries a decorator that registers at import/decoration time (`@mcp.tool()`, a route decorator, an event-handler registry), the decorator — plus the docstring and any argument validation that ran as the function's first statements — must move to the new wrapper `foo`, not stay on `_foo_impl`. Prescribe a success criterion that exercises the *registration path itself* (e.g. `mcp._tool_manager._tools["foo"].fn is foo`), not just a call to the bare module attribute `foo(...)`. A test that only calls the attribute passes even when the decorator silently deregistered the real entrypoint — Python's late name-binding makes the attribute and the registered object diverge invisibly.
- **Prefer anchored `str_replace` over line-number `replace_lines` on files >~1,000 lines.** A resumed run's transcript is trimmed to fit the context budget, so line numbers computed from an earlier `view_file` are frequently stale by the time a later step acts on them; a stale-line-number `replace_lines` can silently delete or corrupt an unrelated span next to the intended edit.
- **Cap a single local-dispatch story at two production files** (test files don't count). A story touching three or more reliably costs a 20B-class model multiple step-cap resumes and rework cycles before it converges, each resume re-deriving codebase context it already had. Split by file/concern instead (e.g. "add the detection function" as one story, "wire it into the gate" as a dependent follow-up) even when the combined work is small enough for a single PR by hand.