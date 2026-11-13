"""``PluginStore``: a plugin record store served as the runtime ``Store``.

Contract under test (ADR 0012 decisions 7 and 9):

* ``build_plugin_store`` refuses a plugin missing any name in
  ``PLUGIN_STORE_METHODS`` -- the record surface plus the outbox and audit
  methods the server calls -- naming the missing method and never the spec.
* The legacy ``Store`` names on ``PluginStore`` agree with the namespaced
  record API they are expressed over, and keep the legacy semantics of
  ``SqliteStore``: a missing manifest raises ``FileNotFoundError``, an unknown
  story key updates to ``None``, a contended transaction yields ``False``.
* ``records()`` is the plugin itself, so a namespace the plugin refuses is still
  refused through it.
* ``InMemoryRecordStore.audit_log()`` is one log per store, so the reference
  plugin satisfies ``PLUGIN_STORE_METHODS``.
"""

from __future__ import annotations

import threading

import pytest

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server  # noqa: F401
from app.plugin_loader import PluginError
from pipeline.audit_memory import InMemoryAuditLog
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_plugin import PLUGIN_STORE_METHODS, PluginStore, build_plugin_store
from pipeline.store_records import UnknownNamespaceError

PLAN = "adapter-plan"
MANIFEST = {"schema_version": 1, "stories": {"S1": {"status": "pending", "title": "T"}}}


class _NoAuditLog(InMemoryRecordStore):
    """A plugin whose ``audit_log`` is not callable."""

    audit_log = None


class _NoReadOutbox(InMemoryRecordStore):
    """A plugin whose ``read_outbox`` is not callable."""

    read_outbox = None


class _DefaultNamespaceOnly(InMemoryRecordStore):
    """A plugin that refuses every namespace but ``default``, as SqliteRecords does."""

    def get_manifest(self, namespace, plan_name):
        if namespace != "default":
            raise UnknownNamespaceError("unknown namespace")
        return super().get_manifest(namespace, plan_name)


def test_wrapped_store_round_trips_manifest_through_both_apis():
    plugin = InMemoryRecordStore()
    store = PluginStore(plugin)

    store.save_manifest(PLAN, MANIFEST)

    assert store.get_manifest(PLAN) == MANIFEST
    assert store.records().get_manifest("default", PLAN) == MANIFEST


def test_get_manifest_of_absent_plan_raises_file_not_found():
    store = PluginStore(InMemoryRecordStore())

    with pytest.raises(FileNotFoundError):
        store.get_manifest(PLAN)


def test_get_manifest_or_none_of_absent_plan_is_none():
    store = PluginStore(InMemoryRecordStore())

    assert store.get_manifest_or_none(PLAN) is None


def test_update_story_applies_fields_and_returns_the_story():
    store = PluginStore(InMemoryRecordStore())
    store.save_manifest(PLAN, MANIFEST)

    story = store.update_story(PLAN, "S1", {"status": "done"})

    assert story == {"status": "done", "title": "T"}
    assert store.get_manifest(PLAN)["stories"]["S1"]["status"] == "done"


def test_update_story_on_unknown_key_returns_none_and_writes_nothing():
    store = PluginStore(InMemoryRecordStore())
    store.save_manifest(PLAN, MANIFEST)

    assert store.update_story(PLAN, "NO-SUCH-KEY", {"status": "done"}) is None
    assert store.get_manifest(PLAN) == MANIFEST


def test_list_manifests_and_list_plans_report_recorded_plans_sorted():
    store = PluginStore(InMemoryRecordStore())
    store.save_manifest("b-plan", MANIFEST)
    store.save_manifest("a-plan", MANIFEST)

    assert store.list_manifests() == ["a-plan", "b-plan"]
    assert store.list_plans() == ["a-plan", "b-plan"]


def test_contended_transaction_yields_false_without_blocking():
    store = PluginStore(InMemoryRecordStore())
    held = threading.Event()
    release = threading.Event()

    def _hold():
        with store.transaction(PLAN):
            held.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=_hold)
    holder.start()
    try:
        assert held.wait(timeout=5)
        with store.transaction(PLAN, wait_seconds=0.0) as acquired:
            assert acquired is False
    finally:
        release.set()
        holder.join(timeout=5)


def test_legacy_decisions_round_trip_through_the_namespaced_api():
    plugin = InMemoryRecordStore()
    store = PluginStore(plugin)

    store.append_decision(PLAN, {"decision": "approve"})

    assert store.get_decisions(PLAN) == [{"decision": "approve"}]
    assert plugin.get_decisions("default", PLAN) == [{"decision": "approve"}]


def test_legacy_journal_fills_display_defaults_and_reports_absence():
    store = PluginStore(InMemoryRecordStore())

    assert store.get_journal(PLAN, "S1") == (False, [])
    assert store.get_journal_final_ts(PLAN, "S1") is None

    store.append_journal(PLAN, "S1", {"ts": "2026-01-01T00:00:00+00:00", "step": "one"})

    available, entries = store.get_journal(PLAN, "S1")
    assert available is True
    assert entries == [
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "step": "one",
            "summary": None,
            "next_hint": None,
        }
    ]
    assert store.get_journal_final_ts(PLAN, "S1") == "2026-01-01T00:00:00+00:00"


def test_legacy_notifications_read_back_as_lines_and_records():
    store = PluginStore(InMemoryRecordStore())
    store.records().append_notification(
        "default",
        PLAN,
        {"ts": "2026-01-01T00:00:00+00:00", "message": "hello", "severity": "info"},
    )

    assert store.get_notifications(PLAN) == ["2026-01-01T00:00:00+00:00 hello"]
    records = store.get_notification_records(PLAN)
    assert records[0]["message"] == "hello"
    assert records[0]["severity"] == "info"


def test_records_is_the_plugin_itself():
    plugin = InMemoryRecordStore()
    store = PluginStore(plugin)

    assert store.records() is plugin


def test_foreign_namespace_through_records_is_still_refused():
    store = PluginStore(_DefaultNamespaceOnly())

    with pytest.raises(UnknownNamespaceError):
        store.records().get_manifest("other", PLAN)


def test_audit_log_is_the_same_object_on_every_call_of_one_store():
    store = InMemoryRecordStore()

    assert isinstance(store.audit_log(), InMemoryAuditLog)
    assert store.audit_log() is store.audit_log()


def test_audit_log_is_not_shared_between_stores():
    assert InMemoryRecordStore().audit_log() is not InMemoryRecordStore().audit_log()


def test_reference_plugin_satisfies_every_plugin_store_method():
    store = build_plugin_store(InMemoryRecordStore)

    missing = [name for name in PLUGIN_STORE_METHODS if not callable(getattr(store, name, None))]
    assert missing == []


def test_plugin_missing_audit_log_is_refused_naming_the_method():
    with pytest.raises(PluginError) as excinfo:
        build_plugin_store(_NoAuditLog)

    assert "audit_log" in str(excinfo.value)
    assert "missing required methods" in str(excinfo.value)


def test_plugin_missing_read_outbox_is_refused_naming_the_method():
    with pytest.raises(PluginError) as excinfo:
        build_plugin_store(_NoReadOutbox)

    assert "read_outbox" in str(excinfo.value)
    assert "missing required methods" in str(excinfo.value)


def test_refusal_does_not_echo_the_factory_or_its_module():
    with pytest.raises(PluginError) as excinfo:
        build_plugin_store(_NoAuditLog)

    assert "_NoAuditLog" not in str(excinfo.value)
    assert "store_plugin" not in str(excinfo.value)
