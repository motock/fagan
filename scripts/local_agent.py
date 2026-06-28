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
LOCAL_AGENT_TEMPERATURE. The process CWD is the worktree. Progress is printed
to stdout (which dispatch redirects to agent.log — a non-empty log is itself
the signal that a real attempt was made).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections import deque
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pipeline_mcp_server as p  # noqa: E402  (reuses _checkpoint_impl + PLAN_DIR config)

CWD = Path.cwd()
MODEL = os.environ["LOCAL_AGENT_MODEL"]
ENDPOINT = os.environ.get("LOCAL_AGENT_ENDPOINT", "http://localhost:11434").rstrip("/")
NUM_CTX = int(os.environ.get("LOCAL_AGENT_NUM_CTX", "16384"))
TIMEOUT = float(os.environ.get("LOCAL_AGENT_TIMEOUT", "900"))
MAX_STEPS = int(os.environ.get("LOCAL_AGENT_MAX_STEPS", "40"))
TEMPERATURE = float(os.environ.get("LOCAL_AGENT_TEMPERATURE", "0.3"))
# Per-bash-invocation timeout. The model can call `cargo fetch` and wedge on
# a network index update forever; without this the agent loop blocks on a
# single subprocess.run until cargo eventually times out (if at all).
# 10 min is generous — most cargo invocations in a worktree finish in <60s
# on a warm target/ — but bounded so a wedged cargo doesn't pin the agent.
BASH_TIMEOUT = float(os.environ.get("LOCAL_AGENT_BASH_TIMEOUT_SECONDS", "600"))

# Read-heavy-pattern guard. Tracks the last N tool names the model called and
# treats "N consecutive non-mutating tools" as paralysis-by-analysis. The
# per-target repetition guard (see main()) misses this because each call hits
# a different file/command — every signature is unique, but the model never
# actually writes anything. Window size 6 catches "5 reads without a write"
# while still allowing the natural 2-3-step warm-up of `bash pwd` / read PLAN.
READ_HEAVY_WINDOW = int(os.environ.get("LOCAL_AGENT_READ_HEAVY_WINDOW", "6"))
# Mutating tools: any that produce new code in the worktree. Anything else
# (view_file, bash, checkpoint) is read-only — including checkpoint, which
# commits existing WIP but doesn't add new code; checkpointing without prior
# edits is itself a sign of "spinning."
MUTATING_TOOLS = frozenset({"create_file", "str_replace"})

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


def chat(messages):
    r = httpx.post(f"{ENDPOINT}/api/chat", json={
        "model": MODEL, "messages": messages, "tools": TOOLS, "stream": False,
        "options": {"num_ctx": NUM_CTX, "temperature": TEMPERATURE}}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["message"]


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


def run_tool(fn, args) -> str:
    if fn == "create_file":
        path = CWD / args["path"]
        if path.exists() and path.read_text().strip():
            return f"ERROR: {args['path']} already exists and is non-empty. Use str_replace to edit it."
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args.get("content", ""))
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
        path.write_text(text.replace(args["old_str"], args["new_str"]))
        return f"edited {args['path']}"
    if fn == "view_file":
        path = CWD / args["path"]
        if not path.exists():
            return f"ERROR: {args['path']} does not exist."
        lines = path.read_text().splitlines(keepends=True)
        return "".join(f"{i + 1:4d}| {ln}" for i, ln in enumerate(lines))[:3000]
    if fn == "bash":
        cmd = args.get("command", "")
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
        f"steps={MAX_STEPS} timeout={TIMEOUT}s",
        flush=True,
    )
    system = os.environ.get("LOCAL_AGENT_SYSTEM", "").strip()
    task = os.environ.get("LOCAL_AGENT_TASK", "")
    system_content = HARNESS_RULES + ("\n\n" + system if system else "")
    messages = [{"role": "system", "content": system_content},
                {"role": "user", "content": task}]

    exclude_runtime_artifacts()

    seen: dict = {}
    nudged_repeat = False
    nudged_read_heavy = False
    recent_tools: deque[str] = deque(maxlen=READ_HEAVY_WINDOW)
    done_rejections = 0

    for step in range(MAX_STEPS):
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
            recent_tools.append(fn)
            if (len(recent_tools) == READ_HEAVY_WINDOW
                    and all(t not in MUTATING_TOOLS for t in recent_tools)):
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
                else:
                    print("   [parking: read-heavy after nudge]", flush=True)
                    if worktree_dirty():
                        auto_wip_commit("read-heavy parking")
                    return 3

    print("[ended without done — step cap reached]", flush=True)
    if worktree_dirty():
        auto_wip_commit("step cap reached")
    return 2


if __name__ == "__main__":
    sys.exit(main())
