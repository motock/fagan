# OT-02-repo-issue — follow-up story for the OT-02 churn

Status: filed in the plan manifest (`~/.claude/plans/observability-otel.manifest.json`,
key `OT-02-repo-issue`) by `pipeline.triage_repo_issue._execute_repo_issue`. This
file is the human-readable, version-controlled copy of that record; the JSON block
below is the exact story object the manifest stores.

## Hold on the parent

OT-02 stays parked for a human. Do **not** mark OT-02 done, do **not** merge PR
#1347, do **not** redispatch OT-02 for another rework round. Its work is live:
`f8453e5` on `agent/ot-02`, PR #1347 open.

## Defect (a) — an unmeasurable new-commits check becomes a false "no new commit" park

Real path (no invented state, no high-water mark exists in this codebase):

1. `pipeline/git_ops.py::_worktree_new_commits` returns `("unknown", base)` when
   the measurement cannot be made — no base branch resolves in that worktree
   (`git rev-parse --verify --quiet main` exits non-zero; only `origin/master` is
   resolvable there).
2. `pipeline/git_ops.py::_worktree_has_new_commits` returns `state == "yes"`, so
   the tri-state `"unknown"` collapses to `False` — indistinguishable from a
   measured zero.
3. `pipeline/story_status.py` (~line 747, and the rework path from ~line 783)
   reads that `False` as "no new commits" and parks the story as failed with
   `"tests passed but agent branch has no new commits vs <base>"` /
   `"no new commit after N rework redispatches"`.

That is how OT-02 was parked as "no new commit" after two wasted redispatches
while `f8453e5` was committed and pushed on `agent/ot-02`.

Tick trace, required behaviour:

- Tick 1: local `main` unresolved → measurement `unknown`. Required: escalate /
  needs-human (a decision request). Forbidden: park as "no new commit".
- Tick 2: a base ref resolves (`origin/main` or `origin/master`), `f8453e5` is one
  commit ahead → `NEW_COMMITS=1` → dispatch proceeds and the existing work is
  recognised.

Named mistake to guard: `if not _worktree_has_new_commits(...)` conflates
"unmeasurable" with "measured zero". Preserve the tri-state and branch explicitly.

## Defect (b) — dispatched sessions cannot call `request_decision`

`app/backend_claude.py` launches the agent with `--strict-mcp-config` and no
`--mcp-config`, so no MCP server (hence no `request_decision` tool) is exposed to
a dispatched session. The agent could not satisfy the scope gate and had to
self-report `SCOPE-CONFLICT`. `pipeline/scope_gate.py::SCOPE_GATE_REMEDY` already
documents the "tool unavailable" branch, and `request_decision` already exists —
the follow-up must expose it, not re-implement it.

## Scope adjudication — IN SCOPE, do not relitigate

The recording sites `pipeline/service.py::PipelineService.dispatch_story` and
`pipeline/advance_merge.py::_adjudicate_merges` are IN SCOPE for OT-02: brief step
7 mandates them and OT-02's own tests assert them. They stay. Reverting them, or
deleting/weakening those tests to pass a gate, is forbidden.

## Pre-merge requirement

Before any human merges PR #1347, re-derive the diff against a clean `main`. The
current changed-files list is unreliable: it includes `app/oidc_login.py`,
`pipeline/authorization.py` and `deploy/*`, which are not OT-02's scope.

## Story record

```json
{
  "key": "OT-02-repo-issue",
  "summary": "OT-02-repo-issue: escalate an inconclusive new-commits measurement instead of parking, and expose request_decision to dispatched sessions",
  "description": "Follow-up repo-issue filed by triage for story OT-02 (agent/ot-02, commit f8453e5, PR #1347). OT-02 stays parked for a human; this story fixes the pipeline defects that caused two wasted redispatches. It does not implement OT-02's telemetry deliverable.",
  "persona": "software-engineer",
  "model": "sonnet",
  "risk": "low",
  "files": [
    "pipeline/git_ops.py",
    "pipeline/story_status.py",
    "app/backend_claude.py",
    "pipeline/scope_gate.py"
  ],
  "agent_instructions": "Follow-up to OT-02 (branch agent/ot-02, commit f8453e5, PR #1347). HOLD CONDITIONS: OT-02 stays parked for a human - do NOT mark OT-02 done, do NOT merge PR #1347, do NOT redispatch OT-02. Do not re-implement OT-02's telemetry deliverable.\n\nDEFECT (a): an inconclusive new-commits measurement is collapsed into a false 'no new commit' park. Real path: pipeline/git_ops.py::_worktree_new_commits returns ('unknown', base) when no base branch resolves in the worktree (`git rev-parse --verify --quiet main` exits non-zero there; only origin/master resolves). pipeline/git_ops.py::_worktree_has_new_commits returns `state == 'yes'`, so 'unknown' becomes False, indistinguishable from a measured zero, and pipeline/story_status.py (~line 747, plus the rework path from ~line 783) parks the story failed with 'tests passed but agent branch has no new commits vs <base>' / 'no new commit after N rework redispatches'. That parked OT-02 while f8453e5 was committed and pushed on agent/ot-02. There is no persistent high-water mark anywhere in pipeline/; do not introduce one.\nRequired behaviour: preserve the tri-state (yes / no / unknown). An 'unknown' measurement must escalate or mark the story needs-human via a decision request - never a park as fresh work, never a False that reads as 'no new commits'. When the local base is unresolved, fall back to a resolvable remote ref (origin/main or origin/master) and log which ref was used; if nothing resolves, escalate.\nRequired trace: tick 1, local main unresolved -> measurement unknown -> escalate/needs-human (park-for-no-commits forbidden). Tick 2, a base ref resolves and f8453e5 is one commit ahead -> NEW_COMMITS=1 -> dispatch proceeds and the committed work is recognised. Named mistake to guard: `if not _worktree_has_new_commits(...)` conflates unmeasurable with measured-zero; compare the state explicitly in both the predicate and the caller.\n\nDEFECT (b): dispatched sessions cannot call request_decision. app/backend_claude.py runs the agent with --strict-mcp-config and no --mcp-config, so no MCP server (and therefore no request_decision tool) is exposed; the agent could not satisfy the scope gate and had to self-report SCOPE-CONFLICT. Expose the pipeline MCP server (or an equivalent request_decision channel) to dispatched sessions. request_decision already exists and pipeline/scope_gate.py::SCOPE_GATE_REMEDY documents the tool-unavailable branch - wire it up, do not re-implement it.\n\nSCOPE ADJUDICATION (final, do not relitigate): the recording sites in pipeline/service.py (PipelineService.dispatch_story) and pipeline/advance_merge.py (_adjudicate_merges) are IN SCOPE for OT-02 - mandated by brief step 7 and asserted by OT-02's own tests. They stay. Reverting them, or deleting or weakening those tests to pass a gate, is forbidden.\n\nPRE-MERGE REQUIREMENT: before any human merges PR #1347, re-derive the diff against a clean main. The current changed-files list is unreliable and wrongly includes app/oidc_login.py, pipeline/authorization.py and deploy/*, which are not this story's scope.\n\nACCEPTANCE CRITERIA (testable):\n1. Unit test drives _worktree_new_commits to return ('unknown', '') and asserts _worktree_has_new_commits does not collapse that to a plain False, and that the story_status caller escalates/needs-human instead of parking: no park or failure_reason may contain 'no new commit'.\n2. Unit test covers the resolvable-base case: unresolved local main plus a resolvable remote fallback yields a real count, so a branch one commit ahead is dispatched; negative case - a branch equal to base still parks (measured zero, not unknown).\n3. Contract test asserts a dispatched session's invocation exposes request_decision (the strict-MCP launch includes the pipeline MCP config), or that the scope gate's tool-unavailable branch produces a needs-human decision rather than a SCOPE-CONFLICT dead end.\n4. Guard test: no existing test asserting PipelineService.dispatch_story or _adjudicate_merges recording is edited, skipped, or deleted (run OT-02's telemetry tests unmodified).\n5. Guard test: no boolean collapse of the 'unknown' sentinel remains in the park path (no `if not _worktree_has_new_commits(...)` and no truthiness test on the _worktree_new_commits tuple state).\nRun the full suite before finishing.",
  "status": "todo"
}
```
