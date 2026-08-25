"""Tests for the W1b ``Store`` seam in ``pipeline/server.py``.

These tests verify the purely-additive introduction of a ``Store`` Protocol, a
``FileStore`` implementation, and a module-level ``_store`` singleton, WITHOUT
touching any existing call site. The 18 follow-up stories re-point the call
sites one function at a time.

They are written to be RED until the implementation exists: the ``Store``
Protocol, the ``FileStore`` class, and the ``_store`` singleton must all be
present for these to pass.
"""

import inspect
import json

import pytest

from pipeline import server as p

# ---------------------------------------------------------------------------
# Helpers / fixtures (mirror the ones in test_pipeline_service_seam.py so this
# file is fully self-contained).
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _write_manifest(plan_dir, plan_name, manifest):
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


# ---------------------------------------------------------------------------
# C1: Store Protocol and FileStore class exist above PipelineService.
# ---------------------------------------------------------------------------

def test_store_protocol_exists():
    assert hasattr(p, "Store"), "pipeline.server must define Store"
    assert inspect.isclass(p.Store)


def test_store_is_a_protocol():
    # Protocol classes carry the runtime_checkable / __protocol_attrs__ machinery.
    # The simplest portable assertion: it subclasses typing.Protocol (directly or
    # via its MRO).
    from typing import Protocol

    assert issubclass(p.Store, Protocol), "Store must be a typing.Protocol"


def test_filestore_class_exists():
    assert hasattr(p, "FileStore"), "pipeline.server must define FileStore"
    assert inspect.isclass(p.FileStore)


def test_filestore_implements_store_protocol_methods():
    # FileStore must provide every method named on the Store Protocol.
    for method in ("manifest_path", "get_manifest", "save_manifest", "transaction"):
        assert hasattr(p.FileStore, method), f"FileStore must define {method}"


def test_store_protocol_declares_four_methods():
    # The Store Protocol itself must declare the four methods.
    for method in ("manifest_path", "get_manifest", "save_manifest", "transaction"):
        assert hasattr(p.Store, method), f"Store Protocol must declare {method}"


def test_store_and_filestore_defined_above_pipeline_service():
    # Store and FileStore were extracted verbatim into pipeline/store.py; the
    # classes are defined there (Store above FileStore) and re-exported from
    # pipeline.server so the bindings still resolve on p.
    from pipeline import store as store_mod

    src = inspect.getsource(store_mod)
    i_store = src.find("class Store(Protocol):")
    i_filestore = src.find("class FileStore:")
    assert i_store != -1, "class Store(Protocol): not found in pipeline/store.py"
    assert i_filestore != -1, "class FileStore: not found in pipeline/store.py"
    assert i_store < i_filestore, (
        "Store and FileStore must be defined in pipeline/store.py, Store first"
    )
    assert hasattr(p, "Store") and hasattr(p, "FileStore"), (
        "pipeline.server must re-export Store and FileStore"
    )


# ---------------------------------------------------------------------------
# C2 / C3 / C4: singleton is a FileStore with no instance state, no __init__.
# ---------------------------------------------------------------------------

def test_store_singleton_is_a_filestore():
    assert isinstance(p._store, p.FileStore), "module-level _store must be a FileStore"


def test_filestore_has_no_instance_state():
    assert p.FileStore().__dict__ == {}, "FileStore instances must hold no state"


def test_filestore_defines_no_init():
    assert "__init__" not in vars(p.FileStore), (
        "FileStore must not define __init__ -- it would risk caching globals (R1)"
    )


def test_filestore_methods_do_not_reach_globals_through_self():
    # R1: no method body may reference self.PLAN_DIR / self._plan_lock / etc.
    for method_name in ("manifest_path", "get_manifest", "save_manifest", "transaction"):
        method = getattr(p.FileStore, method_name)
        src = inspect.getsource(method)
        assert "self.PLAN_DIR" not in src, f"{method_name} must not use self.PLAN_DIR"
        assert "self._plan_lock" not in src, f"{method_name} must not use self._plan_lock"
        assert "self._atomic_write_json" not in src, (
            f"{method_name} must not use self._atomic_write_json"
        )


# ---------------------------------------------------------------------------
# manifest_path
# ---------------------------------------------------------------------------

def test_manifest_path_is_plan_dir_manifest_json(plan_dir):
    assert p._store.manifest_path("demo") == plan_dir / "demo.manifest.json"


def test_manifest_path_follows_monkeypatched_plan_dir(tmp_path, monkeypatch):
    # R1 regression detector: patch PLAN_DIR to a SECOND temp dir AFTER import
    # and assert manifest_path moves with it. Fails if FileStore cached PLAN_DIR.
    second = tmp_path / "elsewhere"
    second.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", second)
    assert p._store.manifest_path("demo") == second / "demo.manifest.json"


def test_manifest_path_returns_a_path():
    from pathlib import Path

    result = p._store.manifest_path("demo")
    assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# get_manifest
# ---------------------------------------------------------------------------

def test_get_manifest_returns_parsed_json(plan_dir):
    manifest = {"epics": {}, "stories": {"s1": {"status": "pending"}}}
    _write_manifest(plan_dir, "demo", manifest)
    assert p._store.get_manifest("demo") == manifest


def test_get_manifest_raises_file_not_found_for_unknown_plan(plan_dir):
    # N1 / R3: must raise exactly FileNotFoundError, NOT return {} or None.
    with pytest.raises(FileNotFoundError):
        p._store.get_manifest("does-not-exist")


def test_get_manifest_does_not_swallow_errors(plan_dir):
    # R3: no .exists() fallback. A missing manifest must propagate the real
    # exception type from Path.read_text().
    try:
        p._store.get_manifest("nope")
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001 - re-raise with a clear message
        pytest.fail(f"get_manifest must raise FileNotFoundError, not {type(exc).__name__}")
    pytest.fail("get_manifest must raise FileNotFoundError for a missing manifest")


# ---------------------------------------------------------------------------
# save_manifest
# ---------------------------------------------------------------------------

def test_save_manifest_writes_atomically_and_round_trips(plan_dir):
    manifest = {"epics": {}, "stories": {"s1": {"status": "done"}}}
    p._store.save_manifest("demo", manifest)
    # Round-trips through get_manifest.
    assert p._store.get_manifest("demo") == manifest
    # The file on disk parses as JSON.
    assert _read_manifest(plan_dir, "demo") == manifest


def test_save_manifest_overwrites_existing(plan_dir):
    _write_manifest(plan_dir, "demo", {"old": True})
    p._store.save_manifest("demo", {"new": True})
    assert p._store.get_manifest("demo") == {"new": True}


def test_save_manifest_writes_to_manifest_path(plan_dir):
    # save_manifest must write to the same path manifest_path reports.
    manifest = {"k": "v"}
    p._store.save_manifest("demo", manifest)
    assert p._store.manifest_path("demo").read_text() == json.dumps(manifest)


# ---------------------------------------------------------------------------
# transaction
# ---------------------------------------------------------------------------

def test_transaction_yields_true_when_lock_free(plan_dir):
    with p._store.transaction("demo") as acquired:
        assert acquired is True


def test_transaction_is_reentrant_within_a_thread(plan_dir):
    # N3: nested transaction in the same thread yields True both times.
    # This proves transaction delegates to _plan_lock rather than
    # re-implementing flock (a fresh flock would BlockingIOError on the
    # nested call).
    with p._store.transaction("demo") as outer:
        assert outer is True
        with p._store.transaction("demo") as inner:
            assert inner is True


def test_transaction_returns_plan_lock_result(plan_dir):
    # C5: transaction must return _plan_lock(plan_name), not re-implement
    # locking. The cleanest behavioural assertion is that the context manager
    # yields the same value _plan_lock does for a free lock.
    with p._plan_lock("demo") as direct:
        expected = direct
    with p._store.transaction("demo") as via_store:
        assert via_store == expected


def test_transaction_does_not_reimplement_fcntl(plan_dir):
    # C5: FileStore.transaction must delegate to _plan_lock, not call
    # fcntl/os.open directly. Inspect the source.
    src = inspect.getsource(p.FileStore.transaction)
    assert "fcntl" not in src, "transaction must not call fcntl directly"
    assert "os.open" not in src, "transaction must not call os.open directly"
    assert "_plan_lock" in src, "transaction must delegate to _plan_lock"


# ---------------------------------------------------------------------------
# Import-line / structural requirements.
# ---------------------------------------------------------------------------

def test_typing_imports_protocol():
    # C1: `from typing import Any, Protocol` (Protocol must be imported).
    # Store/FileStore moved to pipeline/store.py, which owns the Protocol import.
    from pipeline import store as store_mod

    src = inspect.getsource(store_mod)
    assert "Protocol" in src, "pipeline/store.py must import Protocol from typing"


def test_only_one_typing_import_line():
    # The task says to widen the existing `from typing import Any` line, not
    # add a second one.
    import re

    from pipeline import store as store_mod

    src = inspect.getsource(store_mod)
    typing_imports = re.findall(r"^from typing import .+$", src, re.MULTILINE)
    assert len(typing_imports) == 1, (
        f"expected exactly one `from typing import` line, found {len(typing_imports)}"
    )
    assert "Protocol" in typing_imports[0]


def test_module_level_store_singleton_exists():
    assert hasattr(p, "_store"), "pipeline.server must define module-level _store"


def test_store_singleton_constructed_at_import():
    # _store must be a FileStore instance created at import time (not a
    # function that returns one).
    assert isinstance(p._store, p.FileStore)


# ---------------------------------------------------------------------------
# No call-site migration (C6): the raw path-construction line count tracks
# each story's migration of one call site.
# ---------------------------------------------------------------------------

def test_no_call_site_was_migrated():
    # C6: `manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"` must
    # appear 9 times -- this branch migrated three call sites (review_story,
    # _advance_pipeline_locked, _approve_merge_impl) and deleted one
    # dead-code occurrence in _repo_root_for.
    import subprocess

    result = subprocess.run(
        ["grep", "-c", 'manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"',
         "pipeline/server.py"],
        capture_output=True, text=True, check=True,
    )
    count = int(result.stdout.strip())
    assert count == 9, (
        f"expected 9 raw manifest_path constructions, found {count}; "
        "this branch migrated review_story, _advance_pipeline_locked, and "
        "_approve_merge_impl, and deleted one dead-code occurrence"
    )


# ---------------------------------------------------------------------------
# W1b-02: list_plans, list_manifests, update_story, append_decision,
# append_journal.
# ---------------------------------------------------------------------------

def test_store_protocol_declares_the_five_new_methods():
    for method in (
        "list_plans", "list_manifests", "update_story",
        "append_decision", "append_journal",
    ):
        assert hasattr(p.Store, method), f"Store Protocol must declare {method}"


def test_filestore_implements_the_five_new_methods():
    for method in (
        "list_plans", "list_manifests", "update_story",
        "append_decision", "append_journal",
    ):
        assert hasattr(p.FileStore, method), f"FileStore must define {method}"


def test_list_plans_includes_both_plan_stem_and_manifest_stem(plan_dir):
    (plan_dir / "demo.json").write_text("{}")
    _write_manifest(plan_dir, "demo", {"stories": {}})
    assert sorted(p._store.list_plans()) == ["demo", "demo.manifest"]


def test_list_plans_empty_dir_returns_empty_list(plan_dir):
    assert p._store.list_plans() == []


def test_list_manifests_returns_sorted_suffix_stripped_names(plan_dir):
    _write_manifest(plan_dir, "zeta", {"stories": {}})
    _write_manifest(plan_dir, "alpha", {"stories": {}})
    assert p._store.list_manifests() == ["alpha", "zeta"]


def test_list_manifests_empty_dir_returns_empty_list(plan_dir):
    assert p._store.list_manifests() == []


def test_update_story_returns_updated_story_and_persists(plan_dir):
    _write_manifest(plan_dir, "demo", {"stories": {"s1": {"status": "pending"}}})
    result = p._store.update_story("demo", "s1", {"status": "done"})
    assert result == {"status": "done"}
    assert _read_manifest(plan_dir, "demo") == {"stories": {"s1": {"status": "done"}}}


def test_update_story_returns_none_for_unknown_story_key(plan_dir):
    # N1: an unknown story key returns None and does not write the manifest.
    manifest = {"stories": {"s1": {"status": "pending"}}}
    _write_manifest(plan_dir, "demo", manifest)
    before = p._store.manifest_path("demo").read_text()
    result = p._store.update_story("demo", "NO-SUCH-KEY", {"status": "done"})
    assert result is None
    assert p._store.manifest_path("demo").read_text() == before


def test_update_story_missing_manifest_raises_file_not_found(plan_dir):
    # N2: get_manifest's FileNotFoundError must propagate, not be swallowed.
    with pytest.raises(FileNotFoundError):
        p._store.update_story("does-not-exist", "s1", {"status": "done"})


def test_append_decision_delegates_to_bare_function():
    src = inspect.getsource(p.FileStore.append_decision)
    assert "_append_decision(" in src
    assert "persistence." not in src


def test_append_journal_delegates_to_bare_function():
    src = inspect.getsource(p.FileStore.append_journal)
    assert "_append_journal(" in src
    assert "persistence." not in src


def test_append_decision_creates_log_file_and_appends_in_order(plan_dir, monkeypatch):
    from pipeline import persistence as ppers

    monkeypatch.setattr(ppers, "PLAN_DIR", plan_dir)
    p._store.append_decision("demo", {"n": 1})
    p._store.append_decision("demo", {"n": 2})
    log_path = plan_dir / "demo.decisions.json"
    assert json.loads(log_path.read_text()) == [{"n": 1}, {"n": 2}]


def test_append_journal_creates_log_file_and_appends_in_order(plan_dir, monkeypatch):
    from pipeline import persistence as ppers

    monkeypatch.setattr(ppers, "PLAN_DIR", plan_dir)
    p._store.append_journal("demo", "s1", {"step": "a"})
    p._store.append_journal("demo", "s1", {"step": "b"})
    log_path = plan_dir / "demo.s1.journal.json"
    assert json.loads(log_path.read_text()) == [{"step": "a"}, {"step": "b"}]