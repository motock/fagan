# LiteLLM Provider

## 1. Overview: the LocalInferenceProvider seam, not a new driver

LiteLLM is wired in at the **`LocalInferenceProvider` seam** (`app/inference_providers.py:374`,
`class LiteLLMProvider`), not as a new `Backend` driver class. The reason is structural:
`OllamaDriver` (`app/backend_ollama.py:73`) already owns every piece of harness mechanics
that is vendor-independent — the native-tool-calling dispatch loop, the read-only review
loop, checkpoint plumbing, and the per-call cost sidecar. The only thing that differs
between inference sources is the wire protocol to the model server itself, and that (and
only that) is what `app/inference_providers.py` isolates behind the
`LocalInferenceProvider` protocol (`app/inference_providers.py:73`).

`LiteLLMProvider` is the fourth implementation of that protocol, following the same
precedent as `LMStudioProvider` (`app/inference_providers.py:484`) and `MLXProvider`
(`app/inference_providers.py:485`): each translates its native wire format into Ollama's
envelope shape — `{"message": {...}, "prompt_eval_count", "eval_count"}` — so
`OllamaDriver`'s existing `complete()`/`_review_loop` code works unchanged.

Routing is a two-level lookup, not a new driver:

| Layer | File | Entry |
|-------|------|-------|
| Backend driver | `app/backend.py:143` | `"litellm": functools.partial(OllamaDriver, provider_name="litellm")` in `_DRIVERS` |
| Wire provider | `app/inference_providers.py:486` | `"litellm": LiteLLMProvider` in `_PROVIDERS` |

So `backend=litellm` still runs `OllamaDriver`; only the provider it delegates chat
completions to changes.

## 2. Installation

`litellm` is an **optional extra, deliberately not in `requirements.txt`**. The module
imports cleanly on a machine with no litellm installed: the import is lazy, inside the
method bodies (`app/inference_providers.py:412-414`, `:473`), never at module top level.

Install command:

```
pip install litellm
```

(No `litellm` extra is declared in `pyproject.toml`; the plain package install is the
supported path, and it is the command `reachable()` itself suggests.)

If litellm is missing, `LiteLLMProvider.reachable()`
(`app/inference_providers.py:471-479`) reports the missing package instead of crashing:

```
(False, 'litellm package not installed; install with pip install litellm')
```

It never raises; the not-installed state surfaces as a not-reachable reason, which backs
`OllamaDriver.resource_status()`.

## 3. Configuration

Three ways to select the provider:

1. **A role in `model_registry.json`.** Add a `litellm` provider block with models, and
   point a role at it. The registry already carries one:

   ```json
   "litellm": {"models": {"gpt5-mini": {"tag": "openai/gpt-5-mini"}}}
   ```

2. **A story-level `"backend": "litellm"`** on the story, which routes that story's role
   through the `litellm` entry of `_DRIVERS` (`app/backend.py:143`).

3. **The per-role environment override** `PIPELINE_BACKEND_<ROLE>` — a *pattern*, not a
   literal variable; `<ROLE>` is the role name uppercased
   (`app/backend.py:173`, `app/role_registry.py:128`). For the `overlord` role defined in
   `model_registry.json`, the concrete variable is:

   ```
   PIPELINE_BACKEND_OVERLORD=litellm
   ```

**Model-string format:** `vendor/model`, e.g. `openai/gpt-5-mini` — the tag recorded for
the `gpt5-mini` model in `model_registry.json`. The prefix before the `/` selects the
upstream vendor; LiteLLM routes the request to that vendor's API. The authoritative list
of supported prefixes is LiteLLM's own provider documentation — do not rely on an
enumerated copy here, it will go stale:
<https://docs.litellm.ai/docs/providers>.

## 4. Environment variables

| Variable | Effect |
|----------|--------|
| `PIPELINE_LOCAL_ENDPOINT_LITELLM` | Optional `api_base` override. There is no local server to point at (the SDK talks straight to the upstream vendor), so `default_endpoint` is `""` (`app/inference_providers.py:405`); when this variable is set to a non-empty value it is forwarded to litellm as its `api_base` (`app/inference_providers.py:393`, `:430-431`). |
| `PIPELINE_ROLE_CALL_TIMEOUT_SECONDS` | Shared with every other local provider. Read at call time by `resolve_role_call_timeout()` (`app/inference_providers.py:37-59`) and applied as the per-attempt completion timeout (`:421-426`); unset/blank/unparseable/non-positive falls back to the hardcoded 600 s default — never an unbounded request. |

**Upstream API keys are read by litellm itself**, from its own provider-specific
environment variables (e.g. `OPENAI_API_KEY` for `openai/...` model strings). The
pipeline passes no key material into `litellm.completion()` — `LiteLLMProvider.chat()`
constructs only `model`, `messages`, `temperature`, `max_tokens`, `timeout`, optionally
`tools` and `api_base` (`app/inference_providers.py:424-431`). Set the vendor's own
variable in the environment and litellm picks it up.

## 5. Security: API keys

Per CLAUDE.md, *"Log messages, stack traces, and error responses must never contain
passwords, tokens, API keys, PII, session identifiers, or internal file paths."*

For this provider the rule is satisfied structurally, not by redaction:

* **Never logged** — the pipeline never receives a key: no `api_key` kwarg is built, and
  litellm reads keys from its own environment variables without the pipeline touching
  the values.
* **Never written to the cost sidecar** — the sidecar
  (`review_token_costs.jsonl`, `app/backend_ollama.py:567-594`) records only token
  counts; `LiteLLMProvider.chat()` maps litellm's usage to
  `prompt_eval_count`/`eval_count` (`app/inference_providers.py:459-463`) and nothing
  else crosses the boundary.
* **Never included in an error message** — errors raised by this provider are built from
  the model name and the exception text only: the 429 path raises
  `RateLimitedError(f"litellm rate limit for model {model}: {exc}")`
  (`app/inference_providers.py:445-447`), and every other exception propagates unchanged
  (`:448`) without the pipeline adding key material to it.

A missing or invalid upstream key therefore surfaces as a provider error from litellm
with no key value in the message — the pipeline never handles the key, so it cannot leak
it. (Note: the code does not perform an explicit redaction step; it never possesses the
key in the first place. Any key text inside a litellm-raised exception is litellm's own
behavior, outside this codebase's control.)

## 6. Known limits

* **`loaded_models()` is always empty.** LiteLLM is a router, not a server: it has no
  loaded-model concept, so `LiteLLMProvider.loaded_models()` returns `set()` without any
  network call (`app/inference_providers.py:466-469`), the same as `MLXProvider`.
* **The free-memory gate is skipped for hosted providers.** `_HOSTED_PROVIDERS =
  frozenset({"litellm"})` (`app/backend_ollama.py:66-69`): a hosted model has no local
  VRAM/RAM footprint, so a free-memory reading is not evidence about whether it can
  serve. `resource_status()` skips the free-memory floor for these providers
  (`app/backend_ollama.py:775-799`) the same way it is skipped for `:cloud` tags;
  reachability still applies.
* **No per-request context-window control.** There is no equivalent of Ollama's
  `options.num_ctx`; `num_ctx` is sent as `max_tokens` — an output-length budget, not a
  true equivalent (`app/inference_providers.py:395-398`, `:426`).
* **`think` is a silent no-op.** Accepted for signature parity with
  `LocalInferenceProvider`; `litellm.completion()` has no equivalent field
  (`app/inference_providers.py:416-418`).
* **Live-host validation: none performed.** No request has been run against a real
  upstream vendor through this provider. The wire-format behavior documented here is
  taken from the code and its docstrings, not from a live capture — unlike
  `LMStudioProvider`/`MLXProvider`, whose formats were each captured live against a
  running server (`app/inference_providers.py:12-14`).

## 7. References

* LiteLLM provider list (authoritative `vendor/` prefixes):
  <https://docs.litellm.ai/docs/providers>
* Sibling specs: [Docker Sandbox](DOCKER_SANDBOX.md),
  [Remote Execution](REMOTE_EXECUTION.md), [Aider Harness](AIDER_HARNESS.md)
* CLAUDE.md — "No sensitive data in logs or errors"
* Code: `app/inference_providers.py` (`LiteLLMProvider`), `app/backend.py` (`_DRIVERS`),
  `app/backend_ollama.py` (`_HOSTED_PROVIDERS`, `resource_status()`),
  `model_registry.json` (`litellm` provider block)