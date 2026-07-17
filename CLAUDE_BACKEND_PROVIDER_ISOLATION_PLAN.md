# Claude Backend Provider Isolation — Fix Plan

**Source:** 2026-07-16 investigation, prompted by a user report: launching the
interactive Claude Code session against a different (non-Anthropic) cloud
model left Anthropic usage flat while the alternate provider's usage climbed,
coinciding with pipeline review activity. Root cause confirmed by reading
`backend.py`/`pipeline_mcp_server.py` (see below) — not yet fixed.

**Mode:** Not yet started. Route through the pipeline's own story workflow
(`ingest_plan`) per `CLAUDE.md` Step 1 once this plan is approved, unless the
user directs a direct-execution mode instead (as `RELIABILITY_PLAN.md` did).

**Files in play:**
- `backend.py` — `ClaudeCliDriver` (the `claude` CLI subprocess wrapper),
  `get_backend()`
- `pipeline_mcp_server.py` — `_run_reviewer`, `_run_security_reviewer`,
  backend-role resolution (`PIPELINE_BACKEND_<ROLE>`)
- `agents/code-reviewer.md`, `agents/security-engineer.md` — persona
  frontmatter (`model: sonnet`)
- `test_backend.py`, `test_pipeline_mcp_server.py` — mirror existing style,
  add new tests, **do not modify existing tests** without flagging first
- `README.md` — backend-routing section (already flagged as prone to
  overstating guarantees, see `RELIABILITY_PLAN.md` T8/T12)

**Test runner:** `pytest` (pyproject present). Run `pytest -q` after each
task and the full suite at the end.

**Suggested order:** T1 (fixes the actual leak) → T4 (cheap, adds
auditability, no dependency on T1) → T2 (adds a detection layer) → T3 (wires
detection into the existing fail-closed resource gate) → T5 (docs, last, so
it reflects what actually shipped).

---

## Root cause (confirmed by reading the code, not inferred)

1. `PIPELINE_BACKEND_REVIEW` defaults to `"claude"` when unset
   (`pipeline_mcp_server.py:1161`).
2. The review persona's model tier is just the string `"sonnet"`
   (`agents/code-reviewer.md` frontmatter; read via `_persona_default_model`,
   `pipeline_mcp_server.py:1148`).
3. `ClaudeCliDriver` turns that into a literal subprocess call:
   `cmd = ["claude", "-p", prompt, "--model", model]`
   (`backend.py:120`, mirrored in `dispatch()` at `backend.py:215`), run via
   `subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)`
   (`backend.py:139`) / `subprocess.Popen(cmd, cwd=cwd, ...)`
   (`backend.py:222`). **Neither call passes `env=`.** Same for
   `usage_probe_text()`'s `claude -p /cost` call (`backend.py:228`).
4. No `env=` means full inheritance of the calling process's environment —
   which, on the interactive path, is the same environment as the top-level
   `claude` session (the pipeline MCP server is its child process, per
   `~/.claude.json`'s `mcpServers.pipeline` stdio config).
5. Grepped the entire repo for `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
   `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `CLAUDE_CODE_USE_BEDROCK`,
   `CLAUDE_CODE_USE_VERTEX` — **zero hits**. Nothing anywhere strips,
   isolates, or verifies these.
6. `claude --help` confirms first-class support for "3P providers (API key
   users only)" — i.e. redirecting the CLI to a non-Anthropic backend via
   `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` is a real, documented Claude
   Code feature, not a hack.

Net effect: `PIPELINE_BACKEND_REVIEW=claude` + `model: sonnet` means "ask
whatever `claude` binary is on `PATH`, under whatever environment this
process inherited, for tier `sonnet`" — not "ask Anthropic for genuine Claude
Sonnet." If the launching session (or the scheduler's shell) has a
3rd-party-provider redirect exported, every `claude`-backend call — review,
security review, overlord, and dispatch under `PIPELINE_BACKEND_DISPATCH=claude`
— silently rides it. Meanwhile `record_token_usage()` hardcodes
`"backend": "claude"` in the cost sidecar (`backend.py:187`) regardless, so
the audit trail actively misreports what ran.

---

## T1 — Strip provider-redirect env vars from every `claude` subprocess call

**Where:** `backend.py`, `ClaudeCliDriver.complete()` (`:115-167`),
`.dispatch()` (`:205-224`), `.usage_probe_text()` (`:226-235`). None of the
three pass `env=` today.

**Change:**
1. Add a module-level constant enumerating every var that can redirect the
   CLI off the first-party Anthropic API — verify the exact set against
   `claude --help`/current docs before finalizing, don't just use the six
   found this session:
   ```python
   _CLAUDE_PROVIDER_REDIRECT_VARS = (
       "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
       "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
       "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
   )
   ```
2. Add a small helper, e.g. `_first_party_claude_env() -> dict`: copy
   `os.environ`, pop each var in the constant, return it.
3. Pass `env=_first_party_claude_env()` to every `subprocess.run`/`Popen`
   call in `ClaudeCliDriver`.
4. Add an opt-back-in escape hatch for legitimate enterprise deployments
   (Bedrock/Vertex are real deployment modes, not just accidental leaks) —
   e.g. `PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV=1` restores today's
   inherit-everything behavior. **Default off** — deny-by-default, per
   `CLAUDE.md`'s Secure by Design / fail-closed standard.

**Tests (new, `test_backend.py`):**
- With `ANTHROPIC_BASE_URL` (and one var from each redirect category) set in
  the test's env, mock `subprocess.run`/`Popen` and assert none of the
  redirect vars appear in the `env=` kwarg for `complete()`, `dispatch()`,
  and `usage_probe_text()`.
- A harmless ambient var (e.g. `PATH`, a custom test var) **does** pass
  through unchanged — this must stay a targeted strip, not a full
  allowlist/reset of the environment.
- `PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV=1` restores full inheritance
  (regression test for the escape hatch).

---

## T2 — Verify the served model matches the requested tier

**Where:** `ClaudeCliDriver.complete()`'s JSON path (`backend.py:137-167`)
already parses `payload` when `cell_dir` is set, but only for text/usage
extraction — it never inspects `payload.get("model")`, the CLI's own report
of what actually served the request.

**Change:**
1. After parsing `payload`, compare `payload.get("model")` against an
   expected-prefix table keyed by requested tier, e.g.
   `{"sonnet": "claude-sonnet-", "opus": "claude-opus-", "haiku": "claude-haiku-"}`.
2. On mismatch, raise a dedicated exception (e.g.
   `ProviderIdentityMismatch(f"requested tier {model!r} but backend served {served!r}")`)
   instead of silently returning `result_text` — turn a silent divergence
   into a loud one. Check whether `review_story`/`_invoke_overlord` callers
   already handle backend exceptions gracefully (mirror `RateLimitedError`'s
   handling if so; otherwise this needs matching catch/defer logic, not just
   an uncaught crash).
3. Record the served model string into the existing `record_token_usage()`
   call regardless of match/mismatch (feeds T4).
4. Skip verification when `cell_dir is None` — same scoping the docstring
   already uses for "callers that don't request structured output."

**Tests (new, `test_backend.py`):** a JSON payload with
`model="claude-sonnet-4-5-..."` and requested tier `"sonnet"` passes
silently; a payload with a non-Anthropic model string and requested tier
`"sonnet"` raises `ProviderIdentityMismatch`; `cell_dir=None` skips
verification entirely (existing text-only behavior unchanged).

---

## T3 — Fail-closed preflight wired into the existing resource gate

**Where:** `backend.get_backend()` (`:1040-1082`) is the single chokepoint
every role (dispatch/review/overlord/security) passes through to obtain a
`ClaudeCliDriver`. `ClaudeCliDriver.resource_status()` (`:237-248`) already
gates dispatch/review on a cached, poller-fed state (`_role_resource_ok`) —
reuse that mechanism rather than adding a new gate.

**Change:**
1. Add `ClaudeCliDriver.verify_identity() -> dict`: run the cheapest
   possible call (`claude -p "1+1" --model sonnet --output-format json`) and
   return `{"ok": bool, "model": str, "reason": str}` using T2's prefix
   check.
2. Cache the result at module scope for the process's lifetime — do not
   re-probe per call (mirrors the usage-gate's own cached-state pattern
   noted in `resource_status()`'s docstring).
3. Fold the cached result into `resource_status()` so a failed identity
   check returns `{"ok": False, "reason": "Claude backend identity check
   failed: served <model>, expected claude-sonnet-*"}` — blocks
   dispatch/review through the exact same path a tripped usage gate already
   uses. No new gating code path needed.

**Tests (new, `test_backend.py`):** `verify_identity()` with a mocked
genuine-Anthropic response returns `ok: True`; with a mocked non-Anthropic
model returns `ok: False` plus a reason string; `resource_status()` reflects
a failed identity check the same way it reflects a tripped usage-gate pause
(mirror the existing pause test).

---

## T4 — Record requested vs. served model in the audit sidecar

**Where:** `record_token_usage()` (`backend.py:169-203`) hardcodes
`"backend": "claude"` (`:187`) and writes `"model": usage.get("model", "?")`
where, today, that value is only ever the *requested tier* string
(`complete()` passes `"model": model` at `:163` — the tier, not what
actually served it).

**Change:** Add a `"served_model"` field populated from `payload.get("model")`
(only available on the JSON path, i.e. when `cell_dir` was set) alongside the
existing `"model"` (requested tier) field. Keeps `"model"` backward-compatible
while giving a retroactive `jq`-able audit trail over
`review_token_costs.jsonl` that can catch drift even for calls predating
T2/T3, or when `PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV=1` is set intentionally.

**Tests (new, `test_backend.py`):** a usage dict containing both `"model"`
and `"served_model"` writes both to the JSONL line; omitting `"served_model"`
(older call sites / non-JSON path) writes `null`, not a `KeyError`.

---

## T5 — Documentation

**Where:** `README.md`'s backend-routing section, `CLAUDE.md`.

**Change:** Document that the `"claude"` backend
(`PIPELINE_BACKEND_<ROLE>=claude`, the default) is, post-T1, hard-isolated
from the invoking session's own provider configuration — an interactive
session pointed at a 3rd-party provider no longer affects pipeline
dispatch/review even though the MCP server is its child process. Note the
`PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV` escape hatch and that it defaults off.

If T1 ships partially or is deferred, document the gap explicitly as an
operational trap instead: *"Do not run the scheduler or interactive pipeline
work from a shell with `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` pointed at
a non-Anthropic provider — this pipeline's `claude` backend has no isolation
from that (see `CLAUDE_BACKEND_PROVIDER_ISOLATION_PLAN.md`) and will
silently route review/dispatch through it."*

---

## Final verification (all tasks)

1. `pytest -q` — full suite green; report pass/fail counts against the
   current baseline.
2. Confirm no existing test was modified (`git diff` the test files).
3. Sanity-check: `python -c "import backend, pipeline_mcp_server"` must not
   raise.
4. Live smoke test (manual, not automated): export a bogus
   `ANTHROPIC_BASE_URL` in a throwaway shell, invoke `_run_reviewer` (or
   `review_story`) against a scratch worktree, and confirm — post T1 — the
   subprocess env passed to `claude` no longer contains it (log/print the
   resolved `env=` under a debug flag if there's no cheaper way to observe
   it).
5. Present a per-file change summary + Conventional Commit message per task
   (logically separate commits, e.g. `fix(backend): isolate claude CLI
   subprocess env from provider-redirect vars`, `feat(backend): verify
   served model matches requested tier`, `feat(backend): gate claude backend
   on identity preflight`, `feat(backend): record served model in usage
   sidecar`, `docs: document claude backend provider isolation`). Ask before
   committing.
