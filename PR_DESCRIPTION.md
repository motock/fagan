# PR: feat(backend): isolate `claude -p` completion subprocesses from the pipeline's own MCP servers with `--strict-mcp-config`

Issue: 954043b9-b445-41d6-870d-23bda8f0fb6e

## What changed

`ClaudeCliDriver.complete()` in `app/backend_claude.py` now appends
`--strict-mcp-config` to the argv it builds, immediately after the base
`cmd = ["claude", "-p", prompt, "--model", model]` line and **before** any
role-conditional append (`--bare`, `--append-system-prompt`, `--allowedTools`,
`--output-format json`). The flag is unconditional — it is not inside any
`if allowed_tools` / role branch — so every `complete()` call (chat, planner,
overlord, review, rebrief, decompose) gets it.

Per `claude --help`: *"--strict-mcp-config: Only use MCP servers from
--mcp-config, ignoring all other MCP configurations."* `complete()` never
passes `--mcp-config`, so the single-shot completion subprocess now sees
**zero** MCP servers, regardless of what the invoking user's global
`~/.claude.json` configures. This closes the least-privilege gap where a
headless completion subprocess could recursively see this repo's own
pipeline-control MCP tools (`dispatch_story`, `ingest_plan`, `approve_merge`,
…).

## Existing-test assertion changes (justification per repo rule)

**None. No pre-existing test assertion was modified.** The only test change on
this branch is the *new* file `tests/unit/test_backend_claude_strict_mcp_config.py`
(249 insertions, commit `ca5f337`, authored by the tech-lead test dispatch).
Verified mechanically:

- `git diff ca5f337~1 HEAD -- tests/` → exactly one file changed: the new test
  file. `git status` shows no modified `test_*.py` in the working tree.
- Every pre-existing assertion that touches the `complete()` argv is
  membership- or index-based and therefore tolerant of the added token:
  - `tests/unit/test_backend_resource_status.py:498,515,529` —
    `assert "--bare" in captured["cmd"]` / `not in` (membership; unaffected).
  - `tests/unit/test_backend_claude_driver_misc.py` role tests — assert on
    individual elements via `cmd.index(...)` extraction, not exact argv
    equality (unaffected).
  - `tests/unit/test_always_on_planner_fallopen_negatives.py:358` —
    `cmd.index("-p")` prompt extraction; its docstring's
    `cmd == ["claude", "-p", prompt, "--model", model, ...]` is explicitly
    open-ended (`...`) (unaffected).
  - `tests/unit/test_chat_bare_passthrough.py` — membership assertions
    (unaffected).
- The only exact-equality argv assertions in the suite
  (`tests/unit/test_detect_lint_command.py:45`,
  `tests/unit/test_execution_ssh.py:67`,
  `tests/unit/test_mcp_self_modification.py:169,188`) pin `git`/`ruff`
  commands, not `claude` completion argv.

Had any pre-existing test pinned the *exact* full argv, it would have been
updated here as a legitimate, requested behavior change per this story — but
none did.

## New tests (committed by the test dispatch; all now green)

15 tests in `tests/unit/test_backend_claude_strict_mcp_config.py`, using the
repo's existing subprocess-fake pattern (`_FakeCompletedProcess` +
monkeypatched `app.backend.subprocess.run`; no real `claude` process spawned):

1. `test_complete_plain_chat_shape_includes_strict_mcp_config` — plain
   `complete()` (no allowed_tools/cwd, the chat shape) carries the flag.
2. `test_complete_review_shape_includes_strict_mcp_config_unconditionally` —
   the review shape (`allowed_tools` + `cwd`) ALSO carries it, proving the
   flag is unconditional, not role-conditional.
3. `test_strict_mcp_config_present_in_every_complete_shape` — parametrized
   over all 6 kwarg shapes (chat-plain, chat-bare, chat-system,
   max-tokens-accepted-ignored, review-tools-cwd, planner-cell-dir-structured).
4. `test_strict_mcp_config_appears_exactly_once_in_argv` — no duplication.
5. `test_strict_mcp_config_is_a_standalone_argv_element` — standalone token,
   not `--strict-mcp-config=...`.
6. `test_strict_mcp_config_present_on_structured_cell_dir_path` — survives the
   `--output-format json` rewrite; structured extraction unchanged.
7. `test_complete_empty_prompt_still_carries_flag` — boundary: empty prompt.
8. `test_complete_empty_allowed_tools_omits_allowedtools_but_keeps_flag` —
   boundary: `allowed_tools=""` still omits `--allowedTools` (pre-existing
   behavior) while keeping the flag.
9. `test_complete_explicit_cwd_none_still_carries_flag` — `cwd=None` forwards
   as `None` and does not suppress the flag.
10. `test_complete_missing_required_model_kwarg_raises_typeerror` — malformed
    call raises TypeError before any subprocess.
11. `test_flag_literal_is_in_complete_method_argv_construction` — read-the-file
    guard: the literal sits inside `complete()`'s argv construction, before
    `subprocess.run(`.

## Call-site audit (no MCP dependency found anywhere)

All `Backend.complete()` call sites were enumerated (grep over
`app/`, `pipeline/`, `scripts/`, `agents/`, non-test) and checked for
dependence on an MCP server being visible during that specific call. The only
`allowedTools` values passed anywhere are built-in CLI tools
(`"Read"`, `"Bash,Read"`); there are **zero** `mcp__*` tool names in the repo
and no call site passes `--mcp-config`.

| Call site | Shape | Verdict |
|---|---|---|
| `pipeline/planner.py:308` | planner: system+model+cell_dir | no MCP dep (plan-JSON authoring) |
| `pipeline/planner.py:407` | rework_planner: system+model+cell_dir | no MCP dep |
| `pipeline/planner.py:442` | rework needs-new-test: system+model | no MCP dep (YES/NO text) |
| `pipeline/planner.py:547` | decompose: system+model+allowed_tools="Read" | no MCP dep (built-in Read only) |
| `pipeline/review.py:363` | reviewer: allowed_tools="Bash,Read", cwd, cell_dir | no MCP dep (verdict from worktree Bash/Read) |
| `pipeline/review.py:422` | security_reviewer: same shape | no MCP dep |
| `pipeline/rebrief.py:434` | diagnosis: prompt/system=None/model | no MCP dep (text diagnosis) |
| `pipeline/rebrief.py:446` | diagnosis fallback: same | no MCP dep |
| `pipeline/overlord.py:54` | overlord: allowed_tools="Read" | no MCP dep (decision text from repo Read) |
| `app/chat.py:461` | chat loop: system+model+cwd=tmp | no MCP dep — chat parses tool calls from the response *text* and executes them itself via `_execute_tool` (HTTP to the pipeline server); it never relies on the subprocess having MCP servers. Read-only inspection; `app/chat.py` was NOT modified (separate story). |

Verdict: no call site depends on MCP visibility → the flag was applied
unconditionally with no per-site bypass, and no `request_decision` escalation
was needed.

## Test results

- Backend subsets: 275 passed (strict-mcp-config + driver-misc +
  resource-status + review-loop + role-routing + tuning-knobs +
  local-provider-dispatch + venv-path-reorg + acceptance + chat-bare +
  planner-fallopen).
- Full suite: **8034 passed, 7 skipped** (`.venv/bin/pytest -q`), zero
  failures.

## Scope guard

- Implementation confined to `app/backend_claude.py` (7 insertions).
- No `test_*.py` modified; `app/chat.py` untouched.