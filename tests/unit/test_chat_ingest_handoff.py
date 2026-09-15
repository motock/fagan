"""The chat prompt must stop instructing an ingest the chat origin forbids.

``app/chat.py``'s ``_SYSTEM_PROMPT_PREFIX`` told the model to call
``ingest_plan`` after ``save_plan`` and to confirm with the user first. That
call can never succeed from a chat session: ``app/dashboard.py``'s
``POST /api/plans/{plan_name}/ingest`` route calls ``refuse_chat_origin``,
which answers 403 ``{"detail": "origin not permitted"}`` for the
``X-Pipeline-Origin: chat`` header the chat harness stamps on every internal
tool call. The same is true of ``POST /api/workspace`` (``set_workspace``).

So the prompt must tell the truth -- stage the plan, then hand off -- name the
exact gate message the model would otherwise hit, and mark the two
origin-gated tools in the registry that ``_available_tools_sentence()``
renders into the prompt.

Assertions here are membership / absence / single-occurrence only: the prompt
and the tools sentence are cumulative artifacts that later stories extend.
"""
import re
from pathlib import Path

import app.chat as chat_module

# The two -- and only two -- tools whose backing routes call refuse_chat_origin:
# POST /api/workspace (set_workspace) and POST /api/plans/{plan_name}/ingest
# (ingest_plan). Those are the only two call sites in app/dashboard.py.
CHAT_REFUSED_TOOLS = {"ingest_plan", "set_workspace"}

STAGE_SENTENCE = "When satisfied, call save_plan to stage the plan."
NO_INGEST_SENTENCE = "Do not call ingest_plan yourself"
GATE_MESSAGE = "origin not permitted"
NO_FALSE_CLAIM_SENTENCE = "Never tell the user a plan is ingested when it is not"
HANDOFF_TAIL = "or from an operator's MCP session."

INGEST_DESCRIPTION = (
    "Ingest a plan's epics and stories. UI-ONLY: this endpoint refuses calls "
    "from a chat session's origin and answers 'origin not permitted'; only the "
    "dashboard UI or an operator's MCP session can ingest."
)
SET_WORKSPACE_DESCRIPTION = (
    "Set the current workspace. UI-ONLY: this endpoint refuses calls from a "
    "chat session's origin and answers 'origin not permitted'; only the "
    "dashboard UI or an operator's MCP session can switch it."
)


# ---------------------------------------------------------------------------
# positive: the prompt now tells the truth about the handoff
# ---------------------------------------------------------------------------

def test_prompt_stages_the_plan_instead_of_ingesting_it():
    assert STAGE_SENTENCE in chat_module.SYSTEM_PROMPT


def test_prompt_tells_the_model_not_to_ingest():
    assert NO_INGEST_SENTENCE in chat_module.SYSTEM_PROMPT


def test_prompt_names_the_gate_message_the_model_would_hit():
    # The model must recognize the exact 403 detail it would otherwise hit.
    assert GATE_MESSAGE in chat_module.SYSTEM_PROMPT


def test_prompt_forbids_claiming_an_ingest_that_did_not_happen():
    assert NO_FALSE_CLAIM_SENTENCE in chat_module.SYSTEM_PROMPT


def test_prompt_keeps_the_guidance_that_was_never_false():
    prompt = chat_module.SYSTEM_PROMPT
    assert "pass the decompose result plan JSON verbatim" in prompt
    assert "ingestion dispatches stories" in prompt
    assert "call decompose with their goal" in prompt


def test_prompt_points_at_the_dashboard_ingest_control_and_the_mcp_handoff():
    prompt = chat_module.SYSTEM_PROMPT
    assert "the plan is staged" in prompt
    assert "dashboard UI's ingest control" in prompt
    assert HANDOFF_TAIL in prompt


def test_the_rewritten_literal_keeps_its_trailing_space():
    """The prompt is a concatenation of adjacent literals; a lost edge space
    would run the sentences together (...MCP session.You can surface...)."""
    assert "operator's MCP session. " in chat_module.SYSTEM_PROMPT


def test_the_rewritten_literal_keeps_its_neighbours_byte_identical():
    """The replacement sits between two untouched literals; both seams must
    still read as one continuous sentence pair."""
    prompt = chat_module.SYSTEM_PROMPT
    # The literal before the replacement ends "...do NOT attempt to bypass it. "
    # and the replacement keeps the decompose sentences that preceded the
    # rewritten opener inside the same literal.
    assert (
        "do NOT attempt to bypass it. To help the user author a plan, call "
        "decompose with their goal to get a first draft. Show the draft and ask "
        "if they want to iterate. " + STAGE_SENTENCE in prompt
    ), "the literal before the replacement must still abut it"
    # The literal after the replacement begins "You can surface decisions...".
    assert (
        HANDOFF_TAIL
        + " You can surface decisions the overlord has ruled on by calling "
        "list_decisions. " in prompt
    ), "the literal after the replacement must still abut it"


def test_the_replacement_literal_is_a_single_source_line():
    """The brief requires the replacement to stay ONE line in app/chat.py."""
    source = (Path(__file__).resolve().parents[2] / "app" / "chat.py").read_text()
    start = source.index(STAGE_SENTENCE)
    end = source.index(HANDOFF_TAIL, start) + len(HANDOFF_TAIL)
    assert "\n" not in source[start:end]


def test_prompt_still_ends_with_the_final_sentence_and_one_tools_sentence():
    prompt = chat_module.SYSTEM_PROMPT
    assert prompt.endswith(chat_module._FINAL_SENTENCE)
    assert prompt.count("Available tools: ") == 1


def test_prefix_carries_the_rewritten_plan_authoring_block():
    prefix = chat_module._SYSTEM_PROMPT_PREFIX
    assert STAGE_SENTENCE in prefix
    assert NO_INGEST_SENTENCE in prefix
    assert GATE_MESSAGE in prefix
    assert NO_FALSE_CLAIM_SENTENCE in prefix
    assert HANDOFF_TAIL in prefix


# ---------------------------------------------------------------------------
# positive: the registry marks exactly the two origin-gated tools
# ---------------------------------------------------------------------------

def test_ingest_plan_description_is_the_ui_only_text():
    assert chat_module.TOOLS["ingest_plan"]["description"] == INGEST_DESCRIPTION


def test_set_workspace_description_is_the_ui_only_text():
    assert chat_module.TOOLS["set_workspace"]["description"] == SET_WORKSPACE_DESCRIPTION


def test_both_marked_descriptions_contain_the_ui_only_marker():
    for name in sorted(CHAT_REFUSED_TOOLS):
        assert "UI-ONLY" in chat_module.TOOLS[name]["description"], name


def test_exactly_the_origin_gated_tools_are_marked_ui_only():
    # The only two refuse_chat_origin call sites in app/dashboard.py are
    # POST /api/workspace and POST /api/plans/{plan_name}/ingest; this keeps
    # the prompt's claim and the server's enforcement from drifting apart.
    marked = {
        name
        for name, entry in chat_module.TOOLS.items()
        if "UI-ONLY" in entry["description"]
    }
    assert marked == CHAT_REFUSED_TOOLS


def test_reachable_tools_are_not_marked_ui_only():
    for name in ("list_plans", "save_plan"):
        assert "UI-ONLY" not in chat_module.TOOLS[name]["description"], name


def test_the_derived_tool_sentence_carries_the_marker():
    sentence = chat_module._available_tools_sentence()
    for name in sorted(CHAT_REFUSED_TOOLS):
        assert re.search(rf"{name} \([^)]*UI-ONLY[^)]*args: ", sentence), name


def test_the_derived_tool_sentence_omits_the_marker_for_reachable_tools():
    sentence = chat_module._available_tools_sentence()
    for name in ("list_plans", "save_plan"):
        assert re.search(rf"{name} \([^)]*UI-ONLY[^)]*args: ", sentence) is None, name


def test_the_derived_tool_sentence_still_enumerates_the_live_registry():
    sentence = chat_module._available_tools_sentence()
    assert sentence.startswith("Available tools: ")
    assert sentence.endswith(" ")
    for name in chat_module.TOOLS:
        assert name in sentence, name


# ---------------------------------------------------------------------------
# negative: the old, false instruction must be gone
# ---------------------------------------------------------------------------

def test_prompt_no_longer_instructs_an_impossible_ingest():
    prompt = chat_module.SYSTEM_PROMPT
    assert "call save_plan then ingest_plan" not in prompt
    assert "Always confirm with the user before calling ingest_plan" not in prompt


def test_only_the_do_not_call_ingest_phrasing_survives():
    prompt = chat_module.SYSTEM_PROMPT
    assert prompt.count("call ingest_plan") == 1
    assert "ingest_plan then" not in prompt


def test_prefix_no_longer_instructs_an_impossible_ingest():
    prefix = chat_module._SYSTEM_PROMPT_PREFIX
    assert "call save_plan then ingest_plan" not in prefix
    assert "Always confirm with the user before calling ingest_plan" not in prefix


# ---------------------------------------------------------------------------
# boundary: the two tools stay registered with their params/execute intact
# ---------------------------------------------------------------------------

def test_marked_tools_keep_their_registry_shape():
    for name in sorted(CHAT_REFUSED_TOOLS):
        entry = chat_module.TOOLS[name]
        # No fourth key: prompt guidance lives in `description`.
        assert set(entry) == {"description", "params", "execute"}, name
        assert callable(entry["execute"]), name


def test_marked_tools_keep_their_params():
    assert chat_module.TOOLS["ingest_plan"]["params"] == {
        "plan_name": "str",
        "only_epics": "list[str] | None",
        "overwrite": "bool",
    }
    assert chat_module.TOOLS["set_workspace"]["params"] == {
        "path": "str",
        "create": "bool",
    }
