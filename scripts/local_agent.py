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
import re
import shlex
import subprocess
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
_CHARS_PER_TOKEN_ESTIMATE = 4

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
    return _measured_chars_per_token or _CHARS_PER_TOKEN_ESTIMATE


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import inference_providers
from app import pipeline_mcp_server as p
from pipeline import edit_guards
from pipeline.local_agent_common import (
    CWD,
    PersistingList,
    _answer_orphaned_calls,
    _dropped_span_digest,  # noqa: F401 (unused here; re-exported for test_local_agent_context_compaction.py)
    _dropped_top_level_defs,
    _dropped_top_level_vars,
    _is_context_overflow_error,
    _load_resume_transcript,
    _message_char_len,
    _persist_messages,
    _repetition_nudge,
    _str_replace_not_found_diag,
    _total_chars,
    _trim_resumed_transcript,
    destructive_git_op,
)
from pipeline.local_agent_common import recover_tool_calls as _recover_tool_calls_shared
from scripts.local_agent_guards import (  # noqa: F401 (re-exported: the step loop references these as bare names)
    CHURN_SAME_PATH_MAX_EDITS,
    _bash_off_task_path,
    _churn_note_test_run,
    _churn_step,
    _expected_task_paths,
    _is_off_task_path,
    _no_tool_nudge,
    _off_task_step,
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

MODEL = os.environ["LOCAL_AGENT_MODEL"]
ENDPOINT = os.environ.get("LOCAL_AGENT_ENDPOINT", "http://localhost:11434").rstrip("/")
# Set by backend.OllamaDriver.dispatch from self.provider.name. "ollama" (the
# default) keeps chat() on the streaming NDJSON /api/chat path below,
# unchanged; any other registered provider (lmstudio, mlx) routes through
# _provider_chat_turn's blocking inference_providers call instead.
PROVIDER = os.environ.get("LOCAL_AGENT_PROVIDER", "ollama").strip().lower()
NUM_CTX = int(os.environ.get("PIPELINE_TRANSPORT_NUM_CTX", "16384"))
TIMEOUT = float(os.environ.get("LOCAL_AGENT_TIMEOUT", "900"))
MAX_STEPS = int(os.environ.get("PIPELINE_TRANSPORT_MAX_STEPS", "40"))
# L1 (REVIEWER_ESCALATION_PLAN.md): on a rework round triggered by a merge-gate
# CI failure, the defect is the agent's OWN committed test, which the reviewer
# (acceptance-scoped) never saw. Without a full-suite done-gate the agent can
# call `done` without ever running its own tests and re-fail CI on the same
# assertion every round. Set by dispatch_story when the rework was CI-triggered
# (story["ci_rework"] -> backend.dispatch rework_full_suite -> this env). When
# set, `done` is rejected unless the FULL worktree suite is green, with the
# failing excerpt fed back. Non-rework dispatches never set this, so their
# done behavior (commit-enforced, no suite gate) is unchanged.
REWORK_FULL_SUITE = os.environ.get("LOCAL_AGENT_REWORK_FULL_SUITE") == "1"
# Same done-bar as REWORK_FULL_SUITE above, but armed on EVERY dispatch (not
# just CI-fail-rework rounds). Running the full suite in-loop (the agent's
# own responsibility on fresh dispatch) overflows the local agent's trimmed
# context window on large suites, causing step-cap thrash even when the work
# is already correct (live: W1a-11, 2026-08-10). Running it out-of-band here
# keeps the agent's context clean: on pass, done proceeds; on fail, only the
# failing tail is fed back. Default OFF (secure defaults / opt-in) so it does
# not change behavior for dispatches that haven't opted in.
FULL_SUITE_DONE_BAR = os.environ.get("LOCAL_AGENT_FULL_SUITE_DONE_BAR") == "1"
# Cap consecutive assistant turns that emit no tool call. A weak model stuck
# on a self-inflicted phantom failure — its own test asserts non-standard
# behavior the correct implementation can never satisfy — will narrate its
# "next step" as prose indefinitely; the generic "call a tool" nudge cannot
# break this because no real action resolves a self-contradictory test. Park
# after this many consecutive no-tool turns rather than burning the whole run.
NO_TOOL_CAP = int(os.environ.get("LOCAL_AGENT_NO_TOOL_CAP", "5"))
# Cap full-suite done-rejections on a CI-fail-rework round. A model that has
# corrupted the code and cannot get the suite green will alternate `done`
# (rejected: suite red) with narration ("I cannot resolve this") — and because
# a `done` tool call resets consecutive_no_tool, that alternation never trips
# NO_TOOL_CAP and never trips the per-target repetition guard (done/str_replace
# are both excluded from it). Before this cap the run burned the entire step
# budget / wall-clock timeout doing nothing, then re-dispatched and repeated
# (observed live: ~20h across 7+ re-dispatches on one story). Park the run once
# the agent has failed to green the suite this many times so a stuck rework
# ends in seconds, not the whole budget.
REWORK_SUITE_REJECT_CAP = int(os.environ.get("LOCAL_AGENT_REWORK_SUITE_REJECT_CAP", "3"))
TEMPERATURE = float(os.environ.get("PIPELINE_TRANSPORT_TEMPERATURE", "0.3"))


# Qwen3 hybrid thinking control. Qwen3.6-27B (and other Qwen3 dense models)
# emit a  Mattis... Mattis reasoning block by default; in this tool-calling
# loop that breaks dispatch — the block lands in `content` with no native
# tool_calls and the driver spins "no tool call" forever (observed:
# charaf/Huihui-Qwen3.6-27B-abliterated-mlx-nvfp4:thinking-coding, 60-step
# degenerate loop, 2026-07-11). Ollama's /api/chat accepts a top-level
# "think": false to suppress the block at the source so the model emits a
# clean native tool call. Opt-in via LOCAL_AGENT_THINK, either "false"/"true"
# or a graded-reasoning level ("low"/"medium"/"high"/"max" — live-validated
# against gemma4:12b-mlx, which 400s on any other string); omitted from the
# request when unset/unrecognized so non-Qwen3/non-gemma4 models (devstral,
# gpt-oss, qwen3-coder) get an unchanged body. See _ollama_payload() and
# test_local_agent.py.
THINK = os.environ.get("LOCAL_AGENT_THINK", "").strip().lower()
_THINK_LEVELS = ("low", "medium", "high", "max")
# Per-bash-invocation timeout. The model can call `cargo fetch` and wedge on
# a network index update forever; without this the agent loop blocks on a
# single subprocess.run until cargo eventually times out (if at all).
# 10 min is generous — most cargo invocations in a worktree finish in <60s
# on a warm target/ — but bounded so a wedged cargo doesn't pin the agent.
BASH_TIMEOUT = float(os.environ.get("LOCAL_AGENT_BASH_TIMEOUT_SECONDS", "600"))

# Streaming + retry for the Ollama chat round-trip. The original single
# blocking httpx.post(stream=False, timeout=900s) with NO retry meant one
# transient Ollama queue stall (or a network blip) killed the whole run
# mid-iteration. Streaming means a slow-but-progressing generation never
# trips a wall-clock timeout (only a true silence stall does); retry means
# a transient stall doesn't kill the run. With OLLAMA_NUM_PARALLEL matched
# to MAX_CONCURRENT_AGENTS there's no steady queue, so these guard the
# residual transients (prefill, network, Ollama 5xx). See the oracle
# harness's local_agent_oracle.py for the full rationale.
#
# CONNECT_TIMEOUT_SECONDS defaults to 60s, not the old hardcoded 10s - see
# local_agent_oracle.py's CONNECT_TIMEOUT_SECONDS comment for the live
# 2026-07-13 incident (glm-4.7-flash / qwen3-coder:30b both timed out cold
# at 10s, causing an infinite load/abort/retry memory-burst cycle).
READ_SILENCE_SECONDS = float(os.environ.get("LOCAL_AGENT_READ_SILENCE_SECONDS", "180"))
CONNECT_TIMEOUT_SECONDS = float(os.environ.get("LOCAL_AGENT_CONNECT_TIMEOUT_SECONDS", "60"))
CHAT_MAX_ATTEMPTS = int(os.environ.get("LOCAL_AGENT_CHAT_MAX_ATTEMPTS", "3"))
CHAT_RETRY_BACKOFF = float(os.environ.get("LOCAL_AGENT_CHAT_RETRY_BACKOFF", "5"))

# Read-heavy-pattern guard. Tracks the last N tool names the model called and
# treats "N consecutive non-mutating tools" as paralysis-by-analysis. The
# per-target repetition guard (see main()) misses this because each call hits
# a different file/command — every signature is unique, but the model never
# actually writes anything. Window size 6 catches "5 reads without a write"
# while still allowing the natural 2-3-step warm-up of `bash pwd` / read PLAN.
READ_HEAVY_WINDOW = int(os.environ.get("LOCAL_AGENT_READ_HEAVY_WINDOW", "6"))
# Exploration-aware leniency: after the nudge, a run whose recent reads are
# all DISTINCT targets (each file/command read once) is exploring a multi-
# file bug, not wedged on one target. Allow up to this many all-distinct
# post-nudge windows before parking, so a bug fix that needs ~3x the reads
# to orient can still reach its first edit. Total distinct-read cap before
# parking = READ_HEAVY_WINDOW + READ_HEAVY_DISTINCT_WINDOWS * READ_HEAVY_WINDOW
# (6 + 3*6 = 24). A run that re-reads an already-seen target (repetition)
# still parks at the strict 2 * READ_HEAVY_WINDOW (12) — only all-distinct
# exploration gets the longer leash, and it is still bounded, not disabled.
READ_HEAVY_DISTINCT_WINDOWS = int(os.environ.get("LOCAL_AGENT_READ_HEAVY_DISTINCT_WINDOWS", "3"))
# Net-progress guard (2026-07-22, MODE-29-REVIEW-STORY-LOCK-GUARD): the
# per-target and read-heavy guards above both reset on ANY successful
# mutation, and the no-tool-call cap only counts CONSECUTIVE narration turns
# - a run that alternates "one small edit" with long stretches of distinct,
# non-repeating inspection (git status, git log, git diff, ...) and isolated
# give-up narration (each followed by a real tool call, so the counter never
# accumulates) evades every existing guard indefinitely. Observed live: 50 of
# 60 steps spent this way with zero further successful edits after an early
# one. This guard tracks steps since the last successful mutation directly -
# immune to how those unproductive steps are distributed or how many
# different-looking-but-equally-useless actions fill them.
# Default (30) is deliberately ABOVE the read-heavy guard's own distinct-
# exploration cap (READ_HEAVY_WINDOW + READ_HEAVY_DISTINCT_WINDOWS *
# READ_HEAVY_WINDOW = 6 + 3*6 = 24 by default), so this coarser, later-firing
# net doesn't preempt that guard's own already-tuned park message for the
# pure-distinct-reads-forever case it already handles. It exists to catch
# the pattern that guard CANNOT see: edits interleaved with unproductive
# investigation, which resets that guard's window every time.
NET_PROGRESS_MAX_STEPS = int(os.environ.get("LOCAL_AGENT_NET_PROGRESS_MAX_STEPS", "30"))
# Scratchpad-maintenance nudge: SEPARATE from NET_PROGRESS_MAX_STEPS above,
# which only tracks "any successful mutation" and stays silent while a model
# keeps editing OTHER files but has stopped touching .agent_scratchpad.md.
# Nudge-only: unlike the net-progress guard, this NEVER parks or returns
# early -- it only injects a reminder message and lets the run continue.
SCRATCHPAD_NUDGE_STEPS = int(os.environ.get("LOCAL_AGENT_SCRATCHPAD_NUDGE_STEPS", "15"))
SCRATCHPAD_ON = (
    os.environ.get("PIPELINE_DECOMPOSE_SCRATCHPAD", "on").strip().lower() != "off"
)
# Once a turn's measured prompt_eval_count reaches this fraction of NUM_CTX,
# trim the transcript BEFORE the next turn instead of waiting for a request
# to overflow and 500. Complements (does not replace) the reactive 5xx
# trim-retry below — that one is the backstop for when no measurement has
# landed yet (e.g. the very first turn after a resume already starts over
# budget); this one prevents most 500s from happening at all once a real
# measurement exists.
PROACTIVE_TRIM_THRESHOLD = float(os.environ.get("LOCAL_AGENT_PROACTIVE_TRIM_THRESHOLD", "0.85"))


def _apply_off_task_action(action: str, path_arg: str, messages: list) -> bool:
    """Perform the off-task-drift guard's side effects for `action` (as
    returned by _off_task_step). Returns True if the caller should treat
    this as a parkable escalation (WIP-committing a dirty tree is done here;
    whether to actually terminate the run is a PARK_ENABLED decision left to
    the caller, exactly like every other guard's kill-switch handling)."""
    if action == "nudge":
        print(f"   [off-task nudge: {path_arg} not in assigned scope]", flush=True)
        messages.append({"role": "user", "content": (
            f"You just touched {path_arg}, which was not named anywhere "
            f"in your assigned task. If this file is genuinely required "
            f"to complete the task, explain why in your next message and "
            f"continue. Otherwise STOP editing unrelated files and refocus "
            f"on the files named in your instructions.")})
        return False
    if action == "escalate":
        print(f"   [parking: off-task drift onto {path_arg} after nudge]", flush=True)
        if worktree_dirty():
            auto_wip_commit("parked on off-task drift")
        return True
    return False


# How many MORE consecutive failed str_replace calls on a path, after the
# one-time nudge, before the failing-str_replace guard escalates to a park.
STR_REPLACE_FAIL_ESCALATE_AFTER = int(
    os.environ.get("LOCAL_AGENT_STR_REPLACE_FAIL_ESCALATE_AFTER", "2"))


# Mutating tools: any that produce new code in the worktree. Anything else
# (view_file, bash, checkpoint) is read-only — including checkpoint, which
# commits existing WIP but doesn't add new code; checkpointing without prior
# edits is itself a sign of "spinning."
MUTATING_TOOLS = frozenset({"create_file", "str_replace", "replace_lines"})
# Parking kill-switch (env LOCAL_AGENT_PARK_ENABLED, default "1"). When
# disabled, the loop guards still nudge the model toward writing but never
# terminate the run (return 3) — the step cap becomes the only bound. A
# capable model that demonstrably writes but re-reads aggressively (e.g.
# minimax-m3:cloud re-viewing a file before editing it) can otherwise hit the
# strict-repetition park before its first edit on stories where orientation
# needs repeated reads. Nudges still fire to steer it; only the premature
# termination is suppressed. Defaults to on so behavior is unchanged unless a
# run opts in.
PARK_ENABLED = os.environ.get("LOCAL_AGENT_PARK_ENABLED", "1") != "0"

HARNESS_RULES = (
    "You are working inside a git repository (the current directory). Complete "
    "the task by calling tools — do NOT explain a plan in prose, call a tool. "
    "Use create_file for NEW files and str_replace or replace_lines to edit "
    "EXISTING files; use bash only to run commands like tests and git (never "
    "to create/edit files). Read each file ONCE with view_file before your "
    "first edit to it — do NOT view_file a path you have already read this "
    "run; its contents are in your context and reading it again wastes your "
    "step budget. Use `git diff` to see your own pending changes. Run "
    "`pytest -q <test_file>` after your first edit to a file that has tests — "
    "not at the end — so test feedback tells you which file actually needs "
    "work. Read the file:line in any error before changing code. Commit your "
    "work with git before finishing. Call done only after your changes are "
    "committed and any tests pass."
)

TOOLS = [
    {"type": "function", "function": {
        "name": "create_file", "description": "Create a new file, or overwrite one with its full corrected contents. To overwrite a file that already exists on disk, view_file it first, then create_file with the complete new contents. Prefer str_replace or replace_lines for targeted edits to existing files; only use create_file for files under 200 lines or brand-new files — never create_file a file over 200 lines, a full rewrite drops unrelated content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
            "confirm_removals": {"type": "boolean", "description": "Set true to confirm you intend to delete the top-level def/class the previous attempt reported as missing from your new content. Only needed after a rejected overwrite."}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "str_replace", "description": "Replace the single unique occurrence of old_str with new_str in an existing file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"},
            "confirm_removals": {"type": "boolean", "description": "Set true to confirm you intend to delete the top-level def/class/constant the previous attempt reported as permanently removed. Only needed after a rejected edit that fully removes a top-level symbol."}},
            "required": ["path", "old_str", "new_str"]}}},
    {"type": "function", "function": {
        "name": "replace_lines", "description": "Replace lines start..end (1-indexed, inclusive) in an existing file with new_str. Use this when str_replace's old_str will not match (e.g. whitespace differences): give the exact line numbers from `nl -ba <file> | sed -n '<start>,<end>p'` and the new content; no byte-exact old_str is required. If the line numbers came from an earlier view_file, supply expect_first/expect_last so stale numbers are caught before the edit is applied.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "start": {"type": "integer"},
            "end": {"type": "integer"}, "new_str": {"type": "string"},
            "expect_first": {"type": "string", "description": "The exact text the model expects to find at line `start`. Optional but strongly recommended on files over 1000 lines because line numbers from an earlier view_file go stale."},
            "expect_last": {"type": "string", "description": "The exact text the model expects to find at line `end`. Optional but strongly recommended on files over 1000 lines because line numbers from an earlier view_file go stale."},
            "confirm_removals": {"type": "boolean", "description": "Set true to confirm you intend to delete the lines the previous attempt reported. Only needed after a rejected edit."}},
            "required": ["path", "start", "end", "new_str"]}}},
    {"type": "function", "function": {
        "name": "view_file",
        "description": (
            "Show a file's contents with line numbers. Large files are "
            "truncated; pass optional 1-indexed inclusive line_start/"
            "line_end to view a specific range instead (e.g. after grep "
            "gives you a line number)."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "line_start": {"type": "integer"},
            "line_end": {"type": "integer"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "restore_file",
        "description": "Discard your changes to ONE file and restore it to the last commit (git checkout HEAD -- <path>). Use this when your edits to a file have gone wrong and you want a clean slate for it specifically, instead of str_replace/replace_lines patches on top of a mess. Does not touch any other file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "bash", "description": "Run a bash command (run tests, git, etc.). Do NOT use to create or edit files.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "checkpoint", "description": "Record a durable checkpoint: commits current work as WIP and logs progress so the story is resumable if interrupted.",
        "parameters": {"type": "object", "properties": {
            "plan_name": {"type": "string"}, "story_key": {"type": "string"}, "step": {"type": "string"},
            "summary": {"type": "string"}, "next_hint": {"type": "string"}},
            "required": ["plan_name", "story_key", "step", "summary"]}}},
    {"type": "function", "function": {
        "name": "done", "description": "Call only when the task is complete, work is committed, and tests pass.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
]


def _stream_one_turn(payload):
    """One streamed chat turn against Ollama's /api/chat. Accumulates the
    assistant message across newline-delimited JSON chunks and returns the
    assembled message dict ({role, content, tool_calls?}) — the same shape
    the non-streaming path returned via r.json()["message"], so main() and
    recover_tool_calls() work unchanged.

    Streaming lets the per-chunk read timeout (READ_SILENCE_SECONDS) fire
    only on a genuine stall (no bytes for N seconds), not on a legitimately
    long generation. A slow-but-progressing gen streams a chunk every ~1-2s
    and never trips it.

    Raises httpx.HTTPStatusError on a bad response (4xx/5xx) or
    httpx.TransportError (TimeoutException/ConnectError/ReadError) on a
    connect/read stall — chat() decides which of those are retryable.
    """
    global _measured_chars_per_token, _last_prompt_eval_count
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls = None
    role = "assistant"
    prompt_eval_count = None
    with httpx.stream(
        "POST", f"{ENDPOINT}/api/chat", json=payload,
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT_SECONDS, read=READ_SILENCE_SECONDS,
                              write=10.0, pool=10.0),
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = chunk.get("message") or {}
            if msg.get("role"):
                role = msg["role"]
            if msg.get("content"):
                content_parts.append(msg["content"])
            if msg.get("thinking"):
                thinking_parts.append(msg["thinking"])
            # Tool calls may land at the top level of a chunk or inside its
            # message; capture from either. (devstral emits tool calls as
            # text content, so tool_calls stays None and recover_tool_calls
            # parses the assembled content downstream.)
            tc = chunk.get("tool_calls") or msg.get("tool_calls")
            if tc:
                tool_calls = tc
            if chunk.get("done"):
                prompt_eval_count = chunk.get("prompt_eval_count")
                break
    content = "".join(content_parts)
    # Reasoning models (e.g. gpt-oss:20b) stream their chain-of-thought in a
    # separate `thinking` field and may leave `content` entirely empty for a
    # turn. Fall back to the assembled thinking text ONLY when there is no
    # real content at all — never append it alongside genuine content, since
    # that would leak raw reasoning traces into recover_tool_calls() parsing
    # and downstream commit messages/logs.
    if not content.strip() and thinking_parts:
        content = "".join(thinking_parts)
    assembled = {"role": role, "content": content}
    if tool_calls:
        assembled["tool_calls"] = tool_calls
    # Calibrate the chars/token ratio from ollama's own real count for this
    # request — see _measured_chars_per_token's docstring for why the fixed
    # estimate alone is not trustworthy.
    if prompt_eval_count:
        _last_prompt_eval_count = prompt_eval_count
        # The tools schema is part of every prompt and is counted in
        # prompt_eval_count, so it must be counted in the numerator too -
        # omitting it biases the ratio badly low early in a run, when the
        # ~3.4KB schema dominates a still-small transcript (measured: 0.67
        # vs a true ~2.35, shrinking the reactive-5xx trim budget ~3.5x more
        # than needed). Chat-template scaffolding is still unaccounted for,
        # which leaves a small residual bias in the same safe (over-trim)
        # direction, and shrinks as the transcript grows.
        sent_chars = (
            sum(_message_char_len(m) for m in payload.get("messages", []))
            + len(json.dumps(payload.get("tools") or []))
        )
        if sent_chars > 0:
            _measured_chars_per_token = sent_chars / prompt_eval_count
    return assembled


def _provider_chat_turn(messages):
    """One provider-backed chat turn for a non-Ollama PROVIDER (lmstudio,
    mlx). Blocking, not streamed — these servers are OpenAI-compatible and
    stream tool calls as index-based deltas that need reassembly across
    chunks, a materially different (and riskier) parser than Ollama's
    whole-message-per-chunk NDJSON; deferred, see
    MODEL_PROVIDER_ABSTRACTION_PLAN.md S3. Bounded by TIMEOUT (the overall
    dispatch wall-clock budget) rather than a per-chunk silence timeout.
    Returns just the assembled message dict — the same shape
    _stream_one_turn returns — so chat()/main() work unchanged regardless of
    which provider is active.

    Records prompt_eval_count/calibration the same way _stream_one_turn does.
    This return shape stays the bare message, but the usage fields must NOT be
    discarded: both LMStudioProvider.chat and MLXProvider.chat already map the
    OpenAI `usage` block into prompt_eval_count, and dropping it left
    _last_prompt_eval_count permanently None on those providers — which is the
    condition main()'s proactive trim is gated on, so the primary overflow
    defense never fired at all outside Ollama (2026-08-07 audit).
    """
    global _measured_chars_per_token, _last_prompt_eval_count
    envelope = inference_providers.get_local_provider().chat(
        messages, model=MODEL, num_ctx=NUM_CTX, temperature=TEMPERATURE,
        tools=TOOLS, endpoint=ENDPOINT, timeout=TIMEOUT,
    )
    prompt_eval_count = envelope.get("prompt_eval_count")
    if prompt_eval_count:
        _last_prompt_eval_count = prompt_eval_count
        # Same numerator as _stream_one_turn: the tools schema is part of
        # every prompt and is counted in prompt_eval_count, so it belongs in
        # the chars total too.
        sent_chars = (
            sum(_message_char_len(m) for m in messages)
            + len(json.dumps(TOOLS))
        )
        if sent_chars > 0:
            _measured_chars_per_token = sent_chars / prompt_eval_count
    return envelope["message"]


def _ollama_payload(messages):
    """Build the Ollama /api/chat request body for one turn.

    Extracted from chat() so the Qwen3/gemma4 thinking-mode flag (THINK) is
    unit-testable without an HTTP boundary. `think` is included only when
    LOCAL_AGENT_THINK is explicitly "true"/"false" (bool) or one of the
    graded-reasoning levels (passed through verbatim as a string) — omitted
    otherwise so models with no tuned opinion get an unchanged request body
    (see THINK's comment).
    """
    payload = {"model": MODEL, "messages": messages, "tools": TOOLS, "stream": True,
               "options": {"num_ctx": NUM_CTX, "temperature": TEMPERATURE}}
    if THINK in ("true", "false"):
        payload["think"] = (THINK == "true")
    elif THINK in _THINK_LEVELS:
        payload["think"] = THINK
    return payload


def chat(messages):
    """One LLM turn, with retry.

    A single transient stall (queue contention, network blip, 5xx, 429)
    must not kill a 30-minute run. For PROVIDER == "ollama" (the default) we
    stream so a slow generation doesn't trip the timeout; other providers go
    through _provider_chat_turn's blocking call instead (see its docstring).
    Either way, retry covers the transient failures: 4xx is a bad request
    (retrying won't help) so it raises immediately; 5xx, transport errors
    (timeout/connect/read), and RateLimitedError (429) are retried up to
    CHAT_MAX_ATTEMPTS. If every attempt fails, the last exception propagates
    to main()'s except, which commits WIP and returns 1 — same terminal
    behavior as before, but only after we've genuinely tried.
    """
    payload = _ollama_payload(messages)
    last_exc: Exception | None = None
    for attempt in range(1, CHAT_MAX_ATTEMPTS + 1):
        try:
            if PROVIDER == "ollama":
                return _stream_one_turn(payload)
            return _provider_chat_turn(messages)
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise  # 4xx — bad request, retrying is pointless
            last_exc = e
        except httpx.TransportError as e:
            last_exc = e  # timeout / connect / read — transient, retry
        except inference_providers.RateLimitedError as e:
            last_exc = e  # 429 — transient, retry like a 5xx
        if attempt < CHAT_MAX_ATTEMPTS:
            time.sleep(CHAT_RETRY_BACKOFF * attempt)
    assert last_exc is not None  # loop ran ≥1 attempt; only reachable w/ an exc
    raise last_exc


def _repair_triple_quoted_strings(candidate):
    """Rewrite Python-style triple-quoted string literals (\"\"\"...\"\"\" or
    '''...''') as JSON-encoded strings. Weaker local models emit multi-line
    code arguments (a str_replace's new_str/old_str) using Python triple-quote
    syntax with literal newlines, which is not valid JSON - json.loads rejects
    it at the opening \"\"\", so the tool call is silently dropped and the
    edit never lands (observed systematically with Qwen2.5-Coder-14B-4bit on
    mlx: 12/12 dropped calls, see MLX_DEFAULT_PROVIDER_PLAN.md). json.dumps of
    the inner text produces a correctly-escaped JSON string in its place."""
    def _sub(m):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        return json.dumps(inner)
    return re.sub(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'', _sub, candidate, flags=re.DOTALL)


def recover_tool_calls(content):
    """Pull a tool call out of message text when the native field is empty.

    Delegates to the shared parser in local_agent_common, binding this
    file's own diverged _repair_triple_quoted_strings as the injectable
    repair fallback (see that module's _loads_tolerant docstring for why
    the repair function isn't imported alongside it)."""
    return _recover_tool_calls_shared(content, _repair_triple_quoted_strings)


def git(*args):
    return subprocess.run(["git", *args], check=False, cwd=CWD, capture_output=True, text=True)


def exclude_runtime_artifacts() -> None:
    """Keep dispatch runtime junk out of git. agent.log lives *inside* the
    worktree and is written live, so without this it would (a) make the tree
    perpetually "dirty" — tripping commit-enforcement on every `done` — and
    (b) get swept into commits by `git add -A` and carried into the eventual
    merge. Same for pytest's __pycache__/*.pyc. Written to git's real
    info/exclude path, which `git rev-parse --git-path` resolves correctly
    whether .git is a directory (plain repo) or a file (a `git worktree`)."""
    rel = git("rev-parse", "--git-path", "info/exclude").stdout.strip()
    if not rel:
        return
    if os.path.isabs(rel):
        path = Path(rel)
    elif (CWD / ".git").is_dir() and not rel.startswith(".git"):
        # git returned a path relative to the git dir (e.g. "info/exclude");
        # resolve it against the .git directory under the worktree root.
        path = CWD / ".git" / rel
    else:
        path = CWD / rel
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text() if path.exists() else ""
        additions = [p for p in ("agent.log", "__pycache__/", "*.pyc", ".agent_transcript.json", ".agent_done", ".agent_done.tmp", ".agent_done.consumed") if p not in existing]
        if additions:
            path.write_text(existing + ("\n" if existing and not existing.endswith("\n") else "")
                            + "\n".join(additions) + "\n")
    except OSError:
        pass


def worktree_dirty() -> bool:
    return bool(git("status", "--porcelain").stdout.strip())


def auto_wip_commit(reason: str) -> None:
    git("add", "-A")
    git("commit", "-m", f"WIP ({reason})")


def _full_suite_result() -> tuple[bool, str, str | None]:
    """Run the FULL worktree suite (unscoped), for the L1 CI-fail-rework
    done-gate. Mirrors the merge gate's _ci_status_stub runner
    (tests/benchmark/harness.py:623) and the oracle variant's helper:
    detect_test_command + the heavy lock, run the detected command verbatim
    (no acceptance scoping - this agent has no acceptance oracle), return
    (passed, tail[-500:], gate). No detectable test command -> (True, '', None) (nothing
    to fail). Kept in sync with scripts/local_agent_oracle.py:_full_suite_result.

    Mode 40: once tests pass, also run detect_lint_command (if the repo has
    one) and fold a lint failure into the same (False, tail, 'lint') result - the
    live incident that motivated this was an agent exiting DONE with a
    green suite but a lint-failing CI, because nothing local ever checked
    lint before this. No detected lint command -> unchanged (True, '', None).
    """
    test_dir, test_cmd = p.detect_test_command(CWD)
    if not test_cmd:
        return True, "", None
    argv = test_cmd
    needs_heavy = bool(argv) and p._is_heavy(argv)
    if needs_heavy:
        with p._heavy_lock():
            r = subprocess.run(argv, check=False, cwd=test_dir, capture_output=True, text=True)
    else:
        r = subprocess.run(argv, check=False, cwd=test_dir, capture_output=True, text=True)
    if r.returncode != 0:
        return False, (r.stdout + r.stderr)[-500:], "test"
    lint = p.detect_lint_command(CWD)
    if lint is not None:
        lint_dir, lint_cmd = lint
        lr = subprocess.run(lint_cmd, check=False, cwd=lint_dir, capture_output=True, text=True)
        if lr.returncode != 0:
            return False, (lr.stdout + lr.stderr)[-500:], "lint"
    return True, "", None


def _reject_done_for_suite(messages: list, step: int, suite_tail: str, gate: str | None) -> None:
    """L1: feed a full-suite failure back as a user turn and announce the rejection. Used at both `done`-rejection sites (clean tree, and the dirty-tree auto-accept escape) so the raised rework done-bar holds and the agent can't dodge it by interleaving dirty/clean done calls. The caller increments `suite_rejections` and `break`s out of the tool-call loop so the next step re-enters with this fed-back excerpt."""
    if gate == 'lint':
        print(f"[step {step}] done rejected — lint gate still fails (rework done-bar); asking agent to fix the lint failure", flush=True)
        messages.append({"role": "user", "content": (
            f"Your tests PASS, but the lint check (`ruff check .`) fails. The merge-gate CI lint gate will reject this on the same failure:\n{suite_tail}\n\nMost lint errors are auto-fixable: run `ruff check . --fix`, then `ruff check .` to confirm it is clean.\n\nDo NOT edit implementation logic — this is a formatting/import/style error, not a correctness bug, and editing logic will not fix it. Do not call done until `ruff check .` passes in full."
        )})
    else:
        print(f"[step {step}] done rejected — full test suite still fails (rework done-bar); asking agent to fix the failure", flush=True)
        messages.append({"role": "user", "content": (
            f"The full test suite still fails. The merge-gate CI will reject this on the same failure:\n{suite_tail}\n\nThe bug could be in the implementation you just changed, or in a test file - do not assume either side is correct. Re-read the failing test and the code it exercises, identify which one is actually wrong, and make ONE targeted fix there. do not call done until pytest passes in full."
        )})
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
    if fn == "create_file":
        path = CWD / args["path"]
        preexisting = path.exists() and path.read_text().strip()
        if (preexisting
                and args["path"] not in _CREATED_THIS_RUN
                and args["path"] not in _VIEWED_THIS_RUN):
            return (
                f"ERROR: {args['path']} already exists and is non-empty. Use "
                f"view_file to read it first, then create_file to overwrite it "
                f"with the full corrected contents."
            )
        content = args.get("content", "")
        err = _python_syntax_error(args["path"], content)
        note = None
        if err:
            repair = _try_repair_indentation(content)
            if repair is None:
                return _record_syntax_rejection(args["path"], err)
            content, note = repair
        if preexisting:
            dropped = _dropped_top_level_defs(path.read_text(), content)
            if path.suffix == ".py":
                dropped += _dropped_top_level_vars(path.read_text(), content)
            if dropped and not args.get("confirm_removals"):
                return (
                    f"ERROR: this create_file overwrite of {args['path']} would "
                    f"silently drop {len(dropped)} top-level def/class that exist "
                    f"in the current file but not in your new content: "
                    f"{', '.join(dropped)}. If this is unintentional, view_file "
                    f"the current contents and include these definitions in your "
                    f"rewrite (use str_replace/replace_lines for a small targeted "
                    f"change instead of a full rewrite). If the removal is "
                    f"intentional, repeat this exact call with "
                    f"confirm_removals=true. The file was NOT overwritten."
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        _SYNTAX_REJECT_COUNTS.pop(args["path"], None)
        _CREATED_THIS_RUN.add(args["path"])
        return (f"created {args['path']}" + (f" ({note})" if note else "")
                + _lint_feedback_for(args['path'], CWD))
    if fn == "str_replace":
        path = CWD / args["path"]
        if not path.exists():
            return f"ERROR: {args['path']} does not exist (use create_file for new files)."
        text = path.read_text()
        n = text.count(args["old_str"])
        if n == 0:
            return _str_replace_not_found_diag(args["path"], text, args["old_str"])
        if n > 1:
            return f"ERROR: old_str occurs {n} times in {args['path']}; include more context to make it unique."
        new_text = text.replace(args["old_str"], args["new_str"])
        err = _python_syntax_error(args["path"], new_text)
        note = None
        if err:
            repair = _try_repair_indentation(new_text)
            if repair is None:
                return _record_syntax_rejection(args["path"], err, len(text.splitlines()))
            new_text, note = repair
        orphaned = _newly_undefined_names(args["path"], text, new_text)
        if orphaned:
            return (
                f"ERROR: this edit to {args['path']} deletes the only assignment "
                f"to {', '.join(orphaned)} while a use of it survives elsewhere - "
                f"this will raise NameError/UnboundLocalError at runtime. Keep the "
                f"assignment, remove the surviving use too, or replace it with an "
                f"equivalent. The edit was NOT applied."
            )
        # Top-level-symbol-loss check: name any def/class that this edit
        # removes in its entirety, even when no same-file reference survives
        # (the symbol may be consumed by OTHER files) - this half is
        # unconditional. A dropped top-level var/constant is only added when
        # it is a genuine unconfirmed loss per _var_drop_is_confirmed_loss:
        # a rename (new_str itself assigns a top-level name) or a
        # self-contained removal (the assignment and its only use both lived
        # in old_str and neither survives) escapes the gate; a bare deletion
        # with nothing replacing it does not. Gated by confirm_removals so
        # an intentional removal still goes through when the flag is set.
        dropped_defs = _dropped_top_level_defs(text, new_text)
        if path.suffix == ".py":
            dropped_vars = [
                name for name in _dropped_top_level_vars(text, new_text)
                if _var_drop_is_confirmed_loss(
                    name, args["old_str"], args["new_str"], orphaned)
            ]
        else:
            dropped_vars = []
        dropped = dropped_defs + dropped_vars
        if dropped and not args.get("confirm_removals"):
            return (
                f"ERROR: this edit to {args['path']} permanently removes these "
                f"top-level symbols in their entirety: {', '.join(dropped)}. "
                f"These may be public API consumed by other files. If this is "
                f"unintentional, keep the definition in new_str. If the removal "
                f"is intentional, repeat this exact call with "
                f"confirm_removals=true. The edit was NOT applied."
            )
        path.write_text(new_text)
        _SYNTAX_REJECT_COUNTS.pop(args["path"], None)
        return (f"edited {args['path']}" + (f" ({note})" if note else "")
                + _lint_feedback_for(args['path'], CWD))
    if fn == "replace_lines":
        path = CWD / args["path"]
        if not path.exists():
            return f"ERROR: {args['path']} does not exist (use create_file for new files)."
        start = args.get("start")
        end = args.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            return (f"ERROR: replace_lines requires integer start and end "
                    f"(got start={start!r}, end={end!r}).")
        if start < 1:
            return f"ERROR: line_start {start} must be >= 1 (1-indexed)."
        if end < start:
            return f"ERROR: line_end {end} is less than line_start {start}."
        old_text = path.read_text()
        lines = old_text.splitlines(keepends=True)
        if start > len(lines):
            return f"ERROR: line_start {start} is beyond {args['path']}'s {len(lines)} lines."
        # Optional stale-range anchors: verified when supplied, absent otherwise.
        # MUST stay optional - _str_replace_not_found_diag steers the model to
        # replace_lines when str_replace's old_str won't match; mandatory anchors
        # would close that escape hatch and strand a weak model with no edit path.
        anchor_err = edit_guards.verify_range_anchors(
            lines, start, end, args.get("expect_first"), args.get("expect_last"))
        if anchor_err:
            return f"ERROR: {anchor_err}\nThe edit was NOT applied."
        new_str = args.get("new_str", "")
        # Keep the block newline-terminated so we don't fuse the next line on.
        if new_str and not new_str.endswith("\n"):
            new_str = new_str + "\n"
        new_text = "".join(lines[:start - 1]) + new_str + "".join(lines[end:])
        err = _python_syntax_error(args["path"], new_text)
        note = None
        if err:
            repair = _try_repair_indentation(new_text)
            if repair is None:
                return _record_syntax_rejection(args["path"], err, len(old_text.splitlines()))
            new_text, note = repair
        orphaned = _newly_undefined_names(args["path"], old_text, new_text)
        if orphaned:
            return (
                f"ERROR: this edit to {args['path']} deletes the only assignment "
                f"to {', '.join(orphaned)} while a use of it survives elsewhere - "
                f"this will raise NameError/UnboundLocalError at runtime. Keep the "
                f"assignment, remove the surviving use too, or replace it with an "
                f"equivalent. The edit was NOT applied."
            )
        deletions, rewrites = edit_guards.classify_removed_lines(lines[start - 1:end], new_str)
        if deletions and not args.get("confirm_removals"):
            report = edit_guards.render_removal_report(deletions, rewrites)
            # Unconditional top-level-symbol-loss check: name any def/class/
            # constant that this range removes in its entirety, even when no
            # same-file reference survives (the symbol may be consumed by OTHER
            # files). This only ENRICHES the existing confirm_removals-gated
            # rejection message -- it is not a separate blocking gate, so
            # confirm_removals=true still short-circuits past it untouched.
            dropped = _dropped_top_level_defs(old_text, new_text)
            if path.suffix == ".py":
                dropped += _dropped_top_level_vars(old_text, new_text)
            symbol_note = ""
            if dropped:
                symbol_note = (
                    f"This edit also permanently removes these top-level symbols "
                    f"in their entirety: {', '.join(dropped)}\n"
                )
            return (
                f"ERROR: this edit to {args['path']} deletes {len(deletions)} line(s) "
                f"that don't appear to survive (as-is or rewritten) in your replacement:"
                f"{symbol_note}{report}\n\nRevise new_str to preserve these lines, or if the deletion "
                f"is intentional, repeat this exact call with confirm_removals=true. "
                f"The edit was NOT applied."
            )
        path.write_text(new_text)
        _SYNTAX_REJECT_COUNTS.pop(args["path"], None)
        removed_echo = edit_guards.render_removal_report([], rewrites)
        # Advisory only: warn if new_str duplicates a block that still lives
        # outside the replaced range. Computed from the ORIGINAL lines read
        # before the write (prefix + suffix), so the range's own former
        # content is not counted as a duplicate. Does not block - the write
        # above has already landed.
        surrounding_text = "".join(lines[:start - 1]) + "".join(lines[end:])
        dup_warn = edit_guards.duplicated_block_warning(new_str, surrounding_text)
        return (f"edited {args['path']} (lines {start}-{end})" + (f" ({note})" if note else "")
                + removed_echo + dup_warn + _lint_feedback_for(args['path'], CWD))
    if fn == "view_file":
        path = CWD / args["path"]
        if not path.exists():
            return f"ERROR: {args['path']} does not exist."
        _VIEWED_THIS_RUN.add(args["path"])
        lines = path.read_text().splitlines(keepends=True)
        line_start, line_end = args.get("line_start"), args.get("line_end")
        if line_start is not None or line_end is not None:
            start = line_start if line_start is not None else 1
            end = line_end if line_end is not None else len(lines)
            if start < 1:
                return f"ERROR: line_start {start} must be >= 1 (1-indexed)."
            if start > len(lines):
                return f"ERROR: line_start {start} is beyond {args['path']}'s {len(lines)} lines."
            if end < start:
                return f"ERROR: line_end {end} is less than line_start {start}."
            selected = lines[start - 1:end]
            return "".join(f"{start + i:4d}| {ln}" for i, ln in enumerate(selected))
        formatted = "".join(f"{i + 1:4d}| {ln}" for i, ln in enumerate(lines))
        if len(formatted) <= 3000:
            return formatted
        return (
            formatted[:3000]
            + f"\n... [truncated; {args['path']} has {len(lines)} lines total — "
              f"call view_file again with line_start/line_end to see more]"
        )
    if fn == "restore_file":
        path_str = args.get("path", "")
        if not path_str:
            return "ERROR: restore_file requires a path."
        result = subprocess.run(
            ["git", "checkout", "HEAD", "--", path_str],
            check=False, cwd=CWD, capture_output=True, text=True,
        )
        if result.returncode != 0:
            return (f"ERROR: could not restore {path_str} to HEAD: "
                     f"{result.stderr.strip()[:300]}")
        return (f"restored {path_str} to its last commit (HEAD) — any "
                 f"uncommitted changes to this file are gone. Other files are untouched.")
    if fn == "bash":
        cmd = args.get("command", "")
        # Refuse destructive git ops before they reach the shell — they discard
        # the branch's WIP commits / working-tree changes (see
        # DESTRUCTIVE_GIT_PATTERNS). Tell the model to use str_replace to change
        # files instead, so a confused rework can't destroy its own work.
        bad = destructive_git_op(cmd)
        if bad:
            return (
                f"ERROR: '{bad}' is blocked — it would discard your work (WIP "
                f"commits or uncommitted changes). To change a file, use "
                f"str_replace; to unstage, use `git reset HEAD <path>` (no "
                f"--hard). To undo a recent commit but keep the changes, use "
                f"`git reset HEAD~1` (default --mixed, keeps the working tree). "
                f"To throw away your OWN uncommitted edits to one specific file "
                f"and start it clean from the last commit, use the restore_file "
                f"tool on that path — it does exactly this, safely, without "
                f"touching any other file."
            )
        # Acquire the cross-dispatch heavy-build lock for any command whose
        # first token is a known build/test executable (cargo, npm, mvn,
        # etc.). The lock lives at PLAN_DIR/heavy.lock; see _heavy_lock
        # docstring for the rationale (one cold build at a time keeps
        # memory bounded across concurrent agents).
        # We shlex-split because the model passes a string; argv[0] of the
        # split gives us the executable. shlex.split raises on malformed
        # shell — fall through to the no-lock path in that case so a
        # weird command doesn't take the agent down.
        try:
            argv0 = shlex.split(cmd)[0] if cmd.strip() else ""
        except ValueError:
            argv0 = ""
        is_heavy = bool(argv0) and p._is_heavy([argv0])
        run_kwargs = {"shell": True, "cwd": CWD, "capture_output": True, "text": True,
                          "timeout": BASH_TIMEOUT}
        if is_heavy:
            with p._heavy_lock():
                pr = subprocess.run(cmd, check=False, **run_kwargs)
        else:
            pr = subprocess.run(cmd, check=False, **run_kwargs)
        return (pr.stdout + pr.stderr)[:3000] or "(no output)"
    if fn == "checkpoint":
        try:
            res = p._checkpoint_impl(args["plan_name"], args["story_key"], args["step"],
                                     args.get("summary", ""), args.get("next_hint", ""))
            return f"checkpoint recorded: {json.dumps(res)[:200]}"
        except Exception as e:  # noqa: BLE001 (checkpoint is a tool call result to the model, not a gate; any failure is reported back as tool output, not raised)
            return f"ERROR checkpointing: {e}"
    if fn == "search":
        return (
            "unknown tool search — there is no search tool. Use bash with "
            "grep or rg to find code (e.g. `grep -n \"def foo\" -R .`), then "
            "view_file with line_start/line_end on the line number it reports."
        )
    return f"unknown tool {fn}"


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
    try:
        return run_tool(fn, args)
    except Exception as e:  # noqa: BLE001 (a tool call's own failure is reported back to the model as tool output, not raised - the agent loop must never crash on an unpredictable tool error)
        msg = f"ERROR running {fn}: {type(e).__name__}: {e}"
        required = _TOOL_SCHEMAS.get(fn, {}).get("required", [])
        missing = [r for r in required if r not in args]
        if missing:
            msg += (
                f" -- {fn} requires {required}; you passed "
                f"{sorted(args.keys())}, missing {missing}. Call {fn} again "
                f"with all required keys."
            )
        return msg


# Backoff between escalation rounds. The pre-2026-08-07 helper fired all its
# rounds back-to-back with no pause at all, which is the worst possible
# response when the 500 is load-induced (memory pressure, a model reload,
# concurrent agents) rather than an overflow.
RECOVERY_BACKOFF_SECONDS = float(
    os.environ.get("LOCAL_AGENT_RECOVERY_BACKOFF_SECONDS", "2"))
# (context fraction, backoff multiplier) per round, in order.
_RECOVERY_ROUNDS = ((0.75, 1), (0.50, 3), (0.30, 6))


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
    print(f"[step {step}] backend error after {CHAT_MAX_ATTEMPTS} attempts on a "
          f"large transcript; escalating trim and retrying (with backoff)",
          flush=True)
    for fraction, backoff_mult in _RECOVERY_ROUNDS:
        budget_chars = int(NUM_CTX * _effective_chars_per_token() * fraction)
        trimmed = _trim_resumed_transcript(messages, budget_chars)
        # Compare CHARS, not message count: the eviction tier shrinks payload
        # without removing any message, so a length test reports "no change"
        # for a trim that in fact reclaimed most of the transcript.
        if _total_chars(trimmed) < _total_chars(messages):
            messages[:] = trimmed
        else:
            # Could not shrink - so this very likely isn't an overflow. Wait
            # it out instead of giving up; the payload goes back unchanged.
            print(f"[step {step}] transcript could not be shrunk further; "
                  f"treating as a transient fault and retrying after backoff",
                  flush=True)
        time.sleep(RECOVERY_BACKOFF_SECONDS * backoff_mult)
        try:
            return chat_fn(messages)
        except httpx.HTTPStatusError as e:
            if not _is_context_overflow_error(e):
                raise  # a real bad request - propagate rather than retry
            continue  # overflow-shaped: shrink harder on the next round
    return None  # all rounds exhausted on a persistent failure


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
                print(f"[step {step}] DONE: {args.get('summary', '')}", flush=True)
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
    return rc


if __name__ == "__main__":
    sys.exit(main())
