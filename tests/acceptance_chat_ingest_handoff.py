"""Read-only acceptance oracle: the chat prompt agrees with the origin gate.

Graded independently of the story's own test file. ``app/chat.py``'s prompt
told the model to ``ingest_plan`` after ``save_plan``, but that call can never
succeed from a chat session -- ``app/dashboard.py``'s ingest route (and the
workspace route) call ``refuse_chat_origin``, answering 403
``{"detail": "origin not permitted"}`` for the ``X-Pipeline-Origin: chat``
header the chat harness stamps on every internal call. Reproduced live
2026-09-15: the model called ``ingest_plan``, got that 403, and correctly
surfaced it.

So the prompt must stop instructing an impossible call, name the gate message
it would otherwise hit, and mark the two origin-gated tools in the registry
that ``_available_tools_sentence()`` renders into the prompt. This fixture is
RED at a clean baseline -- the prompt still instructs ``ingest_plan`` -- which
is the intended TDD direction, not a broken oracle.
"""
import re

from app import chat

# The two -- and only two -- tools whose backing routes call refuse_chat_origin.
CHAT_REFUSED_TOOLS = {"ingest_plan", "set_workspace"}


def test_prompt_stages_the_plan_instead_of_ingesting_it():
    assert "When satisfied, call save_plan to stage the plan." in chat.SYSTEM_PROMPT


def test_prompt_tells_the_model_not_to_ingest():
    assert "Do not call ingest_plan yourself" in chat.SYSTEM_PROMPT


def test_prompt_names_the_gate_message_the_model_would_hit():
    assert "origin not permitted" in chat.SYSTEM_PROMPT


def test_prompt_forbids_claiming_an_ingest_that_did_not_happen():
    assert "Never tell the user a plan is ingested when it is not" in chat.SYSTEM_PROMPT


def test_prompt_keeps_the_guidance_that_was_never_false():
    prompt = chat.SYSTEM_PROMPT
    assert "pass the decompose result plan JSON verbatim" in prompt
    assert "ingestion dispatches stories" in prompt
    assert "call decompose with their goal" in prompt


def test_prompt_no_longer_instructs_an_impossible_ingest():
    prompt = chat.SYSTEM_PROMPT
    assert "call save_plan then ingest_plan" not in prompt
    assert "Always confirm with the user before calling ingest_plan" not in prompt


def test_the_rewritten_literal_keeps_its_trailing_space():
    """The prompt is a concatenation of adjacent literals; a lost edge space
    would run the sentences together (...MCP session.You can surface...)."""
    assert "or from an operator's MCP session. " in chat.SYSTEM_PROMPT


def test_exactly_the_origin_gated_tools_are_marked_ui_only():
    marked = {
        name
        for name, entry in chat.TOOLS.items()
        if "UI-ONLY" in entry["description"]
    }
    assert marked == CHAT_REFUSED_TOOLS


def test_the_derived_tool_sentence_carries_the_marker():
    sentence = chat._available_tools_sentence()
    for name in sorted(CHAT_REFUSED_TOOLS):
        assert re.search(rf"{name} \([^)]*UI-ONLY[^)]*args: ", sentence), name


def test_reachable_tools_are_not_marked_ui_only():
    for name in ("list_plans", "save_plan"):
        assert "UI-ONLY" not in chat.TOOLS[name]["description"], name
