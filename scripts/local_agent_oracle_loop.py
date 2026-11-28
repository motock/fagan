"""Main-loop phases extracted from the oracle dispatch agent module (ART-04).

``_main_impl`` in scripts/local_agent_oracle.py used to hold the whole step
loop. Its phases live here, moved with their logic and messages unchanged:
startup and resume, the missing-fixture / oracle-snapshot preflight, the
per-step budget guards, one chat turn (with context-overflow recovery), the
no-tool-call turn, and the per-tool-call chain (the oracle ``done`` gate,
repetition tracking, mutating-tool bookkeeping, the failing-str_replace nudge,
the read-heavy nudge and the harness-decided ``finish_if_green`` check).

Same shape as scripts/local_agent_loop.py: every phase receives the oracle
module's ``globals()`` as ``origin`` and reads each oracle-module-owned name
as ``origin["NAME"]`` at call time, so a test that patches ``lao.NAME`` is
observed. The loop state is the plain agent's ``LoopState`` and the phases
that are identical to the plain agent's (``_decode_args``, ``_tool_signature``,
``_note_repeat``, ``_proactive_trim``) are imported from it. Everything the
oracle does differently (``auto_commit`` rather than ``auto_wip_commit``, no
scratchpad / off-task / churn guards, the oracle-gated ``done``) has its own
version here. This module never imports the oracle module itself.

Return convention for the per-tool-call phases matches local_agent_loop.py:
``None`` carries on, ``BREAK`` leaves the tool-call loop, an ``int`` is the exit
code ``_main_impl`` returns.
"""
from __future__ import annotations

import os
from collections import deque
from typing import Any

from scripts.local_agent_loop import (
    BREAK,
    LoopState,
    Outcome,
    _decode_args,
    _note_repeat,
    _proactive_trim,
    _tool_signature,
)


def _park_if_dirty(origin: dict, reason: str) -> None:
    if origin["worktree_dirty"]():
        origin["auto_commit"](reason)


def startup_impl(origin: dict) -> LoopState:
    """Reset calibration, print the boot heartbeat, build/resume the transcript."""
    # A fresh dispatch has no calibration data yet - clear any value left
    # over from a prior dispatch that shared this process (or, in-process, a
    # prior test). Ported from local_agent.py; keep both copies in sync.
    origin["_measured_chars_per_token"] = None
    origin["_last_prompt_eval_count"] = None
    print(
        f"[boot] pid={os.getpid()} model={origin['MODEL']} endpoint={origin['ENDPOINT']} "
        f"provider={origin['PROVIDER']} steps={origin['MAX_STEPS']} timeout={origin['TIMEOUT']}s",
        flush=True,
    )
    system = os.environ.get("LOCAL_AGENT_SYSTEM", "").strip()
    task = os.environ.get("LOCAL_AGENT_TASK", "")
    # Resume path: if LOCAL_AGENT_RESUME_TRANSCRIPT_PATH points at a valid
    # transcript, load it instead of building the fresh system/task pair (the
    # loaded transcript already contains the original system+task prompt).
    # LOCAL_AGENT_TRANSCRIPT_PATH (persistence) is independent of resume — a
    # dispatch can persist without resuming, resume without persisting, or both.
    transcript_path = os.environ.get("LOCAL_AGENT_TRANSCRIPT_PATH")
    messages = origin["PersistingList"](transcript_path=transcript_path)
    if resume := origin["_load_resume_transcript"]():
        # No live calibration exists yet this early (reset above, before any
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
    return LoopState(
        messages=messages,
        transcript_path=transcript_path,
        expected_paths=None,
        start_time=0.0,
        recent_tools=deque(maxlen=origin["READ_HEAVY_WINDOW"]),
    )


def preflight_impl(origin: dict, st: LoopState) -> int | None:
    """Missing-fixture check, oracle snapshot, runtime-artifact exclusion.

    A missing fixture means the oracle cannot grade this story at all. End the
    run before the first model call with exit 1 (LLM-call-failed path): it
    writes no commit, and the done marker grades the story as failed rather
    than parking or re-dispatching it (no step/infra retry can restore a file
    the checkout dropped). No new exit code.
    """
    acceptance_paths = origin["ACCEPTANCE_PATHS"]
    missing = origin["missing_acceptance_fixtures"]() if acceptance_paths else []
    if missing:
        print(f"[boot] acceptance fixture missing from the working copy: {', '.join(missing)}",
              flush=True)
        return 1

    if not acceptance_paths:
        print("[local_agent_oracle] WARNING: no LOCAL_AGENT_ACCEPTANCE; "
              "this variant should not have been launched. Falling back to "
              "no-oracle behavior — done is honored on clean worktree + model `done`.", flush=True)
    else:
        origin["_capture_oracle_snapshot"]()

    origin["exclude_runtime_artifacts"]()

    # Mirror local_agent.py: cap full-suite done-rejections on a rework round so
    # a model that cannot green the suite parks rather than burning the whole
    # budget alternating `done` with narration. See REWORK_SUITE_REJECT_CAP.
    origin["_reset_suite_rejections"]()
    st.start_time = origin["time"].monotonic()
    return None


def step_guards_impl(origin: dict, st: LoopState, step: int) -> int | None:
    """Wall-clock and net-progress checks at the top of a step."""
    timeout = origin["TIMEOUT"]
    if origin["time"].monotonic() - st.start_time > timeout:
        print(f"[step {step}] wall-clock timeout ({timeout}s) reached; parking", flush=True)
        _park_if_dirty(origin, "WIP (wall-clock timeout)")
        return 2
    if step - st.last_progress_step >= origin["NET_PROGRESS_MAX_STEPS"]:
        print(f"[step {step}] no successful edit in {step - st.last_progress_step} steps "
              f"(last progress at step {st.last_progress_step}); parking", flush=True)
        _park_if_dirty(origin, "WIP (no net progress)")
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
    return None


def _llm_error_exit(origin: dict) -> int:
    _park_if_dirty(origin, "WIP (llm error)")
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
    with a 400, and a 5xx that survived chat()'s own retries is very likely a
    context-window overflow. The helper only catches httpx.HTTPStatusError, and
    a sibling except clause does NOT catch exceptions raised from inside another
    except handler, so ANY failure during escalation is caught here and gives up
    gracefully (exit 1) instead of crashing the loop. Ported from local_agent.py
    - keep both copies in sync.
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


def no_tool_turn_impl(origin: dict, st: LoopState, step: int, m: dict) -> int | None:
    """A turn with no tool call: count it, park at the narration cap, else nudge."""
    cap = origin["NO_TOOL_CAP"]
    st.consecutive_no_tool += 1
    print(f"[step {step}] no tool call ({st.consecutive_no_tool} consecutive): "
          f"{(m.get('content') or '')[:100]!r}", flush=True)
    if st.consecutive_no_tool >= cap:
        print(f"[step {step}] narration cap ({cap} consecutive "
              f"no-tool turns) reached; parking", flush=True)
        _park_if_dirty(origin, "WIP (narration cap)")
        return 2
    st.messages.append({"role": "user", "content": origin["_no_tool_nudge"](
        st.consecutive_no_tool, content=m.get("content", ""))})
    return None


def _done_suite_gate(origin: dict, st: LoopState, step: int) -> Outcome:
    """L1: on a CI-fail-rework round the model can dodge the raised done-bar
    that finish_if_green enforces by calling `done` directly (observed
    2026-07-18: gpt-oss called done on round 3 with its own pasted pytest
    showing 3 failed, oracle green, done accepted - bypassed the gate). Close
    the bypass: require the full suite green here too, else feed the failing
    excerpt back and reject. Cold-start dispatches skip this unless
    FULL_SUITE_DONE_BAR arms it."""
    if not (origin["REWORK_FULL_SUITE"] or origin["FULL_SUITE_DONE_BAR"]):
        return None
    full_ok, full_tail, gate = origin["_full_suite_result"]()
    if full_ok:
        return None
    st.suite_rejections += 1
    cap = origin["REWORK_SUITE_REJECT_CAP"]
    if st.suite_rejections >= cap:
        _park_if_dirty(origin, "wip: rework suite-reject cap")
        print(f"[step {step}] rework suite-reject cap "
              f"({cap}) reached; agent "
              f"cannot green the full suite — parking", flush=True)
        return 2
    if gate == 'lint':
        print(f"[step {step}] done rejected — lint gate still "
              f"fails (rework done-bar); asking agent to fix the "
              f"lint failure", flush=True)
        st.messages.append({"role": "user", "content": (
            f"Your tests PASS, but the lint check (`ruff check .`) fails. The merge-gate CI lint gate will reject this on the same failure:\n{full_tail}\n\nMost lint errors are auto-fixable: run `ruff check . --fix`, then `ruff check .` to confirm it is clean.\n\nDo NOT edit implementation logic — this is a formatting/import/style error, not a correctness bug, and editing logic will not fix it. Do not call done until `ruff check .` passes in full.")})
    else:
        print(f"[step {step}] done rejected — full test suite "
              f"still fails (rework done-bar); asking agent to "
              f"fix the failure", flush=True)
        st.messages.append({"role": "user", "content": (
            "The acceptance oracle passes but the FULL test suite "
            f"still fails. The merge-gate CI will reject this on "
            f"the same failure:\n{full_tail}\n\nThe bug could be "
            "in the implementation you just changed, or in a test "
            "file - do not assume either side is correct. Re-read "
            "the failing test and the code it exercises, identify "
            "which one is actually wrong, and make ONE targeted "
            "fix there. Do NOT call done until `pytest` passes in "
            "full.")})
    return BREAK


def _done_gate(origin: dict, st: LoopState, step: int, args: dict) -> Outcome:
    ok, tail = origin["oracle_result"]()
    if ok:
        outcome = _done_suite_gate(origin, st, step)
        if outcome is not None:
            return outcome
        if origin["worktree_dirty"]():
            origin["auto_commit"](origin["GREEN_COMMIT_SUBJECT"])
        print(f"[step {step}] DONE (oracle green): {args.get('summary', '')}", flush=True)
        return 0
    print(f"[step {step}] done rejected — acceptance tests still failing", flush=True)
    oracle_list = ", ".join(origin["ACCEPTANCE_PATHS"]) or "(none)"
    st.messages.append({"role": "user", "content": (
        "The acceptance tests are not passing yet:\n" + tail +
        "\nFix the implementation file and try again. Do not "
        f"modify the acceptance suite at: {oracle_list}.")})
    return BREAK


def _repetition_trip(origin: dict, st: LoopState) -> Outcome:
    if not st.nudged_repeat:
        st.nudged_repeat = True
        print("   [repetition nudge]", flush=True)
        acceptance_paths = origin["ACCEPTANCE_PATHS"]
        oracle_list = ", ".join(acceptance_paths) or "(none)"
        st.messages.append({"role": "user", "content": (
            "You have repeated the same action 3 times with no progress. STOP. "
            "Run `pytest " + " ".join(acceptance_paths) + "` to see the real failure, "
            "view the actual file contents, and fix the ROOT cause in the implementation "
            f"file. Do not modify the acceptance suite at: {oracle_list}.")})
        return BREAK
    print("   [parking: repeated action after nudge]", flush=True)
    _park_if_dirty(origin, "WIP (parked on repetition)")
    if not origin["PARK_ENABLED"]:
        return BREAK
    return 3


def _mutation_progress(st: LoopState, step: int, tool_result: Any) -> None:
    """A GENUINELY SUCCESSFUL mutation (a result that isn't an "ERROR..." string)
    resets every OTHER signature's accumulated count: `seen` was a lifetime
    cumulative counter, so re-viewing a file 2x, editing it, then viewing it
    again to check the edit landed hit the >=3 threshold from stale pre-edit
    reads. Gated on SUCCESS, not merely on tool identity - a REJECTED
    create_file/str_replace changed nothing on disk and must not be treated as
    progress (pre-fix, the clear ran on every mutating call, so alternating
    failed create_file/str_replace calls never tripped the guard; observed live
    2026-07-15)."""
    if isinstance(tool_result, str) and not tool_result.startswith("ERROR"):
        st.seen.clear()
        st.last_progress_step = step


def _str_replace_guard(st: LoopState, args: dict, tool_result: Any) -> None:
    """Failing-str_replace loop guard (mirrored from local_agent.py).

    str_replace is excluded from the per-target repetition guard, so a no-match
    loop on one file runs uncaught. After 2 consecutive FAILED str_replace on
    the same path, steer to replace_lines (line numbers, no byte-exact match).
    A successful str_replace resets the counter and re-arms the nudge."""
    sr_path = args.get("path", "")
    if not (isinstance(tool_result, str) and tool_result.startswith("ERROR")):
        st.failed_sr[sr_path] = 0
        st.nudged_sr_fail.discard(sr_path)
        return
    st.failed_sr[sr_path] = st.failed_sr.get(sr_path, 0) + 1
    if st.failed_sr[sr_path] >= 2 and sr_path not in st.nudged_sr_fail:
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


def _read_heavy_nudge(origin: dict, st: LoopState, window: int) -> None:
    print(f"   [read-heavy nudge: {window} reads in a row]", flush=True)
    oracle_list = ", ".join(origin["ACCEPTANCE_PATHS"]) or "(none)"
    st.messages.append({"role": "user", "content": (
        f"You've made {window} tool calls in a row without writing "
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
    st.recent_tools.clear()
    st.distinct_windows = 0


def _read_heavy_park(origin: dict, st: LoopState) -> Outcome:
    _park_if_dirty(origin, "WIP (read-heavy parking)")
    if not origin["PARK_ENABLED"]:
        st.recent_tools.clear()
        return BREAK
    return 3


def _read_heavy_guard(origin: dict, st: LoopState, fn: str, sig: tuple) -> Outcome:
    """Read-heavy-pattern guard.

    Tracked separately from the per-target repetition guard, which misses the
    case where every call hits a different file/command (every signature is
    unique). The model is still in a paralysis-by-analysis loop if the last
    READ_HEAVY_WINDOW tool calls were all non-mutating. Two-stage response: one
    corrective nudge, then park."""
    window = origin["READ_HEAVY_WINDOW"]
    mutating = origin["MUTATING_TOOLS"]
    st.recent_tools.append((fn, sig[1]))
    if not (len(st.recent_tools) == window
            and all(f not in mutating for (f, _t) in st.recent_tools)):
        return None
    has_repetition = len({_t for (_f, _t) in st.recent_tools}) < window
    if not st.nudged_read_heavy:
        st.nudged_read_heavy = True
        _read_heavy_nudge(origin, st, window)
        return None
    if has_repetition:
        # Re-reading an already-seen target after being nudged to act is
        # wedging, not exploration. Park at the strict 2 * READ_HEAVY_WINDOW.
        print("   [parking: read-heavy after nudge]", flush=True)
        return _read_heavy_park(origin, st)
    # All-distinct reads after the nudge: the model is exploring multiple files
    # (each read once), not wedged on one. A multi-file bug fix legitimately
    # needs more than READ_HEAVY_WINDOW reads to orient before its first edit.
    # Bounded by READ_HEAVY_DISTINCT_WINDOWS all-distinct windows (bounded, not
    # disabled). Ported from local_agent.py per the Mode 3a lesson.
    st.distinct_windows += 1
    if st.distinct_windows >= origin["READ_HEAVY_DISTINCT_WINDOWS"]:
        print(f"   [parking: read-heavy after {st.distinct_windows} distinct windows]",
              flush=True)
        return _read_heavy_park(origin, st)
    st.recent_tools.clear()
    return None


def _finish_if_green(origin: dict, step: int, st: LoopState) -> int | None:
    """Fix #1: the harness, not the model, decides done. The instant the
    independent oracle is green, auto-commit and exit. This fires *during* the
    model's iteration so a correct first attempt finishes in a single step."""
    if origin["finish_if_green"](step, st.messages):
        return 0
    # The automatic done-bar is unwinnable (oracle green, full suite red CAP
    # times running). Park rather than spend the remaining steps re-running a
    # suite that cannot go green.
    if origin["suite_reject_cap_reached"]():
        return 2
    return None


def tool_call_impl(origin: dict, st: LoopState, step: int, tc: dict) -> Outcome:
    """Run one tool call through the done gate and every per-call guard."""
    fn = tc["function"]["name"]
    args = _decode_args(tc["function"]["arguments"])

    if fn == "done":
        return _done_gate(origin, st, step, args)

    sig = _tool_signature(fn, args)
    above_threshold = _note_repeat(st, fn, sig)
    print(f"[step {step}] {fn}: {str(args.get('command') or args.get('path') or '')[:120]}", flush=True)
    if above_threshold:
        return _repetition_trip(origin, st)

    # A malformed tool call (e.g. a model that omits a required arg like
    # old_str) must nudge the model with a recoverable error, not crash the
    # whole unattended agent with an uncaught exception.
    tool_result = origin["safe_run_tool"](fn, args)
    st.messages.append({"role": "tool", "content": tool_result})

    if fn in origin["MUTATING_TOOLS"]:
        _mutation_progress(st, step, tool_result)
    if fn == "str_replace":
        _str_replace_guard(st, args, tool_result)
    outcome = _read_heavy_guard(origin, st, fn, sig)
    if outcome is not None:
        return outcome
    if origin["ACCEPTANCE_PATHS"] and fn in ("create_file", "str_replace", "bash"):
        return _finish_if_green(origin, step, st)
    return None


def step_cap_impl(origin: dict) -> int:
    """The loop ran out of steps: commit what is there and park."""
    print("[ended without oracle green — step cap reached]", flush=True)
    _park_if_dirty(origin, "WIP (step cap reached)")
    return 2
