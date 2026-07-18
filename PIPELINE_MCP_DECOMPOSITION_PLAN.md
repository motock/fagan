# `pipeline_mcp_server.py` Decomposition Plan

**Origin:** 2026-07-18 review. `pipeline_mcp_server.py` is 4,985 lines in one file with ~143 top-level defs/classes and a module-level `FastMCP` instance plus ~30 env-var-driven config constants. The test suite (`test_pipeline_mcp_server.py`, 12,107 lines, 1,138 tests) imports the whole module as `p` and patches attributes on it directly, so any split must preserve `pipeline_mcp_server` as the public surface the tests already see.

**Goal:** break the monolith into cohesive submodules without changing the public API the MCP tools expose or the shape the test suite imports. Keep all 1,138 tests green at every step.

**Non-goals:** rewriting logic, changing tool behavior, renaming MCP tools, refactoring the test suite beyond `import` adjustments. This is a pure *move*, not a refactor.

---

## 1. Constraints and ground rules

1. **`pipeline_mcp_server` stays the entry point.** `python pipeline_mcp_server.py` must still launch the server; `from pipeline_mcp_server import *` (and the tests' `import pipeline_mcp_server as p`) must keep resolving every name they currently resolve. The decomposition is internal.
2. **The `@mcp.tool()`-decorated functions stay in `pipeline_mcp_server.py`.** FastMCP registration is path-sensitive (decorator side effect on the module-level `mcp` instance); keeping the tool definitions in the server module means the MCP surface never moves. What moves is their *helpers* and the non-tool business logic.
3. **One PR per module extraction, in dependency order.** Each PR: (a) creates the new submodule, (b) moves code verbatim, (c) re-exports it from `pipeline_mcp_server` (`from .submod import *` or explicit re-export), (d) runs the full suite. No mixed-concern PRs. A 4,985-line file moved in one shot is unreviewable.
4. **Tests are the regression oracle.** The existing 1,138 tests must stay green after every PR. New tests are *not* required for the move itself — the move has no behavior. If a helper's tests currently live in `test_pipeline_mcp_server.py` and patch `p.<helper>`, keep them patching `p.<helper>` via re-export; do not relocate tests in the same PR as the code move.
5. **Module-level config constants stay in `pipeline_mcp_server.py`** unless a submodule clearly owns them (e.g. `PLANE_*` moves to `pipeline_ticketing.py`). The constants are read at import time by env vars; scattering them risks subtle ordering bugs. When a submodule needs a constant, pass it in or import it from the server module — do not re-read the env var in two places.
6. **No new abstractions.** If the move exposes a duplication or a cleaner seam, note it in the PR description but do not fix it in the same PR. One concern per diff.
7. **Respect the existing `# ---------- Section ----------` markers.** They already name the natural seams; the module boundaries below follow them.

---

## 2. Proposed module layout

All new modules live alongside `pipeline_mcp_server.py` (flat package, no `src/` move). The test import surface (`import pipeline_mcp_server as p`) is preserved by re-export.

```
pipeline_mcp_server.py          # entry point: mcp = FastMCP("pipeline"), @mcp.tool defs,
                                # config constants, and `from .pipeline_* import *`
pipeline_config.py              # env-var-driven constants + _RISK_ORDER + _LOCAL_BACKEND_NAMES
pipeline_paths.py               # PLAN_DIR / WORKTREE_ROOT / AGENTS_DIR helpers, _repo_root_for,
                                # _scoped_repo_root, _default_branch, _exclude_worktree_logs_from_tracking
pipeline_ticketing.py           # TicketProvider, NullTicketProvider, PlaneTicketProvider,
                                # JiraTicketProvider, get_ticket_provider, LogicalState,
                                # _PLANE_STATE_GROUP, _TICKET_PROVIDERS, _plane_*, plane_request,
                                # _resolve_issue_uuid, _get_or_create_label, _get_state,
                                # _plane_set_state, _mark_plane_done, PLANE_* constants
pipeline_build_detect.py        # _venv_python_for, _test_command_for, _build_command_for,
                                # detect_build_command, detect_test_command,
                                # _is_pytest_cmd, _scope_test_cmd_to_acceptance,
                                # _acceptance_rel_paths
pipeline_persona.py             # _persona_path, _persona_body, _persona_default_model,
                                # _allowed_tools_for, _build_dispatch_command
pipeline_overlord.py             # _load_policy, _invoke_overlord, _parse_ruling
pipeline_planner.py              # _planner_system, _resolve_planner_backend, _run_planner,
                                # _run_rework_planner, _extract_json_block, _run_decompose
pipeline_review.py              # _run_reviewer, _run_security_reviewer, _parse_verdict,
                                # _has_review_findings, _is_rate_limited, _is_transient_backend_error
pipeline_pr.py                  # _open_pr, _merge_decision, _merge_pr
pipeline_rebase.py              # _parse_conflict_blocks, _resolve_conflict_blocks,
                                # _git_show_stage, _is_pure_additive_import_diff,
                                # _try_auto_resolve_conflict, _rebase_onto_master
pipeline_ci.py                  # _repo_has_ci_configured, _ci_status, _ci_rerun,
                                # _reverify_acceptance, _reverify_build
pipeline_escalation.py           # _escalate_to_claude, _escalate_to_local_fallback_model,
                                # _escalate_review_to_claude, _auto_escalation_enabled
pipeline_persistence.py          # _atomic_write_json, _validate_key, _notify_user,
                                # _decisions_path, _append_decision, _journal_path,
                                # _append_journal, _read_journal, _plan_role_config
pipeline_usage.py                # _parse_usage_output, _run_usage_probe, _write_usage_state,
                                # _read_usage_state, _usage_state_age_seconds, _usage_gate,
                                # _persona_requires_claude, _route_dispatch_backend,
                                # _role_resource_ok
pipeline_concurrency.py          # _count_in_progress_agents, _reap_zombie_in_progress_stories,
                                # _plan_lock, _held_plan_locks, _heavy_lock, _is_heavy
pipeline_git_ops.py              # _last_nonempty_line, _commit_wip, _worktree_has_new_commits
pipeline_advance.py              # _advance_pipeline_locked, _set_plan_paused,
                                # _completed_dep_ids
                                # (advance_pipeline, approve_merge, pause_plan, resume_plan,
                                # advance_all_plans stay in pipeline_mcp_server.py as @mcp.tool)
```

**What stays in `pipeline_mcp_server.py`:** the module docstring + env-var header comment, `mcp = FastMCP("pipeline")`, all `@mcp.tool()` definitions, all config constants that don't have an obvious owning submodule, the `if __name__ == "__main__"` block, and `from .pipeline_* import *` re-exports. Estimated residual size: ~1,500–1,800 lines (the tool bodies themselves plus the config block). That is still large but is a single coherent concern — the MCP surface.

---

## 3. Extraction order (one PR per step)

Each step is independently mergeable and leaves the full suite green. Order is bottom-up: leaf modules (no intra-server deps) first, then the modules that import them, then the server thinning last.

| Step | Module | Lines (approx) | Depends on | Notes |
|---|---|---|---|---|
| 1 | `pipeline_config.py` | ~120 | none | Pure env-var reads + `_RISK_ORDER`, `_LOCAL_BACKEND_NAMES`, `_LOCAL_SKIP_PERSONAS`, step-cap markers. Other modules import from here instead of re-reading env vars. |
| 2 | `pipeline_paths.py` | ~80 | `pipeline_config` | `PLAN_DIR`, `WORKTREE_ROOT`, repo-root helpers. Tests patch `p.PLAN_DIR` etc. — re-export keeps that working. |
| 3 | `pipeline_ticketing.py` | ~380 | `pipeline_paths` | Biggest single concern after the tools. Absorbs `PLANE_*` constants, `LogicalState`, the three provider classes, `get_ticket_provider`, and all `_plane_*` helpers. The `TICKETING_ABSTRACTION_PLAN.md` already scoped this; this PR is the physical move that doc described. |
| 4 | `pipeline_build_detect.py` | ~140 | `pipeline_config` | Test/build command detection. Pure helpers, heavily tested — good early confidence builder. |
| 5 | `pipeline_persona.py` | ~110 | `pipeline_config`, `pipeline_paths` | Persona file reads + dispatch command builder. |
| 6 | `pipeline_persistence.py` | ~80 | `pipeline_paths` | Atomic JSON writes, decisions/journal/notify. |
| 7 | `pipeline_usage.py` | ~140 | `pipeline_config`, `backend`, `role_registry` | Usage probe + gate + resource routing. |
| 8 | `pipeline_concurrency.py` | ~140 | `pipeline_paths` | In-progress accounting, zombie reaper, plan/heavy locks. |
| 9 | `pipeline_git_ops.py` | ~60 | `pipeline_paths` | `commit_wip`, `worktree_has_new_commits`, `_last_nonempty_line`. |
| 10 | `pipeline_overlord.py` | ~110 | `backend`, `role_registry` | Policy load + invoke + ruling parse. |
| 11 | `pipeline_planner.py` | ~200 | `pipeline_overlord`, `backend`, `role_registry` | Planner/decompose entry points. |
| 12 | `pipeline_review.py` | ~230 | `backend`, `role_registry` | Reviewer + security reviewer + verdict parsing + transient-error classification. |
| 13 | `pipeline_pr.py` | ~120 | `pipeline_paths` | PR open/merge decision. |
| 14 | `pipeline_rebase.py` | ~170 | `pipeline_git_ops` | Conflict parsing/auto-resolve + rebase. |
| 15 | `pipeline_ci.py` | ~160 | `pipeline_paths` | CI status/rerun + acceptance reverify. |
| 16 | `pipeline_escalation.py` | ~100 | `pipeline_review`, `backend` | Escalation paths. |
| 17 | `pipeline_advance.py` | ~430 | all of the above | `_advance_pipeline_locked` is the largest non-tool function in the file (~380 lines) and pulls from nearly every other concern. Extract last, when all its dependencies are already in their own modules. |

Steps 1–9 are leaf-ish and can largely be done in parallel; steps 10–17 have intra-server deps and should be sequential in the listed order. Steps 1–16 are mechanical; step 17 (`pipeline_advance.py`) is the only one with real risk because `_advance_pipeline_locked` is the orchestrator's heart — budget extra review there.

---

## 4. Re-export strategy

After each step, `pipeline_mcp_server.py` gains a line:

```python
from pipeline_ticketing import *  # noqa: F401,F403
```

(plus explicit names where `import *` is blocked by `__all__` absence — prefer defining `__all__` in each new submodule so `import *` is deterministic).

This keeps:
- `import pipeline_mcp_server as p` working unchanged in tests.
- `p.detect_test_command(...)`, `p._invoke_overlord(...)`, etc. resolving to the moved code.
- Monkeypatching (`monkeypatch.setattr(p, "detect_test_command", fake)`) still landing on the re-exported reference — **caveat below**.

### Monkeypatch caveat

`monkeypatch.setattr(p, "detect_test_command", fake)` patches the *attribute on `pipeline_mcp_server`*, not the binding inside `pipeline_build_detect`. If the moved function is called *from inside another submodule* via its original module reference (e.g. `dispatch_story` in the server calls `pipeline_build_detect.detect_test_command(...)` after a refactor), the patch won't take effect. Two safe options:

- **Option A (default, zero behavior change):** keep call sites in the server module referencing the re-exported name (`detect_test_command(...)`), not `pipeline_build_detect.detect_test_command(...)`. The server's symbol is the re-export, so patches land. This costs nothing and preserves the existing test shape.
- **Option B (later, optional):** after all moves land and tests are stable, switch call sites to fully-qualified `pipeline_build_detect.detect_test_command(...)`, and in the same PR update the affected tests to patch the submodule. Out of scope for this plan; track as a follow-up.

**The move plan uses Option A.** No call-site rewrites, no test rewrites, just moves + re-exports.

---

## 5. Per-step verification

Every PR in §3 must pass, in order:

1. `.venv/bin/python -m pytest -q` — all 1,138 tests green.
2. `.venv/bin/ruff check pipeline_mcp_server.py <new_module>.py` — no new lint findings.
3. `git diff --stat` shows only the new file + the re-export line added to `pipeline_mcp_server.py` + the moved code deleted from `pipeline_mcp_server.py`. Net line count change should be ≈ +re-export-line. A diff that adds net lines means code was duplicated, not moved — reject.
4. `grep -nE "^def |^class " <new_module>.py` matches the list in §2 for that module, and `grep -nE "^def |^class " pipeline_mcp_server.py` no longer contains those names (except re-exports and tool defs).

Step 17 adds:

5. Manual smoke: run `advance_all_plans` against an existing plan with a ready story and confirm the tick still works end-to-end. `_advance_pipeline_locked` is the one function where a bad move could silently break orchestration without a test catching it.

---

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Import cycle: `pipeline_advance` imports many submodules, and a submodule accidentally imports back from the server | Submodules must never `import pipeline_mcp_server`. They may import `backend`, `role_registry`, `inference_providers`, and each other. Add a `ruff` lint rule or a CI grep: `grep -nE "import pipeline_mcp_server" pipeline_*.py` must return nothing. |
| Module-level state initialized at import time (e.g. `_TICKET_PROVIDERS` dict, lock objects) | Keep that state in the submodule that owns it. The server module re-exports the *reference*, not a copy — mutation of `_TICKET_PROVIDERS` from inside a submodule still visible to `p._TICKET_PROVIDERS` consumers. Verified by writing a one-line test that asserts identity: `assert p._TICKET_PROVIDERS is pipeline_ticketing._TICKET_PROVIDERS`. |
| `mcp.tool()` decorator needs `mcp` in scope | Tool defs stay in `pipeline_mcp_server.py` — this is why §1 rule 2 exists. Helpers used only by tools stay in submodules and are imported. |
| A test patches an attribute that the moved code no longer reads from `p` | Option A in §4 keeps call sites on the re-exported name; verify with the existing test run. Any test that goes red is the signal. |
| The `# ---------- Section ----------` markers are load-bearing for navigation | Re-create equivalent section markers at the top of each new submodule's file header so `grep -n "# -----"` still finds the seams. |
| Step 17 breaks orchestration silently | §5 step 5 manual smoke + run an existing end-to-end plan (`audio-bugfixes` or similar) through one tick before merging. |

---

## 7. What this plan does NOT do

- Does not split `test_pipeline_mcp_server.py` (12k lines). Out of scope; the test file can stay monolithic and import from `pipeline_mcp_server` via re-export indefinitely. If a later effort splits tests by concern, it can mirror this module layout, but only after the code split is stable.
- Does not introduce a `pipeline/` package directory. Flat modules alongside the server are simpler and avoid a `src/`-layout migration. A package can be a follow-up if submodule count grows further.
- Does not change any env var, tool name, manifest schema, or file path. Pure internal relocation.
- Does not address the unrelated log clutter in the repo root (15+ `*.log` files) — separate cleanup.
- Does not address `dashboard.py` (807 lines) or `backend.py` (1,232 lines). Both are candidates for their own decomposition but have different seams; track separately.

---

## 8. Definition of Done

- [ ] All 17 submodules in §3 extracted, each in its own PR, each with the full suite green.
- [ ] `pipeline_mcp_server.py` is ≤ ~1,800 lines and contains only: config block, `mcp = FastMCP(...)`, `@mcp.tool()` defs, re-export lines, and `__main__`.
- [ ] `grep -nE "import pipeline_mcp_server" pipeline_*.py` returns nothing (no back-imports).
- [ ] 1,138 tests still green, unchanged.
- [ ] One follow-up issue opened for the "Option B" call-site rewrite (§4) — optional, not blocking.
- [ ] This plan doc updated with actual line counts after each PR lands, so progress is visible.

---

## 9. Progress log (executed 2026-07-18 on branch `refactor/decompose-pipeline-mcp-server`)

| Step | Module | Status | Commit | Notes |
|---|---|---|---|---|
| 1 | `pipeline_config.py` (238 lines) | done | `2fa7c09` | Scalar env-var knobs. |
| 2 | `pipeline_paths.py` (79 lines) | done | `99f734b` | Path constants + `_exclude_worktree_logs_from_tracking`. `REPO_ROOT` + the three functions that read it as a free var stay in the server (tests patch `p.REPO_ROOT`). |
| 4 | `pipeline_build_detect.py` (222 lines) | done | `316f1a0` | Test/build command detection + acceptance scoping. Pure leaves. |
| 9 | `pipeline_git_ops.py` (104 lines) | done | `a0b7830` | `_last_nonempty_line` / `_commit_wip` / `_worktree_has_new_commits`. Pure leaves. |
| — | `pipeline_parsers.py` (298 lines) | done | `87a3441` | Consolidated pure string/data parsers from steps 6/12/14: `_extract_json_block`, `_parse_ruling`, `_parse_verdict`, `_has_review_findings`, `_is_rate_limited`, `_is_transient_backend_error`, conflict-block parsers, `_atomic_write_json`, `_validate_key`, `_completed_dep_ids`, `_is_give_up_summary`. |

**`pipeline_mcp_server.py`:** 4,985 → 4,311 lines (−674, −13.5%). Five new modules, 941 lines extracted.

### Why the remaining steps (3, 5, 7, 8, 10–17) are deferred

The remaining planned extractions are blocked by a single pattern that the plan's §4 "Option B" follow-up was specifically written to defer: the functions in question read module-level globals (`PLAN_DIR`, `AGENTS_DIR`, `REPO_ROOT`, `DEFAULT_MODEL`, `PLANE_*`, `PIPELINE_AUTONOMY`, `PIPELINE_RISK_THRESHOLD`, `PIPELINE_MERGE_CI_GATE`, `USAGE_STATE_PATH`, `_RISK_ORDER`, etc.) as **free variables**, and the test suite patches those globals via `monkeypatch.setattr(p, "<global>", ...)`. Moving a function that reads `PLAN_DIR` as a free variable to a new module breaks the patch — the patch lands on `pipeline_mcp_server.PLAN_DIR` (the re-exported binding) while the moved function reads `pipeline_persistence.PLAN_DIR` (its own module's binding), and the two are no longer the same attribute.

Concretely:
- **Step 3 (ticketing):** `_plane_enabled`, `plane_request`, `_plane_set_state`, the provider classes, etc. all read `PLANE_API_KEY` / `PLANE_WORKSPACE` / `PLANE_PROJECT` / `PLANE_BASE` as free vars, and tests patch `p.PLANE_API_KEY` etc.
- **Step 5 (persona):** `_persona_path` reads `AGENTS_DIR`; tests patch `p.AGENTS_DIR`. `_build_dispatch_command` reads `DEFAULT_MODEL`.
- **Step 6 (persistence):** `_notify_user`, `_decisions_path`, `_journal_path`, `_plan_role_config` all read `PLAN_DIR` as a free var; tests patch `p.PLAN_DIR`.
- **Step 7 (usage):** `_write_usage_state` / `_read_usage_state` read `USAGE_STATE_PATH`; `_parse_usage_output` reads `DAILY_REQUEST_THRESHOLD` / `WEEKLY_REQUEST_THRESHOLD`; `_usage_gate` reads `SESSION_PAUSE_THRESHOLD` etc.
- **Step 10 (overlord):** `_load_policy` reads `POLICY_PATH` and `REPO_ROOT`.
- **Step 12 (review):** `_run_reviewer` reads `PIPELINE_REVIEW_MAX_TOKENS`.
- **Step 13 (pr):** `_merge_pr` reads `REPO_ROOT`.
- **Step 14 (rebase):** `_rebase_onto_master` reads `REPO_ROOT` and calls `_default_branch()`.
- **Step 15 (ci):** `_ci_status` reads `PIPELINE_MERGE_CI_GATE` / `PIPELINE_MERGE_CI_TIMEOUT`; tests patch `p.PIPELINE_MERGE_CI_GATE`.
- **Step 17 (advance):** the orchestrator's heart, reads many globals.

The §4 Option B fix — switching call sites to `pipeline_persistence.PLAN_DIR` and updating tests to patch `pipeline_persistence.PLAN_DIR` — is a cross-cutting test-suite rewrite that the plan deliberately kept out of scope for the no-behavior-change move. It is the right next step, but it should be its own focused PR series (one submodule per PR, with the test patches in the same PR so the intermediate state is internally consistent) rather than mixed into the mechanical-move PRs.

### Recommended next work

1. **Option B pilot:** pick one submodule where the gain is highest (recommend `pipeline_ticketing` — ~380 lines, the single biggest remaining concern, and `TICKETING_ABSTRACTION_PLAN.md` already scoped it). In one PR: move the helpers, switch their free-var reads to `pipeline_ticketing.<global>` reads, update the ~15 affected tests to patch `pipeline_ticketing.<global>` instead of `p.<global>`. Validate the pattern works end-to-end before repeating.
2. If the pilot is clean, repeat for `pipeline_persistence`, `pipeline_persona`, `pipeline_usage`, `pipeline_ci`, then the larger orchestration modules.
3. The mechanical moves already landed (steps 1, 2, 4, 9, and the consolidated `pipeline_parsers`) need no follow-up — they were pure leaves with no free-var coupling, and the re-export is the permanent interface.