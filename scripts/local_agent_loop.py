"""Main-loop phases extracted from the local dispatch agent module (ART-03).

``_main_impl`` in scripts/local_agent.py used to hold the whole step loop.
Its phases live here, moved with their logic and messages unchanged: startup
and resume, the per-step budget guards, one chat turn (with context-overflow
and oversized-response recovery), the no-tool-call turn, and the per-tool-call
chain (argument decoding, the ``done`` gate, repetition tracking, mutating-tool
bookkeeping, the failing-str_replace guard, the bash off-task guard and the
read-heavy nudge).

Like scripts/local_agent_tools.py, every phase receives the agent module's
``globals()`` as ``origin`` and reads each agent-module-owned name as
``origin["NAME"]`` at call time, so a test that patches ``la.NAME`` is
observed. This module imports ONLY the stdlib: it must never import the agent
module or its config/guard modules (see the LA-TOOLS contract in
scripts/local_agent_tools.py).

The loop's local variables live in one ``LoopState`` passed to each phase.
The two calibration globals (``_measured_chars_per_token``,
``_last_prompt_eval_count``) stay module globals of the agent module and are
reset/read through ``origin``.

Return convention for the per-tool-call phases: ``None`` means carry on with
the next check, ``"break"`` means leave the tool-call loop (the old
``break``), and an ``int`` is the exit code ``_main_impl`` returns.
"""
from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

Outcome = int | Literal["break"] | None
BREAK: Literal["break"] = "break"


@dataclass
class LoopState:
    """Everything the step loop used to keep in local variables."""

    messages: Any
    transcript_path: str | None
    expected_paths: Any
    start_time: float
    seen: dict = field(default_factory=dict)
    nudged_repeat: bool = False
    nudged_read_heavy: bool = False
    recent_tools: deque = field(default_factory=deque)
    distinct_windows: int = 0
    done_rejections: int = 0
    # L1: separate counter for full-suite done-rejections on a CI-fail-rework
    # round. Kept distinct from done_rejections so it cannot trip the dirty-
    # tree auto-accept-at-2 logic (a failing suite must NEVER be auto-
    # accepted). Bounded by REWORK_SUITE_REJECT_CAP: once the agent has failed
    # to green the suite that many times the run parks (return 2) rather than
    # burning the rest of the step/wall-clock budget re-prompting a model that
    # cannot make progress.
    suite_rejections: int = 0
    consecutive_no_tool: int = 0
    # Failing-str_replace loop guard: consecutive FAILED str_replace per path;
    # sr_fail_since_nudge counts further failures AFTER the nudge so a model
    # that ignores it parks instead of getting the one nudge for the run.
    failed_sr: dict = field(default_factory=dict)
    nudged_sr_fail: set = field(default_factory=set)
    sr_fail_since_nudge: dict = field(default_factory=dict)
    last_progress_step: int = 0
    last_scratchpad_step: int = 0
    # Off-task-drift guard (Mode 31): every distinct mutated path that
    # _is_off_task_path flags (informational — see _off_task_step for the
    # actual escalation rule).
    off_task_targets: set = field(default_factory=set)
    nudged_off_task: bool = False
    # Same-path edit-churn guard state (see _churn_step).
    churn_state: dict = field(
        default_factory=lambda: {"path": "", "count": 0, "nudged": False})


def startup_impl(origin: dict) -> LoopState:
    """Reset calibration, print the boot heartbeat, build/resume the transcript."""
    # A fresh dispatch has no calibration data yet - clear any value left
    # over from a prior dispatch that shared this process (or, in-process,
    # a prior test) so it never leaks in.
    origin["_measured_chars_per_token"] = None
    origin["_last_prompt_eval_count"] = None
    # Startup heartbeat. flush=True is load-bearing: stdout is block-buffered
    # to a non-tty file (the worktree's agent.log, redirected by Popen), so
    # without flush the line wouldn't hit disk until the buffer fills or the
    # process exits. With it, check_story_status's "empty agent.log = failed
    # launch" check has a precise signal: a 0-byte log means the process
    # never reached main() (a genuine failed launch), while a log with [boot]
    # and no further output means the agent is alive and queued (e.g. on
    # Ollama's -np 1 worker waiting for an inference slot).
    print(
        f"[boot] pid={os.getpid()} model={origin['MODEL']} endpoint={origin['ENDPOINT']} "
        f"provider={origin['PROVIDER']} steps={origin['MAX_STEPS']} timeout={origin['TIMEOUT']}s",
        flush=True,
    )
    system = os.environ.get("LOCAL_AGENT_SYSTEM", "").strip()
    task = os.environ.get("LOCAL_AGENT_TASK", "")
    expected_paths = origin["_expected_task_paths"](task)
    # Resume path: if LOCAL_AGENT_RESUME_TRANSCRIPT_PATH points at a valid
    # transcript, load it instead of building the fresh system/task pair (the
    # loaded transcript already contains the original system+task prompt).
    # LOCAL_AGENT_TRANSCRIPT_PATH (persistence) is independent of resume.
    transcript_path = os.environ.get("LOCAL_AGENT_TRANSCRIPT_PATH")
    messages = origin["PersistingList"](transcript_path=transcript_path)
    if resume := origin["_load_resume_transcript"]():
        # Reserve headroom below NUM_CTX for the resume-append content (e.g.
        # reviewer feedback), the model's own generation, and the rough
        # imprecision of the chars/token estimate - trimming to the exact
        # limit would still overflow once more content is added below. No
        # live calibration exists yet this early (reset above, before any
        # turn has run), so this always uses the fixed guess via
        # _effective_chars_per_token()'s fallback.
        budget_chars = int(origin["NUM_CTX"] * origin["_effective_chars_per_token"]() * 0.75)
        resume = origin["_trim_resumed_transcript"](resume, budget_chars)
        messages.extend(resume)
    else:
        system_content = origin["HARNESS_RULES"] + ("\n\n" + system if system else "")
        messages.extend([{"role": "system", "content": system_content},
                         {"role": "user", "content": task}])
    # When resuming, optionally append exactly one new user turn as a
    # continuation (e.g. reviewer feedback) instead of re-injecting the
    # original system/task prompt.
    resume_append = os.environ.get("LOCAL_AGENT_RESUME_APPEND_CONTENT")
    if resume and resume_append:
        messages.append({"role": "user", "content": resume_append})

    origin["exclude_runtime_artifacts"]()

    return LoopState(
        messages=messages,
        transcript_path=transcript_path,
        expected_paths=expected_paths,
        start_time=origin["time"].monotonic(),
        recent_tools=deque(maxlen=origin["READ_HEAVY_WINDOW"]),
    )


def _park_if_dirty(origin: dict, reason: str) -> None:
    if origin["worktree_dirty"]():
        origin["auto_wip_commit"](reason)


def step_guards_impl(origin: dict, st: LoopState, step: int) -> int | None:
    """Wall-clock, net-progress and scratchpad checks at the top of a step."""
    timeout = origin["TIMEOUT"]
    if origin["time"].monotonic() - st.start_time > timeout:
        print(f"[step {step}] wall-clock timeout ({timeout}s) reached; parking", flush=True)
        _park_if_dirty(origin, "wall-clock timeout")
        return 2
    if step - st.last_progress_step >= origin["NET_PROGRESS_MAX_STEPS"]:
        print(f"[step {step}] no successful edit in {step - st.last_progress_step} steps "
              f"(last progress at step {st.last_progress_step}); parking", flush=True)
        _park_if_dirty(origin, "no net progress")
        if not origin["PARK_ENABLED"]:
            st.messages.append({"role": "user", "content": (
                f"You have made no successful file edit in the last "
                f"{step - st.last_progress_step} steps. Stop investigating "
                f"and make ONE concrete change now: str_replace/"
                f"replace_lines to fix something specific, restore_file "
                f"if a file's edits went wrong and you want to restart "
                f"it clean, or checkpoint if you need to save partial "
                f"progress before continuing.")})
            st.last_progress_step = step
        else:
            return 3
    if origin["SCRATCHPAD_ON"] and step - st.last_scratchpad_step >= origin["SCRATCHPAD_NUDGE_STEPS"]:
        print(f"[step {step}] no scratchpad update in "
              f"{step - st.last_scratchpad_step} steps; nudging", flush=True)
        st.messages.append({"role": "user", "content": (
            "You haven't updated .agent_scratchpad.md in a while. "
            "Before your next tool call, str_replace it with a short "
            "running summary of what you've done and what's next "
            "(create_file first if it doesn't exist yet).")})
        st.last_scratchpad_step = step
    return None


def _llm_error_exit(origin: dict) -> int:
    _park_if_dirty(origin, "llm error")
    return 1


def chat_turn_impl(origin: dict, st: LoopState, step: int) -> tuple[dict | None, int | None]:
    """One chat turn: returns (message, None), or (None, exit_code) on failure."""
    httpx = origin["httpx"]
    chat = origin["chat"]
    _is_context_overflow_error = origin["_is_context_overflow_error"]
    try:
        m = chat(st.messages)
    except httpx.HTTPStatusError as e:
        if not _is_context_overflow_error(e):
            print(f"[step {step}] LLM call failed: {e}", flush=True)
            return None, _llm_error_exit(origin)
        m, rc = _recover_overflow(origin, st, step, e)
        if rc is not None:
            return None, rc
    except Exception as e:  # noqa: BLE001 - an LLM backend call can fail in unpredictable ways; must not crash the agent loop
        print(f"[step {step}] LLM call failed: {e}", flush=True)
        return None, _llm_error_exit(origin)
    st.messages.append(m)
    _proactive_trim(origin, st, step)
    return m, None


def _recover_overflow(origin: dict, st: LoopState, step: int, e: Exception) -> tuple[dict | None, int | None]:
    """Escalate a context-overflow HTTP error through the shrinking-budget retry.

    Classified by BODY, not just status: LM Studio rejects an oversized prompt
    with a 400. A 5xx that survived chat()'s own retries is very likely a
    context-window overflow, so retry with a shrinking context budget before
    giving up. The helper only catches httpx.HTTPStatusError and a sibling
    except clause does NOT catch exceptions raised from inside another except
    handler, so ANY failure during escalation is caught here and gives up
    gracefully (exit 1) instead of crashing the loop.
    """
    try:
        m = origin["recover_from_oversized_5xx"](st.messages, origin["chat"], step=step)
    except Exception as e2:  # noqa: BLE001 - an LLM backend call during escalation can fail in unpredictable ways; must not crash the agent loop
        print(f"[step {step}] LLM call failed during 5xx escalation: {e2}", flush=True)
        return None, _llm_error_exit(origin)
    if m is None:
        print(f"[step {step}] LLM call failed after trim-retry: {e}", flush=True)
        return None, _llm_error_exit(origin)
    return m, None


def _proactive_trim(origin: dict, st: LoopState, step: int) -> None:
    """Once a turn's measured prompt_eval_count is already close to NUM_CTX,
    shrink the transcript now rather than waiting for the next turn to overflow
    and 500 - the reactive path is the backstop for when no measurement exists
    yet. No-ops when _trim_resumed_transcript finds nothing to drop."""
    num_ctx = origin["NUM_CTX"]
    threshold = origin["PROACTIVE_TRIM_THRESHOLD"]
    count = origin["_last_prompt_eval_count"]
    if count is None or count < num_ctx * threshold:
        return
    budget_chars = int(num_ctx * origin["_effective_chars_per_token"]() * 0.75)
    trimmed = origin["_trim_resumed_transcript"](st.messages, budget_chars)
    if origin["_total_chars"](trimmed) < origin["_total_chars"](st.messages):
        print(f"[step {step}] measured prompt_eval_count="
              f"{count} >= {threshold:.0%} of "
              f"NUM_CTX={num_ctx}; trimming proactively", flush=True)
        st.messages[:] = trimmed
        origin["_persist_messages"](st.messages, st.transcript_path)
        origin["_last_prompt_eval_count"] = None


def no_tool_turn_impl(origin: dict, st: LoopState, step: int, m: dict) -> int | None:
    """A turn with no tool call: count it, park at the narration cap, else nudge."""
    cap = origin["NO_TOOL_CAP"]
    st.consecutive_no_tool += 1
    print(f"[step {step}] no tool call ({st.consecutive_no_tool} consecutive): "
          f"{(m.get('content') or '')[:100]!r}", flush=True)
    if st.consecutive_no_tool >= cap:
        print(f"[step {step}] narration cap ({cap} consecutive "
              f"no-tool turns) reached; parking", flush=True)
        _park_if_dirty(origin, "narration cap")
        return 2
    st.messages.append({"role": "user", "content": origin["_no_tool_nudge"](
        st.consecutive_no_tool, content=m.get("content", ""))})
    return None


def _decode_args(args: Any) -> Any:
    if isinstance(args, str):
        try:
            return json.loads(args)
        except Exception:  # noqa: BLE001 - malformed tool-call args from the model fall back to empty args rather than crashing the loop
            return {}
    return args


def _done_dirty(origin: dict, st: LoopState, step: int, tcs: list, tc_idx: int, args: dict) -> Outcome:
    st.done_rejections += 1
    if st.done_rejections >= 2:
        # L1: the dirty-tree auto-accept escape must not bypass the raised
        # rework done-bar. Without this gate an agent could dodge it by
        # interleaving dirty/clean done calls (dirty->reject, commit,
        # clean+failing-suite->reject, dirty->reject->auto-accept with the
        # suite never checked). On a rework round, check the suite first; a
        # failure WIP-commits (so the next attempt starts clean) and rejects
        # instead of auto-accepting. Bounded by MAX_STEPS.
        if origin["REWORK_FULL_SUITE"] or origin["FULL_SUITE_DONE_BAR"]:
            suite_ok, suite_tail, gate = origin["_full_suite_result"]()
            if not suite_ok:
                st.suite_rejections += 1
                origin["auto_wip_commit"]("commit enforcement")
                cap = origin["REWORK_SUITE_REJECT_CAP"]
                if st.suite_rejections >= cap:
                    print(f"[step {step}] rework suite-reject cap "
                          f"({cap}) reached; agent "
                          f"cannot green the full suite — parking", flush=True)
                    return 2
                origin["_reject_done_for_suite"](st.messages, step, suite_tail, gate)
                origin["_answer_orphaned_calls"](tcs, tc_idx + 1, st.messages)
                return BREAK
        origin["auto_wip_commit"]("commit enforcement")
        print(f"[step {step}] DONE with auto-WIP-commit (agent left tree dirty): "
              f"{args.get('summary', '')}", flush=True)
        return 0
    print(f"[step {step}] done rejected — worktree dirty, asking agent to commit", flush=True)
    st.messages.append({"role": "user", "content": (
        "You have uncommitted changes. Commit your work with git "
        "(git add -A && git commit -m ...) before calling done.")})
    origin["_answer_orphaned_calls"](tcs, tc_idx + 1, st.messages)
    return BREAK


def _done_clean(origin: dict, st: LoopState, step: int, tcs: list, tc_idx: int, args: dict) -> Outcome:
    # L1: on a CI-fail-rework round, the reviewer was acceptance-scoped and
    # never saw the agent's own test - so a clean worktree + `done` is not
    # sufficient. Require the FULL suite green; otherwise feed the failing
    # excerpt back and reject done so the agent fixes its own broken assertion
    # (or the step cap binds). Non-rework dispatches skip this gate entirely.
    if origin["REWORK_FULL_SUITE"] or origin["FULL_SUITE_DONE_BAR"]:
        suite_ok, suite_tail, gate = origin["_full_suite_result"]()
        if not suite_ok:
            st.suite_rejections += 1
            cap = origin["REWORK_SUITE_REJECT_CAP"]
            if st.suite_rejections >= cap:
                _park_if_dirty(origin, "rework suite-reject cap")
                print(f"[step {step}] rework suite-reject cap "
                      f"({cap}) reached; agent "
                      f"cannot green the full suite — parking", flush=True)
                return 2
            origin["_reject_done_for_suite"](st.messages, step, suite_tail, gate)
            origin["_answer_orphaned_calls"](tcs, tc_idx + 1, st.messages)
            return BREAK
    emit_step_line = origin["emit_step_line"]
    emit_step_line(
        step,
        f"DONE: {args.get('summary', '')}",
        correlation_id=origin["read_correlation_id"](),
    )
    return 0


def _done_gate(origin: dict, st: LoopState, step: int, tcs: list, tc_idx: int, args: dict) -> Outcome:
    if origin["worktree_dirty"]():
        return _done_dirty(origin, st, step, tcs, tc_idx, args)
    return _done_clean(origin, st, step, tcs, tc_idx, args)


def _tool_signature(fn: str, args: dict) -> tuple:
    """The repetition-guard key for a call.

    view_file is range-aware: different regions of one large file are not
    repetition. Both bounds given bucket into the 200-line window (an
    off-by-one retry still counts as the same target); a one-sided read is
    unbounded (to EOF, or from line 1), so its one bound identifies the region
    exactly; a bare call keeps the path-only key, since re-issuing it is
    always a true repeat."""
    if fn != "view_file":
        return (fn, args.get("path") or args.get("command") or args.get("old_str", ""))
    ls, le = args.get("line_start"), args.get("line_end")
    if ls is None and le is None:
        return (fn, args.get("path"))
    if isinstance(ls, int) and isinstance(le, int):
        return (fn, args.get("path"), ls // 200, le // 200)
    return (fn, args.get("path"), ls, le)


def _note_repeat(st: LoopState, fn: str, sig: tuple) -> bool:
    """Count the call and report whether it crossed the repetition threshold.

    str_replace and replace_lines calls are excluded from the per-target
    repetition guard: each one produces a *different* file state, so a
    sequence of edits to the same file is a legitimate fix-build cycle, not a
    repetition. The read-heavy guard (MUTATING_TOOLS) still catches a model
    stuck in a bad edit loop, and the failing-str_replace guard catches the
    no-match loop this exclusion would otherwise hide."""
    if fn in ("str_replace", "replace_lines"):
        return False
    st.seen[sig] = st.seen.get(sig, 0) + 1
    return st.seen[sig] >= 3


def _repetition_trip(origin: dict, st: LoopState, tcs: list, tc_idx: int) -> Outcome:
    first_trip = not st.nudged_repeat
    st.nudged_repeat = True
    print("   [repetition nudge]" if first_trip
          else "   [parking: repeated action after nudge]", flush=True)
    # Every trip answers the triggering tool_calls entry with a `tool`-role
    # message, not just the first: leaving a later trip's call unanswered
    # orphans it for the rest of the run, which live (Mode 33, 2026-07-22)
    # correlated directly with gpt-oss:20b's Harmony-format output degrading
    # into leaked special tokens. Re-delivering the guidance every time also
    # gives the model a fresh chance to self-correct.
    st.messages.append({"role": "tool", "content": origin["_repetition_nudge"]()})
    if first_trip:
        origin["_answer_orphaned_calls"](tcs, tc_idx + 1, st.messages)
        return BREAK
    _park_if_dirty(origin, "parked on repetition")
    if not origin["PARK_ENABLED"]:
        # Suppressed: let the nudge steer and continue to the next step
        # instead of terminating. The step cap bounds the run.
        origin["_answer_orphaned_calls"](tcs, tc_idx + 1, st.messages)
        return BREAK
    return 3


def _off_task_guard(origin: dict, st: LoopState, path: str) -> int | None:
    off_action, st.nudged_off_task = origin["_off_task_step"](
        path, st.expected_paths, st.off_task_targets, st.nudged_off_task)
    if off_action != "none":
        escalated = origin["_apply_off_task_action"](off_action, path, st.messages)
        if escalated:
            if origin["PARK_ENABLED"]:
                return 3
            st.messages.append({"role": "user", "content": (
                f"You are still touching {path}, outside your "
                f"assigned scope, after already being told to stop. "
                f"Refocus on the files named in your instructions "
                f"now.")})
    return None


def _churn_guard(origin: dict, st: LoopState, path_arg: str) -> int | None:
    limit = origin["CHURN_SAME_PATH_MAX_EDITS"]
    churn_action = origin["_churn_step"](path_arg, st.churn_state)
    if churn_action == "nudge":
        print(f"   [churn nudge: {limit}+ edits to "
              f"{path_arg} with no test run]", flush=True)
        st.messages.append({"role": "user", "content": (
            f"You've made {limit} edits to "
            f"{path_arg} in a row without running its "
            f"tests. Run "
            f"`pytest -q <the test file for {path_arg}>` (or the "
            f"project's full test command) now to check whether "
            f"your changes actually work, before making another "
            f"edit.")})
    elif churn_action == "escalate":
        print(f"   [parking: churn on {path_arg} continues with "
              f"no test run]", flush=True)
        _park_if_dirty(origin, "parked on edit churn")
        if origin["PARK_ENABLED"]:
            return 3
        st.messages.append({"role": "user", "content": (
            f"You are still editing {path_arg} without running its "
            f"tests. STOP editing and run the tests now — if they "
            f"fail, read the failure and fix the ROOT cause; if "
            f"they pass, call done.")})
    return None


def _mutation_bookkeeping(origin: dict, st: LoopState, step: int, fn: str, args: dict, tool_result: Any) -> int | None:
    """After a mutating tool call.

    A GENUINELY SUCCESSFUL mutation (a result that isn't an "ERROR..." string —
    a REJECTED call changed nothing on disk and must not be treated as
    progress) resets every OTHER signature's accumulated count: `seen` was a
    lifetime cumulative counter, so re-viewing a file 2x, editing it, then
    viewing it again hit the >=3 threshold from stale pre-edit reads. A real
    edit invalidates prior reads' staleness, and the guards that staleness
    armed, so the counts start over."""
    if not (isinstance(tool_result, str) and not tool_result.startswith("ERROR")):
        return None
    st.seen.clear()
    st.last_progress_step = step
    if str(args.get("path", "")) .endswith(".agent_scratchpad.md"):
        st.last_scratchpad_step = step
    st.nudged_repeat = False
    st.nudged_read_heavy = False
    st.distinct_windows = 0

    path_arg = args.get("path", "")
    if not path_arg:
        return None
    rc = _off_task_guard(origin, st, path_arg)
    if rc is not None:
        return rc
    return _churn_guard(origin, st, path_arg)


def _str_replace_guard(origin: dict, st: LoopState, args: dict, tool_result: Any) -> int | None:
    """Failing-str_replace loop guard.

    After 2 consecutive FAILED str_replace on the same path, steer to
    replace_lines (line numbers, no byte-exact match). A successful
    str_replace resets the counter and re-arms the nudge. If the model ignores
    the nudge and keeps failing on the same path, park after
    STR_REPLACE_FAIL_ESCALATE_AFTER more failures."""
    sr_path = args.get("path", "")
    if not (isinstance(tool_result, str) and tool_result.startswith("ERROR")):
        st.failed_sr[sr_path] = 0
        st.nudged_sr_fail.discard(sr_path)
        st.sr_fail_since_nudge.pop(sr_path, None)
        return None
    st.failed_sr[sr_path] = st.failed_sr.get(sr_path, 0) + 1
    if st.failed_sr[sr_path] < 2:
        return None
    if sr_path not in st.nudged_sr_fail:
        st.nudged_sr_fail.add(sr_path)
        print(f"   [str_replace-fail nudge: {st.failed_sr[sr_path]} "
              f"failed matches on {sr_path}]", flush=True)
        st.messages.append({"role": "user", "content": (
            f"Your last {st.failed_sr[sr_path]} str_replace calls on {sr_path} "
            f"did not match — you cannot construct a matching old_str (likely "
            f"a whitespace difference). STOP retrying str_replace on this file. "
            f"Run `nl -ba {sr_path} | sed -n '<start>,<end>p'` to get exact line "
            f"numbers, then use replace_lines(path, start, end, new_str) which "
            f"needs no byte-exact old_str.")})
        return None
    st.sr_fail_since_nudge[sr_path] = st.sr_fail_since_nudge.get(sr_path, 0) + 1
    if st.sr_fail_since_nudge[sr_path] < origin["STR_REPLACE_FAIL_ESCALATE_AFTER"]:
        return None
    st.sr_fail_since_nudge[sr_path] = 0
    print(f"   [parking: str_replace still failing on {sr_path} "
          f"after nudge]", flush=True)
    _park_if_dirty(origin, "parked on failing str_replace")
    if origin["PARK_ENABLED"]:
        return 3
    st.messages.append({"role": "user", "content": (
        f"You are still retrying str_replace on {sr_path} after "
        f"being told to switch. Use replace_lines(path, start, "
        f"end, new_str) now — do not attempt str_replace on this "
        f"file again.")})
    return None


def _bash_guard(origin: dict, st: LoopState, args: dict) -> int | None:
    """Bash-mutation off-task detection (Mode 31 follow-up).

    A formatter/linter --fix, sed/perl -i, or shell redirection can mutate a
    file directly through `bash`, bypassing the file-tool path the guard
    watches. Route any detected off-task write through the same escalation.
    Also: a `pytest` bash call is the churn guard's own "the model IS checking
    its work" signal, independent of whether this call named an off-task path."""
    cmd = args.get("command", "")
    origin["_churn_note_test_run"](cmd, st.churn_state)
    off_path = origin["_bash_off_task_path"](cmd, st.expected_paths)
    if off_path:
        return _off_task_guard(origin, st, off_path)
    return None


def _read_heavy_guard(origin: dict, st: LoopState, tcs: list, tc_idx: int, fn: str, sig: tuple) -> Outcome:
    """Read-heavy-pattern guard.

    Tracked separately from the per-target repetition guard, which misses the
    case where every call hits a different file/command. The model is in a
    paralysis-by-analysis loop if the last READ_HEAVY_WINDOW tool calls were
    all non-mutating. Two-stage response: one corrective nudge, then park."""
    window = origin["READ_HEAVY_WINDOW"]
    mutating = origin["MUTATING_TOOLS"]
    st.recent_tools.append((fn, sig[1]))
    if not (len(st.recent_tools) == window
            and all(f not in mutating for (f, _t) in st.recent_tools)):
        return None
    distinct_targets = len({_t for (_f, _t) in st.recent_tools})
    has_repetition = distinct_targets < window
    if not st.nudged_read_heavy:
        st.nudged_read_heavy = True
        print(f"   [read-heavy nudge: {window} reads in a row]", flush=True)
        st.messages.append({"role": "user", "content": (
            f"You've made {window} tool calls in a row without writing "
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
        st.recent_tools.clear()
        st.distinct_windows = 0
        return None
    if has_repetition:
        # Re-reading an already-seen target after being nudged to act is
        # wedging, not exploration. Park at the strict 2 * READ_HEAVY_WINDOW.
        print("   [parking: read-heavy after nudge]", flush=True)
        _park_if_dirty(origin, "read-heavy parking")
        if not origin["PARK_ENABLED"]:
            # Same class of bug as the per-target guard (Mode 33): a silent
            # break here means the model gets no renewed feedback on any
            # window after the first nudge.
            st.messages.append({"role": "user", "content": (
                "You're still re-reading targets you've already "
                "seen instead of acting. STOP READING and make an "
                "edit (create_file, str_replace, or checkpoint) "
                "now.")})
            st.recent_tools.clear()
            origin["_answer_orphaned_calls"](tcs, tc_idx + 1, st.messages)
            return BREAK
        return 3
    # All-distinct reads after the nudge: the model is exploring multiple
    # files (each read once), not wedged on one. A multi-file bug fix
    # legitimately needs more than READ_HEAVY_WINDOW reads to orient before its
    # first edit. Let it continue, bounded by READ_HEAVY_DISTINCT_WINDOWS
    # all-distinct windows (bounded, not disabled).
    st.distinct_windows += 1
    if st.distinct_windows >= origin["READ_HEAVY_DISTINCT_WINDOWS"]:
        print(f"   [parking: read-heavy after {st.distinct_windows} distinct windows]",
              flush=True)
        _park_if_dirty(origin, "read-heavy parking")
        if not origin["PARK_ENABLED"]:
            st.messages.append({"role": "user", "content": (
                "You've read many distinct files without "
                "making an edit. STOP READING and make an "
                "edit (create_file, str_replace, or "
                "checkpoint) now.")})
            st.recent_tools.clear()
            origin["_answer_orphaned_calls"](tcs, tc_idx + 1, st.messages)
            return BREAK
        return 3
    st.recent_tools.clear()
    return None


def tool_call_impl(origin: dict, st: LoopState, step: int, tcs: list, tc_idx: int, tc: dict) -> Outcome:
    """Run one tool call through the done gate and every per-call guard."""
    fn = tc["function"]["name"]
    args = _decode_args(tc["function"]["arguments"])

    if fn == "done":
        return _done_gate(origin, st, step, tcs, tc_idx, args)

    sig = _tool_signature(fn, args)
    above_threshold = _note_repeat(st, fn, sig)
    print(f"[step {step}] {fn}: {str(args.get('command') or args.get('path') or '')[:120]}", flush=True)
    if above_threshold:
        return _repetition_trip(origin, st, tcs, tc_idx)

    # A malformed tool call (e.g. a model that omits a required arg like
    # str_replace without old_str) must nudge the model with a recoverable
    # error, not crash the whole unattended agent with an uncaught exception.
    tool_result = origin["safe_run_tool"](fn, args)
    st.messages.append({"role": "tool", "content": tool_result})

    if fn in origin["MUTATING_TOOLS"]:
        rc = _mutation_bookkeeping(origin, st, step, fn, args, tool_result)
        if rc is not None:
            return rc
    if fn == "str_replace":
        rc = _str_replace_guard(origin, st, args, tool_result)
        if rc is not None:
            return rc
    if fn == "bash":
        rc = _bash_guard(origin, st, args)
        if rc is not None:
            return rc
    return _read_heavy_guard(origin, st, tcs, tc_idx, fn, sig)


def step_cap_impl(origin: dict) -> int:
    """The loop ran out of steps: commit first (the auto-done check diffs HEAD
    against the default branch, so uncommitted work must be in HEAD before it
    is measured), then auto-done if the work already landed."""
    emit_step_line = origin["emit_step_line"]
    _step_cap_auto_done = origin["_step_cap_auto_done"]
    auto_wip_commit = origin["auto_wip_commit"]
    if origin["worktree_dirty"]():
        auto_wip_commit("step cap reached")
    if _step_cap_auto_done():
        emit_step_line(
            origin["MAX_STEPS"],
            "DONE: step cap reached with the work already landed - "
            "production changes present and the full suite and lint pass",
            correlation_id=origin["read_correlation_id"](),
        )
        return 0
    print("[ended without done — step cap reached]", flush=True)
    return 2
