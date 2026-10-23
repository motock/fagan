# Plan — Local-fleet branch safety (use case 1) and the shared-server ADR (use case 2)

**Status:** Proposed, not yet ingested (2026-10-23). Follows
`SHARED_SERVICE_TOPOLOGY_PLAN.md`, which maps the two stated use cases to T1
(many developers, each running the pipeline locally, sharing only `origin`) and
T3 (one remote pipeline, engineers connected over MCP).

**Tier:** Claude-class (live registry pins `dispatch` to claude/sonnet; no
`PIPELINE_BACKEND_DISPATCH`). No `Preflight:` lines are needed. If the tier ever
changes, the preflight in `.claude/rules/local-dispatch-preflight.md` applies
first.

## 1. Use case 1 — what actually breaks

With no shared filesystem, T1 has one real hazard and one gap:

- **Hazard (destructive):** story branches are `agent/{story_key.lower()}`,
  hardcoded at ten sites in nine modules (`pipeline/pr.py` x2, `dispatch.py`,
  `git_ops.py` x2, `merge.py`, `review_orchestrator.py`, `escalation.py` x2,
  `build_detect.py`). Auto-minted keys are UUIDs and do not collide; keys a plan
  author writes (`SETUP-01`) do. Two engineers on two machines push the same
  branch to `origin`, and the second push overwrites or force-pushes over the
  first. The topology-collision-guards work covers only one machine, because its
  key check reads local manifests.
- **Gap (non-destructive):** the cross-plan file-overlap advisory reads local
  manifests, so it cannot see another machine's in-flight work. Out of scope
  here; see §5.

## 2. Design

Two layers, smallest first, so the destructive failure is closed before any
rename happens.

1. **Fail-closed guard (no migration).** Before a *fresh* dispatch creates
   `agent/<key>`, ask `origin` whether that ref already exists. If it does and
   this story has no record of having created it, refuse the dispatch with a
   recorded reason and leave the story `todo`. Resume and rework dispatches are
   exempt: they already own the branch. No `origin` configured means the guard
   does not apply. An `origin` that is configured but unreachable blocks the
   dispatch (deny by default), matching the merge gate's fail-then-retry stance.
2. **Opt-in namespacing (no breaking change).** One definition of the branch
   name replaces the ten literals. An operator who sets
   `PIPELINE_BRANCH_NAMESPACE` gets `agent/<ns>/<key>` for *new* dispatches. The
   chosen name is recorded on the story as `branch` at dispatch, and every
   reader prefers the recorded name, so stories already in flight resume on
   their old branch. Unset keeps today's names exactly.

Why not namespace by plan name by default: that renames every existing branch
and breaks the resume path, and two engineers can still choose the same plan
name. A per-operator namespace is the identifier that is actually distinct
across machines.

Git constraint to design around: a ref cannot be both a file and a directory.
`agent/alice` (a story keyed `alice`) and `agent/alice/s1` cannot coexist.
Validate the namespace against `^[a-z0-9][a-z0-9-]{0,31}$` and refuse to
dispatch a story whose lowercased key equals the configured namespace.

## 3. Use case 2 — the ADR

The topology record says not to build T3 until someone can answer "who answers
a decision on a plan they did not create". The ADR records that answer. The
decision is the project owner's, so the story drafts it with a recommended rule
and **Status: Proposed**; it is not Accepted until confirmed.

Recommended rule to put in the draft: a plan has one `owner`, taken from the
authenticated MCP caller at ingest; `request_decision` goes to the overlord
first, and anything the overlord escalates goes to the plan owner, with an
optional `delegates` list that may also answer; a decision nobody answers within
a bounded time parks the story rather than proceeding. Open alternatives to list
in the ADR: team-owned plans, and routing by risk class.

## 4. Stories

| Key | Story | Depends on | Files |
|---|---|---|---|
| SHT-01 | One definition of the story branch name; replace the ten literals | none | pipeline modules above |
| SHT-02 | Fail-closed guard against an existing remote `agent/<key>` | SHT-01 | `pipeline/dispatch.py`, new `pipeline/branch_guard.py`, `REFERENCE.md` |
| SHT-03 | Opt-in `PIPELINE_BRANCH_NAMESPACE` with the branch recorded on the story | SHT-01, SHT-02 | `pipeline/` branch helper, `dispatch.py`, `REFERENCE.md` |
| SHT-04 | ADR 0006: decision routing and ownership on a shared pipeline server | none | `docs/adr/` |

## 5. Out of scope

- Cross-machine file-overlap visibility (for example reading open PR file lists
  from the forge). A possible follow-up once the branch guard has run.
- Building any T3 piece: transport, authentication, tenancy, per-operator
  configuration precedence. SHT-04 only records the decision those need.
- Renaming existing branches or worktrees.

## 6. Ingest-ready plan JSON

```json
{
  "repo_root": "/Users/jessecarroll/git/fagan",
  "epics": [
    {
      "summary": "Local-fleet branch safety (use case 1)",
      "stories": [
        {
          "key": "SHT-01",
          "summary": "Define the story branch name once and replace the hardcoded agent/<key> literals",
          "description": "Behavior-preserving refactor that makes the later stories one-line changes.",
          "agent_instructions": "Read `docs/plans/LOCAL_FLEET_BRANCH_SAFETY_PLAN.md` §2. `pipeline/pr.py` already has `_convention_branch(story_key)`. Move that definition to a small new module `pipeline/branch_names.py` as `story_branch(story_key: str) -> str` returning exactly `agent/<key lowercased>`, and make `_convention_branch` delegate to it. Replace every other `f\"agent/{story_key.lower()}\"` literal under `pipeline/` (merge.py, review_orchestrator.py, git_ops.py x2, dispatch.py, escalation.py x2, build_detect.py, and the one inside `_resolve_story_branch`) with a call to `story_branch`. Find them with `grep -rn 'agent/{' pipeline/`; the grep must return only the definition when you are done. Behavior must not change: do not alter alias handling in `_resolve_story_branch`. Existing tests keep passing unchanged. Write the failing tests FIRST in a NEW file `tests/unit/test_branch_names.py`: `story_branch('SETUP-01') == 'agent/setup-01'`; a lowercase key is unchanged; and a source-scan test that asserts no module under `pipeline/` other than `branch_names.py` contains the literal `agent/{`. Do not modify any existing test. Success: new tests plus the full suite pass; `ruff check .` clean.",
          "persona": "software-engineer",
          "risk": "low",
          "files": [
            "pipeline/branch_names.py",
            "pipeline/pr.py",
            "pipeline/merge.py",
            "pipeline/review_orchestrator.py",
            "pipeline/git_ops.py",
            "pipeline/dispatch.py",
            "pipeline/escalation.py",
            "pipeline/build_detect.py"
          ],
          "dependencies": []
        },
        {
          "key": "SHT-02",
          "summary": "Refuse a fresh dispatch when origin already has the story's branch",
          "description": "Closes the destructive cross-machine overwrite without any rename.",
          "agent_instructions": "Depends on SHT-01. Read `docs/plans/LOCAL_FLEET_BRANCH_SAFETY_PLAN.md` §1-§2. Create `pipeline/branch_guard.py` with `remote_branch_exists(repo_root, branch) -> bool` using `git ls-remote --exit-code --heads origin <branch>` (exit 0 exists, 2 absent; any other failure raises `BranchGuardError`, a new exception with a message that never includes the remote URL). If the repo has no `origin` remote, return False. In `pipeline/dispatch.py`, on a FRESH dispatch only (no existing worktree and no recorded prior attempt for the story; read how the resume path decides this and reuse that predicate, do not invent a second one), call the guard with `story_branch(key)` before the worktree is created. If the branch exists remotely, or the guard raises, block the dispatch: leave the story `todo`, record a clear reason naming the story and branch, and emit a structured event with the story key, branch, and outcome. Resume and rework dispatches must not call the guard. Document the behavior and the unreachable-origin rule in `REFERENCE.md` with anchored `str_replace` edits outside any section pinned by `tests/unit/test_readme_reference_split.py`. Write failing tests FIRST in a NEW file using a real local bare repo as `origin`: remote branch exists blocks a fresh dispatch; absent branch allows it; resume ignores an existing remote branch; no origin allows; an unreachable origin blocks with the URL absent from the message. Do not modify any existing test. Success: new tests plus the full suite pass; `ruff check .` clean.",
          "persona": "software-engineer",
          "risk": "medium",
          "files": [
            "pipeline/branch_guard.py",
            "pipeline/dispatch.py",
            "REFERENCE.md"
          ],
          "dependencies": ["SHT-01"]
        },
        {
          "key": "SHT-03",
          "summary": "Add opt-in PIPELINE_BRANCH_NAMESPACE and record the dispatched branch on the story",
          "description": "Per-operator namespacing with no migration: old stories keep their recorded branch.",
          "agent_instructions": "Depends on SHT-01 and SHT-02. Read `docs/plans/LOCAL_FLEET_BRANCH_SAFETY_PLAN.md` §2. In `pipeline/branch_names.py`, make `story_branch(story_key, recorded=None)` return `recorded` when given; otherwise `agent/<ns>/<key lowercased>` when `PIPELINE_BRANCH_NAMESPACE` is set and valid, otherwise `agent/<key lowercased>`. Validate the namespace against `^[a-z0-9][a-z0-9-]{0,31}$`; an invalid non-empty value raises `ValueError` naming the variable (never silently ignored). Refuse to dispatch a story whose lowercased key equals the namespace, because git cannot hold both `agent/x` and `agent/x/y`. At dispatch, record the chosen name on the story as `branch`. Make every reader that has the manifest story pass `recorded=story.get('branch')`, so a story dispatched before this change resumes on its old branch. Update `_resolve_story_branch` in `pipeline/pr.py` so its alias-suffix check uses the effective convention branch, not the literal. Document the variable in `REFERENCE.md` (anchored edits, outside pinned sections) and state that two operators sharing an origin should each set a distinct value. Write failing tests FIRST in a NEW file: unset keeps `agent/<key>`; set produces `agent/<ns>/<key>`; recorded name wins over the namespace; invalid namespace raises with the variable name; key equal to namespace is refused; dispatch records `branch`; a story with no recorded `branch` resumes on the convention name. Do not modify any existing test. Success: new tests plus the full suite pass; `ruff check .` clean.",
          "persona": "software-engineer",
          "risk": "medium",
          "files": [
            "pipeline/branch_names.py",
            "pipeline/dispatch.py",
            "pipeline/pr.py",
            "REFERENCE.md"
          ],
          "dependencies": ["SHT-01", "SHT-02"]
        }
      ]
    },
    {
      "summary": "Shared pipeline server decision (use case 2)",
      "stories": [
        {
          "key": "SHT-04",
          "summary": "Write ADR 0006 on decision routing and plan ownership for a shared pipeline server",
          "description": "Doc-only. Drafts the answer the topology record requires before any T3 work; stays Proposed until the project owner confirms it.",
          "agent_instructions": "Read `docs/plans/SHARED_SERVICE_TOPOLOGY_PLAN.md` (T3 section and 'Prerequisites for C') and `docs/plans/LOCAL_FLEET_BRANCH_SAFETY_PLAN.md` §3. Write `docs/adr/0006-decision-routing-on-a-shared-server.md` following `docs/adr/template.md`, with **Status:** Proposed and **Date:** 2026-10-23, and add its row to the index table in `docs/adr/README.md`. Record: the question (who answers a decision on a plan they did not create); the recommended rule from §3 (single plan `owner` taken from the authenticated MCP caller at ingest; overlord first; escalation to the owner with an optional `delegates` list; an unanswered decision parks the story after a bounded time); the alternatives considered (team-owned plans, routing by risk class, last-writer answers) and why each was not chosen; that ownership must be enforced in the mutating tool surface and not only in routing; that namespaced branches (PIPELINE_BRANCH_NAMESPACE) are a prerequisite for a multi-tenant server; and the consequences, including that `.pipeline.env` precedence and the stdio transport are not addressed here. State plainly that nothing is decided until the status changes to Accepted. Do not write code and do not edit any existing ADR. [no-new-tests] — `tests/unit/test_adr_records.py` already grades format and index membership. Success: that test file and `tests/unit/test_docs_have_no_personal_home_path.py` pass.",
          "persona": "tech-writer",
          "risk": "low",
          "files": [
            "docs/adr/0006-decision-routing-on-a-shared-server.md",
            "docs/adr/README.md"
          ],
          "dependencies": []
        }
      ]
    }
  ]
}
```

## 7. Definition of Done

- SHT-01 to SHT-03 merged through `approve_merge`, each with a reproducing test
  observed failing first.
- A real two-clone check, run once for real: two local clones of one bare
  `origin`, the same plan key dispatched from each; the second is refused with
  a recorded reason and the first branch is untouched. Print the refusal
  beside the branch's untouched SHA (per `testing-config-gates.md`: validate a
  gate that withholds work against a real environment).
- ADR 0006 merged as Proposed; its status is changed only by the project owner.
