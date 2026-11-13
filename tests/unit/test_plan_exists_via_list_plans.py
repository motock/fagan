"""Plan-existence checks answer through the record store's ``list_plans``.

Seven call sites used to ask the store for the plan's manifest file and test it. That
answers from a file on disk, which a record-only store does not have. Each
site now asks ``name in _store.records().list_plans("default")``. The store
below has no manifest file anywhere: a file-based check always says "absent",
so a test that expects "present" fails on the old code for the right reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import authorization, plan_approval
from pipeline import server as p
from pipeline.service import PipelineService
from pipeline.store_memory import InMemoryRecordStore
from pipeline.tools import reset_outdated_approvals

NAMESPACE = "default"


class _RecordsOnlyStore:
    """A store whose manifests live only in its record view.

    ``manifest_path`` points at a file that never exists, so any caller that
    still checks the disk reports every plan as missing.
    """

    def __init__(self, tmp_path: Path, manifests: dict[str, dict]):
        self._records = InMemoryRecordStore()
        for name, manifest in manifests.items():
            self._records.put_manifest(NAMESPACE, name, manifest)
        self._never_written = tmp_path / "no-plans-here" / "plan.manifest.json"

    def audit_log(self):
        """The file audit log, which is what the real store serves under file."""
        from pipeline.audit_file import FileAuditLog

        return FileAuditLog()

    def manifest_path(self, plan_name: str) -> Path:
        return self._never_written

    def records(self):
        return self._records

    def get_manifest(self, plan_name: str):
        return self._records.get_manifest(NAMESPACE, plan_name)

    def save_manifest(self, plan_name: str, manifest: dict) -> None:
        self._records.put_manifest(NAMESPACE, plan_name, manifest)

    def transaction(self, plan_name: str, wait_seconds: float = 0.0):
        return self._records.transaction(NAMESPACE, plan_name, wait_seconds)

    def list_plans(self):
        return self._records.list_plans(NAMESPACE)


def _paused_manifest() -> dict:
    return {"paused": True, "stories": {}, "role_config": {}, "repo_root": "/srv/r"}


@pytest.fixture
def use_store(monkeypatch, tmp_path):
    """Install a records-only store as the server's live store."""

    def _install(manifests: dict[str, dict]) -> _RecordsOnlyStore:
        store = _RecordsOnlyStore(tmp_path, manifests)
        monkeypatch.setattr(p, "_store", store)
        return store

    return _install


# ---------- advance: _advance_pipeline_locked_impl ----------

def test_advance_treats_a_plan_listed_by_the_record_store_as_present(use_store):
    use_store({"alpha": _paused_manifest()})

    from pipeline import advance

    result = advance._advance_pipeline_locked_impl("alpha")

    assert result == {"ok": True, "skipped": "plan_paused"}


def test_advance_reports_a_plan_the_record_store_does_not_list_as_missing(use_store):
    use_store({"alpha": _paused_manifest()})

    from pipeline import advance

    assert advance._advance_pipeline_locked_impl("alp") == {
        "ok": False,
        "error": "No manifest for alp",
    }


# ---------- plan_approval: _decide_plan ----------

@pytest.fixture
def approval_gate_open(monkeypatch):
    """Reach the plan-existence check: enable approval and authorize the caller."""
    monkeypatch.setattr(plan_approval, "approval_enabled", lambda: True)
    monkeypatch.setattr(authorization, "authorize", lambda *args, **kwargs: None)


def test_decide_plan_treats_a_plan_listed_by_the_record_store_as_present(
    use_store, approval_gate_open
):
    use_store({"alpha": _paused_manifest()})

    result = PipelineService()._decide_plan("alpha", status="approved", reason="ok")

    assert result.get("error") != "No such plan 'alpha'"


def test_decide_plan_reports_a_plan_the_record_store_does_not_list_as_missing(
    use_store, approval_gate_open
):
    use_store({"alpha-2": _paused_manifest()})

    result = PipelineService()._decide_plan("alpha", status="approved", reason="ok")

    assert result == {"ok": False, "error": "No such plan 'alpha'"}


# ---------- server: _repo_root_for ----------

def test_repo_root_for_uses_the_manifest_of_a_plan_listed_by_the_record_store(use_store):
    use_store({"alpha": {"repo_root": "/srv/alpha", "stories": {}}})

    assert p._repo_root_for("alpha") == Path("/srv/alpha")


def test_repo_root_for_falls_back_when_the_record_store_does_not_list_the_plan(use_store):
    use_store({"alpha-2": {"repo_root": "/srv/alpha-2", "stories": {}}})

    assert p._repo_root_for("alpha") == p.REPO_ROOT


# ---------- service: list_ready_stories ----------

def _todo_manifest() -> dict:
    return {
        "stories": {
            "S1": {
                "summary": "first",
                "status": "todo",
                "dependencies": [],
                "files": [],
            }
        }
    }


def test_list_ready_stories_reads_a_plan_listed_by_the_record_store(use_store):
    use_store({"alpha": _todo_manifest()})

    ready = PipelineService().list_ready_stories("alpha")

    assert [story["summary"] for story in ready] == ["first"]


def test_list_ready_stories_returns_nothing_for_a_plan_the_store_does_not_list(use_store):
    use_store({"alpha-2": _todo_manifest()})

    assert PipelineService().list_ready_stories("alpha") == []


# ---------- service_role_config: set_plan_role_config / patch_plan ----------

def test_set_plan_role_config_treats_a_listed_plan_as_present(use_store):
    use_store({"alpha": _paused_manifest()})

    result = PipelineService().set_plan_role_config("alpha", "review")

    assert result.get("error") != "No such plan 'alpha'"


def test_set_plan_role_config_reports_an_unlisted_plan_as_missing(use_store):
    use_store({"alpha-2": _paused_manifest()})

    result = PipelineService().set_plan_role_config("alpha", "review")

    assert result == {"ok": False, "error": "No such plan 'alpha'"}


def test_patch_plan_treats_a_plan_listed_by_the_record_store_as_present(use_store):
    use_store({"alpha": _paused_manifest()})

    result = PipelineService().patch_plan("alpha", {"role_config": {}})

    assert result == {"ok": True, "plan_name": "alpha"}


def test_patch_plan_reports_an_unlisted_plan_as_missing(use_store):
    use_store({"alpha-2": _paused_manifest()})

    result = PipelineService().patch_plan("alpha", {"role_config": {}})

    assert result == {"ok": False, "error": "No such plan 'alpha'"}


# ---------- tools/reset_outdated_approvals: _plan_names ----------

class _ListingStore:
    """A store with a legacy plan listing and a record view that disagree."""

    def __init__(self, tmp_path: Path, listed: list[str], recorded: list[str]):
        self._listed = listed
        self._records = InMemoryRecordStore()
        for name in recorded:
            self._records.put_manifest(NAMESPACE, name, {"stories": {}})
        self._never_written = tmp_path / "no-plans-here" / "plan.manifest.json"

    def list_plans(self):
        return list(self._listed)

    def manifest_path(self, plan_name: str) -> Path:
        return self._never_written

    def records(self):
        return self._records


def test_reset_plan_names_keeps_only_plans_the_record_store_lists(tmp_path):
    store = _ListingStore(tmp_path, listed=["alpha", "beta"], recorded=["alpha"])

    assert reset_outdated_approvals._plan_names(store) == ["alpha"]


def test_reset_plan_names_drops_a_plan_that_is_only_a_prefix_of_a_listed_plan(tmp_path):
    store = _ListingStore(tmp_path, listed=["alpha", "alpha-2"], recorded=["alpha-2"])

    assert reset_outdated_approvals._plan_names(store) == ["alpha-2"]


def test_reset_plan_names_reads_the_record_view_once(tmp_path):
    store = _ListingStore(tmp_path, listed=["alpha"], recorded=["alpha"])
    calls = []
    real_records = store.records

    def _counting_records():
        calls.append(1)
        return real_records()

    store.records = _counting_records

    reset_outdated_approvals._plan_names(store)

    assert len(calls) == 1
