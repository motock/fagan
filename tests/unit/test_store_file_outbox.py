"""The file store's spool reader lives in :mod:`pipeline.store_file_outbox`.

The read-only summaries over the spool files are file store implementation.
They take the directory they read as a parameter, and ``pipeline.outbox_health``
keeps its public names by re-exporting them, so the health payload and the
per-plan view are unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline import outbox_health, store_file_outbox


def _write(path: Path, *timestamps: str) -> None:
    path.write_text("".join(json.dumps({"ts": ts}) + "\n" for ts in timestamps), encoding="utf-8")


def test_should_summarise_every_spool_file_in_the_given_directory(tmp_path: Path) -> None:
    _write(tmp_path / "alpha.outbox.jsonl", "2020-01-01T00:00:00+00:00")
    _write(tmp_path / "beta.outbox.slack.jsonl", "2020-01-02T00:00:00+00:00", "not-a-date")
    summary = store_file_outbox.outbox_summary(tmp_path)
    assert summary["files"] == 2
    assert summary["records"] == 3
    assert summary["oldest_age_s"] > 0


def test_should_count_only_the_named_plans_spool_files(tmp_path: Path) -> None:
    _write(tmp_path / "alpha.outbox.jsonl", "2020-01-01T00:00:00+00:00")
    _write(tmp_path / "alpha.outbox.slack.jsonl", "2020-01-01T00:00:00+00:00")
    _write(tmp_path / "alphabet.outbox.jsonl", "2020-01-01T00:00:00+00:00")
    summary = store_file_outbox.outbox_summary_for_plan(tmp_path, "alpha")
    assert summary["files"] == 2
    assert summary["records"] == 2


def test_should_return_the_empty_summary_for_an_invalid_plan_name(tmp_path: Path) -> None:
    _write(tmp_path / "alpha.outbox.jsonl", "2020-01-01T00:00:00+00:00")
    assert store_file_outbox.outbox_summary_for_plan(tmp_path, "bad/name") == {
        "files": 0,
        "records": 0,
        "oldest_age_s": None,
    }


def test_should_return_the_empty_summary_for_a_missing_directory(tmp_path: Path) -> None:
    assert store_file_outbox.outbox_summary(tmp_path / "missing") == {
        "files": 0,
        "records": 0,
        "oldest_age_s": None,
    }


def test_should_keep_the_health_module_names_pointing_at_the_file_store_reader() -> None:
    assert outbox_health.outbox_summary is store_file_outbox.outbox_summary
    assert outbox_health.outbox_summary_for_plan is store_file_outbox.outbox_summary_for_plan
