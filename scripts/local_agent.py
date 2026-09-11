"""Local dispatch agent loop — the body of a local `dispatch_story`.

Runs headless as a subprocess (so `check_story_status` can poll its pid and,
on exit, detect completion via the worktree's commits + tests). Drives a local
Ollama model through a native-tool-calling loop over `/api/chat`, working in
the story's git worktree.

Why this exists instead of OpenHands: a full investigation (see
Local_LLM_Port_Plan.md §Status/§8) found OpenHands' CLI is the wrong harness
for a 24B local model — its ~7 bloated tool schemas collapse devstral's native
`[TOOL_CALLS]` adherence (the model reverts to prose / markdown-json), and its
strict native-only parsing has no recovery when a tool call arrives as text.
A minimal loop with a small clean tool set + a tolerant parser was validated
end-to-end on real TDD stories (devstral 3/4 fully correct; qwen2.5-coder:14b
worse). Three guards proved load-bearing and are implemented here:

1. Non-destructive editor — `create_file` refuses to overwrite a non-empty
   file; edits must go through `str_replace`. Prevents the model silently
   clobbering its own work (observed without it).
2. Loop-repetition detection — a confused run gets one corrective nudge, then
   parks (WIP-commits and exits) instead of burning the step cap.
3. Commit enforcement — `done` is rejected while the worktree is dirty; after
   repeated rejections the loop auto-WIP-commits so correct work is never
   lost (the pipeline uses git commits as the done-signal).
4. Read-heavy-pattern guard — when the model rotates across many distinct
   `view_file`/`bash` targets without ever calling `create_file` or
   `str_replace`, the per-target repetition guard misses it (each call has
   a unique signature) but the model is still in a paralysis-by-analysis
   loop. A sliding window over recent tool names catches "many reads, zero
   writes" and parks the run before the step cap is wasted.

Tolerant parser: if the native `tool_calls` field is empty, a tool call is
recovered from the message content ([TOOL_CALLS]/bare arrays/```json fences) —
devstral intermittently leaks well-formed calls as text even with clean tools.

Config is read from the environment (set by backend.OllamaDriver.dispatch):
LOCAL_AGENT_SYSTEM, LOCAL_AGENT_TASK, LOCAL_AGENT_MODEL, LOCAL_AGENT_ENDPOINT,
PIPELINE_TRANSPORT_NUM_CTX, LOCAL_AGENT_TIMEOUT, PIPELINE_TRANSPORT_MAX_STEPS,
PIPELINE_TRANSPORT_TEMPERATURE, LOCAL_AGENT_PROVIDER (default "ollama"; "lmstudio"/
"mlx" route chat() through inference_providers instead of Ollama's streaming
/api/chat — see PROVIDER/_provider_chat_turn below). The process CWD is the
worktree. Progress is printed to stdout (which dispatch redirects to
agent.log — a non-empty log is itself the signal that a real attempt was
made).
"""
from __future__ import annotations

import json
import os
import re  # noqa: F401 (kept: tests patch module attrs on la; moved chat cluster owned the only re.* use)
import subprocess  # noqa: F401 (kept: tests patch la.subprocess.run; the moved run_tool_impl's own stdlib import binds the same module object, so those patches propagate)
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Rough chars-per-token estimate (no tokenizer available here). Observed live
# 2026-07-20: a resumed transcript sat at ~129544 chars / ~32386 tokens
# (~4.0 chars/token) right before hitting NUM_CTX=32768 and truncating -
# llama.cpp then returned a 500 on every retry (the truncated request is
# identical each time, so CHAT_MAX_ATTEMPTS's retry can never help). A rework
# resume reuses the ENTIRE prior transcript and appends more (reviewer
# feedback, a tech-lead fix checklist) with no bound, so repeated rework
# cycles on the same story compound: story 93fdc371 died this way on its
# 2nd AND 3rd rework attempts, both times on the model's very first turn.
# LA-CHAT: the constant itself moved to scripts/local_agent_chat.py; the name
# is re-exported below (after the sys.path setup, so bare-script execution
# resolves the scripts package) and the moved _effective_chars_per_token reads
# it via its own module constant.

# Calibrated at runtime from ollama's own measured prompt_eval_count (the
# streamed done chunk's real token count for everything sent in that
# request). The fixed guess above is frequently wrong: live on the ollama
# server log 2026-07-29, a real 41,921-token gpt-oss prompt measured ~2.35
# chars/token against the 4.0 guess, so a budget computed from 4.0 alone was
# already ~28% over NUM_CTX by the time a 500 forced a reactive trim. Set by
# _stream_one_turn on every ollama turn that reports a count; never set by
# _provider_chat_turn, whose contract deliberately returns only the bare
# message (see test_provider_chat_turn_extracts_message_from_envelope) - an
# lmstudio/mlx dispatch falls back to the fixed guess via
# _effective_chars_per_token(). Reset to None at the top of main() so
# calibration never leaks between dispatches (or, in-process, test runs)
# that share this module.
_measured_chars_per_token: float | None = None
_last_prompt_eval_count: int | None = None


def _effective_chars_per_token() -> float:
    """The live-calibrated chars/token ratio when available, else the fixed
    _CHARS_PER_TOKEN_ESTIMATE guess."""
    from scripts.local_agent_chat import _effective_chars_per_token_impl
    return _effective_chars_per_token_impl(globals())


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# LA-CHAT: kept bound for the moved chat cluster, which reads it via origin
# (origin["inference_providers"]) and for tests that patch la.inference_providers.
from app import inference_providers  # noqa: F401
from app import (
    pipeline_mcp_server as p,  # noqa: F401 (kept: tests patch la.p attrs; moved run_tool_impl reads the same module object via origin["p"])
)
from pipeline import (
    edit_guards,  # noqa: F401 (kept: tests assert la.edit_guards is edit_guards; moved run_tool_impl reads it via origin["edit_guards"])
)
from pipeline.agent_log_format import format_step
from pipeline.local_agent_common import (
    CWD,
    PersistingList,
    _answer_orphaned_calls,
    _dropped_span_digest,  # noqa: F401 (unused here; re-exported for test_local_agent_context_compaction.py)
    _dropped_top_level_defs,  # noqa: F401 (kept: moved run_tool_impl reads it via origin["_dropped_top_level_defs"])
    _dropped_top_level_vars,  # noqa: F401 (kept: moved run_tool_impl reads it via origin["_dropped_top_level_vars"])
    _is_context_overflow_error,
    _load_resume_transcript,
    _message_char_len,  # noqa: F401 (kept: moved chat cluster reads it via origin["_message_char_len"])
    _persist_messages,
    _repetition_nudge,
    _str_replace_not_found_diag,  # noqa: F401 (kept: moved run_tool_impl reads it via origin["_str_replace_not_found_diag"])
    _total_chars,
    _trim_resumed_transcript,
    destructive_git_op,  # noqa: F401 (kept: moved run_tool_impl reads it via origin["destructive_git_op"])
)
from pipeline.local_agent_common import (  # noqa: F401 (kept: moved recover_tool_calls_impl reads it via origin["_recover_tool_calls_shared"])
    recover_tool_calls as _recover_tool_calls_shared,
)

# LA-CHAT: the chars/token estimate constant itself moved to
# scripts/local_agent_chat.py; the name is re-exported here (tests read
# la._CHARS_PER_TOKEN_ESTIMATE) and the moved _effective_chars_per_token reads
# it via its own module constant.
from scripts.local_agent_chat import (  # noqa: F401 (re-exported: tests read la._CHARS_PER_TOKEN_ESTIMATE)
    _CHARS_PER_TOKEN_ESTIMATE,
)

# Several tests exec this file fresh via importlib (spec_from_file_location +
# exec_module) after mutating os.environ, to verify env-var-derived constants
# recompute per load (e.g. test_acceptance_transport_alias.py,
# test_local_agent_persistence.py's load_module_with_env). Before the config
# split below, those constants lived directly in this file's body, so a fresh
# exec always recomputed them. Now they live in scripts/local_agent_config.py,
# which Python's import system would otherwise serve from its sys.modules
# cache on a second exec — silently keeping stale env-derived values. Evict it
# here so every fresh exec of *this* file also gets a fresh read of current
# os.environ, matching pre-split behavior with no test changes required.
sys.modules.pop("scripts.local_agent_config", None)
# LA-RECOVERY: RECOVERY_BACKOFF_SECONDS/_RECOVERY_ROUNDS moved to
# scripts/local_agent_recovery.py (env read at that module's import), so a
# second fresh exec of this file must evict the cached recovery module too or
# the wrapper's lazy import would serve stale env-derived constants.
sys.modules.pop("scripts.local_agent_recovery", None)
from scripts.local_agent_config import (
    _THINK_LEVELS,  # noqa: F401 (kept: moved _ollama_payload_impl reads it via origin["_THINK_LEVELS"])
    BASH_TIMEOUT,  # noqa: F401 (kept: moved run_tool_impl reads it via origin["BASH_TIMEOUT"])
    CHAT_MAX_ATTEMPTS,  # noqa: F401 (kept: moved recover_from_oversized_5xx_impl reads it via origin["CHAT_MAX_ATTEMPTS"])
    CHAT_RETRY_BACKOFF,  # noqa: F401 (kept: moved chat_impl reads it via origin["CHAT_RETRY_BACKOFF"])
    CONNECT_TIMEOUT_SECONDS,  # noqa: F401 (kept: moved _stream_one_turn_impl reads it via origin)
    ENDPOINT,
    FULL_SUITE_DONE_BAR,
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
    READ_SILENCE_SECONDS,  # noqa: F401 (kept: moved _stream_one_turn_impl reads it via origin["READ_SILENCE_SECONDS"])
    REWORK_FULL_SUITE,
    REWORK_SUITE_REJECT_CAP,
    SCRATCHPAD_NUDGE_STEPS,
    SCRATCHPAD_ON,
    STR_REPLACE_FAIL_ESCALATE_AFTER,
    TEMPERATURE,  # noqa: F401 (kept: moved _ollama_payload_impl/_provider_chat_turn_impl read it via origin)
    THINK,  # noqa: F401 (kept: moved _ollama_payload_impl reads it via origin["THINK"])
    TIMEOUT,
    TOOLS,
)
from scripts.local_agent_guards import (  # noqa: F401 (re-exported: the step loop references these as bare names)
    CHURN_SAME_PATH_MAX_EDITS,
    _apply_off_task_action_impl,
    _bash_off_task_path,
    _churn_note_test_run,
    _churn_step,
    _expected_task_paths,
    _is_off_task_path,
    _no_tool_nudge,
    _off_task_step,
    _reject_done_for_suite_impl,
)
from scripts.local_agent_repair import (  # noqa: F401 (re-exported: run_tool references these as bare names)
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


def read_correlation_id() -> str:
    """The orchestrator-minted correlation ID for this dispatch (W4L-02 mints
    one per story and exports it as PIPELINE_CORRELATION_ID into the agent
    subprocess env). Read at call time — never cached at import — so a fresh
    exec of this module under a mutated environment sees the current value.
    Empty string when unset; callers treat "" as "no correlation id"."""
    return os.environ.get("PIPELINE_CORRELATION_ID", "")


def emit_step_line(step: int, message: str, correlation_id: str = "") -> str:
    """Emit one structured ``[step N] ...`` line to stdout — the stream Popen
    redirects into the worktree's agent.log — and return it.

    When ``correlation_id`` is non-empty the line gains a trailing
    ``[cid=<id>]`` suffix so the agent's own log records join to the
    orchestrator's events by that id (W4L-03). The default is a literal ""
    (NOT an env read): existing call sites that omit the kwarg keep producing
    today's exact output, and call sites that want the id pass
    ``read_correlation_id()`` explicitly. The suffix is computed fresh per
    call and never accumulated.
    """
    idx = message.find(": ")
    if idx != -1:
        arg = message[idx + 2:]
        if len(arg) <= 120:
            line = format_step(
                step, message[:idx], arg, correlation_id=correlation_id
            )
        else:
            # format_step truncates its argument to arg[:120]
            # (pipeline/agent_log_format.py), but the pre-delegation
            # emit_step_line never truncated.  The DONE call site passes a
            # free-form summary, so render long arguments verbatim here —
            # exactly the old f-string — instead of losing characters past
            # char 120 (build_detect._last_done_summary takes the summary
            # verbatim into story_status._is_give_up_summary).
            line = f"[step {step}] {message}"
            if correlation_id:
                line += f" [cid={correlation_id}]"
    else:
        line = f"[step {step}] {message}"
        if correlation_id:
            line += f" [cid={correlation_id}]"
    print(line, flush=True)
    return line


def _apply_off_task_action(action: str, path_arg: str, messages: list) -> bool:
    """Perform the off-task-drift guard's side effects for `action` (as
    returned by _off_task_step). Returns True if the caller should treat
    this as a parkable escalation (WIP-committing a dirty tree is done here;
    whether to actually terminate the run is a PARK_ENABLED decision left to
    the caller, exactly like every other guard's kill-switch handling)."""
    return _apply_off_task_action_impl(globals(), action, path_arg, messages)




def _stream_one_turn(payload):
    """One streamed chat turn against Ollama's /api/chat."""
    from scripts.local_agent_chat import _stream_one_turn_impl
    return _stream_one_turn_impl(globals(), payload)


def _provider_chat_turn(messages):
    """One provider-backed chat turn for a non-Ollama PROVIDER (lmstudio, mlx)."""
    from scripts.local_agent_chat import _provider_chat_turn_impl
    return _provider_chat_turn_impl(globals(), messages)


def _ollama_payload(messages):
    """Build the Ollama /api/chat request body for one turn."""
    from scripts.local_agent_chat import _ollama_payload_impl
    return _ollama_payload_impl(globals(), messages)


def chat(messages):
    """One LLM turn, with retry."""
    from scripts.local_agent_chat import chat_impl
    return chat_impl(globals(), messages)


def _repair_triple_quoted_strings(candidate):
    """Rewrite Python-style triple-quoted string literals (\"\"\"...\"\"\" or '''...''') as JSON-encoded strings."""
    from scripts.local_agent_chat import _repair_triple_quoted_strings_impl
    return _repair_triple_quoted_strings_impl(globals(), candidate)


def recover_tool_calls(content):
    """Pull a tool call out of message text when the native field is empty."""
    from scripts.local_agent_chat import recover_tool_calls_impl
    return recover_tool_calls_impl(globals(), content)


def git(*args):
    from scripts.local_agent_git import git_impl
    return git_impl(globals(), *args)


def exclude_runtime_artifacts() -> None:
    """Keep dispatch runtime junk out of git."""
    from scripts.local_agent_git import exclude_runtime_artifacts_impl
    return exclude_runtime_artifacts_impl(globals())


def worktree_dirty() -> bool:
    from scripts.local_agent_git import worktree_dirty_impl
    return worktree_dirty_impl(globals())


def auto_wip_commit(reason: str) -> None:
    from scripts.local_agent_git import auto_wip_commit_impl
    return auto_wip_commit_impl(globals(), reason)


def _full_suite_result() -> tuple[bool, str, str | None]:
    """Run the FULL worktree suite (unscoped), for the L1 CI-fail-rework done-gate."""
    from scripts.local_agent_git import _full_suite_result_impl
    return _full_suite_result_impl(globals())


def _reject_done_for_suite(messages: list, step: int, suite_tail: str, gate: str | None) -> None:
    """L1: feed a full-suite failure back as a user turn and announce the rejection. Used at both `done`-rejection sites (clean tree, and the dirty-tree auto-accept escape) so the raised rework done-bar holds and the agent can't dodge it by interleaving dirty/clean done calls. The caller increments `suite_rejections` and `break`s out of the tool-call loop so the next step re-enters with this fed-back excerpt."""
    return _reject_done_for_suite_impl(globals(), messages, step, suite_tail, gate)
# Paths successfully written via create_file THIS process run. The
# non-destructive-editor guard (see run_tool's create_file branch) exists to
# protect PRE-EXISTING repo/seed files from being clobbered by a confused
# model - it was never meant to also block the model from overwriting a file
# it wrote itself moments ago. A weak model that can't construct a correct
# str_replace old_str often has "rewrite the whole small file" as its only
# real recovery strategy; forcing surgical edits it can't produce just
# deadlocks it. Observed live 2026-07-15 (lru_cache): a model alternated
# rejected create_file / rejected str_replace calls for dozens of steps,
# never finishing, because create_file on its own just-created file was
# unconditionally rejected. Scoped to this process's lifetime (module-level,
# reset on every fresh dispatch/rework subprocess) so a REWORK's inherited
# file - which may need a surgical fix, not a wholesale rewrite - is still
# protected until the model creates it again itself in the new process.
_CREATED_THIS_RUN: set[str] = set()

# Companion to _CREATED_THIS_RUN for the RESUME/rework case. On a step-cap
# resume, the impl and test files already exist on disk from the interrupted
# run, so they are NOT in _CREATED_THIS_RUN in the fresh process - and the
# create_file guard would force the weak model onto str_replace it cannot
# construct. Requiring the model to view_file the target first makes the
# overwrite an INFORMED one (it read the current contents before replacing
# them), which preserves the guard's real purpose - stopping a blind clobber
# of a file the model has never seen - while unblocking the whole-file rewrite
# recovery path. Observed live 2026-07-16 (interval_merge resume): the guard
# steered a resumed run to str_replace, which then ground through 28+ rejected
# surgical-edit cycles (~2310s) instead of one whole-file rewrite.
_VIEWED_THIS_RUN: set[str] = set()


def run_tool(fn, args) -> str:
    # Wiring contract (LA-TOOLS): the replace_lines branch below-in-impl still
    # calls edit_guards.verify_range_anchors (with the "MUST stay optional"
    # comment) and edit_guards.duplicated_block_warning (advisory, not a
    # block, and never gated on a .py extension) — the call sites now live in
    # scripts/local_agent_tools.py::run_tool_impl, which this wrapper
    # delegates to with this module's live namespace.
    from scripts.local_agent_tools import run_tool_impl
    return run_tool_impl(globals(), fn, args)


_TOOL_SCHEMAS = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}


def safe_run_tool(fn, args) -> str:
    """Run a tool, turning any exception into a recoverable error message.

    A model that omits a required argument (e.g. str_replace without old_str,
    observed with weaker local models) would otherwise raise an uncaught
    KeyError and crash the whole unattended agent. Feeding the error back as a
    tool result lets the model correct itself, bounded by the loop guard / step
    cap, instead of taking the run down.

    When the exception coincides with a missing declared-required argument,
    append what the tool actually requires and what was passed instead
    (TDD_SPLIT_PRODUCTION_PLAN.md Phase 5 live validation, 2026-07-18:
    gpt-oss:20b called view_file with hallucinated {"line_start", "line_end"}
    in place of the declared {"path"}; the bare "ERROR running view_file:
    KeyError: 'path'" this used to return names what's missing but not the
    tool's actual shape, and the model needed several malformed retries to
    self-correct, tripping the per-target repetition guard into a park). This
    only ever appends to the existing message - the base "ERROR running
    {fn}: ..." text is unchanged, so it stays a strict superset.
    """
    from scripts.local_agent_tools import safe_run_tool_impl
    return safe_run_tool_impl(globals(), fn, args)


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
    """
    from scripts.local_agent_recovery import recover_from_oversized_5xx_impl

    return recover_from_oversized_5xx_impl(globals(), messages, chat_fn, step=step)


def _main_impl() -> int:
    # A fresh dispatch has no calibration data yet - clear any value left
    # over from a prior dispatch that shared this process (or, in-process,
    # a prior test) so it never leaks in.
    global _measured_chars_per_token, _last_prompt_eval_count
    _measured_chars_per_token = None
    _last_prompt_eval_count = None
    # Startup heartbeat. flush=True is load-bearing: stdout is block-buffered
    # to a non-tty file (the worktree's agent.log, redirected by Popen), so
    # without flush the line wouldn't hit disk until the buffer fills or the
    # process exits. With it, check_story_status's "empty agent.log = failed
    # launch" check has a precise signal: a 0-byte log means the process
    # never reached main() (a genuine failed launch), while a log with [boot]
    # and no further output means the agent is alive and queued (e.g. on
    # Ollama's -np 1 worker waiting for an inference slot).
    print(
        f"[boot] pid={os.getpid()} model={MODEL} endpoint={ENDPOINT} "
        f"provider={PROVIDER} steps={MAX_STEPS} timeout={TIMEOUT}s",
        flush=True,
    )
    system = os.environ.get("LOCAL_AGENT_SYSTEM", "").strip()
    task = os.environ.get("LOCAL_AGENT_TASK", "")
    expected_paths = _expected_task_paths(task)
    # Initialize messages list with optional persistence support.
    # Resume path: if LOCAL_AGENT_RESUME_TRANSCRIPT_PATH points at a valid
    # transcript, load it instead of building the fresh system/task pair (the
    # loaded transcript already contains the original system+task prompt).
    # Otherwise fall back to the fresh pair. LOCAL_AGENT_TRANSCRIPT_PATH
    # (persistence) is independent of resume — a dispatch can persist without
    # resuming, resume without persisting, or both.
    transcript_path = os.environ.get("LOCAL_AGENT_TRANSCRIPT_PATH")
    messages = PersistingList(transcript_path=transcript_path)
    if resume := _load_resume_transcript():
        # Reserve headroom below NUM_CTX for the resume-append content (e.g.
        # reviewer feedback), the model's own generation, and the rough
        # imprecision of the chars/token estimate - trimming to the exact
        # limit would still overflow once more content is added below. No
        # live calibration exists yet this early (reset below, before any
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

    exclude_runtime_artifacts()

    seen: dict = {}
    nudged_repeat = False
    nudged_read_heavy = False
    recent_tools: deque[tuple[str, str]] = deque(maxlen=READ_HEAVY_WINDOW)
    distinct_windows = 0
    done_rejections = 0
    # L1: separate counter for full-suite done-rejections on a CI-fail-rework
    # round. Kept distinct from done_rejections so it cannot trip the dirty-
    # tree auto-accept-at-2 logic (a failing suite must NEVER be auto-
    # accepted). Bounded by REWORK_SUITE_REJECT_CAP: once the agent has failed
    # to green the suite that many times the run parks (return 2) rather than
    # burning the rest of the step/wall-clock budget re-prompting a model that
    # cannot make progress.
    suite_rejections = 0
    consecutive_no_tool = 0
    # Failing-str_replace loop guard: str_replace is excluded from the
    # per-target repetition guard (each old_str differs), so a no-match loop
    # on one file runs uncaught. Track consecutive FAILED str_replace per
    # path; after 2, steer to replace_lines. Reset on a successful str_replace
    # to that path (and re-arm the nudge so a second stall is caught too).
    # sr_fail_since_nudge counts further failures AFTER the nudge so a model
    # that ignores it and keeps retrying str_replace on the same path parks
    # instead of getting the one nudge for the rest of the run (Mode 31
    # follow-up: this guard previously had no escalation at all).
    failed_sr: dict[str, int] = {}
    nudged_sr_fail: set[str] = set()
    sr_fail_since_nudge: dict[str, int] = {}
    last_progress_step = 0
    last_scratchpad_step = 0
    start_time = time.monotonic()
    # Off-task-drift guard (Mode 31): a dispatched agent once abandoned its
    # assigned task and spent 20+ steps of real, coherent tool calls on a
    # completely unrelated subject. off_task_targets tracks every distinct
    # mutated path that _is_off_task_path flags (informational — see
    # _off_task_step for the actual escalation rule, which no longer
    # requires distinctness).
    off_task_targets: set[str] = set()
    nudged_off_task = False
    # Same-path edit-churn guard state (see _churn_step above main()).
    churn_state = {"path": "", "count": 0, "nudged": False}

    for step in range(MAX_STEPS):
        if time.monotonic() - start_time > TIMEOUT:
            print(f"[step {step}] wall-clock timeout ({TIMEOUT}s) reached; parking", flush=True)
            if worktree_dirty():
                auto_wip_commit("wall-clock timeout")
            return 2
        if step - last_progress_step >= NET_PROGRESS_MAX_STEPS:
            print(f"[step {step}] no successful edit in {step - last_progress_step} steps "
                  f"(last progress at step {last_progress_step}); parking", flush=True)
            if worktree_dirty():
                auto_wip_commit("no net progress")
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
        if SCRATCHPAD_ON and step - last_scratchpad_step >= SCRATCHPAD_NUDGE_STEPS:
            print(f"[step {step}] no scratchpad update in "
                  f"{step - last_scratchpad_step} steps; nudging", flush=True)
            messages.append({"role": "user", "content": (
                "You haven't updated .agent_scratchpad.md in a while. "
                "Before your next tool call, str_replace it with a short "
                "running summary of what you've done and what's next "
                "(create_file first if it doesn't exist yet).")})
            last_scratchpad_step = step
        try:
            m = chat(messages)
        except httpx.HTTPStatusError as e:
            if not _is_context_overflow_error(e):
                print(f"[step {step}] LLM call failed: {e}", flush=True)
                if worktree_dirty():
                    auto_wip_commit("llm error")
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
            # complete converging run).
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
                    auto_wip_commit("llm error")
                return 1
            if m is None:
                print(f"[step {step}] LLM call failed after trim-retry: {e}", flush=True)
                if worktree_dirty():
                    auto_wip_commit("llm error")
                return 1
        except Exception as e:  # noqa: BLE001 (an LLM backend call can fail in unpredictable ways; must not crash the agent loop)
            print(f"[step {step}] LLM call failed: {e}", flush=True)
            if worktree_dirty():
                auto_wip_commit("llm error")
            return 1
        messages.append(m)
        # Proactive trim: once a turn's measured prompt_eval_count is already
        # close to NUM_CTX, shrink the transcript now rather than waiting for
        # the next turn to overflow and 500 - the reactive path above is the
        # backstop for when no measurement exists yet, not the primary
        # defense. No-ops when _trim_resumed_transcript finds nothing to drop
        # (e.g. only one block of post-head content so far).
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
                    auto_wip_commit("narration cap")
                return 2
            messages.append({"role": "user", "content": _no_tool_nudge(
                consecutive_no_tool, content=m.get("content", ""))})
            continue
        consecutive_no_tool = 0

        for tc_idx, tc in enumerate(tcs):
            fn = tc["function"]["name"]
            args = tc["function"]["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:  # noqa: BLE001 (malformed tool-call args from the model fall back to empty args rather than crashing the loop)
                    args = {}

            if fn == "done":
                if worktree_dirty():
                    done_rejections += 1
                    if done_rejections >= 2:
                        # L1: the dirty-tree auto-accept escape must not bypass
                        # the raised rework done-bar. Without this gate an agent
                        # could dodge it by interleaving dirty/clean done calls
                        # (dirty->reject, commit, clean+failing-suite->reject,
                        # dirty->reject->auto-accept with the suite never
                        # checked). The merge-gate backstop would still catch a
                        # broken merge (bounded by MERGE_MAX_ATTEMPTS), but the
                        # bar should hold in the agent loop too. On a rework
                        # round, check the suite first; a failure WIP-commits
                        # (so the next attempt starts clean) and rejects instead
                        # of auto-accepting. Bounded by MAX_STEPS.
                        if REWORK_FULL_SUITE or FULL_SUITE_DONE_BAR:
                            suite_ok, suite_tail, gate = _full_suite_result()
                            if not suite_ok:
                                suite_rejections += 1
                                auto_wip_commit("commit enforcement")
                                if suite_rejections >= REWORK_SUITE_REJECT_CAP:
                                    print(f"[step {step}] rework suite-reject cap "
                                          f"({REWORK_SUITE_REJECT_CAP}) reached; agent "
                                          f"cannot green the full suite — parking", flush=True)
                                    return 2
                                _reject_done_for_suite(messages, step, suite_tail, gate)
                                _answer_orphaned_calls(tcs, tc_idx + 1, messages)
                                break
                        auto_wip_commit("commit enforcement")
                        print(f"[step {step}] DONE with auto-WIP-commit (agent left tree dirty): "
                              f"{args.get('summary', '')}", flush=True)
                        return 0
                    print(f"[step {step}] done rejected — worktree dirty, asking agent to commit", flush=True)
                    messages.append({"role": "user", "content": (
                        "You have uncommitted changes. Commit your work with git "
                        "(git add -A && git commit -m ...) before calling done.")})
                    _answer_orphaned_calls(tcs, tc_idx + 1, messages)
                    break
                # L1: on a CI-fail-rework round, the reviewer was acceptance-
                # scoped and never saw the agent's own test - so a clean
                # worktree + `done` is not sufficient. Require the FULL suite
                # green; otherwise feed the failing excerpt back and reject
                # done so the agent fixes its own broken assertion (or the step
                # cap binds). Non-rework dispatches skip this gate entirely.
                if REWORK_FULL_SUITE or FULL_SUITE_DONE_BAR:
                    suite_ok, suite_tail, gate = _full_suite_result()
                    if not suite_ok:
                        suite_rejections += 1
                        if suite_rejections >= REWORK_SUITE_REJECT_CAP:
                            if worktree_dirty():
                                auto_wip_commit("rework suite-reject cap")
                            print(f"[step {step}] rework suite-reject cap "
                                  f"({REWORK_SUITE_REJECT_CAP}) reached; agent "
                                  f"cannot green the full suite — parking", flush=True)
                            return 2
                        _reject_done_for_suite(messages, step, suite_tail, gate)
                        _answer_orphaned_calls(tcs, tc_idx + 1, messages)
                        break
                emit_step_line(
                    step,
                    f"DONE: {args.get('summary', '')}",
                    correlation_id=read_correlation_id(),
                )
                return 0

            if fn == "view_file":
                # Range-aware: reading several DIFFERENT regions of one large
                # file (exactly what orienting in a multi-hundred-line
                # function requires) is not repetition and must not share a
                # signature with re-reading the SAME region. Bucket by 200-
                # line window so near-identical ranges (e.g. an off-by-one
                # retry) still count as the same target, but a genuinely
                # different region does not. A bare call (no range - the
                # file's head, truncated) keeps the old path-only signature,
                # since re-issuing that exact call is always a true repeat.
                ls, le = args.get("line_start"), args.get("line_end")
                if isinstance(ls, int) and isinstance(le, int):
                    sig = (fn, args.get("path"), ls // 200, le // 200)
                else:
                    sig = (fn, args.get("path"))
            else:
                sig = (fn, args.get("path") or args.get("command") or args.get("old_str", ""))
            # str_replace and replace_lines calls are excluded from the
            # per-target repetition guard: each one produces a *different*
            # file state (the `old_str`/line-range next time will differ, or
            # `run_tool` will reject it as "not found"), so a sequence of
            # edits to the same file is a legitimate fix-build cycle, not a
            # repetition. The read-heavy guard (MUTATING_TOOLS) still catches
            # a model stuck in a bad edit loop — mutating calls reset that
            # window. The failing-str_replace guard below catches the no-match
            # loop str_replace's exclusion would otherwise hide.
            #
            above_threshold = False
            if fn not in ("str_replace", "replace_lines"):
                seen[sig] = seen.get(sig, 0) + 1
                if seen[sig] >= 3:
                    above_threshold = True
            print(f"[step {step}] {fn}: {str(args.get('command') or args.get('path') or '')[:120]}", flush=True)

            if above_threshold:
                first_trip = not nudged_repeat
                nudged_repeat = True
                print("   [repetition nudge]" if first_trip
                      else "   [parking: repeated action after nudge]", flush=True)
                # Every trip answers the triggering tool_calls entry with a
                # `tool`-role message, not just the first: leaving a later
                # trip's call unanswered orphans it for the rest of the run
                # (no tool_call ever goes without a reply), which live
                # (Mode 33, 2026-07-22) correlated directly with gpt-oss:20b's
                # Harmony-format output degrading into leaked special tokens
                # a few turns after the first orphaned call. Re-delivering the
                # guidance every time also gives the model a fresh chance to
                # self-correct instead of going silent after one warning.
                messages.append({"role": "tool", "content": _repetition_nudge()})
                if first_trip:
                    _answer_orphaned_calls(tcs, tc_idx + 1, messages)
                    break
                if worktree_dirty():
                    auto_wip_commit("parked on repetition")
                if not PARK_ENABLED:
                    # Suppressed: let the nudge steer and continue to the next
                    # step instead of terminating. The step cap bounds the run.
                    _answer_orphaned_calls(tcs, tc_idx + 1, messages)
                    break
                return 3

            # A malformed tool call (e.g. a model that omits a required arg
            # like str_replace without old_str) must nudge the model with a
            # recoverable error, not crash the whole unattended agent with an
            # uncaught exception.
            tool_result = safe_run_tool(fn, args)
            messages.append({"role": "tool", "content": tool_result})

            # A GENUINELY SUCCESSFUL mutation (str_replace/create_file)
            # resets every OTHER signature's accumulated count: `seen` was a
            # lifetime cumulative counter, so re-viewing a file 2x, editing
            # it, then viewing it again to check the edit landed hit the >=3
            # threshold from stale pre-edit reads, even though real progress
            # happened in between. That false-triggered on gpt-oss's
            # ratelimiter_inspect RLI-2 runs on 2026-07-04: view, view, edit,
            # view (3rd cumulative view -> nudge), view (park) -- despite the
            # edit and a test run in between. A real edit invalidates prior
            # reads' staleness, so the count for everything else should start
            # over.
            #
            # Gated on SUCCESS (a result that isn't an "ERROR..." string),
            # not merely on tool identity — a REJECTED create_file/str_replace
            # changed nothing on disk and must not be treated as progress.
            # Pre-fix, this clear ran unconditionally on every mutating call
            # regardless of outcome, which broke it two ways: (1) create_file
            # is itself one of MUTATING_TOOLS, so its own repeated FAILURES
            # wiped their own accumulating count before ever reaching the
            # threshold (34 consecutive rejected create_file calls, live
            # 2026-07-15, zero nudges); (2) a failed str_replace interleaved
            # between failed create_file attempts also wiped create_file's
            # count, so even alternating the two tools indefinitely never
            # tripped the guard (same incident - the actual failure pattern
            # observed was create_file/str_replace/str_replace/bash on
            # repeat, not pure consecutive create_file).
            if fn in MUTATING_TOOLS:
                succeeded = isinstance(tool_result, str) and not tool_result.startswith("ERROR")
                if succeeded:
                    current = seen.get(sig, 0)
                    seen.clear()
                    if fn not in ("str_replace", "replace_lines"):
                        seen[sig] = current
                    last_progress_step = step
                    if str(args.get("path", "")) .endswith(".agent_scratchpad.md"):
                        last_scratchpad_step = step
                    # A genuinely successful mutation invalidates the staleness
                    # that armed these guards too, not just `seen` — without
                    # this, a run that already made real progress since its
                    # first repetition/read-heavy nudge would skip straight to
                    # parking on its NEXT trip instead of getting a fresh
                    # nudge, even though the underlying `seen`/`recent_tools`
                    # signal it's reacting to has already been invalidated by
                    # that same progress.
                    nudged_repeat = False
                    nudged_read_heavy = False
                    distinct_windows = 0

                    path_arg = args.get("path", "")
                    if path_arg:
                        off_action, nudged_off_task = _off_task_step(
                            path_arg, expected_paths, off_task_targets, nudged_off_task)
                        if off_action != "none":
                            escalated = _apply_off_task_action(off_action, path_arg, messages)
                            if escalated:
                                if PARK_ENABLED:
                                    return 3
                                messages.append({"role": "user", "content": (
                                    f"You are still touching {path_arg}, outside your "
                                    f"assigned scope, after already being told to stop. "
                                    f"Refocus on the files named in your instructions "
                                    f"now.")})

                        churn_action = _churn_step(path_arg, churn_state)
                        if churn_action == "nudge":
                            print(f"   [churn nudge: {CHURN_SAME_PATH_MAX_EDITS}+ edits to "
                                  f"{path_arg} with no test run]", flush=True)
                            messages.append({"role": "user", "content": (
                                f"You've made {CHURN_SAME_PATH_MAX_EDITS} edits to "
                                f"{path_arg} in a row without running its tests. Run "
                                f"`pytest -q <the test file for {path_arg}>` (or the "
                                f"project's full test command) now to check whether "
                                f"your changes actually work, before making another "
                                f"edit.")})
                        elif churn_action == "escalate":
                            print(f"   [parking: churn on {path_arg} continues with "
                                  f"no test run]", flush=True)
                            if worktree_dirty():
                                auto_wip_commit("parked on edit churn")
                            if PARK_ENABLED:
                                return 3
                            messages.append({"role": "user", "content": (
                                f"You are still editing {path_arg} without running its "
                                f"tests. STOP editing and run the tests now — if they "
                                f"fail, read the failure and fix the ROOT cause; if "
                                f"they pass, call done.")})

            # Failing-str_replace loop guard. str_replace is excluded from the
            # per-target repetition guard above, so a no-match loop on one file
            # (the 2026-07-20 server.py wall: gpt-oss retried slightly-different
            # old_str values that never matched the file's whitespace) runs
            # uncaught. After 2 consecutive FAILED str_replace on the same path,
            # steer to replace_lines (line numbers, no byte-exact match). A
            # successful str_replace resets the counter and re-arms the nudge.
            # If the model ignores the nudge and keeps failing on the same
            # path, escalate to a park after STR_REPLACE_FAIL_ESCALATE_AFTER
            # more failures (Mode 31 follow-up: this guard previously had no
            # escalation at all past the one nudge — a model that kept
            # retrying str_replace forever after being told to switch to
            # replace_lines got no further guard action for the rest of the
            # run).
            if fn == "str_replace":
                sr_path = args.get("path", "")
                if isinstance(tool_result, str) and tool_result.startswith("ERROR"):
                    failed_sr[sr_path] = failed_sr.get(sr_path, 0) + 1
                    if failed_sr[sr_path] >= 2:
                        if sr_path not in nudged_sr_fail:
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
                            sr_fail_since_nudge[sr_path] = sr_fail_since_nudge.get(sr_path, 0) + 1
                            if sr_fail_since_nudge[sr_path] >= STR_REPLACE_FAIL_ESCALATE_AFTER:
                                sr_fail_since_nudge[sr_path] = 0
                                print(f"   [parking: str_replace still failing on {sr_path} "
                                      f"after nudge]", flush=True)
                                if worktree_dirty():
                                    auto_wip_commit("parked on failing str_replace")
                                if PARK_ENABLED:
                                    return 3
                                messages.append({"role": "user", "content": (
                                    f"You are still retrying str_replace on {sr_path} after "
                                    f"being told to switch. Use replace_lines(path, start, "
                                    f"end, new_str) now — do not attempt str_replace on this "
                                    f"file again.")})
                else:
                    failed_sr[sr_path] = 0
                    nudged_sr_fail.discard(sr_path)
                    sr_fail_since_nudge.pop(sr_path, None)

            # Bash-mutation off-task detection (Mode 31 follow-up): a
            # formatter/linter --fix, sed/perl -i, or shell redirection can
            # mutate a file directly through `bash`, bypassing the
            # create_file/str_replace/replace_lines path the guard above
            # watches. Route any detected off-task write through the same
            # escalation as a direct file-tool edit. Also: a `pytest` bash
            # call is the churn guard's own "the model IS checking its work"
            # signal, independent of whether this call named an off-task path.
            if fn == "bash":
                cmd = args.get("command", "")
                _churn_note_test_run(cmd, churn_state)
                off_path = _bash_off_task_path(cmd, expected_paths)
                if off_path:
                    off_action, nudged_off_task = _off_task_step(
                        off_path, expected_paths, off_task_targets, nudged_off_task)
                    if off_action != "none":
                        escalated = _apply_off_task_action(off_action, off_path, messages)
                        if escalated:
                            if PARK_ENABLED:
                                return 3
                            messages.append({"role": "user", "content": (
                                f"You are still touching {off_path}, outside your "
                                f"assigned scope, after already being told to stop. "
                                f"Refocus on the files named in your instructions "
                                f"now.")})

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
                    messages.append({"role": "user", "content": (
                        f"You've made {READ_HEAVY_WINDOW} tool calls in a row without writing "
                        "or editing any file (only view_file / bash / checkpoint). "
                        "STOP READING and make an edit. Either:\n"
                        "1. create_file for a new module or test,\n"
                        "2. str_replace to modify an existing file based on what you've "
                        "already read, or\n"
                        "3. checkpoint if you need to commit a work-in-progress before "
                        "deciding the next concrete change.\n"
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
                        auto_wip_commit("read-heavy parking")
                    if not PARK_ENABLED:
                        # Same class of bug as the per-target guard (Mode 33):
                        # a silent break here means the model gets no renewed
                        # feedback on any window after the first nudge.
                        messages.append({"role": "user", "content": (
                            "You're still re-reading targets you've already "
                            "seen instead of acting. STOP READING and make an "
                            "edit (create_file, str_replace, or checkpoint) "
                            "now.")})
                        recent_tools.clear()
                        _answer_orphaned_calls(tcs, tc_idx + 1, messages)
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
                    # caught (bounded, not disabled).
                    distinct_windows += 1
                    if distinct_windows >= READ_HEAVY_DISTINCT_WINDOWS:
                        print(f"   [parking: read-heavy after {distinct_windows} distinct windows]",
                              flush=True)
                        if worktree_dirty():
                            auto_wip_commit("read-heavy parking")
                        if not PARK_ENABLED:
                            messages.append({"role": "user", "content": (
                                "You've read many distinct files without "
                                "making an edit. STOP READING and make an "
                                "edit (create_file, str_replace, or "
                                "checkpoint) now.")})
                            recent_tools.clear()
                            _answer_orphaned_calls(tcs, tc_idx + 1, messages)
                            break
                        return 3
                    recent_tools.clear()

    print("[ended without done — step cap reached]", flush=True)
    if worktree_dirty():
        auto_wip_commit("step cap reached")
    return 2


_DONE_REASONS = {0: "done", 1: "error", 2: "parked", 3: "infra_failure"}


def main() -> int:
    """Run the agent loop, then drop a completion marker for the orchestrator.

    Written last, so its existence means the agent has genuinely finished. A
    marker failure never changes the run's exit code."""
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
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        tmp = CWD / ".agent_done.tmp"
        tmp.write_text(json.dumps(marker) + "\n", encoding="utf-8")
        os.replace(tmp, CWD / ".agent_done")
    except Exception as e:  # noqa: BLE001 - a marker failure must never mask the run's exit code
        print(f"[warn] .agent_done marker not written: {e}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
