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
  - `LOCAL_AGENT_REVIEW_FEEDBACK_REWORK=1` marks a round that reworks a
    reviewer's REQUEST_CHANGES feedback. The oracle is usually already green
    when such a round starts, so oracle-green is not evidence the findings
    were addressed: the automatic done-bar stands down and the model's own
    `done` - still dirty-tree- and suite-gated by the loop - decides it.

Everything else (native /api/chat loop, tolerant parser, per-target
repetition guard, read-heavy-pattern guard, runtime-artifact exclusion) is
copied verbatim from scripts/local_agent.py so this is a faithful test of
just the one lever. PR #30 added the read-heavy guard to the base but
missed this variant — every story with an `acceptance` block dispatches
here, which is exactly the cohort that hits the "paralysis by analysis"
pattern most.

An acceptance fixture file missing from the working copy fails the oracle
(oracle_result returns False, naming the path) without running the test command.
At startup, a missing fixture ends the run before any model call.

Exit codes:
  0 = oracle green, auto-committed, done
  1 = LLM call failed, or a startup-missing acceptance fixture (no commit)
  2 = step cap reached, WIP-committed
  3 = repetition OR read-heavy guard fired, WIP-committed
"""
from __future__ import annotations

import json
import os
import re  # noqa: F401 (re-exported: the moved chat/repair impls use their own; kept for parity with the moved cluster's module context)
import subprocess
import sys
import time  # noqa: F401 (kept: the moved loop phases read it via origin)
from collections import deque  # noqa: F401 (kept: asserted present by test_oracle_counter_import_removed_as_dead_code_but_deque_kept)
from datetime import UTC, datetime
from pathlib import Path

import httpx  # noqa: F401 (kept: the moved loop phases read it via origin)

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
    PersistingList,  # noqa: F401 (kept: the moved loop phases read it via origin)
    _dropped_top_level_defs,
    _dropped_top_level_vars,
    _is_context_overflow_error,  # noqa: F401 (kept: the moved loop phases read it via origin)
    _load_resume_transcript,  # noqa: F401 (kept: the moved loop phases read it via origin)
    _message_char_len,  # noqa: F401 (re-exported: the moved calibration impls read it via origin)
    _persist_messages,  # noqa: F401 (kept: the moved loop phases read it via origin)
    _str_replace_not_found_diag,
    _total_chars,  # noqa: F401 (kept: the moved loop phases read it via origin)
    _trim_resumed_transcript,  # noqa: F401 (kept: the moved loop phases read it via origin)
    destructive_git_op,
)
from pipeline.worker_scope import worker_namespace
from scripts.local_agent_oracle_done_marker import restore_tampered_oracle_files_impl
from scripts.local_agent_oracle_guards import _no_tool_nudge  # noqa: F401 (kept: the moved loop phases read it via origin)
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
    FULL_SUITE_DONE_BAR,
    GREEN_COMMIT_SUBJECT,
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
    REVIEW_FEEDBACK_REWORK,
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


def missing_acceptance_fixtures() -> list[str]:
    """ACCEPTANCE_PATHS entries that are not regular files under CWD.

    Checked before any runner is trusted: a scoped runner given an absent
    fixture matches no tests and exits 0, so its exit code cannot prove the
    fixture passed.
    """
    return [rel for rel in ACCEPTANCE_PATHS if not (CWD / rel).is_file()]


def oracle_result() -> tuple[bool, str]:
    """Run the harness-owned acceptance suite. Returns (passed, tail).

    The suite is whatever test command fits the project — pytest for Python
    repos (with ACCEPTANCE_PATHS appended), cargo test for Rust, npm test for
    JS, make test where the Makefile defines one, etc. Detected via
    pipeline_mcp_server.detect_test_command so the oracle is language-agnostic
    (matches the same detector the orchestrator's check_story_status uses
    after the run finishes). On a non-zero exit (compile error, test
    failure, collection error) the tail is captured so the model can react.
    A missing fixture file fails the oracle without running the command.
    """
    if not ACCEPTANCE_PATHS:
        return True, "(no acceptance files configured)"
    missing = missing_acceptance_fixtures()
    if missing:
        return False, f"acceptance fixture missing from the working copy: {', '.join(missing)}"
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


# Whether the reviewer-feedback rework nudge (see finish_if_green) has been
# appended to the transcript. One-shot: finish_if_green runs after EVERY
# mutating tool call, so an un-gated nudge would append an identical user turn
# on each step and flood the model's context.
_FEEDBACK_NUDGED = False


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

    The oracle is the primary bar, but it is not the only one. On a
    CI-fail-rework round (REWORK_FULL_SUITE), or on ANY dispatch when the
    operator knob FULL_SUITE_DONE_BAR is armed, oracle-green is necessary but
    no longer sufficient: the full worktree suite must ALSO be green before
    the loop may terminate. The acceptance oracle excludes the agent's own
    committed test file, so without this second gate a run would stop and
    commit while that test still fails and re-fail the merge gate on the same
    assertion every round. On a full-suite failure the failing excerpt is fed
    back into `messages` (a user turn) and False is returned so the loop keeps
    working the broken test until fixed or the step cap binds; no commit
    happens on a failure. With neither signal set the oracle-green done-bar is
    unchanged. A reviewer-feedback rework round (REVIEW_FEEDBACK_REWORK) does
    not terminate on oracle-green at all - the model's own `done` decides it,
    still suite- and dirty-tree-gated.
    """
    global _SUITE_REJECTIONS, _FEEDBACK_NUDGED
    ok, _ = oracle_result()
    if not ok:
        return False
    if REVIEW_FEEDBACK_REWORK:
        # A reviewer-feedback rework round: the oracle was usually green
        # BEFORE this round started (the first dispatch is what the reviewer
        # read), so oracle-green is not evidence the findings were addressed.
        # Deciding `done` here ends the round after the agent's FIRST mutating
        # step - committing whatever it happened to touch and never the
        # finding (observed live 2026-09-21 on ASB-2: three of four
        # redispatches ended this way, two with no commit at all). The
        # findings are this round's bar and only the reviewer can judge them,
        # so defer to the model's explicit `done` - which still has to clear
        # the dirty-tree and full-suite gates in the loop. Deliberately does
        # NOT touch _SUITE_REJECTIONS: this path no longer enforces the suite,
        # so a red suite here must not count toward the automatic-path park
        # cap that would park the round for the wrong reason.
        if messages is not None and not _FEEDBACK_NUDGED:
            _FEEDBACK_NUDGED = True
            messages.append({"role": "user", "content": (
                "The acceptance oracle passes already - it was passing "
                "before this round began, so it is NOT evidence that the "
                "review findings are addressed. This round's bar is the "
                "reviewer's feedback: work those findings, then call done.")})
        print(f"[step {step}] ORACLE GREEN — reviewer-feedback rework: the "
              f"harness will not decide done on oracle-green alone; the "
              f"review findings are the bar.", flush=True)
        return False
    if REWORK_FULL_SUITE or FULL_SUITE_DONE_BAR:
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
        auto_commit(GREEN_COMMIT_SUBJECT)
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
    return restore_tampered_oracle_files_impl(_ORACLE_SNAPSHOT, CWD)


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
    """The agent step loop; phases live in scripts/local_agent_oracle_loop.py."""
    from scripts.local_agent_oracle_loop import (
        chat_turn_impl,
        no_tool_turn_impl,
        preflight_impl,
        startup_impl,
        step_cap_impl,
        step_guards_impl,
        tool_call_impl,
    )
    st = startup_impl(globals())
    rc = preflight_impl(globals(), st)
    if rc is not None:
        return rc
    for step in range(MAX_STEPS):
        rc = step_guards_impl(globals(), st, step)
        if rc is not None:
            return rc
        m, rc = chat_turn_impl(globals(), st, step)
        if rc is not None:
            return rc
        tcs = m.get("tool_calls") or recover_tool_calls(m.get("content", ""))
        if not tcs:
            rc = no_tool_turn_impl(globals(), st, step, m)
            if rc is not None:
                return rc
            continue
        st.consecutive_no_tool = 0

        for tc in tcs:
            outcome = tool_call_impl(globals(), st, step, tc)
            if outcome == "break":
                break
            if outcome is not None:
                return outcome
    return step_cap_impl(globals())


_DONE_REASONS = {0: "done", 1: "error", 2: "parked", 3: "infra_failure"}


def main() -> int:
    """Run the agent loop, then drop a completion marker for the orchestrator.

    Written last, so its existence means the agent has genuinely finished. A
    marker failure never changes the run's exit code."""
    with worker_namespace():
        rc = _main_impl()
    write_done_marker(rc)
    return rc


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
            "ts": datetime.now(UTC).isoformat(),
        }
        tmp = CWD / ".agent_done.tmp"
        tmp.write_text(json.dumps(marker) + "\n", encoding="utf-8")
        os.replace(tmp, CWD / ".agent_done")
    except Exception as e:  # noqa: BLE001 - a marker failure must never mask the run's exit code
        print(f"[warn] .agent_done marker not written: {e}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
