# Plan — `check_story_status` raises on finished stories and unknown plans

**Status (verified 2026-10-23): DONE.** Merged as PR #1209 (pipeline plan
`check-story-status-terminal`, story `CSST-1`, CI green): a finished story and
an unknown plan now return a result instead of raising.

**Earlier status:** ingested as `check-story-status-terminal`. Re-verified on master `23777ba` before ingest: the bug still
reproduces, and an impact run of a stand-in fix gave 0 pre-existing failures.

**Original status:** Proposed, not yet ingested (2026-10-22). Claude-class dispatch
(live registry `roles.dispatch` is claude/sonnet), so the local-dispatch
preflight does not apply.
**One-line goal:** make `check_story_status` return a structured result instead
of an unhandled exception for (1) stories that are already `done`/`merged` and
(2) a plan name with no manifest.

## 1. Diagnosis

Found 2026-10-22 while asking for the status of the finished
`cross-plan-awareness` plan. The MCP layer surfaced only a bare
`Error executing tool check_story_status`; calling the function directly
shows the real exceptions.

| Input | Result today | Cause |
|---|---|---|
| `("cross-plan-awareness", "XP-1")` — story `done`, worktree deleted at merge | `FileNotFoundError` on `~/.claude/worktrees/XP-1` | A finished story keeps its `pid` in the manifest, so the `"pid" not in story` guard (`pipeline/story_status.py:88`) does not fire. The process is dead, so the function proceeds to `detect_test_command(worktree)` (`pipeline/story_status.py:512`), whose `cwd.iterdir()` (`pipeline/ecosystem_detect.py:240`) raises. |
| `("no-such-plan", "X")` | `FileNotFoundError` on the manifest path | `_store.get_manifest` (`pipeline/store.py:128`) does `json.loads(self.manifest_path(plan_name).read_text())` with no existence handling. |
| `("real-plan", "NOPE")` | `{"ok": false, "error": "Story not dispatched"}` | Already correct; keep it. |

The wiring is fine (`bind_to_server` binds the globals correctly). Only these
two input classes fail.

## 2. Scope

One story, two production files, new function count 0–1.

**Production changes** (`pipeline/story_status.py`; `pipeline/store.py` only if
the missing-manifest handling belongs there rather than at the call site —
prefer the call site so `get_manifest`'s other callers keep raising):

1. Unknown plan: catch the missing manifest at the top of
   `check_story_status` and return `{"ok": False, "error": "Plan not found"}`.
   Do not echo the manifest path (no internal paths in errors).
2. Finished story: after the "Story not dispatched" guard, if
   `story["status"]` is `done` or `merged`, return
   `{"status": story["status"], "pid": story["pid"]}` without touching the
   worktree or running tests. This mirrors the existing `interrupted`
   short-circuit directly below it.

Do NOT change behaviour for `in_progress`, `tests_passed`, `pr_open`,
`changes_requested`, `parked` or `failed` stories, and do not alter the
watchdog, wedge or oracle paths.

## 3. Tests (new file `tests/unit/test_check_story_status_terminal.py`)

Build manifests in a tmp `PLAN_DIR`; do not use the real one.

Positive:
- a `done` story with a `pid` and a nonexistent worktree returns
  `{"status": "done", ...}` and does not call `detect_test_command`;
- a `merged` story behaves the same.

Negative / boundary:
- an unknown plan returns `{"ok": False, "error": "Plan not found"}` and the
  message contains no filesystem path;
- an unknown story key in a real plan still returns "Story not dispatched";
- a dispatched `interrupted` story still returns its existing result;
- a `done` story WITHOUT a `pid` still returns "Story not dispatched"
  (guard order unchanged).

TDD: write the first two and the unknown-plan test first and observe them fail
with `FileNotFoundError` (the right reason), then fix.

## 4. Success criteria

- Full suite green (`.venv/bin/python -m pytest -q`), `ruff check .` clean.
- `scripts/check_line_limit.py` clean.
- Calling `check_story_status` for any finished story in the plan store
  returns a dict, never raises.

## 5. Notes for plan authoring

- `story_status.py` reads server globals injected by `bind_to_server`
  (hence the `# noqa: F821` markers); new code must follow the same
  convention and not import them directly.
- Include `Preflight:` only if this is later routed to a non-Claude tier; if it
  is, run the impact check in `.claude/rules/local-dispatch-preflight.md` first.
