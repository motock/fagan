# Post-LD90 items 5 and 6

Follow-on to the 2026-09-23 "what next" review. Items 1-4 are the
`ld90-closeout` plan (`docs/plans/LD90_CLOSEOUT_PLAN.json`, ingested
2026-09-23). Item 7 (PR title/commit message vs. final diff) comes last.

## Item 5 - prove the new gates fire on real stories

The LD90 gates merged 2026-09-22/23 with unit tests only. Per
`.claude/rules/testing-config-gates.md`, a gate that can withhold work is not
done until it has run against the real environment.

| Gate | Event | Status (2026-09-23) |
|---|---|---|
| W1a preflight ingest gate | rejection text, `preflight_override` | **Verified live**: a probe plan with no `Preflight:` line was rejected by `ingest_plan` and wrote no manifest. |
| W3-2 sizing auto-route | `sizing_auto_routed` | Not yet exercised. `ld90-closeout` pins every oversized story explicitly, so it will not fire there. |
| W3-4 pre-review scope gate | `scope_gate_failed` | Not yet exercised. Every `ld90-closeout` story declares `files`, so any out-of-scope edit will trip it. |
| W1b plan-conflict ruling | `plan_conflict_preauthorized`, or `story_parked` with a plan-conflict reason | Not yet exercised. It fires only when a red suite fails solely in untouched pre-existing tests. |

Check after `ld90-closeout` completes:

1. `grep -h '"event": "\(scope_gate_failed\|plan_conflict_preauthorized\|sizing_auto_routed\)"' ~/.claude/plans/ld90-closeout.notifications.jsonl`.
2. For every story that went red in a pre-existing test it did not touch,
   confirm the W1b ruling ran instead of a rework cycle. If it did not,
   that is a wiring bug: file it with the story key and the grade output.
3. Re-run `scripts/local_success_report.py --window 30` and record the
   on-device and cloud-OSS rates next to the 2026-09-23 baseline (86.7%
   overall, 2/3 on-device, 24/27 cloud-OSS over the last 30).

No code is planned for item 5 unless one of these checks fails.

## Item 6 - bring the oversized modules under 1000 lines

Plan: `docs/plans/OVERSIZED_MODULE_SPLIT_PLAN.json` (5 stories, cloud-OSS
tier because every story edits a file over the cap).

| Story | Moves | Result |
|---|---|---|
| SPL-1 | triage patch_acceptance executor -> `triage_patch_acceptance.py` | triage.py 1319 -> 1030 |
| SPL-2 (after SPL-1) | split_story / mark_done executors -> `triage_story_actions.py` | triage.py -> 845 |
| SPL-3 | `PipelineService.request_decision` body -> `service_decisions.py` | service.py 1090 -> ~1004 |
| SPL-4 (after SPL-3) | `set_plan_role_config` / `set_role_default` bodies -> `service_role_config.py` | service.py -> 896 |
| SPL-5 | decision/review/merge/plan-control MCP tools -> `server_tools_lifecycle.py` | server.py 1175 -> 991 |

Each brief carries a self-contained move script (anchors asserted unique,
verbatim slice, `_ModuleRef`/`_ServerRef` bindings back to the origin
module so existing monkeypatches keep landing). The exact brief commands
were replayed on fresh worktrees; the resulting trees passed the full
suite (12567 passed) and ruff. SPL-5 pre-authorizes three
definition-count test edits that pin each tool wrapper to server.py's
source.

**Deliberately excluded:** `scheduler_daemon.py` (1024) and
`worktree_patch.py` (1004). Both are marginally over, and their tests
patch module globals (`sd.subprocess`, `_DRAIN_JOIN_TIMEOUT_SECONDS`) and
read function source from the original module, so a split would rewrite
tests for a 4-24 line gain. Revisit when either grows.
