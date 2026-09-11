"""Local dispatch agent loop with a harness-owned acceptance oracle.

The base `local_agent.py` grades a story done when (a) the model calls `done`
and (b) the worktree has a clean commit. That oracle is the model's own tests
plus a non-empty tree. A controlled experiment (see Local_LLM_Port_Plan.md and
the `tests/experiments/local_oracle/` rig) showed this routinely throws away
correct deliverables: devstral often produces a working implementation but
writes self-tests with wrong expected values, then loops on its own test
failures until the repetition guard parks it.

This variant owns the acceptance suite:
  - `LOCAL_AGENT_ACCEPTANCE` is a JSON list of paths the model must NOT
    touch (`create_file`/`str_replace` refuse on those paths with a
    recoverable error message).
  - After every code-changing tool, the harness runs the project's test
    command (detected via pipeline_mcp_server.detect_test_command — the
    same detector check_story_status uses) against the oracle files.
    First time it passes -> auto-commit -> exit 0. The model cannot
    author or modify the exam it's graded on.

Everything else (native /api/chat loop, tolerant parser, per-target
repetition guard, read-heavy-pattern guard, runtime-artifact exclusion) is
copied verbatim from scripts/local_agent.py so this is a faithful test of
just the one lever. PR #30 added the read-heavy guard to the base but
missed this variant — every story with an `acceptance` block dispatches
here, which is exactly the cohort that hits the "paralysis by analysis"
pattern most.

Exit codes:
  0 = oracle green, auto-committed, done
  1 = LLM call failed (no commit)
  2 = step cap reached, WIP-committed
  3 = repetition OR read-heavy guard fired, WIP-committed
"""
from __future__ import annotations

import json
import os
import re  # noqa: F401 (re-exported: the moved chat/repair impls use their own; kept for parity with the moved cluster's module context)
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Calibrated at runtime from ollama's own measured prompt_eval_count. Ported
# from local_agent.py - see that file's comment for the live incident
# (2026-07-29 ollama server log, ~2.35 chars/token measured vs the 4.0 guess)
# that motivated this. Keep both copies in sync.
_measured_chars_per_token: float | None = None
_last_prompt_eval_count: int | None = None

# The constant itself moved to scripts/local_agent_oracle_chat.py (LAO-CHAT);
# re-exported here as a one-line copy (same "keep both copies in sync"
# convention as the comments above) because tests still read it off this
# module. Deliberately NOT a module-level import of the twin: that would pin
# the twin in sys.modules and defeat the env-freshness eviction below.
_CHARS_PER_TOKEN_ESTIMATE = 4


def _effective_chars_per_token() -> float:
    from scripts.local_agent_oracle_chat import _effective_chars_per_token_impl
    return _effective_chars_per_token_impl(globals())


# The pipeline_mcp_server module lives in this script's parent directory.
# The agent subprocess runs with cwd=<worktree>, NOT the pipeline repo, so
# without this path insert the import below fails with ModuleNotFoundError
# and the harness dies before printing [boot] (which check_story_status
# reads as "process never reached main()" = a genuine failed launch).
# local_agent.py has the same line; the oracle variant didn't until PR #32
# because it didn't depend on pipeline_mcp_server.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import (
    inference_providers,  # noqa: F401 (re-exported: the moved chat impls read it via origin; tests patch provider dispatch through it)
)
from app import pipeline_mcp_server as p
from pipeline import edit_guards
from pipeline.local_agent_common import (
    CWD,
    PersistingList,
    _dropped_top_level_defs,
    _dropped_top_level_vars,
    _is_context_overflow_error,
    _load_resume_transcript,
    _message_char_len,  # noqa: F401 (re-exported: the moved calibration impls read it via origin)
    _persist_messages,
    _str_replace_not_found_diag,
    _total_chars,
    _trim_resumed_transcript,
    destructive_git_op,
)
from scripts.local_agent_oracle_guards import _no_tool_nudge
from scripts.local_agent_oracle_repair import (  # noqa: F401 (re-exported: run_tool references these as bare names)
    _SYNTAX_REJECT_COUNTS,
    _function_name_scopes,
    _lint_feedback_for,
    _newly_undefined_module_defs,
    _newly_undefined_module_vars,
    _newly_undefined_names,
    _python_syntax_error,
    _record_syntax_rejection,
    _try_repair_indentation,
    _var_drop_is_confirmed_loss,
)

# _answer_orphaned_calls, _DIGEST_MAX_CHARS, _dropped_span_digest,
# _evict_tool_outputs, _EVICT_HEAD_CHARS, _EVICT_KEEP_RECENT,
# _OVERFLOW_BODY_MARKERS, _repetition_nudge, _strip_eviction_marker,
# _TEST_COMMAND_MARKERS, _tool_call_pairs, _validate_message_list,
# _VALID_ROLES, _whitespace_visible, and DESTRUCTIVE_GIT_PATTERNS also moved
# to local_agent_common, but aren't imported here: nothing in this file (or
# its tests) calls them directly — they're only reachable as internal
# implementation details of other functions that already live in
# local_agent_common (e.g. _tool_call_pairs is only used inside
# local_agent_common's own _dropped_span_digest). Importing an unused name
# is a lint error (F401), so they're left off this list.
#
# NOTE: git, exclude_runtime_artifacts, worktree_dirty, _loads_tolerant, and
# recover_tool_calls are intentionally NOT imported from local_agent_common
# despite being byte-identical/behaviorally-identical there. Each reads a
# caller-supplied value (CWD for the first three; the local
# _repair_triple_quoted_strings callback for the last two) via its OWN
# module's globals, not the importing module's. A bare import would silently
# break two real things: (1) tests that monkeypatch THIS module's CWD to
# redirect git/exclude_runtime_artifacts/worktree_dirty at a tmp_path repo
# would instead operate on the real worktree, since the imported functions'
# CWD lookup resolves against pipeline.local_agent_common's own globals; (2)
# recover_tool_calls's triple-quote repair fallback (this module's own
# _repair_triple_quoted_strings, defined below) would stop firing in
# production, since the shared function's repair callback defaults to None
# and every call site here (including tests) invokes it with one argument.
# Kept local so each continues to close over this module's own globals.

# See scripts/local_agent.py's identical comment for why this eviction is
# needed: several tests exec this file fresh via importlib after mutating
# os.environ, expecting env-derived constants to recompute; without evicting
# the split-out config module first, a cached copy would serve stale values.
sys.modules.pop("scripts.local_agent_oracle_config", None)
# The chat/transport twin (LAO-CHAT) is imported at CALL time by the
# delegating wrappers below, so it lands in sys.modules on first use. Evict
# it here too, for the same env-freshness reason as the config module: a
# fresh exec of this file must not inherit a cached twin.
sys.modules.pop("scripts.local_agent_oracle_chat", None)
# LAO-RECOVERY: RECOVERY_BACKOFF_SECONDS/_RECOVERY_ROUNDS moved to
# scripts/local_agent_oracle_recovery.py (env read at that module's import),
# so a second fresh exec of this file must evict the cached recovery module
# too or the wrapper's lazy import would serve stale env-derived constants.
sys.modules.pop("scripts.local_agent_oracle_recovery", None)
from scripts.local_agent_oracle_config import (  # noqa: F401 (re-exported: the moved chat/transport impls read these via origin, and tests monkeypatch them on this module)
    _THINK_LEVELS,
    ACCEPTANCE_PATHS,
    BASH_TIMEOUT,
    CHAT_MAX_ATTEMPTS,
    CHAT_RETRY_BACKOFF,
    CONNECT_TIMEOUT_SECONDS,
    ENDPOINT,
    HARNESS_RULES,
    MAX_STEPS,
    MODEL,
    MUTATING_TOOLS,
    NET_PROGRESS_MAX_STEPS,
    NO_TOOL_CAP,
    NUM_CTX,
    PARK_ENABLED,
    PROACTIVE_TRIM_THRESHOLD,
    PROVIDER,
    READ_HEAVY_DISTINCT_WINDOWS,
    READ_HEAVY_WINDOW,
    READ_SILENCE_SECONDS,
    REWORK_FULL_SUITE,
    REWORK_SUITE_REJECT_CAP,
    TEMPERATURE,
    THINK,
    TIMEOUT,
    TOOLS,
)


def _stream_one_turn(payload):
    from scripts.local_agent_oracle_chat import _stream_one_turn_impl
    return _stream_one_turn_impl(globals(), payload)


def _provider_chat_turn(messages):
    from scripts.local_agent_oracle_chat import _provider_chat_turn_impl
    return _provider_chat_turn_impl(globals(), messages)


def chat(messages):
    from scripts.local_agent_oracle_chat import chat_impl
    return chat_impl(globals(), messages)


def _repair_triple_quoted_strings(candidate):
    from scripts.local_agent_oracle_chat import _repair_triple_quoted_strings_impl
    return _repair_triple_quoted_strings_impl(globals(), candidate)


def _loads_tolerant(candidate):
    from scripts.local_agent_oracle_chat import _loads_tolerant_impl
    return _loads_tolerant_impl(globals(), candidate)


def recover_tool_calls(content):
    from scripts.local_agent_oracle_chat import recover_tool_calls_impl
    return recover_tool_calls_impl(globals(), content)


def git(*args):
    from scripts.local_agent_oracle_git import git_impl
    return git_impl(globals(), *args)


def exclude_runtime_artifacts() -> None:
    """Keep dispatch runtime junk out of git."""
    from scripts.local_agent_oracle_git import exclude_runtime_artifacts_impl
    return exclude_runtime_artifacts_impl(globals())


def worktree_dirty() -> bool:
    from scripts.local_agent_oracle_git import worktree_dirty_impl
    return worktree_dirty_impl(globals())


def auto_commit(reason: str) -> None:
    from scripts.local_agent_oracle_git import auto_commit_impl
    return auto_commit_impl(globals(), reason)


def oracle_result() -> tuple[bool, str]:
    """Run the harness-owned acceptance suite. Returns (passed, tail).

    The suite is whatever test command fits the project — pytest for Python
    repos (with ACCEPTANCE_PATHS appended), cargo test for Rust, npm test for
    JS, make test where the Makefile defines one, etc. Detected via
    pipeline_mcp_server.detect_test_command so the oracle is language-agnostic
    (matches the same detector the orchestrator's check_story_status uses
    after the run finishes). On a non-zero exit (compile error, test
    failure, collection error) the tail is captured so the model can react.
    """
    if not ACCEPTANCE_PATHS:
        return True, "(no acceptance files configured)"
    test_dir, test_cmd = p.detect_test_command(CWD)
    if test_cmd and test_cmd[0] == "pytest":
        argv = [*test_cmd, *ACCEPTANCE_PATHS, "-q", "--no-header", "-p", "no:cacheprovider"]
    else:
        # FM-A: scope non-pytest runners to the acceptance fixtures too, so a
        # correct implementation isn't rejected because the implementer's OWN
        # test file has wrong assertions (observed: interval_merge_js wrote a
        # correct src/merge.js but a buggy merge.test.js; unscoped `npm test`
        # ran both and reported failure). _scope_test_cmd_to_acceptance scopes
        # cargo (--test <stem>) and npm/yarn node --test; runners it can't
        # safely scope fall back to the full suite (argv = test_cmd).
        scoped = p._scope_test_cmd_to_acceptance(
            test_cmd, ACCEPTANCE_PATHS, test_dir
        )
        argv = scoped if scoped is not None else test_cmd
    # Heavy-build lock: cargo/npm/gradle etc. share the same serialization
    # contract with check_story_status and the agent's own bash tool.
    needs_heavy = bool(argv) and p._is_heavy(argv)
    if needs_heavy:
        with p._heavy_lock():
            r = subprocess.run(argv, check=False, cwd=test_dir, capture_output=True, text=True)
    else:
        r = subprocess.run(argv, check=False, cwd=test_dir, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr)[-800:]


def _full_suite_result() -> tuple[bool, str, str | None]:
    """Gate-aware wrapper around the original _full_suite_result logic.
    Returns (passed, tail, gate)."""
    from scripts.local_agent_oracle_git import _full_suite_result_impl
    passed, tail, gate = _full_suite_result_impl(globals())
    return passed, tail, gate

_full_suite_result = _full_suite_result  # noqa: PLW0127


# Full-suite rejections recorded by finish_if_green. The `done` handler keeps
# its own local counter for the path the model drives; this one bounds the
# AUTOMATIC path, which fires after every mutating tool call and so can spin
# far faster. Module-level (not a main() local) because finish_if_green is a
# module function called from the loop and directly by tests.
_SUITE_REJECTIONS = 0


def _reset_suite_rejections() -> None:
    """Clear the automatic-path rejection count (suite went green, or a fresh
    run is starting)."""
    global _SUITE_REJECTIONS
    _SUITE_REJECTIONS = 0


def suite_reject_cap_reached() -> bool:
    """Whether finish_if_green has rejected `done` for a red full suite
    REWORK_SUITE_REJECT_CAP times in a row - the signal for main() to park
    instead of continuing to burn steps on an unwinnable done-bar."""
    return _SUITE_REJECTIONS >= REWORK_SUITE_REJECT_CAP


def finish_if_green(step: int, messages: list | None = None) -> bool:
    """If the oracle passes, auto-commit and end the run: the caller
    terminates the loop.

    On a CI-fail-rework round (REWORK_FULL_SUITE), oracle-green is necessary
    but no longer sufficient: the full worktree suite must ALSO be green before
    the loop may terminate. The acceptance oracle excludes the agent's own
    committed test file, so without this second gate a CI-fail rework would
    stop and commit while that test still fails and re-fail the merge gate on
    the same assertion every round. On a full-suite failure the failing
    excerpt is fed back into `messages` (a user turn) and False is returned so
    the loop keeps working the broken test until fixed or the step cap binds;
    no commit happens on a failure. Cold-start dispatches never set
    REWORK_FULL_SUITE, so their oracle-green done-bar is unchanged.
    """
    global _SUITE_REJECTIONS
    ok, _ = oracle_result()
    if not ok:
        return False
    if REWORK_FULL_SUITE:
        full_ok, full_tail, gate = _full_suite_result()
        if not full_ok:
            _SUITE_REJECTIONS += 1
            if suite_reject_cap_reached():
                # Unwinnable done-bar (e.g. a rework-authored test that
                # contradicts the read-only oracle). Stop re-prompting: WIP-
                # commit and let main() park, exactly as the `done` path does.
                if worktree_dirty():
                    auto_commit("wip: rework suite-reject cap")
                print(f"[step {step}] rework suite-reject cap "
                      f"({REWORK_SUITE_REJECT_CAP}) reached; agent cannot green "
                      f"the full suite — parking", flush=True)
                return False
            if messages is not None:
                messages.append({"role": "user", "content": (
    f"Your tests PASS, but the lint check (`ruff check .`) fails. The merge-gate CI lint gate will reject this on the same failure:\n{full_tail}\n\nMost lint errors are auto-fixable: run `ruff check . --fix`, then `ruff check .` to confirm it is clean.\n\nDo NOT edit implementation logic — this is a formatting/import/style error, not a correctness bug, and editing logic will not fix it. Do not call done until `ruff check .` passes in full."
)}) if gate == 'lint' else messages.append({"role": "user", "content": (
    f"The acceptance oracle passes but the full test suite still fails. The merge-gate CI will reject this on the same failure:\n{full_tail}\n\nThe bug could be in the implementation you just changed, or in a test file - do not assume either side is correct. Re-read the failing test and the code it exercises, identify which one is actually wrong, and make ONE targeted fix there. Do NOT call done until `pytest` passes in full."
)})
            print(f"[step {step}] ORACLE GREEN but full suite still fails - "
                  f"rework done-bar not met; continuing.", flush=True)
            return False
    _reset_suite_rejections()
    if worktree_dirty():
        auto_commit("feat: implement task (acceptance oracle green)")
    print(f"[step {step}] ORACLE GREEN — acceptance tests pass; committed & done.", flush=True)
    write_done_marker(0)
    return True


def is_oracle_path(path: str) -> bool:
    # Normalise: strip leading "./" and treat absolute paths the user wouldn't
    # use here as already disambiguated.
    norm = path.lstrip("./")
    return any(norm == p.lstrip("./") or norm.endswith("/" + p.lstrip("./"))
               for p in ACCEPTANCE_PATHS)


# is_oracle_path only guards create_file/str_replace (run_tool checks it
# directly for those two). It's a no-op for the bash tool, which can
# rm/overwrite/sed-in-place the acceptance file with no such check -- a model
# could dodge the guard entirely via shell. Arbitrary shell syntax can't be
# reliably pattern-matched (rm, mv, redirection, sed -i, a python one-liner,
# ... too many ways to write the same effect), so instead of trying to block
# the command text, snapshot the oracle files' real content up front and
# restore them after any bash call that changed them -- this can't be evaded
# by shell cleverness since it checks the actual bytes on disk.
_ORACLE_SNAPSHOT: dict[str, str | None] = {}


def _capture_oracle_snapshot() -> None:
    _ORACLE_SNAPSHOT.clear()
    for rel in ACCEPTANCE_PATHS:
        path = CWD / rel
        _ORACLE_SNAPSHOT[rel] = path.read_text() if path.exists() else None


def _restore_tampered_oracle_files() -> str:
    """Restore any acceptance path whose on-disk content no longer matches
    its snapshot. Returns a warning to append to the bash tool's result, or
    "" if nothing was tampered with."""
    restored = []
    for rel, original in _ORACLE_SNAPSHOT.items():
        path = CWD / rel
        current = path.read_text() if path.exists() else None
        if current != original:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(original)
            restored.append(rel)
    if not restored:
        return ""
    names = ", ".join(restored)
    return (f"\n\nWARNING: {names} is the read-only acceptance suite and was "
            f"restored after being modified via bash. It must NOT be edited "
            f"or deleted by any means, including shell commands. Change the "
            f"implementation file instead.")



# Paths successfully written via create_file THIS process run - lets the
# model overwrite a file it just wrote itself (full rewrite is often the
# only real recovery strategy for a weak model that can't construct a
# correct str_replace old_str) without weakening protection for
# pre-existing repo/seed files or a rework's inherited file. Ported verbatim
# from local_agent.py; keep both copies in sync.
_CREATED_THIS_RUN: set[str] = set()

# Companion to _CREATED_THIS_RUN for the RESUME/rework case: a pre-existing
# file the model has read via view_file THIS run becomes eligible for an
# informed whole-file overwrite, so a resumed run isn't forced onto str_replace
# it can't construct. Ported verbatim from local_agent.py; keep both copies in
# sync. See local_agent.py for the full rationale (interval_merge resume,
# 2026-07-16).
_VIEWED_THIS_RUN: set[str] = set()


def run_tool(fn, args) -> str:
    # Wiring contract (LAO-TOOLS): the replace_lines branch below-in-impl still
    # calls edit_guards.verify_range_anchors (with the "MUST stay optional"
    # comment) and edit_guards.duplicated_block_warning (advisory, not a
    # block, and never gated on a .py extension) — the call sites now live in
    # scripts/local_agent_oracle_tools.py::run_tool_impl, which this wrapper
    # delegates to with this module's live namespace.
    from scripts.local_agent_oracle_tools import run_tool_impl
    return run_tool_impl({
        "ACCEPTANCE_PATHS": ACCEPTANCE_PATHS,
        "BASH_TIMEOUT": BASH_TIMEOUT,
        "CWD": CWD,
        "_CREATED_THIS_RUN": _CREATED_THIS_RUN,
        "_VIEWED_THIS_RUN": _VIEWED_THIS_RUN,
        "_SYNTAX_REJECT_COUNTS": _SYNTAX_REJECT_COUNTS,
        "_python_syntax_error": _python_syntax_error,
        "_try_repair_indentation": _try_repair_indentation,
        "_record_syntax_rejection": _record_syntax_rejection,
        "_dropped_top_level_defs": _dropped_top_level_defs,
        "_dropped_top_level_vars": _dropped_top_level_vars,
        "_lint_feedback_for": _lint_feedback_for,
        "_str_replace_not_found_diag": _str_replace_not_found_diag,
        "_newly_undefined_names": _newly_undefined_names,
        "_var_drop_is_confirmed_loss": _var_drop_is_confirmed_loss,
        "_restore_tampered_oracle_files": _restore_tampered_oracle_files,
        "edit_guards": edit_guards,
        "destructive_git_op": destructive_git_op,
        "p": p,
        "is_oracle_path": is_oracle_path,
        "run_tool": run_tool,
        "safe_run_tool": safe_run_tool,
    }, fn, args)


def safe_run_tool(fn, args) -> str:
    """Run a tool, turning any exception into a recoverable error message.

    A model that omits a required argument (e.g. str_replace without old_str,
    observed with weaker local models) would otherwise raise an uncaught
    KeyError and crash the whole unattended agent. Feeding the error back as a
    tool result lets the model correct itself, bounded by the loop guard / step
    cap, instead of taking the run down.
    """
    from scripts.local_agent_oracle_tools import safe_run_tool_impl
    return safe_run_tool_impl({
        "run_tool": run_tool,
        "safe_run_tool": safe_run_tool,
    }, fn, args)


def recover_from_oversized_5xx(messages, chat_fn, *, step=None):
    """Recover from a backend error on an oversized transcript by retrying
    with an escalating (shrinking) context budget before giving up. A single
    trim-retry can also fail on a still-oversized payload, so shrink harder
    each round, and pause between rounds so a load-induced failure gets time
    to clear. Returns the assistant message dict on success, or None if every
    round fails (caller gives up). Mutates ``messages`` in place. Bounded:
    3 rounds.

    When the trim cannot shrink the payload the request is retried UNCHANGED
    rather than abandoned. An unshrinkable payload is positive evidence that
    the failure was not an overflow at all - which is exactly the case where
    waiting works. The old code returned None here, which is how a transient
    Ollama 500 killed a ~95%-complete converging run on 2026-07-30.

    Ported from local_agent.py - keep both copies in sync. The oracle runs the
    production path for acceptance-bearing dispatches (oracle_mode = bool(acceptance)
    in backend.py), so a transient 5xx killing a ~95%-complete converging run here
    (2026-07-30 LAUNCHD-PLIST-PORTABILITY) is the live failure this recovers from.
    """
    from scripts.local_agent_oracle_recovery import recover_from_oversized_5xx_impl

    return recover_from_oversized_5xx_impl(globals(), messages, chat_fn, step=step)


def _main_impl() -> int:
    # A fresh dispatch has no calibration data yet - clear any value left
    # over from a prior dispatch that shared this process (or, in-process, a
    # prior test). Ported from local_agent.py; keep both copies in sync.
    global _measured_chars_per_token, _last_prompt_eval_count
    _measured_chars_per_token = None
    _last_prompt_eval_count = None
    system = os.environ.get("LOCAL_AGENT_SYSTEM", "").strip()
    task = os.environ.get("LOCAL_AGENT_TASK", "")
    # Initialize messages list with optional persistence support.
    # Resume path: if LOCAL_AGENT_RESUME_TRANSCRIPT_PATH points at a valid
    # transcript, load it instead of building the fresh system/task pair (the
    # loaded transcript already contains the original system+task prompt).
    # Otherwise fall back to the fresh pair. LOCAL_AGENT_TRANSCRIPT_PATH
    # (persistence) is independent of resume — a dispatch can persist without
    # resuming, resume without persisting, or both. Ported from
    # local_agent.py; keep both copies in sync.
    transcript_path = os.environ.get("LOCAL_AGENT_TRANSCRIPT_PATH")
    messages = PersistingList(transcript_path=transcript_path)
    if resume := _load_resume_transcript():
        # No live calibration exists yet this early (reset above, before any
        # turn has run), so this always uses the fixed guess via
        # _effective_chars_per_token()'s fallback.
        budget_chars = int(NUM_CTX * _effective_chars_per_token() * 0.75)
        resume = _trim_resumed_transcript(resume, budget_chars)
        messages.extend(resume)
    else:
        system_content = HARNESS_RULES + ("\n\n" + system if system else "")
        messages.extend([{"role": "system", "content": system_content},
                         {"role": "user", "content": task}])
    # When resuming, optionally append exactly one new user turn as a
    # continuation (e.g. reviewer feedback) instead of re-injecting the
    # original system/task prompt.
    resume_append = os.environ.get("LOCAL_AGENT_RESUME_APPEND_CONTENT")
    if resume and resume_append:
        messages.append({"role": "user", "content": resume_append})

    if not ACCEPTANCE_PATHS:
        print("[local_agent_oracle] WARNING: no LOCAL_AGENT_ACCEPTANCE; "
              "this variant should not have been launched. Falling back to "
              "no-oracle behavior — done is honored on clean worktree + model `done`.", flush=True)
    else:
        _capture_oracle_snapshot()

    exclude_runtime_artifacts()

    seen: dict = {}
    nudged_repeat = False
    nudged_read_heavy = False
    recent_tools: deque[tuple[str, str]] = deque(maxlen=READ_HEAVY_WINDOW)
    distinct_windows = 0
    consecutive_no_tool = 0
    # Mirror local_agent.py: cap full-suite done-rejections on a rework round so
    # a model that cannot green the suite parks rather than burning the whole
    # budget alternating `done` with narration. See REWORK_SUITE_REJECT_CAP.
    suite_rejections = 0
    _reset_suite_rejections()
    # Failing-str_replace loop guard: str_replace is excluded from the
    # per-target repetition guard (each old_str differs), so a no-match loop
    # on one file runs uncaught. Track consecutive FAILED str_replace per
    # path; after 2, steer to replace_lines. Reset on a successful str_replace
    # to that path (and re-arm the nudge so a second stall is caught too).
    failed_sr: dict[str, int] = {}
    nudged_sr_fail: set[str] = set()
    last_progress_step = 0
    start_time = time.monotonic()

    for step in range(MAX_STEPS):
        if time.monotonic() - start_time > TIMEOUT:
            print(f"[step {step}] wall-clock timeout ({TIMEOUT}s) reached; parking", flush=True)
            if worktree_dirty():
                auto_commit("WIP (wall-clock timeout)")
            return 2
        if step - last_progress_step >= NET_PROGRESS_MAX_STEPS:
            print(f"[step {step}] no successful edit in {step - last_progress_step} steps "
                  f"(last progress at step {last_progress_step}); parking", flush=True)
            if worktree_dirty():
                auto_commit("WIP (no net progress)")
            if not PARK_ENABLED:
                messages.append({"role": "user", "content": (
                    f"You have made no successful file edit in the last "
                    f"{step - last_progress_step} steps. Stop investigating "
                    f"and make ONE concrete change now: str_replace/"
                    f"replace_lines to fix something specific, restore_file "
                    f"if a file's edits went wrong and you want to restart "
                    f"it clean, or checkpoint if you need to save partial "
                    f"progress before continuing.")})
                last_progress_step = step
            else:
                return 3
        try:
            m = chat(messages)
        except httpx.HTTPStatusError as e:
            if not _is_context_overflow_error(e):
                print(f"[step {step}] LLM call failed: {e}", flush=True)
                if worktree_dirty():
                    auto_commit("WIP (llm error)")
                return 1
            # Classified by BODY, not just status: LM Studio rejects an
            # oversized prompt with a 400, which the old `status_code < 500`
            # test treated as an unretryable bad request and died on - skipping
            # the trim escalation that is the actual remedy. Ollama/llama.cpp
            # 500s still route here exactly as before.
            # A 5xx that survived chat()'s own CHAT_MAX_ATTEMPTS retries is
            # very likely a context-window overflow (Ollama/llama.cpp returns
            # 500 rather than a clean 4xx for this), not a transient fault.
            # Escalate: retry with a shrinking context budget before giving
            # up - a single trim-retry can also 500 on a still-oversized
            # payload (confirmed live 2026-07-22: a ~190K char / ~47.6K
            # estimated-token transcript against a 32768-token NUM_CTX; and
            # 2026-07-30: a single trim-retry also 500'd, killing a ~95%-
            # complete converging run). Ported from local_agent.py - keep both
            # copies in sync.
            # The escalation helper only catches httpx.HTTPStatusError: a 4xx
            # is re-raised, and a non-HTTP backend failure (httpx.TransportError,
            # inference_providers.RateLimitedError, any other Exception chat()
            # can raise) propagates out of the helper. A sibling except clause of
            # this same try does NOT catch exceptions raised from inside another
            # except handler, so the except-Exception backstop below would NOT
            # catch what the helper lets escape - it would propagate out of
            # main() and kill the run. The original trim-retry path this replaced
            # wrapped its chat() in a try/except-Exception "must not crash the
            # agent loop" guard; restore that guard here so ANY failure during
            # escalation gives up gracefully (return 1) instead of crashing.
            try:
                m = recover_from_oversized_5xx(messages, chat, step=step)
            except Exception as e2:  # noqa: BLE001 (an LLM backend call during escalation can fail in unpredictable ways; must not crash the agent loop)
                print(f"[step {step}] LLM call failed during 5xx escalation: {e2}", flush=True)
                if worktree_dirty():
                    auto_commit("WIP (llm error)")
                return 1
            if m is None:
                print(f"[step {step}] LLM call failed after trim-retry: {e}", flush=True)
                if worktree_dirty():
                    auto_commit("WIP (llm error)")
                return 1
        except Exception as e:  # noqa: BLE001 (an LLM backend call can fail in unpredictable ways; must not crash the agent loop)
            print(f"[step {step}] LLM call failed: {e}", flush=True)
            if worktree_dirty():
                auto_commit("WIP (llm error)")
            return 1
        messages.append(m)
        # Proactive trim: once a turn's measured prompt_eval_count is already
        # close to NUM_CTX, shrink the transcript now rather than waiting for
        # the next turn to overflow and 500. Ported from local_agent.py; keep
        # both copies in sync.
        if (
            _last_prompt_eval_count is not None
            and _last_prompt_eval_count >= NUM_CTX * PROACTIVE_TRIM_THRESHOLD
        ):
            budget_chars = int(NUM_CTX * _effective_chars_per_token() * 0.75)
            trimmed = _trim_resumed_transcript(messages, budget_chars)
            if _total_chars(trimmed) < _total_chars(messages):
                print(f"[step {step}] measured prompt_eval_count="
                      f"{_last_prompt_eval_count} >= {PROACTIVE_TRIM_THRESHOLD:.0%} of "
                      f"NUM_CTX={NUM_CTX}; trimming proactively", flush=True)
                messages[:] = trimmed
                _persist_messages(messages, transcript_path)
                _last_prompt_eval_count = None
        tcs = m.get("tool_calls") or recover_tool_calls(m.get("content", ""))
        if not tcs:
            consecutive_no_tool += 1
            print(f"[step {step}] no tool call ({consecutive_no_tool} consecutive): "
                  f"{(m.get('content') or '')[:100]!r}", flush=True)
            if consecutive_no_tool >= NO_TOOL_CAP:
                print(f"[step {step}] narration cap ({NO_TOOL_CAP} consecutive "
                      f"no-tool turns) reached; parking", flush=True)
                if worktree_dirty():
                    auto_commit("WIP (narration cap)")
                return 2
            messages.append({"role": "user", "content": _no_tool_nudge(
                consecutive_no_tool, content=m.get("content", ""))})
            continue
        consecutive_no_tool = 0

        for tc in tcs:
            fn = tc["function"]["name"]
            args = tc["function"]["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:  # noqa: BLE001 (malformed tool-call args from the model fall back to empty args rather than crashing the loop)
                    args = {}

            if fn == "done":
                ok, tail = oracle_result()
                if ok:
                    # L1: on a CI-fail-rework round the model can dodge the
                    # raised done-bar that finish_if_green enforces by calling
                    # `done` directly (observed 2026-07-18: gpt-oss called done
                    # on round 3 with its own pasted pytest showing 3 failed,
                    # oracle green, done accepted - bypassed the gate). Close
                    # the bypass: require the full suite green here too, else
                    # feed the failing excerpt back and reject. Cold-start
                    # dispatchs skip this (REWORK_FULL_SUITE unset).
                    if REWORK_FULL_SUITE:
                        full_ok, full_tail, gate = _full_suite_result()
                        if not full_ok:
                            suite_rejections += 1
                            if suite_rejections >= REWORK_SUITE_REJECT_CAP:
                                if worktree_dirty():
                                    auto_commit("wip: rework suite-reject cap")
                                print(f"[step {step}] rework suite-reject cap "
                                      f"({REWORK_SUITE_REJECT_CAP}) reached; agent "
                                      f"cannot green the full suite — parking", flush=True)
                                return 2
                            if gate == 'lint':
                                print(f"[step {step}] done rejected — lint gate still "
                                      f"fails (rework done-bar); asking agent to fix the "
                                      f"lint failure", flush=True)
                                messages.append({"role": "user", "content": (
                                    f"Your tests PASS, but the lint check (`ruff check .`) fails. The merge-gate CI lint gate will reject this on the same failure:\n{full_tail}\n\nMost lint errors are auto-fixable: run `ruff check . --fix`, then `ruff check .` to confirm it is clean.\n\nDo NOT edit implementation logic — this is a formatting/import/style error, not a correctness bug, and editing logic will not fix it. Do not call done until `ruff check .` passes in full.")})
                            else:
                                print(f"[step {step}] done rejected — full test suite "
                                      f"still fails (rework done-bar); asking agent to "
                                      f"fix the failure", flush=True)
                                messages.append({"role": "user", "content": (
                                    "The acceptance oracle passes but the FULL test suite "
                                    f"still fails. The merge-gate CI will reject this on "
                                    f"the same failure:\n{full_tail}\n\nThe bug could be "
                                    "in the implementation you just changed, or in a test "
                                    "file - do not assume either side is correct. Re-read "
                                    "the failing test and the code it exercises, identify "
                                    "which one is actually wrong, and make ONE targeted "
                                    "fix there. Do NOT call done until `pytest` passes in "
                                    "full.")})
                            break
                    if worktree_dirty():
                        auto_commit("feat: implement task (acceptance oracle green)")
                    print(f"[step {step}] DONE (oracle green): {args.get('summary', '')}", flush=True)
                    return 0
                print(f"[step {step}] done rejected — acceptance tests still failing", flush=True)
                oracle_list = ", ".join(ACCEPTANCE_PATHS) or "(none)"
                messages.append({"role": "user", "content": (
                    "The acceptance tests are not passing yet:\n" + tail +
                    "\nFix the implementation file and try again. Do not "
                    f"modify the acceptance suite at: {oracle_list}.")})
                break

            if fn == "view_file":
                # Range-aware: reading several DIFFERENT regions of one large
                # file is not repetition. Ported from local_agent.py
                # (2026-07-22, MODE-29-REVIEW-STORY-LOCK-GUARD).
                ls, le = args.get("line_start"), args.get("line_end")
                if isinstance(ls, int) and isinstance(le, int):
                    sig = (fn, args.get("path"), ls // 200, le // 200)
                else:
                    sig = (fn, args.get("path"))
            else:
                sig = (fn, args.get("path") or args.get("command") or args.get("old_str", ""))
            # str_replace calls are excluded from the per-target repetition
            # guard: each one produces a *different* file state (the
            # `old_str` next time will differ, or `run_tool` will reject
            # it as "not found"), so a sequence of edits to the same file
            # is a legitimate fix-build cycle, not a repetition. The
            # read-heavy guard (MUTATING_TOOLS) still catches a model stuck
            # in a bad edit loop — str_replace calls reset that window.
            #
            above_threshold = False
            if fn not in ("str_replace", "replace_lines"):
                seen[sig] = seen.get(sig, 0) + 1
                if seen[sig] >= 3:
                    above_threshold = True
            print(f"[step {step}] {fn}: {str(args.get('command') or args.get('path') or '')[:120]}", flush=True)

            if above_threshold:
                if not nudged_repeat:
                    nudged_repeat = True
                    print("   [repetition nudge]", flush=True)
                    oracle_list = ", ".join(ACCEPTANCE_PATHS) or "(none)"
                    messages.append({"role": "user", "content": (
                        "You have repeated the same action 3 times with no progress. STOP. "
                        "Run `pytest " + " ".join(ACCEPTANCE_PATHS) + "` to see the real failure, "
                        "view the actual file contents, and fix the ROOT cause in the implementation "
                        f"file. Do not modify the acceptance suite at: {oracle_list}.")})
                    break
                print("   [parking: repeated action after nudge]", flush=True)
                if worktree_dirty():
                    auto_commit("WIP (parked on repetition)")
                if not PARK_ENABLED:
                    break
                return 3

            # A malformed tool call (e.g. a model that omits a required arg
            # like old_str) must nudge the model with a recoverable error, not
            # crash the whole unattended agent with an uncaught exception.
            tool_result = safe_run_tool(fn, args)
            messages.append({"role": "tool", "content": tool_result})

            # A GENUINELY SUCCESSFUL mutation (str_replace/create_file)
            # resets every OTHER signature's accumulated count: `seen` was a
            # lifetime cumulative counter, so re-viewing a file 2x, editing
            # it, then viewing it again to check the edit landed hit the >=3
            # threshold from stale pre-edit reads, even though real progress
            # happened in between (see local_agent.py's mirrored comment).
            #
            # Gated on SUCCESS (a result that isn't an "ERROR..." string),
            # not merely on tool identity — a REJECTED create_file/str_replace
            # changed nothing on disk and must not be treated as progress.
            # Pre-fix, this clear ran unconditionally on every mutating call
            # regardless of outcome: create_file's own repeated FAILURES
            # wiped their own accumulating count before ever reaching the
            # threshold, AND a failed str_replace interleaved between failed
            # create_file attempts also wiped create_file's count - so even
            # alternating the two tools indefinitely never tripped the guard
            # (observed live 2026-07-15: create_file/str_replace/str_replace/
            # bash on repeat, dozens of times, zero nudges).
            if fn in MUTATING_TOOLS:
                succeeded = isinstance(tool_result, str) and not tool_result.startswith("ERROR")
                if succeeded:
                    current = seen.get(sig, 0)
                    seen.clear()
                    if fn not in ("str_replace", "replace_lines"):
                        seen[sig] = current
                    last_progress_step = step

            # Failing-str_replace loop guard (mirrored from local_agent.py):
            # str_replace is excluded from the per-target repetition guard
            # above, so a no-match loop on one file runs uncaught. After 2
            # consecutive FAILED str_replace on the same path, steer to
            # replace_lines (line numbers, no byte-exact match). A successful
            # str_replace resets the counter and re-arms the nudge.
            if fn == "str_replace":
                sr_path = args.get("path", "")
                if isinstance(tool_result, str) and tool_result.startswith("ERROR"):
                    failed_sr[sr_path] = failed_sr.get(sr_path, 0) + 1
                    if failed_sr[sr_path] >= 2 and sr_path not in nudged_sr_fail:
                        nudged_sr_fail.add(sr_path)
                        print(f"   [str_replace-fail nudge: {failed_sr[sr_path]} "
                              f"failed matches on {sr_path}]", flush=True)
                        messages.append({"role": "user", "content": (
                            f"Your last {failed_sr[sr_path]} str_replace calls on {sr_path} "
                            f"did not match — you cannot construct a matching old_str (likely "
                            f"a whitespace difference). STOP retrying str_replace on this file. "
                            f"Run `nl -ba {sr_path} | sed -n '<start>,<end>p'` to get exact line "
                            f"numbers, then use replace_lines(path, start, end, new_str) which "
                            f"needs no byte-exact old_str.")})
                else:
                    failed_sr[sr_path] = 0
                    nudged_sr_fail.discard(sr_path)

            # Read-heavy-pattern guard. Tracked separately from the per-target
            # repetition guard: the per-target one misses this case because
            # every call hits a different file/command (every signature is
            # unique). The model is still in a paralysis-by-analysis loop if
            # the last READ_HEAVY_WINDOW tool calls were all non-mutating.
            # Two-stage response: one corrective nudge, then park if the
            # pattern persists.
            recent_tools.append((fn, sig[1]))
            if (len(recent_tools) == READ_HEAVY_WINDOW
                    and all(f not in MUTATING_TOOLS for (f, _t) in recent_tools)):
                distinct_targets = len({_t for (_f, _t) in recent_tools})
                has_repetition = distinct_targets < READ_HEAVY_WINDOW
                if not nudged_read_heavy:
                    nudged_read_heavy = True
                    print(f"   [read-heavy nudge: {READ_HEAVY_WINDOW} reads in a row]", flush=True)
                    oracle_list = ", ".join(ACCEPTANCE_PATHS) or "(none)"
                    messages.append({"role": "user", "content": (
                        f"You've made {READ_HEAVY_WINDOW} tool calls in a row without writing "
                        "or editing any file (only view_file / bash / checkpoint). "
                        "STOP READING and make an edit. Either:\n"
                        "1. create_file for a new module or test,\n"
                        "2. str_replace to modify an existing file based on what you've "
                        "already read, or\n"
                        "3. checkpoint if you need to commit a work-in-progress before "
                        "deciding the next concrete change.\n"
                        f"Do not modify the acceptance suite at: {oracle_list}.\n"
                        "Reading more files without acting is wasting your step budget.")})
                    # Reset so the next detection needs another full window of reads
                    # (not whatever happens to be left in the deque).
                    recent_tools.clear()
                    distinct_windows = 0
                elif has_repetition:
                    # Re-reading an already-seen target after being nudged to
                    # act is wedging, not exploration. Park at the strict
                    # 2 * READ_HEAVY_WINDOW (12) -- unchanged from the flat
                    # cutoff for this signal.
                    print("   [parking: read-heavy after nudge]", flush=True)
                    if worktree_dirty():
                        auto_commit("WIP (read-heavy parking)")
                    if not PARK_ENABLED:
                        recent_tools.clear()
                        break
                    return 3
                else:
                    # All-distinct reads after the nudge: the model is
                    # exploring multiple files (each read once), not wedged
                    # on one. A multi-file bug fix legitimately needs more
                    # than READ_HEAVY_WINDOW reads to orient before its
                    # first edit. Let it continue, bounded by
                    # READ_HEAVY_DISTINCT_WINDOWS all-distinct windows -- so
                    # exploration that reaches an edit is not cut off, but a
                    # model that reads forever with no mutation is still
                    # caught (bounded, not disabled). Ported from
                    # local_agent.py per the Mode 3a lesson.
                    distinct_windows += 1
                    if distinct_windows >= READ_HEAVY_DISTINCT_WINDOWS:
                        print(f"   [parking: read-heavy after {distinct_windows} distinct windows]",
                              flush=True)
                        if worktree_dirty():
                            auto_commit("WIP (read-heavy parking)")
                        if not PARK_ENABLED:
                            recent_tools.clear()
                            break
                        return 3
                    recent_tools.clear()

            # Fix #1: the harness, not the model, decides done. The instant the
            # independent oracle is green, auto-commit and exit. This fires
            # *during* the model's iteration so a correct first attempt
            # finishes in a single step.
            if ACCEPTANCE_PATHS and fn in ("create_file", "str_replace", "bash"):
                if finish_if_green(step, messages):
                    return 0
                # The automatic done-bar is unwinnable (oracle green, full
                # suite red CAP times running). Park rather than spend the
                # remaining steps re-running a suite that cannot go green.
                if suite_reject_cap_reached():
                    return 2

    print("[ended without oracle green — step cap reached]", flush=True)
    if worktree_dirty():
        auto_commit("WIP (step cap reached)")
    return 2


_DONE_REASONS = {0: "done", 1: "error", 2: "parked", 3: "infra_failure"}


def write_done_marker(rc: int) -> None:
    """Write the .agent_done completion marker for the orchestrator.

    Written last, so its existence means the agent has genuinely finished. A
    marker failure never changes the run's exit code. Idempotent-safe: main()
    calls this again on the way out, and a non-zero rc must never downgrade an
    already-written done (rc 0) marker."""
    try:
        existing_path = CWD / ".agent_done"
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and existing.get("exit_code") == 0 and rc != 0:
            return  # keep the proof of completion; a deliberate skip is not a failure
    except (OSError, ValueError):  # no readable done marker: fall through and write
        pass
    try:
        marker = {
            "reason": _DONE_REASONS.get(rc, "error"),
            "exit_code": rc,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        tmp = CWD / ".agent_done.tmp"
        tmp.write_text(json.dumps(marker) + "\n", encoding="utf-8")
        os.replace(tmp, CWD / ".agent_done")
    except Exception as e:  # noqa: BLE001 - a marker failure must never mask the run's exit code
        print(f"[warn] .agent_done marker not written: {e}", flush=True)


def main() -> int:
    """Run the agent loop, then drop a completion marker for the orchestrator.

    Written last, so its existence means the agent has genuinely finished. A
    marker failure never changes the run's exit code."""
    rc = _main_impl()
    write_done_marker(rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
