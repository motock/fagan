"""``SqliteStore`` is self-contained: the database is the only record.

ADR 0012 decision 7. Under ``PIPELINE_STORE=sqlite`` the store serves and
writes every record from its database. It must not keep an internal file store
(``_files``), must not write the plans directory's manifest, journal or
notification files, and must not adopt a manifest a writer dropped into that
directory by hand. The one-time import tool is the only path from files to the
database.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline.persistence

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline import store as store_module
from pipeline.store_records import DEFAULT_NAMESPACE
from pipeline.store_sqlite import SqliteStore

PLAN = "self-contained-plan"
STORY = "S1"
NS = DEFAULT_NAMESPACE

# The plans directory's sidecar names that a database-backed store must never
# write. The audit sidecar (``<plan>.audit.jsonl``) is deliberately absent: it
# moves to the database in a later story.
FORBIDDEN_SUFFIXES = (".manifest.json", ".journal.json")
FORBIDDEN_INFIXES = (".notifications.",)


@pytest.fixture()
def plans_dir(tmp_path, monkeypatch):
    """An empty plans directory every file-store path would write into."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", directory)
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", directory)
    return directory


@pytest.fixture()
def sqlite_store(plans_dir, tmp_path, monkeypatch):
    """The store ``build_store`` selects under ``PIPELINE_STORE=sqlite``."""
    monkeypatch.setenv("PIPELINE_STORE", "sqlite")
    monkeypatch.setenv("PIPELINE_STORE_PATH", str(tmp_path / "store.db"))
    built = store_module.build_store()
    assert type(built) is SqliteStore
    yield built
    built.close()


def _sidecar_files(plans_dir: Path) -> list[str]:
    """Names of the plans directory's manifest, journal or notification files."""
    return sorted(
        path.name
        for path in plans_dir.rglob("*")
        if path.is_file()
        and (
            path.name.endswith(FORBIDDEN_SUFFIXES)
            or any(infix in path.name for infix in FORBIDDEN_INFIXES)
        )
    )


def test_full_cycle_writes_no_plan_sidecar_files(sqlite_store, plans_dir):
    """Ingest, dispatch state, decision, journal and notification stay in the database."""
    records = sqlite_store.records()

    with records.transaction(NS, PLAN) as acquired:
        assert acquired is True
        records.put_manifest(
            NS, PLAN, {"schema_version": 1, "stories": {STORY: {"status": "pending"}}}
        )
    with records.transaction(NS, PLAN) as acquired:
        assert acquired is True
        sqlite_store.update_story(PLAN, STORY, {"status": "running"})
        records.append_decision(NS, PLAN, {"kind": "approve", "by": "operator"})
        records.append_journal(NS, PLAN, STORY, {"step": "dispatch", "summary": "started"})
        records.append_notification(
            NS,
            PLAN,
            {"ts": "t1", "message": "started", "severity": "info", "story_key": STORY},
        )

    assert _sidecar_files(plans_dir) == [], (
        "a database-backed store must not write manifest, journal or "
        "notification files into the plans directory"
    )
    assert sqlite_store.get_manifest(PLAN)["stories"][STORY]["status"] == "running"
    assert sqlite_store.get_notifications(PLAN) == ["t1 started"]


def test_hand_written_manifest_file_is_not_adopted(sqlite_store, plans_dir):
    """A manifest dropped into the plans directory is invisible to the store.

    Only the one-time import tool may bring a file-store manifest into the
    database; a read must never adopt a file a writer left behind.
    """
    hand_written = plans_dir / f"{PLAN}.manifest.json"
    hand_written.write_text(json.dumps({"stories": {STORY: {"status": "parked"}}}))

    assert sqlite_store.get_manifest_or_none(PLAN) is None
    with pytest.raises(FileNotFoundError):
        sqlite_store.get_manifest(PLAN)
    assert PLAN not in sqlite_store.list_manifests()
    assert PLAN not in sqlite_store.list_plans()
    assert PLAN not in sqlite_store.records().list_plans(NS)
    assert sqlite_store.records().get_manifest(NS, PLAN) is None


def test_hand_written_manifest_file_is_left_untouched(sqlite_store, plans_dir):
    """Reading past a hand-written manifest must not import or rewrite it."""
    hand_written = plans_dir / f"{PLAN}.manifest.json"
    original = json.dumps({"stories": {STORY: {"status": "parked"}}})
    hand_written.write_text(original)

    sqlite_store.get_manifest_or_none(PLAN)
    sqlite_store.list_manifests()

    assert hand_written.read_text() == original
    assert sqlite_store.records().get_manifest_updated_at(NS, PLAN) is None


def test_store_has_no_internal_file_store(sqlite_store):
    """The database store holds no file store to delegate records to."""
    assert not hasattr(sqlite_store, "_files")


def test_manifest_path_touches_no_file(sqlite_store, plans_dir):
    """``manifest_path`` names no file the store would read or write."""
    with pytest.raises(NotImplementedError, match="database"):
        sqlite_store.manifest_path(PLAN)

    assert list(plans_dir.iterdir()) == []
