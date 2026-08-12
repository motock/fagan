"""Verified byte-identical helpers shared by scripts/local_agent.py and
scripts/local_agent_oracle.py.

These two scripts duplicate a large amount of code under identical function
names. The functions and constants below were confirmed (via an AST-body
diff run 2026-08-12) to be byte-identical between the two files, so they
live here once instead of drifting out of sync in two places.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from pathlib import Path

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

CWD = Path.cwd()


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


def _loads_tolerant(candidate, repair_triple_quoted_strings=None):
    """json.loads, tolerating raw control characters inside strings, with a
    triple-quote repair pass as a further fallback. Valid JSON is never
    transformed - both fallbacks only ever ACCEPT more inputs than a strict
    parse would, never reinterpret one that already parses.

    `repair_triple_quoted_strings` is injected by the caller rather than
    imported here: local_agent.py and local_agent_oracle.py each keep their
    own (verified diverged) copy of that function file-local, so this shared
    module never imports a copy that could silently drift from whichever one
    a caller actually needs. Passing None (the default) simply skips that
    fallback stage.

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
    if repair_triple_quoted_strings is not None:
        repaired = repair_triple_quoted_strings(candidate)
        if repaired != candidate:
            try:
                return json.loads(repaired, strict=False)
            except json.JSONDecodeError:
                pass
    return None


def recover_tool_calls(content, repair_triple_quoted_strings=None):
    """Pull a tool call out of message text when the native field is empty."""
    if not content:
        return None
    text = content.strip().replace("[TOOL_CALLS]", "")
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    m = re.search(r"(\[\s*\{.*\}\s*\]|\{.*\})", text, re.DOTALL)
    if m:
        candidates.append(m.group(1))
    for c in candidates:
        obj = _loads_tolerant(c, repair_triple_quoted_strings)
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


# LM Studio's context-overflow error also arrives as a 400 whose BODY text
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
