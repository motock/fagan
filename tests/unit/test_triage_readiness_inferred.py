"""Tests for how the readiness report treats inferred human resolutions.

A ``human resolution`` written by the backfill carries ``inferred: true``. The
report keeps those pairings apart: they are counted in ``paired_inferred`` and
their agree / false-proceed / false-park outcomes are reported beside the
human-made ones. They count toward the thresholds only when the caller asks
for ``include_inferred``; promotion stays a human decision.

Every record here is synthetic, so the thresholds are not tied to live data.
"""

from __future__ import annotations

import json

import pytest

from pipeline import server
from pipeline import triage_readiness as tr

_CLASS = "rebase_conflict"


def _ruling(key: str, *, action: str) -> dict:
    return {
        "question": "failure triage",
        "story_key": key,
        "action": action,
        "park_class": _CLASS,
        "decided_at": f"2026-11-01T00:00:{key}",
    }


def _resolution(ruling: dict, human_action: str, *, inferred: bool) -> dict:
    record = {
        "question": "human resolution",
        "story_key": ruling["story_key"],
        "ruling_ref": ruling["decided_at"],
        "ruling_action": ruling["action"],
        "park_class": ruling["park_class"],
        "human_action": human_action,
    }
    if inferred:
        record["source"] = "backfill"
        record["inferred"] = True
    return record


def _pair(key: str, *, ruling_action: str, human_action: str, inferred: bool) -> list[dict]:
    ruling = _ruling(key, action=ruling_action)
    return [ruling, _resolution(ruling, human_action, inferred=inferred)]


def _cls(report: dict) -> dict:
    return report[_CLASS]


# --- the report keeps inferred pairings apart -------------------------------


def test_inferred_pairing_is_reported_apart_from_human_pairing() -> None:
    records = _pair("A", ruling_action=tr.PARK_ACTION, human_action="park", inferred=True)

    verdict = _cls(tr.readiness(records))

    assert verdict["paired_inferred"] == 1
    assert verdict["agree_inferred"] == 1
    assert verdict["paired_count"] == 0


def test_inferred_pairing_counts_toward_shadow_but_not_paired_by_default() -> None:
    records = _pair("A", ruling_action=tr.PARK_ACTION, human_action="park", inferred=True)

    verdict = _cls(tr.readiness(records))

    assert verdict["shadow_count"] == 1
    assert verdict["paired_count"] == 0


def test_inferred_false_proceed_is_reported_as_false_proceed_inferred() -> None:
    records = _pair("A", ruling_action="retry", human_action="park", inferred=True)

    verdict = _cls(tr.readiness(records))

    assert verdict["false_proceed_inferred"] == 1
    assert verdict["false_proceed"] == 0


def test_inferred_false_park_is_reported_as_false_park_inferred() -> None:
    records = _pair("A", ruling_action=tr.PARK_ACTION, human_action="retry", inferred=True)

    verdict = _cls(tr.readiness(records))

    assert verdict["false_park_inferred"] == 1
    assert verdict["false_park"] == 0


def test_human_pairing_is_reported_without_inferred_counts() -> None:
    records = _pair("A", ruling_action=tr.PARK_ACTION, human_action="park", inferred=False)

    verdict = _cls(tr.readiness(records))

    assert verdict["paired_count"] == 1
    assert verdict["paired_inferred"] == 0


# --- the flag decides whether inferred pairs count --------------------------


def test_include_inferred_counts_inferred_pairs_toward_the_verdict() -> None:
    records = _pair("A", ruling_action=tr.PARK_ACTION, human_action="park", inferred=True)

    verdict = _cls(tr.readiness(records, include_inferred=True))

    assert verdict["paired_count"] == 1
    assert verdict["agree"] == 1
    assert verdict["paired_inferred"] == 1


def test_include_inferred_lets_an_inferred_false_proceed_block_the_class() -> None:
    records = _pair("A", ruling_action="retry", human_action="park", inferred=True)

    verdict = _cls(tr.readiness(records, include_inferred=True))

    assert verdict["false_proceed"] == 1
    assert verdict["eligible"] is False


def test_inferred_false_proceed_does_not_block_the_class_by_default() -> None:
    records = _pair("A", ruling_action="retry", human_action="park", inferred=True)

    verdict = _cls(tr.readiness(records))

    assert verdict["false_proceed"] == 0
    assert not any("false proceed" in blocker for blocker in verdict["blockers"])


def test_include_inferred_leaves_human_pairs_unchanged() -> None:
    records = _pair("A", ruling_action=tr.PARK_ACTION, human_action="retry", inferred=False)

    without = _cls(tr.readiness(records))
    with_flag = _cls(tr.readiness(records, include_inferred=True))

    assert without["false_park"] == with_flag["false_park"] == 1


def test_scoped_readiness_pools_inferred_pairs_only_with_the_flag() -> None:
    records = _pair("A", ruling_action=tr.PARK_ACTION, human_action="park", inferred=True)
    plans = [("default", "forge", records)]

    default = tr.scoped_readiness(plans, tr.SCOPE_DEPLOYMENT)[tr.SCOPE_DEPLOYMENT][_CLASS]
    flagged = tr.scoped_readiness(plans, tr.SCOPE_DEPLOYMENT, include_inferred=True)[
        tr.SCOPE_DEPLOYMENT
    ][_CLASS]

    assert default["paired_count"] == 0
    assert default["paired_inferred"] == 1
    assert flagged["paired_count"] == 1


def test_thresholds_need_twenty_inferred_pairs_only_with_the_flag() -> None:
    records: list[dict] = []
    for index in range(20):
        records += _pair(f"P{index}", ruling_action=tr.PARK_ACTION, human_action="park", inferred=True)

    assert _cls(tr.readiness(records))["eligible"] is False
    assert _cls(tr.readiness(records, include_inferred=True))["eligible"] is True


# --- the command line -------------------------------------------------------


class _FakeRecords:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store

    def list_namespaces(self) -> list[str]:
        return ["default"]

    def list_plans(self, namespace: str) -> list[str]:
        return sorted(self._store._plans)

    def get_decisions(self, namespace: str, plan_name: str) -> list[dict]:
        return self._store._plans.get(plan_name, [])


class _FakeStore:
    def __init__(self, plans: dict[str, list[dict]]) -> None:
        self._plans = plans

    def get_decisions(self, plan_name: str) -> list[dict]:
        return self._plans.get(plan_name, [])

    def records(self) -> _FakeRecords:
        return _FakeRecords(self)


@pytest.fixture
def store(monkeypatch) -> _FakeStore:
    fake = _FakeStore({"forge": _pair("A", ruling_action=tr.PARK_ACTION, human_action="park", inferred=True)})
    monkeypatch.setattr(server, "_store", fake)
    return fake


def test_cli_reports_inferred_pairs_apart_by_default(store, capsys) -> None:
    assert tr.main(["--plan", "forge", "--json"]) == 0

    verdict = _cls(json.loads(capsys.readouterr().out)["forge"])
    assert verdict["paired_inferred"] == 1
    assert verdict["paired_count"] == 0


def test_cli_include_inferred_flag_counts_inferred_pairs(store, capsys) -> None:
    assert tr.main(["--plan", "forge", "--json", "--include-inferred"]) == 0

    verdict = _cls(json.loads(capsys.readouterr().out)["forge"])
    assert verdict["paired_count"] == 1
