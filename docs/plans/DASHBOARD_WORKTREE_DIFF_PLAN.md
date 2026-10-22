# Plan — Worktree Diff Snapshot in the Story Modal

**Status:** Proposed, not yet ingested (2026-10-22).
**One-line goal:** let a human in the dashboard see what an in-flight story's
agent has actually changed in its worktree — a live `git diff` snapshot against
the branch's fork point, including uncommitted and untracked work.

**Relationship to `DASHBOARD_STORY_PROGRESS_PLAN.md`:** that plan answers *"how
far through its checklist is this attempt?"* (checklist/scratchpad/step budget).
This plan answers a different question: *"what has the attempt changed?"* It is
independent of, and complementary to, that plan's tiers.

---

## 1. Background — what already exists

Reuse, don't rebuild:

- **Story modal sections.** `static/app/render/story-modal.js` already renders
  Checklist, Journal, Replay, Notifications and a Log tail, each loaded
  asynchronously into a slot after the modal opens. A Diff section follows the
  same pattern.
- **The worktree path is on the story.** `pipeline/dispatch.py` records the
  absolute worktree path as `story["worktree"]` at dispatch time. A story that
  was never dispatched has no worktree.
- **Worktree lifetime.** A fresh dispatch creates
  `WORKTREE_ROOT / <story_key>` as a real git worktree on branch
  `agent/<story_key.lower()>`, branched from `origin/<default>`. `_merge_pr`
  (`pipeline/pr.py`) removes the worktree and deletes the local and remote
  branches after a successful squash merge. So a diff is available while the
  story is in flight and gone afterwards — "gone" is a normal state, not an
  error.
- **A diff renderer already exists.** `static/app/patch.js` has
  `diffLineClass()` + `renderDiff()` (escaping every line, classing
  add/del/hunk/context). It is currently module-private; exporting it gives the
  new section an XSS-safe renderer for free.
- **Base-ref resolution has a precedent.** `pipeline/review.py`'s
  `_first_review_base()` tries `origin/HEAD`, `origin/main`, `origin/master`,
  `main`, `master` with `git merge-base`. The reviewer also already receives an
  inline branch diff capped by `REVIEWER_INLINE_DIFF_MAX_CHARS` (default 40k).
  The dashboard view is the same information, on demand.
- **Worktree artifact exclusions already exist.** `pipeline/paths.py`'s
  `_WORKTREE_LOG_EXCLUDES` (`*.log`, `*.log.ts`, `.agent_plan.md`,
  `.agent_scratchpad*.md`, `.tdd_split_test_author_done`,
  `.dispatch_baseline_test_checked`, …) is written into `.git/info/exclude` and
  must also be applied as pathspec excludes so a snapshot never reports the
  agent's own bookkeeping as source changes.
- **A service seam.** `pipeline/service.py`'s `PipelineService` is what both
  the dashboard and the MCP server call; `app/dashboard_routes.py` holds the
  read-only routes; `pipeline/server.py` registers the MCP tools.

## 2. Design

### 2.1 What "snapshot" means

Three layers, all shown in one view:

| Layer | Source | Why |
|---|---|---|
| Committed on the branch | `git diff <base_sha>..HEAD` | what a reviewer/merger will see |
| Uncommitted (staged + unstaged) | `git diff HEAD` | the agent's work since its last checkpoint |
| Untracked files | `git ls-files --others --exclude-standard` | new files not yet `git add`ed |

The combined diff = `git diff <base_sha>` (committed + uncommitted, relative to
the fork point) plus one synthesized addition diff per untracked text file.
`base_sha` is `git merge-base <candidate> HEAD` for the first candidate that
resolves, using the same candidate list as `_first_review_base`. If no candidate
resolves, the view degrades to `available: false, reason: "no_base_ref"` rather
than diffing against an arbitrary commit.

The summary intentionally reports the three layers separately so a human can see
whether the agent has checkpointed — a large uncommitted layer is a useful
warning that a crash would lose work.

### 2.2 Backend module: `pipeline/worktree_diff.py` (new)

Pure read-only logic plus one subprocess boundary. No writes to the worktree,
ever — a live agent may be using it.

```
resolve_worktree(story, worktree_root) -> Path | None
    absolute + resolve(strict=False) + is_relative_to(worktree_root.resolve())
    and is_dir(); None otherwise. Mirrors pipeline/store.py::get_worktree_file.

resolve_base_sha(worktree) -> tuple[str | None, str | None]   # (sha, ref)
    merge-base against the review.py candidate list.

collect_diff_summary(worktree, base_sha, max_files=500) -> dict
    numstat + name-status + untracked listing + per-layer counts.

collect_file_diff(worktree, base_sha, path, allowed_paths, max_chars) -> dict
    tracked: git diff --no-color <base_sha> -- <path>
    untracked: git diff --no-index --no-color -- /dev/null <path>
```

**Command hygiene (every invocation):**

- argv list only, `cwd=<worktree>`, `shell=False`, `timeout=10`.
- env `GIT_OPTIONAL_LOCKS=0` so reads never take the index lock and cannot race
  an agent's own `git add`/`git commit`. `git diff`/`git status` refresh the
  index by default; this is the single most important concurrency control.
- `git --no-pager -c core.quotepath=false …`
- `--no-ext-diff --no-textconv` (a repo-configured external diff or textconv
  filter must never run from a dashboard/agent read path).
- `--no-color`; decode with `encoding="utf-8", errors="replace"`.
- `-z` for `ls-files` and `--name-status`; parse NUL-delimited, handling
  rename entries (`R<score>\0old\0new\0`). Test a path with spaces and a rename.

**Exclusions.** Pass `:(exclude)…` pathspecs built from
`_WORKTREE_LOG_EXCLUDES` to both the diff and the untracked listing.
`.gitignore`d paths are already excluded by git; the pathspecs cover repos whose
`.git/info/exclude` was never written (e.g. a test fixture).

**Untracked-file safety.** Before diffing an untracked entry: `lstat` it, skip
symlinks and non-regular files, and require the resolved path to stay under the
worktree root. `git diff --no-index` reads the filesystem, so a symlink is the
one way this path could read outside the repo. Also skip files above a size cap
(e.g. 1 MiB) and mark them `skipped: "too_large"`.

**Caps and failure modes.** `max_files` and `max_chars` bound both the number of
entries and each rendered diff. A diff over cap returns the first `max_chars`
with `truncated: true` and an explicit note; it is never silently cut. Binary
files are reported (`binary: true`, no text) using numstat's `-` marker. Every
git failure returns `available: false` with a reason enum —
`no_worktree`, `worktree_gone`, `not_a_worktree`, `no_base_ref`, `git_failed` —
never an exception and never a 500.

**Read-only guarantee:** collecting a snapshot twice must leave
`git status --porcelain` and `.git/index` unchanged; a test asserts this.

### 2.3 HTTP contract

`GET /api/plans/{plan_name}/stories/{story_key}/diff[?path=<repo-relative>]`

- No `path` → summary:

```json
{
  "available": true,
  "reason": null,
  "branch": "agent/pipe-123",
  "head_sha": "abc1234",
  "base_ref": "origin/master",
  "base_sha": "def5678",
  "files": [
    {"path": "pipeline/x.py", "status": "modified", "added": 12,
     "removed": 3, "binary": false, "untracked": false}
  ],
  "totals": {"files": 1, "added": 12, "removed": 3,
             "untracked": 2, "committed": 4, "working": 1},
  "truncated": false
}
```

- `?path=pipeline/x.py` → `{"available": true, "path": "...",
  "diff": "...", "truncated": false, "binary": false}`.
  `path` must be present in the summary's file list — containment alone is not
  enough, and this rejects `../`, absolute paths and `.git/**` by construction.
  A path not in the list is `400 invalid path`.

- Contract rules, mirroring the log/journal/checklist endpoints:
  - **404** only for an unknown plan or story key.
  - **200 + `available:false` + `reason`** for a never-dispatched story, a
    deleted worktree (post-merge), a non-git directory, no resolvable base, or a
    git failure. The UI renders an explanatory empty state.
  - The route is under `/api/`, so the existing API-key middleware covers it by
    construction. Add `Cache-Control: no-store`; never log diff content.

### 2.4 Service seam and route

- `PipelineService.get_story_diff(plan_name, story_key, path=None,
  max_chars=…)` in `pipeline/service.py` resolves the manifest/story, then calls
  the new module, passing `WORKTREE_ROOT` via `_ServerRef` (so the existing test
  fixtures that patch `pipeline.server.WORKTREE_ROOT` keep working). Returns
  `{"ok": false, "not_found": true}` for an unknown plan/story so the route can
  map it to 404.
- The handler lives in `app/dashboard_routes.py` (dashboard.py is at 998 lines;
  only a one-line registration is added there, beside the existing
  `app.get(...)(handler)` block).

### 2.5 Frontend

New module `static/app/render/story-diff.js`:

- `fetchStoryDiff(plan, key, path?)` — same API-key/header convention as
  `static/app/api.js`.
- `renderStoryDiffSummary(summary)` — totals line, per-layer counts, and a file
  list with status badges (`M`/`A`/`D`/`R`/`?`) and `+N −M`.
- `renderStoryDiffFile(file)` — delegates to `renderDiff` exported from
  `static/app/patch.js`; every path and line is escaped by that renderer.

Wiring in `story-modal.js`: a new `[data-diff-slot]` section rendered with the
other async slots. The summary loads when the modal opens; an individual file's
diff loads on click (cached in the slot for the modal's lifetime). A **Refresh**
button re-fetches the summary. Deliberately **no polling**: a snapshot is a
point-in-time view, and a git subprocess per poll per open modal is not free.
Guard stale responses with the existing `modal.dataset.plan/story` race pattern.

Empty states: "No changes yet.", "Worktree no longer available — merged or
cleaned up.", "Could not resolve the branch base.", "Diff too large — showing
the file list only."

### 2.6 Is this useful as an MCP tool?

**Yes — as a read-only tool, with the same caps and containment.**

Add `pipeline/server_tools_read.py` with a plain `get_story_diff(plan_name,
story_key, path=None, max_chars=100_000)` delegating to the same
`PipelineService` method, and register it in `pipeline/server.py` the way
`server_tools_lifecycle.py` is registered (`mcp.tool()(fn)`).

Why it earns its place:

- **Diagnosis from any Claude session.** The triage/overlord path currently has
  to shell out and reconstruct which worktree and base ref matter. A
  story-scoped tool returns exactly those plus the changes.
- **Chat is one wrapper away.** `app/chat.py` has its own `TOOLS` registry over
  HTTP; registering a `get_story_diff` entry there is ~10 lines and makes the
  same view available to the chat model when diagnosing a stuck story. (Not
  required for v1; note that the registry is cumulative — tests must assert
  membership, never the exact total.)
- **One implementation.** The dashboard and the tool share the service method,
  so there is no second diff path to keep correct.

Honest caveats: if the only consumer is a human with a shell, `git -C <worktree>
diff` is cheaper than any tool. The value comes from *story context* (which
worktree, which fork point, which exclusions) and from agents that cannot be
trusted to assemble that themselves. Output size must be capped, and the tool is
strictly read-only — it must never be extended with stage/commit/apply
operations, which already exist elsewhere under explicit gates.

### 2.7 Documentation

Add one bullet to `REFERENCE.md`'s Monitoring dashboard list, immediately before
the "Backend and escalated badges + filters" bullet:

> **Worktree diff snapshot.** Each story modal fetches
> `GET /api/plans/{plan}/stories/{key}/diff` and renders the worktree's changes
> relative to the branch's fork point — committed WIP, uncommitted edits, and
> untracked files — with per-file diffs on demand. Read-only; a merged story's
> worktree is gone and the section shows an empty state.

`REFERENCE.md` is 1,877 lines: use an anchored insert at that bullet, never a
line-number edit.

## 3. Implementation stories

All stories: TDD (failing test first), Conventional Commits, explicit `key`.
Dependency graph: `S1 → S2`, `S1 → S3a → S3b → S4a → S4b`, `S3a → S5`.

| # | Key | Story | Production files | Tests |
|---|---|---|---|---|
| S1 | `WTD-1` | Worktree/base resolution + diff **summary** | `pipeline/worktree_diff.py` (new) | `tests/unit/test_worktree_diff_summary.py` (real temp git repo) |
| S2 | `WTD-2` | Per-file diff (tracked + untracked, binary, truncation) | `pipeline/worktree_diff.py` | `tests/unit/test_worktree_diff_file.py` |
| S3a | `WTD-3` | `PipelineService.get_story_diff` seam | `pipeline/service.py` | `tests/unit/test_story_diff_service.py` |
| S3b | `WTD-4` | Dashboard route + registration | `app/dashboard_routes.py`, `app/dashboard.py` (1 line) | `tests/unit/test_dashboard_story_diff.py` |
| S4a | `WTD-5` | Export `renderDiff`; add `story-diff.js` | `static/app/patch.js`, `static/app/render/story-diff.js` (new) | `tests/unit/test_story_diff_render.mjs` |
| S4b | `WTD-6` | Wire the modal section + CSS + REFERENCE bullet | `static/app/render/story-modal.js`, `static/style.css`, `REFERENCE.md` | `tests/unit/test_story_diff_modal.mjs` |
| S5 | `WTD-7` | MCP tool `get_story_diff` | `pipeline/server_tools_read.py` (new), `pipeline/server.py` | `tests/unit/test_story_diff_mcp_tool.py` |

### S1 — summary collection

Build the temp-git-repo fixture: `git init`, a base commit, `git worktree add`
a branch, then modify/add/delete files and leave some uncommitted.

Acceptance (all mechanically checkable):

- A modified tracked file, an added tracked file, a deleted tracked file and an
  untracked file each appear exactly once with correct `status`.
- `agent.log`, `agent.log.ts`, `.agent_scratchpad.md`, `.agent_plan.md` and
  `.dispatch_baseline_test_checked` never appear (excluded by pathspec even
  when `.git/info/exclude` is absent).
- A worktree path outside `WORKTREE_ROOT` → `available:false`.
- A missing worktree directory → `reason:"worktree_gone"`; a plain directory
  with no `.git` → `reason:"not_a_worktree"`.
- A repo with no resolvable base candidate → `reason:"no_base_ref"`.
- Running the collector twice does not change `git status --porcelain` and does
  not modify `.git/index` (read-only guarantee).
- Renamed file and a path containing a space parse correctly (`-z` parser).
- `max_files` reached → `truncated:true`.

### S2 — per-file diff

- Tracked modified file → exactly that file's hunks, no other path.
- Untracked text file → a synthesized addition diff.
- `path="../../etc/passwd"`, an absolute path, `.git/config`, or any path not
  in `allowed_paths` → `ok:false` / `400`; no filesystem read occurs.
- Untracked symlink → skipped, not followed.
- Binary file → `binary:true`, `diff:""`.
- Diff over `max_chars` → `truncated:true` and a prefix, never a silent cut.
- Deleted file → `binary:false`, a valid deletion diff.

### S3a — service seam

- Returns `not_found` for an unknown plan and for an unknown story key.
- Delegates `worktree_root` from `pipeline.server.WORKTREE_ROOT` (patch it in a
  fixture and assert the patched root is the one used).
- No plan/story mutation: manifest bytes unchanged after the call.

### S3b — route

- 200 summary shape for a real worktree; 200 + `available:false` for a story
  with no worktree; 404 for unknown plan/story.
- `?path=` present in the summary returns that file; an absent path → 400.
- API-key middleware still applies (a request without the key → 401/403 per the
  existing contract).
- `Cache-Control: no-store` on the response.
- The route is registered before the catch-all `StaticFiles` mount.

### S4a / S4b — frontend

- `renderStoryDiffSummary` escapes a malicious path
  (`<img src=x onerror=…>` renders as text).
- Totals, per-layer counts and status badges render for a fixture summary.
- Empty states render for each `reason`.
- File click fetches once, renders via `renderDiff`, and is inert when stale
  (a different story opened in the meantime).
- No polling: a test advances fake timers and asserts no second summary request.

### S5 — MCP tool

- Registered: fetch it through the MCP tool manager (not just the module
  attribute) and assert the registered callable is the tool function.
- Delegates to the service method; `path` passes through; cap is clamped.
- Returns the same `available:false` reasons; never raises on a missing
  worktree.
- **Line gate:** `pipeline/server.py` is exactly 1,000 lines, the blocking
  ceiling in `scripts/check_line_limit.py` (allowlist empty). The story must
  leave it ≤1,000: add the two registration lines and remove at least two lines
  of non-semantic content (e.g. compress the lifecycle registration comments),
  or move the registration into a 3-line loop. Run
  `python scripts/check_line_limit.py` in the story's own verification and paste
  the result into the PR.

## 4. Preflight and tier notes

### 4.1 Dispatch tier (to confirm before ingest)

The live registry (`PIPELINE_MODEL_REGISTRY_PATH`) resolves
`roles.dispatch` = `ollama` / `deepseek-v4.1-flash` (`:cloud`), and
`PIPELINE_BACKEND_DISPATCH` is unset. That is **cloud open-source**, treated as
Claude-class for story splitting, but every mechanically-checkable requirement
above is graded per `pipeline-story-schema.md`. Confirm or override this before
ingesting; if it is re-pinned to a local ~20B tier, split S1/S2's git-fixture
tests into their own stories (fixture-heavy tests are a known weak-tier failure
mode) and keep every story at ≤2 production files.

### 4.2 Impact-run risks (run the full suite, don't grep)

Each story needs the standard scratch-worktree impact run. Specific known
collision shapes in this repo:

- `tests/unit/test_dashboard_no_direct_plan_dir.py` — new dashboard code must
  not read `PLAN_DIR` directly; route worktree access through the service.
- Source-text assertions over the story modal and patch UI
  (`test_dashboard_frontend.py`, `test_patch_ui_wiring.mjs`,
  `test_dashboard_comms_*`) — inserting a section can break a pinned string or
  ordering assertion. Pre-authorize any reconciling edit with literal
  before/after text.
- `scripts/check_line_limit.py` — `pipeline/server.py` at 1,000 (S5) and
  `app/dashboard.py` at 998 (S3b adds one line; still under).
- `tests/unit/test_dashboard_api.py` and checklist tests pin exact response
  shapes for adjacent endpoints — do not alter existing endpoints.
- The acceptance-fixture and cumulative-registry rules: the chat `TOOLS` registry
  (if the optional follow-up lands) is cumulative; tests assert membership only.

### 4.3 Operational preconditions

- Every story in the plan JSON has an explicit `key` (avoid duplicate ingestion).
- Any story brief carries a `Preflight:` one-line summary naming the impact run
  and date.
- Any hand-authored acceptance fixture is dry-run through both `pytest` and
  `ruff` before ingest.

## 5. Risks and open questions

1. **A live agent mutates the worktree while we read it.** `GIT_OPTIONAL_LOCKS=0`
   prevents index-lock races; the snapshot is still eventually consistent (it can
   include a half-written file). That is acceptable for a view explicitly
   labelled a snapshot; say so in the UI.
2. **Untracked symlinks.** Handled by lstat + containment; a test pins it.
3. **Size.** A generated or vendored file can dominate the diff. Caps per file
   and per summary keep the response bounded; the summary always remains
   available even when every diff is truncated.
4. **Post-merge visibility.** The worktree and branches are deleted on merge, so
   the snapshot disappears exactly when a human might want to review it. If that
   matters, a follow-up can persist the final snapshot (`.diff.json` under
   `PLAN_DIR`) during `approve_merge`/`mark_story_done`. That adds a write path
   and is deliberately out of scope here.
5. **Base-ref ambiguity.** If the default branch moved and the worktree's remote
   refs are stale, `merge-base` still gives the fork point, which is the intended
   semantic; recording `base_sha` at dispatch is a possible future hardening, not
   needed for v1.
6. **Should the summary auto-refresh?** No in v1. Manual Refresh only; polling
   is a follow-up if the view proves useful enough to pay for it.

## 6. Out of scope

- Any write operation (stage, commit, discard, apply).
- Persisting snapshots after worktree cleanup (follow-up, see §5.4).
- A chat `TOOLS` entry (small follow-up; described in §2.6).
- Replacing the reviewer's inline-diff path; this is a human/agent *view*, not a
  review gate.

## 7. Definition of Done

- All stories' new and existing tests pass; `ruff check .` clean; line-limit
  script clean (especially `pipeline/server.py`).
- Negative and boundary tests exist for every failure reason, path rejection,
  binary handling and truncation.
- `REFERENCE.md`'s dashboard list documents the endpoint; the endpoint contract
  above is implemented exactly (the plan is the contract for this feature).
- Each story merged through the pipeline's review gate, with the worktree diff
  verified by hand once against a real in-progress story (the "run it for real"
  check: open the modal, confirm the file list matches `git -C <worktree> status`
  and at least one per-file diff renders).
