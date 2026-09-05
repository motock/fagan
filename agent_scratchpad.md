# Scratchpad — 1f9f2c46 maturity panel story

## State
- Prior WIP commit (b8d8cd8) broke shared files: main.js lost `renderDecisions`
  import, gutted module.exports, mangled renderConfigError; plan-detail.js lost
  board.js imports; story-modal.js switched to a static NOTIF_SEVERITY_COLOR import.
- Restored plan-detail.js / story-modal.js / main.js partially by hand.
- Toast tests fail ONLY because main.js:46 `renderDecisions is not defined`
  (import was dropped). Re-added the import; now restoring module.exports block.

## Next
1. DONE: baseline restored; toast files green (30 passed).
2. DONE: real maturity.js renderer (metrics + guard sections, error rows,
   recurrence alerts, muted dataset-missing line, "-" for null cost).
3. DONE: plan-detail.js mounts #maturity-panel and fires renderMaturityPanel.
4. DONE: api.js helpers encodeURIComponent'd; maturity.js uses them.
5. DONE: tests/unit/test_maturity_panel.py — 10 passed.
6. DONE: dashboard slice 949 passed after fixing main.js import name.
7. NEXT: ruff check ., full gate, commit, push.
5. ruff check . ; full gate; commit.