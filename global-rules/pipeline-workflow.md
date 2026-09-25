# Pipeline workflow (any project, any agent CLI)

Applies when the `pipeline` MCP server (fagan) is registered with your client.
If it is not registered, skip this file: the engineering standards still apply.

Tool names below are the bare names. Your client may prefix them
(`mcp__pipeline__save_plan` in Claude Code); match on the suffix.

## 1. Drive work through the pipeline
Do not start coding against an ad hoc, untracked request. Register it first,
then claim it, then work on the branch the pipeline names.

1. Resolve the implementer strength class BEFORE decomposing (see
   `fagan-rules/agent-dispatch-story-sizing.md`). Check a `dispatch` role in the
   live model registry (the path in `PIPELINE_MODEL_REGISTRY_PATH`, not
   necessarily `model_registry.json`), then `PIPELINE_BACKEND_DISPATCH`. State what
   you found and let the user confirm. Unset means Claude-class. `auto` means
   assume the weak end.
2. Turn the goal into a plan with `decompose_plan` (or the `product-analyst`
   persona). Pass the confirmed tier explicitly; the decomposer cannot ask.
   Do not hand-write large stories.
3. For any non-Claude tier, run the local-dispatch preflight
   (`fagan-rules/local-dispatch-preflight.md`) on every story before ingesting.
4. Register with `save_plan` or `ingest_plan`. The story schema is in
   `fagan-rules/pipeline-story-schema.md`; do not invent fields.
5. Find work with `list_ready_stories`, claim with `dispatch_story`, then
   `mark_story_in_progress`. Branch as `agent/{STORY-ID}`.
6. Blocked on a decision that is the user's to make: `request_decision`
   (check `list_decisions` for precedent first).

Never spawn a separate implementation agent for a pipeline story:
`dispatch_story` already starts the worker.

## 2. Finish through the review gate
1. Full test suite green, then `review_story`. Do not commit on a blocking verdict.
2. Clean verdict: `advance_pipeline`. Merge via `approve_merge`, never a manual
   `gh pr merge` (it skips the rebase, CI poll, and acceptance re-run). If a manual
   merge is unavoidable, confirm `gh pr checks <branch>` is all-green first.
3. Show the user the branch, files changed, and the proposed commit message,
   and ask before committing or opening a PR.
4. After it lands, `mark_story_done`.

## 3. A failed or stalled attempt is not "try again"
Pull the actual failure evidence, find the exact cause in the code, and put a
two-sentence diagnosis into the next attempt's instructions. Narrow the scope
to one concern. Weaker executors need more of the diagnosis done up front.

## 4. Rules to read on demand (installed beside this file)
| File | Read it when |
|---|---|
| `fagan-rules/pipeline-story-schema.md` | authoring or reviewing a plan |
| `fagan-rules/agent-dispatch-story-sizing.md` | sizing stories for a weaker executor |
| `fagan-rules/local-dispatch-preflight.md` | before ingesting any non-Claude story |
| `fagan-rules/code-review.md` | reviewing a change |
| `fagan-rules/testing-config-gates.md` | testing config-driven logic or resource gates |
