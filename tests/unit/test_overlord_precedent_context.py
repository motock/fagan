"""Precedent reaches the overlord's triage and high-risk merge prompts, and its digest reaches the ruling."""

from __future__ import annotations

import hashlib
import logging
from types import SimpleNamespace

import pytest

import pipeline.server  # noqa: F401  (pre-existing import cycle: load server first)
from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import overlord_context as overlord_context_mod
from pipeline import overlord_precedent as precedent
from pipeline import persistence, server
from pipeline import triage as triage_mod
from pipeline.actor import bind_namespace
from pipeline.settings_registry import REGISTRY
from pipeline.store_memory import InMemoryRecordStore

PLAN = "PREC-CTX-PLAN"
KEY = "CL-9"
EVIDENCE = "=== FAILURE EVIDENCE ===\nbuild failed: ModuleNotFoundError\n"
STORY = {"key": KEY, "status": "parked", "parked_reason": "lint failed"}
MERGE_STORY = {"key": KEY, "status": "pr_open", "risk": "high", "worktree": ""}


def _triage_class() -> str:
    return precedent.observed_park_class(STORY)


@pytest.fixture
def store(monkeypatch):
    memory = InMemoryRecordStore()
    monkeypatch.setattr(server, "_store", SimpleNamespace(records=lambda: memory))
    memory.put_manifest("default", PLAN, {"stories": {KEY: dict(STORY)}})
    return memory


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    precedent.clear_cache()
    monkeypatch.setattr(triage_mod, "_load_policy", lambda: "GLOBAL POLICY")
    monkeypatch.setattr(triage_mod, "_plan_role_config", lambda plan_name: {})
    monkeypatch.setattr(triage_mod, "_append_decision", lambda plan_name, record: None)
    monkeypatch.setattr(persistence, "_plan_role_config", lambda plan_name: {})
    monkeypatch.setattr(merge_mod, "_append_adjudication_decision", lambda plan_name, record: None)
    yield
    precedent.clear_cache()


def _seed_triage_pair(store, namespace="default"):
    cls = _triage_class()
    store.append_decision(namespace, PLAN, {
        "question": "failure triage", "story_key": "CL-OLD", "action": "park_for_human",
        "park_class": cls, "decided_at": "2026-11-20T10:00:00+00:00",
    })
    store.append_decision(namespace, PLAN, {
        "question": "human resolution", "story_key": "CL-OLD", "ruling_ref": "2026-11-20T10:00:00+00:00",
        "human_action": "retry", "rationale": "the lint job was flaky",
    })


def _capture_triage(monkeypatch, appended):
    captured = {}

    def fake_invoke(prompt, plan_role_config=None):
        captured["prompt"] = prompt
        return "RULING: proceed\nACTION: escalate_model"

    monkeypatch.setattr(triage_mod, "_invoke_overlord", fake_invoke)
    monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)
    monkeypatch.setattr(triage_mod, "_append_decision", lambda plan_name, record: appended.append(record))
    return captured


def _rule():
    return triage_mod.rule_on_story(PLAN, KEY, dict(STORY), EVIDENCE)


def test_triage_prompt_shows_the_paired_answer_as_precedent_before_the_question(store, monkeypatch):
    _seed_triage_pair(store)
    captured = _capture_triage(monkeypatch, [])
    _rule()
    prompt = captured["prompt"]
    assert "the lint job was flaky" in prompt
    assert "story CL-OLD" in prompt
    assert prompt.index("PRECEDENT") < prompt.index("TRIAGE QUESTION")


def test_ruling_snapshot_records_the_precedent_digest_and_the_text_is_recoverable(store, monkeypatch):
    _seed_triage_pair(store)
    appended = []
    captured = _capture_triage(monkeypatch, appended)
    _rule()
    digests = appended[0]["input"]["context_digests"]
    assert len(digests) == 1
    snapshot = store.get_context_snapshot("default", digests[0])
    assert snapshot is not None
    assert snapshot["content"] in captured["prompt"]
    assert digests[0] == "sha256:" + hashlib.sha256(snapshot["content"].encode("utf-8")).hexdigest()


def test_high_risk_merge_prompt_shows_paired_merge_answers_as_precedent(store, monkeypatch):
    store.append_decision("default", PLAN, {
        "question": "high-risk merge adjudication", "story_key": "CL-OLD", "ruling": "proceed",
        "decided_at": "2026-11-21T09:00:00+00:00",
    })
    store.append_decision("default", PLAN, {
        "question": "human resolution", "story_key": "CL-OLD", "ruling_ref": "2026-11-21T09:00:00+00:00",
        "human_action": "merge_held", "rationale": "the rollback note was missing",
    })
    captured = {}

    def fake_invoke(prompt, plan_role_config=None):
        captured["prompt"] = prompt
        return "RULING: proceed\nRATIONALE: ok"

    monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)
    # Pin the CI read: unstubbed, the gather asks the host's gh, and a pending
    # or unreadable read defers the ruling before the overlord is called.
    monkeypatch.setattr(server, "_ci_status_once", lambda branch, **_: {"state": "pass", "error": ""})
    merge_mod._adjudicate_high_risk_merge(dict(MERGE_STORY), PLAN)
    assert "the rollback note was missing" in captured["prompt"]
    assert "PRECEDENT" in captured["prompt"]


def test_precedent_from_another_namespace_never_reaches_the_prompt(store, monkeypatch):
    _seed_triage_pair(store, namespace="default")
    store.put_manifest("tenant-b", PLAN, {"stories": {KEY: dict(STORY)}})
    captured = _capture_triage(monkeypatch, [])
    with bind_namespace("tenant-b"):
        _rule()
    assert "the lint job was flaky" not in captured["prompt"]
    assert "PRECEDENT" not in captured["prompt"]


def test_precedent_read_failure_logs_one_warning_and_the_ruling_still_proceeds(store, monkeypatch, caplog):
    def broken(park_class, limit):
        raise RuntimeError("token-xyz-secret")

    monkeypatch.setattr(precedent, "precedent_entries", broken)
    captured = _capture_triage(monkeypatch, [])
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        ruling = _rule()
    assert "prompt" in captured
    assert "PRECEDENT" not in captured["prompt"]
    assert ruling["failed_open"] is False
    warnings = [r.getMessage() for r in caplog.records if "precedent" in r.getMessage()]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0]
    assert "token-xyz-secret" not in warnings[0]


def test_limit_zero_adds_no_precedent_block_and_no_digest(store, monkeypatch):
    _seed_triage_pair(store)
    monkeypatch.setattr(precedent, "effective", lambda key, legacy, context: 0)
    appended = []
    captured = _capture_triage(monkeypatch, appended)
    _rule()
    assert "PRECEDENT" not in captured["prompt"]
    assert appended[0]["input"]["context_digests"] == []


def test_limit_one_shows_only_the_newest_paired_answer(store, monkeypatch):
    _seed_triage_pair(store)
    cls = _triage_class()
    store.append_decision("default", PLAN, {
        "question": "failure triage", "story_key": "CL-NEW", "action": "park_for_human",
        "park_class": cls, "decided_at": "2026-11-24T10:00:00+00:00",
    })
    store.append_decision("default", PLAN, {
        "question": "human resolution", "story_key": "CL-NEW", "ruling_ref": "2026-11-24T10:00:00+00:00",
        "human_action": "park", "rationale": "newest answer",
    })
    monkeypatch.setattr(precedent, "effective", lambda key, legacy, context: 1)
    captured = _capture_triage(monkeypatch, [])
    _rule()
    assert "newest answer" in captured["prompt"]
    assert "the lint job was flaky" not in captured["prompt"]


def test_setting_is_a_tenant_int_preference_that_defaults_to_eight_and_accepts_zero_to_fifty():
    key = REGISTRY["overlord_precedent_limit"]
    assert (key.kind, key.type, key.safe_value, key.bounds) == ("pref", "int", 8, (0, 50))
    assert key.accepts(0) and key.accepts(50)
    assert not key.accepts(-1) and not key.accepts(51) and not key.accepts(True)


def test_precedent_still_reaches_the_ruling_when_the_context_render_fails(store, monkeypatch):
    _seed_triage_pair(store)

    def broken_render(manifest, story_key, phase):
        raise OSError("snapshot store unreachable")

    monkeypatch.setattr(overlord_context_mod, "_render", broken_render)
    captured = _capture_triage(monkeypatch, [])
    _rule()
    assert "the lint job was flaky" in captured["prompt"]


def test_empty_context_with_rendered_precedent_logs_no_unavailable_warning(store, monkeypatch, caplog):
    _seed_triage_pair(store)
    _capture_triage(monkeypatch, [])
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        _rule()
    unavailable = [r.getMessage() for r in caplog.records if "overlord context unavailable" in r.getMessage()]
    assert unavailable == []
