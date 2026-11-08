"""One store-first manifest read, one explicit failure contract.

ADR 0012 stage B made several callers read a plan's manifest from the store's
record view first. That read was reimplemented per module and the copies drifted
into two *contradictory* failure contracts: ``pipeline.checkpoint`` and
``pipeline.worktree_patch`` let a store error propagate, while
``pipeline.persistence`` swallowed it and fell back to the file. These tests pin
the single implementation, :func:`pipeline.manifest_source.read_plan_manifest`,
and its two explicit contracts:

* ``fallback_on_error`` - the caller states, per call site, whether a failed
  read degrades to the other source (advisory reads) or fails closed (reads
  whose answer is about to be acted on);
* ``plan_dir`` - the caller passes *its own* patchable ``PLAN_DIR`` binding, so
  the fallback honours what the test suite patches.

They also pin the second defect: with the legacy ``FileStore`` the record view
delegates straight back to the same JSON files through a
``pipeline.server.PLAN_DIR`` binding of its own, so consulting it added no
information while letting a manifest outside the caller's binding shadow it.
The file store therefore has no record view for this read.
"""

from __future__ import annotations

import json

import pytest

from pipeline import checkpoint, manifest_source, persistence
from pipeline import server as p
from pipeline.store import FileStore


class _RecordStore:
    """A store whose only useful surface is the single-manifest record read."""

    def __init__(self, manifests=None, error=None):
        self._manifests = manifests or {}
        self._error = error
        self.requested = []

    def records(self):
        return self

    def get_manifest(self, namespace, plan_name):
        self.requested.append((namespace, plan_name))
        if self._error is not None:
            raise self._error
        return self._manifests.get(plan_name)


class _LegacyStore:
    """An in-process stand-in with no record view at all."""

    def __init__(self, plan_dir):
        self._plan_dir = plan_dir

    def manifest_path(self, plan_name):
        self._plan_dir / f"{plan_name}.manifest.json"


def _write_manifest(plan_dir, plan_name, role_config):
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {}, "role_config": role_config})
    )


@pytest.fixture
def dirs(tmp_path):
    """Two distinct plan directories: the server's and persistence's."""
    return {"server": tmp_path / "server-plans", "persistence": tmp_path / "persistence-plans"}


def test_file_store_record_view_does_not_shadow_the_callers_binding(dirs, monkeypatch):
    """The legacy FileStore must read the file, not its own server binding.

    Only ``pipeline.server.PLAN_DIR`` knows this plan. If the file store's
    record view were consulted, its own ``pipeline.server.PLAN_DIR`` binding
    would resolve the manifest and override the caller's
    ``pipeline.persistence.PLAN_DIR`` - i.e. a developer's real plans directory
    could shadow a fixture, and a patched binding would not be honoured on the
    primary path.
    """
    _write_manifest(dirs["server"], "shadow", {"planner": {"model": "from-server-binding"}})
    monkeypatch.setattr(p, "PLAN_DIR", dirs["server"])
    monkeypatch.setattr(p, "_store", p.build_store() if hasattr(p, "build_store") else p._store)
    monkeypatch.setattr(persistence, "PLAN_DIR", dirs["persistence"])

    assert isinstance(p._store, type(p._store))  # the default store, untouched
    assert persistence._plan_role_config("shadow") == {}


def test_file_store_reads_the_callers_own_binding(dirs, monkeypatch):
    """With the file store the caller's PLAN_DIR is the primary read."""
    _write_manifest(dirs["persistence"], "local", {"planner": {"model": "fixture"}})
    monkeypatch.setattr(p, "PLAN_DIR", dirs["server"])
    monkeypatch.setattr(persistence, "PLAN_DIR", dirs["persistence"])

    assert persistence._plan_role_config("local") == {"planner": {"model": "fixture"}}


def test_store_backed_record_view_is_the_primary_read(dirs, monkeypatch):
    """A genuinely store-backed deployment still reads the database first."""
    _write_manifest(dirs["persistence"], "shared", {"planner": {"model": "from-file"}})
    store = _RecordStore({"shared": {"role_config": {"planner": {"model": "from-store"}}}})
    monkeypatch.setattr(p, "_store", store)
    monkeypatch.setattr(persistence, "PLAN_DIR", dirs["persistence"])

    assert persistence._plan_role_config("shared") == {"planner": {"model": "from-store"}}
    assert store.requested == [("default", "shared")]


def test_plan_dir_is_honoured_on_the_fallback_path(dirs, monkeypatch):
    """A plan the record view does not know falls back to the caller's file."""
    _write_manifest(dirs["server"], "fallback", {"planner": {"model": "from-file"}})
    monkeypatch.setattr(p, "_store", _RecordStore())
    monkeypatch.setattr(p, "PLAN_DIR", dirs["server"])

    manifest = manifest_source.read_plan_manifest(
        "fallback", fallback_on_error=False, plans_root=p.PLAN_DIR
    )
    assert manifest["role_config"] == {"planner": {"model": "from-file"}}


def test_fallback_on_error_true_degrades_to_the_file(dirs, monkeypatch):
    """An advisory read swallows a store failure and reads the file."""
    _write_manifest(dirs["persistence"], "degraded", {"planner": {"model": "from-file"}})
    monkeypatch.setattr(p, "_store", _RecordStore(error=RuntimeError("store down")))
    monkeypatch.setattr(persistence, "PLAN_DIR", dirs["persistence"])

    assert persistence._plan_role_config("degraded") == {"planner": {"model": "from-file"}}


def test_fallback_on_error_false_propagates_the_store_error(dirs, monkeypatch):
    """A read about to be acted on fails closed instead of using a stale file."""
    _write_manifest(dirs["server"], "strict", {"role_config": {}})
    monkeypatch.setattr(p, "_store", _RecordStore(error=RuntimeError("store down")))
    monkeypatch.setattr(p, "PLAN_DIR", dirs["server"])

    with pytest.raises(RuntimeError, match="store down"):
        checkpoint._plan_manifest("strict")


def test_a_missing_plan_file_is_not_found_not_a_failure(dirs, monkeypatch):
    """Neither source knowing the plan is ``None``, even on the strict path."""
    monkeypatch.setattr(p, "_store", _RecordStore())
    monkeypatch.setattr(p, "PLAN_DIR", dirs["server"])

    assert (
        manifest_source.read_plan_manifest(
            "absent", fallback_on_error=False, plans_root=p.PLAN_DIR
        )
        is None
    )


def test_a_store_without_a_record_view_keeps_working(dirs, monkeypatch):
    """An in-process stand-in with no ``records()`` reads the file."""
    _write_manifest(dirs["server"], "legacy", {"planner": {"model": "file"}})
    monkeypatch.setattr(p, "_store", _LegacyStore(dirs["server"]))
    monkeypatch.setattr(p, "PLAN_DIR", dirs["server"])

    assert checkpoint._plan_manifest("legacy")["role_config"] == {"planner": {"model": "file"}}


def test_worktree_patch_delegates_fail_closed(dirs, monkeypatch):
    """apply_patch uses the shared read with ``fallback_on_error=False``."""
    from pipeline import worktree_patch

    calls = {}

    def spy(plan_name, *, fallback_on_error, plans_root):
        calls.update(
            plan_name=plan_name, fallback_on_error=fallback_on_error, plans_root=plans_root
        )

    monkeypatch.setattr(manifest_source, "read_plan_manifest", spy)
    monkeypatch.setattr(p, "PLAN_DIR", dirs["server"])

    from pipeline import concurrency

    class _Lock:
        def __enter__(self):
            return True

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(concurrency, "_plan_lock", lambda plan: _Lock())

    result = worktree_patch.apply_patch("plan-x", "story-x", "patch-1", "tok")

    assert result["status_code"] == 404
    assert calls == {
        "plan_name": "plan-x",
        "fallback_on_error": False,
        "plans_root": dirs["server"],
    }


# --- read_plan_names: the same read, one level up ---------------------------
#
# ``pipeline.tick_checks`` attributes its platform-wide notices to the first plan
# by name, so it needs "which plans exist?" rather than one manifest. That listing
# is the same store-first read, so it lives in the same module with the same two
# contracts - pinned here, next to the manifest read it mirrors, rather than in the
# tick's own module where a fourth hand-rolled copy of the store probe sat.


class _ListingStore:
    """A store whose record view lists plan names - or raises, on demand."""

    def __init__(self, names=(), error=None):
        self._names = list(names)
        self._error = error
        self.namespaces = []

    def records(self):
        return self

    def list_plans(self, namespace):
        self.namespaces.append(namespace)
        if self._error is not None:
            raise self._error
        return list(self._names)


def _write_plans(plan_dir, *names):
    plan_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (plan_dir / f"{name}.manifest.json").write_text("{}", encoding="utf-8")


def _advisory_names(plans_root):
    """The listing contract ``pipeline.tick_checks`` asks for."""
    return manifest_source.read_plan_names(
        fallback_on_error=True, plans_root=plans_root
    )


def test_read_plan_names_lists_the_store_records_first(tmp_path, monkeypatch):
    """A non-empty record listing is the answer; the disk is not consulted."""
    _write_plans(tmp_path, "only-on-disk")
    store = _ListingStore(["zeta", "alpha"])
    monkeypatch.setattr(p, "_store", store)

    assert _advisory_names(tmp_path) == ["alpha", "zeta"]
    assert store.namespaces == ["default"]


def test_read_plan_names_ignores_the_legacy_file_store(tmp_path, monkeypatch):
    """``FileStore``'s record view re-reads the same files, so it is no source."""
    _write_plans(tmp_path, "b-local", "a-local")
    monkeypatch.setattr(p, "_store", FileStore())

    assert _advisory_names(tmp_path) == ["a-local", "b-local"]


def test_an_empty_store_listing_is_read_as_no_store_view(tmp_path, monkeypatch):
    """An empty record listing falls through to the caller's plan directory.

    Deliberate: a store that records no plans yet must not make attribution
    impossible on a host that does have plans, so "empty listing" means "no store
    view" rather than "no plans" (documented in the docstring).
    """
    _write_plans(tmp_path, "a-local")
    monkeypatch.setattr(p, "_store", _ListingStore([]))

    assert _advisory_names(tmp_path) == ["a-local"]


def test_read_plan_names_without_a_store_lists_the_files(tmp_path, monkeypatch):
    _write_plans(tmp_path, "b-plan", "a-plan")
    monkeypatch.setattr(p, "_store", _LegacyStore(tmp_path))

    names = manifest_source.read_plan_names(
        fallback_on_error=False, plans_root=tmp_path
    )
    assert names == ["a-plan", "b-plan"]


def test_plan_names_degrade_when_the_store_cannot_list(tmp_path, monkeypatch):
    """``fallback_on_error=True``: an advisory read degrades to the file."""
    _write_plans(tmp_path, "a-local")
    monkeypatch.setattr(p, "_store", _ListingStore(error=RuntimeError("store down")))

    assert _advisory_names(tmp_path) == ["a-local"]


def test_plan_names_fail_closed_on_a_store_error(tmp_path, monkeypatch):
    """``fallback_on_error=False``: a store error propagates, not degraded."""
    monkeypatch.setattr(p, "_store", _ListingStore(error=RuntimeError("store down")))

    with pytest.raises(RuntimeError, match="store down"):
        manifest_source.read_plan_names(fallback_on_error=False, plans_root=tmp_path)


def test_no_plan_anywhere_lists_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_store", _ListingStore([]))

    assert _advisory_names(tmp_path) == []
