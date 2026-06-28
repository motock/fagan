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
  - After every code-changing tool, the harness runs pytest against the
    oracle files. First time it passes -> auto-commit -> exit 0. The model
    cannot author or modify the exam it's graded on.

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
from collections import deque
from pathlib import Path

import httpx

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

# Read-heavy-pattern guard (ported from local_agent.py PR #30 — this variant
# missed the original PR because backend routes to it whenever a story carries
# an `acceptance` block). Tracks the last N tool names the model called and
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


def auto_commit(reason: str) -> None:
    git("add", "-A")
    git("commit", "-m", reason)


def oracle_result() -> tuple[bool, str]:
    """Run the harness-owned acceptance suite. Returns (passed, tail).

    Pytest is run once across every oracle path (most stories will have just
    one). On a collection error (e.g. a syntax error in the implementation
    file the model just emitted) pytest returns non-zero AND emits its error
    to stderr — captured in `tail` so the model can act on it.
    """
    if not ACCEPTANCE_PATHS:
        return True, "(no acceptance files configured)"
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *ACCEPTANCE_PATHS, "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=CWD, capture_output=True, text=True,
    )
    return r.returncode == 0, (r.stdout + r.stderr)[-800:]


def is_oracle_path(path: str) -> bool:
    # Normalise: strip leading "./" and treat absolute paths the user wouldn't
    # use here as already disambiguated.
    norm = path.lstrip("./")
    return any(norm == p.lstrip("./") or norm.endswith("/" + p.lstrip("./"))
               for p in ACCEPTANCE_PATHS)


def run_tool(fn, args) -> str:
    if fn in ("create_file", "str_replace") and is_oracle_path(args.get("path", "")):
        return (f"ERROR: {args['path']} is the read-only acceptance suite and "
                f"must NOT be modified. Change the implementation file instead.")
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
    recent_tools: deque[str] = deque(maxlen=READ_HEAVY_WINDOW)

    for step in range(MAX_STEPS):
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
            recent_tools.append(fn)
            if (len(recent_tools) == READ_HEAVY_WINDOW
                    and all(t not in MUTATING_TOOLS for t in recent_tools)):
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
                else:
                    print("   [parking: read-heavy after nudge]", flush=True)
                    if worktree_dirty():
                        auto_commit("WIP (read-heavy parking)")
                    return 3

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
