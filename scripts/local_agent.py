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
LOCAL_AGENT_NUM_CTX, LOCAL_AGENT_TIMEOUT, LOCAL_AGENT_MAX_STEPS,
LOCAL_AGENT_TEMPERATURE, LOCAL_AGENT_PROVIDER (default "ollama"; "lmstudio"/
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

# Persistence helpers
import json, os, tempfile
from pathlib import Path

def _validate_message_list(msgs):
    if not isinstance(msgs, list) or not msgs:
        return False
    for m in msgs:
        if not isinstance(m, dict) or 'role' not in m or 'content' not in m:
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
    def append(self, item):
        super().append(item)
        if self.transcript_path:
            _persist_messages(self, self.transcript_path)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pipeline_mcp_server as p  # noqa: E402  (reuses _checkpoint_impl + PLAN_DIR config)
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
MAX_STEPS = int(os.environ.get("LOCAL_AGENT_MAX_STEPS", "40"))
TEMPERATURE = float(os.environ.get("LOCAL_AGENT_TEMPERATURE", "0.3"))
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
READ_SILENCE_SECONDS = float(os.environ.get("LOCAL_AGENT_READ_SILENCE_SECONDS", "180"))
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
# Mutating tools: any that produce new code in the worktree. Anything else
# (view_file, bash, checkpoint) is read-only — including checkpoint, which
# commits existing WIP but doesn't add new code; checkpointing without prior
# edits is itself a sign of "spinning."
MUTATING_TOOLS = frozenset({"create_file", "str_replace"})
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
    "Use create_file for NEW files and str_replace to edit EXISTING files; use "
    "bash only to run commands like tests and git (never to create/edit files). "
    "Use view_file to read a file's real contents before editing it, and read "
    "the file:line in any error before changing code. Commit your work with git "
    "before finishing. Call done only after your changes are committed and any "
    "tests pass."
)

TOOLS = [
    {"type": "function", "function": {
        "name": "create_file", "description": "Create a NEW file. Fails if the file already exists and is non-empty (use str_replace to edit existing files).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "str_replace", "description": "Replace the single unique occurrence of old_str with new_str in an existing file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}},
            "required": ["path", "old_str", "new_str"]}}},
    {"type": "function", "function": {
        "name": "view_file", "description": "Show a file's contents with line numbers.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
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
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls = None
    role = "assistant"
    with httpx.stream(
        "POST", f"{ENDPOINT}/api/chat", json=payload,
        timeout=httpx.Timeout(connect=10.0, read=READ_SILENCE_SECONDS,
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
        try:
            obj = json.loads(c)
        except Exception:
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
        additions = [p for p in ("agent.log", "__pycache__/", "*.pyc") if p not in existing]
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



# Consecutive syntax-rejection count per path, so a model that resubmits the
# same broken content can be escalated instead of silently retrying forever
# (observed: gpt-oss retried near-identical broken content 4x until the
# repetition guard parked the run with no file ever landing). Resets on any
# successful write to that path (see run_tool). NOT a repair mechanism — the
# write itself is always either exactly what the model submitted, or
# refused; this only tracks how many times in a row that refusal happened.
_SYNTAX_REJECT_COUNTS: dict[str, int] = {}


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
        ast.parse(content)
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


def _record_syntax_rejection(path_str: str, err: str) -> str:
    """Bump the consecutive-rejection counter for `path_str` and, from the
    second consecutive rejection onward, append a nudge to regenerate the
    file from scratch instead of resubmitting the same broken content."""
    count = _SYNTAX_REJECT_COUNTS.get(path_str, 0) + 1
    _SYNTAX_REJECT_COUNTS[path_str] = count
    if count >= 2:
        err += (
            "\nDo NOT resubmit the same content. Regenerate the ENTIRE file "
            "from scratch, with no diff markers and no surrounding prose."
        )
    return err


def run_tool(fn, args) -> str:
    if fn == "create_file":
        path = CWD / args["path"]
        if path.exists() and path.read_text().strip():
            return f"ERROR: {args['path']} already exists and is non-empty. Use str_replace to edit it."
        content = args.get("content", "")
        err = _python_syntax_error(args["path"], content)
        if err:
            return _record_syntax_rejection(args["path"], err)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        _SYNTAX_REJECT_COUNTS.pop(args["path"], None)
        return f"created {args['path']}"
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
        if err:
            return _record_syntax_rejection(args["path"], err)
        path.write_text(new_text)
        _SYNTAX_REJECT_COUNTS.pop(args["path"], None)
        return f"edited {args['path']}"
    if fn == "view_file":
        path = CWD / args["path"]
        if not path.exists():
            return f"ERROR: {args['path']} does not exist."
        lines = path.read_text().splitlines(keepends=True)
        return "".join(f"{i + 1:4d}| {ln}" for i, ln in enumerate(lines))[:3000]
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
                f"`git reset HEAD~1` (default --mixed, keeps the working tree)."
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
        run_kwargs = dict(shell=True, cwd=CWD, capture_output=True, text=True,
                          timeout=BASH_TIMEOUT)
        if is_heavy:
            with p._heavy_lock():
                pr = subprocess.run(cmd, **run_kwargs)
        else:
            pr = subprocess.run(cmd, **run_kwargs)
        return (pr.stdout + pr.stderr)[:3000] or "(no output)"
    if fn == "checkpoint":
        try:
            res = p._checkpoint_impl(args["plan_name"], args["story_key"], args["step"],
                                     args.get("summary", ""), args.get("next_hint", ""))
            return f"checkpoint recorded: {json.dumps(res)[:200]}"
        except Exception as e:
            return f"ERROR checkpointing: {e}"
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
    system_content = HARNESS_RULES + ("\n\n" + system if system else "")
    # Initialize messages list with optional persistence support
transcript_path = os.environ.get("LOCAL_AGENT_TRANSCRIPT_PATH")
resume_transcript_path = os.environ.get("LOCAL_AGENT_RESUME_TRANSCRIPT_PATH")
messages = PersistingList(transcript_path=transcript_path)
if resume := _load_resume_transcript():
    messages.extend(resume)
else:
    system_content = HARNESS_RULES + ("\n\n" + system if system else "")
    messages.extend([{"role": "system", "content": system_content},
                     {"role": "user", "content": task}])
# If resuming, optionally append new user turn
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
    start_time = time.monotonic()

    for step in range(MAX_STEPS):
        if time.monotonic() - start_time > TIMEOUT:
            print(f"[step {step}] wall-clock timeout ({TIMEOUT}s) reached; parking", flush=True)
            if worktree_dirty():
                auto_wip_commit("wall-clock timeout")
            return 2
        try:
            m = chat(messages)
        except Exception as e:
            print(f"[step {step}] LLM call failed: {e}", flush=True)
            if worktree_dirty():
                auto_wip_commit("llm error")
            return 1
        messages.append(m)
        tcs = m.get("tool_calls") or recover_tool_calls(m.get("content", ""))
        if not tcs:
            print(f"[step {step}] no tool call: {(m.get('content') or '')[:100]!r}", flush=True)
            messages.append({"role": "user", "content": "Call a tool now (do not write prose)."})
            continue

        for tc in tcs:
            fn = tc["function"]["name"]
            args = tc["function"]["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}

            if fn == "done":
                if worktree_dirty():
                    done_rejections += 1
                    if done_rejections >= 2:
                        auto_wip_commit("commit enforcement")
                        print(f"[step {step}] DONE with auto-WIP-commit (agent left tree dirty): "
                              f"{args.get('summary', '')}", flush=True)
                        return 0
                    print(f"[step {step}] done rejected — worktree dirty, asking agent to commit", flush=True)
                    messages.append({"role": "user", "content": (
                        "You have uncommitted changes. Commit your work with git "
                        "(git add -A && git commit -m ...) before calling done.")})
                    break
                print(f"[step {step}] DONE: {args.get('summary', '')}", flush=True)
                return 0

            sig = (fn, args.get("path") or args.get("command") or args.get("old_str", ""))
            # str_replace calls are excluded from the per-target repetition
            # guard: each one produces a *different* file state (the
            # `old_str` next time will differ, or `run_tool` will reject
            # it as "not found"), so a sequence of edits to the same file
            # is a legitimate fix-build cycle, not a repetition. The
            # read-heavy guard (MUTATING_TOOLS) still catches a model stuck
            # in a bad edit loop — str_replace calls reset that window.
            #
            # Any mutating call (str_replace/create_file) also resets every
            # OTHER signature's accumulated count: `seen` was a lifetime
            # cumulative counter, so re-viewing a file 2x, editing it, then
            # viewing it again to check the edit landed hit the >=3 threshold
            # from stale pre-edit reads, even though real progress happened
            # in between. That false-triggered on gpt-oss's ratelimiter_
            # inspect RLI-2 runs on 2026-07-04: view, view, edit, view (3rd
            # cumulative view -> nudge), view (park) -- despite the edit and
            # a test run in between. A real edit invalidates prior reads'
            # staleness, so the count for everything else should start over.
            if fn in MUTATING_TOOLS:
                seen.clear()
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
                    messages.append({"role": "user", "content": (
                        "You have repeated the same action 3 times with no progress. STOP. "
                        "Use view_file to read the actual current file contents and re-read the "
                        "error's file:line, then fix the ROOT cause in the correct file.")})
                    break
                print("   [parking: repeated action after nudge]", flush=True)
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
            messages.append({"role": "tool", "content": safe_run_tool(fn, args)})

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
