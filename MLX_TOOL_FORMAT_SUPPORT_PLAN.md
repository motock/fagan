# Plan: Expand MLX tool-call format support (Mistral + Qwen3-Coder)

> **Status (2026-07-15):** DRAFT, not started. Persisted for execution later. Written during the
> MLX-default-provider investigation (see `MLX_DEFAULT_PROVIDER_PLAN.md`) after establishing that
> most non-Qwen models in the 14B→30B gap are blocked purely by tool-call *plumbing*, not model
> capability. Two independent tracks; **Track A (Mistral) is the higher-value one on this 24GB host**
> because Mistral-Small-24B fits the headroom budget, whereas Qwen3-Coder's only fitting size (30B-A3B)
> panics on 24GB regardless (Track B is a cheap correctness fix whose live payoff is gated by hardware).

## Background: the two layers of "lacks tools"

"Model X lacks tools" conflates two independent, separately-fixable layers. Neither is an intrinsic
model limitation — both live in *our* serving stack:

- **Layer 1 — input template** (rendering available tools *to* the model). `mlx_lm.server` applies the
  model's Jinja `chat_template` (from `tokenizer_config.json`) to format `tools=[...]` into the prompt.
  If the template has no `tools` branch, the schemas are **silently dropped** and the model never learns
  any tools exist. Injection point: `mlx_lm.server` supports a **`--chat-template` override**
  (verified at `.venv-mlx/.../mlx_lm/server.py:323` — `if cli_args.chat_template:
  self._tokenizer_config["chat_template"] = cli_args.chat_template`).
- **Layer 2 — output parser** (extracting the call *from* the model's text). `mlx_lm.server` natively
  understands only `<tool_call>{json}</tool_call>` (Hermes/Qwen). Everything else falls to our
  `recover_tool_calls()` fallback (`scripts/local_agent.py:404` + its verbatim copy in
  `scripts/local_agent_oracle.py`). That function already handles Hermes `<tool_call>`, bare JSON,
  Mistral `[TOOL_CALLS]`, ```json fences, and (added 2026-07-14) Python triple-quoted args.

The only genuinely unfixable-by-plumbing limit is **raw coding capability** — a model that writes
wrong logic stays wrong. That is out of scope here; this plan is purely about wire-format plumbing.

---

## Track A — Mistral tool support (Layer 1: chat template)

**Applies to:** `Mistral-Small-24B-Instruct-2501`, `Codestral-22B`, `Devstral-Small` (all share the
Mistral tokenizer/template family). Highest-value: Mistral-Small-24B is agentic/function-calling-strong,
~13GB at 4-bit (fits this host's headroom), non-thinking.

**Problem (confirmed):** the shipped `mlx-community`/official Mistral chat templates omit the
tool-rendering block entirely — `tools` and tool-result messages are dropped. This is exactly why the
earlier Devstral MLX attempt failed (see `MLX_DEFAULT_PROVIDER_PLAN.md` G1/S1), *not* an output-format
problem. Confirmed against Mistral-Small-24B's own HF discussions (#3, #13). The **output** side is
already done: Mistral emits `[TOOL_CALLS][{"name":...,"arguments":...}]`, which `recover_tool_calls`
already strips + array-parses (`local_agent.py:440,449`).

**Approach:** supply a correct Mistral-v3 tool-aware Jinja template via the `--chat-template` override
(no model-file mutation). The template must render: `[INST]…[/INST]` turns; `[AVAILABLE_TOOLS][{json
schemas}][/AVAILABLE_TOOLS]` before the (last) user turn; assistant tool calls in history as
`[TOOL_CALLS][…]`; tool results as `[TOOL_RESULTS]{…}[/TOOL_RESULTS]`; correct `<s>`/`</s>` and
special-token handling. Source the template from `mistral_common` (official), vLLM's Mistral tool
template, or the community HF PRs — do **not** hand-roll from scratch if a vetted one exists.

### Stories (TDD, in order)

**A1 — Establish the template contract + verify the injection mechanism.**
- Read `mlx_lm/server.py`'s `--chat-template` handling: does the CLI arg take a **file path** or an
  **inline string**? (Line 323 assigns the value directly to `chat_template`; confirm how argparse
  reads it and whether a file must be read first.) Document the exact form the supervisor must pass.
- Obtain a vetted Mistral-v3 tool template (mistral_common / vLLM / community PR). Store it in-repo,
  e.g. `scripts/chat_templates/mistral_v3_tools.jinja`.
- **Test:** render a fixed conversation-with-tools (system + user + a `tools` list) through the
  template and assert the output contains `[AVAILABLE_TOOLS]`, each tool's name/JSON schema, and a
  well-formed `[INST]` structure. Render a follow-up including an assistant `[TOOL_CALLS]` and a tool
  result; assert `[TOOL_RESULTS]` appears. **Acceptance:** tools and tool-results both render.

**A2 — Wire the template override into the supervisor.**
- Add `MLX_CHAT_TEMPLATE_PATH` env (unset = use the model's own template, current behavior). When set,
  `scripts/mlx_server_supervisor.py`'s `start_server()` passes `--chat-template` (path or file
  contents per A1's finding) through the wrapper to `mlx_lm.server`.
- **Test:** with the env set, the constructed launch argv contains the `--chat-template` argument
  pointing at the configured template; unset → no such arg (regression: Qwen path unchanged).

**A3 — Lock in the output side.**
- Add a regression test: `recover_tool_calls` parses `[TOOL_CALLS][{"name":"str_replace",
  "arguments":{...}}]` (Mistral array form) to the correct `{name, arguments}`. Add to both
  `test_local_agent.py` and `test_local_agent_oracle.py`. (Likely already green — this pins it.)

**A4 — Live end-to-end validation.**
- Download a Mistral-Small-24B MLX 4-bit build (~13GB; watch peak wired memory vs the ~18-19GB panic
  ceiling, same discipline as the 32B-3bit test). Launch with `MLX_CHAT_TEMPLATE_PATH` set. Run one
  benchmark cell (`token_bucket`).
- **Acceptance:** the model actually emits tool calls that land as edits (impl file written) — i.e. the
  exact failure mode Devstral hit is gone. Capability grading is secondary to proving the tool loop
  closes.

**Risks:** Jinja correctness for multi-turn + tool-results is the main one; the MLX-quantized build's
tokenizer must carry the `[AVAILABLE_TOOLS]`/`[TOOL_CALLS]`/`[TOOL_RESULTS]` special tokens (Devstral's
did — verify for the chosen build). Effort: **low–moderate** (output already handled; the work is one
vetted template + wiring + tests).

---

## Track B — Qwen3-Coder tool support (Layer 2: XML output parser)

**Applies to:** `Qwen3-Coder-30B-A3B-Instruct` (and the 480B, irrelevant here). **Lower priority on
this host:** the 30B-A3B footprint (~17GB) reproduces the panic condition on 24GB, so even with the
parser fixed the model isn't stably runnable here. The parser is still worth building — it's cheap, a
genuine correctness fix, and unblocks Qwen3-Coder on any adequate-RAM host / future smaller release.

**Problem (confirmed):** Qwen3-Coder's chat template *does* render tools (Layer 1 is fine — it's a Qwen
template), but the model emits calls in a bespoke XML dialect, not JSON:
```
<tool_call>
<function=NAME>
<parameter=ARG>
value
</parameter>
</function>
</tool_call>
```
`mlx_lm.server`'s native parser expects `json.loads`-able content between `<tool_call>` tags, so this
XML falls through; `recover_tool_calls` doesn't recognize it either. This — not capability — is why the
earlier Qwen3-Coder MLX run produced step-0 prose and zero edits (`MLX_DEFAULT_PROVIDER_PLAN.md` G1).

**Approach:** add an XML-format branch to `recover_tool_calls` (both files). Detect `<function=` and
parse: the function name from `<function=(\w+)>`, then each `<parameter=(\w+)>\n(.*?)\n</parameter>`
pair (DOTALL, non-greedy) into the arguments dict; support multiple parameters, multi-line code values,
and multiple `<function=…>` blocks inside one `<tool_call>`.

### Stories (TDD, in order)

**B1 — Add the Qwen3-Coder XML parser to `recover_tool_calls`.**
- **Failing tests first**, using real Qwen3-Coder XML samples: (a) single call, one param; (b) multiple
  params; (c) a multi-line code value inside a `<parameter>`; (d) two `<function=…>` blocks in one
  `<tool_call>`. Assert each parses to the correct `{name, arguments}`.
- Implement the parser branch; mirror verbatim into `local_agent_oracle.py`.
- **Acceptance:** all XML samples parse correctly; existing formats (Hermes/JSON/`[TOOL_CALLS]`/fences/
  triple-quote) still parse; ordinary prose still returns `None` (fail closed — no phantom calls).

**B2 — Argument type handling decision.**
- XML parameter values arrive as strings; the tool schema may declare non-string types. Decide whether
  to coerce (e.g. attempt `json.loads` per value, fall back to string) or keep strings. For the actual
  tools used (str_replace/create_file/bash — path/content/command are all strings) strings suffice;
  document the decision and test one representative case. Keep it minimal — don't over-coerce.

**B3 — Wire-level validation (host-constrained).**
- Unit tests are the primary proof. For a live check, feed a canned Qwen3-Coder XML response through
  the dispatch parsing path and assert a tool call is recovered. A full benchmark run is **deferred**
  until run on adequate-RAM hardware (30B-A3B panics on 24GB); note this explicitly so nobody burns a
  session trying to benchmark it here.
- **Acceptance:** the parsing path recovers a tool call from a real Qwen3-Coder XML sample end-to-end
  (no full dispatch run required on this host).

**Risks:** the XML dialect isn't always well-formed XML (values contain `<`/`>`/quotes), so use targeted
regex, not an XML parser. Effort: **moderate** (one parser + tests, ×2 files). Live payoff on this host
is gated by the 30B memory ceiling — build it for correctness/portability, not for immediate use here.

---

## Prioritization & execution

1. **Track A (Mistral)** first — unlocks a capable model (Mistral-Small-24B) that actually fits this
   host, and the output side is already done.
2. **Track B (Qwen3-Coder)** second — cheap correctness fix, but no immediate live payoff on 24GB.

Both tracks are self-contained in this repo (`recover_tool_calls`, the supervisor, an in-repo template)
and follow the same TDD + `review_story` gate as any other work. To execute: register as a pipeline
plan via `save_plan`/`ingest_plan` (each story above maps to a story with `agent_instructions` +
acceptance), or drive directly. Shared validation discipline for any live model run: watch wired memory
via `vm_stat` against the ~18-19GB panic ceiling, keep `MLX_PROMPT_CACHE_SIZE=1`, and treat a new
`/Library/Logs/DiagnosticReports/*.panic` as an immediate stop.
