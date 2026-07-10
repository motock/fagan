# Plan: Abstract the local model-inference layer (Ollama default, pluggable)

> Status: **All four stories implemented 2026-07-09/10; all three registered providers
> (Ollama, MLX, LM Studio) are real, live-validated implementations** — none are stubs
> anymore. `inference_providers.py` holds `LocalInferenceProvider`, `OllamaProvider`,
> `MLXProvider`, `LMStudioProvider`, `RateLimitedError`, and `get_local_provider()`.
> `OllamaDriver._chat`/`resource_status()` delegate to `self.provider`; `_ollama_loaded_models`
> stays hardcoded to `OllamaProvider` (see below). All existing + 35 new tests pass unchanged
> (791 total vs. 756 on `master`).
>
> **MLXProvider was validated against a real `mlx_lm.server`** (installed into a throwaway
> venv, not a project dependency), not just mocked: `mlx-lm==0.28.3` on Python 3.11 (Python
> 3.14 + `transformers` 5.13.0 breaks `mlx_lm`'s tokenizer registration — pin
> `transformers<5`) serving `mlx-community/Qwen2.5-1.5B-Instruct-4bit`. Confirmed live: basic
> `complete()` content, a full tool-calling round trip (`tools` → `tool_calls` with
> JSON-string `arguments`, matching what `backend.py`'s existing parser already handles), and
> a genuine multi-turn review loop (`bash`/`view_file` tool calls exchanged correctly via
> `OllamaDriver.complete()` in review mode — the 1.5B model didn't converge to a verdict in 5
> steps, a model-quality/step-budget limit, not a wiring bug). Findings baked into
> `MLXProvider`'s docstring: `tool_calls[].function.arguments` is a JSON string,
> `tool_calls[].id` is always `null`, there's no per-request context-window control (so
> `num_ctx` is sent as `max_tokens` instead, since the server's own default is a tiny 512
> tokens), and a model emitting malformed tool-call JSON crashes the server's connection
> (surfaces as an `httpx.HTTPError` subclass, already handled by existing callers).
>
> **LMStudioProvider was validated the same way against a real LM Studio server** (already
> installed on this machine — `~/.lmstudio/bin/lms server start`, no throwaway venv needed),
> serving the user's already-downloaded `google/gemma-4-e4b` (4B, tool-capable). Confirmed
> live: basic `complete()` content, a full tool-calling round trip, and — going further than
> MLX's validation — a genuine multi-turn review loop that actually **converged to a real
> `VERDICT: APPROVE`** (bash + view_file tool calls, then submit_review; the stronger 4B model
> succeeded where MLX's 1.5B one hit its step cap). Findings baked into `LMStudioProvider`'s
> docstring: same JSON-string `tool_calls[].function.arguments` shape as MLX, but `id` is a
> real non-null string here (harmless either way — the caller only reads name/arguments); no
> per-request context-window control (same `max_tokens`-for-`num_ctx` mapping as MLX);
> loaded-model state comes from LM Studio's own `/api/v0/models` (`state: loaded/not-loaded`),
> not Ollama's `/api/ps`; and LM Studio JIT-loads a model on its first request (~30s observed
> for the 4B model) rather than requiring it pre-loaded like Ollama expects.
>
> **Also discovered (pre-existing, not introduced by this work):** `_resolve_local_model`'s
> "contains `:` → concrete tag, else → tier name" heuristic assumes Ollama's `name:tag`
> convention; an MLX/LM Studio Hugging Face repo id (e.g. `mlx-community/...`,
> `google/gemma-4-e4b`) has no `:`, so it must be set via
> `PIPELINE_LOCAL_MODEL_DEFAULT`/`_OPUS`/`_SONNET`/`_HAIKU`, not passed directly as `model=`.
> Documented in the README; not changed, since it's an existing Ollama-tag-oriented function
> outside this task's scope.
>
> **S3 implemented** (2026-07-09/10, direct-edit + PR, not a dispatched pipeline story — a
> deliberate scope decision, see below): rather than porting Ollama's streaming NDJSON
> `/api/chat` loop to SSE for the OpenAI-compatible servers (the materially higher-risk path
> originally flagged here), `scripts/local_agent.py`/`local_agent_oracle.py`'s `chat()` now
> branches on `LOCAL_AGENT_PROVIDER` (threaded through by `OllamaDriver.dispatch` from
> `self.provider.name`): `"ollama"` (the default) keeps today's streaming path byte-for-byte
> unchanged; any other provider goes through a new blocking `_provider_chat_turn`, which calls
> `inference_providers.get_local_provider().chat(...)` directly (the same call `complete()`/the
> review loop already use) and returns just the assembled message, matching
> `_stream_one_turn`'s contract. `RateLimitedError` (429) is folded into the existing 5xx-style
> retry branch. Trade-off accepted: a slow non-Ollama generation is bounded by a flat
> `LOCAL_AGENT_TIMEOUT` instead of a per-chunk silence timeout — acceptable since MLX/LM
> Studio's SSE tool-call deltas were the actual risk being deferred, not streaming per se.
> `_ollama_loaded_models` (the VRAM-swap warning) stays intentionally hardcoded to
> `OllamaProvider` — it's an Ollama-specific concept (one process, many models swapped in/out
> of VRAM), not a stand-in for provider-awareness; LM Studio JIT-loads and mlx_lm.server is
> one-model-per-process, so neither has an equivalent warning to give. `tests/benchmark/
> models.py` gained `lmstudio_gemma4` and `mlx` cells (`_local_provider` helper) so both
> providers can now be benchmarked as the *implementer*, not just the reviewer.

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
- ~~Implement `LMStudioProvider` for real~~ and ~~implement `MLXProvider` for real~~ — both
  done 2026-07-09/10, live-validated (see status header above). Neither needed SSE: both are
  driven through `complete()`/the review loop, which only ever call `chat()` non-streaming
  (`"stream": false`).
- ~~S3 (provider-ize `scripts/local_agent.py`'s dispatch subprocess)~~ — done 2026-07-09/10 via
  the blocking `_provider_chat_turn` branch (see status header above), not the originally
  planned SSE-streaming port. **Still open:** a true streaming path for non-Ollama providers
  (per-chunk silence timeout instead of a flat wall-clock bound) would need the SSE
  index-based tool-call-delta parser this plan deferred as materially higher risk — worth
  revisiting only if the flat-timeout trade-off proves too tight in practice (e.g. a capable
  but slow MLX/LM Studio model timing out mid-generation on a real story).
- Optional generic `OpenAICompatProvider` base class, now that two concrete OpenAI-compatible
  subclasses (MLX, LM Studio) exist with near-identical `chat()`/`reachable()` bodies — could
  be factored to reduce duplication once a third such server is added.
- Live-validate `_provider_chat_turn` against a running LM Studio server dispatching a real
  story end-to-end (create_file/str_replace/bash/done), mirroring how `LMStudioProvider`
  itself was validated — done in code + mocked tests here, not yet against a live server.
