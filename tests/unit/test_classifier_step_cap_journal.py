"""M56-1: a step_cap_reached journal entry makes a story not first-pass clean."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pipeline import persistence, plan_completion, server
from pipeline.local_success import classify_story
from pipeline.plan_summary import format_plan_summary
from pipeline.store import build_store

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


def _read_through_store(tmp_path, read) -> object:
    """Run ``read`` against the record view of the file store rooted at tmp_path."""
    with local_success_report._PlanDirBinding(tmp_path):
        return read(build_store().records())


def _classify_with_journal_file(tmp_path, content: str | None) -> list[dict]:
    (tmp_path / "p.manifest.json").write_text(json.dumps({"stories": {KEY: _story()}}))
    if content is not None:
        (tmp_path / f"p.{KEY}.journal.json").write_text(content)
    manifest = {"stories": {KEY: _story()}}
    return _read_through_store(
        tmp_path, lambda records: local_success_report._classify_plan(records, "p", [], manifest)
    )


def test_report_reads_step_cap_entry_from_journal_file(tmp_path) -> None:
    classified = _classify_with_journal_file(tmp_path, json.dumps([CAP]))

    assert classified[0]["reasons"] == ["step_cap_reached"]


def test_report_missing_journal_file_stays_clean(tmp_path) -> None:
    assert _classify_with_journal_file(tmp_path, None)[0]["clean"] is True


def test_report_malformed_journal_json_stays_clean(tmp_path) -> None:
    assert _classify_with_journal_file(tmp_path, "{not json")[0]["clean"] is True


def test_report_top_level_dict_journal_stays_clean(tmp_path) -> None:
    assert _classify_with_journal_file(tmp_path, json.dumps(CAP))[0]["clean"] is True


def test_report_drops_non_dict_journal_entries(tmp_path) -> None:
    classified = _classify_with_journal_file(tmp_path, json.dumps([CAP, "x", None, 4]))

    assert classified[0]["reasons"] == ["step_cap_reached"]


def _load_journal_through_store(tmp_path) -> list[dict]:
    return _read_through_store(
        tmp_path, lambda records: local_success_report._load_journal(records, "p", KEY)
    )


def test_load_journal_missing_file_returns_empty_list(tmp_path) -> None:
    assert _load_journal_through_store(tmp_path) == []


def test_load_journal_malformed_json_returns_empty_list(tmp_path) -> None:
    (tmp_path / f"p.{KEY}.journal.json").write_text("{not json")

    assert _load_journal_through_store(tmp_path) == []


def test_load_journal_top_level_dict_returns_empty_list(tmp_path) -> None:
    (tmp_path / f"p.{KEY}.journal.json").write_text(json.dumps(CAP))

    assert _load_journal_through_store(tmp_path) == []


def test_load_journal_drops_non_dict_entries(tmp_path) -> None:
    (tmp_path / f"p.{KEY}.journal.json").write_text(json.dumps([CAP, "x", None, 4]))

    assert _load_journal_through_store(tmp_path) == [CAP]


def test_report_marks_story_with_step_cap_journal_not_clean(tmp_path) -> None:
    manifest = {"stories": {KEY: _story()}}
    (tmp_path / "p.manifest.json").write_text(json.dumps(manifest))
    (tmp_path / f"p.{KEY}.journal.json").write_text(json.dumps([CAP]))

    classified = _read_through_store(
        tmp_path, lambda records: local_success_report._classify_plan(records, "p", [], manifest)
    )

    assert [(c["clean"], c["reasons"]) for c in classified] == [(False, ["step_cap_reached"])]


def test_plan_summary_marks_story_with_loaded_journal_not_clean() -> None:
    manifest = {"stories": {KEY: _story(journal=[CAP])}}

    out = format_plan_summary("p", manifest, [])

    assert "not first-pass clean: step_cap_reached" in out


def _complete_plan(tmp_path, monkeypatch, journal: str | None) -> str:
    # The journals are read through the record store now, and the record view
    # resolves a journal from the *writer's* directory (``pipeline.persistence``
    # own ``PLAN_DIR`` binding) rather than from the reader module's symbol, so
    # that binding is the one that has to point at tmp_path for the journal
    # written below to be the one read back.
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    # The completion marker and the report are records of the store now, so
    # the plans directory the store resolves them against is the server
    # module's binding.
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    # The module-level binding is patched only while it still exists, so no
    # run ever writes into the real plans directory.
    monkeypatch.setattr(plan_completion, "PLAN_DIR", tmp_path, raising=False)
    monkeypatch.setattr(plan_completion, "_notify_user", lambda *a, **k: None)
    if journal is not None:
        (tmp_path / f"p.{KEY}.journal.json").write_text(journal)
    manifest = {"stories": {KEY: _story()}}
    assert plan_completion.notify_if_plan_completed("p", manifest) is True
    assert "journal" not in manifest["stories"][KEY]
    return (tmp_path / "p.report.md").read_text()


def test_plan_completion_report_includes_step_cap_from_journal_file(tmp_path, monkeypatch) -> None:
    report = _complete_plan(tmp_path, monkeypatch, json.dumps([CAP]))

    assert "step_cap_reached" in report


def test_plan_completion_report_without_journal_file_has_no_step_cap(tmp_path, monkeypatch) -> None:
    report = _complete_plan(tmp_path, monkeypatch, None)

    assert "step_cap_reached" not in report


def test_plan_completion_report_ignores_malformed_journal(tmp_path, monkeypatch) -> None:
    report = _complete_plan(tmp_path, monkeypatch, "{not json")

    assert "step_cap_reached" not in report


def test_plan_completion_report_ignores_top_level_dict_journal(tmp_path, monkeypatch) -> None:
    report = _complete_plan(tmp_path, monkeypatch, json.dumps(CAP))

    assert "step_cap_reached" not in report


def test_plan_completion_report_drops_non_dict_journal_entries(tmp_path, monkeypatch) -> None:
    report = _complete_plan(tmp_path, monkeypatch, json.dumps([CAP, "x", None, 4]))

    assert "step_cap_reached" in report


def test_plan_completion_report_with_only_non_dict_entries_stays_clean(tmp_path, monkeypatch) -> None:
    report = _complete_plan(tmp_path, monkeypatch, json.dumps(["x", None, 4]))

    assert "step_cap_reached" not in report
