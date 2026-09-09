"""Story: append a no-native-tools / no-MCP framing sentence to _SYSTEM_PROMPT_PREFIX.

The sentence must tell the model, in substance:
  * this session deliberately has no native Claude Code tools and no MCP
    servers connected (by design, for least-privilege isolation);
  * do not conclude from that that the [TOOL_CALL] instruction is
    non-functional or unwired - the surrounding chat harness parses
    [TOOL_CALL] blocks out of the response text and executes them on the
    model's behalf, so it is the real and only mechanism available;
  * it must always be used to call a tool rather than describing an intended
    action or answering directly without calling one.

The sentence is appended to _SYSTEM_PROMPT_PREFIX, so in the assembled
SYSTEM_PROMPT it lands BEFORE the "Available tools:" rendering and BEFORE
_FINAL_SENTENCE (the enumerated tools follow the framing, not precede it).

Grading rules honored here (see story brief):
  * membership/regex checks only - NEVER exact equality on SYSTEM_PROMPT or
    _SYSTEM_PROMPT_PREFIX, because _available_tools_sentence() grows as more
    tools register in TOOLS and a later sibling story must not break this
    file;
  * no live LLM calls - assertions run against the SYSTEM_PROMPT string
    directly, per this repo's chat test conventions;
  * this is a pure append: nothing existing is removed or reordered, so the
    pre-existing substring contracts are re-asserted here unmodified.
"""

from __future__ import annotations

import re

import pytest

import app.chat as chat_module
from app.chat import _FINAL_SENTENCE, _SYSTEM_PROMPT_PREFIX, SYSTEM_PROMPT

# ---------------------------------------------------------------------------
# Fixtures: the assembled prompt and its three parts, freshly derived.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def prompt() -> str:
    return SYSTEM_PROMPT


@pytest.fixture(scope="module")
def prefix() -> str:
    return _SYSTEM_PROMPT_PREFIX


@pytest.fixture(scope="module")
def tools_sentence() -> str:
    return chat_module._available_tools_sentence()


# ---------------------------------------------------------------------------
# 1. The new sentence's key substance is present (membership checks only).
# ---------------------------------------------------------------------------

class TestNewSentenceSubstance:
    """Each test pins one clause of the required substance via substring or
    regex membership - never exact full-string equality."""

    def test_no_native_tools_claim_present(self, prompt: str) -> None:
        assert re.search(
            r"no native (Claude Code )?tools", prompt, re.IGNORECASE
        ), "prompt must state the session has no native Claude Code tools"

    def test_no_mcp_servers_claim_present(self, prompt: str) -> None:
        assert re.search(
            r"no MCP servers", prompt, re.IGNORECASE
        ), "prompt must state that no MCP servers are connected"

    def test_by_design_least_privilege_rationale_present(self, prompt: str) -> None:
        assert re.search(
            r"by design", prompt, re.IGNORECASE
        ), "prompt must state the no-tools/no-MCP setup is deliberate (by design)"
        assert re.search(
            r"least[- ]privilege", prompt, re.IGNORECASE
        ), "prompt must give least-privilege isolation as the rationale"

    def test_do_not_conclude_unwired_warning_present(self, prompt: str) -> None:
        assert re.search(
            r"do not conclude", prompt, re.IGNORECASE
        ), "prompt must warn the model not to conclude the [TOOL_CALL] instruction is dead"

    def test_non_functional_or_unwired_mentioned(self, prompt: str) -> None:
        assert re.search(
            r"(non-?functional|unwired)", prompt, re.IGNORECASE
        ), "prompt must name the wrong conclusion to avoid (non-functional / unwired)"

    def test_harness_parses_tool_call_blocks_claim_present(self, prompt: str) -> None:
        assert re.search(
            r"harness", prompt, re.IGNORECASE
        ), "prompt must name the surrounding chat harness as the executor"
        assert re.search(
            r"pars\w+ .{0,40}\[TOOL_CALL\]", prompt, re.IGNORECASE
        ), "prompt must state the harness parses [TOOL_CALL] blocks out of the response text"

    def test_executes_on_models_behalf_claim_present(self, prompt: str) -> None:
        assert re.search(
            r"execut\w+ .{0,60}(on your behalf|on the model'?s behalf|on your behalf)",
            prompt,
            re.IGNORECASE,
        ), "prompt must state the harness executes the parsed calls on the model's behalf"

    def test_real_and_only_mechanism_claim_present(self, prompt: str) -> None:
        assert re.search(
            r"(real and only|only .{0,30}mechanism|only mechanism)", prompt, re.IGNORECASE
        ), "prompt must state [TOOL_CALL] is the real and only tool-calling mechanism in this session"

    def test_always_use_not_describe_or_answer_directly_present(self, prompt: str) -> None:
        assert re.search(
            r"always", prompt, re.IGNORECASE
        ), "prompt must instruct that the mechanism must always be used to call a tool"
        assert re.search(
            r"rather than (describing|describ\w+)", prompt, re.IGNORECASE
        ), "prompt must forbid describing an intended action instead of calling"
        assert re.search(
            r"(answering directly|answer directly)", prompt, re.IGNORECASE
        ), "prompt must forbid answering directly without calling a tool"

    def test_tool_call_tag_referenced_in_new_substance(self, prompt: str) -> None:
        # The framing sentence must keep the concrete [TOOL_CALL] tag visible
        # so the model connects the warning to the protocol it already knows.
        assert "[TOOL_CALL]" in prompt


# ---------------------------------------------------------------------------
# 2. Position: new sentence sits before the tool list and before the final
#    sentence in the assembled SYSTEM_PROMPT.
# ---------------------------------------------------------------------------

class TestNewSentencePosition:
    def test_substance_appears_before_available_tools_rendering(
        self, prompt: str, tools_sentence: str
    ) -> None:
        # Anchor on the "Available tools:" rendering itself, not on any tool
        # name, so a later sibling story registering more tools cannot break
        # this ordering assertion.
        anchor = "Available tools:"
        assert anchor in prompt
        assert anchor in tools_sentence
        # The no-native-tools / no-MCP framing must precede the tool list.
        framing_idx = prompt.lower().index("no native")
        assert framing_idx < prompt.index(anchor), (
            "the new framing sentence must appear before the 'Available tools:' "
            "rendering in the assembled SYSTEM_PROMPT"
        )

    def test_substance_appears_before_final_sentence(self, prompt: str) -> None:
        assert _FINAL_SENTENCE in prompt
        framing_idx = prompt.lower().index("no native")
        assert framing_idx < prompt.index(_FINAL_SENTENCE), (
            "the new framing sentence must appear before _FINAL_SENTENCE in the "
            "assembled SYSTEM_PROMPT"
        )

    def test_new_sentence_lives_in_prefix_not_in_tools_sentence(
        self, prefix: str, tools_sentence: str
    ) -> None:
        # The story appends to _SYSTEM_PROMPT_PREFIX specifically; the derived
        # "Available tools:" sentence must stay derived from TOOLS only.
        assert re.search(r"no native", prefix, re.IGNORECASE)
        assert re.search(r"no MCP servers", prefix, re.IGNORECASE)
        assert not re.search(r"no native", tools_sentence, re.IGNORECASE)
        assert not re.search(r"no MCP servers", tools_sentence, re.IGNORECASE)


# ---------------------------------------------------------------------------
# 3. Pure append: nothing existing is removed or reordered.
# ---------------------------------------------------------------------------

class TestPureAppendContract:
    def test_assembly_formula_unchanged(self) -> None:
        # SYSTEM_PROMPT must still be prefix + derived tools sentence + final
        # sentence (same contract the readonly-tools story pins).
        assert SYSTEM_PROMPT == (
            chat_module._SYSTEM_PROMPT_PREFIX
            + chat_module._available_tools_sentence()
            + chat_module._FINAL_SENTENCE
        )

    def test_final_sentence_still_ends_the_prompt(self, prompt: str) -> None:
        assert prompt.endswith(_FINAL_SENTENCE)

    def test_preexisting_prefix_sentences_all_still_present(self, prefix: str) -> None:
        # Every sentence of the old prefix survives the append, in order.
        originals = [
            "You are a helpful assistant.",
            "When you need to call a tool, emit a JSON object with keys name and args, wrapped exactly in [TOOL_CALL] and [/TOOL_CALL] tags.",
            "When a tool returns a result, wrap it in [TOOL_RESULT name=...] and [/TOOL_RESULT] tags.",
            "If no tool calls are needed, simply answer in natural language.",
            "You can read plan and story status, journals, logs, and checklists.",
            "You can execute control actions (dispatch, interrupt, patch, review, advance, pause, resume, mark done).",
            "All actions go through the HTTP API and are subject to server-side gates - if a gate blocks an action, surface the rejection to the user; do NOT attempt to bypass it.",
            "To help the user author a plan, call decompose with their goal to get a first draft.",
            "Show the draft and ask if they want to iterate.",
            "When satisfied, call save_plan then ingest_plan.",
            "Always confirm with the user before calling ingest_plan - ingestion dispatches stories.",
            "You can surface decisions the overlord has ruled on by calling list_decisions.",
            "If the user wants to override or supplement a ruling, record their answer via answer_decision.",
            "Human answers are appended to the same decision log as overlord rulings, preserving the audit trail.",
        ]
        cursor = 0
        for sentence in originals:
            idx = prefix.find(sentence, cursor)
            assert idx != -1, f"pre-existing prefix sentence was removed or reordered: {sentence!r}"
            cursor = idx + len(sentence)

    def test_new_sentence_is_appended_not_prepended(self, prefix: str) -> None:
        # The new framing must come AFTER the last pre-existing prefix
        # sentence, i.e. it is an append at the end of the prefix.
        last_original = (
            "Human answers are appended to the same decision log as overlord rulings, preserving the audit trail."
        )
        assert last_original in prefix
        assert prefix.index(last_original) < prefix.lower().index("no native"), (
            "the new sentence must be appended after the existing prefix text, not inserted before it"
        )

    def test_prefix_still_ends_with_the_new_sentence_before_closing_paren(
        self, prefix: str
    ) -> None:
        # The prefix constant is a parenthesized string literal; the new
        # sentence should be its final content.
        stripped = prefix.rstrip()
        assert re.search(r"no native", stripped, re.IGNORECASE)
        assert stripped.lower().rindex("no native") > stripped.index(
            "Human answers are appended"
        )

    def test_tools_sentence_still_derived_from_registry(self) -> None:
        # The derived sentence still enumerates every registered tool name -
        # membership only, never exact contents (shared artifact rule).
        for name in chat_module.TOOLS:
            assert name in chat_module._available_tools_sentence()


def sys_modules_this_module():
    import sys

    return sys.modules[__name__]