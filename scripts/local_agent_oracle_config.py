"""Environment-derived configuration constants and tool schemas for the
acceptance-oracle variant of the local dispatch agent loop
(scripts/local_agent_oracle.py).

Pure data — mirrors scripts/local_agent_config.py's split of the base
harness: no functions here read or mutate module-level mutable state, so
this carries none of the monkeypatch-retargeting risk a function
extraction would. local_agent_oracle.py re-imports every name from here
and evicts this module from sys.modules before each import so a fresh
exec of local_agent_oracle.py (as several tests do after mutating
os.environ) still recomputes these from current env, matching pre-split
behavior.
"""
from __future__ import annotations

import json
import os

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
# Mirror local_agent.py's THINK: "true"/"false" or a graded-reasoning level
# ("low"/"medium"/"high"/"max") — omitted from the payload when unset/
# unrecognized. Ported from local_agent.py; keep both copies in sync.
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
