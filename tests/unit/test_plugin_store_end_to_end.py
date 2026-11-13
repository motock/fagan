"""The server runs end to end on a ``PIPELINE_STORE=plugin`` record store.

With the store selected by ``PIPELINE_STORE=plugin`` and
``PIPELINE_STORE_PLUGIN`` naming an in-memory record store, the operations the
sqlite end-to-end tests drive are driven here against the plugin: ingest,
claim, patch, journal, decision, security-audit state, audit append and read,
notification enqueue and drain, and checkpoint. After each group the state must
be in the plugin and the plans directory must hold no record file. Only the
host-local files named in ``ALLOWED_HOST_FILES`` may appear there.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

# pipeline.server must load before the other pipeline modules (import cycle).
import pipeline.server as p
from pipeline import audit, checkpoint, notification_outbox, ownership, security_audit
from pipeline import persistence as ppers
from pipeline.plan_approval import APPROVAL_DIGEST_VERSION, canonical_digest
from pipeline.service import PipelineService
from pipeline.store import FileStore, build_store, store_kind
from pipeline.store_plugin import PluginStore
from pipeline.store_records import DEFAULT_NAMESPACE
from tests.fixtures import store_plugin

STORE_ENV = "PIPELINE_STORE"
PLUGIN_ENV = "PIPELINE_STORE_PLUGIN"
PLAN_ENV = "PLAN_DIR"
APPROVAL_ENV = "PIPELINE_PLAN_APPROVAL"
SHARED_SPEC = "tests.fixtures.store_plugin:build_shared_record_store"
REFUSING_SPEC = "tests.fixtures.store_plugin:build_audit_refusing_record_store"

PLAN = "e2e-plugin"
STORY = "s1"

#: The only entries the plans directory may hold: host-local state that is not a
#: plan record. ``.wake`` holds per-plan wake markers (ADR 0012 decision 9), the
#: scheduler owns the lock and health files, and ``<plan>.lock`` is this plan's
#: lock. A manifest, journal, decision, notification, outbox, draft or audit
#: file is a record and belongs in the store.
ALLOWED_HOST_FILES = frozenset(
    {
        ".wake",
        ".scheduler_daemon.lock",
        ".scheduler_health.json",
        "heavy.lock",
        f"{PLAN}.lock",
    }
)


# ---------------------------------------------------------------------------
# Harness


@pytest.fixture
def plugin_server(monkeypatch, tmp_path):
    """Run the server on the plugin store, with the plans directory emptied."""
    store_plugin.reset_shared_record_store()
    plans = tmp_path / "plans"
    plans.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    monkeypatch.setenv(STORE_ENV, "plugin")
    monkeypatch.setenv(PLUGIN_ENV, SHARED_SPEC)
    monkeypatch.setenv(PLAN_ENV, str(plans))
    monkeypatch.delenv(APPROVAL_ENV, raising=False)

    store = build_store()
    assert isinstance(store, PluginStore)
    monkeypatch.setattr(p, "_store", store)
    monkeypatch.setattr(p, "PLAN_DIR", plans)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    monkeypatch.setattr(p, "WORKTREE_ROOT", worktrees)

    yield store, plans, tmp_path
    store_plugin.reset_shared_record_store()


def _plan(repo) -> dict:
    return {
        "repo_root": str(repo),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    {
                        "key": STORY,
                        "summary": "Story one",
                        "agent_instructions": "Preflight: plugin store fixture\nDo it.",
                    }
                ],
            }
        ],
    }


def _ingest(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    svc = PipelineService()
    staged = svc.save_plan(PLAN, json.dumps(_plan(repo)))
    assert staged["ok"] is True, staged
    ingested = svc.ingest_plan(PLAN)
    assert ingested["ok"] is True, ingested
    return repo


def _assert_no_record_files(plans):
    extra = sorted(entry.name for entry in plans.iterdir() if entry.name not in ALLOWED_HOST_FILES)
    assert extra == [], f"record files written under the plans directory: {extra}"


# ---------------------------------------------------------------------------
# 1. ingest, claim, patch


def test_ingest_claim_and_patch_persist_only_in_the_plugin(plugin_server):
    store, plans, tmp_path = plugin_server
    _ingest(store, tmp_path)
    records = store.records()

    manifest = records.get_manifest(DEFAULT_NAMESPACE, PLAN)
    assert manifest is not None, "ingest did not persist the manifest to the plugin"
    assert manifest["stories"][STORY]["status"] == "todo"
    _assert_no_record_files(plans)

    claimed = p.mark_story_in_progress(PLAN, STORY)
    assert claimed["ok"] is True, claimed
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN)["stories"][STORY]["status"] == "in_progress"
    _assert_no_record_files(plans)

    patched = p.patch_story(PLAN, STORY, {"agent_instructions": "Do it again."})
    assert patched["ok"] is True, patched
    assert records.get_manifest(DEFAULT_NAMESPACE, PLAN)["stories"][STORY]["agent_instructions"] == (
        "Do it again."
    )
    _assert_no_record_files(plans)


def test_patching_an_approved_plan_resets_the_approval_in_the_plugin(plugin_server, monkeypatch):
    store, plans, tmp_path = plugin_server
    monkeypatch.setenv(APPROVAL_ENV, "1")
    _ingest(store, tmp_path)
    records = store.records()

    manifest = records.get_manifest(DEFAULT_NAMESPACE, PLAN)
    manifest["approval"] = {
        "status": "approved",
        "digest": canonical_digest(manifest),
        "digest_version": APPROVAL_DIGEST_VERSION,
        "decided_by": "operator",
    }
    records.put_manifest(DEFAULT_NAMESPACE, PLAN, manifest)

    patched = p.patch_story(PLAN, STORY, {"agent_instructions": "Changed after approval."})
    assert patched["ok"] is True, patched

    approval = records.get_manifest(DEFAULT_NAMESPACE, PLAN)["approval"]
    assert approval["status"] == "pending_approval"
    assert "decided_by" not in approval
    _assert_no_record_files(plans)


def test_patch_of_a_missing_story_is_refused_and_writes_nothing(plugin_server):
    store, plans, tmp_path = plugin_server
    _ingest(store, tmp_path)
    before = store.records().get_manifest(DEFAULT_NAMESPACE, PLAN)

    result = p.patch_story(PLAN, "no-such-story", {"agent_instructions": "x"})

    assert result["ok"] is False
    assert store.records().get_manifest(DEFAULT_NAMESPACE, PLAN) == before
    _assert_no_record_files(plans)


# ---------------------------------------------------------------------------
# 2. journal, decision, security-audit state


def test_journal_decision_and_security_audit_state_persist_in_the_plugin(plugin_server):
    store, plans, tmp_path = plugin_server
    repo = _ingest(store, tmp_path)
    records = store.records()

    store.append_journal(PLAN, STORY, {"step": "manual", "summary": "by hand"})
    journal = records.get_journal(DEFAULT_NAMESPACE, PLAN, STORY)
    assert [entry["step"] for entry in journal] == ["manual"]

    PipelineService().append_decision(PLAN, {"story_key": STORY, "decision": "approve"})
    assert records.get_decisions(DEFAULT_NAMESPACE, PLAN)[-1]["decision"] == "approve"
    _assert_no_record_files(plans)

    now = datetime.now(UTC)
    recorded = security_audit.record_audit(records, str(repo), "abc1234", now)
    assert recorded["last_audited_sha"] == "abc1234"
    assert security_audit.read_audit_state(records, str(repo))["last_audited_sha"] == "abc1234"
    _assert_no_record_files(plans)


# ---------------------------------------------------------------------------
# 3. audit append and read-back through pipeline.audit


def test_audit_append_and_read_back_go_through_the_plugin_audit_log(plugin_server):
    store, plans, _tmp = plugin_server

    written = audit.append_audit(
        PLAN,
        action="plan:ingest",
        outcome="allowed",
        target={"plan": PLAN},
    )

    chain = store.audit_log().read_chain(DEFAULT_NAMESPACE, PLAN)
    assert [record["action"] for record in chain] == ["plan:ingest"]
    assert chain[0]["hash"] == written["hash"]
    assert audit.verify_audit_chain(PLAN) is None
    _assert_no_record_files(plans)


def test_a_refused_ownership_change_is_audited_in_the_plugin(plugin_server):
    store, plans, _tmp = plugin_server
    prior = {"owner": "alice", "delegates": []}

    with pytest.raises(ValueError, match="current owner"):
        ownership.resolve_ownership(PLAN, {"owner": "mallory"}, prior, "mallory")

    chain = store.audit_log().read_chain(DEFAULT_NAMESPACE, PLAN)
    assert [(record["action"], record["outcome"]) for record in chain] == [
        ("plan:ownership_change", "denied")
    ]
    _assert_no_record_files(plans)


def test_audit_refusal_by_the_plugin_fails_closed(plugin_server, monkeypatch):
    _store, plans, _tmp = plugin_server
    monkeypatch.setenv(PLUGIN_ENV, REFUSING_SPEC)
    refusing = build_store()
    assert isinstance(refusing, PluginStore)
    monkeypatch.setattr(p, "_store", refusing)

    with pytest.raises(RuntimeError, match="audit log unavailable"):
        audit.record_audit(PLAN, action="plan:ingest", outcome="allowed")

    _assert_no_record_files(plans)


def test_an_ownership_change_is_refused_when_the_plugin_cannot_audit(plugin_server, monkeypatch):
    _store, plans, _tmp = plugin_server
    monkeypatch.setenv(PLUGIN_ENV, REFUSING_SPEC)
    monkeypatch.setattr(p, "_store", build_store())
    prior = {"owner": "alice", "delegates": []}

    # The denial cannot be audited, so the change is refused with the audit
    # failure itself, never answered as an unaudited ownership decision.
    with pytest.raises(RuntimeError, match="audit log unavailable"):
        ownership.resolve_ownership(PLAN, {"owner": "mallory"}, prior, "mallory")

    _assert_no_record_files(plans)


def test_the_plans_directory_check_fails_on_a_stray_record_file(plugin_server):
    _store, plans, _tmp = plugin_server
    (plans / f"{PLAN}.manifest.json").write_text("{}")

    with pytest.raises(AssertionError, match=r"\.manifest\.json"):
        _assert_no_record_files(plans)


# ---------------------------------------------------------------------------
# 4. notification enqueue and drain


def test_notification_enqueue_and_drain_go_through_the_plugin_outbox(plugin_server, monkeypatch):
    store, plans, _tmp = plugin_server
    monkeypatch.setenv(notification_outbox.OUTBOX_ENABLED_ENV, "1")
    records = store.records()
    event = {"plan": PLAN, "message": "plan done", "payload": {"event": "plan_completed"}}

    notification_outbox.outbox_sink(event)

    queued = records.read_outbox(DEFAULT_NAMESPACE, PLAN, notification_outbox.LEGACY_OUTBOX_CHANNEL)
    assert [entry["record"]["message"] for entry in queued] == ["plan done"]
    delivered = []
    assert notification_outbox.drain_outbox(PLAN, lambda record: delivered.append(record) or True) == 1
    assert delivered[0]["message"] == "plan done"
    assert records.read_outbox(DEFAULT_NAMESPACE, PLAN, notification_outbox.LEGACY_OUTBOX_CHANNEL) == []
    _assert_no_record_files(plans)


def test_a_rejected_notification_stays_queued_in_the_plugin(plugin_server, monkeypatch):
    store, plans, _tmp = plugin_server
    monkeypatch.setenv(notification_outbox.OUTBOX_ENABLED_ENV, "1")
    records = store.records()
    notification_outbox.outbox_sink({"plan": PLAN, "message": "plan done", "payload": {"event": "plan_completed"}})

    assert notification_outbox.drain_outbox(PLAN, lambda record: False) == 0

    queued = records.read_outbox(DEFAULT_NAMESPACE, PLAN, notification_outbox.LEGACY_OUTBOX_CHANNEL)
    assert [entry["record"]["message"] for entry in queued] == ["plan done"]
    _assert_no_record_files(plans)


# ---------------------------------------------------------------------------
# 5. checkpoint


def test_checkpoint_reads_and_journals_through_the_plugin(plugin_server, monkeypatch, tmp_path):
    store, plans, _ingest_root = plugin_server
    _ingest(store, tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    store.update_story(PLAN, STORY, {"worktree": str(worktree)})
    monkeypatch.setattr(
        checkpoint, "_commit_wip", lambda wt, key, step, **kwargs: "abc1234"
    )

    result = p.checkpoint_story(PLAN, STORY, "step-1", "did the thing", "continue")

    assert result["ok"] is True, result
    journal = store.records().get_journal(DEFAULT_NAMESPACE, PLAN, STORY)
    assert [entry["step"] for entry in journal] == ["step-1"]
    assert journal[-1]["commit"] == "abc1234"
    _assert_no_record_files(plans)


# ---------------------------------------------------------------------------
# Negative: selection


def test_plugin_spec_alone_never_selects_the_plugin(monkeypatch):
    monkeypatch.delenv(STORE_ENV, raising=False)
    monkeypatch.setenv(PLUGIN_ENV, SHARED_SPEC)

    assert store_kind() == "file"
    assert isinstance(build_store(), FileStore)
