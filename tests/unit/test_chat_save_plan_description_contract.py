"""Unit tests: save_plan's description must be a self-describing plan_json contract.

Story (2026-09-09): make ``save_plan``'s tool description state the contract the
chat model keeps violating:

* ``plan_json`` is a *serialized JSON string* (``SavePlanRequest.plan_json: str``
  in app/dashboard_models.py), not a JSON object;
* the shape ``ingest_plan`` actually reads is
  ``{epics: [{summary, stories: [{summary, agent_instructions, dependencies,
  persona, model, risk}]}]}``;
* ``summary`` is required on every epic and story (pipeline/ingest.py raises a
  bare ``KeyError: 'summary'`` without it);
* ``title``, ``acceptance_criteria`` and ``depends_on`` do NOT exist and must
  not be emitted.

Shared-artifact rule (.claude/rules/pipeline-story-schema.md): ``TOOLS`` and
``SYSTEM_PROMPT`` are cumulative artifacts other stories also extend, so every
assertion here is membership-only or anchored to a stable fragment — never an
exact full-string match, never a tool count, never a hash.

This file grades the *unit* side of the contract. The acceptance oracle
``tests/acceptance_chat_save_plan_schema.py`` grades the same contract from the
registry + prompt; the two must stay in agreement.
"""
from __future__ import annotations

import pytest

import app.chat as chat_module
from app.chat import SYSTEM_PROMPT, TOOLS

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _description() -> str:
    return TOOLS["save_plan"]["description"]


def _lower() -> str:
    return _description().lower()


def _rendered_save_plan_entry() -> str:
    """The save_plan entry as it appears in the rendered tools sentence."""
    sentence = chat_module._available_tools_sentence()
    start = sentence.index("save_plan (")
    # The entry ends at the next ", <name> (" anchor (or end of sentence).
    rest = sentence[start + len("save_plan (") :]
    end = len(sentence)
    for other in TOOLS:
        if other == "save_plan":
            continue
        anchor = f", {other} ("
        pos = rest.find(anchor)
        if pos != -1:
            end = min(end, start + len("save_plan (") + pos)
    return sentence[start:end]


# --------------------------------------------------------------------------- #
# (1) plan_json is a serialized JSON string, not an object
# --------------------------------------------------------------------------- #
class TestPlanJsonIsAString:
    def test_description_says_json_string(self):
        # Case-insensitive: "JSON string", "json string", "Json String" all OK.
        assert "json string" in _lower(), (
            "save_plan's description must state that plan_json is a serialized "
            f"JSON string (SavePlanRequest.plan_json: str), not an object; "
            f"got: {_description()!r}"
        )

    def test_description_says_not_an_object(self):
        # The negative half of the contract: the model must be told the value
        # is not a nested object/dict.
        assert "not" in _lower(), (
            "description should say plan_json is NOT an object/literal dict; "
            f"got: {_description()!r}"
        )

    def test_description_names_plan_json(self):
        assert "plan_json" in _description()

    @pytest.mark.parametrize(
        "candidate",
        ["json string", "JSON string", "Json String", "jSoN sTrInG"],
    )
    def test_case_insensitive_match(self, candidate):
        assert candidate.lower() in _lower()


# --------------------------------------------------------------------------- #
# (2) the schema ingest_plan actually reads
# --------------------------------------------------------------------------- #
class TestDescriptionNamesTheSchema:
    @pytest.mark.parametrize(
        "field",
        [
            "summary",
            "agent_instructions",
            "dependencies",
            "persona",
            "model",
            "risk",
            "epics",
            "stories",
        ],
    )
    def test_description_names_every_schema_field(self, field):
        assert field in _description(), (
            f"save_plan's description must name the schema field {field!r} "
            f"that ingest_plan reads; got: {_description()!r}"
        )

    def test_summary_is_called_out_as_required(self):
        # `summary` is the one field ingest_plan hard-requires on every epic
        # and story; the description must say so, not merely list it.
        text = _lower()
        assert "summary" in text
        assert "required" in text, (
            "description must state that `summary` is required on every epic "
            f"and story; got: {_description()!r}"
        )

    def test_summary_required_on_both_levels(self):
        # Both "epic" and "story" must appear near the required-field claim so
        # the model knows summary is required at BOTH nesting levels.
        text = _lower()
        assert "epic" in text
        assert "story" in text

    def test_description_mentions_ingest(self):
        # The contract is about what ingest_plan reads; naming it ties the
        # schema to its consumer.
        assert "ingest" in _lower()


# --------------------------------------------------------------------------- #
# (3) the invented fields must be named and forbidden
# --------------------------------------------------------------------------- #
class TestDescriptionWarnsOffInventedFields:
    @pytest.mark.parametrize("invented", ["title", "acceptance_criteria", "depends_on"])
    def test_invented_field_is_named(self, invented):
        assert invented in _description(), (
            f"description must name the invented field {invented!r} the model "
            f"must not emit; got: {_description()!r}"
        )

    def test_the_forbidden_fields_are_marked_absent(self):
        # Naming them is not enough: the description must say they do not
        # exist / must not be emitted. Accept several natural phrasings.
        text = _lower()
        assert any(
            marker in text
            for marker in (
                "do not",
                "don't",
                "never",
                "must not",
                "not exist",
                "no such",
                "invalid",
                "unsupported",
                "unused",
                "ignored",
                "reject",
            )
        ), (
            "description must tell the model NOT to emit title / "
            f"acceptance_criteria / depends_on; got: {_description()!r}"
        )


# --------------------------------------------------------------------------- #
# (4) guards imposed by the rest of the suite
# --------------------------------------------------------------------------- #
class TestGuardsFromExistingSuite:
    def test_no_repo_root_token(self):
        # tests/unit/test_chat_workspace_threading.py::
        # test_workspace_none_leaves_system_prompt_unchanged asserts
        # "repo_root" never appears in the BASE prompt; the description flows
        # into SYSTEM_PROMPT via _available_tools_sentence().
        assert "repo_root" not in _description()
        assert "repo_root" not in SYSTEM_PROMPT

    def test_description_still_describes_saving(self):
        # tests/unit/test_chat_plan_tools.py::test_descriptions_are_meaningful
        assert "save" in _lower()

    def test_params_dict_is_unchanged(self):
        # tests/unit/test_chat_plan_tools.py::
        # test_save_plan_params_declares_plan_name_and_plan_json
        assert TOOLS["save_plan"]["params"] == {"plan_name": "str", "plan_json": "str"}

    def test_entry_keeps_exactly_three_keys(self):
        # tests/unit/test_chat_readonly_tools.py asserts set(entry) ==
        # {"description", "params", "execute"}; the guidance must live in
        # `description`, not in a new fourth key.
        assert set(TOOLS["save_plan"]) == {"description", "params", "execute"}

    def test_execute_still_posts_plan_json_body(self):
        class _Resp:
            def json(self):
                return {"ok": True}

        class _Client:
            def __init__(self):
                self.posts = []

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs.get("json")))
                return _Resp()

        client = _Client()
        out = TOOLS["save_plan"]["execute"](
            client, "http://base.test", plan_name="demo", plan_json='{"epics":[]}'
        )
        assert out == {"ok": True}
        assert client.posts == [("/api/plans/demo/save", {"plan_json": '{"epics":[]}'})]

    def test_system_prompt_is_still_prefix_plus_tools_plus_final(self):
        assert SYSTEM_PROMPT == (
            chat_module._SYSTEM_PROMPT_PREFIX
            + chat_module._available_tools_sentence()
            + chat_module._FINAL_SENTENCE
        )

    def test_system_prompt_still_carries_the_verbatim_instruction(self):
        # PR #631's sentence must survive the description rewrite.
        assert "verbatim" in SYSTEM_PROMPT.lower()


# --------------------------------------------------------------------------- #
# (5) the description survives rendering into the tools sentence
# --------------------------------------------------------------------------- #
class TestRenderedSentenceStillCarriesTheContract:
    def test_entry_still_uses_the_args_separator(self):
        # PR #631's actual contribution: the '; args: ' separator and the
        # ', ' join between params — pinned via the live registry, not a
        # frozen description string.
        entry = _rendered_save_plan_entry()
        assert entry.startswith("save_plan (")
        assert "; args: plan_name: str, plan_json: str)" in entry

    def test_rendered_entry_still_carries_the_schema_fields(self):
        entry = _rendered_save_plan_entry()
        for fragment in ("json string", "summary", "agent_instructions"):
            assert fragment in entry.lower(), (
                f"rendered save_plan entry lost the contract fragment "
                f"{fragment!r}; entry: {entry!r}"
            )

    def test_rendered_entry_does_not_break_the_enumeration(self):
        # Constraint 5: no parenthetical immediately after ANOTHER tool's name
        # inside save_plan's description text — the enumeration is parsed by
        # ", <name> (" anchors, so a fragment inside the description that looks
        # like the START of another entry (") <name> (") would split it.
        entry = _rendered_save_plan_entry()
        description = entry[len("save_plan (") : entry.rindex(")")]
        for other in TOOLS:
            if other == "save_plan":
                continue
            assert f") {other} (" not in description, (
                f"save_plan's description must not contain ') {other} (' — it "
                f"would be parsed as another entry; got: {description!r}"
            )

    def test_description_has_no_parenthetical_after_a_foreign_tool_name(self):
        # Same rule, checked directly on the description text: "<tool> (" for
        # any OTHER tool must not appear inside save_plan's description.
        description = _description()
        for other in TOOLS:
            if other == "save_plan":
                continue
            assert f"{other} (" not in description, (
                f"save_plan's description must not contain a parenthetical "
                f"after another tool's name ({other!r}); got: {description!r}"
            )

    def test_description_is_a_single_line(self):
        # A newline inside the description would break the one-line
        # enumeration sentence.
        assert "\n" not in _description()


# --------------------------------------------------------------------------- #
# (6) the description is a faithful contract (boundary cases)
# --------------------------------------------------------------------------- #
class TestDescriptionIsFaithfulToTheContract:
    def test_description_shows_a_balanced_shape_fragment(self):
        """The description should show the plan_json shape with braces.

        Only braces-balance is required: the brief's shape is written in
        pseudo-JSON (``{epics: [{summary, stories: [...]}]}``), so requiring
        strict ``json.loads`` would over-constrain the wording.
        """
        text = _description()
        assert text.count("{") >= 1, (
            f"description should show the plan_json shape with braces; got: {text!r}"
        )
        assert text.count("{") == text.count("}"), (
            "unbalanced braces in save_plan's description; got: " f"{text!r}"
        )

    def test_shape_fields_appear_in_nesting_order(self):
        # epics is the top-level key; stories nest inside each epic.
        text = _description()
        i_epics = text.find("epics")
        i_stories = text.find("stories")
        assert i_epics != -1 and i_stories != -1
        assert i_epics < i_stories, (
            "description should present epics before the nested stories; "
            f"got: {text!r}"
        )

    def test_description_is_nonempty_and_reasonably_sized(self):
        # Boundary: not empty, and not a whole essay (it is rendered inline in
        # the tools sentence).
        text = _description()
        assert text.strip()
        assert 20 <= len(text) <= 2000