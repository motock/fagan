"""M56-1: a step_cap_reached journal entry makes a story not first-pass clean."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pipeline.local_success import classify_story, load_journal
from pipeline.plan_summary import format_plan_summary

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import local_success_report

KEY = "S-1"
CAP = {"step": "step_cap_reached", "summary": "cap"}


def _story(**extra) -> dict:
    story = {"status": "done", "backend": "ollama", "dispatched_at": "2026-10-01T00:00:00+00:00"}
    story.update(extra)
    return story


def test_should_mark_step_cap_journal_entry_not_clean() -> None:
    result = classify_story(KEY, _story(), [], journal=[CAP])

    assert result["clean"] is False
    assert result["reasons"] == ["step_cap_reached"]


def test_should_report_both_reasons_sorted_when_journal_and_rebrief_event() -> None:
    records = [{"story_key": KEY, "event": "step_cap_rebrief"}]

    result = classify_story(KEY, _story(), records, journal=[CAP])

    assert result["reasons"] == ["step_cap_reached", "step_cap_rebrief"]


@pytest.mark.parametrize("journal", [None, [], [{"step": "interrupted"}], ["step_cap_reached", None, 3]])
def test_should_stay_clean_without_a_step_cap_journal_entry(journal) -> None:
    result = classify_story(KEY, _story(), [], journal=journal)

    assert result["clean"] is True
    assert result["reasons"] == []


def test_should_not_match_step_name_with_suffix() -> None:
    result = classify_story(KEY, _story(), [], journal=[{"step": "step_cap_reached_x"}])

    assert result["clean"] is True


def test_load_journal_returns_entries(tmp_path) -> None:
    (tmp_path / f"p.{KEY}.journal.json").write_text(json.dumps([CAP]))

    assert load_journal(tmp_path, "p", KEY) == [CAP]


def test_load_journal_missing_file_is_empty(tmp_path) -> None:
    assert load_journal(tmp_path, "p", KEY) == []


def test_load_journal_malformed_json_is_empty(tmp_path) -> None:
    (tmp_path / f"p.{KEY}.journal.json").write_text("{not json")

    assert load_journal(tmp_path, "p", KEY) == []


def test_load_journal_top_level_dict_is_empty(tmp_path) -> None:
    (tmp_path / f"p.{KEY}.journal.json").write_text(json.dumps(CAP))

    assert load_journal(tmp_path, "p", KEY) == []


def test_load_journal_drops_non_dict_entries(tmp_path) -> None:
    (tmp_path / f"p.{KEY}.journal.json").write_text(json.dumps([CAP, "x", None, 4]))

    assert load_journal(tmp_path, "p", KEY) == [CAP]


def test_report_marks_story_with_step_cap_journal_not_clean(tmp_path) -> None:
    manifest = {"stories": {KEY: _story()}}
    (tmp_path / "p.manifest.json").write_text(json.dumps(manifest))
    (tmp_path / f"p.{KEY}.journal.json").write_text(json.dumps([CAP]))

    classified = local_success_report._classify_plan(tmp_path, "p", [])

    assert [(c["clean"], c["reasons"]) for c in classified] == [(False, ["step_cap_reached"])]


def test_plan_summary_marks_story_with_loaded_journal_not_clean() -> None:
    manifest = {"stories": {KEY: _story(journal=[CAP])}}

    out = format_plan_summary("p", manifest, [])

    assert "not first-pass clean: step_cap_reached" in out
