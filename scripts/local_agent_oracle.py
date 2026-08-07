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

# Rough chars-per-token estimate (no tokenizer available here). Ported from
# local_agent.py - see that file's comment for the live incident (story
# 93fdc371, 2026-07-20) that motivated this. Keep both copies in sync.
_CHARS_PER_TOKEN_ESTIMATE = 4

# Calibrated at runtime from ollama's own measured prompt_eval_count. Ported
# from local_agent.py - see that file's comment for the live incident
# (2026-07-29 ollama server log, ~2.35 chars/token measured vs the 4.0 guess)
# that motivated this. Keep both copies in sync.
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


def _total_chars(messages) -> int:
    """Total character weight of a transcript. The trim path compares this
    rather than len(messages): the tool-output eviction tier shrinks a
    transcript substantially without changing its message count, so a
    length-based "did the trim help?" test silently discards its result."""
    return sum(_message_char_len(m) for m in messages)


# Tool-output eviction (tier 1 of the trim). Nearly all of a transcript's
# bytes are tool-role output (a `git diff`, a file view, a test log) while
# nearly all of its MEANING is in the short assistant turns that decided to
# make those calls. Dropping whole blocks therefore throws away the agent's
# decision history to reclaim bytes that were mostly log noise. Claude Code's
# /compact does the same thing in the same order ("clears older tool outputs
# first, then summarizes the conversation if needed").
#
# _EVICT_HEAD_CHARS keeps each evicted output's opening span rather than
# blanking it: the signal in a tool result is at the FRONT ("ERROR: content
# for rate_limiter.py has invalid Python syntax at line 106"), and that one
# line is the difference between an agent that retries the write correctly
# and one that re-derives the failure from scratch.
_EVICT_HEAD_CHARS = int(os.environ.get("LOCAL_AGENT_EVICT_HEAD_CHARS", "240"))
# The newest outputs are what the model is actually acting on this turn;
# evicting those would break the very next decision. Eviction runs oldest-first
# and never touches this many trailing tool messages.
_EVICT_KEEP_RECENT = int(os.environ.get("LOCAL_AGENT_EVICT_KEEP_RECENT", "4"))

# Static allowlist of test-runner command prefixes, used only to locate the
# last test result in a span being dropped. Deliberately a fixed list rather
# than runtime language detection, matching the rest of the harness.
# Hard cap on the digest note; it must never cost more than the span it
# replaces, or the trim becomes a net expansion and is discarded.
_DIGEST_MAX_CHARS = int(os.environ.get("LOCAL_AGENT_DIGEST_MAX_CHARS", "600"))

_TEST_COMMAND_MARKERS = (
    "pytest", "npm test", "yarn test", "cargo test", "go test",
    "mvn test", "gradlew test", "gradle test", "make test", "unittest",
)


def _evict_tool_outputs(messages: list, max_chars: int) -> list:
    """Shrink `messages` toward max_chars by truncating tool-role content,
    oldest first, without removing any message. Returns a new list; the
    original dicts are never mutated (they are the live, persisted
    transcript). Stops as soon as the total fits."""
    total = sum(_message_char_len(m) for m in messages)
    if total <= max_chars:
        return messages

    tool_idxs = [i for i, m in enumerate(messages)
                 if i >= 2 and m.get("role") == "tool"]
    # _EVICT_KEEP_RECENT is a SOFT preference, not a floor: pass 1 spares the
    # newest outputs, and pass 2 reaches them only if the budget still isn't
    # met. A hard floor would hand control to the block-dropping tier while
    # recoverable bytes were still sitting in tool output - and a truncated
    # recent result is strictly better than a dropped assistant decision,
    # since eviction keeps the result's leading span either way.
    older = tool_idxs[:-_EVICT_KEEP_RECENT] if _EVICT_KEEP_RECENT else tool_idxs
    recent = tool_idxs[len(older):]

    out = list(messages)
    for i in list(older) + list(recent):
        if total <= max_chars:
            break
        original = str(out[i].get("content") or "")
        if len(original) <= _EVICT_HEAD_CHARS:
            continue
        replacement = (
            original[:_EVICT_HEAD_CHARS]
            + f"\n[... {len(original) - _EVICT_HEAD_CHARS} chars of this tool "
              "output evicted to fit the context window ...]"
        )
        out[i] = {**out[i], "content": replacement}
        total -= len(original) - len(replacement)
    return out


def _tool_call_pairs(message: dict):
    """Yield (tool_name, args_dict) for each well-formed call on `message`."""
    for tc in (message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(args, dict) and fn.get("name"):
            yield fn["name"], args


def _strip_eviction_marker(text: str) -> str:
    """Drop the trailing "[... N chars ... evicted ...]" note left by
    _evict_tool_outputs. The digest reads blocks AFTER eviction has run, so
    without this the last-test-result line reports the marker itself instead
    of the failure summary it is supposed to surface."""
    head, sep, tail = text.rpartition("\n[... ")
    if sep and "evicted to fit the context window" in tail:
        return head.strip()
    return text.strip()


def _dropped_span_digest(blocks: list) -> str:
    """A factual digest of what happened in the blocks being dropped.

    Deterministic and evidence-only: every line is read straight off the
    span's own tool calls and results, so unlike an LLM summary it cannot
    invent a file or claim a passing suite that actually errored. A live
    comparison on 2026-08-07 had a local model assert "0 errors (all tests
    pass)" for a span whose test run had in fact failed to collect and whose
    file writes had all been rejected for invalid syntax - which is why this
    tier is mechanical rather than generated. Sections with no evidence are
    omitted entirely rather than filled with a guess.
    """
    written: list[str] = []
    commands: list[str] = []
    last_test: str | None = None

    flat = [m for block in blocks for m in block]
    for idx, m in enumerate(flat):
        for name, args in _tool_call_pairs(m):
            path = args.get("path")
            if name in ("create_file", "str_replace", "replace_lines") and path:
                if path not in written:
                    written.append(path)
            elif name == "bash" and args.get("command"):
                command = str(args["command"])
                commands.append(command)
                if any(marker in command for marker in _TEST_COMMAND_MARKERS):
                    for follower in flat[idx + 1:]:
                        if follower.get("role") != "tool":
                            break
                        last_test = _strip_eviction_marker(
                            str(follower.get("content") or ""))[-200:]
                        break

    lines = []
    if written:
        lines.append("Files already modified in the dropped span: "
                     + ", ".join(written))
    if commands:
        lines.append(f"Shell commands already run there: {len(commands)}"
                     f" (most recent: {commands[-1][:120]})")
    if last_test:
        lines.append(f"Last test result seen there: {last_test}")
    digest = "\n".join(lines)
    return digest[:_DIGEST_MAX_CHARS]


def _trim_resumed_transcript(messages: list, max_chars: int) -> list:
    """Bound a resumed transcript to max_chars when it would otherwise
    overflow the model's context window.

    Two tiers, cheapest first (see _evict_tool_outputs for why this order):

    1. Evict older tool-role OUTPUT down to its leading span, keeping every
       assistant decision intact. Most transcripts fit again after this, and
       the agent keeps its full history of what it chose to do and why.
    2. Only if that is still not enough, drop whole blocks from the oldest.

    Tier 2 always preserves the original system+task head (messages[:2]) and
    works backward from the end to keep as much recent activity as fits.
    Content is dropped in whole blocks (an assistant message plus any
    tool-role messages immediately following it, which are that call's
    results) so a tool_calls message is never split from its own tool
    response - either both survive or both are dropped. When anything is
    dropped, a synthetic user note carrying _dropped_span_digest's factual
    summary of the dropped span is inserted between the head and the
    surviving tail, so the model isn't confused by a discontinuity in its own
    history and doesn't re-derive facts that were already established.
    """
    total = sum(_message_char_len(m) for m in messages)
    if total <= max_chars:
        return messages

    evicted = _evict_tool_outputs(messages, max_chars)
    evicted_total = sum(_message_char_len(m) for m in evicted)
    if evicted_total <= max_chars:
        print(f"[local_agent] CONTEXT EVICTED: truncated older tool output "
              f"({total} -> {evicted_total} chars); all assistant turns kept",
              flush=True)
        return evicted
    messages = evicted
    total = evicted_total

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

    digest = _dropped_span_digest(blocks[:len(blocks) - len(kept)])
    note_text = (
        f"[{dropped} earlier turn(s) were dropped from this transcript to "
        "fit the model's context window. Continue the task using only "
        "the history below - do not assume anything happened that isn't "
        "shown here.]"
    )
    if digest:
        note_text += (
            "\n[Established facts from the dropped turns, recorded directly "
            "from what was run - treat these as already done:\n" + digest + "]"
        )
    note = {"role": "user", "content": note_text}
    print(f"[local_agent] RESUME TRIMMED: dropped {dropped} block(s) "
          f"({total} -> {head_chars + kept_chars + len(note_text)} chars) "
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


# The pipeline_mcp_server module lives in this script's parent directory.
# The agent subprocess runs with cwd=<worktree>, NOT the pipeline repo, so
# without this path insert the import below fails with ModuleNotFoundError
# and the harness dies before printing [boot] (which check_story_status
# reads as "process never reached main()" = a genuine failed launch).
# local_agent.py has the same line; the oracle variant didn't until PR #32
# because it didn't depend on pipeline_mcp_server.
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
MAX_STEPS = int(os.environ.get("PIPELINE_TRANSPORT_MAX_STEPS", "30"))
# Cap consecutive assistant turns that emit no tool call. A weak model stuck
# on a self-inflicted phantom failure — its own test asserts non-standard
# behavior the correct implementation can never satisfy — will narrate its
# "next step" as prose indefinitely; the generic "call a tool" nudge cannot
# break this because no real action resolves a self-contradictory test. Park
# after this many consecutive no-tool turns rather than burning the whole run.
NO_TOOL_CAP = int(os.environ.get("LOCAL_AGENT_NO_TOOL_CAP", "5"))
# Mirror local_agent.py: park a rework round after this many full-suite
# done-rejections so a model that cannot green the suite stops burning budget.
REWORK_SUITE_REJECT_CAP = int(os.environ.get("LOCAL_AGENT_REWORK_SUITE_REJECT_CAP", "3"))
TEMPERATURE = float(os.environ.get("PIPELINE_TRANSPORT_TEMPERATURE", "0.3"))


_COMPLETION_PHRASES = ("all done", "i'm done", "i am done", "all finished", "finished")


def _no_tool_nudge(consecutive: int, content: str = "") -> str:
    """Nudge for an assistant turn that emitted no tool call.

    Early turns get the plain call-to-action (the model may simply have
    forgotten) - unless `content` itself narrates completion (e.g. "All
    done."), in which case it's directed to call the `done` tool specifically.
    From the third consecutive narration turn onward, escalate to behavioral
    guidance regardless of content: a weak model stuck looping on a failing
    self-test is usually chasing a phantom — its own test asserts behavior
    the correct implementation can never satisfy. Tell it to re-check the
    spec and fix the *test*, not the implementation, then call done.

    Kept in sync with scripts/local_agent.py's _no_tool_nudge (this file is a
    verbatim copy used for acceptance grading - see pipeline_mcp_server.py).
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
# Per-bash-invocation timeout. The model can call `cargo fetch` and wedge on
# a network index update forever; without this the agent loop blocks on a
# single subprocess.run until cargo eventually times out (if at all).
# 10 min is generous — most cargo invocations in a worktree finish in <60s
# on a warm target/ — but bounded so a wedged cargo doesn't pin the agent.
BASH_TIMEOUT = float(os.environ.get("LOCAL_AGENT_BASH_TIMEOUT_SECONDS", "600"))

# Streaming + retry for the Ollama chat round-trip. The original single
# blocking httpx.post(stream=False, timeout=900s) with NO retry meant one
# transient Ollama queue stall (or a network blip) killed the whole run
# after 7-11 steps of real progress — observed 2026-06-28: all 3 e2e agents
# died at "LLM call failed: timed out" mid-iteration. Streaming means a
# slow-but-progressing generation never trips a wall-clock timeout (only a
# true silence stall does); retry means a transient stall doesn't kill the
# run. With OLLAMA_NUM_PARALLEL matched to MAX_CONCURRENT_AGENTS there's no
# steady queue, so these guard the residual transients (prefill, network,
# Ollama hiccup, 5xx).
#   READ_SILENCE_SECONDS — per-chunk read timeout. A generation streams a
#       chunk every ~1-2s, so this only fires on a genuine stall (no bytes
#       for N seconds). 180s is generous enough to absorb prompt prefill
#       (30-60s on a 16k-ctx 24b model) plus any residual queue wait.
#   CONNECT_TIMEOUT_SECONDS — time to wait for Ollama to start responding to
#       a *cold* model load, before the client gives up. Was hardcoded to
#       10.0 - too short: observed live 2026-07-13, both glm-4.7-flash
#       (~17s cold load) and qwen3-coder:30b timed out identically on a
#       fresh (never-loaded-this-session) dispatch. Worse than a plain
#       failure: Ollama aborts the in-flight load and frees the memory it
#       had claimed the instant the client disconnects, so a too-short
#       connect timeout produces an infinite load/abort/retry cycle (visible
#       as repeating multi-GB memory bursts) that can never converge, rather
#       than a genuine, retryable failure. 60s covers a cold load of a
#       20GB-class model on Apple Silicon with headroom.
#   CHAT_MAX_ATTEMPTS — turns to try before giving up (main()'s except
#       then commits WIP and returns 1, same as before, but only after all
#       attempts are exhausted).
#   CHAT_RETRY_BACKOFF — sleep before attempt N is BACKOFF * N seconds.
READ_SILENCE_SECONDS = float(os.environ.get("LOCAL_AGENT_READ_SILENCE_SECONDS", "180"))
CONNECT_TIMEOUT_SECONDS = float(os.environ.get("LOCAL_AGENT_CONNECT_TIMEOUT_SECONDS", "60"))
CHAT_MAX_ATTEMPTS = int(os.environ.get("LOCAL_AGENT_CHAT_MAX_ATTEMPTS", "3"))
CHAT_RETRY_BACKOFF = float(os.environ.get("LOCAL_AGENT_CHAT_RETRY_BACKOFF", "5"))

# Read-heavy-pattern guard (ported from local_agent.py PR #30 — this variant
# missed the original PR because backend routes to it whenever a story carries
# an `acceptance` block). Tracks the last N tool names the model called and
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
# still parks at the strict 2 * READ_HEAVY_WINDOW (12) -- only all-distinct
# exploration gets the longer leash, and it is still bounded, not disabled.
# Ported verbatim from local_agent.py per the Mode 3a lesson: any loop-guard
# change in the base harness must be mirrored here or acceptance-bearing
# stories silently regress.
READ_HEAVY_DISTINCT_WINDOWS = int(os.environ.get("LOCAL_AGENT_READ_HEAVY_DISTINCT_WINDOWS", "3"))
# Net-progress guard. Ported from local_agent.py (2026-07-22,
# MODE-29-REVIEW-STORY-LOCK-GUARD) — see that file for the full rationale.
# Default (30) stays above the read-heavy guard's own distinct-exploration
# cap (6 + 3*6 = 24) so it doesn't preempt that guard's already-tuned park.
NET_PROGRESS_MAX_STEPS = int(os.environ.get("LOCAL_AGENT_NET_PROGRESS_MAX_STEPS", "30"))
# Proactive-trim threshold. Ported from local_agent.py - keep both copies in
# sync.
PROACTIVE_TRIM_THRESHOLD = float(os.environ.get("LOCAL_AGENT_PROACTIVE_TRIM_THRESHOLD", "0.85"))
# Mutating tools: any that produce new code in the worktree. Anything else
# (view_file, bash, checkpoint) is read-only — including checkpoint, which
# commits existing WIP but doesn't add new code; checkpointing without prior
# edits is itself a sign of "spinning."
MUTATING_TOOLS = frozenset({"create_file", "str_replace", "replace_lines"})
# Destructive git ops an agent must never run — they discard the branch's WIP
# commits or working-tree changes (a blind-rework agent once ran
# `git reset --hard <master>` and threw away its own tests-passed WIP). Ported
# verbatim from local_agent.py per the Mode 3a lesson: any loop-guard change in
# the base harness must be mirrored here or acceptance-bearing stories silently
# regress.
DESTRUCTIVE_GIT_PATTERNS = [
    (re.compile(r"\bgit\s+reset\b[^;&|\n]*--hard"), "git reset --hard"),
    (re.compile(r"\bgit\s+clean\b[^;&|\n]*(--force|-[a-zA-Z]*f[a-zA-Z]*)"), "git clean --force"),
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
# Parking kill-switch (env LOCAL_AGENT_PARK_ENABLED, default "1"). See
# local_agent.py for the full rationale. When disabled, the loop guards still
# nudge but never terminate (return 3) — the step cap bounds the run. Kept in
# sync with local_agent.py per the rule that any loop-guard change ports to
# both harnesses or acceptance-bearing stories silently regress.
PARK_ENABLED = os.environ.get("LOCAL_AGENT_PARK_ENABLED", "1") != "0"

# L1 (REVIEWER_ESCALATION_PLAN.md): on a rework round triggered by a merge-gate
# CI failure, the defect is the agent's OWN committed test, but the acceptance
# oracle excludes that file - so oracle-green would let the loop terminate and
# re-fail CI on the same assertion every round (observed 2026-07-17 on gpt-oss
# token_bucket: 3.0 vs 9.0, three identical rounds). Set by dispatch_story when
# the rework was CI-triggered (story["ci_rework"] -> backend.dispatch
# rework_full_suite -> this env). When set, finish_if_green additionally
# requires the FULL worktree suite green before terminating, feeding the
# failing excerpt back so the agent must fix its own test. Cold-start dispatches
# never set this, so their oracle-green done-bar is byte-for-byte unchanged.
REWORK_FULL_SUITE = os.environ.get("LOCAL_AGENT_REWORK_FULL_SUITE") == "1"

# Oracle paths: the harness owns these, the model cannot author or edit them.
# Set by backend.OllamaDriver.dispatch as a JSON list when the story carries
# an `acceptance` block; empty otherwise (in which case this variant should
# not have been launched in the first place).
try:
    _ORACLE_RAW = os.environ.get("LOCAL_AGENT_ACCEPTANCE", "").strip()
    ACCEPTANCE_PATHS: list[str] = json.loads(_ORACLE_RAW) if _ORACLE_RAW else []
except json.JSONDecodeError:
    ACCEPTANCE_PATHS = []

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
            "path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}},
            "required": ["path", "old_str", "new_str"]}}},
    {"type": "function", "function": {
        "name": "replace_lines", "description": "Replace lines start..end (1-indexed, inclusive) in an existing file with new_str. Use this when str_replace's old_str will not match (e.g. whitespace differences): give the exact line numbers from `nl -ba <file> | sed -n '<start>,<end>p'` and the new content; no byte-exact old_str is required. If the line numbers came from an earlier view_file, supply expect_first/expect_last so stale numbers are caught before the edit is applied.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "start": {"type": "integer"},
            "end": {"type": "integer"}, "new_str": {"type": "string"},
            "expect_first": {"type": "string", "description": "The exact text the model expects to find at line `start`. Optional but strongly recommended on files over 1000 lines because line numbers from an earlier view_file go stale."},
            "expect_last": {"type": "string", "description": "The exact text the model expects to find at line `end`. Optional but strongly recommended on files over 1000 lines because line numbers from an earlier view_file go stale."},
            "confirm_removals": {"type": "boolean", "description": "Set true to confirm you intend to delete the lines the previous attempt reported. Only needed after a pending edit."}},
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
    assembled = {"role": role, "content": "".join(content_parts)}
    if tool_calls:
        assembled["tool_calls"] = tool_calls
    # Calibrate the chars/token ratio from ollama's own real count for this
    # request. Ported from local_agent.py - keep both copies in sync.
    if prompt_eval_count:
        _last_prompt_eval_count = prompt_eval_count
        # The tools schema is counted in prompt_eval_count, so it must be
        # counted here too - see local_agent.py's copy for the measured bias
        # this avoids. Keep both copies in sync.
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
    payload = {"model": MODEL, "messages": messages, "tools": TOOLS, "stream": True,
               "options": {"num_ctx": NUM_CTX, "temperature": TEMPERATURE}}
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
    the inner text produces a correctly-escaped JSON string in its place.

    Kept in sync with scripts/local_agent.py's copy (this module is a verbatim
    port of that agent for oracle grading)."""
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
    interval_merge task, the benchmark harness this oracle agent is
    dispatched through): a distinct malformation from the triple-quote case
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
    rejecting on this one class of already-well-structured input.

    Kept in sync with scripts/local_agent.py's copy (this module is a
    verbatim port of that agent for oracle grading)."""
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


def auto_commit(reason: str) -> None:
    git("add", "-A")
    git("commit", "-m", reason)


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


def _full_suite_result() -> tuple[bool, str]:
    """Run the FULL worktree suite (unscoped), for the L1 CI-fail-rework
    done-bar. Mirrors oracle_result's runner (detect_test_command + the heavy
    lock) but does NOT scope to ACCEPTANCE_PATHS - it runs the detected test
    command verbatim, exactly what the merge gate's _ci_status_stub runs
    (tests/benchmark/harness.py:623) so the done-bar matches the gate that
    tripped the rework. Returns (passed, tail[-500:]); the tail feeds back into
    the agent loop on a failure so the model sees the broken assertion. No
    detectable test command -> (True, '') (nothing to fail, mirrors the gate's
    no-test-cmd -> pass).

    Mode 40: once tests pass, also run detect_lint_command (if the repo has
    one) and fold a lint failure into the same (False, tail) result. Kept in
    sync with scripts/local_agent.py:_full_suite_result.
    """
    test_dir, test_cmd = p.detect_test_command(CWD)
    if not test_cmd:
        return True, ""
    argv = test_cmd
    needs_heavy = bool(argv) and p._is_heavy(argv)
    if needs_heavy:
        with p._heavy_lock():
            r = subprocess.run(argv, cwd=test_dir, capture_output=True, text=True)  # noqa: PLW1510 (check=False would break test fakes with fixed signatures; see test_local_agent_oracle.py)
    else:
        r = subprocess.run(argv, cwd=test_dir, capture_output=True, text=True)  # noqa: PLW1510 (check=False would break test fakes with fixed signatures; see test_local_agent_oracle.py)
    if r.returncode != 0:
        return False, (r.stdout + r.stderr)[-500:]
    lint = p.detect_lint_command(CWD)
    if lint is not None:
        lint_dir, lint_cmd = lint
        lr = subprocess.run(lint_cmd, check=False, cwd=lint_dir, capture_output=True, text=True)
        if lr.returncode != 0:
            return False, (lr.stdout + lr.stderr)[-500:]
    return True, ""


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
    """If the oracle passes, auto-commit and return True to terminate the loop.

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
        full_ok, full_tail = _full_suite_result()
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
                    "The acceptance oracle passes but the FULL test suite still "
                    f"fails. The merge-gate CI will reject this on the same "
                    f"failure:\n{full_tail}\n\nThe bug could be in the "
                    "implementation you just changed, or in a test file - do "
                    "not assume either side is correct. Re-read the failing "
                    "test and the code it exercises, identify which one is "
                    "actually wrong, and make ONE targeted fix there. Do NOT "
                    "call done until `pytest` passes in full.")})
            print(f"[step {step}] ORACLE GREEN but full suite still fails - "
                  f"rework done-bar not met; continuing.", flush=True)
            return False
    _reset_suite_rejections()
    if worktree_dirty():
        auto_commit("feat: implement task (acceptance oracle green)")
    print(f"[step {step}] ORACLE GREEN — acceptance tests pass; committed & done.", flush=True)
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



# Consecutive syntax-rejection count per path, so a model that resubmits the
# same broken content can be escalated instead of silently retrying forever
# (observed: gpt-oss retried near-identical broken content 4x until the
# repetition guard parked the run with no file ever landing). Resets on any
# successful write to that path (see run_tool). NOT a repair mechanism — the
# write itself is always either exactly what the model submitted, or
# refused; this only tracks how many times in a row that refusal happened.
# Ported verbatim from local_agent.py; keep both copies in sync.
_SYNTAX_REJECT_COUNTS: dict[str, int] = {}

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


def _python_syntax_error(path_str: str, content: str) -> str | None:
    """Return an ERROR string if `path_str` is a .py file and `content` is not
    valid Python, else None. Defense-in-depth against malformed model output
    (e.g. a stray unified-diff leading '+', or an unmatched triple-quote)
    landing on disk — not an attempt to explain why a model emits it.

    The message quotes the offending line (by e.lineno) plus up to 2 lines of
    context either side, verbatim from the SUBMITTED content — never a
    repaired/transformed version — so the model can see exactly what it wrote
    and where. Ported verbatim from local_agent.py; keep both copies in
    sync."""
    if not path_str.endswith(".py"):
        return None
    try:
        # compile(), not ast.parse(): ast.parse() only validates grammar,
        # not that `return`/`yield` sit inside a function or `break`/
        # `continue` inside a loop - those are SyntaxErrors too, but only
        # surface at compile() time. See local_agent.py's copy for the live
        # incident that found this gap.
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

    Ported verbatim from local_agent.py; keep both copies in sync. Shallow
    and conservative on purpose: every Name node anywhere inside the
    function body (including nested functions/comprehensions) is attributed
    to the outer function rather than modeling real scope nesting, and two
    functions sharing the same name collide in the returned dict - a false
    negative (the check silently doesn't fire), never a false positive."""
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
    """Return names of module-level `def`/`class` statements deleted by this
    edit while a reference to that name survives. Ported verbatim from
    local_agent.py; keep both copies in sync - see that file's docstring
    for the live-incident rationale (MODE-29-REVIEW-STORY-LOCK-GUARD,
    2026-07-22: a replace_lines edit deleted only the `def
    _review_story_impl(...):` line, leaving its body as syntactically
    valid trailing dead code in the caller and the caller's call site
    intact - compile() accepts it, only a runtime NameError surfaces)."""
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


def _dropped_top_level_defs(old_content: str, new_content: str) -> list[str]:
    """Return names of module-level `def`/`class` statements present in
    `old_content` but absent from `new_content` - unlike
    `_newly_undefined_module_defs`, this does NOT require the name to still
    be referenced somewhere in `new_content`. It exists for create_file's
    whole-file-overwrite path specifically: `_newly_undefined_module_defs`
    only catches a removed def that's still CALLED (a NameError), but a
    create_file rewrite that silently drops a function nobody in THIS file
    calls - because it's a public API consumed elsewhere (imported by
    another module, exercised only by tests) - produces no such call site to
    catch. Observed live 2026-08-07 (w3a-effective-config-provenance,
    story f7fd39c4): a create_file rewrite of pipeline/config_provenance.py
    dropped 4 of 5 functions (read_plist_env, read_mcp_server_env,
    _scheduler_plist_path, _claude_json_path) that story 1 had already
    landed - none of them called from within config_provenance.py itself,
    so _newly_undefined_module_defs's reference check never fires. Returns
    [] (never raises) on a non-.py path or when either side fails to parse."""
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
    return sorted(old_top_defs - new_top_defs)


def _newly_undefined_module_vars(old_content: str, new_content: str) -> list[str]:
    """Return names of module-level VARIABLE assignments (top-level
    ast.Assign / ast.AnnAssign targets) deleted by this edit while a reference
    to that name survives anywhere in `new_content`. Ported verbatim from
    local_agent.py; keep both copies in sync - see that file's docstring for the
    live-incident rationale (MODE-43, 2026-07-30, TRANSPORT-ALIAS-READERS: a
    replace_lines edit replaced the module-level
    `TIMEOUT = float(os.environ.get(...))` line with a duplicate of the
    preceding `NUM_CTX = ...` line, deleting the `TIMEOUT` assignment while
    every later `TIMEOUT` read survived - compile() accepts it, only a runtime
    NameError surfaces). `_newly_undefined_names` only tracks function-LOCAL
    bindings and `_newly_undefined_module_defs` only covers `def`/`class`
    names - neither sees a deleted module-level variable assignment."""
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
    """Return "name (in function)" entries for a name whose only assignment
    within a function existed in `old_content`, was read later in that SAME
    function, and has been deleted by this edit while the read survives in
    `new_content`. Ported verbatim from local_agent.py; keep both copies in
    sync - see that file's docstring for the live-incident rationale
    (gpt-oss:20b / qwen3-coder:30b both deleted a load-bearing assignment
    while a use of it survived, landing a NameError/UnboundLocalError that
    compile()-based syntax checking cannot catch). Returns [] (never raises)
    on a non-.py path or when either side fails to parse."""
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
    for the normal rejection path.

    Kept in sync with scripts/local_agent.py's copy (this module is a
    verbatim port of that agent for oracle grading)."""
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
    Ported verbatim from local_agent.py; keep both copies in sync."""
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
    """Mode 40: ported verbatim from local_agent.py; keep both copies in
    sync. After a successful write, run a fast, single-file-scoped lint
    check and return a short findings suffix, or "" when there's nothing
    to report."""
    if not path_str.endswith(".py"):
        return ""
    lint = p.detect_lint_command(CWD)
    if lint is None:
        return ""
    lint_dir, cmd = lint
    if not cmd or "ruff" not in cmd[0]:
        return ""
    try:
        res = subprocess.run(  # noqa: PLW1510 (check=False would break test fakes with fixed signatures; see test_local_agent_oracle.py)
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
    if fn in ("create_file", "str_replace") and is_oracle_path(args.get("path", "")):
        return (f"ERROR: {args['path']} is the read-only acceptance suite and "
                f"must NOT be modified. Change the implementation file instead.")
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
        # (prefix + suffix), so the range's own former content is not
        # counted as a duplicate. Does not block - the write above has already
        # landed.
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
        # DESTRUCTIVE_GIT_PATTERNS). Ported from local_agent.py (Mode 3a).
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
        # etc.). See _heavy_lock docstring for the rationale.
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
        result = (pr.stdout + pr.stderr)[:3000] or "(no output)"
        if ACCEPTANCE_PATHS:
            result += _restore_tampered_oracle_files()
        return result
    if fn == "search":
        return (
            "unknown tool search — there is no search tool. Use bash with "
            "grep or rg to find code (e.g. `grep -n \"def foo\" -R .`), then "
            "view_file with line_start/line_end on the line number it reports."
        )
    return f"unknown tool {fn}"


def safe_run_tool(fn, args) -> str:
    """Run a tool, turning any exception into a recoverable error message.

    A model that omits a required argument (e.g. str_replace without old_str,
    observed with weaker local models) would otherwise raise an uncaught
    KeyError and crash the whole unattended agent. Feeding the error back as a
    tool result lets the model correct itself, bounded by the loop guard / step
    cap, instead of taking the run down.
    """
    try:
        return run_tool(fn, args)
    except Exception as e:  # noqa: BLE001 (a tool call's own failure is reported back to the model as tool output, not raised - the agent loop must never crash on an unpredictable tool error)
        return f"ERROR running {fn}: {type(e).__name__}: {e}"


# Substrings that identify a context-overflow rejection in an error body.
# Ollama/llama.cpp signal overflow with a bare 500 (no useful body), but the
# OpenAI-compatible servers reject it as a 400 with one of these: LM Studio
# emits the prose form ("Trying to keep the first N tokens when context the
# overflows. However, the model is loaded with context length of only ..."),
# while the standard OpenAI error shape uses the `context_length_exceeded`
# code. Both were being swallowed by the `status_code < 500` fast path, which
# treats every 4xx as an unretryable bad request - so on LM Studio the one
# remedy that would actually have worked (trimming) was skipped precisely
# because the server reported the problem accurately.
_OVERFLOW_BODY_MARKERS = (
    "context_length_exceeded",
    "context length",
    "context window",
    "context the overflows",
    "maximum context",
    "too many tokens",
    "prompt is too long",
)


def _is_context_overflow_error(exc) -> bool:
    """True when `exc` looks like a context-window overflow rather than a
    malformed request. Any 5xx qualifies (Ollama/llama.cpp return 500 for
    overflow and give no body to inspect); a 4xx qualifies only when its body
    carries an explicit overflow marker, so a genuinely bad request still
    fails fast instead of burning the escalation budget."""
    response = getattr(exc, "response", None)
    if response is None:
        return False
    status = response.status_code
    if status >= 500:
        return True
    if status not in (400, 413, 422):
        return False
    try:
        body = (response.text or "").lower()
    except Exception:  # noqa: BLE001 (a streamed/unread body is simply unavailable here)
        return False
    return any(marker in body for marker in _OVERFLOW_BODY_MARKERS)


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

    Ported from local_agent.py - keep both copies in sync. The oracle runs the
    production path for acceptance-bearing dispatches (oracle_mode = bool(acceptance)
    in backend.py), so a transient 5xx killing a ~95%-complete converging run here
    (2026-07-30 LAUNCHD-PLIST-PORTABILITY) is the live failure this recovers from.
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


def _answer_orphaned_calls(tcs: list, from_idx: int, messages: list) -> None:
    """Append a stub tool-role response for every tool_calls entry from
    `from_idx` onward. A loop guard's `break` out of the per-call loop ends
    that turn early — any of THIS turn's tool_calls entries at or after
    `from_idx` that haven't been answered yet would otherwise be sent back
    to the model on the next turn with no matching tool-role reply. Mode 33
    (2026-07-22) traced exactly this shape (an orphaned tool_calls entry, in
    that case from the per-target repetition guard before it was fixed to
    always answer the triggering call) directly to gpt-oss:20b's
    Harmony-format output degrading into leaked special tokens a few turns
    later. Only ever observed with multiple tool_calls in one turn — not
    reproduced live, since this harness's models issue one call per turn in
    practice — but the fix is cheap and closes the class outright."""
    for _ in tcs[from_idx:]:
        messages.append({"role": "tool", "content": (
            "(skipped — a loop guard interrupted this turn before this "
            "call could run)")})


def main() -> int:
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
                        full_ok, full_tail = _full_suite_result()
                        if not full_ok:
                            suite_rejections += 1
                            if suite_rejections >= REWORK_SUITE_REJECT_CAP:
                                if worktree_dirty():
                                    auto_commit("wip: rework suite-reject cap")
                                print(f"[step {step}] rework suite-reject cap "
                                      f"({REWORK_SUITE_REJECT_CAP}) reached; agent "
                                      f"cannot green the full suite — parking", flush=True)
                                return 2
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


if __name__ == "__main__":
    sys.exit(main())
