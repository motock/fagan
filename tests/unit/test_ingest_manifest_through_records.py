"""Ingest reads and writes the manifest through the record store (ADR 0012).

``_ingest_plan_impl`` used to read ``<plan>.manifest.json`` by path and report
that path back in its response.  It now reads the manifest with
``records().get_manifest``, writes it with ``records().put_manifest``, and
checks for a prior ingest with ``records().list_plans``.  These tests run the
real ingest against :class:`SqliteStore`, so a path-based read would find no
manifest and fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import pipeline.server as p
from pipeline import ingest as ingest_mod
from pipeline import plan_draft
from pipeline.store_sqlite import SqliteStore

PLAN = "plan-records"


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    return d


@pytest.fixture
def sqlite_store(tmp_path: Path, monkeypatch, repo_root: Path):
    """A SqliteStore wired in as the live store, with no ticket side effects."""
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plans)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)

    class _Provider:
        enabled = False

        def create_epic(self, summary):
            return None

        def create_story(self, summary, description, epic_id, agent):
            return None

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _Provider())
    store = SqliteStore(str(tmp_path / "records.db"))
    monkeypatch.setattr(p, "_store", store)
    try:
        yield store
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()


def _plan(repo_root: Path, key: str = "S-1") -> dict:
    return {
        "repo_root": str(repo_root),
        "epics": [
            {
                "summary": "epic one",
                "stories": [
                    {
                        "summary": "story one",
                        "key": key,
                        "agent_instructions": "Do the thing.",
                    }
                ],
            }
        ],
    }


def _stage(store, plan: dict) -> None:
    store.records().put_plan_draft("default", PLAN, plan)


def _ingest(plan_name: str = PLAN, overwrite: bool = False) -> dict:
    return ingest_mod._ingest_plan_impl(plan_name, None, overwrite)


# --------------------------------------------------------------------------- #
# positive: first ingest and re-ingest through the record store
# --------------------------------------------------------------------------- #
def test_first_ingest_under_sqlite_succeeds_and_stores_the_manifest(
    sqlite_store, repo_root: Path
) -> None:
    _stage(sqlite_store, _plan(repo_root))

    result = _ingest()

    assert result["ok"] is True, result
    stored = sqlite_store.records().get_manifest("default", PLAN)
    assert stored is not None
    assert list(stored["stories"]) == ["S-1"]


def test_re_ingest_with_explicit_story_key_merges_into_the_stored_manifest(
    sqlite_store, repo_root: Path
) -> None:
    _stage(sqlite_store, _plan(repo_root))
    assert _ingest()["ok"] is True
    # A full ingest consumes the draft; the operator stages it again.
    _stage(sqlite_store, _plan(repo_root))

    result = _ingest()

    assert result["ok"] is True, result
    stored = sqlite_store.records().get_manifest("default", PLAN)
    assert list(stored["stories"]) == ["S-1"]


def test_ingest_response_has_no_manifest_path_key(
    sqlite_store, repo_root: Path
) -> None:
    _stage(sqlite_store, _plan(repo_root))

    result = _ingest()

    assert result["ok"] is True, result
    assert "manifest_path" not in result


# --------------------------------------------------------------------------- #
# negative
# --------------------------------------------------------------------------- #
def test_plan_name_failing_validation_is_refused_before_any_record_call(
    sqlite_store, monkeypatch
) -> None:
    def _no_records(*args, **kwargs):
        raise AssertionError("records() must not be reached for an invalid name")

    monkeypatch.setattr(sqlite_store, "records", _no_records)

    with pytest.raises(ValueError):
        _ingest("../escape")


# --------------------------------------------------------------------------- #
# negative: plan_draft refuses to read without a record view, no path fallback
# --------------------------------------------------------------------------- #
def test_read_plan_draft_raises_when_store_has_no_record_view(monkeypatch) -> None:
    class _LegacyOnlyStore:
        def manifest_path(self, plan_name):
            raise AssertionError("the legacy path must not be consulted")

    monkeypatch.setattr(p, "_store", _LegacyOnlyStore())

    with pytest.raises(RuntimeError, match="record view"):
        plan_draft.read_plan_draft(PLAN)
