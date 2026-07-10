# Plan: Abstract the local model-inference layer (Ollama default, pluggable)

> Status: **Proposed, not started.** Written 2026-07-09. Scope: introduce a pluggable
> local-inference *provider* seam with Ollama as the working default; ship LM Studio and
> MLX as documented stubs (working default now, real impls in a follow-up — mirrors the
> Jira-stub decision in TICKETING_ABSTRACTION_PLAN.md). Not yet registered in the pipeline.

## Goal
Make the *local inference server* pluggable so the pipeline can drive Ollama (default),
LM Studio, MLX, or any OpenAI-compatible local server — without duplicating the local
driver's harness mechanics. Ollama stays the default and behaves bit-for-bit as today.

## Current state (assessment)

The **role-level** backend seam already exists and is good — leave it alone:
- `Backend` Protocol (`backend.py:47`) — `complete` / `dispatch` / `record_token_usage` /
  `usage_probe_text` / `resource_status`.
- `ClaudeCliDriver` (name `claude`) and `OllamaDriver` (name `local`), a `_DRIVERS` registry
  (`:918`), and `get_backend(role, name=)` (`:924`) with `PIPELINE_BACKEND_<ROLE>` routing,
  per-call `name=` override, and `auto` resolution. This decides *claude vs local* per role.

What is **not** abstracted: the local driver is welded to Ollama's native protocol. The
coupling points, all of which a second local server (LM Studio/MLX) would differ on:
- **`OllamaDriver`** (`backend.py:405`) — `_chat()` posts to Ollama's native `/api/chat`,
  sets `options.num_ctx` / `temperature`, reads Ollama's response envelope
  (`message.content`, `eval_count` / `prompt_eval_count` usage), raises `RateLimitedError`
  on 429, and `resource_status()` probes `/api/tags`.
- **`_ollama_loaded_models()`** (`backend.py:888`) — hits `/api/ps` for the VRAM-swap warn;
  also called from `pipeline_mcp_server.py:2251`.
- **`scripts/local_agent.py`** — the dispatch subprocess talks to `/api/chat` **directly**
  (streaming NDJSON, native `message.tool_calls`) at `LOCAL_AGENT_ENDPOINT` (`:63`). It
  already `import pipeline_mcp_server as p` (`:59`), so a shared provider module is importable
  from both the driver and the subprocess.
- **`tests/benchmark/models.py`** — Ollama endpoint + tags.

### Key architectural insight
LM Studio, MLX (`mlx_lm.server`), vLLM, and llama.cpp-server all speak the **OpenAI-compatible
`/v1/chat/completions`** surface and share *all* of the local driver's mechanics — the
native-tool-calling loop, the dispatch subprocess, the read-only review loop, the cost
sidecar, the loop/commit guards. The **only** thing that differs is the wire protocol to the
inference server. So they must **not** be added as sibling `_DRIVERS` entries (that would
duplicate hundreds of lines). Instead: keep one local driver and inject a
`LocalInferenceProvider` that owns the transport.

## Design

**Selection.** New env var `PIPELINE_LOCAL_PROVIDER` ∈ `{ollama, lmstudio, mlx, openai_compat}`,
default `ollama`. `PIPELINE_LOCAL_ENDPOINT` keeps its meaning (per-provider default when unset).

**New shared module `inference_providers.py`** (importable by both `backend.py` and
`scripts/local_agent.py`; deps limited to `httpx` so the subprocess stays light):

```python
@dataclass
class ChatResult:
    text: str
    tool_calls: list[dict]          # normalized to one internal shape
    usage: dict                     # normalized: {input_tokens, output_tokens, ...}

class LocalInferenceProvider(Protocol):
    name: str
    default_endpoint: str
    def chat(self, messages, *, model, num_ctx, temperature, tools) -> ChatResult
    def stream_chat(self, messages, *, model, num_ctx, temperature, tools) -> Iterator[dict]
    def loaded_models(self, endpoint) -> set[str]     # empty set if the server has no such concept
    def reachable(self, endpoint) -> tuple[bool, str] # backs resource_status()

def get_local_provider(name: str | None = None) -> LocalInferenceProvider  # factory, default ollama
```

**Providers:**
- `OllamaProvider` (default) — native `/api/chat`, `options.num_ctx`, `/api/ps`, `/api/tags`,
  native `message.tool_calls`, Ollama usage envelope, 429 → `RateLimitedError`. Wraps today's
  exact behavior — this is the regression baseline.
- `OpenAICompatProvider` (base) — `/v1/chat/completions` (SSE stream), OpenAI `tools` /
  `tool_calls` schema, `usage.prompt_tokens` / `completion_tokens`, `/v1/models` for
  reachability. Context is set at model-load time, so `num_ctx` is accepted but not sent
  per-request (documented divergence from Ollama).
  - `LMStudioProvider` (**stub**) — subclass; overrides `loaded_models()` to read LM Studio's
    `/api/v0/models` (which reports loaded state) and sets `default_endpoint`
    `http://localhost:1234`.
  - `MLXProvider` (**stub**) — subclass; `mlx_lm.server` serves one model per process, so
    `loaded_models()` returns an empty set (no swap concept) and `default_endpoint`
    `http://localhost:8080`.

Stubs are registered in the factory and raise `NotImplementedError` from `chat`/`stream_chat`
with a message naming exactly what a real impl must validate (SSE parsing, tool-call schema
mapping, usage-field mapping) — same pattern as the Jira stub in the ticketing plan.

## Stories (TDD, in order)

**S1 — Provider interface + factory + `OllamaProvider`.** Create `inference_providers.py`
with the Protocol, `ChatResult`, `get_local_provider()` (default `ollama`), and
`OllamaProvider` carrying the exact `/api/chat` + `/api/ps` + `/api/tags` behavior lifted
verbatim from `OllamaDriver._chat` and `_ollama_loaded_models`. No call sites changed.
- Tests: factory defaults to Ollama; `PIPELINE_LOCAL_PROVIDER=lmstudio|mlx` returns the
  right stub class; unknown value raises a clear config error; `OllamaProvider.chat` builds
  the same request body (native `num_ctx`, tools) as today against a mocked endpoint;
  usage envelope normalizes correctly.

**S2 — Route `OllamaDriver` through the provider (rename → `LocalDriver`).** Replace the
inline `_chat`/`resource_status`/`_ollama_loaded_models` bodies with delegation to
`self.provider = get_local_provider()`. Keep the `OllamaDriver` name as an alias so nothing
importing it breaks. `pipeline_mcp_server.py:2251` calls the provider's `loaded_models`.
- Tests: **existing Ollama-path tests pass unchanged** (golden regression) — this is the
  critical gate; `resource_status` still probes reachability via the provider; the VRAM-swap
  warn still fires against a mocked `/api/ps`.

**S3 — Provider-ize the dispatch subprocess.** `scripts/local_agent.py` builds its chat
round-trip via `get_local_provider(os.environ["LOCAL_AGENT_PROVIDER"])` instead of hardcoding
`/api/chat`. `OllamaDriver.dispatch` passes `LOCAL_AGENT_PROVIDER` through the subprocess env
(alongside the existing `LOCAL_AGENT_ENDPOINT` at `backend.py:832`).
- Tests: with provider unset/`ollama` the streaming loop is byte-for-byte today's behavior
  against a mocked stream; env plumbs the provider name into the subprocess.

**S4 — LM Studio + MLX stubs + docs.** Register both stub providers; document
`PIPELINE_LOCAL_PROVIDER` and each provider's default endpoint in the `backend.py` module
header, README env table, and CLAUDE.md. Note the `num_ctx`-at-load divergence for
OpenAI-compat servers.
- Tests: selecting `lmstudio`/`mlx` and calling `chat` raises the documented
  `NotImplementedError`; `MLXProvider.loaded_models` returns `set()`; skipped contract-test
  files exist for the follow-up impls.

## Back-compat & risk
- Ollama stays the default; `PIPELINE_LOCAL_ENDPOINT` and every `PIPELINE_LOCAL_*` /
  `LOCAL_AGENT_*` knob keep working. Unset `PIPELINE_LOCAL_PROVIDER` ⇒ identical behavior.
- Highest-risk story is **S2** (regression surface = every local dispatch/review/overlord run
  and the whole benchmark). Gate it on the existing Ollama test suite passing untouched before
  merge; do not modify those tests (per CLAUDE.md Step 4).
- The dispatch subprocess (`local_agent.py`) is the trickiest seam because it runs
  out-of-process — S3 must plumb the provider name through env, not assume shared globals.

## Follow-up (separate plans)
- Implement `LMStudioProvider` for real (SSE parsing, `/api/v0/models` loaded-state, tool-call
  schema mapping), validated against a running LM Studio.
- Implement `MLXProvider` for real against `mlx_lm.server`.
- Optional generic `OpenAICompatProvider` exposed directly (vLLM / llama.cpp-server) once the
  base is proven by one concrete subclass.
