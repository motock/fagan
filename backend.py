"""LLM backend abstraction — the seam between orchestration and execution.

pipeline_mcp_server.py owns the *why* (state machine, gating, decisions). This
module owns the *how* (spawning a model to do the work). ClaudeCliDriver wraps
today's `claude` CLI subprocess calls with identical mechanics to what it
replaced; a future local-model driver implements the same Backend protocol so
the orchestrator never depends on which backend runs a given role.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx


@dataclass
class AgentHandle:
    """A non-blocking agentic run (dispatch/review-style), identified by pid."""
    pid: int


class Backend(Protocol):
    def complete(
        self, prompt: str, *, system: str | None, model: str,
        allowed_tools: str | None = None, cwd: str | None = None,
    ) -> str:
        """Run a blocking, single invocation and return its captured output."""
        ...

    def dispatch(
        self, prompt: str, *, system: str | None, model: str,
        allowed_tools: str | None, cwd: Path, log_path: Path, append: bool,
    ) -> AgentHandle:
        """Spawn a non-blocking agentic run, streaming output to log_path."""
        ...

    def usage_probe_text(self) -> str:
        """Return the raw usage/cost report text for the resource gate."""
        ...

    def resource_status(self) -> dict:
        """Whether this backend is resource-available to take work right now.

        Returns {"ok": bool, "reason": str}. The orchestrator's per-role gate
        (advance_pipeline) consults the backend serving each role, so a limit
        on one backend (e.g. Claude's weekly usage) no longer freezes work on
        another (e.g. local dispatch). A future vLLM/cloud driver defines its
        own check here without touching the orchestrator.
        """
        ...


class ClaudeCliDriver:
    """Backend driver wrapping the `claude` CLI."""

    def complete(
        self, prompt: str, *, system: str | None = None, model: str,
        allowed_tools: str | None = None, cwd: str | None = None,
    ) -> str:
        cmd = ["claude", "-p", prompt, "--model", model]
        if system:
            cmd += ["--append-system-prompt", system]
        if allowed_tools:
            cmd += ["--allowedTools", allowed_tools]
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        return proc.stdout

    def dispatch(
        self, prompt: str, *, system: str | None = None, model: str,
        allowed_tools: str | None = None, cwd: Path, log_path: Path, append: bool,
    ) -> AgentHandle:
        cmd = ["claude", "-p", prompt, "--model", model]
        if system:
            cmd += ["--append-system-prompt", system]
        if allowed_tools:
            cmd += ["--allowedTools", allowed_tools]
        log_file = open(log_path, "a" if append else "w")
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=log_file, stderr=log_file)
        log_file.close()
        return AgentHandle(pid=proc.pid)

    def usage_probe_text(self) -> str:
        proc = subprocess.run(
            ["claude", "-p", "/cost", "--output-format", "json"],
            capture_output=True, text=True, check=True,
        )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Usage probe returned invalid JSON: {e}") from e
        return payload.get("result", "")

    def resource_status(self) -> dict:
        """Claude's gate is the poller-fed, hysteresis-stabilized usage state
        (see pipeline_mcp_server.check_usage / _usage_gate), not a live /cost
        probe — reading the cached `paused` flag here is cheap and reflects the
        same decision the poller already made. Imported locally because the
        orchestrator imports this module (a top-level import would cycle); by
        call time pipeline_mcp_server is fully loaded. Failing open (ok) on
        missing/garbled state matches check_usage's own fail-open behavior.
        """
        import pipeline_mcp_server as _p  # local: avoids an import cycle
        paused = bool(_p._read_usage_state().get("paused", False))
        return {"ok": not paused, "reason": "Claude usage gate tripped" if paused else ""}


# Tier names (opus/sonnet/haiku) come from Claude persona frontmatter and
# story overrides — see pipeline_mcp_server.py's _persona_default_model. Map
# them to concrete local model names; any tier without its own override (or
# any unrecognized tier) falls back to PIPELINE_LOCAL_MODEL_DEFAULT.
_LOCAL_TIER_ENV = {
    "opus": "PIPELINE_LOCAL_MODEL_OPUS",
    "sonnet": "PIPELINE_LOCAL_MODEL_SONNET",
    "haiku": "PIPELINE_LOCAL_MODEL_HAIKU",
}
_LOCAL_DEFAULT_MODEL = "devstral:24b"


def _resolve_local_model(tier: str) -> str:
    default = os.environ.get("PIPELINE_LOCAL_MODEL_DEFAULT", _LOCAL_DEFAULT_MODEL)
    env_var = _LOCAL_TIER_ENV.get(tier.lower())
    return os.environ.get(env_var, default) if env_var else default


class OllamaDriver:
    """Backend driver for Ollama's native /api/chat endpoint.

    Uses the native API rather than Ollama's OpenAI-compatible /v1 surface
    because only the native API accepts `options.num_ctx`. That control turned
    out to matter in practice: Ollama's default context (131072) made a 24B
    model's KV cache blow past 24GB of unified memory, forcing a CPU/GPU
    split that made a single completion time out at 600s. Pinning num_ctx to
    a size that actually fits keeps the model 100% on GPU. This is an Ollama-
    specific knob - a future vLLM/cloud driver would set its context size
    differently (server-side, e.g. --max-model-len) and would not share this
    class.

    complete() only sends prompt+system text and returns the model's reply -
    there is no tool execution here. That's fine for self-contained prompts
    (e.g. the overlord's decision prompts, which embed all needed context as
    text).

    dispatch() runs scripts/local_agent.py as a subprocess in `cwd`, giving
    the model a real native-tool-calling loop (create_file/str_replace/
    view_file/bash/checkpoint/done) to edit files, run tests, use git, and
    checkpoint. It deliberately does NOT use OpenHands: a full investigation
    (Local_LLM_Port_Plan.md §Status/§8) found OpenHands' CLI is the wrong
    harness for a 24B local model - its ~7 bloated tool schemas collapse
    devstral's native `[TOOL_CALLS]` adherence and its strict native-only
    parsing has no recovery when a tool call arrives as text. The minimal
    loop (clean tool set + tolerant parser + non-destructive editor + loop
    guard + commit enforcement) was validated end-to-end on real TDD stories.
    See scripts/local_agent.py for the loop and its guards.

    dispatch() refuses any allowed_tools that excludes Edit/Write: the local
    agent is a writing/coding harness, so a read-only role (e.g. review) is
    not yet supported here and would otherwise silently edit files. (A
    read-only tool set for review is a future addition, noted in §8.)
    """

    def __init__(self) -> None:
        self.endpoint = os.environ.get(
            "PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434",
        ).rstrip("/")
        self.timeout = float(os.environ.get("PIPELINE_LOCAL_TIMEOUT_SECONDS", "600"))
        # 16384 fits 100% on GPU on a 24GB M4 and gives the agentic loop real
        # headroom; complete()'s self-contained prompts are smaller so the same
        # value is safe there too.
        self.num_ctx = int(os.environ.get("PIPELINE_LOCAL_NUM_CTX", "16384"))
        self.dispatch_timeout = float(
            os.environ.get("PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS", "900"))
        self.max_steps = int(os.environ.get("PIPELINE_LOCAL_MAX_STEPS", "40"))
        self.temperature = float(os.environ.get("PIPELINE_LOCAL_TEMPERATURE", "0.3"))

    def complete(
        self, prompt: str, *, system: str | None = None, model: str,
        allowed_tools: str | None = None, cwd: str | None = None,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resolved_model = _resolve_local_model(model)
        try:
            resp = httpx.post(
                f"{self.endpoint}/api/chat",
                json={
                    "model": resolved_model, "messages": messages, "stream": False,
                    "options": {"num_ctx": self.num_ctx},
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"Local backend at {self.endpoint} (model={resolved_model}) "
                f"is unreachable or errored: {e}"
            ) from e
        payload = resp.json()
        return payload["message"]["content"]

    # The local agent loop lives in a standalone script so it can run as a
    # pollable subprocess; run it with this project's venv python (which has
    # httpx and can import pipeline_mcp_server for in-process checkpointing).
    _AGENT_SCRIPT = Path(__file__).resolve().parent / "scripts" / "local_agent.py"
    _VENV_PYTHON = Path(__file__).resolve().parent / ".venv" / "bin" / "python3"

    def dispatch(
        self, prompt: str, *, system: str | None = None, model: str,
        allowed_tools: str | None = None, cwd: Path, log_path: Path, append: bool,
    ) -> AgentHandle:
        if allowed_tools and not ({"Edit", "Write"} & set(allowed_tools.split(","))):
            raise NotImplementedError(
                f"OllamaDriver.dispatch is a writing/coding harness and cannot "
                f"honor read-only allowed_tools={allowed_tools!r} (it would let "
                f"the agent edit files anyway). Keep this role on claude until a "
                f"read-only local tool set is implemented and verified."
            )
        resolved_model = _resolve_local_model(model)
        env = {
            **os.environ,
            "LOCAL_AGENT_MODEL": resolved_model,
            "LOCAL_AGENT_SYSTEM": system or "",
            "LOCAL_AGENT_TASK": prompt,
            "LOCAL_AGENT_ENDPOINT": self.endpoint,
            "LOCAL_AGENT_NUM_CTX": str(self.num_ctx),
            "LOCAL_AGENT_TIMEOUT": str(self.dispatch_timeout),
            "LOCAL_AGENT_MAX_STEPS": str(self.max_steps),
            "LOCAL_AGENT_TEMPERATURE": str(self.temperature),
        }
        argv = [str(self._VENV_PYTHON), str(self._AGENT_SCRIPT)]
        log_file = open(log_path, "a" if append else "w")
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log_file, stderr=log_file)
        log_file.close()
        return AgentHandle(pid=proc.pid)

    def usage_probe_text(self) -> str:
        raise NotImplementedError(
            "OllamaDriver has no usage/cost concept - its resource gate is "
            "resource_status() (Ollama reachability), not a /cost probe."
        )

    def resource_status(self) -> dict:
        """Local backend has no usage/cost limit to respect, so the only gate
        is whether the Ollama endpoint is up. (The concurrency ceiling is
        enforced separately by advance_pipeline via MAX_CONCURRENT_AGENTS.)
        This is what unlocks overnight autonomy decoupled from Claude's weekly
        limit: as long as Ollama is reachable, local dispatch keeps running.
        """
        try:
            resp = httpx.get(f"{self.endpoint}/api/tags", timeout=10)
            resp.raise_for_status()
            return {"ok": True, "reason": ""}
        except httpx.HTTPError as e:
            return {"ok": False, "reason": f"Ollama endpoint {self.endpoint} unreachable: {e}"}


# Registry of available drivers by config name. Register new drivers here -
# orchestration code never changes.
_DRIVERS: dict[str, type] = {
    "claude": ClaudeCliDriver,
    "local": OllamaDriver,
}


def get_backend(role: str | None = None) -> Backend:
    """Resolve the active backend.

    role=None returns ClaudeCliDriver unconditionally - used today only by
    the Claude-specific usage probe (its `/cost` gate is Claude-only until
    Step 5 replaces it with a per-backend resource check).

    For everything else, pass the role being executed: "dispatch", "review",
    or "overlord". Each is independently routable via
    PIPELINE_BACKEND_<ROLE> (e.g. PIPELINE_BACKEND_OVERLORD=local), so any
    role can move off Claude without touching the others. Defaults to
    "claude".

    "local" (OllamaDriver) implements complete() (overlord-style
    self-contained prompts) and dispatch() (a native-tool-calling agent loop,
    scripts/local_agent.py, run as a subprocess - real file edits, tests, and
    git). It refuses to dispatch with a read-only allowed_tools, so routing
    "review" to it raises NotImplementedError until a read-only local tool set
    is implemented and verified.
    """
    if role is None:
        return ClaudeCliDriver()
    env_var = f"PIPELINE_BACKEND_{role.upper()}"
    name = os.environ.get(env_var, "claude").strip().lower()
    driver_cls = _DRIVERS.get(name)
    if driver_cls is None:
        raise NotImplementedError(
            f"{env_var}={name!r} has no registered driver. Only "
            f"{sorted(_DRIVERS)} are implemented today; additional drivers "
            f"(e.g. a local-model driver) land in a later step."
        )
    return driver_cls()
