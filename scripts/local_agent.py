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

import ast
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import httpx

_VALID_ROLES = {"system", "user", "assistant", "tool"}

def _validate_message_list(msgs):
    if not isinstance(msgs, list) or not msgs:
        return False
    for m in msgs:
        if not isinstance(m, dict) or 'role' not in m or 'content' not in m:
            return False
        if m['role'] not in _VALID_ROLES:
            return False
    return True

def _load_resume_transcript() -> list | None:
    path = os.environ.get("LOCAL_AGENT_RESUME_TRANSCRIPT_PATH")
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not _validate_message_list(data):
            print("[local_agent] RESUME FAILED: transcript shape invalid", flush=True)
            return None
        return data
    except Exception as e:  # noqa: BLE001 (resume is best-effort; any failure falls back to a cold start)
        print(f"[local_agent] RESUME FAILED: {e}", flush=True)
        return None

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


def _message_char_len(m: dict) -> int:
    n = len(str(m.get("content") or ""))
    tool_calls = m.get("tool_calls")
    if tool_calls:
        n += len(json.dumps(tool_calls))
    return n


def _trim_resumed_transcript(messages: list, max_chars: int) -> list:
    """Bound a resumed transcript to max_chars, dropping the oldest middle
    content when it would otherwise overflow the model's context window.

    Always preserves the original system+task head (messages[:2] - the
    resumed transcript's first two entries) and works backward from the end
    to keep as much recent activity as fits, so the model still has its most
    recent progress to continue from. Content is dropped in whole blocks (an
    assistant message plus any tool-role messages immediately following it,
    which are that call's results) so a tool_calls message is never split
    from its own tool response - either both survive or both are dropped.
    When anything is dropped, a synthetic user note is inserted between the
    head and the surviving tail explaining what happened, so the model isn't
    confused by a discontinuity in its own history.
    """
    total = sum(_message_char_len(m) for m in messages)
    if total <= max_chars:
        return messages

    head_len = min(2, len(messages))
    head = messages[:head_len]
    head_chars = sum(_message_char_len(m) for m in head)

    blocks: list[list[dict]] = []
    i = head_len
    while i < len(messages):
        block = [messages[i]]
        i += 1
        while i < len(messages) and messages[i].get("role") == "tool":
            block.append(messages[i])
            i += 1
        blocks.append(block)

    budget = max_chars - head_chars
    kept: list[list[dict]] = []
    kept_chars = 0
    for block in reversed(blocks):
        block_chars = sum(_message_char_len(m) for m in block)
        if kept and kept_chars + block_chars > budget:
            break
        kept.append(block)
        kept_chars += block_chars
    kept.reverse()

    dropped = len(blocks) - len(kept)
    if dropped == 0:
        return messages

    note = {
        "role": "user",
        "content": (
            f"[{dropped} earlier turn(s) were dropped from this transcript to "
            "fit the model's context window. Continue the task using only "
            "the history below - do not assume anything happened that isn't "
            "shown here.]"
        ),
    }
    print(f"[local_agent] RESUME TRIMMED: dropped {dropped} block(s) "
          f"({total} -> {head_chars + kept_chars + len(note['content'])} chars) "
          "to fit the context budget", flush=True)
    return head + [note] + [m for block in kept for m in block]

def _persist_messages(messages, path):
    if not path:
        return
    dirpath = os.path.dirname(path) or "."
    tmp_path = Path(dirpath) / f".{Path(path).name}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:  # noqa: BLE001 (transcript persistence is best-effort; a write failure must not crash the agent loop)
        print(f"[local_agent] persistence error: {e}", flush=True)

class PersistingList(list):
    def __init__(self, *args, transcript_path=None):
        super().__init__(*args)
        self.transcript_path = transcript_path
    # Only append() triggers persistence. extend() is intentionally
    # non-persisting: main() uses extend() for the initial load (both the
    # fresh system+task pair and a resumed transcript), and that initial
    # state is either trivially reconstructible (fresh pair) or already
    # durable in its source resume file. If a future extend() call site
    # needs durability, override extend() too — do not assume it persists.
    def append(self, item):
        super().append(item)
        if self.transcript_path:
            _persist_messages(self, self.transcript_path)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import inference_providers
from app import pipeline_mcp_server as p
from pipeline import edit_guards

CWD = Path.cwd()
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


_COMPLETION_PHRASES = ("all done", "i'm done", "i am done", "all finished", "finished")


def _no_tool_nudge(consecutive: int, content: str = "") -> str:
    """Nudge for an assistant turn that emitted no tool call.

    Early turns get the plain call-to-action (the model may simply have
    forgotten) - unless `content` itself narrates completion (e.g. "All
    done."), in which case it's directed to call the `done` tool specifically
    (live 2026-07-29: a model that narrates "All done." as prose instead of
    calling a tool can burn turns toward NO_TOOL_CAP before the generic nudge
    happens to work). From the third consecutive narration turn onward,
    escalate to behavioral guidance regardless of content: a weak model stuck
    looping on a failing self-test is usually chasing a phantom — its own
    test asserts behavior the correct implementation can never satisfy. Tell
    it to re-check the spec and fix the *test*, not the implementation, then
    call done.

    Phrase matching uses \\b word boundaries, not bare substring search - a
    naive `"finished" in content` also matches inside "unfinished"/
    "refinished", wrongly flagging genuinely incomplete work as completion
    (caught in review 2026-07-29; the acceptance oracle that shipped first
    only exercised "not done yet"/"not finished" and missed this compound-
    word case). A phrase match immediately preceded by the word "not" (e.g.
    "not finished") is a negation, not completion, and is excluded too.
    """
    if consecutive < 3:
        lc = content.lower()
        for phrase in _COMPLETION_PHRASES:
            for match in re.finditer(r"\b" + re.escape(phrase) + r"\b", lc):
                preceding_words = lc[:match.start()].split()
                if preceding_words and preceding_words[-1] == "not":
                    continue
                return "You reported being done — call the done tool now to finish."
        return "Call a tool now (do not write prose)."
    return (
        "You have not called a tool for several turns. If you are stuck on a "
        "failing test that you wrote, that test may assert the wrong behavior — "
        "re-read the task spec. If your implementation already matches the spec, "
        "fix or delete the failing test rather than the implementation, then call "
        "done. Otherwise call a tool now (do not write prose)."
    )


def _repetition_nudge() -> str:
    """Nudge for a read-only action repeated 3x. Steers the model AWAY from
    reading, not back into it.

    The old text said "Use view_file to read the actual current file contents
    and re-read the error's file:line" — i.e. it told the model to repeat the
    exact read that tripped the guard. With PARK_ENABLED=0 (the scheduler
    plist) the per-target guard nudges but never parks, so gpt-oss:20b re-read
    the same file for 31 of 60 steps on 2026-07-20, never editing. The guard
    already intercepts the call (run_tool is never reached on the 3rd+ read),
    so the only lever is what the nudge says — it must direct a concrete
    non-reading action."""
    return (
        "You have repeated the same read-only action 3 times with no progress. "
        "STOP reading — you already have this file's contents in context; "
        "reading it again will not change them. Either:\n"
        "1. str_replace or replace_lines to make the edit you keep reading for, or\n"
        "2. run `pytest -q <test_file>` to get feedback on what actually needs fixing.\n"
        "Do not view_file this path again. Fix the ROOT cause in the correct file."
    )


def _whitespace_visible(line: str) -> str:
    """Make leading whitespace visible so the model can see why its str_replace
    old_str did not match: middle-dot (·) for spaces, arrow (→) for tabs.
    Interior whitespace is left intact so the line stays readable and copiable.
    Leading whitespace is the usual mismatch cause (tabs vs spaces)."""
    stripped = line.lstrip(" \t")
    lead = line[: len(line) - len(stripped)]
    lead = lead.replace(" ", "·").replace("\t", "→")
    return lead + stripped


def _str_replace_not_found_diag(path_str: str, text: str, old_str: str) -> str:
    """Diagnostic for str_replace's 'old_str not found': show up to 3 nearby
    lines (located by the first non-whitespace token of old_str) with line
    numbers and visible whitespace, and steer to replace_lines so the model
    can sidestep the byte-exact-match requirement it cannot meet.

    Keeps the 'ERROR: old_str not found in <path>.' prefix so the main loop's
    success-gating (result.startswith('ERROR')) still treats it as a failure."""
    lines = text.splitlines(keepends=True)
    tok = next((w for w in old_str.split() if w), None)
    picks: list[tuple[int, str]] = []
    if tok:
        for i, ln in enumerate(lines, 1):
            if tok in ln:
                picks.append((i, ln))
                if len(picks) >= 3:
                    break
    if not picks:
        picks = list(enumerate(lines[:3], 1))
    shown = "".join(f"{i:4d}| {_whitespace_visible(ln)}" for i, ln in picks)
    return (
        f"ERROR: old_str not found in {path_str}. Nearest lines "
        f"(leading whitespace shown as · for space, → for tab):\n{shown}"
        f"Use replace_lines(path, start, end, new_str) with those line numbers, "
        f"or copy old_str exactly from the bytes above."
    )
# Qwen3 hybrid thinking control. Qwen3.6-27B (and other Qwen3 dense models)
# emit a  Mattis... Mattis reasoning block by default; in this tool-calling
# loop that breaks dispatch — the block lands in `content` with no native
# tool_calls and the driver spins "no tool call" forever (observed:
# charaf/Huihui-Qwen3.6-27B-abliterated-mlx-nvfp4:thinking-coding, 60-step
# degenerate loop, 2026-07-11). Ollama's /api/chat accepts a top-level
# "think": false to suppress the block at the source so the model emits a
# clean native tool call. Opt-in via LOCAL_AGENT_THINK ("false"/"true");
# omitted from the request when unset so non-Qwen3 models (devstral, gpt-oss,
# qwen3-coder) get an unchanged body. See _ollama_payload() and
# test_local_agent.py.
THINK = os.environ.get("LOCAL_AGENT_THINK", "").strip().lower()
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
# Once a turn's measured prompt_eval_count reaches this fraction of NUM_CTX,
# trim the transcript BEFORE the next turn instead of waiting for a request
# to overflow and 500. Complements (does not replace) the reactive 5xx
# trim-retry below — that one is the backstop for when no measurement has
# landed yet (e.g. the very first turn after a resume already starts over
# budget); this one prevents most 500s from happening at all once a real
# measurement exists.
PROACTIVE_TRIM_THRESHOLD = float(os.environ.get("LOCAL_AGENT_PROACTIVE_TRIM_THRESHOLD", "0.85"))
# Mutating tools: any that produce new code in the worktree. Anything else
# (view_file, bash, checkpoint) is read-only — including checkpoint, which
# commits existing WIP but doesn't add new code; checkpointing without prior
# edits is itself a sign of "spinning."
MUTATING_TOOLS = frozenset({"create_file", "str_replace", "replace_lines"})
# Destructive git ops an agent must never run — they discard the branch's WIP
# commits or working-tree changes. A blind-rework agent once ran
# `git reset --hard <master>` mid-story and threw away its own tests-passed
# WIP; this guard turns those ops into an error the model can react to instead
# of silently destroying its work. The agent edits files via str_replace, so it
# has no legitimate need to discard working-tree state. Patterns scan the whole
# command string so a destructive op inside a `&&`/`;`/`|` chain is still
# caught; `[^;&|]*` keeps each pattern within its own subcommand.
DESTRUCTIVE_GIT_PATTERNS = [
    (re.compile(r"\bgit\s+reset\b[^;&|\n]*--hard"), "git reset --hard"),
    # `(-f|--force)` as a bare substring misses force-clean clusters where `f`
    # is not the first flag (`-xf`, `-df`, `-xdf`) — all force-removes. Match
    # any short-flag cluster containing `f`: `-[a-zA-Z]*f[a-zA-Z]*`.
    (re.compile(r"\bgit\s+clean\b[^;&|\n]*(--force|-[a-zA-Z]*f[a-zA-Z]*)"), "git clean --force"),
    # `git checkout -- <paths>` discards working-tree changes; the `\s--(\s|$)`
    # anchor matches only the pathspec separator, not merge opts like --theirs.
    (re.compile(r"\bgit\s+checkout\b[^;&|\n]*\s--(\s|$)"), "git checkout -- <paths>"),
    (re.compile(r"\bgit\s+restore\b"), "git restore"),
]


def destructive_git_op(cmd: str) -> str | None:
    """Return the name of the destructive git op in `cmd`, or None.

    Scans the whole command string so it catches destructive ops inside
    compound (`&&`/`;`/`|`) commands, not just a bare git invocation.
    """
    for pat, name in DESTRUCTIVE_GIT_PATTERNS:
        if pat.search(cmd):
            return name
    return None
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
            "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "str_replace", "description": "Replace the single unique occurrence of old_str with new_str in an existing file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}},
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
    which provider is active."""
    envelope = inference_providers.get_local_provider().chat(
        messages, model=MODEL, num_ctx=NUM_CTX, temperature=TEMPERATURE,
        tools=TOOLS, endpoint=ENDPOINT, timeout=TIMEOUT,
    )
    return envelope["message"]


def _ollama_payload(messages):
    """Build the Ollama /api/chat request body for one turn.

    Extracted from chat() so the Qwen3 thinking-mode flag (THINK) is
    unit-testable without an HTTP boundary. `think` is included only when
    LOCAL_AGENT_THINK is explicitly "true"/"false" — omitted otherwise so
    non-Qwen3 models get an unchanged request body (see THINK's comment).
    """
    payload = {"model": MODEL, "messages": messages, "tools": TOOLS, "stream": True,
               "options": {"num_ctx": NUM_CTX, "temperature": TEMPERATURE}}
    if THINK in ("true", "false"):
        payload["think"] = (THINK == "true")
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


def _loads_tolerant(candidate):
    """json.loads, tolerating raw control characters inside strings, with a
    triple-quote repair pass as a further fallback. Valid JSON is never
    transformed - both fallbacks only ever ACCEPT more inputs than a strict
    parse would, never reinterpret one that already parses.

    strict=False (observed live, 2026-07-17, Qwen2.5-Coder-14B-4bit on mlx,
    interval_merge task): a distinct malformation from the triple-quote case
    below - the model uses ordinary double-quoted JSON string syntax for a
    multi-line create_file `content` argument, but embeds a RAW literal
    newline instead of escaping it as `\\n`. A strict parse rejects this
    ("Invalid control character"); the triple-quote repair does not apply
    (no triple quotes present), so the tool call was silently dropped every
    retry and the agent looped regenerating the same correct-but-unparseable
    content until the wall-clock park, with the real fix never landing.
    json.loads(strict=False) permits control characters (newlines, tabs,
    etc.) inside strings without weakening validation of anything else -
    it never accepts input a strict parse would reject, it only stops
    rejecting on this one class of already-well-structured input."""
    try:
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        pass
    repaired = _repair_triple_quoted_strings(candidate)
    if repaired != candidate:
        try:
            return json.loads(repaired, strict=False)
        except json.JSONDecodeError:
            pass
    return None


def recover_tool_calls(content):
    """Pull a tool call out of message text when the native field is empty."""
    if not content:
        return None
    text = content.strip().replace("[TOOL_CALLS]", "")
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    m = re.search(r"(\[\s*\{.*\}\s*\]|\{.*\})", text, re.DOTALL)
    if m:
        candidates.append(m.group(1))
    for c in candidates:
        obj = _loads_tolerant(c)
        if obj is None:
            continue
        items = obj if isinstance(obj, list) else [obj]
        out = [{"function": {"name": it["name"], "arguments": it.get("arguments", it.get("parameters", {}))}}
               for it in items if isinstance(it, dict) and "name" in it]
        if out:
            return out
    return None


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
    path = Path(rel) if os.path.isabs(rel) else CWD / rel
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text() if path.exists() else ""
        additions = [p for p in ("agent.log", "__pycache__/", "*.pyc", ".agent_transcript.json") if p not in existing]
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


def _full_suite_result() -> tuple[bool, str]:
    """Run the FULL worktree suite (unscoped), for the L1 CI-fail-rework
    done-gate. Mirrors the merge gate's _ci_status_stub runner
    (tests/benchmark/harness.py:623) and the oracle variant's helper:
    detect_test_command + the heavy lock, run the detected command verbatim
    (no acceptance scoping - this agent has no acceptance oracle), return
    (passed, tail[-500:]). No detectable test command -> (True, '') (nothing
    to fail). Kept in sync with scripts/local_agent_oracle.py:_full_suite_result.

    Mode 40: once tests pass, also run detect_lint_command (if the repo has
    one) and fold a lint failure into the same (False, tail) result - the
    live incident that motivated this was an agent exiting DONE with a
    green suite but a lint-failing CI, because nothing local ever checked
    lint before this. No detected lint command -> unchanged (True, '').
    """
    test_dir, test_cmd = p.detect_test_command(CWD)
    if not test_cmd:
        return True, ""
    argv = test_cmd
    needs_heavy = bool(argv) and p._is_heavy(argv)
    if needs_heavy:
        with p._heavy_lock():
            r = subprocess.run(argv, check=False, cwd=test_dir, capture_output=True, text=True)
    else:
        r = subprocess.run(argv, check=False, cwd=test_dir, capture_output=True, text=True)
    if r.returncode != 0:
        return False, (r.stdout + r.stderr)[-500:]
    lint = p.detect_lint_command(CWD)
    if lint is not None:
        lint_dir, lint_cmd = lint
        lr = subprocess.run(lint_cmd, check=False, cwd=lint_dir, capture_output=True, text=True)
        if lr.returncode != 0:
            return False, (lr.stdout + lr.stderr)[-500:]
    return True, ""


def _reject_done_for_suite(messages: list, step: int, suite_tail: str) -> None:
    """L1: feed a full-suite failure back as a user turn and announce the
    rejection. Used at both `done`-rejection sites (clean tree, and the
    dirty-tree auto-accept escape) so the raised rework done-bar holds and
    the agent can't dodge it by interleaving dirty/clean done calls. The
    caller increments `suite_rejections` and `break`s out of the tool-call
    loop so the next step re-enters with this fed-back excerpt."""
    print(f"[step {step}] done rejected — full test suite still fails "
          f"(rework done-bar); asking agent to fix the failure", flush=True)
    messages.append({"role": "user", "content": (
        "The full test suite still fails. The merge-gate CI will reject "
        f"this on the same failure:\n{suite_tail}\n\nThe bug could be in "
        "the implementation you just changed, or in a test file - do not "
        "assume either side is correct. Re-read the failing test and the "
        "code it exercises, identify which one is actually wrong, and make "
        "ONE targeted fix there. Do NOT call done until `pytest` passes in "
        "full.")})


# Consecutive syntax-rejection count per path, so a model that resubmits the
# same broken content can be escalated instead of silently retrying forever
# (observed: gpt-oss retried near-identical broken content 4x until the
# repetition guard parked the run with no file ever landing). Resets on any
# successful write to that path (see run_tool). NOT a repair mechanism — the
# write itself is always either exactly what the model submitted, or
# refused; this only tracks how many times in a row that refusal happened.
_SYNTAX_REJECT_COUNTS: dict[str, int] = {}

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


def _python_syntax_error(path_str: str, content: str) -> str | None:
    """Return an ERROR string if `path_str` is a .py file and `content` is not
    valid Python, else None. Defense-in-depth against malformed model output
    (e.g. a stray unified-diff leading '+', or an unmatched triple-quote)
    landing on disk — not an attempt to explain why a model emits it.

    The message quotes the offending line (by e.lineno) plus up to 2 lines of
    context either side, verbatim from the SUBMITTED content — never a
    repaired/transformed version — so the model can see exactly what it wrote
    and where."""
    if not path_str.endswith(".py"):
        return None
    try:
        # compile(), not ast.parse(): ast.parse() only validates grammar
        # (parens balanced, indentation forms a legal block structure) - it
        # does NOT check that `return`/`yield` sit inside a function or
        # `break`/`continue` inside a loop. Those are SyntaxErrors too, but
        # only surface at compile() time. Observed live: a dedented `for`
        # loop landed `return` at module scope, ast.parse() accepted it, and
        # the file reached the groundtruth oracle as an import-breaking
        # SyntaxError this guard exists specifically to catch before disk.
        compile(content, path_str, "exec")
    except SyntaxError as e:
        lines = content.splitlines()
        lineno = e.lineno or 0
        offending = lines[lineno - 1] if 1 <= lineno <= len(lines) else ""
        ctx_start = max(1, lineno - 2)
        ctx_end = min(len(lines), lineno + 2)
        context = "\n".join(f"{i:4d}| {lines[i - 1]}" for i in range(ctx_start, ctx_end + 1))
        return (
            f"ERROR: content for {path_str} has invalid Python syntax at line "
            f"{lineno}: {e}. Offending line: {offending!r}\n"
            f"Context (submitted content, lines {ctx_start}-{ctx_end}):\n{context}\n"
            f"Check for stray formatting artifacts (e.g. a leading '+' from "
            f"pasted diff/patch text, or an unmatched/duplicated triple-quote) "
            f"and retry."
        )
    return None


def _function_name_scopes(tree: ast.AST) -> dict[str, tuple[set[str], set[str]]]:
    """Map each function's name to (assigned_names, loaded_names) within it.

    Shallow and conservative on purpose: every Name node anywhere inside the
    function body (including nested functions/comprehensions) is attributed
    to the outer function rather than modeling real scope nesting, and two
    functions sharing the same name (e.g. same-named methods on different
    classes) collide in the returned dict - a false negative (the check
    silently doesn't fire), never a false positive. Good enough for a
    presence check ("was this name assigned/read anywhere near here"), not a
    real data-flow analysis."""
    scopes: dict[str, tuple[set[str], set[str]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assigned: set[str] = set()
            loaded: set[str] = set()
            for n in ast.walk(node):
                if isinstance(n, ast.Name):
                    if isinstance(n.ctx, ast.Store):
                        assigned.add(n.id)
                    elif isinstance(n.ctx, ast.Load):
                        loaded.add(n.id)
                elif isinstance(n, ast.arg):
                    assigned.add(n.arg)
            scopes[node.name] = (assigned, loaded)
    return scopes


def _newly_undefined_module_defs(old_content: str, new_content: str) -> list[str]:
    """Return names of module-level `def`/`class` statements present in
    `old_content` but deleted by this edit while a reference to that name
    survives anywhere in `new_content` - the shape of the MODE-29 incident
    (2026-07-22): a replace_lines edit deleted only the
    `def _review_story_impl(...):` line itself, leaving its ~300-line body
    correctly indented as trailing dead code inside the CALLER's function
    and the caller's `return _review_story_impl(...)` untouched. That
    result is syntactically valid Python (compile() accepts it - the body
    is now just unreachable code after an earlier return), so only a
    NameError surfaces, at runtime, on every call.

    `_newly_undefined_names` above only tracks function-LOCAL Name-Store/
    Load bindings via `_function_name_scopes` and cannot see this: a `def`
    statement's name isn't an `ast.Name` node, and the deleted function's
    own body being reachable syntax elsewhere is irrelevant to whether the
    NAME `_review_story_impl` is still defined. This is deliberately a
    separate, narrower check (top-level statements only, not nested defs)
    rather than folding module scope into `_function_name_scopes`."""
    try:
        old_tree = ast.parse(old_content)
        new_tree = ast.parse(new_content)
    except SyntaxError:
        return []
    old_top_defs = {
        n.name for n in old_tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    new_top_defs = {
        n.name for n in new_tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    removed = old_top_defs - new_top_defs
    if not removed:
        return []
    new_loaded = {
        n.id for n in ast.walk(new_tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    return [
        f"{name} (module-level def deleted but still called)"
        for name in sorted(removed & new_loaded)
    ]


def _newly_undefined_module_vars(old_content: str, new_content: str) -> list[str]:
    """Return names of module-level VARIABLE assignments (top-level
    ast.Assign / ast.AnnAssign targets) present in `old_content` but deleted by
    this edit while a reference to that name survives anywhere in
    `new_content` - the shape of the MODE-43 incident (2026-07-30,
    TRANSPORT-ALIAS-READERS): a replace_lines edit on
    scripts/local_agent_oracle.py replaced the module-level
    `TIMEOUT = float(os.environ.get("LOCAL_AGENT_TIMEOUT", "900"))` line with a
    duplicate of the preceding `NUM_CTX = ...` line, deleting the `TIMEOUT`
    assignment while every later `TIMEOUT` read survived. compile() accepts the
    result - a missing module-level name is a runtime NameError, not a
    SyntaxError - so only a NameError surfaces, on every call.

    `_newly_undefined_names` only tracks function-LOCAL bindings and
    `_newly_undefined_module_defs` only covers `def`/`class` names - NEITHER
    sees a deleted module-level variable assignment. Deliberately a separate,
    narrower check (top-level statements only) mirroring
    `_newly_undefined_module_defs`."""
    try:
        old_tree = ast.parse(old_content)
        new_tree = ast.parse(new_content)
    except SyntaxError:
        return []

    def _top_assigned_names(tree: ast.AST) -> set[str]:
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    names.update(
                        n.id for n in ast.walk(tgt) if isinstance(n, ast.Name)
                    )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        return names

    removed = _top_assigned_names(old_tree) - _top_assigned_names(new_tree)
    if not removed:
        return []
    new_loaded = {
        n.id for n in ast.walk(new_tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    return [
        f"{name} (module-level variable deleted but still read)"
        for name in sorted(removed & new_loaded)
    ]


def _newly_undefined_names(path_str: str, old_content: str, new_content: str) -> list[str]:
    """Return "name (in function)" entries for every name whose only
    assignment within a function existed in `old_content`, was read later in
    that SAME function, and has been deleted by this edit while the read
    survives in `new_content` - the exact shape of two separate live
    incidents (gpt-oss:20b deleting `plan_role_config = _plan_role_config(...)`,
    qwen3-coder:30b deleting `branch = ...`/`worktree = ...`), both of which
    landed a NameError/UnboundLocalError that compile()-based syntax
    checking cannot catch (an undefined name is a runtime error, not a
    SyntaxError). Returns [] (never raises) on a non-.py path or when either
    side fails to parse - a genuine syntax problem is `_python_syntax_error`'s
    job, not this check's."""
    if not path_str.endswith(".py"):
        return []
    try:
        old_scopes = _function_name_scopes(ast.parse(old_content))
        new_scopes = _function_name_scopes(ast.parse(new_content))
    except SyntaxError:
        return []
    orphaned = []
    for name, (old_assigned, old_loaded) in old_scopes.items():
        if name not in new_scopes:
            continue
        new_assigned, new_loaded = new_scopes[name]
        for var in sorted(old_assigned & old_loaded):
            if var in new_loaded and var not in new_assigned:
                orphaned.append(f"{var} (in {name})")
    orphaned.extend(_newly_undefined_module_defs(old_content, new_content))
    orphaned.extend(_newly_undefined_module_vars(old_content, new_content))
    return orphaned


def _try_repair_indentation(content: str) -> tuple[str, str] | None:
    """Attempt a deterministic, semantics-preserving indentation repair on
    `content` when compile() rejects it with an IndentationError (unexpected
    indent / unexpected unindent / unindent does not match any outer level).

    The repair ITERATES: re-indent the offending line (e.lineno) to the
    leading whitespace of the nearest preceding non-blank, non-comment line,
    re-compile, and if compile still flags an IndentationError fix the next
    offending line too, until the content compiles clean or a non-indentation
    error (or no progress) is hit. Only when the final content compiles clean
    is (repaired_content, note) returned; otherwise None (fall through to the
    normal rejection path).

    The iteration is required because the decoding defect drops the
    indentation on the `def` line after EVERY decorator in the file, not
    just the first (observed live, 2026-07-17, lru_cache: both the
    `@property` getter `def size` AND the `@size.setter` `def size` were
    dedented to column 0). A single-line repair fixed the getter, but the
    setter still broke compile, so the repair returned None and correct code
    was rejected every retry until the wall-clock park (Mode 21 sibling).

    Rationale (GUIDED_DECOMPOSITION_PLAN.md, 2026-07-16, lru_cache t7/t8/
    t10/t11): the 14B has a reproducible decoding defect that drops the
    leading indentation on the line immediately after a decorator - it
    writes `    @property` then `def size(self):` at column 0, a SyntaxError
    (unexpected unindent) it resubmits byte-identical until it parks. A
    prompt-level worked example did NOT prevent it (t11: the defect is
    decoding-level, not understanding-level). Re-indenting the dedented
    line to match the preceding decorator is exactly what the model
    intended and is whitespace-only, so the groundtruth logic gate still
    catches any real error; this converts a syntax death-loop into
    executable code the test gate can evaluate.

    Scoped to IndentationError only: other SyntaxErrors (return/yield
    outside a function, dangling triple-quote, stray diff '+') are real
    logic/format errors the model must fix, not indentation, and are left
    for the normal rejection path."""
    lines_changed = 0
    last_lineno = None
    for _ in range(64):  # bound: no real file has >64 dedented decorator lines
        try:
            compile(content, "<repair>", "exec")
            break  # clean - done
        except IndentationError as e:
            lineno = e.lineno or 0
        except SyntaxError:
            return None  # non-indentation syntax error - do not touch
        if lineno == last_lineno:
            return None  # re-indent didn't advance past this line - can't fix
        lines = content.splitlines(keepends=True)
        if not (1 <= lineno <= len(lines)):
            return None
        # Find the nearest preceding non-blank, non-comment line to take the
        # target indentation from.
        target = None
        for i in range(lineno - 1, 0, -1):
            prev = lines[i - 1]
            stripped = prev.strip()
            if not stripped or stripped.startswith("#"):
                continue
            target = len(prev) - len(prev.lstrip(" \t"))
            break
        if target is None:
            return None  # no preceding line to reference (e.g. top-level indent)
        cur = lines[lineno - 1]
        cur_stripped = cur.lstrip(" \t")
        cur_indent = len(cur) - len(cur_stripped)
        if cur_indent == target:
            return None  # already at target - re-indenting won't help this line
        lines[lineno - 1] = (" " * target) + cur_stripped
        content = "".join(lines)
        lines_changed += 1
        last_lineno = lineno
    try:
        compile(content, "<repair>", "exec")
    except SyntaxError:
        return None  # exhausted without compiling clean - leave for rejection
    if lines_changed == 0:
        return None  # original was already valid
    note = (f"auto-reindented {lines_changed} dedented line(s) to match the "
            f"preceding line's indentation (decorator-dedent decoding defect)")
    return content, note


def _record_syntax_rejection(path_str: str, err: str, existing_line_count: int | None = None) -> str:
    """Bump the consecutive-rejection counter for `path_str` and, from the
    second consecutive rejection onward, append a nudge to regenerate the
    ENTIRE file from scratch instead of resubmitting the same broken content.
    If *existing_line_count* is provided and exceeds THRESHOLD (500 lines),
    use a different smaller-anchored-edit nudge instead - regenerating a
    large file from scratch risks corrupting the untouched majority of it.
    """
    count = _SYNTAX_REJECT_COUNTS.get(path_str, 0) + 1
    _SYNTAX_REJECT_COUNTS[path_str] = count
    if count >= 2:
        # Threshold for large files: 500 lines. If the file is larger than this,
        # advise a smaller anchored edit instead of regenerating.
        THRESHOLD = 500
        if existing_line_count is not None and existing_line_count > THRESHOLD:
            err += (
                f"\nDo NOT resubmit the same content. The file has {existing_line_count} lines; "
                "instead retry with a SMALLER anchored str_replace: quote a few exact lines of surrounding context immediately before and after the specific span you need to change, and change only that minimal span."
            )
        else:
            err += (
                "\nDo NOT resubmit the same content. Regenerate the ENTIRE file "
                "from scratch, with no diff markers and no surrounding prose."
            )
    return err
def _lint_feedback_for(path_str: str) -> str:
    """Mode 40: after a successful write to `path_str`, run a fast,
    single-file-scoped lint check and return a short findings suffix to
    append to the tool's success message, or "" when there's nothing to
    report. Only ruff supports cheap single-file scoping (swap the "."
    arg for the file path); other detected linters (eslint, golangci-lint)
    are skipped here and only caught by the full-repo _full_suite_result
    check at done-time, to keep this per-edit check fast.

    The point is closing the loop that let a live incident ship 18 ruff
    violations undetected until CI: the model previously had zero lint
    signal until the very end of a run (or, before this fix, never at
    all locally). This surfaces it at the moment the mistake is made.
    """
    if not path_str.endswith(".py"):
        return ""
    lint = p.detect_lint_command(CWD)
    if lint is None:
        return ""
    lint_dir, cmd = lint
    if not cmd or "ruff" not in cmd[0]:
        return ""
    try:
        res = subprocess.run(  # noqa: PLW1510 (check=False would break test fakes with fixed signatures; see test_local_agent.py)
            [cmd[0], "check", path_str], cwd=lint_dir,
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if res.returncode == 0:
        return ""
    findings = (res.stdout + res.stderr).strip()[:800]
    return f"\n\n[lint] `ruff check {path_str}` found issues (fix before calling done):\n{findings}"


def run_tool(fn, args) -> str:
    if fn == "create_file":
        path = CWD / args["path"]
        if (path.exists() and path.read_text().strip()
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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        _SYNTAX_REJECT_COUNTS.pop(args["path"], None)
        _CREATED_THIS_RUN.add(args["path"])
        return (f"created {args['path']}" + (f" ({note})" if note else "")
                + _lint_feedback_for(args['path']))
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
        path.write_text(new_text)
        _SYNTAX_REJECT_COUNTS.pop(args["path"], None)
        return (f"edited {args['path']}" + (f" ({note})" if note else "")
                + _lint_feedback_for(args['path']))
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
            return (
                f"ERROR: this edit to {args['path']} deletes {len(deletions)} line(s) "
                f"that don't appear to survive (as-is or rewritten) in your replacement:"
                f"{report}\n\nRevise new_str to preserve these lines, or if the deletion "
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
                + removed_echo + dup_warn + _lint_feedback_for(args['path']))
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


def recover_from_oversized_5xx(messages, chat_fn, *, step=None):
    """Recover from a 5xx on an oversized transcript by retrying with an
    escalating (shrinking) context budget before giving up. A single trim-retry
    can also 500 on a still-oversized payload, so shrink harder each round.
    Returns the assistant message dict on success, or None if every round fails
    (caller gives up). Mutates ``messages`` in place. Bounded: 3 budgets."""
    print(f"[step {step}] 5xx after {CHAT_MAX_ATTEMPTS} attempts with an "
          f"oversized transcript; escalating trim and retrying", flush=True)
    for fraction in (0.75, 0.50, 0.30):
        budget_chars = int(NUM_CTX * _effective_chars_per_token() * fraction)
        trimmed = _trim_resumed_transcript(messages, budget_chars)
        if len(trimmed) == len(messages):
            return None  # trim could not shrink the payload; nothing more to do
        messages[:] = trimmed
        try:
            return chat_fn(messages)
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise  # only 5xx is escalation-worthy; propagate 4xx and others
            continue  # 5xx: shrink harder on the next smaller budget
    return None  # all budgets exhausted on a persistent 5xx


def main() -> int:
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
    failed_sr: dict[str, int] = {}
    nudged_sr_fail: set[str] = set()
    last_progress_step = 0
    start_time = time.monotonic()

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
        try:
            m = chat(messages)
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                print(f"[step {step}] LLM call failed: {e}", flush=True)
                if worktree_dirty():
                    auto_wip_commit("llm error")
                return 1
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
            if len(trimmed) != len(messages):
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

        for tc in tcs:
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
                        if REWORK_FULL_SUITE:
                            suite_ok, suite_tail = _full_suite_result()
                            if not suite_ok:
                                suite_rejections += 1
                                auto_wip_commit("commit enforcement")
                                if suite_rejections >= REWORK_SUITE_REJECT_CAP:
                                    print(f"[step {step}] rework suite-reject cap "
                                          f"({REWORK_SUITE_REJECT_CAP}) reached; agent "
                                          f"cannot green the full suite — parking", flush=True)
                                    return 2
                                _reject_done_for_suite(messages, step, suite_tail)
                                break
                        auto_wip_commit("commit enforcement")
                        print(f"[step {step}] DONE with auto-WIP-commit (agent left tree dirty): "
                              f"{args.get('summary', '')}", flush=True)
                        return 0
                    print(f"[step {step}] done rejected — worktree dirty, asking agent to commit", flush=True)
                    messages.append({"role": "user", "content": (
                        "You have uncommitted changes. Commit your work with git "
                        "(git add -A && git commit -m ...) before calling done.")})
                    break
                # L1: on a CI-fail-rework round, the reviewer was acceptance-
                # scoped and never saw the agent's own test - so a clean
                # worktree + `done` is not sufficient. Require the FULL suite
                # green; otherwise feed the failing excerpt back and reject
                # done so the agent fixes its own broken assertion (or the step
                # cap binds). Non-rework dispatches skip this gate entirely.
                if REWORK_FULL_SUITE:
                    suite_ok, suite_tail = _full_suite_result()
                    if not suite_ok:
                        suite_rejections += 1
                        if suite_rejections >= REWORK_SUITE_REJECT_CAP:
                            if worktree_dirty():
                                auto_wip_commit("rework suite-reject cap")
                            print(f"[step {step}] rework suite-reject cap "
                                  f"({REWORK_SUITE_REJECT_CAP}) reached; agent "
                                  f"cannot green the full suite — parking", flush=True)
                            return 2
                        _reject_done_for_suite(messages, step, suite_tail)
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
                    break
                if worktree_dirty():
                    auto_wip_commit("parked on repetition")
                if not PARK_ENABLED:
                    # Suppressed: let the nudge steer and continue to the next
                    # step instead of terminating. The step cap bounds the run.
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

            # Failing-str_replace loop guard. str_replace is excluded from the
            # per-target repetition guard above, so a no-match loop on one file
            # (the 2026-07-20 server.py wall: gpt-oss retried slightly-different
            # old_str values that never matched the file's whitespace) runs
            # uncaught. After 2 consecutive FAILED str_replace on the same path,
            # steer to replace_lines (line numbers, no byte-exact match). A
            # successful str_replace resets the counter and re-arms the nudge.
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
                            break
                        return 3
                    recent_tools.clear()

    print("[ended without done — step cap reached]", flush=True)
    if worktree_dirty():
        auto_wip_commit("step cap reached")
    return 2


if __name__ == "__main__":
    sys.exit(main())
