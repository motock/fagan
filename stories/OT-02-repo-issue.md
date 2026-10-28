```json
{
  "id": "OT-02-repo-issue",
  "key": "OT-02-repo-issue",
  "type": "repo-issue",
  "parent": "OT-02",
  "parent_commit": "f8453e5",
  "parent_branch": "agent/ot-02",
  "parent_pr": {
    "number": 1347,
    "state": "OPEN",
    "head_ref_name": "agent/ot-02",
    "merged": false
  },
  "status": "open",
  "severity": "high",
  "persona": "software-engineer",
  "model": null,
  "risk": "medium",
  "backend": null,
  "summary": "Repo issue from OT-02: the new-commits measurement parks completed work on an INCONCLUSIVE base-branch read, and dispatched sessions have no request_decision tool to resolve a scope conflict",
  "files": ["pipeline/advance_merge.py", "pipeline/service.py", "pipeline/server.py"],
  "acceptance": [],
  "dependencies": [],
  "defects": {
    "a": "The new-commits/base-branch measurement returned INCONCLUSIVE because `main` is unresolved in the OT-02 worktree (`git rev-parse --verify main` exits non-zero, and the `origin/main` fallback is unresolved there too), and the pipeline converted that inconclusive result into a false \"no new commit\" park — after two wasted redispatches — even though the work was committed at f8453e5 on agent/ot-02 and is present on that branch.",
    "b": "Dispatched sessions expose no `request_decision` tool, so the agent could not satisfy the scope gate and had to self-report SCOPE-CONFLICT instead of getting a ruling."
  },
  "scope_adjudication": {
    "ruling": "IN SCOPE",
    "reverted": false,
    "tests_deleted": false,
    "in_scope_sites": [
      "pipeline/service.py::PipelineService.dispatch_story",
      "pipeline/advance_merge.py::_adjudicate_merges"
    ],
    "basis": "Brief step 7 mandates the recording sites in PipelineService.dispatch_story and _adjudicate_merges, and OT-02's own tests assert them, so they stay. Reverting them or deleting the tests to pass the scope gate is forbidden."
  },
  "hold_conditions": [
    "OT-02 stays parked for a human: do not mark it done.",
    "Do not merge PR #1347.",
    "Do not redispatch OT-02 for another rework round — the work is already committed and pushed at f8453e5."
  ],
  "pre_merge_requirement": "Before any human merge, re-derive the diff against a clean `main`. The current changed-files list is unreliable: it appears to include auth/identity (app/oidc_login.py, pipeline/authorization.py) and deploy/production (deploy/*) files that are not this story's scope.",
  "agent_instructions": "Fix two pipeline measurement/escalation defects. Do not implement OT-02's own deliverable and do not touch the IN SCOPE recording sites' behaviour. See the body of stories/OT-02-repo-issue.md for the required two-call trace (Call 1 must not advance the high-water mark) and the acceptance criteria for defects (a) and (b).",
  "acceptance_criteria": [
    "An unresolved base branch (`git rev-parse --verify main` non-zero, `origin/main` also unresolved) yields a measurement state distinct from NO_NEW_COMMITS and escalates as needs-human; it never parks the story as \"no new commit\".",
    "On an INCONCLUSIVE measurement the tracked high-water mark is left unchanged: it must not be advanced, so a later tick can still observe the new commit.",
    "The high-water mark is advanced only when a measurement positively counts commits, and only to the measured HEAD.",
    "A regression test drives two consecutive ticks — tick 1 with `main` unresolved and HEAD f8453e5, tick 2 with `origin/main` = 1a2b3c4 — and asserts the first escalates without moving high_water off aaaa111 and the second reports NEW_COMMITS=1 and proceeds.",
    "Dispatched sessions have a `request_decision` tool that lets the agent ask the overlord to adjudicate a scope conflict and receive a ruling, instead of self-reporting SCOPE-CONFLICT and stalling.",
    "A session that hits a scope conflict receives a recorded ruling that names the offending paths and whether they are in or out of scope."
  ]
}
```

# OT-02-repo-issue — pipeline defect: completed work parked as "no new commit"

**Type:** repo-issue **Parent:** OT-02 **Status:** open **Severity:** high

Triage ruled that OT-02 failed for an environmental reason, not because of its own
scope. This story fixes the repository/pipeline condition. It does **not** implement
OT-02's own deliverable.

## Live evidence (read-only, captured in the OT-02 worktree)

| Check | Result |
| --- | --- |
| `git rev-parse --verify --quiet main` | `UNRESOLVED` |
| `git rev-parse --verify --quiet origin/main` | `UNRESOLVED` (only `origin/master` exists) |
| `git log --oneline -3 agent/ot-02` | `f8453e5 refactor(telemetry): own the dispatch result classifier` |
| `gh pr view 1347 --json number,state,headRefName` | `{"headRefName":"agent/ot-02","number":1347,"state":"OPEN"}` |

The recorded `parked_reason` is **stale**. The work is committed at `f8453e5` on
`agent/ot-02` and present on that branch. Escalating or splitting the scope would
duplicate finished work.

### Evidence freshness

The table above is the read-only state captured while filing this story, and
`f8453e5` is the incident commit named by the triage ruling. While this story was
being written the pipeline re-dispatched OT-02, which rebased `agent/ot-02` onto a
newer base: the branch head became `db75253` (PR #1347's `headRefOid`) carrying the
same telemetry change, and `f8453e5` still resolves as an object. Neither commit
should be read as a new deliverable — the content is the work already reviewed at
`f8453e5`. This re-dispatch is itself an instance of the churn defect (a) exists to
stop.

## Defect (a) — INCONCLUSIVE measurement turned into a false "no new commit" park

The new-commits/base-branch measurement returned `INCONCLUSIVE` because `main` is
unresolved in that worktree. The pipeline then converted that inconclusive result
into a false **"no new commit"** park — after **two wasted redispatches** — even
though the work was committed at `f8453e5` on `agent/ot-02` and present there.

An unresolved base branch is an *unknown*, not a *zero*. It must escalate as
`needs-human`, never be reported as "no new commit".

### Required two-call trace

**Call 1 (dispatch):** HEAD = `f8453e5`; local `main` unresolved → measurement
`INCONCLUSIVE`. Prior high-water mark stays `aaaa111`. Required action:
**escalate / needs-human**, never "no new commit", and **the high-water mark must
not be advanced**.

**Call 2 (next tick, `origin/main` = `1a2b3c4` now resolvable):**
`git rev-list --count aaaa111..f8453e5` = `1` → `NEW_COMMITS=1` → dispatch proceeds;
only now does the high-water mark become `f8453e5`.

**The specific mistake to avoid:** getting Call 1's *return value* right ("not a new
commit, don't park this as fresh work") while still overwriting the tracked state
with `high_water=f8453e5`. Then Call 2 compares `f8453e5..f8453e5` = `0` →
`NO_NEW_COMMITS` → the story is parked forever. That is the churn this issue exists
to prevent.

## Defect (b) — no `request_decision` tool in dispatched sessions

Dispatched sessions have no `request_decision` tool, so the agent could not satisfy
the scope gate and had to self-report `SCOPE-CONFLICT` with no way to get a ruling.
The agent should have been able to ask the overlord to adjudicate.

## Scope adjudication — IN SCOPE

The recording sites in `pipeline/service.py` (`PipelineService.dispatch_story`) and
`pipeline/advance_merge.py` (`_adjudicate_merges`) are mandated by brief step 7 and
asserted by OT-02's own tests, so **they stay**. Reverting them is forbidden, and
deleting the tests to pass the gate is forbidden too. Note the asymmetry: the
story's declared `files` list does not name the last two paths, which is what
tripped the scope gate — the scope list was wrong, not the code. This story did not
revert the recording sites and did not delete their tests.

## Hold conditions

- OT-02 stays **parked for a human** — do not mark OT-02 done.
- Do **not** merge PR #1347.
- Do **not** redispatch OT-02 for another rework round; the work is at `f8453e5`.

## Pre-merge requirement

Before any human merge, **re-derive the diff against a clean `main`**. The current
changed-files list is unreliable: it appears to include auth/identity
(`app/oidc_login.py`, `pipeline/authorization.py`) and deploy/production (`deploy/*`)
files that are not this story's scope.

## Acceptance criteria

See `acceptance_criteria` in the record above: an unresolved base branch yields a
non-`NO_NEW_COMMITS` state that escalates and leaves the high-water mark unchanged;
the mark advances only on a positive measurement; a two-tick regression test pins
this; and dispatched sessions gain a `request_decision` tool that returns a recorded
ruling for scope conflicts.
