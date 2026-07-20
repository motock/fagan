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
    except Exception as e:
        print(f"[local_agent] RESUME FAILED: {e}", flush=True)
        return None


def _persist_messages(messages, path):
    if not path:
        return
    dirpath = os.path.dirname(path) or "."
    tmp_path = Path(dirpath) / f".{Path(path).name}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
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
import pipeline_mcp_server as p  # noqa: E402  (reuses _checkpoint_impl, _heavy_lock, _is_heavy)
import inference_providers  # noqa: E402  (non-Ollama chat() branch, see PROVIDER below)

CWD = Path.cwd()
MODEL = os.environ["LOCAL_AGENT_MODEL"]
ENDPOINT = os.environ.get("LOCAL_AGENT_ENDPOINT", "http://localhost:11434").rstrip("/")
# Set by backend.OllamaDriver.dispatch from self.provider.name. "ollama" (the
# default) keeps chat() on the streaming NDJSON /api/chat path below,
# unchanged; any other registered provider (lmstudio, mlx) routes through
# _provider_chat_turn's blocking inference_providers call instead.
PROVIDER = os.environ.get("LOCAL_AGENT_PROVIDER", "ollama").strip().lower()
NUM_CTX = int(os.environ.get("LOCAL_AGENT_NUM_CTX", "16384"))
TIMEOUT = float(os.environ.get("LOCAL_AGENT_TIMEOUT", "900"))
MAX_STEPS = int(os.environ.get("LOCAL_AGENT_MAX_STEPS", "30"))
# Cap consecutive assistant turns that emit no tool call. A weak model stuck
# on a self-inflicted phantom failure — its own test asserts non-standard
# behavior the correct implementation can never satisfy — will narrate its
# "next step" as prose indefinitely; the generic "call a tool" nudge cannot
# break this because no real action resolves a self-contradictory test. Park
# after this many consecutive no-tool turns rather than burning the whole run.
NO_TOOL_CAP = int(os.environ.get("LOCAL_AGENT_NO_TOOL_CAP", "5"))
TEMPERATURE = float(os.environ.get("LOCAL_AGENT_TEMPERATURE", "0.3"))


def _no_tool_nudge(consecutive: int) -> str:
    """Nudge for an assistant turn that emitted no tool call.

    Early turns get the plain call-to-action (the model may simply have
    forgotten). From the third consecutive narration turn onward, escalate to
    behavioral guidance: a weak model stuck looping on a failing self-test is
    usually chasing a phantom — its own test asserts behavior the correct
    implementation can never satisfy. Tell it to re-check the spec and fix the
    *test*, not the implementation, then call done.
    """
    if consecutive < 3:
        return "Call a tool now (do not write prose)."
    return (
        "You have not called a tool for several turns. If you are stuck on a "
        "failing test that you wrote, that test may assert the wrong behavior — "
        "re-read the task spec. If your implementation already matches the spec, "
        "fix or delete the failing test rather than the implementation, then call "
        "done. Otherwise call a tool now (do not write prose)."
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
# Mutating tools: any that produce new code in the worktree. Anything else
# (view_file, bash, checkpoint) is read-only — including checkpoint, which
# commits existing WIP but doesn't add new code; checkpointing without prior
# edits is itself a sign of "spinning."
MUTATING_TOOLS = frozenset({"create_file", "str_replace"})
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
    "Use create_file for NEW files and str_replace to edit EXISTING files; use "
    "bash only to run commands like tests and git (never to create/edit files). "
    "Use view_file to read a file's real contents before editing it, and read "
    "the file:line in any error before changing code. Commit your work with git "
    "before finishing. Call done only after your changes are committed and any "
    "tests pass."
)

TOOLS = [
    {"type": "function", "function": {
        "name": "create_file", "description": "Create a new file, or overwrite one with its full corrected contents. To overwrite a file that already exists on disk, view_file it first, then create_file with the complete new contents (a whole-file rewrite is preferred over str_replace for the file you are implementing).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "str_replace", "description": "Replace the single unique occurrence of old_str with new_str in an existing file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}},
            "required": ["path", "old_str", "new_str"]}}},
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
    content_parts: list[str] = []
    tool_calls = None
    role = "assistant"
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
                break
    assembled = {"role": role, "content": "".join(content_parts)}
    if tool_calls:
        assembled["tool_calls"] = tool_calls
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
    except Exception:
        pass
    repaired = _repair_triple_quoted_strings(candidate)
    if repaired != candidate:
        try:
            return json.loads(repaired, strict=False)
        except Exception:
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
    return subprocess.run(["git", *args], cwd=CWD, capture_output=True, text=True)


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
            r = subprocess.run(argv, cwd=test_dir, capture_output=True, text=True)
    else:
        r = subprocess.run(argv, cwd=test_dir, capture_output=True, text=True)
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
    """
    test_dir, test_cmd = p.detect_test_command(CWD)
    if not test_cmd:
        return True, ""
    argv = test_cmd
    needs_heavy = bool(argv) and p._is_heavy(argv)
    if needs_heavy:
        with p._heavy_lock():
            r = subprocess.run(argv, cwd=test_dir, capture_output=True, text=True)
    else:
        r = subprocess.run(argv, cwd=test_dir, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr)[-500:]


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
    ok, _ = oracle_result()
    if not ok:
        return False
    if REWORK_FULL_SUITE:
        full_ok, full_tail = _full_suite_result()
        if not full_ok:
            if messages is not None:
                messages.append({"role": "user", "content": (
                    "The acceptance oracle passes but the FULL test suite still "
                    "fails - your own committed test has a wrong assertion. The "
                    f"merge-gate CI will reject this on the same failure:\n{full_tail}"
                    "\n\nFix the failing test (re-read the file:line above, correct "
                    "the expected value or the code so the assertion holds) and do "
                    "NOT call done until `pytest` passes in full.")})
            print(f"[step {step}] ORACLE GREEN but full suite still fails - "
                  f"rework done-bar not met; continuing.", flush=True)
            return False
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


def _record_syntax_rejection(path_str: str, err: str) -> str:
    """Bump the consecutive-rejection counter for `path_str` and, from the
    second consecutive rejection onward, append a nudge to regenerate the
    file from scratch instead of resubmitting the same broken content.
    Ported verbatim from local_agent.py; keep both copies in sync."""
    count = _SYNTAX_REJECT_COUNTS.get(path_str, 0) + 1
    _SYNTAX_REJECT_COUNTS[path_str] = count
    if count >= 2:
        err += (
            "\nDo NOT resubmit the same content. Regenerate the ENTIRE file "
            "from scratch, with no diff markers and no surrounding prose."
        )
    return err


def run_tool(fn, args) -> str:
    if fn in ("create_file", "str_replace") and is_oracle_path(args.get("path", "")):
        return (f"ERROR: {args['path']} is the read-only acceptance suite and "
                f"must NOT be modified. Change the implementation file instead.")
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
        return f"created {args['path']}" + (f" ({note})" if note else "")
    if fn == "str_replace":
        path = CWD / args["path"]
        if not path.exists():
            return f"ERROR: {args['path']} does not exist (use create_file for new files)."
        text = path.read_text()
        n = text.count(args["old_str"])
        if n == 0:
            return f"ERROR: old_str not found in {args['path']}."
        if n > 1:
            return f"ERROR: old_str occurs {n} times in {args['path']}; include more context to make it unique."
        new_text = text.replace(args["old_str"], args["new_str"])
        err = _python_syntax_error(args["path"], new_text)
        note = None
        if err:
            repair = _try_repair_indentation(new_text)
            if repair is None:
                return _record_syntax_rejection(args["path"], err)
            new_text, note = repair
        path.write_text(new_text)
        _SYNTAX_REJECT_COUNTS.pop(args["path"], None)
        return f"edited {args['path']}" + (f" ({note})" if note else "")
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
                f"`git reset HEAD~1` (default --mixed, keeps the working tree)."
            )
        # Acquire the cross-dispatch heavy-build lock for any command whose
        # first token is a known build/test executable (cargo, npm, mvn,
        # etc.). See _heavy_lock docstring for the rationale.
        try:
            argv0 = shlex.split(cmd)[0] if cmd.strip() else ""
        except ValueError:
            argv0 = ""
        is_heavy = bool(argv0) and p._is_heavy([argv0])
        run_kwargs = dict(shell=True, cwd=CWD, capture_output=True, text=True,
                          timeout=BASH_TIMEOUT)
        if is_heavy:
            with p._heavy_lock():
                pr = subprocess.run(cmd, **run_kwargs)
        else:
            pr = subprocess.run(cmd, **run_kwargs)
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
    except Exception as e:
        return f"ERROR running {fn}: {type(e).__name__}: {e}"


def main() -> int:
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
    start_time = time.monotonic()

    for step in range(MAX_STEPS):
        if time.monotonic() - start_time > TIMEOUT:
            print(f"[step {step}] wall-clock timeout ({TIMEOUT}s) reached; parking", flush=True)
            if worktree_dirty():
                auto_commit("WIP (wall-clock timeout)")
            return 2
        try:
            m = chat(messages)
        except Exception as e:
            print(f"[step {step}] LLM call failed: {e}", flush=True)
            if worktree_dirty():
                auto_commit("WIP (llm error)")
            return 1
        messages.append(m)
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
            messages.append({"role": "user", "content": _no_tool_nudge(consecutive_no_tool)})
            continue
        consecutive_no_tool = 0

        for tc in tcs:
            fn = tc["function"]["name"]
            args = tc["function"]["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
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
                            print(f"[step {step}] done rejected — full test suite "
                                  f"still fails (rework done-bar); asking agent to "
                                  f"fix its own test", flush=True)
                            messages.append({"role": "user", "content": (
                                "The acceptance oracle passes but the FULL test suite "
                                "still fails - your own committed test has a wrong "
                                "assertion. The merge-gate CI will reject this on the "
                                f"same failure:\n{full_tail}\n\nRe-read the file:line "
                                "above, correct the expected value or the code so the "
                                "assertion holds, and do NOT call done until `pytest` "
                                "passes in full.")})
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
            if fn != "str_replace":
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
                    if fn != "str_replace":
                        seen[sig] = current

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

    print("[ended without oracle green — step cap reached]", flush=True)
    if worktree_dirty():
        auto_commit("WIP (step cap reached)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
