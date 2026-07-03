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

# The pipeline_mcp_server module lives in this script's parent directory.
# The agent subprocess runs with cwd=<worktree>, NOT the pipeline repo, so
# without this path insert the import below fails with ModuleNotFoundError
# and the harness dies before printing [boot] (which check_story_status
# reads as "process never reached main()" = a genuine failed launch).
# local_agent.py has the same line; the oracle variant didn't until PR #32
# because it didn't depend on pipeline_mcp_server.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pipeline_mcp_server as p  # noqa: E402  (reuses _checkpoint_impl, _heavy_lock, _is_heavy)

CWD = Path.cwd()
MODEL = os.environ["LOCAL_AGENT_MODEL"]
ENDPOINT = os.environ.get("LOCAL_AGENT_ENDPOINT", "http://localhost:11434").rstrip("/")
NUM_CTX = int(os.environ.get("LOCAL_AGENT_NUM_CTX", "16384"))
TIMEOUT = float(os.environ.get("LOCAL_AGENT_TIMEOUT", "900"))
MAX_STEPS = int(os.environ.get("LOCAL_AGENT_MAX_STEPS", "30"))
TEMPERATURE = float(os.environ.get("LOCAL_AGENT_TEMPERATURE", "0.3"))
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
#   CHAT_MAX_ATTEMPTS — turns to try before giving up (main()'s except
#       then commits WIP and returns 1, same as before, but only after all
#       attempts are exhausted).
#   CHAT_RETRY_BACKOFF — sleep before attempt N is BACKOFF * N seconds.
READ_SILENCE_SECONDS = float(os.environ.get("LOCAL_AGENT_READ_SILENCE_SECONDS", "180"))
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


def chat(messages):
    """One LLM turn, with streaming + retry.

    A single transient Ollama stall (queue contention, network blip, Ollama
    5xx) must not kill a 30-minute run. We stream so a slow generation
    doesn't trip the timeout, and retry the transient failures. 4xx is a
    bad request (retrying won't help) so it raises immediately; 5xx and
    transport errors (timeout/connect/read) are retried up to
    CHAT_MAX_ATTEMPTS. If every attempt fails, the last exception propagates
    to main()'s except, which commits WIP and returns 1 — same terminal
    behavior as before, but only after we've genuinely tried.
    """
    payload = {"model": MODEL, "messages": messages, "tools": TOOLS, "stream": True,
               "options": {"num_ctx": NUM_CTX, "temperature": TEMPERATURE}}
    last_exc: Exception | None = None
    for attempt in range(1, CHAT_MAX_ATTEMPTS + 1):
        try:
            return _stream_one_turn(payload)
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise  # 4xx — bad request, retrying is pointless
            last_exc = e
        except httpx.TransportError as e:
            last_exc = e  # timeout / connect / read — transient, retry
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
        argv = test_cmd
    # Heavy-build lock: cargo/npm/gradle etc. share the same serialization
    # contract with check_story_status and the agent's own bash tool.
    needs_heavy = bool(argv) and p._is_heavy(argv)
    if needs_heavy:
        with p._heavy_lock():
            r = subprocess.run(argv, cwd=test_dir, capture_output=True, text=True)
    else:
        r = subprocess.run(argv, cwd=test_dir, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr)[-800:]


def is_oracle_path(path: str) -> bool:
    # Normalise: strip leading "./" and treat absolute paths the user wouldn't
    # use here as already disambiguated.
    norm = path.lstrip("./")
    return any(norm == p.lstrip("./") or norm.endswith("/" + p.lstrip("./"))
               for p in ACCEPTANCE_PATHS)



# Consecutive syntax-rejection count per path, so a model that resubmits the
# same broken content can be escalated instead of silently retrying forever
# (observed: gpt-oss retried near-identical broken content 4x until the
# repetition guard parked the run with no file ever landing). Resets on any
# successful write to that path (see run_tool). NOT a repair mechanism — the
# write itself is always either exactly what the model submitted, or
# refused; this only tracks how many times in a row that refusal happened.
# Ported verbatim from local_agent.py; keep both copies in sync.
_SYNTAX_REJECT_COUNTS: dict[str, int] = {}


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
        return (pr.stdout + pr.stderr)[:3000] or "(no output)"
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


def finish_if_green(step: int) -> bool:
    """If the oracle passes, auto-commit and return True to terminate the loop."""
    ok, _ = oracle_result()
    if ok:
        if worktree_dirty():
            auto_commit("feat: implement task (acceptance oracle green)")
        print(f"[step {step}] ORACLE GREEN — acceptance tests pass; committed & done.", flush=True)
        return True
    return False


def main() -> int:
    system = os.environ.get("LOCAL_AGENT_SYSTEM", "").strip()
    task = os.environ.get("LOCAL_AGENT_TASK", "")
    system_content = HARNESS_RULES + ("\n\n" + system if system else "")
    messages = [{"role": "system", "content": system_content},
                {"role": "user", "content": task}]

    if not ACCEPTANCE_PATHS:
        print("[local_agent_oracle] WARNING: no LOCAL_AGENT_ACCEPTANCE; "
              "this variant should not have been launched. Falling back to "
              "no-oracle behavior — done is honored on clean worktree + model `done`.", flush=True)

    exclude_runtime_artifacts()

    seen: dict = {}
    nudged_repeat = False
    nudged_read_heavy = False
    recent_tools: deque[tuple[str, str]] = deque(maxlen=READ_HEAVY_WINDOW)
    distinct_windows = 0
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
                ok, tail = oracle_result()
                if ok:
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
                if finish_if_green(step):
                    return 0

    print("[ended without oracle green — step cap reached]", flush=True)
    if worktree_dirty():
        auto_commit("WIP (step cap reached)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
