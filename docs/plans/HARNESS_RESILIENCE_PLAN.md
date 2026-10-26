# Harness resilience plan

Status: ingested 2026-10-25 as plan `harness-resilience` (all stories `todo`). Tier: whatever the live registry's `dispatch` role
resolves to when the plan runs (planned: ollama, 2026-10-25 evening; no
per-story pin). Ingest JSON:
`~/.claude/plans/harness-resilience.json` (plan name `harness-resilience`).

## 1. Why

Follow-ups from the parked-story recovery work (2026-10-23/24), ranked by how
often they cost a human a manual step.

| Gap | What happened | Epic |
| --- | --- | --- |
| Usage cap is silent | Session usage hit 100%, the gate paused dispatch and interrupted running agents, stories sat in `interrupted`, and nobody was told. An agent can also die on the cap before the lagging poller trips the gate, then be redispatched into the same wall. | 1 |
| Merge-gate rebase conflicts park | CL-12 and CL-20 hit two additive blocks at one insertion point. The gate aborts the rebase, retries identically, and parks. | 2 |
| Instance-queue spooler has one guard | NC-10 isolates the pytest session via `tests/conftest.py`; any test that sets its own config path can again leak fixture events to live e-mail and Slack. | 3 |

## 2. Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Who resolves a rebase conflict | The implementer agent, via the existing CI-rework route (`review_feedback`, `ci_rework`) | An agent can read both sides; a heuristic resolver cannot. Reuses bounded machinery instead of a second one. |
| Conflict rework default | On (`PIPELINE_REWORK_ON_CONFLICT` unset or any value but `0`), capped at 2 rounds; `0` disables | Owner decision: it ships enabled. The cap, the review re-entry and the high-risk hold are the safety net. |
| Review after a conflict rework | The agent commits, so the story re-enters tests, review and merge. High-risk stories return to the human hold | A resolved conflict is new code. It must not skip review. |
| Usage-cap death | Not charged to `rework_attempts`, `transport_retries` or `dispatch_attempts`; own counter capped at 20 | Waiting for a reset is not a failure. The cap stops a mis-detection from looping. |
| Cap lag | A cooldown file (default 600 s) makes `ClaudeBackend.resource_status` report not-ok | The poller trails the real cap; the agent's own death is the earliest signal. Fails open on a corrupt file. |
| Stall visibility | Detection-only `story_stalled` notification, one per story per threshold | Mirrors `run_wedge_scan`. No new recovery path. |
| Slack route | Unchanged here. Revert the Slack events to the e-mail list when `parked-story-recovery` completes | Operational, not code. |

## 3. Stories

| Key | Summary | Files | Risk | After |
| --- | --- | --- | --- | --- |
| UC-01 | Announce usage-gate pause and resume (`usage_paused`, `usage_resumed`) | `usage_transitions.py` (new), `usage.py`, `REFERENCE.md` | low | `parked-story-recovery:HR-04` (shares `REFERENCE.md`) |
| UC-02 | Cooldown after an agent hits the cap | `usage_cooldown.py` (new), `app/backend_claude.py` | medium | |
| UC-03 | Treat an agent dying on the cap as a capacity wait | `no_progress.py` | medium | UC-01, UC-02 |
| UC-04 | `story_stalled` notification for ready-but-undispatched stories | `stall_scan.py` (new), `advance.py`, `REFERENCE.md` | medium | UC-01 |
| RC-01 | Build the rebase-conflict rework brief | `rebase_conflict_feedback.py` (new) | low | |
| RC-02 | Route a gate rebase conflict to the implementer | `advance_merge.py`, `REFERENCE.md` | high | RC-01, UC-04 |
| NG-01 | Spooler refuses the live plan directory under pytest | `notification_outbox.py` | low | |

Headroom checked with `wc -l`: `advance.py` 927 (UC-04 adds about 8 lines),
`advance_merge.py` 532, `usage.py` 486, everything else under 400.
`REFERENCE.md` is over 2000 lines; the three stories that edit it add a
subsection or a table row outside any section `test_readme_reference_split.py`
pins, and run on Claude, not a weak tier.

Order: UC-01, then UC-04 (both edit `REFERENCE.md`), then RC-02. UC-02, UC-03,
RC-01 and NG-01 touch separate files and can run alongside.

## 4. Security notes

- RC-02 is `risk: high`: it lets an agent rewrite already-reviewed code. Mitigated by routing the result back through tests and review, the 2-round cap, and the flag defaulting off. `parse_conflict_files` rejects absolute paths, `..` and control characters before any path reaches a brief.
- The cooldown and announced files fail open (a corrupt file never blocks dispatch or suppresses the first announcement).
- Notification text carries percentages, reset text and file paths only, never environment values.

## 5. Not in this plan

- Granting `request_decision` to dispatched agents (revisit after HR-04 shows how many scope parks remain).
- Wiring `REBASE-CONFLICT-UNRESOLVED:` into a park path (RC-01 only provides `parse_unresolved`).
- Syncing `~/.claude/fagan-rules/` from the repo's `.claude/rules/` in the installer.
- Splitting near-limit modules; HR-06 warns at ingest first.

## 6. Definition of done

All seven stories merged through `approve_merge`; full suite and `ruff check .`
green; the `story_stalled` threshold run against the live manifests with the
measured ages printed beside the threshold (UC-04); the `PIPELINE_REWORK_ON_CONFLICT=0` kill switch tested.

## 7. Ollama-tier note

Dispatch now resolves to `ollama` / `deepseek-v4.1-flash` (all roles), so every
story carries a one-line `Preflight:` summary — impact run 2026-10-25 on
`901a4ac` in a scratch worktree, rough stand-in per story, full suite and
`ruff` (0 conflicts; the local venv's ruff 0.16.8 flags one pre-existing `I001`
in `tests/unit/test_branch_namespace.py` that the pinned ruff 0.17.1 accepts).
The three `REFERENCE.md` stories (UC-01, UC-04, RC-02) are chained by
`dependencies` so their edits to that file never race at the merge gate.
