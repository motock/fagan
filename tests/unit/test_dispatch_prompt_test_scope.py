"""Dispatch prompt test-scope paragraph (test_scope=scoped) and full-mode pin.

A scoped story must be told which tests to run instead of the full suite;
a full-mode story (no resolution, or any non-scoped value) must produce a
prompt byte-for-byte identical to the pre-change prompt.
"""
from pathlib import Path

import pytest

from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import agents_dir  # noqa: F401

JOURNAL = [
    {"step": "step-1", "summary": "Wrote the parser", "next_hint": "add validation"},
]
ACCEPTANCE = [{"path": "tests/acceptance_x.py", "source": "def test_x(): pass\n"}]
FAILED_LINE = "FAILED tests/unit/test_y.py::test_z - assert False"
SCOPE_HEADER = "Test scope: this story runs with test_scope=scoped."


def _story(**extra):
    story = {
        "summary": "Scoped prompt story",
        "agent_instructions": "Add the scoped prompt paragraph.",
        "persona": "software-engineer",
    }
    story.update(extra)
    return story


def _prompt(story, journal=None, feedback=None):
    return p._build_dispatch_command(
        story, "TSC-1", resume_journal=journal, review_feedback=feedback,
    )["prompt"]


def _scope_paragraph(prompt):
    start = prompt.index(SCOPE_HEADER)
    return prompt[start:prompt.index("\n\n", start)]


@pytest.mark.parametrize("journal", [None, JOURNAL], ids=["fresh", "resume"])
@pytest.mark.usefixtures("agents_dir")
def test_full_mode_prompt_without_resolution_is_byte_identical_to_explicit_full(
    journal,
):
    plain = _prompt(_story(), journal)
    explicit = _prompt(_story(test_scope_resolution={"test_scope": "full"}), journal)
    assert plain == explicit


@pytest.mark.parametrize("journal", [None, JOURNAL], ids=["fresh", "resume"])
@pytest.mark.usefixtures("agents_dir")
def test_unknown_test_scope_value_produces_full_mode_prompt(journal):
    plain = _prompt(_story(), journal)
    bogus = _prompt(_story(test_scope_resolution={"test_scope": "bogus"}), journal)
    assert plain == bogus


@pytest.mark.parametrize("journal", [None, JOURNAL], ids=["fresh", "resume"])
@pytest.mark.usefixtures("agents_dir")
def test_full_mode_prompt_has_no_test_scope_paragraph(journal):
    prompt = _prompt(_story(test_scope_resolution={"test_scope": "full"}), journal)
    assert SCOPE_HEADER not in prompt


@pytest.mark.usefixtures("agents_dir")
def test_scoped_prompt_names_acceptance_paths():
    story = _story(
        acceptance=ACCEPTANCE,
        test_scope_resolution={"test_scope": "scoped"},
    )
    prompt = _prompt(story)
    assert SCOPE_HEADER in prompt
    assert "`tests/acceptance_x.py`" in prompt


@pytest.mark.usefixtures("agents_dir")
def test_scoped_paragraph_sits_after_brief_and_before_remote_state_rule():
    story = _story(
        acceptance=ACCEPTANCE,
        test_scope_resolution={"test_scope": "scoped"},
    )
    prompt = _prompt(story)
    brief_end = prompt.index("Add the scoped prompt paragraph.")
    scope_at = prompt.index(SCOPE_HEADER)
    owns_at = prompt.index("The pipeline owns this branch's remote state")
    assert brief_end < scope_at < owns_at


@pytest.mark.usefixtures("agents_dir")
def test_scoped_resume_prompt_also_carries_scope_paragraph():
    story = _story(
        acceptance=ACCEPTANCE,
        test_scope_resolution={"test_scope": "scoped"},
    )
    prompt = _prompt(story, JOURNAL)
    assert SCOPE_HEADER in prompt
    assert prompt.index("Add the scoped prompt paragraph.") < prompt.index(SCOPE_HEADER)


@pytest.mark.usefixtures("agents_dir")
def test_scoped_ci_rework_names_failed_node_id_in_backticks():
    story = _story(
        ci_rework=True,
        review_feedback=FAILED_LINE,
        test_scope_resolution={"test_scope": "scoped"},
    )
    prompt = _prompt(story, feedback=FAILED_LINE)
    assert "`tests/unit/test_y.py::test_z`" in _scope_paragraph(prompt)


@pytest.mark.usefixtures("agents_dir")
def test_scoped_without_ci_rework_does_not_name_failed_node_id():
    story = _story(
        review_feedback=FAILED_LINE,
        test_scope_resolution={"test_scope": "scoped"},
    )
    prompt = _prompt(story, feedback=FAILED_LINE)
    assert "tests/unit/test_y.py::test_z" not in _scope_paragraph(prompt)


@pytest.mark.usefixtures("agents_dir")
def test_scoped_with_no_acceptance_and_no_ci_ids_uses_default_target():
    story = _story(test_scope_resolution={"test_scope": "scoped"})
    prompt = _prompt(story)
    assert prompt.count("Run only: the test files you add or change.") == 1


@pytest.mark.usefixtures("agents_dir")
def test_scoped_default_target_omits_plus_clause():
    story = _story(test_scope_resolution={"test_scope": "scoped"})
    prompt = _prompt(story)
    assert "Run only: the test files you add or change. Run lint before committing." in prompt


@pytest.mark.usefixtures("agents_dir")
def test_scoped_paragraph_does_not_echo_path_traversal_from_review_feedback():
    malicious = "FAILED ../../etc/passwd.py::x - y"
    story = _story(
        ci_rework=True,
        review_feedback=malicious,
        test_scope_resolution={"test_scope": "scoped"},
    )
    prompt = _prompt(story, feedback=malicious)
    assert "passwd" not in _scope_paragraph(prompt)


def test_software_engineer_persona_says_test_scope_overrides_full_suite():
    content = (Path(__file__).parent.parent.parent / "agents" / "software-engineer.md").read_text()
    assert '"Test scope" paragraph, which replaces the full-suite run' in content
