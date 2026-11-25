"""Recorded context sources reach the overlord's triage and high-risk merge prompts.

The overlord reads only the snapshots recorded on the story at dispatch (never
fetches), so a source reaches it only when it lists the ``dispatch`` phase and
also lists ``triage`` or ``merge_adjudication``. The context is REFERENCE DATA
that guides the ruling; it never grants an action.
"""
from __future__ import annotations

import logging

import pytest

import pipeline.server  # noqa: F401  (pre-existing import cycle: load server first)
from pipeline import bound_records, paths, persistence, server
from pipeline import context_sources as ctxsrc
from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import overlord_context as oc
from pipeline import triage as triage_mod

PLAN = "CTX-PLAN"
KEY = "CL-1"
POLICY_TEXT = "GLOBAL POLICY TEXT"
TEXT = "Our house rule: never merge without a rollback note."
EVIDENCE = "=== FAILURE EVIDENCE ===\nbuild failed: ModuleNotFoundError\n"
MERGE_STORY = {
    "key": KEY,
    "summary": "Rotate the signing key",
    "risk": "high",
    "review_verdict": "APPROVE",
    "security_review_verdict": "APPROVE",
    "pr_checks": {"ci": "pass", "state": "pass", "error": ""},
    "worktree": "",
}
TRIAGE_STORY = {"key": KEY, "status": "parked", "parked_reason": "lint failed"}


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(triage_mod, "_load_policy", lambda: POLICY_TEXT)
    monkeypatch.setattr(triage_mod, "_plan_role_config", lambda plan_name: {})
    monkeypatch.setattr(triage_mod, "_append_decision", lambda plan_name, record: None)
    monkeypatch.setattr(persistence, "_plan_role_config", lambda plan_name: {})
    monkeypatch.setattr(merge_mod, "_append_adjudication_decision", lambda plan_name, record: None)
    monkeypatch.setattr(overlord_mod, "_invoke_overlord", lambda prompt, plan_role_config=None: "RULING: proceed")


def _source(name="house", phases=("dispatch", "triage", "merge_adjudication"), **extra):
    return {
        "name": name,
        "provider": "dir",
        "uri": "/x",
        "mode": "prompt",
        "authority": extra.pop("authority", "policy"),
        "phases": list(phases),
        "required": extra.pop("required", True),
        **extra,
    }


def _manifest(sources, digests, max_total_chars=None):
    context = {"sources": sources}
    if max_total_chars is not None:
        context["max_total_chars"] = max_total_chars
    story = {"summary": "S", "status": "parked", "context_digests": digests}
    return {"context": context, "stories": {KEY: story}}


def _digest_entry(digest, text):
    return {"digest": digest, "bytes": len(text)}


def _use_manifest(monkeypatch, manifest):
    monkeypatch.setattr(bound_records, "get_manifest", lambda store, plan_name: manifest)


def _capture_triage(monkeypatch):
    captured = {}

    def fake_invoke(prompt, plan_role_config=None):
        captured["prompt"] = prompt
        return "RULING: proceed\nACTION: escalate_model"

    monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)
    monkeypatch.setattr(triage_mod, "_invoke_overlord", fake_invoke)
    return captured


def _capture_merge(monkeypatch):
    captured = {"records": []}

    def fake_invoke(prompt, plan_role_config=None):
        captured["prompt"] = prompt
        return "RULING: proceed\nRATIONALE: ok"

    monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)
    monkeypatch.setattr(
        merge_mod, "_append_adjudication_decision", lambda plan_name, record: captured["records"].append(record)
    )
    return captured


def _adjudicate():
    return merge_mod._adjudicate_high_risk_merge(dict(MERGE_STORY), PLAN)


def _triage():
    return triage_mod.rule_on_story(PLAN, KEY, dict(TRIAGE_STORY), EVIDENCE)


# ---------------------------------------------------------------------------
# phase validation
# ---------------------------------------------------------------------------


def test_should_reject_phase_other_than_triage_or_merge_adjudication(plan_dir):
    with pytest.raises(ValueError):
        oc.overlord_context(PLAN, KEY, "overlord")


def test_should_reject_review_phase_for_overlord_context(plan_dir):
    with pytest.raises(ValueError):
        oc.overlord_context(PLAN, KEY, "review")


# ---------------------------------------------------------------------------
# no sources: prompts are unchanged
# ---------------------------------------------------------------------------


def test_triage_prompt_without_sources_matches_prompt_with_empty_context(plan_dir, monkeypatch):
    _use_manifest(monkeypatch, {"stories": {KEY: {"status": "parked"}}})
    captured = _capture_triage(monkeypatch)
    _triage()
    real = captured["prompt"]

    monkeypatch.setattr(triage_mod, "overlord_context", lambda plan, key, phase: ("", []))
    _triage()
    assert real == captured["prompt"]
    assert "REFERENCE DATA" not in real


def test_merge_prompt_without_sources_matches_prompt_with_empty_context(plan_dir, monkeypatch):
    _use_manifest(monkeypatch, {"stories": {KEY: {"status": "pr_open"}}})
    captured = _capture_merge(monkeypatch)
    _adjudicate()
    real = captured["prompt"]

    monkeypatch.setattr(merge_mod, "overlord_context", lambda plan, key, phase: ("", []))
    _adjudicate()
    assert real == captured["prompt"]
    assert "REFERENCE DATA" not in real


# ---------------------------------------------------------------------------
# recorded policy source reaches triage and merge prompts
# ---------------------------------------------------------------------------


def test_policy_source_listing_triage_appears_after_policy_in_triage_prompt(plan_dir, monkeypatch):
    digest = ctxsrc.snapshot_context(TEXT)
    _use_manifest(monkeypatch, _manifest([_source(name="house")], {"house": _digest_entry(digest, TEXT)}))
    captured = _capture_triage(monkeypatch)
    _triage()
    prompt = captured["prompt"]
    assert TEXT in prompt
    assert prompt.index(POLICY_TEXT) < prompt.index(TEXT) < prompt.index("TRIAGE QUESTION")


def test_policy_source_listing_merge_adjudication_appears_in_merge_prompt(plan_dir, monkeypatch):
    digest = ctxsrc.snapshot_context(TEXT)
    _use_manifest(monkeypatch, _manifest([_source(name="house")], {"house": _digest_entry(digest, TEXT)}))
    captured = _capture_merge(monkeypatch)
    _adjudicate()
    prompt = captured["prompt"]
    assert TEXT in prompt
    assert prompt.index(TEXT) < prompt.index("Respond in this exact format:")


def test_source_listing_only_review_does_not_reach_either_overlord_prompt(plan_dir, monkeypatch):
    digest = ctxsrc.snapshot_context(TEXT)
    source = _source(name="house", phases=("dispatch", "review"))
    _use_manifest(monkeypatch, _manifest([source], {"house": _digest_entry(digest, TEXT)}))
    triage_captured = _capture_triage(monkeypatch)
    _triage()
    merge_captured = _capture_merge(monkeypatch)
    _adjudicate()
    assert TEXT not in triage_captured["prompt"]
    assert TEXT not in merge_captured["prompt"]


def test_source_not_recorded_at_dispatch_is_not_fetched_for_overlord(plan_dir, monkeypatch):
    ctxsrc.snapshot_context(TEXT)
    _use_manifest(monkeypatch, _manifest([_source(name="house")], {}))
    captured = _capture_triage(monkeypatch)
    _triage()
    assert TEXT not in captured["prompt"]


# ---------------------------------------------------------------------------
# budget: an optional source over the total budget is skipped
# ---------------------------------------------------------------------------


def test_optional_source_over_budget_is_skipped_and_prompt_stays_in_budget(plan_dir, monkeypatch):
    small = "Short convention."
    big = "B" * 5000
    small_digest = ctxsrc.snapshot_context(small)
    big_digest = ctxsrc.snapshot_context(big)
    sources = [
        _source(name="small", required=True, priority=0),
        _source(name="big", required=False, priority=1),
    ]
    digests = {"small": _digest_entry(small_digest, small), "big": _digest_entry(big_digest, big)}
    _use_manifest(monkeypatch, _manifest(sources, digests, max_total_chars=1500))

    block, block_digests = oc.overlord_context(PLAN, KEY, "triage")

    assert "skipped optional context source big" in block
    assert small in block
    assert big not in block
    assert block_digests == [small_digest]
    assert len(block) <= 1500 + 200


# ---------------------------------------------------------------------------
# recorded digests on the ruling
# ---------------------------------------------------------------------------


def test_triage_ruling_records_rendered_digests_and_context_free_evidence(plan_dir, monkeypatch):
    digest = ctxsrc.snapshot_context(TEXT)
    _use_manifest(monkeypatch, _manifest([_source(name="house")], {"house": _digest_entry(digest, TEXT)}))
    appended = []
    monkeypatch.setattr(triage_mod, "_append_decision", lambda plan_name, record: appended.append(record))
    _capture_triage(monkeypatch)
    _triage()
    record = appended[0]
    assert record["input"]["context_digests"] == [digest]
    assert TEXT not in record["input"]["evidence"]
    assert "TRIAGE QUESTION" in record["input"]["evidence"]


def test_merge_ruling_records_rendered_digests_and_context_free_evidence(plan_dir, monkeypatch):
    digest = ctxsrc.snapshot_context(TEXT)
    _use_manifest(monkeypatch, _manifest([_source(name="house")], {"house": _digest_entry(digest, TEXT)}))
    captured = _capture_merge(monkeypatch)
    _adjudicate()
    record = captured["records"][0]
    assert record["input"]["context_digests"] == [digest]
    assert TEXT not in record["input"]["evidence"]
    assert "MERGE ADJUDICATION" in record["input"]["evidence"]


def test_merge_ruling_without_context_records_no_digests(plan_dir, monkeypatch):
    _use_manifest(monkeypatch, {"stories": {KEY: {"status": "pr_open"}}})
    captured = _capture_merge(monkeypatch)
    _adjudicate()
    assert captured["records"][0]["input"]["context_digests"] == []


# ---------------------------------------------------------------------------
# failure: the ruling still happens, one WARNING without the exception text
# ---------------------------------------------------------------------------


def test_manifest_read_failure_returns_empty_context_and_ruling_still_happens(plan_dir, monkeypatch, caplog):
    def broken(store, plan_name):
        raise RuntimeError("token-xyz-secret")

    monkeypatch.setattr(bound_records, "get_manifest", broken)
    captured = _capture_triage(monkeypatch)
    assert oc.overlord_context(PLAN, KEY, "triage") == ("", [])
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        ruling = _triage()

    assert "prompt" in captured
    assert ruling["failed_open"] is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()
    assert "token-xyz-secret" not in warnings[0].getMessage()


def test_missing_manifest_returns_empty_context(plan_dir, monkeypatch):
    _use_manifest(monkeypatch, None)
    assert oc.overlord_context(PLAN, KEY, "merge_adjudication") == ("", [])


# ---------------------------------------------------------------------------
# config validation accepts the two new phases and still rejects overlord
# ---------------------------------------------------------------------------


def test_validate_context_config_accepts_triage_and_merge_adjudication():
    from pipeline.context_sources import validate_context_config

    context = {"sources": [_source(phases=("dispatch", "triage", "merge_adjudication"))]}
    assert validate_context_config(context) == []


def test_validate_context_config_still_rejects_overlord_phase():
    from pipeline.context_sources import validate_context_config

    errors = validate_context_config({"sources": [_source(phases=("dispatch", "overlord"))]})
    assert any("unsupported phase 'overlord'" in error for error in errors)


# ---------------------------------------------------------------------------
# docs name the new phase
# ---------------------------------------------------------------------------


def test_reference_doc_names_merge_adjudication_phase():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "REFERENCE.md").read_text()
    assert "merge_adjudication" in text


def test_adr_0005_names_merge_adjudication_phase():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "docs" / "adr" / "0005-external-context-sources.md").read_text()
    assert "merge_adjudication" in text
