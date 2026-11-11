"""Ingest warning: a brief's ``Docs`` section edits a doc the story omits.

A story whose ``agent_instructions`` says it edits ``REFERENCE.md`` but whose
``files`` list names only production code is sent back by the scope gate
(``pipeline/scope_gate.py``) after the agent has done the work.  The ingest
check in ``pipeline/scope_headroom.py`` must warn about that mismatch while
the plan is still being authored.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

import pipeline.server as p
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline.scope_headroom import scope_headroom_warning
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _isolate_usage_state,
)

PREFLIGHT = "Preflight: test fixture - not a real plan\n"

REFERENCE_MD = Path(__file__).resolve().parents[2] / "REFERENCE.md"


def _story(instructions: str, files: list[str] | None = None) -> dict:
    story = {"summary": "s", "agent_instructions": instructions}
    if files is not None:
        story["files"] = files
    return story


# --------------------------------------------------------------------------
# positive: the warning names every doc the brief edits but `files` omits
# --------------------------------------------------------------------------


def test_should_warn_naming_both_omitted_docs(tmp_path):
    story = _story(
        "Do the thing.\nDocs: REFERENCE.md gains a row, CHANGELOG.md an entry\n",
        files=["pipeline/a.py"],
    )

    warning = scope_headroom_warning(story, str(tmp_path))

    assert warning is not None
    assert "REFERENCE.md" in warning
    assert "CHANGELOG.md" in warning


def test_should_warn_for_doc_named_in_parenthesised_docs_segment(tmp_path):
    story = _story(
        "Do the thing.\n"
        "Docs (pin each with an assertion): docs/specs/PLAN_APPROVAL.md section 3 "
        "records the new gate.\n",
        files=["pipeline/a.py"],
    )

    warning = scope_headroom_warning(story, str(tmp_path))

    assert warning is not None
    assert "docs/specs/PLAN_APPROVAL.md" in warning


def test_should_warn_for_a_repo_relative_docs_dir_path(tmp_path):
    story = _story(
        "Do the thing.\nDocs: docs/specs/PLAN_APPROVAL.md is updated.\n",
        files=["pipeline/a.py"],
    )

    warning = scope_headroom_warning(story, str(tmp_path))

    assert warning is not None
    assert "docs/specs/PLAN_APPROVAL.md" in warning


def test_should_join_headroom_and_docs_warnings_in_one_string(tmp_path):
    (tmp_path / "pipeline").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pipeline" / "big.py").write_text("x = 1\n" * 999)
    story = _story(
        "Do the thing.\nDocs: REFERENCE.md gains a row\n",
        files=["pipeline/big.py"],
    )

    warning = scope_headroom_warning(story, str(tmp_path))

    assert warning is not None
    assert "pipeline/big.py" in warning
    assert "headroom 1" in warning
    assert "REFERENCE.md" in warning
    # the two halves live in one string, joined with "; "
    headroom_at = warning.index("headroom 1")
    docs_at = warning.index("REFERENCE.md")
    assert headroom_at < docs_at
    assert "; " in warning[headroom_at:docs_at]


def test_should_keep_the_headroom_text_byte_for_byte(tmp_path):
    (tmp_path / "pipeline").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pipeline" / "big.py").write_text("x = 1\n" * 999)

    warning = scope_headroom_warning({"files": ["pipeline/big.py"]}, str(tmp_path))

    assert warning == (
        "declared files near the 1000-line limit: pipeline/big.py "
        "(999 lines, headroom 1); declare the sibling module the new code "
        "will move to, or split the story"
    )


def test_docs_warning_names_the_scope_gate_parking_the_story(tmp_path):
    story = _story(
        "Do the thing.\nDocs: REFERENCE.md gains a row\n",
        files=["pipeline/a.py"],
    )

    warning = scope_headroom_warning(story, str(tmp_path))

    assert warning is not None
    assert re.search(r"scope[- ]?gate|park", warning, re.IGNORECASE)
    # the remedy points at the story's `files` list
    assert "files" in warning


def test_should_name_each_omitted_doc_once(tmp_path):
    story = _story(
        "Do the thing.\n"
        "Docs: REFERENCE.md gains a row.\n"
        "Docs: REFERENCE.md also loses a stale row, CHANGELOG.md an entry.\n",
        files=["pipeline/a.py"],
    )

    warning = scope_headroom_warning(story, str(tmp_path))

    assert warning is not None
    assert warning.count("REFERENCE.md") == 1
    assert "CHANGELOG.md" in warning


def test_helper_should_return_sorted_deduplicated_paths_not_in_files(tmp_path):
    from pipeline.scope_headroom import _brief_docs_outside_files

    story = _story(
        "Do the thing.\n"
        "Docs: REFERENCE.md gains a row, CHANGELOG.md an entry, REFERENCE.md again\n",
        files=["pipeline/a.py", "CHANGELOG.md"],
    )

    omitted = _brief_docs_outside_files(story)

    assert omitted == ["REFERENCE.md"]


def test_helper_should_return_the_named_docs_sorted(tmp_path):
    from pipeline.scope_headroom import _brief_docs_outside_files

    story = _story(
        "Do the thing.\nDocs: REFERENCE.md gains a row, CHANGELOG.md an entry\n",
        files=["pipeline/a.py"],
    )

    omitted = _brief_docs_outside_files(story)

    assert omitted == ["CHANGELOG.md", "REFERENCE.md"]


@pytest.mark.parametrize("files", [None, []])
def test_helper_should_return_empty_list_without_declared_files(tmp_path, files):
    from pipeline.scope_headroom import _brief_docs_outside_files

    story = _story("Docs: REFERENCE.md gains a row")
    if files is not None:
        story["files"] = files

    assert _brief_docs_outside_files(story) == []


# --------------------------------------------------------------------------
# negative / boundary
# --------------------------------------------------------------------------


def test_should_not_warn_when_every_named_doc_is_declared(tmp_path):
    story = _story(
        "Do the thing.\nDocs: REFERENCE.md gains a row, CHANGELOG.md an entry\n",
        files=["pipeline/a.py", "REFERENCE.md", "CHANGELOG.md"],
    )

    assert scope_headroom_warning(story, str(tmp_path)) is None


def test_should_not_warn_without_a_docs_segment(tmp_path):
    story = _story(
        "Do the thing. Edit pipeline/a.py and mention REFERENCE.md in passing.\n",
        files=["pipeline/a.py"],
    )

    assert scope_headroom_warning(story, str(tmp_path)) is None


def test_should_not_warn_for_a_doc_named_outside_a_docs_segment(tmp_path):
    story = _story(
        "Do the thing.\n"
        "Risk: the ADR 0007 rollback (ADR 0007, docs/specs/PLAN_APPROVAL.md) may "
        "confuse the reviewer.\n",
        files=["pipeline/a.py"],
    )

    assert scope_headroom_warning(story, str(tmp_path)) is None


def test_should_not_warn_when_files_is_absent(tmp_path):
    story = _story("Do the thing.\nDocs: REFERENCE.md gains a row\n")

    assert scope_headroom_warning(story, str(tmp_path)) is None


def test_should_not_warn_when_files_is_empty(tmp_path):
    story = _story(
        "Do the thing.\nDocs: REFERENCE.md gains a row\n",
        files=[],
    )

    assert scope_headroom_warning(story, str(tmp_path)) is None


def test_should_ignore_a_test_docs_path_named_in_the_docs_segment(tmp_path):
    story = _story(
        "Do the thing.\nDocs: docs/tests/plan.md gains a row\n",
        files=["pipeline/a.py"],
    )

    assert scope_headroom_warning(story, str(tmp_path)) is None


@pytest.mark.parametrize("instructions", [None, 42, ["Docs: REFERENCE.md"], {"a": 1}])
def test_should_return_none_without_raising_on_malformed_instructions(
    tmp_path, instructions
):
    story = {"summary": "s", "files": ["pipeline/a.py"], "agent_instructions": instructions}

    assert scope_headroom_warning(story, str(tmp_path)) is None


def test_should_return_none_when_agent_instructions_is_missing(tmp_path):
    story = {"summary": "s", "files": ["pipeline/a.py"]}

    assert scope_headroom_warning(story, str(tmp_path)) is None


def test_should_not_match_a_path_more_than_800_chars_into_the_segment(tmp_path):
    filler = "x" * 900
    story = _story(
        f"Do the thing.\nDocs: {filler} then REFERENCE.md gains a row\n",
        files=["pipeline/a.py"],
    )

    assert scope_headroom_warning(story, str(tmp_path)) is None


def test_should_match_a_path_inside_the_800_char_window(tmp_path):
    filler = "x" * 700
    story = _story(
        f"Do the thing.\nDocs: {filler} then REFERENCE.md gains a row\n",
        files=["pipeline/a.py"],
    )

    warning = scope_headroom_warning(story, str(tmp_path))

    assert warning is not None
    assert "REFERENCE.md" in warning


def test_should_not_warn_for_malformed_files_value(tmp_path):
    story = {
        "summary": "s",
        "files": "pipeline/a.py",
        "agent_instructions": "Docs: REFERENCE.md gains a row\n",
    }

    assert scope_headroom_warning(story, str(tmp_path)) is None


def test_should_ignore_a_lowercase_docs_header(tmp_path):
    story = _story(
        "Do the thing.\ndocs: REFERENCE.md gains a row\n",
        files=["pipeline/a.py"],
    )

    assert scope_headroom_warning(story, str(tmp_path)) is None


def test_should_ignore_a_docs_header_longer_than_40_characters(tmp_path):
    header = "Docs " + ("x" * 60) + ":"
    story = _story(
        f"Do the thing.\n{header} REFERENCE.md gains a row\n",
        files=["pipeline/a.py"],
    )

    assert scope_headroom_warning(story, str(tmp_path)) is None


def test_should_warn_for_a_short_docs_header(tmp_path):
    header = "Docs " + ("x" * 30) + ":"
    story = _story(
        f"Do the thing.\n{header} REFERENCE.md gains a row\n",
        files=["pipeline/a.py"],
    )

    warning = scope_headroom_warning(story, str(tmp_path))

    assert warning is not None
    assert "REFERENCE.md" in warning


# --------------------------------------------------------------------------
# the check is advisory: it must never raise and keeps its debug log
# --------------------------------------------------------------------------


class _ExplodingStory(dict):
    """A story whose ``get`` blows up, to exercise the advisory except path."""

    def get(self, *args, **kwargs):
        raise RuntimeError("boom")


def test_should_never_raise_on_a_broken_story(tmp_path):
    assert scope_headroom_warning(_ExplodingStory(), str(tmp_path)) is None


def test_should_log_the_failed_check_at_debug_level(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="pipeline")

    scope_headroom_warning(_ExplodingStory(), str(tmp_path))

    debug_records = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and r.name.startswith("pipeline")
    ]
    assert debug_records, "the advisory check must log a debug record when it fails"


# --------------------------------------------------------------------------
# docs: REFERENCE.md mentions the new ingest warning
# --------------------------------------------------------------------------


def test_reference_documents_the_docs_ingest_warning():
    text = REFERENCE_MD.read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", text)
    sentences = re.split(r"(?<=[.!?]) ", flat)

    hits = [
        s
        for s in sentences
        if re.search(
            r"warn|flag|advisor|notic|notice|nudge|remind|scope gate|scope_gate|park",
            s,
            re.IGNORECASE,
        )
        and "files" in s
        and re.search(r"\bdocs?\b", s, re.IGNORECASE)
    ]

    assert hits, (
        "REFERENCE.md must document the ingest warning that fires when a "
        "story's brief edits a doc its `files` list omits"
    )


# --------------------------------------------------------------------------
# ingest wiring: the existing call site notifies with the returned string
# --------------------------------------------------------------------------


@pytest.fixture
def headroom_plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


def test_ingest_notifies_with_the_docs_warning(headroom_plan_dir, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: calls.append((a, k)))
    repo = tmp_path / "repo"
    (repo / "pipeline").mkdir(parents=True, exist_ok=True)
    (repo / "pipeline" / "a.py").write_text("x = 1\n")
    story = {
        "key": "DOCS-1",
        "summary": "edit a doc the brief names",
        "agent_instructions": PREFLIGHT + "Do it.\nDocs: REFERENCE.md gains a row\n",
        "files": ["pipeline/a.py"],
    }
    plan = {"repo_root": str(repo), "epics": [{"summary": "E1", "stories": [story]}]}
    (headroom_plan_dir / "docs1.json").write_text(json.dumps(plan))

    result = p.ingest_plan("docs1")

    assert result["ok"] is True
    scoped = [(a, k) for a, k in calls if k.get("event") == "scope_headroom"]
    assert len(scoped) == 1
    assert scoped[0][1]["story_key"] == "DOCS-1"
    assert "REFERENCE.md" in scoped[0][0][1]
