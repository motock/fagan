# Plan retrospectives

One line per retro, newest first. See `PLAN_RETROSPECTIVE_PROCESS_PLAN.md` at
the repo root for the process this index is part of.

| Date | Plan | Outcome | Retro |
|---|---|---|---|
| 2026-07-21 | `plan-retro-process` (mark_story_done plan-completion + TDD-split unconditional fix) | Fix validated live end-to-end; feature merged clean, but surfaced Mode 29 (stale duplicate review pass corrupts an already-merged story's status), Mode 30 (scheduler-vs-manual-git race **corrupted a source file on disk**), and an MCP-server-restart gap (L4) | [tdd-split-unconditional-and-review-race_2026-07-21.md](tdd-split-unconditional-and-review-race_2026-07-21.md) |
| 2026-07-21 | `always-on-checklist-and-tdd-split` (Story 2, TDD-split always-on) | Gate logic shipped correct; story merged **incomplete** (5 unaddressed review findings, Mode 28) and needed a manual cleanup follow-up (PR #155) | [tdd-split-always-on_2026-07-21.md](tdd-split-always-on_2026-07-21.md) |
