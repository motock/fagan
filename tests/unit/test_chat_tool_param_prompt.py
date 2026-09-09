"""Tests for app/chat.py — tool *parameter* rendering in the tools sentence.

Root cause being fixed (already diagnosed; do not re-derive):
``_available_tools_sentence()`` rendered ``name (description)`` per tool but
omitted ``TOOLS[name]['params']``, so the chat model guessed wrong argument
names for ``save_plan`` (e.g. inventing argument names instead of using
``plan_name`` / ``plan_json``).

This story therefore:

1. rewrites ``_available_tools_sentence()`` so each entry becomes
   ``name (desc; args: k: v, k2: v2)`` — or ``args: none`` when the tool's
   ``params`` dict is empty — keeping the ``"Available tools: "`` prefix and
   trailing space exactly; and
2. appends ONE sentence to ``_SYSTEM_PROMPT_PREFIX``'s plan-authoring sentence
   telling the model to pass the decompose result plan JSON verbatim as
   ``plan_json``.

Cumulative-artifact rule: ``SYSTEM_PROMPT`` / the tools sentence are shared
artifacts that later stories extend with more tools. These tests assert only
what THIS story adds — membership of the new fragments, and ordering relative
to fixed anchors — never the exact full sentence, never a total tool count,
never full-string equality.
"""
from __future__ import annotations

import app.chat as chat_module

# Fixed anchors that already exist and that later stories must not move.
PLAN_AUTHORING_ANCHOR = "When satisfied, call save_plan then ingest_plan."
TOOLS_SENTENCE_ANCHOR = "Available tools: "

# The exact sentence this story appends to _SYSTEM_PROMPT_PREFIX (verbatim
# from the brief, including the leading space).
PASSTHROUGH_SENTENCE = (
    " When calling save_plan, pass the decompose result plan JSON verbatim "
    "as plan_json (a JSON string) - do not rewrite, summarize, or re-derive "
    "it; keep its epics/stories fields exactly as decompose returned them."
)


def _sentence() -> str:
    return chat_module._available_tools_sentence()


# --------------------------------------------------------------------------- #
# (1) save_plan params are rendered into the tools sentence
# --------------------------------------------------------------------------- #
class TestSavePlanParamsRendered:
    def test_plan_name_param_in_sentence(self):
        assert "plan_name: str" in _sentence()

    def test_plan_json_param_in_sentence(self):
        assert "plan_json: str" in _sentence()

    def test_save_plan_entry_full_format(self):
        # The whole save_plan entry, pinning the '; args: ' separator and the
        # ', ' join between params (membership of one entry, not the sentence).
        assert (
            "save_plan (Save a plan JSON to a named plan.; "
            "args: plan_name: str, plan_json: str)"
        ) in _sentence()

    def test_old_format_without_args_is_gone(self):
        # Negative: the pre-fix rendering of save_plan (description only, no
        # args) must no longer appear.
        assert "save_plan (Save a plan JSON to a named plan.)" not in _sentence()


# --------------------------------------------------------------------------- #
# (2) empty params render as 'args: none'; boundary param counts
# --------------------------------------------------------------------------- #
class TestEmptyParamsAndBoundaries:
    def test_args_none_in_sentence(self):
        # list_plans has params == {} — the zero-param boundary.
        assert "args: none" in _sentence()

    def test_list_plans_entry_uses_args_none(self):
        assert "list_plans (List all pipeline plans.; args: none)" in _sentence()

    def test_single_param_tool(self):
        # One-param boundary: search_code has exactly {"pattern": "str"}.
        assert "pattern: str" in _sentence()

    def test_two_params_joined_with_comma(self):
        # set_workspace has {"path": "str", "create": "bool"}.
        assert "path: str, create: bool" in _sentence()

    def test_param_type_rendered_verbatim(self):
        # ingest_plan's param type contains spaces and a pipe; it must be
        # rendered verbatim, not mangled.
        assert "only_epics: list[str] | None" in _sentence()


# --------------------------------------------------------------------------- #
# (3) sentence shape: prefix, trailing space, registry-derived names
# --------------------------------------------------------------------------- #
class TestSentenceShape:
    def test_starts_with_available_tools_prefix(self):
        assert _sentence().startswith(TOOLS_SENTENCE_ANCHOR)

    def test_keeps_trailing_space(self):
        assert _sentence().endswith(" ")

    def test_every_registered_tool_is_named(self):
        # Drift prevention: the sentence is derived from the live registry.
        sentence = _sentence()
        for name in chat_module.TOOLS:
            assert name in sentence, f"registered tool {name!r} missing from sentence"

    def test_docstring_keeps_drift_rationale_and_mentions_params(self):
        doc = chat_module._available_tools_sentence.__doc__ or ""
        assert "drift" in doc.lower()
        assert "params" in doc.lower()


# --------------------------------------------------------------------------- #
# Dynamic registry: tools registered after import time render too
# --------------------------------------------------------------------------- #
class TestDynamicRegistryEntries:
    def test_tool_added_after_import_renders_params(self, monkeypatch):
        monkeypatch.setitem(
            chat_module.TOOLS,
            "zz_probe_tool",
            {
                "description": "Probe tool.",
                "params": {"alpha": "str", "beta": "int"},
                "execute": lambda *a, **k: {},
            },
        )
        assert "zz_probe_tool (Probe tool.; args: alpha: str, beta: int)" in _sentence()

    def test_tool_added_after_import_empty_params(self, monkeypatch):
        monkeypatch.setitem(
            chat_module.TOOLS,
            "zz_empty_probe_tool",
            {
                "description": "Empty probe.",
                "params": {},
                "execute": lambda *a, **k: {},
            },
        )
        assert "zz_empty_probe_tool (Empty probe.; args: none)" in _sentence()


# --------------------------------------------------------------------------- #
# (4) SYSTEM_PROMPT gains the verbatim-passthrough guidance
# --------------------------------------------------------------------------- #
class TestSystemPromptPassthroughGuidance:
    def test_anchor_phrase_in_system_prompt(self):
        assert "pass the decompose result" in chat_module.SYSTEM_PROMPT

    def test_full_appended_sentence_in_prefix(self):
        assert PASSTHROUGH_SENTENCE in chat_module._SYSTEM_PROMPT_PREFIX

    def test_appended_after_existing_plan_authoring_sentence(self):
        prefix = chat_module._SYSTEM_PROMPT_PREFIX
        assert PLAN_AUTHORING_ANCHOR in prefix
        assert prefix.index(PLAN_AUTHORING_ANCHOR) < prefix.index(
            "pass the decompose result"
        )

    def test_guidance_sits_before_tools_sentence(self):
        # The append must land in the prefix, not after the tools sentence.
        prompt = chat_module.SYSTEM_PROMPT
        assert prompt.index("pass the decompose result") < prompt.index(
            TOOLS_SENTENCE_ANCHOR
        )

    def test_existing_plan_authoring_sentences_survive(self):
        prefix = chat_module._SYSTEM_PROMPT_PREFIX
        assert "To help the user author a plan, call decompose with their goal" in prefix
        assert "Always confirm with the user before calling ingest_plan" in prefix

    def test_system_prompt_still_ends_with_final_sentence(self):
        assert chat_module.SYSTEM_PROMPT.endswith(chat_module._FINAL_SENTENCE)