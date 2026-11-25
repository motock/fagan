"""Tests for the backfill of inferred human answers to past overlord rulings.

A ruling with no human resolution says what the machine decided but not what
a person did next. ``pipeline.triage_backfill`` reads the story's own history
(audit records, notifications, operator judgments, the story's status) and
infers the human action only when the history says so with certainty. A
ruling whose history is ambiguous is counted as undetermined and not written.

Every history here is fabricated, and the fake store only answers reads, so a
test that asserts "nothing was written" also proves the backfill never touched
a story or a plan.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from pipeline import decision_log
from pipeline import persistence as ppers
from pipeline import triage_backfill as tb

_PLAN = "forge"
_STORY = "FORGE-S1"
_RULED_AT = "2026-11-10T09:00:00+00:00"


def _ruling(
    *,
    action: str = "park_for_human",
    park_class: str = "rebase_conflict",
    decided_at: str = _RULED_AT,
    story: str = _STORY,
) -> dict:
    return {
        "question": "failure triage",
        "story_key": story,
        "action": action,
        "park_class": park_class,
        "decided_at": decided_at,
    }


def _at(hour: int) -> datetime:
    return datetime(2026, 11, 10 + (hour // 24), hour % 24, tzinfo=UTC)


def _event(kind: str, hour: int = 12, decision_class: str = "") -> tb.HistoryEvent:
    return tb.HistoryEvent(ts=_at(hour), kind=kind, decision_class=decision_class)


# --- inference from a fabricated history -----------------------------------


def test_merge_by_a_person_after_the_ruling_is_merge_held() -> None:
    assert tb.infer_human_action(_ruling(), [_event("merged")], "done") == "merge_held"


def test_brief_patched_by_a_person_after_the_ruling_is_rebrief() -> None:
    assert tb.infer_human_action(_ruling(), [_event("brief_patched")], "todo") == "rebrief"


def test_scope_widen_judgment_by_a_person_is_amend_scope() -> None:
    events = [_event("judgment", decision_class="scope_widen")]
    assert tb.infer_human_action(_ruling(), events, "todo") == "amend_scope"


def test_existing_test_edit_judgment_by_a_person_is_approve_test_edit() -> None:
    events = [_event("judgment", decision_class="existing_test_edit")]
    assert tb.infer_human_action(_ruling(), events, "todo") == "approve_test_edit"


def test_high_risk_merge_judgment_by_a_person_is_merge_held() -> None:
    events = [_event("judgment", decision_class="high_risk_merge")]
    assert tb.infer_human_action(_ruling(), events, "done") == "merge_held"


def test_dispatch_by_a_person_after_the_ruling_is_retry() -> None:
    assert tb.infer_human_action(_ruling(), [_event("dispatched")], "interrupted") == "retry"


def test_repeated_dispatches_by_a_person_are_still_one_action() -> None:
    events = [_event("dispatched", 12), _event("dispatched", 13)]
    assert tb.infer_human_action(_ruling(), events, "interrupted") == "retry"


def test_unpark_by_a_person_is_undetermined_because_it_may_have_widened_scope() -> None:
    # An unpark with --add-files looks the same as one without: certainty fails.
    assert tb.infer_human_action(_ruling(), [_event("unparked")], "todo") is None


def test_still_parked_with_no_human_event_is_park() -> None:
    assert tb.infer_human_action(_ruling(), [], "parked") == "park"


def test_no_human_event_and_not_parked_is_undetermined() -> None:
    # A story moved off park with no person on record may be the machine's own retry.
    assert tb.infer_human_action(_ruling(), [], "todo") is None


def test_human_events_before_the_ruling_are_ignored() -> None:
    events = [_event("merged", hour=8)]
    assert tb.infer_human_action(_ruling(), events, "parked") == "park"


def test_merge_and_rebrief_after_the_ruling_are_undetermined() -> None:
    events = [_event("merged", 12), _event("brief_patched", 13)]
    assert tb.infer_human_action(_ruling(), events, "done") is None


def test_unknown_judgment_class_is_undetermined() -> None:
    events = [_event("judgment", decision_class="something_new")]
    assert tb.infer_human_action(_ruling(), events, "todo") is None


def test_event_with_no_readable_timestamp_is_undetermined() -> None:
    unknown_time = tb.HistoryEvent(ts=None, kind="merged")
    assert tb.infer_human_action(_ruling(), [unknown_time], "parked") is None


def test_ruling_with_no_readable_decided_at_is_undetermined() -> None:
    assert tb.infer_human_action(_ruling(decided_at="not-a-time"), [], "parked") is None


# --- reading the story's history -------------------------------------------


class _Chain:
    def __init__(self, chains: dict[str, list]) -> None:
        self._chains = chains
        self.reads: list[tuple[str, str]] = []

    def read_chain(self, namespace: str, chain: str) -> list:
        self.reads.append((namespace, chain))
        return self._chains.get(chain, [])


class _Records:
    def __init__(self, notices: list[dict]) -> None:
        self._notices = notices

    def get_notification_history(self, namespace: str, plan_name: str) -> tuple[list, int]:
        return list(self._notices), 0


class _Store:
    """Read-only stand-in for the configured store: every write attribute is absent."""

    def __init__(self, *, audit: list, notices: list[dict], stories: dict | None = None) -> None:
        self._chain = _Chain({_PLAN: audit})
        self._records = _Records(notices)
        self._manifest = {"plan": _PLAN, "stories": stories or {}}

    def audit_log(self) -> _Chain:
        return self._chain

    def records(self) -> _Records:
        return self._records

    def get_manifest(self, plan_name: str) -> dict:
        return self._manifest


def _audit(action: str, *, principal: str = "human", story: str = _STORY, ts: str = "2026-11-11T10:00:00+00:00") -> dict:
    return {
        "action": action,
        "ts": ts,
        "principal_type": principal,
        "target": {"plan": _PLAN, "story": story},
        "outcome": "allowed",
    }


def _notice(event: str, *, story: str = _STORY, ts: str = "2026-11-11T10:00:00+00:00") -> dict:
    return {"event": event, "story_key": story, "ts": ts, "message": "x"}


def _judgment(decision_class: str, *, tier: str = "human", story: str = _STORY, ts: str = "2026-11-11T10:00:00+00:00") -> dict:
    return {"class": decision_class, "tier": tier, "subject": {"plan": _PLAN, "story": story}, "ts": ts}


def test_collects_a_person_merge_and_ignores_a_machine_merge() -> None:
    store = _Store(audit=[_audit("story.merge"), _audit("story.merge", principal="system", ts="2026-11-11T11:00:00+00:00")], notices=[])
    events = tb.collect_events(store, _PLAN, _STORY, [])
    assert [e.kind for e in events] == ["merged"]


def test_ignores_audit_and_notices_for_other_stories() -> None:
    store = _Store(
        audit=[_audit("story.merge", story="FORGE-S2")],
        notices=[_notice("brief_patched", story="FORGE-S2")],
    )
    assert tb.collect_events(store, _PLAN, _STORY, []) == []


def test_collects_brief_patch_and_manual_unpark_but_not_machine_notices() -> None:
    notices = [
        _notice("brief_patched"),
        _notice("story_unparked_manually", ts="2026-11-11T12:00:00+00:00"),
        _notice("story_rebriefed_by_overlord", ts="2026-11-11T13:00:00+00:00"),
        _notice("story_merged", ts="2026-11-11T14:00:00+00:00"),
    ]
    kinds = sorted(e.kind for e in tb.collect_events(_Store(audit=[], notices=notices), _PLAN, _STORY, []))
    assert kinds == ["brief_patched", "unparked"]


def test_collects_only_human_tier_judgments_for_the_story() -> None:
    decisions = [
        _judgment("scope_widen"),
        _judgment("scope_widen", tier="auto"),
        _judgment("scope_widen", story="FORGE-S2"),
    ]
    events = tb.collect_events(_Store(audit=[], notices=[]), _PLAN, _STORY, decisions)
    assert [(e.kind, e.decision_class) for e in events] == [("judgment", "scope_widen")]


def test_unreadable_timestamp_on_a_relevant_record_is_kept_as_unknown_time() -> None:
    store = _Store(audit=[_audit("story.merge", ts="yesterday-ish")], notices=[])
    events = tb.collect_events(store, _PLAN, _STORY, [])
    assert [(e.kind, e.ts) for e in events] == [("merged", None)]


# --- the backfill run ------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    # Keep the writer and the reader on the legacy decisions file, as the
    # outcome tests do, so nothing reaches a configured store.
    monkeypatch.setattr(decision_log, "store_owns_records", lambda: False)
    return d


def _write_decisions(records: list[dict]) -> None:
    path = ppers._decisions_path(_PLAN)
    path.write_text(json.dumps(records))


def _decisions() -> list[dict]:
    return json.loads(ppers._decisions_path(_PLAN).read_text())


def _resolutions() -> list[dict]:
    return [r for r in _decisions() if r.get("question") == "human resolution"]


def _store_with_merge() -> _Store:
    stories = {_STORY: {"status": "done"}}
    return _Store(audit=[_audit("story.merge")], notices=[], stories=stories)


def test_dry_run_writes_nothing_and_reports_the_inference(plan_dir) -> None:
    _write_decisions([_ruling()])
    before = ppers._decisions_path(_PLAN).read_bytes()

    report = tb.backfill([_PLAN], since=datetime(2026, 11, 1).date(), apply=False, store=_store_with_merge())

    assert ppers._decisions_path(_PLAN).read_bytes() == before
    assert report["rebase_conflict"]["inferred"] == {"merge_held": 1}
    assert report["rebase_conflict"]["rulings"] == 1


def test_apply_writes_an_inferred_resolution_linked_to_its_ruling(plan_dir) -> None:
    _write_decisions([_ruling()])

    tb.backfill([_PLAN], since=datetime(2026, 11, 1).date(), apply=True, store=_store_with_merge())

    (resolution,) = _resolutions()
    assert resolution["ruling_ref"] == _RULED_AT
    assert resolution["human_action"] == "merge_held"
    assert resolution["source"] == "backfill"
    assert resolution["inferred"] is True
    assert resolution["story_key"] == _STORY


def test_apply_is_idempotent_a_second_run_writes_nothing(plan_dir) -> None:
    _write_decisions([_ruling()])
    since = datetime(2026, 11, 1).date()
    tb.backfill([_PLAN], since=since, apply=True, store=_store_with_merge())
    after_first = ppers._decisions_path(_PLAN).read_bytes()

    report = tb.backfill([_PLAN], since=since, apply=True, store=_store_with_merge())

    assert ppers._decisions_path(_PLAN).read_bytes() == after_first
    assert report == {}


def test_ruling_with_a_human_resolution_is_not_backfilled(plan_dir) -> None:
    ruling = _ruling()
    human = {"question": "human resolution", "story_key": _STORY, "ruling_ref": _RULED_AT, "human_action": "retry"}
    _write_decisions([ruling, human])

    report = tb.backfill([_PLAN], since=datetime(2026, 11, 1).date(), apply=True, store=_store_with_merge())

    assert len(_resolutions()) == 1
    assert report == {}


def test_ruling_before_the_since_date_is_left_out(plan_dir) -> None:
    _write_decisions([_ruling(decided_at="2026-10-01T09:00:00+00:00")])

    report = tb.backfill([_PLAN], since=datetime(2026, 11, 1).date(), apply=True, store=_store_with_merge())

    assert report == {}
    assert _resolutions() == []


def test_undetermined_ruling_is_counted_and_not_written(plan_dir) -> None:
    _write_decisions([_ruling()])
    store = _Store(audit=[], notices=[_notice("story_unparked_manually")], stories={_STORY: {"status": "todo"}})

    report = tb.backfill([_PLAN], since=datetime(2026, 11, 1).date(), apply=True, store=store)

    assert report["rebase_conflict"]["undetermined"] == 1
    assert report["rebase_conflict"]["inferred"] == {}
    assert _resolutions() == []


def test_backfill_never_changes_the_ruling_or_the_manifest(plan_dir) -> None:
    ruling = _ruling()
    _write_decisions([ruling])
    tb.backfill([_PLAN], since=datetime(2026, 11, 1).date(), apply=True, store=_store_with_merge())

    assert [r for r in _decisions() if r.get("question") == "failure triage"] == [ruling]


def test_backfill_report_is_empty_when_there_are_no_rulings(plan_dir) -> None:
    _write_decisions([])
    report = tb.backfill([_PLAN], since=datetime(2026, 11, 1).date(), apply=False, store=_Store(audit=[], notices=[]))
    assert report == {}


# --- the command line ------------------------------------------------------


def test_cli_rejects_a_since_date_that_is_not_a_date() -> None:
    with pytest.raises(SystemExit) as refused:
        tb.main(["--since", "last-tuesday"])
    assert refused.value.code == 2


def test_cli_dry_run_prints_a_table_and_writes_nothing(plan_dir, monkeypatch, capsys) -> None:
    _write_decisions([_ruling()])
    monkeypatch.setattr(tb, "_configured_store", lambda: _store_with_merge())
    before = ppers._decisions_path(_PLAN).read_bytes()

    assert tb.main(["--since", "2026-11-01", "--plan", _PLAN]) == 0

    out = capsys.readouterr().out
    assert "rebase_conflict" in out
    assert "merge_held" in out
    assert ppers._decisions_path(_PLAN).read_bytes() == before


def test_cli_apply_writes_the_inferred_resolution(plan_dir, monkeypatch, capsys) -> None:
    _write_decisions([_ruling()])
    monkeypatch.setattr(tb, "_configured_store", lambda: _store_with_merge())

    assert tb.main(["--since", "2026-11-01", "--plan", _PLAN, "--apply"]) == 0

    assert len(_resolutions()) == 1
