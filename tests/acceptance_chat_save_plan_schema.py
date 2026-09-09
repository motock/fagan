"""Acceptance oracle: save_plan must tell the model the plan_json contract.

Root cause this grades (2026-09-09): ``save_plan``'s description was "Save a
plan JSON to a named plan." -- it said nothing about ``plan_json`` being a
serialized JSON *string* (``SavePlanRequest.plan_json: str``), nor about the
schema ``ingest_plan`` actually reads. Handed a correct draft by ``decompose``,
the chat model re-authored it into ``{name, goal, epics[].name,
stories[].id/title/depends_on/acceptance_criteria}``: a shape ``save_plan``
happily persists (it validates only that ``"epics"`` exists) and ``ingest_plan``
then dies on a bare ``KeyError: 'summary'``.

Assertions are membership-only against the live registry and prompt, never an
exact full-string match -- SYSTEM_PROMPT is a cumulative artifact several
stories grow in turn (see .claude/rules/pipeline-story-schema.md).
"""

from app.chat import SYSTEM_PROMPT, TOOLS


def _save_plan_description() -> str:
    return TOOLS["save_plan"]["description"]


def test_description_says_plan_json_is_a_serialized_string():
    text = _save_plan_description().lower()
    assert "json string" in text, (
        "save_plan's description must state that plan_json is a serialized "
        f"JSON string, not an object; got: {_save_plan_description()!r}"
    )


def test_base_prompt_still_omits_repo_root():
    # tests/unit/test_chat_workspace_threading.py::
    # test_workspace_none_leaves_system_prompt_unchanged asserts "repo_root"
    # never appears in the BASE prompt -- the repo_root instruction is the
    # per-turn workspace sentence's job, and save_plan overwrites the
    # model-authored value with the server-validated workspace path anyway
    # (WS-11, pipeline/service.py). So the schema guidance must describe the
    # story/epic shape WITHOUT using the literal token "repo_root".
    assert "repo_root" not in SYSTEM_PROMPT
    assert "repo_root" not in _save_plan_description()


def test_description_names_the_required_story_field():
    # `summary` is the one field ingest_plan hard-requires on every epic and
    # story (pipeline/ingest.py raises a bare KeyError without it).
    assert "summary" in _save_plan_description()


def test_description_names_the_implementation_brief_field():
    assert "agent_instructions" in _save_plan_description()


def test_description_warns_off_the_invented_fields():
    text = _save_plan_description()
    for invented in ("title", "acceptance_criteria", "depends_on"):
        assert invented in text, (
            f"description must name the invented field {invented!r} the model "
            "must not emit"
        )


def test_description_still_describes_saving():
    # tests/unit/test_chat_plan_tools.py::test_descriptions_are_meaningful
    # requires the substring "save" (case-insensitive).
    assert "save" in _save_plan_description().lower()


def test_system_prompt_tells_the_model_to_pass_the_decompose_plan_through():
    assert "verbatim" in SYSTEM_PROMPT.lower(), (
        "SYSTEM_PROMPT must instruct the model to serialize decompose's own "
        "plan verbatim rather than re-authoring it"
    )


def test_system_prompt_still_ends_with_the_final_sentence():
    import app.chat as chat_module

    assert SYSTEM_PROMPT.endswith(chat_module._FINAL_SENTENCE)


def test_system_prompt_is_still_assembled_from_the_derived_sentence():
    import app.chat as chat_module

    assert SYSTEM_PROMPT == (
        chat_module._SYSTEM_PROMPT_PREFIX
        + chat_module._available_tools_sentence()
        + chat_module._FINAL_SENTENCE
    )


def test_registry_entries_keep_exactly_the_three_declared_keys():
    # The schema guidance must live in `description`; adding a fourth key
    # breaks tests/unit/test_chat_readonly_tools.py's set(entry) assertion.
    for name, entry in TOOLS.items():
        assert set(entry) == {"description", "params", "execute"}, name


def test_save_plan_params_are_unchanged():
    assert TOOLS["save_plan"]["params"] == {"plan_name": "str", "plan_json": "str"}


def test_save_plan_still_posts_the_plan_json_body_unchanged():
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
