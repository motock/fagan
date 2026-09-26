"""Tool docstrings and the story-schema doc name every field the code accepts.

``patch_story`` edits ``_PATCHABLE_STORY_FIELDS``; re-ingest refreshes
``_INGEST_AUTHORED_STORY_FIELDS`` (including ``files``, which ``patch_story``
cannot edit). The docs drifted from both sets; these tests fail whenever a
field is added to a set without being documented.
"""

from pathlib import Path

import pytest

from pipeline import server, service

_REPO = Path(__file__).resolve().parents[2]
_SCHEMA_DOC = (_REPO / ".claude/rules/pipeline-story-schema.md").read_text()


@pytest.mark.parametrize("field", sorted(service._PATCHABLE_STORY_FIELDS))
def test_patch_story_docstring_names_every_patchable_field(field):
    assert field in server.patch_story.__doc__


@pytest.mark.parametrize("field", sorted(server._INGEST_AUTHORED_STORY_FIELDS))
def test_ingest_plan_docstring_names_every_refreshed_field(field):
    assert field in server.ingest_plan.__doc__


@pytest.mark.parametrize("field", sorted(server._INGEST_AUTHORED_STORY_FIELDS))
def test_schema_doc_has_a_code_span_for_every_authored_field(field):
    assert f"`{field}`" in _SCHEMA_DOC


def test_patch_story_docstring_says_files_is_not_patchable():
    assert "files" in server.patch_story.__doc__


def test_patch_story_docstring_points_at_ingest_plan_for_files():
    assert "ingest_plan" in server.patch_story.__doc__


def test_server_module_stays_within_the_line_cap():
    lines = (_REPO / "pipeline/server.py").read_text().splitlines()

    assert len(lines) <= 1000
