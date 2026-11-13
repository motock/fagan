"""The local success report reads its records through the selected store.

The same records seeded into a file store and into a sqlite store must produce
the same report, and a sqlite deployment must not need a plan directory at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pipeline.paths
import pipeline.persistence
from pipeline.store_sqlite import SqliteStore
from scripts.local_success_report import main

REPO = Path(__file__).resolve().parents[2]
NS = "default"
PLAN = "alpha"


def _manifest():
    return {
        "repo_root": str(REPO),
        "stories": {
            "s1": {
                "status": "done",
                "backend": "ollama",
                "dispatched_at": "2026-01-01T00:00:00Z",
                "dispatched_model": "m",
            },
            "s2": {
                "status": "parked",
                "backend": "ollama",
                "dispatched_at": "2026-01-02T00:00:00Z",
                "dispatched_model": "m",
            },
        },
    }


NOTIFICATIONS = [
    {"ts": "2026-01-02T01:00:00Z", "story_key": "s2", "message": "story parked: rebase conflict"},
]
JOURNAL_S1 = [{"step": "step_cap_reached", "summary": "cap"}]


def _seed_files(plan_dir: Path) -> None:
    (plan_dir / f"{PLAN}.manifest.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    (plan_dir / f"{PLAN}.notifications.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in NOTIFICATIONS), encoding="utf-8"
    )
    (plan_dir / f"{PLAN}.s1.journal.json").write_text(json.dumps(JOURNAL_S1), encoding="utf-8")


def _seed_sqlite(db_path: Path) -> None:
    records = SqliteStore(str(db_path)).records()
    records.put_manifest(NS, PLAN, _manifest())
    for record in NOTIFICATIONS:
        records.append_notification(NS, PLAN, record)
    for entry in JOURNAL_S1:
        records.append_journal(NS, PLAN, "s1", entry)


def test_file_store_report_is_unchanged_by_reading_through_records(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PIPELINE_STORE", raising=False)
    _seed_files(tmp_path)

    assert main(["--plan-dir", str(tmp_path), "--all-repos"]) == 0

    out = capsys.readouterr().out
    assert out.splitlines()[2] == "cohort: 2026-01-01T00:00:00Z .. 2026-01-02T00:00:00Z (2 stories)"
    assert "overall: 0/2 clean (0.0%)" in out
    assert "  step_cap_reached: 1" in out
    assert "  story_parked: 1" in out


def test_sqlite_store_with_the_same_records_gives_the_same_report(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PIPELINE_STORE", raising=False)
    _seed_files(tmp_path)
    assert main(["--plan-dir", str(tmp_path), "--all-repos"]) == 0
    file_out = capsys.readouterr().out

    monkeypatch.setenv("PIPELINE_STORE", "sqlite")
    monkeypatch.setenv("PIPELINE_STORE_PATH", str(tmp_path / "store.db"))
    _seed_sqlite(tmp_path / "store.db")
    missing_dir = tmp_path / "not-a-plan-dir"

    assert main(["--plan-dir", str(missing_dir), "--all-repos"]) == 0
    sqlite_out = capsys.readouterr().out

    assert sqlite_out == file_out


def test_missing_plan_dir_on_the_file_store_still_exits_two(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PIPELINE_STORE", raising=False)

    assert main(["--plan-dir", str(tmp_path / "nope")]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does not exist" in captured.err


def test_plan_dir_flag_points_the_file_store_without_touching_module_bindings(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("PIPELINE_STORE", raising=False)
    other = tmp_path / "other"
    other.mkdir()
    seeded = tmp_path / "seeded"
    seeded.mkdir()
    _seed_files(seeded)
    monkeypatch.setenv("PLAN_DIR", str(other))
    before_paths = pipeline.paths.PLAN_DIR
    before_persistence = pipeline.persistence.PLAN_DIR

    assert main(["--plan-dir", str(seeded), "--all-repos"]) == 0

    assert "overall: 0/2 clean (0.0%)" in capsys.readouterr().out
    assert pipeline.paths.PLAN_DIR == before_paths
    assert pipeline.persistence.PLAN_DIR == before_persistence


def test_sqlite_store_report_does_not_require_a_plan_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PIPELINE_STORE", "sqlite")
    monkeypatch.setenv("PIPELINE_STORE_PATH", str(tmp_path / "store.db"))
    _seed_sqlite(tmp_path / "store.db")

    assert main(["--plan-dir", str(tmp_path / "absent"), "--all-repos"]) == 0

    assert "overall: 0/2 clean (0.0%)" in capsys.readouterr().out
