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

    dispatch() drives the OpenHands CLI (`uv tool install openhands`) headless
    in `cwd`, giving the model a real tool loop (Bash/Edit/Read/etc.) to edit
    files, run tests, and use git - verified end-to-end against devstral:24b.
    Two fixes were needed beyond just pointing OpenHands at Ollama, both baked
    into a persisted settings file by scripts/setup_openhands_local.py (see
    that script's docstring for the full story):
    - devstral errors on OpenHands' default reasoning_effort ("high"); needs
      reasoning_effort="none".
    - The same num_ctx problem as complete() recurs here independently
      (OpenHands' LiteLLM path doesn't set it on its own); needs
      litellm_extra_body={"options": {"num_ctx": ...}}.
    OpenHands' CLI has no way to restrict tool access (no read-only mode), so
    dispatch() refuses any allowed_tools that excludes Edit/Write - that would
    silently fail to honor a read-only role (e.g. review) and let the agent
    edit files anyway.

    dispatch() also registers a minimal checkpoint-only MCP server with
    OpenHands (see _write_mcp_config / scripts/checkpoint_mcp_server.py) so
    the agent can call the `checkpoint` tool the dispatch prompt instructs it
    to use - without this, a devstral agent given the checkpoint instruction
    tries to call a tool that doesn't exist, gets confused, and gives up
    without doing any work. It's a *separate minimal* server, not this
    pipeline's full one: registering the full server gave the dispatched
    agent direct MCP access to orchestration tools (approve_merge,
    advance_pipeline, ...) it has no business calling, and the extra ~18
    irrelevant tools also degraded tool selection (an agent hallucinated a
    "create_file" tool instead of using its own). All found via real
    end-to-end testing, not theoretical.
    """

    def __init__(self) -> None:
        self.endpoint = os.environ.get(
            "PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434",
        ).rstrip("/")
        self.timeout = float(os.environ.get("PIPELINE_LOCAL_TIMEOUT_SECONDS", "600"))
        self.num_ctx = int(os.environ.get("PIPELINE_LOCAL_NUM_CTX", "8192"))

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

    def dispatch(
        self, prompt: str, *, system: str | None = None, model: str,
        allowed_tools: str | None = None, cwd: Path, log_path: Path, append: bool,
    ) -> AgentHandle:
        if allowed_tools and not ({"Edit", "Write"} & set(allowed_tools.split(","))):
            raise NotImplementedError(
                f"OpenHands has no read-only mode, so OllamaDriver cannot "
                f"honor allowed_tools={allowed_tools!r} (it would let the "
                f"agent edit files anyway). Keep this role on claude until "
                f"tool restriction is verified."
            )
        settings_path = self._settings_dir() / "agent_settings.json"
        if not settings_path.exists():
            raise RuntimeError(
                f"No OpenHands settings at {settings_path}. Run the one-time "
                f"local-dispatch setup first: "
                f"$(uv tool dir)/openhands/bin/python3 "
                f"scripts/setup_openhands_local.py"
            )
        resolved_model = _resolve_local_model(model)
        task = f"{system}\n\n{prompt}" if system else prompt
        self._write_mcp_config(self._settings_dir())
        env = {
            **os.environ,
            "LLM_MODEL": f"ollama/{resolved_model}",
            "LLM_BASE_URL": self.endpoint,
            "LLM_API_KEY": os.environ.get("PIPELINE_LOCAL_API_KEY", "dummy"),
            "OPENHANDS_PERSISTENCE_DIR": str(self._settings_dir()),
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "NO_COLOR": "1",
        }
        argv = [
            "openhands", "--headless", "--override-with-envs",
            "--always-approve", "--exit-without-confirmation",
            "-t", task,
        ]
        log_file = open(log_path, "a" if append else "w")
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log_file, stderr=log_file)
        log_file.close()
        return AgentHandle(pid=proc.pid)

    @staticmethod
    def _settings_dir() -> Path:
        return Path(os.environ.get(
            "PIPELINE_OPENHANDS_PERSISTENCE_DIR", "~/.claude/openhands-pipeline",
        )).expanduser()

    # _checkpoint_impl only touches PLAN_DIR (manifest/journal lookup) and the
    # worktree path it's given explicitly - no other pipeline config needed.
    _MCP_PASSTHROUGH_VARS = ("PLAN_DIR",)

    @classmethod
    def _write_mcp_config(cls, settings_dir: Path) -> None:
        """Register the minimal checkpoint-only MCP server (see
        scripts/checkpoint_mcp_server.py) with OpenHands, so dispatched local
        agents can checkpoint without getting access to this pipeline's full
        orchestration toolset (dispatch_story, approve_merge,
        advance_pipeline, ...) - a privilege-escalation risk for a less
        reliable local model, and in practice also degraded tool selection
        (~20 irrelevant tools led an agent to hallucinate a "create_file"
        tool instead of using its own file-edit tool). Found via real
        end-to-end testing, not theoretical.

        Rewritten on every dispatch (cheap, idempotent) rather than once at
        setup time, so it always reflects this process's current PLAN_DIR
        rather than going stale if that's overridden per-project.
        """
        pipeline_dir = Path(__file__).resolve().parent
        config = {
            "mcpServers": {
                "pipeline-checkpoint": {
                    "transport": "stdio",
                    "command": str(pipeline_dir / ".venv" / "bin" / "python3"),
                    "args": [str(pipeline_dir / "scripts" / "checkpoint_mcp_server.py")],
                    "env": {
                        k: os.environ[k] for k in cls._MCP_PASSTHROUGH_VARS
                        if k in os.environ
                    },
                }
            }
        }
        settings_dir.mkdir(parents=True, exist_ok=True)
        (settings_dir / "mcp.json").write_text(json.dumps(config))

    def usage_probe_text(self) -> str:
        raise NotImplementedError(
            "OllamaDriver has no usage/cost concept - the resource gate "
            "for local backends is a separate, not-yet-implemented check."
        )


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
    self-contained prompts) and dispatch() (via OpenHands - real tool
    execution, requires one-time setup, see scripts/setup_openhands_local.py).
    It refuses to dispatch with a read-only allowed_tools, so routing "review"
    to it raises NotImplementedError until tool restriction is verified.
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
