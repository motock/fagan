"""Standalone helpers OllamaDriver uses: local-model tag/tuning resolution,
tool-call recovery from raw text, and the read-only review loop's transcript
trimming / tool execution. Split out of app/backend_ollama.py purely to keep
that file under the project's line-count target; OllamaDriver itself (the
harness mechanics: dispatch loop, review loop, checkpoint plumbing) stays in
backend_ollama.py and re-exports these names, since its methods reference
them as bare names.
"""
import json
import os
import re
import subprocess
from pathlib import Path

from pipeline.config_provenance import ENV_VAR_CATALOG

_LOCAL_TIER_ENV = {
    "opus": "PIPELINE_LOCAL_MODEL_OPUS",
    "sonnet": "PIPELINE_LOCAL_MODEL_SONNET",
    "haiku": "PIPELINE_LOCAL_MODEL_HAIKU",
}
# Sourced from pipeline/config_provenance.py's ENV_VAR_CATALOG rather than
# hardcoded here a second time - that catalog is the existing single source
# of truth for env var defaults (see IGNORED_ENV_VARS in that module for the
# same "exactly one definition - a second copy would drift" rationale).
_LOCAL_DEFAULT_MODEL = next(
    spec.default for spec in ENV_VAR_CATALOG if spec.name == "PIPELINE_LOCAL_MODEL_DEFAULT"
)


def _resolve_local_model(tier: str, provider: str = "ollama") -> str:
    # A value containing ':' (Ollama's tag separator, e.g. "devstral:24b")
    # or '/' (LiteLLM's vendor/model form, e.g. "openai/gpt-5-mini") is
    # already a concrete model tag, not a tier name - return as-is
    # rather than looking it up in _LOCAL_TIER_ENV, where it would never
    # match and silently fall back to PIPELINE_LOCAL_MODEL_DEFAULT instead
    # of the caller's explicit choice (see _run_reviewer's
    # PIPELINE_LOCAL_REVIEW_MODEL override). Tier names are always bare
    # lowercase words, so neither separator can appear in a legitimate tier.
    if ":" in tier or "/" in tier:
        return tier
    default = os.environ.get("PIPELINE_LOCAL_MODEL_DEFAULT", _LOCAL_DEFAULT_MODEL)
    env_var = _LOCAL_TIER_ENV.get(tier.lower())
    if not env_var:
        return default
    # Provider-scoped override (e.g. PIPELINE_LOCAL_MODEL_MLX_SONNET) is
    # checked before the provider-agnostic PIPELINE_LOCAL_MODEL_SONNET, so
    # two roles on different local providers never silently share one
    # tier->model mapping meant for a single wire format/model namespace.
    # provider= defaults to "ollama" - the historical implicit assumption
    # every existing caller made before this parameter existed.
    scoped_env_var = env_var.replace(
        "PIPELINE_LOCAL_MODEL_", f"PIPELINE_LOCAL_MODEL_{provider.upper()}_"
    )
    scoped = os.environ.get(scoped_env_var)
    if scoped:
        return scoped
    return os.environ.get(env_var, default)


# Per-model tuned defaults, keyed by the RESOLVED concrete model tag (e.g.
# "gpt-oss:20b"), not the tier ("sonnet"/"opus"/"haiku"). Populated as a
# model's temperature/num_ctx is empirically settled (see tests/benchmark/
# A/B experiments). An explicit PIPELINE_LOCAL_TEMPERATURE/PIPELINE_LOCAL_NUM_CTX
# env var always overrides an entry here (operator override wins); a model
# tag with no entry, or an entry missing one of the two keys, falls back to
# OllamaDriver's constructor-captured default for that specific value.
_LOCAL_MODEL_TUNING: dict[str, dict[str, float | int | str]] = {
    # 2026-07-03 A/B experiment (tests/benchmark/_runs/full_20260703_postfix
    # vs temp_tune_20260703, 15 cells each): temp=1.0 -> 6/15 success, 3
    # cells where the implementation file never landed at all; temp=0.3 ->
    # 9/15 success, only 1 zero-code-landed cell, same 11/15 ground-truth
    # pass rate. Lower temperature measurably improves self-correction
    # without costing correctness. No num_ctx entry: an earlier 32768 value
    # here was never itself A/B-tested (the experiment above only varied
    # temperature) and PIPELINE_LOCAL_NUM_CTX always overrides this table
    # anyway (see _tuned_num_ctx) - keeping an unvalidated number here just
    # invited it to be mistaken for a settled finding. See
    # TOKEN_CONTEXT_OPTIMIZATION_PLAN.md and the launchd plist's
    # PIPELINE_LOCAL_NUM_CTX for where num_ctx is actually decided.
    "gpt-oss:20b": {"temperature": 0.3},
    # 2026-08-14 manual probe (not a full benchmark matrix - 5 tasks,
    # single trial each, think="medium" throughout, no A/B against
    # low/high for this specific model): cron_field, retry_backoff,
    # ratelimiter_inspect, lru_cache, and token_bucket all passed their
    # full acceptance oracle, including the backward-jump/high-water-mark
    # mutation-timing bug that both gemma4:12b-mlx (medium AND high) and
    # gpt-oss-20b-high got wrong identically. "medium" was used as a
    # reasonable default, not shown optimal versus low/high for this tag -
    # revisit if a real benchmark run says otherwise.
    "gemma4:26b-a4b-it-qat": {"think": "medium"},
    # 2026-09-18 manual sweep (Apple M4, 24GB unified memory) of gpt-oss-20b-high:latest
    # 1048576 tested and found to have no effect
    # swept num_ctx values 32768, 49152, 65536, 81920, 98304, 114688, 131072
    # observed 100% GPU usage, resident size 12GB→13GB, no swap growth
    # 131072 is Ollama's hard clamp for this model; values above it have no effect
    # PIPELINE_LOCAL_NUM_CTX still overrides this table entry; the table value
    # only takes effect once the launchd plist's PIPELINE_LOCAL_NUM_CTX is
    # removed by the sibling story.  The table entry is inert in production
    # today because the env var is set to 16384.
    "gpt-oss-20b-high:latest": {"num_ctx": 131072},
}

def _tuned_num_ctx(model_tag: str, fallback: int) -> int:
    # A ":cloud"-tagged model (deepseek-v4-flash:cloud, glm-5.2:cloud,
    # minimax-m3:cloud) is a frontier model proxied through the local
    # Ollama-compatible endpoint, not a constrained on-device model. The
    # on-device PIPELINE_LOCAL_NUM_CTX ceiling (e.g. 32K) would needlessly
    # cap and trim its transcript; use a cloud-specific knob so a capable
    # cloud model keeps its full reasoning context. The ":cloud" suffix is
    # the existing convention (planner.py:973, backend.py:135). See the
    # matching max_steps/park relaxation in OllamaDriver.dispatch.
    if model_tag.endswith(":cloud"):
        return int(os.environ.get("PIPELINE_CLOUD_NUM_CTX", "131072"))
    env = os.environ.get("PIPELINE_LOCAL_NUM_CTX")
    if env is not None:
        return int(env)
    tuned = _LOCAL_MODEL_TUNING.get(model_tag, {}).get("num_ctx")
    return int(tuned) if tuned is not None else fallback


def _tuned_temperature(model_tag: str, fallback: float) -> float:
    env = os.environ.get("PIPELINE_LOCAL_TEMPERATURE")
    if env is not None:
        return float(env)
    tuned = _LOCAL_MODEL_TUNING.get(model_tag, {}).get("temperature")
    return float(tuned) if tuned is not None else fallback


_THINK_LEVELS = ("low", "medium", "high", "max")


def _tuned_think(model_tag: str) -> bool | str | None:
    """Resolve the "think" value for model_tag: env override (only the exact
    tokens "true"/"false"/"low"/"medium"/"high"/"max" opt in - anything else,
    e.g. a typo, falls through rather than coercing to a bogus value) > the
    RESOLVED model tag's _LOCAL_MODEL_TUNING entry > None. Unlike temperature/
    num_ctx there is no fallback default - None means the request body omits
    "think" entirely, leaving the model/provider's own default behavior
    unchanged, since not every deployment has an opinion on reasoning depth.
    """
    env = os.environ.get("PIPELINE_LOCAL_THINK", "").strip().lower()
    if env in ("true", "false"):
        return env == "true"
    if env in _THINK_LEVELS:
        return env
    return _LOCAL_MODEL_TUNING.get(model_tag, {}).get("think")


def _recover_tool_calls(content: str | None) -> list | None:
    """Recover tool calls from message text when the native tool_calls field
    is empty — the local model intermittently emits well-formed calls as text
    ([TOOL_CALLS]/bare arrays/```json fences). Mirrors scripts/local_agent.py's
    parser (kept in sync; both are small)."""
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
        except ValueError:
            continue
        items = obj if isinstance(obj, list) else [obj]
        out = [{"function": {"name": it["name"], "arguments": it.get("arguments", it.get("parameters", {}))}}
               for it in items if isinstance(it, dict) and "name" in it]
        if out:
            return out
    return None


def _infer_review_tool_call(content: str | None) -> list | None:
    """Last-resort recovery for the review loop: the local model sometimes
    emits a bare arguments object (no tool name) like {"command": "git diff"}.
    Infer the intended review tool from its keys (unambiguous for this small
    read-only tool set). Returns None if nothing parseable is found."""
    if not content:
        return None
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict) or "name" in obj:
        return None  # named calls are handled by _recover_tool_calls
    if "verdict" in obj:
        return [{"function": {"name": "submit_review", "arguments": obj}}]
    if "command" in obj:
        return [{"function": {"name": "bash", "arguments": obj}}]
    if "path" in obj:
        return [{"function": {"name": "view_file", "arguments": obj}}]
    return None


def _review_msg_chars(m: dict) -> int:
    n = len(str(m.get("content") or ""))
    tool_calls = m.get("tool_calls")
    if tool_calls:
        n += len(json.dumps(tool_calls))
    return n


def _trim_review_transcript(messages: list, max_chars: int) -> list:
    """Bound a review transcript to max_chars, dropping the oldest middle
    content when it would otherwise overflow the model's context window.

    Same block-preserving algorithm as scripts/local_agent.py's
    _trim_resumed_transcript (own copy - the review loop's message shape and
    call site differ enough that sharing the dispatch-loop original isn't a
    clean fit; keep both in sync if the algorithm changes): messages[:2] (the
    system+task head) is always kept, an assistant tool_calls message is
    never split from its own tool-role response, and content is dropped in
    whole blocks starting from the oldest until the rest fits the budget.
    """
    total = sum(_review_msg_chars(m) for m in messages)
    if total <= max_chars:
        return messages
    head_len = min(2, len(messages))
    head = messages[:head_len]
    head_chars = sum(_review_msg_chars(m) for m in head)
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
        block_chars = sum(_review_msg_chars(m) for m in block)
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
            f"[{dropped} earlier turn(s) were dropped from this review "
            "transcript to fit the model's context window. Continue "
            "reviewing using only the history below.]"
        ),
    }
    return head + [note] + [m for block in kept for m in block]


def _append_review_log(cwd: str, text: str) -> None:
    """Best-effort transcript write for the review loop: the verdict is the
    deliverable, the log is diagnostic only, so any OSError (unwritable
    path, read-only tree, review.log pre-empted by a directory) must be
    swallowed rather than breaking the review."""
    try:
        with open(Path(cwd) / "review.log", "a") as f:
            f.write(text)
    except OSError:
        pass


def _run_readonly_tool(fn: str, args: dict, cwd: Path) -> str:
    """Execute a review (read-only) tool: bash (run commands) or view_file."""
    if fn == "view_file":
        path = cwd / args.get("path", "")
        if not path.exists():
            return f"ERROR: {args.get('path')} does not exist."
        lines = path.read_text().splitlines(keepends=True)
        return "".join(f"{i + 1:4d}| {ln}" for i, ln in enumerate(lines))[:3000]
    if fn == "bash":
        # Acquire the heavy-build lock when the reviewer triggers a build
        # or test command — review runs cargo/npm/etc. to verify the agent's
        # claim, and we don't want it stomping on a concurrent in-flight
        # dispatch's build. Same lock the agent harness uses, see
        # pipeline_mcp_server._heavy_lock docstring.
        import shlex

        from app import (
            pipeline_mcp_server as _p,  # local: avoid import cycle at module load
        )
        cmd = args.get("command", "")
        try:
            argv0 = shlex.split(cmd)[0] if cmd.strip() else ""
        except ValueError:
            argv0 = ""
        if argv0 and _p._is_heavy([argv0]):
            with _p._heavy_lock():
                pr = subprocess.run(cmd, check=False, shell=True, cwd=cwd,
                                    capture_output=True, text=True)
        else:
            pr = subprocess.run(cmd, check=False, shell=True, cwd=cwd,
                                capture_output=True, text=True)
        return (pr.stdout + pr.stderr)[:3000] or "(no output)"
    return f"unknown tool {fn}"


__all__ = [
    "_LOCAL_DEFAULT_MODEL",
    "_LOCAL_MODEL_TUNING",
    "_LOCAL_TIER_ENV",
    "_THINK_LEVELS",
    "_append_review_log",
    "_infer_review_tool_call",
    "_recover_tool_calls",
    "_resolve_local_model",
    "_review_msg_chars",
    "_run_readonly_tool",
    "_trim_review_transcript",
    "_tuned_num_ctx",
    "_tuned_temperature",
    "_tuned_think",
]
