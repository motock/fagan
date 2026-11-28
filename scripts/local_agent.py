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
import time  # noqa: F401 (kept: the moved loop phases read it via origin)
from collections import deque  # noqa: F401 (kept: the moved loop phases read it via origin)
from datetime import UTC, datetime
from pathlib import Path

import httpx  # noqa: F401 (kept: the moved loop phases read it via origin)

# Calibrated at runtime from ollama's measured prompt_eval_count; the fixed
# 4.0 chars/token guess was ~28% over NUM_CTX live (2026-07-29, gpt-oss).
# Set by _stream_one_turn on every ollama turn that reports a count (never by
# _provider_chat_turn, which returns only the bare message); lmstudio/mlx fall
# back to the fixed guess. Reset to None at the top of main() so calibration
# never leaks between dispatches (or in-process test runs).
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
    PersistingList,  # noqa: F401 (kept: the moved loop phases read it via origin)
    _answer_orphaned_calls,  # noqa: F401 (kept: the moved loop phases read it via origin)
    _dropped_span_digest,  # noqa: F401 (unused here; re-exported for test_local_agent_context_compaction.py)
    _dropped_top_level_defs,  # noqa: F401 (kept: moved run_tool_impl reads it via origin["_dropped_top_level_defs"])
    _dropped_top_level_vars,  # noqa: F401 (kept: moved run_tool_impl reads it via origin["_dropped_top_level_vars"])
    _is_context_overflow_error,  # noqa: F401 (kept: the moved loop phases read it via origin)
    _load_resume_transcript,  # noqa: F401 (kept: the moved loop phases read it via origin)
    _message_char_len,  # noqa: F401 (kept: moved chat cluster reads it via origin["_message_char_len"])
    _persist_messages,  # noqa: F401 (kept: the moved loop phases read it via origin)
    _repetition_nudge,  # noqa: F401 (kept: the moved loop phases read it via origin)
    _str_replace_not_found_diag,  # noqa: F401 (kept: moved run_tool_impl reads it via origin["_str_replace_not_found_diag"])
    _total_chars,  # noqa: F401 (kept: the moved loop phases read it via origin)
    _trim_resumed_transcript,  # noqa: F401 (kept: the moved loop phases read it via origin)
    destructive_git_op,  # noqa: F401 (kept: moved run_tool_impl reads it via origin["destructive_git_op"])
)
from pipeline.local_agent_common import (  # noqa: F401 (kept: moved recover_tool_calls_impl reads it via origin["_recover_tool_calls_shared"])
    recover_tool_calls as _recover_tool_calls_shared,
)
from pipeline.worker_scope import worker_namespace

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
    ENDPOINT,  # noqa: F401 (kept: the moved loop phases read it via origin)
    FULL_SUITE_DONE_BAR,  # noqa: F401 (kept: the moved loop phases read it via origin)
    HARNESS_RULES,  # noqa: F401 (kept: the moved loop phases read it via origin)
    MAX_STEPS,
    MODEL,  # noqa: F401 (kept: the moved loop phases read it via origin)
    MUTATING_TOOLS,  # noqa: F401 (kept: the moved loop phases read it via origin)
    NET_PROGRESS_MAX_STEPS,  # noqa: F401 (kept: the moved loop phases read it via origin)
    NO_TOOL_CAP,  # noqa: F401 (kept: the moved loop phases read it via origin)
    NUM_CTX,  # noqa: F401 (kept: the moved loop phases read it via origin)
    PARK_ENABLED,  # noqa: F401 (kept: the moved loop phases read it via origin)
    PROACTIVE_TRIM_THRESHOLD,  # noqa: F401 (kept: the moved loop phases read it via origin)
    PROVIDER,  # noqa: F401 (kept: the moved loop phases read it via origin)
    READ_HEAVY_DISTINCT_WINDOWS,  # noqa: F401 (kept: the moved loop phases read it via origin)
    READ_HEAVY_WINDOW,  # noqa: F401 (kept: the moved loop phases read it via origin)
    READ_SILENCE_SECONDS,  # noqa: F401 (kept: moved _stream_one_turn_impl reads it via origin["READ_SILENCE_SECONDS"])
    REWORK_FULL_SUITE,  # noqa: F401 (kept: the moved loop phases read it via origin)
    REWORK_SUITE_REJECT_CAP,  # noqa: F401 (kept: the moved loop phases read it via origin)
    SCRATCHPAD_NUDGE_STEPS,  # noqa: F401 (kept: the moved loop phases read it via origin)
    SCRATCHPAD_ON,  # noqa: F401 (kept: the moved loop phases read it via origin)
    STR_REPLACE_FAIL_ESCALATE_AFTER,  # noqa: F401 (kept: the moved loop phases read it via origin)
    TEMPERATURE,  # noqa: F401 (kept: moved _ollama_payload_impl/_provider_chat_turn_impl read it via origin)
    THINK,  # noqa: F401 (kept: moved _ollama_payload_impl reads it via origin["THINK"])
    TIMEOUT,  # noqa: F401 (kept: the moved loop phases read it via origin)
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
from scripts.local_agent_runstate import (  # noqa: F401 (re-exported: RH-10 moved these; run_tool_impl reads the sets via origin[...])
    _CREATED_THIS_RUN,
    _DONE_REASONS,
    _VIEWED_THIS_RUN,
    read_correlation_id,
)


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


def _step_cap_auto_done() -> bool:
    """See scripts/local_agent_git.py::_step_cap_auto_done_impl."""
    from scripts.local_agent_git import _step_cap_auto_done_impl
    return _step_cap_auto_done_impl(globals())


def _reject_done_for_suite(messages: list, step: int, suite_tail: str, gate: str | None) -> None:
    """L1: feed a full-suite failure back as a user turn and announce the rejection. Used at both `done`-rejection sites (clean tree, and the dirty-tree auto-accept escape) so the raised rework done-bar holds and the agent can't dodge it by interleaving dirty/clean done calls. The caller increments `suite_rejections` and `break`s out of the tool-call loop so the next step re-enters with this fed-back excerpt."""
    return _reject_done_for_suite_impl(globals(), messages, step, suite_tail, gate)


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
    """See scripts/local_agent_tools.py::safe_run_tool_impl (full contract)."""
    from scripts.local_agent_tools import safe_run_tool_impl
    return safe_run_tool_impl(globals(), fn, args)


def recover_from_oversized_5xx(messages, chat_fn, *, step=None):
    """See scripts/local_agent_recovery.py::recover_from_oversized_5xx_impl (full contract)."""
    from scripts.local_agent_recovery import recover_from_oversized_5xx_impl

    return recover_from_oversized_5xx_impl(globals(), messages, chat_fn, step=step)


def _main_impl() -> int:
    """The agent step loop; phases live in scripts/local_agent_loop.py."""
    from scripts.local_agent_loop import (
        chat_turn_impl,
        no_tool_turn_impl,
        startup_impl,
        step_cap_impl,
        step_guards_impl,
        tool_call_impl,
    )
    st = startup_impl(globals())
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

        for tc_idx, tc in enumerate(tcs):
            outcome = tool_call_impl(globals(), st, step, tcs, tc_idx, tc)
            if outcome == "break":
                break
            if outcome is not None:
                return outcome
    return step_cap_impl(globals())



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
