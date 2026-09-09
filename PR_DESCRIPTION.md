# PR: fix(chat): tell the model the [TOOL_CALL] text protocol is real even with zero visible native/MCP tools

Issue: 4aee6deb-2ecc-4374-ad81-bfc2ede6291c

## What changed

One sentence appended to the `_SYSTEM_PROMPT_PREFIX` string constant in
`app/chat.py` (the parenthesized literal assembled into `SYSTEM_PROMPT` via
`SYSTEM_PROMPT = _SYSTEM_PROMPT_PREFIX + _available_tools_sentence() +
_FINAL_SENTENCE`). Nothing else changed: `_available_tools_sentence`,
`_FINAL_SENTENCE`, the `TOOLS` registry, the assembly line, and
`app/backend_claude.py` are untouched. The diff is a single inserted line
inside the existing literal — a pure append.

The new sentence, in the model's framing position (before the
`Available tools:` rendering and before `_FINAL_SENTENCE`), states in substance:

* this session deliberately has **no native Claude Code tools and no MCP
  servers** connected — that isolation is **by design, for least-privilege**;
* **do not conclude** from that that the `[TOOL_CALL]` instruction is
  **non-functional or unwired**;
* the surrounding chat **harness parses `[TOOL_CALL]` blocks** out of the
  response text and **executes them on your behalf**, making that protocol the
  **real and only mechanism** available in this session;
* it must **always** be used to call a tool **rather than describing** an
  intended action or **answering directly** without calling one.

## Why

The prompt already teaches the `[TOOL_CALL]` text protocol, but this session
exposes zero native/MCP tools, so a model can reasonably infer the protocol is
dead config and fall back to describing actions or answering directly instead
of emitting a call. The sentence pre-empts that inference: the harness parses
and executes `[TOOL_CALL]` blocks regardless of the native-tool inventory.

## Tests

New per-story file `tests/unit/test_chat_prompt_no_native_tools_framing.py`
(committed RED by the tech lead; 19 tests, now green):

* substance via substring/regex **membership only** — never exact equality on
  `SYSTEM_PROMPT` or `_SYSTEM_PROMPT_PREFIX`, since
  `_available_tools_sentence()` grows as more tools register in `TOOLS` and a
  later sibling story adding a tool must not have to touch this test;
* position: the framing precedes the `Available tools:` anchor and
  `_FINAL_SENTENCE`, and lives in the prefix, not the derived tools sentence;
* pure-append contract: assembly formula unchanged, `_FINAL_SENTENCE` still
  ends the prompt, all 14 pre-existing prefix sentences present in order, new
  sentence appended after the last original sentence.

Full suite before vs after (failure lists diffed): the **only** delta is the 14
story tests flipping RED→GREEN (58 failed / 8112 passed → 44 failed / 8126
passed; 3 pre-existing errors unchanged). Every pre-existing chat test pinning
an exact substring of `SYSTEM_PROMPT` / `_FINAL_SENTENCE` (e.g. the
final-sentence contract tests) passes completely unmodified. No test file was
edited, and no live LLM / `claude -p` subprocess call was added — assertions
run against the `SYSTEM_PROMPT` string directly, per this repo's chat test
conventions.

## ⚠️ What these green tests do NOT prove

These green tests confirm the **prompt TEXT changed as specified** (membership
check, positioned before the tool list, pure append with existing pins
untouched). They do **NOT** prove that Claude's real behavior has measurably
improved — that requires a live spot-check via a real `claude -p` run outside
this test suite, which the requester will perform manually after merge.

## Files changed

* `app/chat.py` — the only code change (one appended sentence inside
  `_SYSTEM_PROMPT_PREFIX`).
* `tests/unit/test_chat_prompt_no_native_tools_framing.py` — committed earlier
  on this branch by the tech lead (RED→GREEN; not modified by this PR's
  implementation work).