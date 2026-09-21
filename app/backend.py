"""LLM backend abstraction — the seam between orchestration and execution.
pipeline_mcp_server.py owns the *why* (state machine, gating, decisions). This
module owns the *how* (spawning a model to do the work).

This is a 4-file layout: backend.py holds the Backend Protocol and the
get_backend() factory; backend_types.py holds the shared AgentHandle;
backend_claude.py holds ClaudeCliDriver (the `claude` CLI subprocess driver);
backend_ollama.py holds OllamaDriver (the local-model harness). backend.py
re-exports the pieces other modules and tests depend on so callers can keep
importing from `app.backend` unchanged.
"""
from __future__ import annotations

import functools
import logging
import os
import subprocess  # noqa: F401 (re-exported: backend.subprocess, used by tests/unit/test_backend.py)
import time  # noqa: F401 (re-exported: backend.time, used by tests/unit/test_backend.py)
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx  # noqa: F401 (re-exported: backend.httpx, used by tests/unit/test_backend.py)

from app import (
    inference_providers,  # noqa: F401 (re-exported: backend.inference_providers, used by tests/unit/test_backend.py)
)
from app.backend_claude import (
    ClaudeCliDriver,
    ProviderIdentityMismatch,  # noqa: F401 (re-exported: backend.ProviderIdentityMismatch)
)
from app.backend_ollama import (
    _LOCAL_MODEL_TUNING,  # noqa: F401 (re-exported: backend._LOCAL_MODEL_TUNING, used by tests/unit/test_backend.py)
    _OLLAMA_MODEL_WEIGHTS_CACHE,  # noqa: F401 (re-exported: backend._OLLAMA_MODEL_WEIGHTS_CACHE, used by tests/unit/test_backend.py)
    OllamaDriver,
    _ollama_loaded_models,  # noqa: F401 (re-exported: backend._ollama_loaded_models, used by pipeline/server.py)
    _ollama_model_weights_mb,  # noqa: F401 (re-exported: backend._ollama_model_weights_mb, used by tests/unit/test_backend.py)
    _ollama_serving_parallelism,  # noqa: F401 (re-exported: backend._ollama_serving_parallelism, used by pipeline/server.py)
    _resolve_local_model,  # noqa: F401 (re-exported: backend._resolve_local_model, used by pipeline/server.py and tests/unit/test_backend.py)
    _review_msg_chars,  # noqa: F401 (re-exported: backend._review_msg_chars, used by tests/unit/test_backend.py)
    _run_readonly_tool,  # noqa: F401 (re-exported: backend._run_readonly_tool, used by tests/unit/test_backend.py)
    _total_memory_mb,  # noqa: F401 (re-exported: backend._total_memory_mb, used by tests/unit/test_backend.py)
    _trim_review_transcript,  # noqa: F401 (re-exported: backend._trim_review_transcript, used by tests/unit/test_backend.py)
)
from app.backend_types import AgentHandle
from app.inference_providers import (
    RateLimitedError,  # noqa: F401 (re-exported: backend.RateLimitedError)
)
from pipeline.config_provenance import IGNORED_ENV_VARS

# Warn if operator mistakenly set transport-only env vars
_logger = logging.getLogger("pipeline")
for _var, _real in IGNORED_ENV_VARS:
    if _var in os.environ:
        _logger.warning(
            f"{_var} is set in the environment but is a transport-only value that backend.py overwrites on every dispatch - it has no effect as an input; "
            f"set {_real} instead."
        )
class Backend(Protocol):
    def complete(
        self, prompt: str, *, system: str | None, model: str,
        allowed_tools: str | None = None, cwd: str | None = None,
        max_tokens: int | None = None,
        cell_dir: str | None = None,
        role: str = "complete",
    ) -> str:
        """Run a blocking, single invocation and return its captured output.

        `max_tokens` is accepted for signature parity across drivers but not
        acted on by any of them: the `claude` CLI dropped `--max-tokens` (see
        ClaudeCliDriver.complete), and Ollama-backed drivers cap the response
        via the model's own context window instead.

        `cell_dir`, when set, is the per-cell directory the caller is running
        in (the benchmark's <run>/<task>__<model>__t<trial>/ path). Drivers
        with structured per-call usage data (Ollama's response envelope,
        Claude's --output-format json) append a JSONL record to
        `<cell_dir>/review_token_costs.jsonl` so the data survives the
        worktree cleanup that wipes the live review.log. Drivers that can't
        extract structured usage silently skip the sidecar.
        """
        ...

    def dispatch(
        self, prompt: str, *, system: str | None, model: str,
        allowed_tools: str | None, cwd: Path, log_path: Path, append: bool,
        acceptance: list[str] | None = None,
        resume_transcript_path: Path | None = None,
        resume_append_content: str | None = None,
        rework_full_suite: bool = False,
        review_feedback_rework: bool = False,
    ) -> AgentHandle:
        """Spawn a non-blocking agentic run, streaming output to log_path."""
        ...

    def record_token_usage(
        self, usage: dict, *, cell_dir: str | None = None,
        role: str = "review", step: int | None = None,
        verdict: str | None = None,
    ) -> None:
        """Append a per-call token-usage record to the cell's sidecar.

        Default no-op so drivers without structured usage data don't have
        to implement anything. Drivers that do have it (Ollama, Claude)
        override to JSONL-append to <cell_dir>/review_token_costs.jsonl
        and swallow OSError so a failed write never breaks the review
        loop. The schema is documented on the concrete drivers.
        """
        ...

    def usage_probe_text(self) -> str:
        """Return the raw usage/cost report text for the resource gate."""
        ...

    def resource_status(self, model_tag: str | None = None) -> dict:
        """Whether this backend is resource-available to take work right now.

        Returns {"ok": bool, "reason": str}. The orchestrator's per-role gate
        (advance_pipeline) consults the backend serving each role, so a limit
        on one backend (e.g. Claude's weekly usage) no longer freezes work on
        another (e.g. local dispatch). A future vLLM/cloud driver defines its
        own check here without touching the orchestrator.
        """
        ...


# Registry of available drivers by config name. Register new drivers here -
# orchestration code never changes.
#
# "local" is a permanent back-compat alias (env-resolved via
# PIPELINE_LOCAL_PROVIDER, default ollama) - manifests persist
# "backend": "local" for already-dispatched stories, so it must never be
# removed. "ollama"/"lmstudio"/"mlx" (RELIABILITY_PLAN.md T16) let a role
# name the actual local transport directly instead of the generic "local",
# which incorrectly implies "runs on this machine" when Ollama/LM Studio can
# equally proxy :cloud-tagged models - and lets different roles pin
# different local providers, since PIPELINE_LOCAL_PROVIDER is process-wide.
_DRIVERS: dict[str, type | Callable[[], OllamaDriver]] = {
    "claude": ClaudeCliDriver,
    "local": OllamaDriver,
    "ollama": functools.partial(OllamaDriver, provider_name="ollama"),
    "lmstudio": functools.partial(OllamaDriver, provider_name="lmstudio"),
    "mlx": functools.partial(OllamaDriver, provider_name="mlx"),
    "litellm": functools.partial(OllamaDriver, provider_name="litellm"),
}


def get_backend(role: str | None = None, *, name: str | None = None) -> Backend:
    """Resolve the active backend.

    role=None returns ClaudeCliDriver unconditionally - used only by the
    Claude-specific usage probe (its `/cost` gate is Claude-only).

    For everything else, pass the role being executed: "dispatch", "review",
    or "overlord". Each is independently routable via PIPELINE_BACKEND_<ROLE>
    (e.g. PIPELINE_BACKEND_OVERLORD=local). Defaults to "claude".

    name= is a per-call override (e.g. a per-story backend choice stored in
    the manifest). When given it skips the env lookup entirely, except for the
    special value "auto" which is not a real driver and must be resolved by the
    caller before calling get_backend.

    "local" (OllamaDriver) implements complete() — both overlord-style
    single-shot prompts and review-style read-only tool loops (runs tests +
    reads, then submit_review) — and dispatch() (a native-tool-calling write
    agent loop, scripts/local_agent.py, run as a subprocess). So all three
    roles (overlord, review, dispatch) can be routed local via
    PIPELINE_BACKEND_<ROLE>=local.
    """
    if role is None:
        return ClaudeCliDriver()
    resolved = (name or "").strip().lower() if name else None
    if not resolved:
        env_var = f"PIPELINE_BACKEND_{role.upper()}"
        resolved = os.environ.get(env_var, "claude").strip().lower()
    if resolved == "auto":
        raise ValueError(
            "get_backend received name='auto', which is not a concrete driver. "
            "The caller must resolve 'auto' to 'local' or 'claude' via "
            "_route_dispatch_backend() before calling get_backend()."
        )
    driver_cls = _DRIVERS.get(resolved)
    if driver_cls is None:
        env_var = f"PIPELINE_BACKEND_{role.upper()}" if role else "(no role)"
        raise NotImplementedError(
            f"{env_var}={resolved!r} has no registered driver. Only "
            f"{sorted(_DRIVERS)} are implemented today."
        )
    return driver_cls()
