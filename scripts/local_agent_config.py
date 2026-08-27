"""Environment-derived configuration constants and tool schemas for the
local dispatch agent loop (scripts/local_agent.py).

Pure data — no functions here read or mutate module-level mutable state,
so this split carries none of the monkeypatch-retargeting risk that a
function extraction would (see local_agent.py's own docstring/comments
for why: local_agent.py re-imports every name from here, so
``monkeypatch.setattr(local_agent, "NAME", ...)`` still lands exactly as
before — the consuming functions all stay in local_agent.py and resolve
these as bare globals from *that* module's namespace, unchanged.
"""
from __future__ import annotations

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
