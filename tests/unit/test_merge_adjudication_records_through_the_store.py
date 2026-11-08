"""STORE-B1b: a merge adjudication ruling is recorded through the record store.

``merge._adjudicate_high_risk_merge`` appended the high-risk merge ruling with
``persistence._append_decision`` directly, spelling out the plan-directory
decisions log at the call site. That is the last decision write still bypassing
the store seam (ADR 0012 stage B: "manifest and decisions" move off direct
``PLAN_DIR`` use one module family at a time), so a deployment whose
``PIPELINE_STORE`` selects a store with its own decision storage loses every
merge-gate ruling to a JSON file on this host.

The contract these tests pin down:

* the ruling is appended through the store's namespaced record view -
  ``_store.records().append_decision(DEFAULT_NAMESPACE, plan_name, record)`` -
  with the adjudication's plan name and the ruling's identifiers intact;
* the file-store seam keeps the one legacy writer (its record view delegates to
  ``persistence._append_decision``, so the ruling still lands in
  ``<plan>.decisions.json``);
* a store with no ``records()`` view (an in-process stand-in) and a record view
  that refuses the plan key both fall back to that legacy writer, so an
  unrecorded ruling is impossible;
* a record store that raises never breaks the merge gate: the ruling's action
  is unchanged and the failure is logged against the story.

The persistent state is the store's decision list per plan: it accumulates
across adjudications (two rulings are two records, order preserved) and a
rejected/raising write must leave it exactly as it was.
"""
import json
import logging

import pytest

from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod
from pipeline import server as server_mod
from pipeline.store_records import DEFAULT_NAMESPACE

PLAN = "STORE-B1B-PLAN"
STORY_KEY = "STORE-B1B-KEY"
PROCEED_REPLY = "RULING: proceed\nRATIONALE: risk accepted, checks are green"
PARK_REPLY = "RULING: park\nRATIONALE: needs a human"
HOLD_REASON = "high risk held for human review"


class _FakeRecords:
    """A namespaced record view that keeps its decisions in memory."""

    def __init__(self, raises=None):
        self.calls = []
        self.decisions = []
        self.raises = raises

    def append_decision(self, namespace, plan_name, record):
        self.calls.append((namespace, plan_name, dict(record)))
        if self.raises is not None:
            raise self.raises
        self.decisions.append((namespace, plan_name, dict(record)))

    def get_decisions(self, namespace, plan_name):
        return [record for ns, name, record in self.decisions if ns == namespace]


class _FakeStore:
    """A stand-in store with a namespaced record view."""

    def __init__(self, records):
        self._records = records

    def records(self):
        return self._records


class _NoRecordsStore:
    """An in-process stand-in with neither the record view nor a decision write."""


class _LegacyOnlyStore:
    """A stand-in carrying only the legacy ``append_decision(plan, record)``."""

    def __init__(self):
        self.decisions = []

    def append_decision(self, plan_name, record):
        self.decisions.append((plan_name, dict(record)))


def _high_risk_story(**overrides):
    story = {
        "status": "pr_open",
        "parked_reason": HOLD_REASON,
        "review_verdict": "APPROVE",
        "security_review_verdict": "PASS",
        "risk": "high",
        "summary": "Rewrite the auth token cache",
        # Truthy, so the CI gather returns early instead of probing a branch
        # for a story that has no worktree.
        "pr_checks": {"ci": "pass"},
        "worktree": "",
    }
    story.update(overrides)
    return story


@pytest.fixture
def adjudication(monkeypatch):
    """Drive the real high-risk adjudication with a stubbed overlord."""
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full", raising=False)
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low", raising=False)
    monkeypatch.setattr(
        persistence_mod,
        "_plan_role_config",
        lambda plan_name: {"role": "overlord", "model": "opus"},
    )

    def stub(reply):
        monkeypatch.setattr(
            overlord_mod, "_invoke_overlord", lambda prompt, plan_role_config=None: reply
        )

    stub(PROCEED_REPLY)

    def run(story=None):
        with merge_mod.merge_adjudication_plan(PLAN), merge_mod.merge_adjudication_story(
            STORY_KEY
        ):
            return merge_mod._adjudicate_high_risk_merge(
                story if story is not None else _high_risk_story(), PLAN
            )

    run.stub = stub
    return run


def _decisions_file(plan_dir):
    return plan_dir / f"{PLAN}.decisions.json"


# --------------------------------------------------------------------------
# 1. the seam: the ruling goes through the record store
# --------------------------------------------------------------------------


def test_ruling_is_appended_through_the_record_store(plan_dir, monkeypatch, adjudication):
    """The store's namespaced record view receives the ruling.

    The plan directory stays empty: the store, not a file write at the call
    site, is what recorded the decision.
    """
    records = _FakeRecords()
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)

    decision = adjudication()

    assert decision == {"action": "merge", "reason": "overlord ruled proceed"}
    assert len(records.calls) == 1, "the ruling must be recorded exactly once"
    namespace, plan_name, record = records.calls[0]
    assert namespace == DEFAULT_NAMESPACE
    assert plan_name == PLAN
    assert record["story_key"] == STORY_KEY
    assert record["question"] == "high-risk merge adjudication"
    assert record["ruling"] == "proceed"
    assert record["rationale"] == "risk accepted, checks are green"
    assert record["decided_by"] == "overlord"
    assert record["decided_at"], "a ruling is timestamped"
    assert not list(plan_dir.glob("*decisions*")), (
        "the store recorded the ruling; no file write at the call site"
    )


def test_a_park_ruling_is_recorded_with_its_rationale(plan_dir, monkeypatch, adjudication):
    records = _FakeRecords()
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)
    adjudication.stub(PARK_REPLY)

    decision = adjudication()

    assert decision == {"action": "park", "reason": HOLD_REASON}
    assert [record["ruling"] for record in records.get_decisions(DEFAULT_NAMESPACE, PLAN)] == [
        "park"
    ]
    assert records.calls[0][2]["rationale"] == "needs a human"


def test_two_adjudications_keep_both_records_in_order(
    plan_dir, monkeypatch, adjudication
):
    """The store's decision list persists and accumulates across calls."""
    records = _FakeRecords()
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)

    adjudication()
    adjudication.stub(PARK_REPLY)
    adjudication()

    rulings = [record["ruling"] for record in records.get_decisions(DEFAULT_NAMESPACE, PLAN)]
    assert rulings == ["proceed", "park"]


# --------------------------------------------------------------------------
# 2. fallbacks: a ruling is never lost
# --------------------------------------------------------------------------


def test_file_store_seam_writes_the_plan_decisions_log(plan_dir, adjudication):
    """The default seam is the file store; its record view is the legacy writer.

    No store double here: ``pipeline.server._store`` is the real built store,
    so this pins the production wiring end to end.
    """
    decision = adjudication()

    assert decision == {"action": "merge", "reason": "overlord ruled proceed"}
    records = json.loads(_decisions_file(plan_dir).read_text())
    assert len(records) == 1
    assert records[0]["story_key"] == STORY_KEY
    assert records[0]["ruling"] == "proceed"


def test_store_without_a_record_view_falls_back_to_the_legacy_writer(
    plan_dir, monkeypatch, adjudication
):
    """An in-process stand-in with no ``records()`` view still records the ruling."""
    monkeypatch.setattr(server_mod, "_store", _NoRecordsStore(), raising=False)

    adjudication()

    records = json.loads(_decisions_file(plan_dir).read_text())
    assert [record["ruling"] for record in records] == ["proceed"]


def test_a_store_with_only_the_legacy_surface_still_records_the_ruling(
    plan_dir, monkeypatch, adjudication
):
    """A store without the namespaced view owns the record through its own API."""
    store = _LegacyOnlyStore()
    monkeypatch.setattr(server_mod, "_store", store, raising=False)

    adjudication()

    assert [plan for plan, _record in store.decisions] == [PLAN]
    assert store.decisions[0][1]["ruling"] == "proceed"
    assert not _decisions_file(plan_dir).exists(), (
        "the store recorded it; the legacy file writer must not also run"
    )


def test_a_rejected_plan_key_falls_back_to_the_legacy_writer(
    plan_dir, monkeypatch, adjudication
):
    """A record view that refuses the key leaves the ruling unrecorded otherwise."""
    records = _FakeRecords(raises=ValueError("invalid plan name"))
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)

    adjudication()

    assert [call[1] for call in records.calls] == [PLAN], (
        "the record store is asked first"
    )
    assert records.decisions == [], "the refusal recorded nothing"
    written = json.loads(_decisions_file(plan_dir).read_text())
    assert [record["ruling"] for record in written] == ["proceed"]


def test_a_failing_record_store_never_breaks_the_gate(
    plan_dir, monkeypatch, adjudication, caplog
):
    """A store that raises must not flip the ruling or lose the story's audit.

    The action is the ruling's own (proceed → merge), the store kept nothing,
    and the failure is logged naming the story rather than swallowed silently.
    """
    records = _FakeRecords(raises=OSError("store unavailable"))
    monkeypatch.setattr(server_mod, "_store", _FakeStore(records), raising=False)

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        decision = adjudication()

    assert decision == {"action": "merge", "reason": "overlord ruled proceed"}
    assert records.decisions == []
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        f"failed to append merge adjudication record for {STORY_KEY}" in message
        for message in messages
    ), messages
